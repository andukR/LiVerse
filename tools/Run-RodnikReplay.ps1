<#
Run every prepared Rodnik sermon WAV from a replay ISO.

The ISO contains this script, Invoke-LiVerseReplay.ps1 and an audio directory.
The LiVerse Windows build workspace remains at C:\Build\LiVerse.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = "C:\Build\LiVerse",

    [ValidateSet("address_only", "text_only", "hybrid_auto", "hybrid_confirm")]
    [string]$CitationDetectionMode = "hybrid_confirm",

    [ValidateRange(1, 8)]
    [int]$SherpaThreads = 1
)

$ErrorActionPreference = "Stop"
$audioDirectory = Join-Path $PSScriptRoot "audio"
$replayScript = Join-Path $PSScriptRoot "Invoke-LiVerseReplay.ps1"
$audio = @(
    Get-ChildItem -LiteralPath $audioDirectory -Filter "*.wav" -File |
        Sort-Object Name |
        Select-Object -ExpandProperty FullName
)

if ($audio.Count -eq 0) {
    throw "На ISO не найдены WAV-записи: $audioDirectory"
}
if (-not (Test-Path -LiteralPath $replayScript -PathType Leaf)) {
    throw "На ISO не найден скрипт эмулятора: $replayScript"
}

& $replayScript `
    -ProjectRoot $ProjectRoot `
    -Audio $audio `
    -CitationDetectionMode $CitationDetectionMode `
    -SherpaThreads $SherpaThreads
if ($LASTEXITCODE -ne 0) {
    throw "LiVerse replay завершился с кодом $LASTEXITCODE."
}

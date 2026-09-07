<#
Replay saved sermon WAV files through the Windows LiVerse build environment.

Run from C:\Build\LiVerse after the verified source snapshot has been synced.
The script deliberately uses the Sherpa model copied into build_assets, so the
Windows Python replay uses the exact model that PyInstaller later packages.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Audio,

    [ValidateSet("address_only", "text_only", "hybrid_auto", "hybrid_confirm")]
    [string]$CitationDetectionMode = "hybrid_confirm",

    [ValidateRange(1, 8)]
    [int]$SherpaThreads = 1,

    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$Python = Join-Path $ProjectRoot ".venv-build\Scripts\python.exe"
$SherpaModel = Join-Path $ProjectRoot "build_assets\models\vosk-model-small-streaming-ru-0.54"
$ReplayScript = Join-Path $ProjectRoot "tools\replay_audio_files.py"

foreach ($path in @($Python, $SherpaModel, $ReplayScript)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Не найден обязательный файл Windows-сборки: $path"
    }
}

$arguments = @(
    $ReplayScript,
    "--run",
    "--asr-engine", "sherpa-0.54",
    "--sherpa-model", $SherpaModel,
    "--sherpa-threads", $SherpaThreads,
    "--citation-detection-mode", $CitationDetectionMode
)

foreach ($audioPath in $Audio) {
    $resolved = Resolve-Path -LiteralPath $audioPath -ErrorAction Stop
    if ([IO.Path]::GetExtension($resolved.Path).ToLowerInvariant() -ne ".wav") {
        throw "Поддерживаются подготовленные WAV-записи 16 kHz mono: $($resolved.Path)"
    }
    $arguments += "--audio"
    $arguments += $resolved.Path
}

Write-Host "LiVerse replay: Sherpa 0.54, потоков: $SherpaThreads"
Write-Host "Режим поиска цитат: $CitationDetectionMode"
Write-Host "Записей: $($Audio.Count)"
Push-Location $ProjectRoot
try {
    & $Python @arguments
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
if ($exitCode -ne 0) {
    throw "LiVerse replay завершился с кодом $exitCode."
}

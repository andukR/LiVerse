param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python was not found: $venvPython"
}
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install LiVerse requirements." }
& $venvPython -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw "Could not install LiVerse in editable mode." }

& $venvPython -c "import sherpa_onnx, bible_parser_core; print('LiVerse speech engine OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the sherpa-onnx speech recognition library."
}

$modelDir = Join-Path $PSScriptRoot ".cache\liverse\models\vosk-model-small-streaming-ru-0.54"
$modelCode = "import sys; from pathlib import Path; from bible_parser_core.sherpa_streaming import ensure_sherpa_model; ensure_sherpa_model(Path(sys.argv[1]))"
& $venvPython -c $modelCode $modelDir
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the Vosk 0.54 model."
}

if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env was created from .env.example. Add HOLYRICS_TOKEN and check HOLYRICS_PORT."
}

$pythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
$guiScript = Join-Path $PSScriptRoot "tools\liverse_gui.py"
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "LiVerse.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $guiScript + '"'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = "Start LiVerse"
$icon = Join-Path $PSScriptRoot "LiVerse.ico"
if (Test-Path $icon) {
    $shortcut.IconLocation = $icon
}
$shortcut.Save()

Write-Host ""
Write-Host "Done. Start LiVerse from the desktop shortcut."
Write-Host "For console diagnostics, use run-liverse.cmd."

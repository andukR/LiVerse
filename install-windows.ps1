param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}

$pip = Join-Path $PSScriptRoot ".venv\Scripts\pip.exe"
& $pip install --upgrade pip
& $pip install -r requirements.txt
& $pip install -e .

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPython -c "import sherpa_onnx, bible_parser_core; print('LiVerse speech engine OK')"
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось установить библиотеку распознавания речи sherpa-onnx."
}

$modelDir = Join-Path $PSScriptRoot ".cache\liverse\models\vosk-model-small-streaming-ru-0.54"
$modelCode = "import sys; from pathlib import Path; from bible_parser_core.sherpa_streaming import ensure_sherpa_model; ensure_sherpa_model(Path(sys.argv[1]))"
& $venvPython -c $modelCode $modelDir
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось установить модель Vosk 0.54."
}

if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env создан из .env.example. Вставьте HOLYRICS_TOKEN и проверьте HOLYRICS_PORT."
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
Write-Host "Готово. Запускайте LiVerse ярлыком на рабочем столе."
Write-Host "Для диагностики с консолью используйте run-liverse.cmd."

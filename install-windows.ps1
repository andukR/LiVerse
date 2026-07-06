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

if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host ".env создан из .env.example. Вставьте HOLYRICS_TOKEN и проверьте HOLYRICS_PORT."
}

Write-Host ""
Write-Host "Готово. Запускайте run-liverse.cmd или .venv\Scripts\liverse.exe"

param(
    [string]$InstallerPath,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$installerName = "LiVerse-Setup-1.1.0.exe"
$expectedHash = "42ef41a1852d4101a83feb5ff176a7c8ea86c02cef4c51923b28053ee62af848"

if (-not $InstallerPath) {
    $InstallerPath = Join-Path $PSScriptRoot $installerName
}

function Complete-Check([int]$ExitCode) {
    if (-not $NoPause) {
        [void](Read-Host "Нажмите Enter, чтобы закрыть окно")
    }
    exit $ExitCode
}

if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    Write-Host "ОШИБКА: установщик не найден:" -ForegroundColor Red
    Write-Host $InstallerPath
    Write-Host "Поместите скрипт рядом с файлом $installerName."
    Complete-Check 2
}

try {
    Write-Host "Проверяется файл $installerName ..."
    $actualHash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
} catch {
    Write-Host "ОШИБКА: не удалось прочитать файл." -ForegroundColor Red
    Write-Host $_.Exception.Message
    Complete-Check 3
}

if ($actualHash -eq $expectedHash) {
    Write-Host "ПРОВЕРКА ПРОЙДЕНА." -ForegroundColor Green
    Write-Host "Файл передан без ошибок и не был изменён."
    Complete-Check 0
}

Write-Host "ПРОВЕРКА НЕ ПРОЙДЕНА." -ForegroundColor Red
Write-Host "Файл повреждён или был изменён. Не запускайте его."
Write-Host "Получено:  $actualHash"
Write-Host "Ожидалось: $expectedHash"
Complete-Check 1

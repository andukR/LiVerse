param(
    [string]$InstallerPath,
    [switch]$NoPause
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Complete-Check([int]$ExitCode) {
    if (-not $NoPause) {
        [void](Read-Host "Нажмите Enter, чтобы закрыть окно")
    }
    exit $ExitCode
}

if (-not $InstallerPath) {
    $installers = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "LiVerse-Setup-*.exe" -File)
    if ($installers.Count -ne 1) {
        Write-Host "ОШИБКА: рядом с проверщиком должен находиться ровно один установщик LiVerse-Setup-*.exe." -ForegroundColor Red
        Write-Host "Найдено установщиков: $($installers.Count)"
        Complete-Check 2
    }
    $InstallerPath = $installers[0].FullName
}

$installerName = Split-Path -Leaf $InstallerPath
$checksumPath = "$InstallerPath.sha256"

if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    Write-Host "ОШИБКА: установщик не найден:" -ForegroundColor Red
    Write-Host $InstallerPath
    Complete-Check 2
}

if (-not (Test-Path -LiteralPath $checksumPath -PathType Leaf)) {
    Write-Host "ОШИБКА: файл контрольной суммы не найден:" -ForegroundColor Red
    Write-Host $checksumPath
    Write-Host "Поместите рядом проверщик, установщик и созданный сборкой файл .sha256."
    Complete-Check 2
}

try {
    $checksumText = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
    $checksumMatch = [regex]::Match($checksumText, '^(?<hash>[0-9a-fA-F]{64})(?:\s+\*?(?<name>.+))?$')
    if (-not $checksumMatch.Success) {
        throw "Неверный формат файла контрольной суммы."
    }
    $expectedHash = $checksumMatch.Groups['hash'].Value.ToLowerInvariant()
    $expectedName = $checksumMatch.Groups['name'].Value.Trim()
    if ($expectedName -and $expectedName -ne $installerName) {
        throw "Файл контрольной суммы предназначен для $expectedName, а найден $installerName."
    }
    Write-Host "Проверяется файл $installerName ..."
    $actualHash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
} catch {
    Write-Host "ОШИБКА: не удалось выполнить проверку." -ForegroundColor Red
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

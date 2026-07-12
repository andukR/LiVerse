param(
    [string]$TargetDir = (Join-Path $HOME "LiVerse"),
    [string]$RepoUrl = "https://github.com/andukR/LiVerse.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

$scriptUrl = "https://raw.githubusercontent.com/andukR/LiVerse/$Branch/update-liverse-windows.ps1"
$scriptPath = Join-Path $env:TEMP "update-liverse-windows.ps1"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $scriptUrl -OutFile $scriptPath -UseBasicParsing

& powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
    -TargetDir $TargetDir `
    -RepoUrl $RepoUrl `
    -Branch $Branch

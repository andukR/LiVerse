param(
    [string]$TargetDir = (Join-Path $HOME "LiVerse"),
    [string]$RepoUrl = "https://github.com/andukR/LiVerse.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"

$scriptUrl = "https://raw.githubusercontent.com/andukR/LiVerse/$Branch/update-liverse-windows.ps1"
$scriptPath = Join-Path $env:TEMP "update-liverse-windows.ps1"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

for ($attempt = 1; $attempt -le 4; $attempt++) {
    try {
        Invoke-WebRequest `
            -Uri $scriptUrl `
            -OutFile $scriptPath `
            -UseBasicParsing `
            -TimeoutSec 120
        break
    }
    catch {
        if ($attempt -ge 4) {
            throw
        }
        $delay = 3 * $attempt
        Write-Warning "Download failed (attempt $attempt of 4): $($_.Exception.Message)"
        Write-Host "Retrying in $delay seconds..."
        Start-Sleep -Seconds $delay
    }
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
    -TargetDir $TargetDir `
    -RepoUrl $RepoUrl `
    -Branch $Branch

param(
    [string]$TargetDir = (Join-Path $HOME "LiVerse"),
    [string]$RepoUrl = "https://github.com/andukR/LiVerse.git",
    [string]$Branch = "main",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $knownGitRoots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA) |
        Where-Object { $_ }
    $knownGitPaths = foreach ($root in $knownGitRoots) {
        $path = if ($root -eq $env:LOCALAPPDATA) {
            Join-Path $root "Programs\Git\cmd"
        } else {
            Join-Path $root "Git\cmd"
        }
        if (Test-Path $path) { $path }
    }
    $env:Path = (@($machinePath, $userPath) + $knownGitPaths) -join ";"
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-GitWithWinget {
    if (-not (Test-Command "winget")) {
        return $false
    }

    Write-Step "Installing Git with winget"
    & winget install --id Git.Git --exact --source winget --scope user --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget user install failed; retrying with default scope."
        & winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    }

    Refresh-Path
    return (Test-Command "git")
}

function Install-GitFromGitHub {
    Write-Step "Downloading Git for Windows installer"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $headers = @{ "User-Agent" = "LiVerse-Windows-Bootstrap" }
    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers $headers `
        -UseBasicParsing
    $asset = $release.assets |
        Where-Object { $_.name -like "Git-*-64-bit.exe" } |
        Select-Object -First 1

    if (-not $asset) {
        throw "Could not find Git for Windows 64-bit installer in the latest GitHub release."
    }

    $installer = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -Headers $headers -UseBasicParsing

    Write-Step "Running Git installer"
    $process = Start-Process `
        -FilePath $installer `
        -ArgumentList "/VERYSILENT /NORESTART" `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
        throw "Git installer failed with exit code $($process.ExitCode)."
    }

    Refresh-Path
    return (Test-Command "git")
}

function Ensure-Git {
    if (Test-Command "git") {
        Write-Host "Git found: $((git --version) -join ' ')"
        return
    }

    if (-not (Install-GitWithWinget) -and -not (Install-GitFromGitHub)) {
        throw "Git was not found and automatic Git installation failed."
    }

    Write-Host "Git installed: $((git --version) -join ' ')"
}

function New-PythonSpec {
    param([string]$File, [string[]]$Args)
    return [pscustomobject]@{
        File = $File
        Args = $Args
    }
}

function Get-PythonCandidates {
    if ($Python.Trim()) {
        if (Test-Path $Python) {
            return @(New-PythonSpec -File $Python -Args @())
        }

        $parts = $Python.Trim() -split "\s+"
        if ($parts.Count -eq 1) {
            return @(New-PythonSpec -File $parts[0] -Args @())
        }
        return @(New-PythonSpec -File $parts[0] -Args $parts[1..($parts.Count - 1)])
    }

    return @(
        (New-PythonSpec -File "py" -Args @("-3")),
        (New-PythonSpec -File "python" -Args @())
    )
}

function Find-Python {
    Write-Step "Checking Python 3.10+"
    foreach ($candidate in Get-PythonCandidates) {
        if (-not (Test-Command $candidate.File) -and -not (Test-Path $candidate.File)) {
            continue
        }

        $code = "import sys; print(sys.executable); print('{}.{}.{}'.format(*sys.version_info[:3])); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
        $output = & $candidate.File @($candidate.Args) -c $code 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Python found: $($output -join ' ')"
            return $candidate
        }
    }

    throw "Python 3.10+ was not found. Install Python first, then rerun this script."
}

function Sync-Repository {
    Write-Step "Downloading latest LiVerse from $RepoUrl"
    if (Test-Path $TargetDir) {
        if (-not (Test-Path (Join-Path $TargetDir ".git"))) {
            throw "Target directory exists but is not a git repository: $TargetDir"
        }

        & git -C $TargetDir fetch origin
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed." }
        & git -C $TargetDir checkout $Branch
        if ($LASTEXITCODE -ne 0) { throw "git checkout $Branch failed." }
        & git -C $TargetDir pull --ff-only origin $Branch
        if ($LASTEXITCODE -ne 0) { throw "git pull failed." }
    } else {
        & git clone --branch $Branch $RepoUrl $TargetDir
        if ($LASTEXITCODE -ne 0) { throw "git clone failed." }
    }

    $commit = (& git -C $TargetDir rev-parse --short HEAD).Trim()
    $subject = (& git -C $TargetDir log -1 --pretty=%s).Trim()
    Write-Host "LiVerse is at $commit - $subject"
}

function Install-LiVerse {
    param($PythonSpec)

    Write-Step "Creating virtual environment and installing LiVerse"
    Push-Location $TargetDir
    try {
        if (-not (Test-Path ".venv")) {
            & $PythonSpec.File @($PythonSpec.Args) -m venv .venv
            if ($LASTEXITCODE -ne 0) { throw "python -m venv failed." }
        }

        $venvPython = Join-Path $TargetDir ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPython)) {
            throw "Virtual environment Python was not created: $venvPython"
        }

        & $venvPython -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
        & $venvPython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "requirements install failed." }
        & $venvPython -m pip install -e .
        if ($LASTEXITCODE -ne 0) { throw "editable install failed." }
        & $venvPython -c "import vosk, sounddevice, bible_parser_core; print('Import check OK')"
        if ($LASTEXITCODE -ne 0) { throw "Import check failed." }

        if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
            Copy-Item ".env.example" ".env"
            Write-Host ".env created from .env.example. Put HOLYRICS_TOKEN into .env and check HOLYRICS_PORT."
        }
    } finally {
        Pop-Location
    }
}

Ensure-Git
$pythonSpec = Find-Python
Sync-Repository
Install-LiVerse -PythonSpec $pythonSpec

Write-Host ""
Write-Host "Done."
Write-Host "Project: $TargetDir"
Write-Host "Run: cd `"$TargetDir`"; .\run-liverse.cmd"

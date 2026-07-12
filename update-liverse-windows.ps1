param(
    [string]$TargetDir = (Join-Path $HOME "LiVerse"),
    [string]$RepoUrl = "https://github.com/andukR/LiVerse.git",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Fail {
    param([string]$Text)
    throw $Text
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Refresh-Path {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra = @(
        (Join-Path $env:ProgramFiles "Git\cmd"),
        (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\Scripts")
    ) | Where-Object { Test-Path $_ }

    $env:Path = (@($machinePath, $userPath) + $extra) -join ";"
}

function Ensure-Git {
    Refresh-Path
    if (Test-Command "git") {
        Write-Host "Git: $(& git --version)"
        return
    }

    if (Test-Command "winget") {
        Write-Step "Installing Git"
        & winget install --id Git.Git --exact --source winget --scope user `
            --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }

    if (-not (Test-Command "git")) {
        Install-GitFromGitHub
    }

    if (-not (Test-Command "git")) {
        Fail "Git is not installed. Install Git for Windows and run this script again."
    }
}

function Install-GitFromGitHub {
    Write-Step "Installing Git from GitHub"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $headers = @{ "User-Agent" = "LiVerse-Windows-Update" }
    $release = Invoke-RestMethod `
        -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
        -Headers $headers `
        -UseBasicParsing
    $asset = $release.assets |
        Where-Object { $_.name -like "Git-*-64-bit.exe" } |
        Select-Object -First 1

    if (-not $asset) {
        Fail "Could not find Git for Windows installer."
    }

    $installer = Join-Path $env:TEMP $asset.name
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installer -Headers $headers -UseBasicParsing

    $process = Start-Process `
        -FilePath $installer `
        -ArgumentList "/VERYSILENT /NORESTART" `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
        Fail "Git installer failed with exit code $($process.ExitCode)."
    }

    Refresh-Path
}

function Find-Python {
    Refresh-Path

    $candidates = @(
        @{ File = "py"; Args = @("-3.12") },
        @{ File = "py"; Args = @("-3.11") },
        @{ File = "py"; Args = @("-3.10") },
        @{ File = "py"; Args = @("-3") },
        @{ File = "python"; Args = @() }
    )

    foreach ($candidate in $candidates) {
        if (-not (Test-Command $candidate.File)) {
            continue
        }

        $code = "import sys; print(sys.executable); raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
        $output = & $candidate.File @($candidate.Args) -c $code 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Python: $($output -join ' ')"
            return [pscustomobject]@{
                File = $candidate.File
                Args = [string[]]$candidate.Args
            }
        }
    }

    if (Test-Command "winget") {
        Write-Step "Installing Python 3.12"
        & winget install --id Python.Python.3.12 --exact --source winget --scope user `
            --accept-package-agreements --accept-source-agreements
        Refresh-Path
        return Find-Python
    }

    Fail "Python 3.10 or newer is not installed."
}

function Backup-Env {
    $envFile = Join-Path $TargetDir ".env"
    if (Test-Path $envFile) {
        $backup = Join-Path $env:TEMP ("LiVerse.env.{0}.backup" -f ([guid]::NewGuid().ToString("N")))
        Copy-Item $envFile $backup -Force
        return $backup
    }
    return $null
}

function Restore-Env {
    param([string]$Backup)
    if ($Backup -and (Test-Path $Backup)) {
        Copy-Item $Backup (Join-Path $TargetDir ".env") -Force
        Remove-Item $Backup -Force -ErrorAction SilentlyContinue
    }
}

function Sync-Repository {
    Write-Step "Downloading the latest LiVerse"

    $envBackup = Backup-Env
    try {
        if (Test-Path $TargetDir) {
            if (-not (Test-Path (Join-Path $TargetDir ".git"))) {
                Fail "The target folder exists but is not a Git repository: $TargetDir"
            }

            # The church computer is a deployment copy, not a development copy.
            # Reset tracked files to GitHub while preserving the ignored .env file.
            & git -C $TargetDir fetch origin $Branch
            if ($LASTEXITCODE -ne 0) { Fail "git fetch failed." }

            & git -C $TargetDir checkout $Branch
            if ($LASTEXITCODE -ne 0) { Fail "git checkout failed." }

            & git -C $TargetDir reset --hard "origin/$Branch"
            if ($LASTEXITCODE -ne 0) { Fail "git reset failed." }
        }
        else {
            & git clone --branch $Branch $RepoUrl $TargetDir
            if ($LASTEXITCODE -ne 0) { Fail "git clone failed." }
        }
    }
    finally {
        Restore-Env $envBackup
    }

    $commit = (& git -C $TargetDir rev-parse --short HEAD).Trim()
    $subject = (& git -C $TargetDir log -1 --pretty=%s).Trim()
    Write-Host "LiVerse: $commit - $subject"
}

function Ensure-Venv {
    param($PythonSpec)

    $venvDir = Join-Path $TargetDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (Test-Path $venvPython) {
        & $venvPython -c "import sys; print(sys.executable)" *> $null
        $pythonOk = $LASTEXITCODE -eq 0
        & $venvPython -m pip --version *> $null
        $pipOk = $LASTEXITCODE -eq 0
        if ($pythonOk -and $pipOk) {
            return $venvPython
        }
    }

    if (Test-Path $venvDir) {
        Write-Host "Removing broken virtual environment..."
        Remove-Item $venvDir -Recurse -Force
    }

    Write-Step "Creating the virtual environment"
    & $PythonSpec.File @($PythonSpec.Args) -m venv $venvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Fail "Could not create the virtual environment."
    }

    return $venvPython
}

function Install-LiVerse {
    param([string]$VenvPython)

    Write-Step "Installing LiVerse"
    Push-Location $TargetDir
    try {
        # Do not upgrade pip here. On this Windows 10 PC, replacing pip itself
        # caused intermittent access-denied and non-zero-exit errors.
        & $VenvPython -m pip install --disable-pip-version-check setuptools
        if ($LASTEXITCODE -ne 0) { Fail "setuptools installation failed." }

        & $VenvPython -m pip install --disable-pip-version-check -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Fail "requirements installation failed." }

        # Avoid an isolated temporary build environment: it caused WinError 5.
        & $VenvPython -m pip install --disable-pip-version-check -e . --no-build-isolation
        if ($LASTEXITCODE -ne 0) { Fail "LiVerse editable installation failed." }

        & $VenvPython -c "import vosk, sounddevice, bible_parser_core; print('Import check OK')"
        if ($LASTEXITCODE -ne 0) { Fail "Import check failed." }

        if (-not (Test-Path ".env")) {
            if (Test-Path ".env.example") {
                Copy-Item ".env.example" ".env"
                Write-Warning ".env was created. Add the Holyrics token before starting LiVerse."
            }
            else {
                Fail ".env and .env.example were not found."
            }
        }
    }
    finally {
        Pop-Location
    }
}

function New-DesktopShortcut {
    Write-Step "Creating the desktop shortcut"

    $runner = Join-Path $TargetDir "run-liverse.cmd"
    if (-not (Test-Path $runner)) {
        Fail "Launcher not found: $runner"
    }

    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "LiVerse.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $runner
    $shortcut.WorkingDirectory = $TargetDir
    $shortcut.Description = "Start LiVerse"
    $shortcut.Save()

    Write-Host "Shortcut: $shortcutPath"
}

try {
    Write-Step "Preparing LiVerse for Windows 10"
    Ensure-Git
    $pythonSpec = Find-Python
    Sync-Repository
    $venvPython = Ensure-Venv -PythonSpec $pythonSpec
    Install-LiVerse -VenvPython $venvPython
    New-DesktopShortcut

    Write-Host ""
    Write-Host "LiVerse was installed or updated successfully." -ForegroundColor Green
    Write-Host "Project: $TargetDir"
    Write-Host "Start it from the LiVerse shortcut on the desktop."
}
catch {
    Write-Host ""
    Write-Host "LiVerse installation/update failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "Press Enter to close this window."
    Read-Host | Out-Null
    exit 1
}

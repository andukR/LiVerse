param(
    [string]$TargetDir = (Join-Path $HOME "LiVerse"),
    [string]$RepoUrl = "https://github.com/andukR/LiVerse.git",
    [string]$Branch = "main",
    [int]$NetworkRetries = 4,
    [string]$LogDir = (Join-Path $HOME "LiVerse-update-logs")
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$script:UpdateLogPath = $null
$script:TranscriptStarted = $false
$script:UpdateFailed = $false

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

function Start-UpdateLog {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $script:UpdateLogPath = Join-Path $LogDir "liverse-update-$timestamp.log"

    try {
        Start-Transcript -Path $script:UpdateLogPath -Force | Out-Null
        $script:TranscriptStarted = $true
        Write-Host "Update log: $script:UpdateLogPath"
        Write-Host "PowerShell: $($PSVersionTable.PSVersion)"
        Write-Host "User: $env:USERNAME"
        Write-Host "Computer: $env:COMPUTERNAME"
        Write-Host "TargetDir: $TargetDir"
        Write-Host "RepoUrl: $RepoUrl"
        Write-Host "Branch: $Branch"
    }
    catch {
        Write-Warning "Could not start update log: $($_.Exception.Message)"
    }
}

function Stop-UpdateLog {
    if ($script:TranscriptStarted) {
        try {
            Stop-Transcript | Out-Null
            $script:TranscriptStarted = $false
        }
        catch {
            Write-Warning "Could not stop update log: $($_.Exception.Message)"
        }
    }
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

function Invoke-WithRetry {
    param(
        [scriptblock]$Action,
        [string]$Description,
        [int]$Attempts = $NetworkRetries,
        [int]$InitialDelaySeconds = 3
    )

    $lastError = $null

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            & $Action
            return
        }
        catch {
            $lastError = $_
            if ($attempt -ge $Attempts) {
                break
            }

            $delay = $InitialDelaySeconds * $attempt
            Write-Warning "$Description failed (attempt $attempt of $Attempts): $($_.Exception.Message)"
            Write-Host "Retrying in $delay seconds..."
            Start-Sleep -Seconds $delay
        }
    }

    throw "$Description failed after $Attempts attempts. Last error: $($lastError.Exception.Message)"
}

function Invoke-Git {
    param(
        [string[]]$Arguments,
        [string]$Description = "Git command",
        [switch]$RetryNetwork
    )

    $action = {
        # Force HTTP/1.1 only for this Git invocation. This keeps the fix local
        # to LiVerse instead of changing the user's global Git configuration.
        & git -c http.version=HTTP/1.1 @Arguments
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "$Description exited with code $exitCode."
        }
    }

    if ($RetryNetwork) {
        Invoke-WithRetry -Action $action -Description $Description
    }
    else {
        & $action
    }
}

function Invoke-GitAndAcceptVerified {
    param(
        [string[]]$Arguments,
        [string]$Description,
        [scriptblock]$Verify
    )

    try {
        Invoke-Git -Arguments $Arguments -Description $Description
        return
    }
    catch {
        $message = $_.Exception.Message
        Write-Warning "$Description reported an error: $message"
        if (& $Verify) {
            Write-Warning "$Description appears to have completed successfully despite the Git process error. Continuing."
            return
        }
        throw
    }
}

function Get-GitOriginUrl {
    $origin = (& git -C $TargetDir config --get remote.origin.url 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return ""
    }
    return (($origin | Select-Object -First 1) -as [string]).Trim()
}

function Test-OriginUrl {
    return (Get-GitOriginUrl) -eq $RepoUrl.Trim()
}

function Test-RepositoryAtOrigin {
    $head = (& git -C $TargetDir rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    $originHead = (& git -C $TargetDir rev-parse "origin/$Branch" 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    return ((($head | Select-Object -First 1) -as [string]).Trim()) -eq ((($originHead | Select-Object -First 1) -as [string]).Trim())
}

function Invoke-WebRequestWithRetry {
    param(
        [string]$Uri,
        [string]$OutFile,
        [hashtable]$Headers = @{}
    )

    Invoke-WithRetry -Description "Download from $Uri" -Action {
        Invoke-WebRequest `
            -Uri $Uri `
            -OutFile $OutFile `
            -Headers $Headers `
            -UseBasicParsing `
            -TimeoutSec 120
    }
}

function Ensure-Git {
    Refresh-Path
    if (Test-Command "git") {
        $gitCommand = Get-Command git
        Write-Host "Git: $(& git --version)"
        Write-Host "Git executable: $($gitCommand.Source)"
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

    Write-Host "Git: $(& git --version)"
    Write-Host "Git executable: $((Get-Command git).Source)"
}

function Install-GitFromGitHub {
    Write-Step "Installing Git from GitHub"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $headers = @{ "User-Agent" = "LiVerse-Windows-Update" }
    $release = Invoke-WithRetry -Description "Reading the latest Git for Windows release" -Action {
        Invoke-RestMethod `
            -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" `
            -Headers $headers `
            -UseBasicParsing `
            -TimeoutSec 120
    }

    $asset = $release.assets |
        Where-Object { $_.name -like "Git-*-64-bit.exe" } |
        Select-Object -First 1

    if (-not $asset) {
        Fail "Could not find Git for Windows installer."
    }

    $installer = Join-Path $env:TEMP $asset.name
    Invoke-WebRequestWithRetry `
        -Uri $asset.browser_download_url `
        -OutFile $installer `
        -Headers $headers

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

            $currentOriginText = Get-GitOriginUrl
            if (-not $currentOriginText) {
                Fail "The Git repository has no usable 'origin' remote."
            }

            Write-Host "Git origin URL: $currentOriginText"

            if ($currentOriginText -ne $RepoUrl.Trim()) {
                Write-Warning "Correcting origin URL: $currentOriginText -> $RepoUrl"
                Invoke-GitAndAcceptVerified `
                    -Arguments @("-C", $TargetDir, "remote", "set-url", "origin", $RepoUrl) `
                    -Description "Updating origin URL" `
                    -Verify { Test-OriginUrl }
            }

            # Only one network operation is needed. Do not use git pull here:
            # pull performs another fetch and caused intermittent HTTPS failures.
            $remoteRef = "+refs/heads/$($Branch):refs/remotes/origin/$($Branch)"
            Invoke-Git `
                -Arguments @("-C", $TargetDir, "fetch", "--prune", "origin", $remoteRef) `
                -Description "git fetch origin $Branch" `
                -RetryNetwork

            if (Test-RepositoryAtOrigin) {
                Write-Host "Repository is already at origin/$Branch. Skipping checkout/reset."
                return
            }

            # The church computer is a deployment copy, not a development copy.
            # Reset tracked files to the fetched GitHub state while preserving .env.
            Invoke-GitAndAcceptVerified `
                -Arguments @("-C", $TargetDir, "checkout", "-B", $Branch, "origin/$Branch") `
                -Description "Checking out $Branch" `
                -Verify { Test-RepositoryAtOrigin }

            Invoke-GitAndAcceptVerified `
                -Arguments @("-C", $TargetDir, "reset", "--hard", "origin/$Branch") `
                -Description "Resetting files to origin/$Branch" `
                -Verify { Test-RepositoryAtOrigin }
        }
        else {
            $parent = Split-Path -Parent $TargetDir
            if ($parent -and -not (Test-Path $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }

            Invoke-WithRetry -Description "git clone" -Action {
                if (Test-Path $TargetDir) {
                    Remove-Item $TargetDir -Recurse -Force
                }

                Invoke-Git `
                    -Arguments @("clone", "--branch", $Branch, "--single-branch", $RepoUrl, $TargetDir) `
                    -Description "Cloning LiVerse"
            }
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
    param(
        $PythonSpec,
        [switch]$Force
    )

    $venvDir = Join-Path $TargetDir ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if ((-not $Force) -and (Test-Path $venvPython)) {
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
    & $PythonSpec.File @($PythonSpec.Args) -m venv $venvDir --without-pip
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) {
        Fail "Could not create the virtual environment."
    }

    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        Fail "Could not install pip into the virtual environment."
    }

    & $venvPython -m pip --version
    if ($LASTEXITCODE -ne 0) {
        Fail "pip is not working in the virtual environment."
    }

    return $venvPython
}

function Test-PythonImports {
    param(
        [string]$VenvPython,
        [string]$Imports,
        [string]$Description
    )

    & $VenvPython -c $Imports
    if ($LASTEXITCODE -eq 0) {
        Write-Warning "$Description failed, but the required imports already work. Continuing."
        return $true
    }
    return $false
}

function Invoke-PipInstallOrVerify {
    param(
        [string]$VenvPython,
        [string[]]$PipArgs,
        [string]$Description,
        [string]$VerifyImports
    )

    & $VenvPython -m pip @PipArgs
    if ($LASTEXITCODE -eq 0) {
        return
    }

    if (Test-PythonImports -VenvPython $VenvPython -Imports $VerifyImports -Description $Description) {
        return
    }

    Fail "$Description failed."
}

function Install-LiVerse {
    param([string]$VenvPython)

    Write-Step "Installing LiVerse"
    Push-Location $TargetDir
    try {
        # Do not upgrade pip here. On this Windows 10 PC, replacing pip itself
        # caused intermittent access-denied and non-zero-exit errors.
        Invoke-PipInstallOrVerify `
            -VenvPython $VenvPython `
            -PipArgs @("install", "--disable-pip-version-check", "setuptools") `
            -Description "setuptools installation" `
            -VerifyImports "import setuptools; print('setuptools import OK')"

        Invoke-PipInstallOrVerify `
            -VenvPython $VenvPython `
            -PipArgs @("install", "--disable-pip-version-check", "-r", "requirements.txt") `
            -Description "requirements installation" `
            -VerifyImports "import vosk, sounddevice, qrcode, pysword; print('requirements import OK')"

        # Avoid an isolated temporary build environment: it caused WinError 5.
        Invoke-PipInstallOrVerify `
            -VenvPython $VenvPython `
            -PipArgs @("install", "--disable-pip-version-check", "-e", ".", "--no-build-isolation") `
            -Description "LiVerse editable installation" `
            -VerifyImports "import bible_parser_core; print('bible_parser_core import OK')"

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
    $icon = Join-Path $TargetDir "LiVerse.ico"
    if (Test-Path $icon) {
        $shortcut.IconLocation = $icon
    }
    $shortcut.Save()

    Write-Host "Shortcut: $shortcutPath"
}

Start-UpdateLog

try {
    Write-Step "Preparing LiVerse for Windows 10"
    Ensure-Git
    $pythonSpec = Find-Python
    Sync-Repository
    $venvPython = Ensure-Venv -PythonSpec $pythonSpec
    try {
        Install-LiVerse -VenvPython $venvPython
    }
    catch {
        Write-Warning "LiVerse installation failed with the current virtual environment: $($_.Exception.Message)"
        Write-Warning "Recreating .venv and retrying once."
        $venvPython = Ensure-Venv -PythonSpec $pythonSpec -Force
        Install-LiVerse -VenvPython $venvPython
    }
    New-DesktopShortcut

    Write-Host ""
    Write-Host "LiVerse was installed or updated successfully." -ForegroundColor Green
    Write-Host "Project: $TargetDir"
    Write-Host "Start it from the LiVerse shortcut on the desktop."
    if ($script:UpdateLogPath) {
        Write-Host "Update log: $script:UpdateLogPath"
    }
}
catch {
    Write-Host ""
    Write-Host "LiVerse installation/update failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    Write-Host "The detailed Git or network error should be visible above."
    if ($script:UpdateLogPath) {
        Write-Host "Send this update log for diagnostics:"
        Write-Host $script:UpdateLogPath
    }
    Write-Host "Press Enter to close this window."
    Read-Host | Out-Null
    $script:UpdateFailed = $true
}
finally {
    Stop-UpdateLog
}

if ($script:UpdateFailed) {
    exit 1
}

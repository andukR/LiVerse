#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUTPUT_DIR="$PROJECT_ROOT/.windows-sync"
ISO_PATH="$OUTPUT_DIR/liverse-source.iso"
VM_NAME="win10"
VM_URI="qemu:///system"
VM_CDROM="sdc"
WINDOWS_DEST='C:\Build\LiVerse'
BIBLE_INDEX_ASSET='bible_index/bible_index.db'
SHERPA_MODEL_NAME='vosk-model-small-streaming-ru-0.54'
SHERPA_MODEL_ASSET=".cache/liverse/models/$SHERPA_MODEL_NAME"
SHERPA_MODEL_SNAPSHOT="build_assets/models/$SHERPA_MODEL_NAME"
INNO_SETUP_VERSION='6.7.3'
INNO_SETUP_NAME="innosetup-$INNO_SETUP_VERSION.exe"
INNO_SETUP_ASSET="$OUTPUT_DIR/tools/$INNO_SETUP_NAME"
INNO_SETUP_SHA256='9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732'
PREPARE_ONLY=false
RUN_TESTS=false
BUILD_ENGINE=false
BUILD_INSTALLER=false
RELEASE_VERSION=''
UPGRADE_FROM_INSTALLER=''
UPGRADE_FROM_NAME=''
UPGRADE_FROM_SHA256=''
GUEST_TIMEOUT_SECONDS=1200

usage() {
    cat <<'EOF'
Usage: tools/sync_windows_build.sh [options]

Create a conservative LiVerse source snapshot and expose it to the Windows VM
as a separate read-only CD-ROM.

Options:
  --prepare-only        Build the ISO without changing the VM configuration.
  --run-tests           Sync the snapshot and run regression tests in the
                        offline Windows VM through QEMU Guest Agent.
  --build-engine        Run both test suites and build/test LiVerseEngine
                        as a PyInstaller onedir directory in the Windows VM.
  --build-installer     Build/test the onedir application, then create and
                        clean-install test the Inno Setup installer.
  --release-version N   Require clean main and exact application version N;
                        implies --build-installer.
  --upgrade-from-installer PATH
                        Test installing the new version over this previous
                        LiVerse installer; implies --build-installer.
  --iso PATH            Write the source ISO to PATH.
  --vm NAME             Libvirt VM name (default: win10).
  --destination PATH    Windows copy destination (default: C:\Build\LiVerse).
  -h, --help            Show this help.
EOF
}

while (($#)); do
    case "$1" in
        --prepare-only)
            PREPARE_ONLY=true
            shift
            ;;
        --run-tests)
            RUN_TESTS=true
            shift
            ;;
        --build-engine)
            BUILD_ENGINE=true
            RUN_TESTS=true
            shift
            ;;
        --build-installer)
            BUILD_INSTALLER=true
            BUILD_ENGINE=true
            RUN_TESTS=true
            shift
            ;;
        --release-version)
            RELEASE_VERSION=${2:?missing value for --release-version}
            BUILD_INSTALLER=true
            BUILD_ENGINE=true
            RUN_TESTS=true
            shift 2
            ;;
        --upgrade-from-installer)
            UPGRADE_FROM_INSTALLER=${2:?missing value for --upgrade-from-installer}
            BUILD_INSTALLER=true
            BUILD_ENGINE=true
            RUN_TESTS=true
            shift 2
            ;;
        --iso)
            ISO_PATH=${2:?missing value for --iso}
            shift 2
            ;;
        --vm)
            VM_NAME=${2:?missing value for --vm}
            shift 2
            ;;
        --destination)
            WINDOWS_DEST=${2:?missing value for --destination}
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -n "$UPGRADE_FROM_INSTALLER" ]]; then
    if [[ ! -f "$UPGRADE_FROM_INSTALLER" ]]; then
        printf 'Previous LiVerse installer was not found: %s\n' "$UPGRADE_FROM_INSTALLER" >&2
        exit 1
    fi
    UPGRADE_FROM_NAME=$(basename "$UPGRADE_FROM_INSTALLER")
    if [[ ! "$UPGRADE_FROM_NAME" =~ ^LiVerse-Setup-[0-9]+\.[0-9]+\.[0-9]+\.exe$ ]]; then
        printf 'Previous installer has an unexpected name: %s\n' "$UPGRADE_FROM_NAME" >&2
        exit 1
    fi
    UPGRADE_FROM_SHA256=$(sha256sum "$UPGRADE_FROM_INSTALLER" | awk '{print $1}')
fi

REQUIRED_COMMANDS=(git rsync xorriso sha256sum find du awk sed python3)

for command in "${REQUIRED_COMMANDS[@]}"; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is not installed: $command" >&2
        exit 1
    fi
done

if [[ -n "$RELEASE_VERSION" ]]; then
    CURRENT_BRANCH=$(git -C "$PROJECT_ROOT" branch --show-current)
    if [[ "$CURRENT_BRANCH" != main ]]; then
        printf 'Release build requires branch main, current branch: %s\n' "${CURRENT_BRANCH:-DETACHED}" >&2
        exit 1
    fi
    RELEASE_GIT_STATUS=$(git -C "$PROJECT_ROOT" status --porcelain=v1 --untracked-files=all)
    if [[ -n "$RELEASE_GIT_STATUS" ]]; then
        printf 'Release build requires a clean Git working tree. Current changes:\n%s\n' \
            "$RELEASE_GIT_STATUS" >&2
        exit 1
    fi
    CURRENT_VERSION=$(python3 - "$PROJECT_ROOT/packages/bible_parser_core/src/bible_parser_core/version.py" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.search(r'__version__\s*=\s*["\x27](\d+\.\d+\.\d+)["\x27]', text)
if not match:
    raise SystemExit("LiVerse version was not found")
print(match.group(1))
PY
)
    if [[ "$CURRENT_VERSION" != "$RELEASE_VERSION" ]]; then
        printf 'Release version mismatch: requested %s, source contains %s.\n' \
            "$RELEASE_VERSION" "$CURRENT_VERSION" >&2
        exit 1
    fi
fi

run_guest_powershell() {
    local description=$1
    local powershell_command=$2
    local request response pid status exited exit_code
    local started_at=$SECONDS

    request=$(python3 -c '
import json
import sys

print(json.dumps({
    "execute": "guest-exec",
    "arguments": {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", sys.argv[1]],
        "capture-output": True,
    },
}))
' "$powershell_command")
    response=$(virsh -c "$VM_URI" qemu-agent-command "$VM_NAME" "$request")
    pid=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["return"]["pid"])' "$response")
    printf 'Windows VM: %s (PID %s)\n' "$description" "$pid"

    while true; do
        status=$(virsh -c "$VM_URI" qemu-agent-command "$VM_NAME" \
            "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}")
        exited=$(python3 -c 'import json, sys; print(str(json.loads(sys.argv[1])["return"].get("exited", False)).lower())' "$status")
        if [[ "$exited" == true ]]; then
            python3 -c '
import base64
import json
import sys

result = json.loads(sys.argv[1])["return"]
if result.get("out-data"):
    sys.stdout.buffer.write(base64.b64decode(result["out-data"]))
if result.get("err-data"):
    sys.stderr.buffer.write(base64.b64decode(result["err-data"]))
' "$status"
            exit_code=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["return"].get("exitcode", 1))' "$status")
            printf 'Windows VM: %s exit code: %s\n' "$description" "$exit_code"
            return "$exit_code"
        fi
        if ((SECONDS - started_at >= GUEST_TIMEOUT_SECONDS)); then
            printf 'Windows VM: timed out after %s seconds: %s\n' \
                "$GUEST_TIMEOUT_SECONDS" "$description" >&2
            return 124
        fi
        sleep 1
    done
}

verify_vm_is_offline() {
    local interface link_state
    while IFS= read -r interface; do
        [[ -n "$interface" ]] || continue
        link_state=$(virsh -c "$VM_URI" domif-getlink "$VM_NAME" "$interface" | awk '{print $NF}' | tr -d '\r')
        if [[ "$link_state" != down ]]; then
            printf 'Refusing to run Windows tests: VM network interface %s is %s, expected down.\n' \
                "$interface" "$link_state" >&2
            return 1
        fi
    done < <(virsh -c "$VM_URI" domiflist "$VM_NAME" | awk 'NR > 2 && NF {print $1}')
}

mkdir -p "$OUTPUT_DIR" "$(dirname "$ISO_PATH")"
STAGE_ROOT=$(mktemp -d "$OUTPUT_DIR/stage.XXXXXX")
trap 'rm -rf -- "$STAGE_ROOT"' EXIT
SNAPSHOT_DIR="$STAGE_ROOT/snapshot"
mkdir -p "$SNAPSHOT_DIR"

copy_file() {
    local relative=$1
    local source="$PROJECT_ROOT/$relative"
    if [[ -f "$source" ]]; then
        mkdir -p "$SNAPSHOT_DIR/$(dirname "$relative")"
        cp -a -- "$source" "$SNAPSHOT_DIR/$relative"
    fi
}

copy_tree() {
    local relative=$1
    local source="$PROJECT_ROOT/$relative"
    if [[ -d "$source" ]]; then
        mkdir -p "$SNAPSHOT_DIR/$relative"
        rsync -a \
            --exclude='.venv/' \
            --exclude='venv/' \
            --exclude='__pycache__/' \
            --exclude='.pytest_cache/' \
            --exclude='.mypy_cache/' \
            --exclude='.ruff_cache/' \
            --exclude='.cache/' \
            --exclude='build/' \
            --exclude='dist/' \
            --exclude='*.egg-info/' \
            --exclude='*.pyc' \
            --exclude='*.pyo' \
            --exclude='*.log' \
            --exclude='*.wav' \
            --exclude='*.part' \
            --exclude='.env' \
            "$source/" "$SNAPSHOT_DIR/$relative/"
    fi
}

ROOT_FILES=(
    .env.example
    .gitignore
    CHANGELOG.md
    LICENSE
    LiVerse.ico
    LiVerse.png
    Makefile
    README.md
    THIRD_PARTY_NOTICES.md
    bootstrap-windows.ps1
    install-windows.ps1
    pyproject.toml
    requirements.txt
    run-liverse.cmd
    update-liverse-windows.cmd
    update-liverse-windows.ps1
)

for relative in "${ROOT_FILES[@]}"; do
    copy_file "$relative"
done

for pattern in 'requirements*.txt' 'constraints*.txt' '*.spec' '*.iss'; do
    while IFS= read -r -d '' source; do
        copy_file "${source#"$PROJECT_ROOT/"}"
    done < <(find "$PROJECT_ROOT" -maxdepth 1 -type f -name "$pattern" -print0)
done

copy_tree .github
copy_tree assets
copy_tree docs
copy_tree slide_display
copy_tree tools
copy_tree packages/bible_parser_core/src/bible_parser_core
copy_tree packages/bible_parser_core/tests

if [[ ! -f "$PROJECT_ROOT/$BIBLE_INDEX_ASSET" ]]; then
    echo "Required Windows build asset is missing: $BIBLE_INDEX_ASSET" >&2
    exit 1
fi
copy_file "$BIBLE_INDEX_ASSET"

python3 - "$PROJECT_ROOT" "$PROJECT_ROOT/$SHERPA_MODEL_ASSET" <<'PY'
import sys
from pathlib import Path

project_root = Path(sys.argv[1])
model_root = Path(sys.argv[2])
sys.path.insert(0, str(project_root / "packages" / "bible_parser_core" / "src"))

from bible_parser_core.sherpa_streaming import SHERPA_MODEL_FILES, file_sha256

failures = []
for relative, expected in SHERPA_MODEL_FILES.items():
    path = model_root / relative
    if not path.is_file():
        failures.append(f"missing: {path}")
    elif file_sha256(path) != expected:
        failures.append(f"SHA-256 mismatch: {path}")
if failures:
    raise SystemExit("Invalid Sherpa 0.54 build asset:\n  - " + "\n  - ".join(failures))
print(f"Sherpa 0.54 build asset verified: {len(SHERPA_MODEL_FILES)} files.")
PY
mkdir -p "$SNAPSHOT_DIR/$SHERPA_MODEL_SNAPSHOT"
rsync -a --exclude='*.part' \
    "$PROJECT_ROOT/$SHERPA_MODEL_ASSET/" \
    "$SNAPSHOT_DIR/$SHERPA_MODEL_SNAPSHOT/"

if [[ "$BUILD_INSTALLER" == true ]]; then
    if [[ ! -f "$INNO_SETUP_ASSET" ]]; then
        printf 'Required Inno Setup build tool is missing: %s\n' "$INNO_SETUP_ASSET" >&2
        exit 1
    fi
    printf '%s  %s\n' "$INNO_SETUP_SHA256" "$INNO_SETUP_ASSET" | sha256sum --check --status
    mkdir -p "$STAGE_ROOT/build-tools"
    cp -a -- "$INNO_SETUP_ASSET" "$STAGE_ROOT/build-tools/$INNO_SETUP_NAME"
fi

if [[ -n "$UPGRADE_FROM_INSTALLER" ]]; then
    mkdir -p "$STAGE_ROOT/previous-installer"
    cp -a -- "$UPGRADE_FROM_INSTALLER" \
        "$STAGE_ROOT/previous-installer/LiVerse-Setup-previous.exe"
    printf '%s\n' "$UPGRADE_FROM_SHA256" \
        > "$STAGE_ROOT/previous-installer/LiVerse-Setup-previous.exe.sha256"
fi

# These source/provenance files are not needed to run or package LiVerse.
rm -rf -- "$SNAPSHOT_DIR/packages/bible_parser_core/src/bible_parser_core/data/archive"
rm -f -- "$SNAPSHOT_DIR/packages/bible_parser_core/src/bible_parser_core/data/sword_russinodal.json"

for optional_dir in packaging installer windows; do
    copy_tree "$optional_dir"
done

COMMIT=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
BRANCH=$(git -C "$PROJECT_ROOT" branch --show-current)
[[ -n "$BRANCH" ]] || BRANCH='DETACHED'
GIT_STATUS=$(git -C "$PROJECT_ROOT" status --porcelain=v1 --untracked-files=all)
if [[ -n "$GIT_STATUS" ]]; then
    DIRTY=true
else
    DIRTY=false
fi
TIMESTAMP_UTC=$(date -u +'%Y-%m-%dT%H:%M:%SZ')

{
    printf 'commit=%s\n' "$COMMIT"
    printf 'branch=%s\n' "$BRANCH"
    printf 'dirty=%s\n' "$DIRTY"
    printf 'timestamp_utc=%s\n' "$TIMESTAMP_UTC"
    printf 'windows_destination=%s\n' "$WINDOWS_DEST"
    printf 'snapshot_policy=explicit source and build assets; no runtime cache, env, logs, build or dist\n'
    printf 'included_build_asset=%s\n' "$BIBLE_INDEX_ASSET"
    printf 'included_build_asset=%s -> %s\n' "$SHERPA_MODEL_ASSET" "$SHERPA_MODEL_SNAPSHOT"
    printf 'included_ignored_test_data=packages/bible_parser_core/tests/parser_regression_cases.json\n'
    if [[ -n "$UPGRADE_FROM_INSTALLER" ]]; then
        printf 'external_upgrade_installer=%s\n' "$UPGRADE_FROM_NAME"
        printf 'external_upgrade_installer_sha256=%s\n' "$UPGRADE_FROM_SHA256"
    fi
    printf '\ngit_diff_stat:\n'
    git -C "$PROJECT_ROOT" diff --stat HEAD
    printf '\ngit_status_porcelain:\n'
    if [[ -n "$GIT_STATUS" ]]; then
        printf '%s\n' "$GIT_STATUS"
    else
        printf '(clean)\n'
    fi
} > "$SNAPSHOT_DIR/BUILD_SOURCE_INFO.txt"

(
    cd "$SNAPSHOT_DIR"
    find . -type f ! -name BUILD_SOURCE_MANIFEST.sha256 -print0 \
        | sort -z \
        | xargs -0 sha256sum \
        > BUILD_SOURCE_MANIFEST.sha256
)

cat > "$STAGE_ROOT/sync_to_workspace.cmd" <<EOF
@echo off
setlocal EnableExtensions
set "SOURCE=%~dp0snapshot"
set "DEST=$WINDOWS_DEST"

if /I "%DEST%"=="C:\Projects\live_verse_vosk" (
  echo Refusing to overwrite the verified Windows working copy.
  exit /b 20
)

if not exist "%DEST%" mkdir "%DEST%"
robocopy "%SOURCE%" "%DEST%" /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /XJ /NP
set "RESULT=%ERRORLEVEL%"
if %RESULT% GEQ 8 (
  echo Synchronization failed with robocopy code %RESULT%.
  exit /b %RESULT%
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify_snapshot.ps1" -Destination "%DEST%"
if errorlevel 1 (
  echo Snapshot hash verification failed.
  exit /b 21
)

echo.
echo LiVerse source snapshot copied successfully.
echo Destination: %DEST%
echo Source marker: %DEST%\BUILD_SOURCE_INFO.txt
echo Existing extra files were not deleted.
exit /b 0
EOF

cat > "$STAGE_ROOT/verify_snapshot.ps1" <<'EOF'
param(
    [Parameter(Mandatory = $true)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
$manifest = Join-Path $Destination "BUILD_SOURCE_MANIFEST.sha256"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
    throw "Snapshot manifest was not found: $manifest"
}

$checked = 0
foreach ($line in Get-Content -LiteralPath $manifest) {
    if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
        throw "Invalid manifest line: $line"
    }
    $expected = $Matches[1].ToLowerInvariant()
    $relative = $Matches[2] -replace '^\./', ''
    $path = Join-Path $Destination $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Snapshot file is missing: $relative"
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Snapshot hash mismatch: $relative"
    }
    $checked += 1
}

Write-Host "Snapshot hash verification passed: $checked files."
EOF

cat > "$STAGE_ROOT/README_FIRST.txt" <<EOF
LiVerse Debian-to-Windows source snapshot

1. Run sync_to_workspace.cmd from this CD-ROM.
2. The snapshot is copied to: $WINDOWS_DEST
3. The verified working copy C:\Projects\live_verse_vosk is not modified.
4. Existing extra files in the build workspace are not deleted.
5. Every copied snapshot file is checked against its SHA-256 manifest.
6. Inspect BUILD_SOURCE_INFO.txt in the destination before building.
EOF

TEMP_ISO="$ISO_PATH.tmp"
rm -f -- "$TEMP_ISO"
xorriso -as mkisofs \
    -iso-level 3 \
    -J \
    -R \
    -V LIVERSE_SOURCE \
    -o "$TEMP_ISO" \
    "$STAGE_ROOT" \
    >/dev/null 2>&1
mv -f -- "$TEMP_ISO" "$ISO_PATH"
chmod 0644 "$ISO_PATH"

FILE_COUNT=$(find "$SNAPSHOT_DIR" -type f | wc -l | awk '{print $1}')
SNAPSHOT_BYTES=$(du -sb "$SNAPSHOT_DIR" | awk '{print $1}')
ISO_BYTES=$(du -b "$ISO_PATH" | awk '{print $1}')

ATTACH_STATUS='prepared only'
if [[ "$PREPARE_ONLY" == false ]]; then
    if ! command -v virsh >/dev/null 2>&1; then
        echo "virsh is required unless --prepare-only is used" >&2
        exit 1
    fi
    VM_STATE=$(virsh -c "$VM_URI" domstate "$VM_NAME" | tr -d '\r')
    if virsh -c "$VM_URI" domblklist "$VM_NAME" --details | awk -v target="$VM_CDROM" '$3 == target {found=1} END {exit !found}'; then
        if [[ "$VM_STATE" == 'running' ]]; then
            # The ISO is atomically replaced at the same path. Explicitly eject
            # it first so QEMU cannot keep serving the previous open file.
            virsh -c "$VM_URI" change-media "$VM_NAME" "$VM_CDROM" --eject --live --config --force >/dev/null
            virsh -c "$VM_URI" change-media "$VM_NAME" "$VM_CDROM" "$ISO_PATH" --insert --live --config >/dev/null
        else
            virsh -c "$VM_URI" change-media "$VM_NAME" "$VM_CDROM" "$ISO_PATH" --update --config >/dev/null
        fi
        ATTACH_STATUS="updated $VM_CDROM on $VM_NAME"
    else
        if [[ "$VM_STATE" == 'running' ]]; then
            virsh -c "$VM_URI" attach-disk "$VM_NAME" "$ISO_PATH" "$VM_CDROM" \
                --type cdrom --mode readonly --targetbus sata --live --config >/dev/null
        else
            virsh -c "$VM_URI" attach-disk "$VM_NAME" "$ISO_PATH" "$VM_CDROM" \
                --type cdrom --mode readonly --targetbus sata --config >/dev/null
        fi
        ATTACH_STATUS="attached $VM_CDROM to $VM_NAME"
    fi
fi

if [[ "$RUN_TESTS" == true ]]; then
    if [[ "$PREPARE_ONLY" == true ]]; then
        echo '--run-tests cannot be combined with --prepare-only' >&2
        exit 2
    fi
    if [[ "$VM_STATE" != 'running' ]]; then
        printf 'Windows VM %s must be running for --run-tests.\n' "$VM_NAME" >&2
        exit 1
    fi
    verify_vm_is_offline
    if ! virsh -c "$VM_URI" qemu-agent-command "$VM_NAME" '{"execute":"guest-ping"}' >/dev/null; then
        printf 'QEMU Guest Agent is not available in Windows VM %s.\n' "$VM_NAME" >&2
        exit 1
    fi

    run_guest_powershell 'source snapshot sync' '
$ErrorActionPreference = "Stop"
$deadline = (Get-Date).AddSeconds(30)
do {
    $volume = Get-Volume -FileSystemLabel "LIVERSE_SOURCE" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($volume -and $volume.DriveLetter) { break }
    Start-Sleep -Seconds 1
} while ((Get-Date) -lt $deadline)
if (-not $volume -or -not $volume.DriveLetter) {
    throw "LIVERSE_SOURCE CD-ROM was not found"
}
$syncScript = "$($volume.DriveLetter):\sync_to_workspace.cmd"
& $syncScript
exit $LASTEXITCODE
'

    run_guest_powershell 'LiVerse regression tests' '
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Set-Location "C:\Build\LiVerse"
& ".\.venv-build\Scripts\python.exe" -m unittest discover -s "packages\bible_parser_core\tests" -p "test_*.py" -q
exit $LASTEXITCODE
'

    if [[ "$BUILD_ENGINE" == true ]]; then
        run_guest_powershell 'LiVerseEngine onedir build' '
$ErrorActionPreference = "Stop"
Set-Location "C:\Build\LiVerse"
& ".\.venv-build\Scripts\python.exe" -m PyInstaller --noconfirm --clean ".\LiVerseEngine.spec"
exit $LASTEXITCODE
'

        run_guest_powershell 'LiVerseEngine smoke tests' '
$ErrorActionPreference = "Stop"
$root = "C:\Build\LiVerse"
$dist = Join-Path $root "dist\LiVerse"
$internal = Join-Path $dist "_internal"
$gui = Join-Path $dist "LiVerse.exe"
$exe = Join-Path $dist "LiVerseEngine.exe"
if (-not (Test-Path -LiteralPath $gui -PathType Leaf)) {
    throw "LiVerse.exe was not created: $gui"
}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    throw "LiVerseEngine.exe was not created: $exe"
}

$assetPairs = @(
    @("bible_index\bible_index.db", "bible_index\bible_index.db"),
    @("build_assets\models\vosk-model-small-streaming-ru-0.54\am-onnx\encoder.onnx", ".cache\liverse\models\vosk-model-small-streaming-ru-0.54\am-onnx\encoder.onnx"),
    @("build_assets\models\vosk-model-small-streaming-ru-0.54\am-onnx\decoder.onnx", ".cache\liverse\models\vosk-model-small-streaming-ru-0.54\am-onnx\decoder.onnx"),
    @("build_assets\models\vosk-model-small-streaming-ru-0.54\am-onnx\joiner.onnx", ".cache\liverse\models\vosk-model-small-streaming-ru-0.54\am-onnx\joiner.onnx"),
    @("build_assets\models\vosk-model-small-streaming-ru-0.54\lang\tokens.txt", ".cache\liverse\models\vosk-model-small-streaming-ru-0.54\lang\tokens.txt"),
    @("slide_display\operator.html", "slide_display\operator.html"),
    @("assets\help\holyrics-api-server.png", "assets\help\holyrics-api-server.png"),
    @("assets\help\holyrics-tokens.png", "assets\help\holyrics-tokens.png"),
    @("assets\help\holyrics-permissions.png", "assets\help\holyrics-permissions.png")
)
foreach ($pair in $assetPairs) {
    $source = Join-Path $root $pair[0]
    $packaged = Join-Path $internal $pair[1]
    if (-not (Test-Path -LiteralPath $packaged -PathType Leaf)) {
        throw "Packaged asset is missing: $packaged"
    }
    if ((Get-FileHash $source -Algorithm SHA256).Hash -ne (Get-FileHash $packaged -Algorithm SHA256).Hash) {
        throw "Packaged asset hash mismatch: $packaged"
    }
}

& $exe --version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $exe --help | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $exe --print-grammar-json | Out-Null
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $exe --list-audio-devices
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $exe --check-runtime-assets
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$guiCheck = Start-Process -FilePath $gui -ArgumentList "--check-packaged-gui" -Wait -PassThru
if ($guiCheck.ExitCode -ne 0) { exit $guiCheck.ExitCode }
$updaterCheck = Start-Process -FilePath $gui -ArgumentList "--check-packaged-update" -Wait -PassThru
if ($updaterCheck.ExitCode -ne 0) { exit $updaterCheck.ExitCode }

$guiItem = Get-Item -LiteralPath $gui
$guiHash = (Get-FileHash -LiteralPath $gui -Algorithm SHA256).Hash.ToLowerInvariant()
$engineItem = Get-Item -LiteralPath $exe
$engineHash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "LiVerse GUI and engine onedir smoke tests passed."
Write-Output "GUI: $gui"
Write-Output "GUI bytes: $($guiItem.Length)"
Write-Output "GUI SHA-256: $guiHash"
Write-Output "EXE: $exe"
Write-Output "EXE bytes: $($engineItem.Length)"
Write-Output "EXE SHA-256: $engineHash"
exit 0
'

        if [[ "$BUILD_INSTALLER" == true ]]; then
            run_guest_powershell 'Inno Setup compiler bootstrap' "
\$ErrorActionPreference = 'Stop'
\$toolDir = 'C:\\Build\\Tools\\InnoSetup-$INNO_SETUP_VERSION'
\$compiler = Join-Path \$toolDir 'ISCC.exe'
if (-not (Test-Path -LiteralPath \$compiler -PathType Leaf)) {
    \$volume = Get-Volume -FileSystemLabel 'LIVERSE_SOURCE' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not \$volume -or -not \$volume.DriveLetter) { throw 'LIVERSE_SOURCE CD-ROM was not found' }
    \$setup = \$volume.DriveLetter + ':\\build-tools\\$INNO_SETUP_NAME'
    if (-not (Test-Path -LiteralPath \$setup -PathType Leaf)) { throw ('Inno Setup package was not found: ' + \$setup) }
    \$hash = (Get-FileHash -LiteralPath \$setup -Algorithm SHA256).Hash.ToLowerInvariant()
    if (\$hash -ne '$INNO_SETUP_SHA256') { throw ('Inno Setup SHA-256 mismatch: ' + \$hash) }
    New-Item -ItemType Directory -Path \$toolDir -Force | Out-Null
    \$process = Start-Process -FilePath \$setup -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-','/PORTABLE=1',('/DIR=' + \$toolDir) -Wait -PassThru
    if (\$process.ExitCode -ne 0) { throw ('Inno Setup bootstrap failed: ' + \$process.ExitCode) }
}
if (-not (Test-Path -LiteralPath \$compiler -PathType Leaf)) { throw ('ISCC.exe was not created: ' + \$compiler) }
Write-Output ('Inno Setup compiler ready: ' + \$compiler)
"

            run_guest_powershell 'LiVerse installer build' "
\$ErrorActionPreference = 'Stop'
\$root = 'C:\\Build\\LiVerse'
\$compiler = 'C:\\Build\\Tools\\InnoSetup-$INNO_SETUP_VERSION\\ISCC.exe'
\$versionFile = Join-Path \$root 'packages\\bible_parser_core\\src\\bible_parser_core\\version.py'
\$match = Select-String -LiteralPath \$versionFile -Pattern '__version__ = \"([^\"]+)\"'
if (-not \$match) { throw 'LiVerse version was not found' }
\$version = \$match.Matches[0].Groups[1].Value
\$source = Join-Path \$root 'dist\\LiVerse'
\$output = Join-Path \$root 'dist\\installer'
\$script = Join-Path \$root 'installer\\LiVerse.iss'
if (Test-Path -LiteralPath (Join-Path \$source '_internal\\.env')) { throw '.env must not be included in the installer source' }
New-Item -ItemType Directory -Path \$output -Force | Out-Null
& \$compiler ('/DAppVersion=' + \$version) ('/DSourceDir=' + \$source) ('/DOutputDir=' + \$output) \$script
if (\$LASTEXITCODE -ne 0) { exit \$LASTEXITCODE }
\$setup = Join-Path \$output ('LiVerse-Setup-' + \$version + '.exe')
if (-not (Test-Path -LiteralPath \$setup -PathType Leaf)) { throw ('Installer was not created: ' + \$setup) }
\$item = Get-Item -LiteralPath \$setup
\$hash = (Get-FileHash -LiteralPath \$setup -Algorithm SHA256).Hash.ToLowerInvariant()
\$checksum = Join-Path \$output (\$item.Name + '.sha256')
Set-Content -LiteralPath \$checksum -Value (\$hash + '  ' + \$item.Name) -Encoding ascii
\$report = [ordered]@{
    version = \$version
    installer = \$item.Name
    bytes = \$item.Length
    sha256 = \$hash
    built_at_utc = (Get-Date).ToUniversalTime().ToString('o')
}
\$reportPath = Join-Path \$output ('LiVerse-Windows-Release-' + \$version + '.json')
\$report | ConvertTo-Json | Set-Content -LiteralPath \$reportPath -Encoding utf8
Write-Output ('Installer: ' + \$setup)
Write-Output ('Installer bytes: ' + \$item.Length)
Write-Output ('Installer SHA-256: ' + \$hash)
Write-Output ('Installer checksum file: ' + \$checksum)
Write-Output ('Release report: ' + \$reportPath)
exit 0
"

            run_guest_powershell 'LiVerse installer clean install test' '
$ErrorActionPreference = "Stop"
$root = "C:\Build\LiVerse"
$versionFile = Join-Path $root "packages\bible_parser_core\src\bible_parser_core\version.py"
$match = Select-String -LiteralPath $versionFile -Pattern "__version__ = .([0-9.]+)."
if (-not $match) { throw "LiVerse version was not found" }
$version = $match.Matches[0].Groups[1].Value
$setup = Join-Path $root ("dist\installer\LiVerse-Setup-" + $version + ".exe")
if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { throw "Installer was not found: $setup" }
$installDir = Join-Path $root "installer-test\LiVerse"
$configDir = Join-Path $env:LOCALAPPDATA "LiVerse"
$sentinel = Join-Path $configDir "installer-preserve-test.txt"
$shortcutPaths = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LiVerse.lnk"),
    (Join-Path ($env:APPDATA -replace "\\system32\\", "\SysWOW64\") "Microsoft\Windows\Start Menu\Programs\LiVerse.lnk")
) | Select-Object -Unique
if (Test-Path -LiteralPath $installDir) { throw "Clean install target already exists: $installDir" }
New-Item -ItemType Directory -Path $configDir -Force | Out-Null
Set-Content -LiteralPath $sentinel -Value "preserve" -Encoding UTF8

$install = Start-Process -FilePath $setup -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-",("/DIR=" + $installDir) -Wait -PassThru
if ($install.ExitCode -ne 0) { throw "Installer failed: $($install.ExitCode)" }
$gui = Join-Path $installDir "LiVerse.exe"
$engine = Join-Path $installDir "LiVerseEngine.exe"
$uninstaller = Join-Path $installDir "unins000.exe"
$installDeadline = (Get-Date).AddSeconds(30)
while ((-not (Test-Path -LiteralPath $uninstaller -PathType Leaf) -or -not ($shortcutPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })) -and (Get-Date) -lt $installDeadline) {
    Start-Sleep -Milliseconds 250
}
foreach ($path in @($gui, $engine, $uninstaller)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Installed file is missing: $path" }
}
if (-not ($shortcutPaths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })) { throw "Start menu shortcut was not created" }
& $engine --version
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $engine --check-runtime-assets
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$guiCheck = Start-Process -FilePath $gui -ArgumentList "--check-packaged-gui" -Wait -PassThru
if ($guiCheck.ExitCode -ne 0) { exit $guiCheck.ExitCode }

$uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
if ($uninstall.ExitCode -ne 0) { throw "Uninstaller failed: $($uninstall.ExitCode)" }
$deadline = (Get-Date).AddSeconds(30)
while ((Test-Path -LiteralPath $gui) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (Test-Path -LiteralPath $gui) { throw "Installed application was not removed: $gui" }
foreach ($shortcut in $shortcutPaths) {
    if (Test-Path -LiteralPath $shortcut) { throw "Start menu shortcut was not removed: $shortcut" }
}
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) { throw "User data was removed by uninstall" }
Remove-Item -LiteralPath $sentinel -Force
Write-Output "Installer clean install, launch and uninstall tests passed."
Write-Output "User data directory was preserved: $configDir"
exit 0
'

            if [[ -n "$UPGRADE_FROM_INSTALLER" ]]; then
                run_guest_powershell 'LiVerse installer upgrade test' '
$ErrorActionPreference = "Stop"
$root = "C:\Build\LiVerse"
$versionFile = Join-Path $root "packages\bible_parser_core\src\bible_parser_core\version.py"
$match = Select-String -LiteralPath $versionFile -Pattern "__version__ = .([0-9.]+)."
if (-not $match) { throw "LiVerse version was not found" }
$newVersionExpected = $match.Matches[0].Groups[1].Value

$volume = Get-Volume -FileSystemLabel "LIVERSE_SOURCE" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $volume -or -not $volume.DriveLetter) { throw "LIVERSE_SOURCE CD-ROM was not found" }
$oldSetup = $volume.DriveLetter + ":\previous-installer\LiVerse-Setup-previous.exe"
$oldHashFile = $oldSetup + ".sha256"
foreach ($path in @($oldSetup, $oldHashFile)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Previous installer asset is missing: $path" }
}
$oldHashExpected = (Get-Content -LiteralPath $oldHashFile -Raw).Trim().ToLowerInvariant()
$oldHashActual = (Get-FileHash -LiteralPath $oldSetup -Algorithm SHA256).Hash.ToLowerInvariant()
if ($oldHashActual -ne $oldHashExpected) { throw "Previous installer SHA-256 mismatch: $oldHashActual" }

$newSetup = Join-Path $root ("dist\installer\LiVerse-Setup-" + $newVersionExpected + ".exe")
if (-not (Test-Path -LiteralPath $newSetup -PathType Leaf)) { throw "New installer was not found: $newSetup" }
$installDir = Join-Path $root "installer-upgrade-test\LiVerse"
$configDir = Join-Path $env:LOCALAPPDATA "LiVerse"
$sentinel = Join-Path $configDir "installer-upgrade-preserve-test.txt"
$sentinelText = "preserve-across-upgrade"
$shortcutPaths = @(
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LiVerse.lnk"),
    (Join-Path ($env:APPDATA -replace "\\system32\\", "\SysWOW64\") "Microsoft\Windows\Start Menu\Programs\LiVerse.lnk")
) | Select-Object -Unique
if (Test-Path -LiteralPath $installDir) { throw "Upgrade test target already exists: $installDir" }

New-Item -ItemType Directory -Path $configDir -Force | Out-Null
$testPassed = $false
try {
    $oldInstall = Start-Process -FilePath $oldSetup -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-",("/DIR=" + $installDir) -Wait -PassThru
    if ($oldInstall.ExitCode -ne 0) { throw "Previous LiVerse installer failed: $($oldInstall.ExitCode)" }

    $engine = Join-Path $installDir "LiVerseEngine.exe"
    $gui = Join-Path $installDir "LiVerse.exe"
    $uninstaller = Join-Path $installDir "unins000.exe"
    foreach ($path in @($engine, $gui, $uninstaller)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Previous installed file is missing: $path" }
    }
    $oldVersion = (& $engine --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Previous installed version check failed: $LASTEXITCODE" }
    if ($oldVersion -match [regex]::Escape($newVersionExpected)) { throw "Previous installer already contains LiVerse $newVersionExpected" }

    Set-Content -LiteralPath $sentinel -Value $sentinelText -Encoding UTF8

    $newInstall = Start-Process -FilePath $newSetup -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-",("/DIR=" + $installDir) -Wait -PassThru
    if ($newInstall.ExitCode -ne 0) { throw "New LiVerse installer failed: $($newInstall.ExitCode)" }

    $newVersion = (& $engine --version | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $newVersion -notmatch [regex]::Escape($newVersionExpected)) {
        throw "Expected installed version $newVersionExpected, got: $newVersion"
    }
    & $engine --check-runtime-assets
    if ($LASTEXITCODE -ne 0) { throw "Runtime assets check failed: $LASTEXITCODE" }
    $guiCheck = Start-Process -FilePath $gui -ArgumentList "--check-packaged-gui" -Wait -PassThru
    if ($guiCheck.ExitCode -ne 0) { throw "Packaged GUI check failed: $($guiCheck.ExitCode)" }
    $updaterCheck = Start-Process -FilePath $gui -ArgumentList "--check-packaged-update" -Wait -PassThru
    if ($updaterCheck.ExitCode -ne 0) { throw "Packaged updater check failed: $($updaterCheck.ExitCode)" }

    if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) { throw "User setting marker disappeared during upgrade" }
    $actualSentinel = (Get-Content -LiteralPath $sentinel -Raw).Trim()
    if ($actualSentinel -ne $sentinelText) { throw "User setting marker changed during upgrade: $actualSentinel" }

    Write-Output "Installer upgrade test passed: $oldVersion -> $newVersion."
    Write-Output "Previous installer SHA-256: $oldHashActual"
    Write-Output "User data was preserved during upgrade: $sentinel"
    $testPassed = $true
}
finally {
    $uninstaller = Join-Path $installDir "unins000.exe"
    if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait -PassThru
        if ($uninstall.ExitCode -ne 0) { Write-Error "Upgrade test cleanup uninstaller failed: $($uninstall.ExitCode)" }
        $deadline = (Get-Date).AddSeconds(60)
        while ((Test-Path -LiteralPath $gui) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
    }
    if (Test-Path -LiteralPath $sentinel -PathType Leaf) { Remove-Item -LiteralPath $sentinel -Force }
}
if (-not $testPassed) { exit 1 }
if (Test-Path -LiteralPath $gui) { throw "Upgrade test application was not removed: $gui" }
foreach ($shortcut in $shortcutPaths) {
    if (Test-Path -LiteralPath $shortcut) { throw "Upgrade test shortcut was not removed: $shortcut" }
}
Write-Output "Upgrade test cleanup passed."
exit 0
'
            fi
        fi
    fi
fi

printf 'LiVerse Windows source sync prepared successfully.\n'
printf '  commit: %s\n' "$COMMIT"
printf '  branch: %s\n' "$BRANCH"
printf '  dirty: %s\n' "$DIRTY"
printf '  destination: %s\n' "$WINDOWS_DEST"
printf '  snapshot: %s files, %s bytes\n' "$FILE_COUNT" "$SNAPSHOT_BYTES"
printf '  ISO: %s (%s bytes)\n' "$ISO_PATH" "$ISO_BYTES"
printf '  VM media: %s\n' "$ATTACH_STATUS"
printf 'Run sync_to_workspace.cmd from the LIVERSE_SOURCE CD-ROM in Windows.\n'

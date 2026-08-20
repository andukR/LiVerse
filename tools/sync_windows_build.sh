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
PREPARE_ONLY=false

usage() {
    cat <<'EOF'
Usage: tools/sync_windows_build.sh [options]

Create a conservative LiVerse source snapshot and expose it to the Windows VM
as a separate read-only CD-ROM.

Options:
  --prepare-only        Build the ISO without changing the VM configuration.
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

for command in git rsync xorriso sha256sum find du awk sed; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is not installed: $command" >&2
        exit 1
    fi
done

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
    printf 'snapshot_policy=explicit build inputs; no runtime cache, models, env, logs, build or dist\n'
    printf 'included_build_asset=%s\n' "$BIBLE_INDEX_ASSET"
    printf 'included_ignored_test_data=packages/bible_parser_core/tests/parser_regression_cases.json\n'
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
            virsh -c "$VM_URI" change-media "$VM_NAME" "$VM_CDROM" "$ISO_PATH" --update --live --config >/dev/null
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

printf 'LiVerse Windows source sync prepared successfully.\n'
printf '  commit: %s\n' "$COMMIT"
printf '  branch: %s\n' "$BRANCH"
printf '  dirty: %s\n' "$DIRTY"
printf '  destination: %s\n' "$WINDOWS_DEST"
printf '  snapshot: %s files, %s bytes\n' "$FILE_COUNT" "$SNAPSHOT_BYTES"
printf '  ISO: %s (%s bytes)\n' "$ISO_PATH" "$ISO_BYTES"
printf '  VM media: %s\n' "$ATTACH_STATUS"
printf 'Run sync_to_workspace.cmd from the LIVERSE_SOURCE CD-ROM in Windows.\n'

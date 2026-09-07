#!/usr/bin/env bash
# Create and insert a replay ISO with all prepared Rodnik sermon recordings.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERMON_ROOT="$PROJECT_ROOT/../bible_parser_cli/.cache/whisper_runs"
STAGE_PARENT="$PROJECT_ROOT/.windows-sync"
VM_NAME="win10"
CDROM_TARGET="sdc"

if [[ ! -d "$SERMON_ROOT" ]]; then
    printf 'Sermon recordings were not found: %s\n' "$SERMON_ROOT" >&2
    exit 1
fi

shopt -s nullglob
sermons=("$SERMON_ROOT"/*/work/Воскресное\ богослужение*_16k_mono.wav)
if ((${#sermons[@]} == 0)); then
    printf 'No prepared Rodnik sermon WAV files were found in: %s\n' "$SERMON_ROOT" >&2
    exit 1
fi

mkdir -p "$STAGE_PARENT"
stage=$(mktemp -d "$STAGE_PARENT/rodnik-replay.XXXXXX")
cleanup() {
    rm -rf -- "$stage"
}
trap cleanup EXIT

mkdir -p "$stage/audio"
# Windows PowerShell 5.1 treats UTF-8 without a BOM as a legacy code page.
# Encode the ISO copies as UTF-16 with a BOM so Russian diagnostic messages
# cannot corrupt PowerShell syntax.
iconv -f UTF-8 -t UTF-16 "$PROJECT_ROOT/tools/Invoke-LiVerseReplay.ps1" \
    > "$stage/Invoke-LiVerseReplay.ps1"
iconv -f UTF-8 -t UTF-16 "$PROJECT_ROOT/tools/Run-RodnikReplay.ps1" \
    > "$stage/Run-RodnikReplay.ps1"
cp -a -- "$PROJECT_ROOT/tools/Run-RodnikReplay.cmd" "$stage/"
cp -a -- "${sermons[@]}" "$stage/audio/"

"$PROJECT_ROOT/tools/transfer_to_windows_vm.sh" \
    --vm "$VM_NAME" \
    --cdrom "$CDROM_TARGET" \
    --label "LIVERSE_REPLAY" \
    "$stage"

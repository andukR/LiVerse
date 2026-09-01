#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  tools/transfer_to_windows_vm.sh [--vm NAME] [--cdrom TARGET] [--label LABEL] PATH

Examples:
  tools/transfer_to_windows_vm.sh ~/Downloads/LiVerse-Setup-1.2.0.exe
  tools/transfer_to_windows_vm.sh --vm win10 --cdrom sdc /path/to/file-or-dir

What it does:
  1. Copies the file or directory into a temporary staging folder.
  2. Builds an ISO image from that staging folder.
  3. Inserts the ISO into the Windows VM as a virtual CD-ROM.

Notes:
  - The ISO is created in .windows-sync/transfers inside the LiVerse repo.
  - If PATH is a directory, its contents are copied into the ISO root.
  - If PATH is a file, it is placed into the ISO root with the same filename.
EOF
}

vm_name="win10"
cdrom_target=""
volume_label="LIVERSE_SOURCE"
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      vm_name=${2:?missing value for --vm}
      shift 2
      ;;
    --cdrom)
      cdrom_target=${2:?missing value for --cdrom}
      shift 2
      ;;
    --label)
      volume_label=${2:?missing value for --label}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
    *)
      break
      ;;
  esac
done

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 2
fi

source_path=$1
if [[ ! -e "$source_path" ]]; then
  printf 'Path not found: %s\n' "$source_path" >&2
  exit 2
fi

if ! command -v xorriso >/dev/null 2>&1; then
  printf 'xorriso is required.\n' >&2
  exit 3
fi
if ! command -v virsh >/dev/null 2>&1; then
  printf 'virsh is required.\n' >&2
  exit 3
fi

tmp_root=$(mktemp -d /tmp/liverse-vm-transfer.XXXXXX)
cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT

stage_dir="$tmp_root/stage"
mkdir -p "$stage_dir"

if [[ -d "$source_path" ]]; then
  cp -a "$source_path"/. "$stage_dir"/
else
  cp -a "$source_path" "$stage_dir"/
fi

transfer_dir="$project_root/.windows-sync/transfers"
mkdir -p "$transfer_dir"
stamp=$(date +%Y%m%d-%H%M%S)
base_name=$(basename -- "$source_path")
iso_path="$transfer_dir/${base_name%.*}-$stamp.iso"
xorriso -as mkisofs \
  -iso-level 3 \
  -J \
  -R \
  -V "$volume_label" \
  -o "$iso_path" \
  "$stage_dir" >/dev/null 2>&1

if [[ -z "$cdrom_target" ]]; then
  cdrom_target=$(
    virsh -c qemu:///system domblklist "$vm_name" --details |
      awk '
        $1 == "file" && $2 == "cdrom" && $4 ~ /liverse-source\.iso$/ { print $3; found = 1; exit }
        $1 == "file" && $2 == "cdrom" && !fallback { fallback = $3 }
        END { if (!found) print fallback }
      '
  )
fi

if [[ -z "$cdrom_target" ]]; then
  printf 'Could not detect the VM CD-ROM target. Pass --cdrom explicitly.\n' >&2
  exit 4
fi

vm_state=$(
  virsh -c qemu:///system domstate "$vm_name" |
    tr -d '\r'
)

if [[ "$vm_state" == "running" ]]; then
  if ! virsh -c qemu:///system change-media "$vm_name" "$cdrom_target" "$iso_path" --update --live --config >/dev/null 2>&1; then
    virsh -c qemu:///system change-media "$vm_name" "$cdrom_target" "$iso_path" --insert --live --config >/dev/null
  fi
else
  virsh -c qemu:///system change-media "$vm_name" "$cdrom_target" "$iso_path" --update --config >/dev/null
fi

printf 'Transferred into VM %s via %s using CD-ROM %s\n' "$vm_name" "$iso_path" "$cdrom_target"
printf 'Guest volume label: %s\n' "$volume_label"

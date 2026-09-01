#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  tools/transfer_windows_release_via_usb.sh to-usb VERSION
  tools/transfer_windows_release_via_usb.sh from-usb VERSION MOUNTPOINT

Examples:
  tools/transfer_windows_release_via_usb.sh to-usb 1.2.1
  tools/transfer_windows_release_via_usb.sh from-usb 1.2.1 /media/$USER/ANDREY

The first command expects the USB drive to be redirected to the running win10 VM.
The second command expects the USB drive to be returned and mounted on Debian.
EOF
}

if [[ $# -lt 2 ]]; then
    usage >&2
    exit 2
fi

action=$1
version=$2
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
release_name="LiVerse-Setup-${version}.exe"

case "$action" in
    to-usb)
        [[ $# -eq 2 ]] || { usage >&2; exit 2; }
        powershell_command=$(cat <<EOF
\$usb = Get-Volume | Where-Object { \$_.DriveType -eq 'Removable' -and \$_.DriveLetter } | Select-Object -First 1
if (-not \$usb) { throw 'Removable USB volume not found' }
\$source = 'C:\Build\LiVerse\dist\installer'
\$destination = "\$(\$usb.DriveLetter):\LiVerse-${version}"
New-Item -ItemType Directory -Path \$destination -Force | Out-Null
\$files = @(
    '${release_name}',
    '${release_name}.sha256',
    'LiVerse-Windows-Release-${version}.json'
)
foreach (\$file in \$files) {
    Copy-Item -LiteralPath (Join-Path \$source \$file) -Destination \$destination -Force
}
Copy-Item -LiteralPath 'C:\Build\LiVerse\installer\Verify-LiVerse.ps1' -Destination \$destination -Force
\$expected = ((Get-Content -LiteralPath (Join-Path \$source '${release_name}.sha256') -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
\$actual = (Get-FileHash -LiteralPath (Join-Path \$destination '${release_name}') -Algorithm SHA256).Hash.ToLowerInvariant()
if (\$actual -ne \$expected) { throw "USB hash mismatch: \$actual != \$expected" }
Write-Output "USB destination: \$destination"
Write-Output "SHA-256: \$actual"
EOF
)
        request=$(python3 -c '
import json, sys
print(json.dumps({
    "execute": "guest-exec",
    "arguments": {
        "path": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "arg": ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", sys.argv[1]],
        "capture-output": True,
    },
}))
' "$powershell_command")
        response=$(virsh -c qemu:///system qemu-agent-command win10 --timeout 30 "$request")
        pid=$(jq -r '.return.pid' <<<"$response")
        while true; do
            status=$(virsh -c qemu:///system qemu-agent-command win10 --timeout 30 \
                "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$pid}}")
            [[ $(jq -r '.return.exited // false' <<<"$status") == true ]] || { sleep 1; continue; }
            jq -r '.return["out-data"] // empty' <<<"$status" | base64 -d
            jq -r '.return["err-data"] // empty' <<<"$status" | base64 -d >&2
            exit_code=$(jq -r '.return.exitcode // 1' <<<"$status")
            exit "$exit_code"
        done
        ;;
    from-usb)
        [[ $# -eq 3 ]] || { usage >&2; exit 2; }
        mountpoint=$3
        source_dir="$mountpoint/LiVerse-${version}"
        destination_dir="$project_root/.windows-release/${version}"
        [[ -d "$source_dir" ]] || { printf 'Release folder not found: %s\n' "$source_dir" >&2; exit 3; }
        mkdir -p "$destination_dir"
        cp -f \
            "$source_dir/$release_name" \
            "$source_dir/$release_name.sha256" \
            "$source_dir/LiVerse-Windows-Release-${version}.json" \
            "$source_dir/Verify-LiVerse.ps1" \
            "$destination_dir/"
        (
            cd "$destination_dir"
            sha256sum --check "$release_name.sha256"
        )
        printf 'Windows release imported to: %s\n' "$destination_dir"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

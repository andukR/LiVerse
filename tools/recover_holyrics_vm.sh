#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  tools/recover_holyrics_vm.sh [--vm NAME]

Emergency recovery for a Windows VM whose desktop is covered by the
Holyrics public-screen window. Only Holyrics.exe and its child processes
are stopped; the VM and other Windows applications keep running.
EOF
}

vm_name="win10"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      vm_name=${2:?missing value for --vm}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v virsh >/dev/null 2>&1; then
  printf 'virsh is required.\n' >&2
  exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf 'python3 is required.\n' >&2
  exit 3
fi

vm_state=$(virsh -c qemu:///system domstate "$vm_name" 2>/dev/null | tr -d '\r')
if [[ "$vm_state" != "running" ]]; then
  printf 'Windows VM %s is not running.\n' "$vm_name" >&2
  exit 4
fi

virsh -c qemu:///system qemu-agent-command "$vm_name" \
  '{"execute":"guest-ping"}' >/dev/null

start_response=$(virsh -c qemu:///system qemu-agent-command "$vm_name" \
  '{"execute":"guest-exec","arguments":{"path":"C:\\Windows\\System32\\taskkill.exe","arg":["/IM","Holyrics.exe","/T","/F"],"capture-output":true}}')

guest_pid=$(printf '%s' "$start_response" | python3 -c \
  'import json, sys; print(json.load(sys.stdin)["return"]["pid"])')

status_response=""
for _attempt in {1..20}; do
  status_response=$(virsh -c qemu:///system qemu-agent-command "$vm_name" \
    "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$guest_pid}}")
  if printf '%s' "$status_response" | python3 -c \
    'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("return", {}).get("exited") else 1)'; then
    break
  fi
  sleep 0.25
done

if [[ -z "$status_response" ]]; then
  printf 'Could not read the Holyrics stop result.\n' >&2
  exit 5
fi

exit_code=$(printf '%s' "$status_response" | python3 -c \
  'import json, sys; print(json.load(sys.stdin).get("return", {}).get("exitcode", -1))')

case "$exit_code" in
  0)
    printf 'Holyrics was stopped in Windows VM %s. The VM is still running.\n' "$vm_name"
    ;;
  128)
    printf 'Holyrics is not running in Windows VM %s, or it is already closed.\n' "$vm_name"
    ;;
  *)
    printf 'Could not stop Holyrics in Windows VM %s (taskkill exit code %s).\n' \
      "$vm_name" "$exit_code" >&2
    exit 6
    ;;
esac

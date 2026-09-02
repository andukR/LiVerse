#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  tools/inspect_holyrics_vm.sh [--vm NAME] [--user WINDOWS_USER]
      [--show-test id|name|name-background|close]

Reads the current Holyrics presentation, theme and background through the
local API inside a Windows VM. The API token is never printed.

With --show-test, sends one temporary John 3:16 slide using the current theme
by id, by name, or by name plus the current background. This changes only the
Holyrics public screen.
EOF
}

vm_name="win10"
windows_user="kriwoscheev"
show_test=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vm)
      vm_name=${2:?missing value for --vm}
      shift 2
      ;;
    --user)
      windows_user=${2:?missing value for --user}
      shift 2
      ;;
    --show-test)
      show_test=${2:?missing mode for --show-test}
      case "$show_test" in
        id|name|name-background|close) ;;
        *)
          printf 'Unknown --show-test mode: %s\n' "$show_test" >&2
          exit 2
          ;;
      esac
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

for command_name in virsh python3 iconv base64; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '%s is required.\n' "$command_name" >&2
    exit 3
  fi
done

vm_state=$(virsh -c qemu:///system domstate "$vm_name" 2>/dev/null | tr -d '\r')
if [[ "$vm_state" != "running" ]]; then
  printf 'Windows VM %s is not running.\n' "$vm_name" >&2
  exit 4
fi

virsh -c qemu:///system qemu-agent-command "$vm_name" \
  '{"execute":"guest-ping"}' >/dev/null

read -r -d '' powershell_script <<EOF || true
\$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding(\$false)
\$envPath = 'C:/Users/${windows_user}/AppData/Local/LiVerse/.env'
\$settings = @{}
Get-Content -LiteralPath \$envPath | ForEach-Object {
  if (\$_ -match '^\s*([^#=]+)=(.*)\$') {
    \$settings[\$matches[1].Trim()] = \$matches[2].Trim()
  }
}
\$token = \$settings['HOLYRICS_TOKEN']
\$port = \$settings['HOLYRICS_PORT']
if (-not \$port) { \$port = '8091' }
function Invoke-Holy([string]\$endpoint, [hashtable]\$body) {
  \$uri = "http://localhost:\$port/api/\$endpoint" + '?token=' + [uri]::EscapeDataString(\$token)
  \$json = \$body | ConvertTo-Json -Compress -Depth 20
  try {
    \$utf8Body = [Text.Encoding]::UTF8.GetBytes(\$json)
    \$raw = (Invoke-WebRequest -UseBasicParsing -Method Post -Uri \$uri -ContentType 'application/json' -Body \$utf8Body).Content
    [pscustomobject]@{ endpoint = \$endpoint; response = \$raw }
  } catch {
    [pscustomobject]@{ endpoint = \$endpoint; error = \$_.Exception.Message }
  }
}
if ('${show_test}' -eq 'close') {
  @((Invoke-Holy 'CloseCurrentQuickPresentation' @{})) |
    ConvertTo-Json -Depth 20 -Compress
  exit
}
if ('${show_test}') {
  \$themeResult = Invoke-Holy 'GetCurrentTheme' @{}
  \$themeResponse = \$themeResult.response
  if (\$themeResponse -is [string]) {
    \$themeResponse = \$themeResponse | ConvertFrom-Json
  }
  \$themeId = [string]\$themeResponse.data.id
  if (-not \$themeId) {
    throw 'GetCurrentTheme did not return a theme id.'
  }
  \$themeName = [string]\$themeResponse.data.name
  \$backgroundResult = Invoke-Holy 'GetCurrentBackground' @{}
  \$backgroundResponse = \$backgroundResult.response
  if (\$backgroundResponse -is [string]) {
    \$backgroundResponse = \$backgroundResponse | ConvertFrom-Json
  }
  \$backgroundId = [string]\$backgroundResponse.data.id
  \$backgroundType = [string]\$backgroundResponse.data.type
  switch ('${show_test}') {
    'id' { \$themeFilter = @{ id = \$themeId } }
    'name' { \$themeFilter = @{ name = \$themeName } }
    'name-background' {
      \$themeFilter = @{
        name = \$themeName
        edit = @{ background = @{ id = \$backgroundId; type = \$backgroundType } }
      }
    }
  }
  \$newLine = [Environment]::NewLine
  @(
    (Invoke-Holy 'ShowQuickPresentation' @{
      slides = @(@{
        text = 'Иоанна 3:16' + \$newLine + \$newLine + 'Ибо так возлюбил Бог мир, что отдал Сына Своего Единородного'
        theme = \$themeFilter
      })
    })
  ) | ConvertTo-Json -Depth 20 -Compress
  exit
}
@(
  (Invoke-Holy 'GetTokenInfo' @{}),
  (Invoke-Holy 'GetCurrentPresentation' @{ include_slides = \$true }),
  (Invoke-Holy 'GetCurrentTheme' @{}),
  (Invoke-Holy 'GetCurrentBackground' @{}),
  (Invoke-Holy 'GetCurrentQuickPresentation' @{})
) | ConvertTo-Json -Depth 20 -Compress
EOF

encoded_command=$(printf '%s' "$powershell_script" | iconv -f UTF-8 -t UTF-16LE | base64 -w 0)
guest_payload=$(python3 -c \
  'import json, sys; print(json.dumps({"execute":"guest-exec","arguments":{"path":r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe","arg":["-NoProfile","-EncodedCommand",sys.argv[1]],"capture-output":True}}))' \
  "$encoded_command")

start_response=$(virsh -c qemu:///system qemu-agent-command "$vm_name" "$guest_payload")
guest_pid=$(printf '%s' "$start_response" | python3 -c \
  'import json, sys; print(json.load(sys.stdin)["return"]["pid"])')

status_response=""
for _attempt in {1..40}; do
  status_response=$(virsh -c qemu:///system qemu-agent-command "$vm_name" \
    "{\"execute\":\"guest-exec-status\",\"arguments\":{\"pid\":$guest_pid}}")
  if printf '%s' "$status_response" | python3 -c \
    'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get("return", {}).get("exited") else 1)'; then
    break
  fi
  sleep 0.25
done

printf '%s' "$status_response" | python3 -c '
import base64
import json
import sys

result = json.load(sys.stdin).get("return", {})
if not result.get("exited"):
    raise SystemExit("Holyrics inspection did not finish in time.")
if result.get("exitcode", -1) != 0:
    error = base64.b64decode(result.get("err-data", "") or "").decode("utf-8", errors="replace")
    raise SystemExit(error or "Inspection failed with exit code {}.".format(result.get("exitcode")))

output = base64.b64decode(result.get("out-data", "") or "").decode("utf-8-sig", errors="replace").strip()
items = json.loads(output)
if isinstance(items, dict):
    items = [items]
for item in items:
    print("\n{}:".format(item["endpoint"]))
    if item.get("error"):
        print(item["error"])
        continue
    raw = item.get("response", "")
    if isinstance(raw, (dict, list)):
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        continue
    try:
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    except json.JSONDecodeError:
        print(raw)
'

@echo off
setlocal
cd /d %~dp0
if not exist .venv (
  echo Run install-windows.ps1 first.
  exit /b 1
)
call .venv\Scripts\activate.bat
if "%~1"=="" (
  python tools\vosk_grammar_probe.py --require-approval --slide-output holyrics --open-operator-qr
) else (
  python tools\vosk_grammar_probe.py %*
)

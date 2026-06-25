@echo off
setlocal
cd /d %~dp0
if not exist .venv (
  echo Run install-windows.ps1 first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python tools\vosk_grammar_probe.py

@echo off
setlocal
cd /d %~dp0
if not exist .venv (
  echo Run install-windows.ps1 first.
  exit /b 1
)
call .venv\Scripts\activate.bat
if "%~1"=="" (
  python tools\vosk_grammar_probe.py --check-updates --ask-approval-mode --slide-output holyrics --open-operator-qr --sermon-plan
) else (
  python tools\vosk_grammar_probe.py %*
)
set "LIVERSE_EXIT=%ERRORLEVEL%"
if not "%LIVERSE_EXIT%"=="0" (
  echo.
  echo LiVerse stopped with error code %LIVERSE_EXIT%.
  echo The error message is shown above. Press any key to close this window.
  pause >nul
)
exit /b %LIVERSE_EXIT%

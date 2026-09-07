@echo off
setlocal EnableExtensions

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run-RodnikReplay.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo LiVerse replay stopped with code %EXIT_CODE%.
) else (
  echo LiVerse replay completed. The citation summary is printed above.
)
pause

exit /b %EXIT_CODE%

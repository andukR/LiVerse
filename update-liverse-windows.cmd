@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "TARGET_DIR=%~1"
if "%TARGET_DIR%"=="" set "TARGET_DIR=%CD%"
set "REPO_URL=https://github.com/andukR/LiVerse.git"
set "BRANCH=main"
set "LOG_DIR=%USERPROFILE%\LiVerse-update-logs"

if not defined LIVERSE_CMD_REEXEC (
  set "LIVERSE_CMD_REEXEC=1"
  set "TEMP_SCRIPT=%TEMP%\update-liverse-windows.cmd"
  copy "%~f0" "%TEMP_SCRIPT%" >nul
  call "%TEMP_SCRIPT%" "%TARGET_DIR%"
  exit /b %ERRORLEVEL%
)

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul
set "LOG_FILE=%LOG_DIR%\liverse-update-cmd-%RANDOM%%RANDOM%.log"

echo LiVerse CMD update log: %LOG_FILE%
echo.

call :main >> "%LOG_FILE%" 2>&1
set "RESULT=%ERRORLEVEL%"

echo.
if "%RESULT%"=="0" (
  echo LiVerse was updated successfully.
) else (
  echo LiVerse update failed with code %RESULT%.
  echo Send this log for diagnostics:
  echo %LOG_FILE%
)
echo.
echo Last log lines:
echo ----------------------------------------
type "%LOG_FILE%"
echo ----------------------------------------

exit /b %RESULT%

:main
call :step Preparing LiVerse CMD updater
echo Date: %DATE% %TIME%
echo User: %USERNAME%
echo Computer: %COMPUTERNAME%
echo TargetDir: %TARGET_DIR%
echo RepoUrl: %REPO_URL%
echo Branch: %BRANCH%

call :refresh_path

call :step Checking Git
where git
if errorlevel 1 (
  echo Git is not installed or not in PATH.
  echo Install Git for Windows, then run this script again.
  exit /b 10
)
git --version

call :step Checking Python
call :find_python
if errorlevel 1 exit /b 11
echo Python command: %PYTHON_CMD%

call :step Updating repository
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
cd /d "%TARGET_DIR%" || exit /b 12

if not exist ".git" (
  echo Target folder is not a Git repository: %TARGET_DIR%
  echo If this is the first install, clone LiVerse manually first:
  echo git clone %REPO_URL% "%TARGET_DIR%"
  exit /b 13
)

git remote set-url origin "%REPO_URL%"
if errorlevel 1 exit /b 14

call :git_retry fetch --prune origin "+refs/heads/%BRANCH%:refs/remotes/origin/%BRANCH%"
if errorlevel 1 exit /b 15

git checkout -B "%BRANCH%" "origin/%BRANCH%"
if errorlevel 1 exit /b 16

git reset --hard "origin/%BRANCH%"
if errorlevel 1 exit /b 17

for /f "delims=" %%C in ('git rev-parse --short HEAD') do set "COMMIT=%%C"
for /f "delims=" %%S in ('git log -1 --pretty^=%%s') do set "SUBJECT=%%S"
echo LiVerse: %COMMIT% - %SUBJECT%

call :step Checking virtual environment
set "VENV_PY=%TARGET_DIR%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys; print(sys.executable)"
  if errorlevel 1 goto recreate_venv
  "%VENV_PY%" -m pip --version
  if errorlevel 1 goto recreate_venv
  goto venv_ready
)

:recreate_venv
if exist "%TARGET_DIR%\.venv" (
  echo Removing broken virtual environment...
  rmdir /s /q "%TARGET_DIR%\.venv"
  if errorlevel 1 exit /b 18
)
echo Creating virtual environment...
%PYTHON_CMD% -m venv "%TARGET_DIR%\.venv" --without-pip
if errorlevel 1 exit /b 19
if not exist "%VENV_PY%" exit /b 20
"%VENV_PY%" -m ensurepip --upgrade
if errorlevel 1 exit /b 27
"%VENV_PY%" -m pip --version
if errorlevel 1 exit /b 28

:venv_ready
call :step Installing LiVerse
if exist "%TARGET_DIR%\liverse.egg-info" rmdir /s /q "%TARGET_DIR%\liverse.egg-info"
"%VENV_PY%" -m pip install --disable-pip-version-check setuptools
if errorlevel 1 exit /b 21
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 exit /b 22
"%VENV_PY%" -m pip install --disable-pip-version-check -e . --no-build-isolation
if errorlevel 1 exit /b 23
"%VENV_PY%" -c "import vosk, sounddevice, bible_parser_core; print('Import check OK')"
if errorlevel 1 exit /b 24

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env"
    echo .env was created. Add the Holyrics token before starting LiVerse.
  ) else (
    echo .env and .env.example were not found.
    exit /b 25
  )
)

call :step Creating desktop shortcut
call :create_shortcut
if errorlevel 1 exit /b 26

echo Project: %TARGET_DIR%
echo Start it from the LiVerse shortcut on the desktop.
exit /b 0

:refresh_path
set "PATH=%PATH%;%ProgramFiles%\Git\cmd;%LOCALAPPDATA%\Programs\Git\cmd;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
exit /b 0

:find_python
set "PYTHON_CMD="
py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if not errorlevel 1 (
  set "PYTHON_CMD=py -3.12"
  exit /b 0
)
py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if not errorlevel 1 (
  set "PYTHON_CMD=py -3.11"
  exit /b 0
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if not errorlevel 1 (
  set "PYTHON_CMD=py -3"
  exit /b 0
)
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if not errorlevel 1 (
  set "PYTHON_CMD=python"
  exit /b 0
)
echo Python 3.10 or newer is not installed or not in PATH.
exit /b 1

:git_retry
set "GIT_ARGS=%*"
set "TRY=1"
:git_retry_loop
echo git -c http.version=HTTP/1.1 %GIT_ARGS%
git -c http.version=HTTP/1.1 %GIT_ARGS%
if not errorlevel 1 exit /b 0
if "%TRY%"=="4" exit /b 1
set /a WAIT=TRY*3
echo Git command failed, retrying in %WAIT% seconds...
timeout /t %WAIT% /nobreak >nul
set /a TRY=TRY+1
goto git_retry_loop

:create_shortcut
set "VBS=%TEMP%\liverse-shortcut-%RANDOM%%RANDOM%.vbs"
> "%VBS%" echo Set shell = CreateObject("WScript.Shell")
>> "%VBS%" echo desktop = shell.SpecialFolders("Desktop")
>> "%VBS%" echo Set shortcut = shell.CreateShortcut(desktop ^& "\LiVerse.lnk")
>> "%VBS%" echo shortcut.TargetPath = "%TARGET_DIR%\.venv\Scripts\pythonw.exe"
>> "%VBS%" echo shortcut.Arguments = Chr(34) ^& "%TARGET_DIR%\tools\liverse_gui.py" ^& Chr(34)
>> "%VBS%" echo shortcut.WorkingDirectory = "%TARGET_DIR%"
>> "%VBS%" echo shortcut.Description = "Start LiVerse"
>> "%VBS%" echo shortcut.IconLocation = "%TARGET_DIR%\LiVerse.ico"
>> "%VBS%" echo shortcut.Save
cscript //nologo "%VBS%"
set "VBS_RESULT=%ERRORLEVEL%"
del "%VBS%" >nul 2>nul
exit /b %VBS_RESULT%

:step
echo.
echo ==^> %*
exit /b 0

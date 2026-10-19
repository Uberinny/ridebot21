@echo off
setlocal
cd /d "%~dp0"

echo.
echo  === Partner ride bot ===
echo.

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  where python >nul 2>nul
  if %ERRORLEVEL%==0 (
    set "PY=python"
  ) else (
    echo Python was not found. Install Python 3.11+ from https://www.python.org/downloads/
    echo Tick "Add python.exe to PATH", then run this file again.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
)

set "VPY=.venv\Scripts\python.exe"
echo Installing / updating packages...
"%VPY%" -m pip install --upgrade pip >nul
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install requirements.
  pause
  exit /b 1
)

echo.
"%VPY%" run.py %*
set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" (
  echo Bot exited with code %EC%.
  pause
)
exit /b %EC%

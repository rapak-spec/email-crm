@echo off
setlocal
cd /d "%~dp0"

echo Starting Odoo Gmail Draft Assistant...
echo Keep this window open while using the app.

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON_CMD=py -3"
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PYTHON_CMD=python"
  ) else (
    echo.
    echo Python is not installed or is not available on this computer.
    echo.
    echo Install Python from:
    echo https://www.python.org/downloads/windows/
    echo.
    echo During install, check the box that says:
    echo Add python.exe to PATH
    echo.
    pause
    exit /b 1
  )
)

echo.
echo Opening CRM at http://127.0.0.1:8765
echo If the browser says it cannot connect, wait 5 seconds and refresh.
echo.
start "" "http://127.0.0.1:8765"

%PYTHON_CMD% crm.py

echo.
echo The CRM stopped. You can close this window.
pause

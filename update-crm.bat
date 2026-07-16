@echo off
setlocal
cd /d "%~dp0"

echo Updating Odoo Gmail Draft Assistant...
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed on this computer.
  echo.
  echo Ask your manager to help install Git for Windows:
  echo https://git-scm.com/download/win
  echo.
  pause
  exit /b 1
)

git pull --ff-only
if errorlevel 1 (
  echo.
  echo Update could not finish automatically.
  echo.
  echo If you changed app files locally, ask your manager for help before updating.
  echo Your CRM data is still safe on this computer.
  echo.
  pause
  exit /b 1
)

echo.
echo Update complete.
echo You can now double-click start-crm.bat.
echo.
pause

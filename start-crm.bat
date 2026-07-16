@echo off
setlocal
cd /d "%~dp0"
echo Starting Odoo Gmail Draft Assistant...
echo Keep this window open while using the app.
start "" "http://127.0.0.1:8765"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 crm.py
) else (
  python crm.py
)
pause

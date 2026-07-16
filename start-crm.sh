#!/bin/sh
cd "$(dirname "$0")"
echo "Starting Odoo Gmail Draft Assistant..."
echo "Keep this terminal open while using the app."
python3 crm.py &
APP_PID=$!
sleep 1
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:8765" >/dev/null 2>&1
elif command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:8765"
fi
wait "$APP_PID"

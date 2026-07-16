#!/bin/sh
cd "$(dirname "$0")" || exit 1

URL="${CRM_URL:-http://127.0.0.1:${CRM_PORT:-8765}}"

echo "Starting Odoo Gmail Draft Assistant..."
echo "Keep this terminal open while using the app."

if ! command -v python3 >/dev/null 2>&1; then
  echo
  echo "Python 3 is not installed on this computer."
  echo "Install it with your software center, or ask IT for help installing python3."
  echo
  echo "On Ubuntu/Debian, the command is usually:"
  echo "sudo apt install python3"
  echo
  exit 1
fi

python3 crm.py &
APP_PID=$!

sleep 2
echo
echo "Open this link in the browser:"
echo "$URL"
echo

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1
elif command -v gio >/dev/null 2>&1; then
  gio open "$URL" >/dev/null 2>&1
elif command -v sensible-browser >/dev/null 2>&1; then
  sensible-browser "$URL" >/dev/null 2>&1
elif command -v open >/dev/null 2>&1; then
  open "$URL"
else
  echo "The browser did not open automatically."
  echo "Copy and paste the link above into Chrome or Firefox."
fi

wait "$APP_PID"

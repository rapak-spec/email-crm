#!/bin/sh
cd "$(dirname "$0")"
echo "Starting Odoo Gmail Draft Assistant..."
echo "Keep this window open while using the app."
python3 crm.py &
APP_PID=$!
sleep 1
open "http://127.0.0.1:8765"
wait "$APP_PID"

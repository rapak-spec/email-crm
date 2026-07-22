#!/bin/sh
cd "$(dirname "$0")" || exit 1

APP_DIR="$(pwd)"
RUN_DIR="$HOME/Library/Application Support/Odoo Gmail Draft Assistant"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.odoo.gmail-draft-assistant.plist"
PORT="${CRM_PORT:-8766}"
PYTHON_BIN="$(command -v python3)"

if [ -z "$PYTHON_BIN" ]; then
  echo
  echo "Python 3 is not installed on this Mac."
  echo "Install Python 3 first, then run this installer again."
  exit 1
fi

mkdir -p "$PLIST_DIR"
mkdir -p "$RUN_DIR/crm_app_source"

cp "$APP_DIR/crm.py" "$RUN_DIR/crm.py"
cp "$APP_DIR"/*.md "$RUN_DIR/" 2>/dev/null
cp "$APP_DIR"/crm_app_source/app_source.py.gz.b64.part_* "$RUN_DIR/crm_app_source/"
cp "$APP_DIR/start-crm.sh" "$RUN_DIR/start-crm.sh" 2>/dev/null
cp "$APP_DIR/update-crm.sh" "$RUN_DIR/update-crm.sh" 2>/dev/null

if [ -f "$APP_DIR/crm.db" ] && [ ! -f "$RUN_DIR/crm.db" ]; then
  cp "$APP_DIR/crm.db" "$RUN_DIR/crm.db"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.odoo.gmail-draft-assistant</string>
  <key>WorkingDirectory</key>
  <string>$RUN_DIR</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$RUN_DIR/crm.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CRM_PORT</key>
    <string>$PORT</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$RUN_DIR/crm-autostart.log</string>
  <key>StandardErrorPath</key>
  <string>$RUN_DIR/crm-autostart.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" >/dev/null 2>&1
launchctl load "$PLIST"

echo
echo "Auto-start is installed."
echo "The CRM will start when you log in and restart if it quits."
echo "Auto-start copy:"
echo "$RUN_DIR"
echo
echo "Open this link in your browser:"
echo "http://127.0.0.1:$PORT"
echo
open "http://127.0.0.1:$PORT" >/dev/null 2>&1

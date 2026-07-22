#!/bin/sh
PLIST="$HOME/Library/LaunchAgents/com.odoo.gmail-draft-assistant.plist"

launchctl unload "$PLIST" >/dev/null 2>&1
rm -f "$PLIST"

echo
echo "Auto-start is removed."
echo "You can still run the CRM manually with start-crm.sh."
echo "The local CRM data copy in Library/Application Support was left in place."

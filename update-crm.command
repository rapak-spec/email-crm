#!/bin/sh
cd "$(dirname "$0")" || exit 1

echo "Updating Odoo Gmail Draft Assistant..."
echo

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed on this computer."
  echo
  echo "Install Git or ask your manager for help:"
  echo "https://git-scm.com/download/mac"
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

if ! git pull --ff-only; then
  echo
  echo "Update could not finish automatically."
  echo
  echo "If you changed app files locally, ask your manager for help before updating."
  echo "Your CRM data is still safe on this computer."
  echo
  read -r -p "Press Enter to close..."
  exit 1
fi

echo
echo "Update complete."
echo "You can now double-click start-crm.command."
echo
read -r -p "Press Enter to close..."

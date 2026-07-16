#!/bin/sh
cd "$(dirname "$0")" || exit 1

echo "Updating Odoo Gmail Draft Assistant..."
echo

if ! command -v git >/dev/null 2>&1; then
  echo "Git is not installed on this computer."
  echo "Install Git, then run this update again."
  exit 1
fi

git pull --ff-only || {
  echo
  echo "Update could not finish automatically."
  echo "If you changed app files locally, ask your manager for help before updating."
  echo "Your CRM data is still safe on this computer."
  exit 1
}

echo
echo "Update complete."

#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

# Install browser INTO the project directory so Render includes it in the deploy
export PLAYWRIGHT_BROWSERS_PATH=$PWD/pw-browsers
playwright install chromium
echo "Browsers installed to: $PWD/pw-browsers"
ls -la $PWD/pw-browsers/

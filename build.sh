#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

# Install browser binary only (Render doesn't allow root for system deps)
# Set browser path explicitly so runtime can find it
export PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
playwright install chromium

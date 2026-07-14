#!/bin/bash
# OneFader — build distributable Mac app
cd "$(dirname "$0")"

echo
echo "  ONEFADER — build Mac app"
echo "  -------------------------"
echo

if [ ! -d ".venv" ]; then
    echo "  Run START_HERE_MAC.command first."
    read -p "Press Enter to close..."
    exit 1
fi
source .venv/bin/activate

pip install --quiet --disable-pip-version-check pyinstaller

echo "  Building OneFader.app (takes a minute)..."
pyinstaller --onefile --windowed --name OneFader \
    --icon OneFader.icns \
    --hidden-import rtmidi \
    --add-data "ui.html:." --add-data "remote.html:." onefader.py

if [ -d "dist/OneFader.app" ]; then
    echo
    echo "  DONE!  Send users:  dist/OneFader.app"
    echo "  (zip it before sending — right-click → Compress)"
    open dist
else
    echo "  Build failed — scroll up for the error."
fi
read -p "Press Enter to close..."

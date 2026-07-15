@echo off
title OneFader - Build EXE
cd /d "%~dp0"

echo.
echo  ONEFADER - build distributable EXE
echo  -----------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  Python is not installed. Run START_HERE.bat first.
    pause
    exit /b
)

echo  Installing PyInstaller...
python -m pip install --quiet --disable-pip-version-check pyinstaller

echo  Building OneFader.exe (this takes a minute)...
python -m PyInstaller --onefile --windowed --name OneFader --icon OneFader.ico --hidden-import rtmidi --collect-all pyaudiowpatch --add-data "ui.html;." --add-data "remote.html;." onefader.py

if exist "dist\OneFader.exe" (
    echo.
    echo  DONE!  The file to send to users is:
    echo.
    echo     %~dp0dist\OneFader.exe
    echo.
    echo  One file. Double-click. No Python needed on their computer.
    start "" "dist"
) else (
    echo.
    echo  Build failed - scroll up for the error.
)
pause

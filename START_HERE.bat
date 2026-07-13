@echo off
title OneFader - Setup ^& Run
cd /d "%~dp0"

echo.
echo  ONEFADER - setup ^& run
echo  ------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo  Python is not installed on this computer.
    echo.
    echo  Opening the download page now.
    echo  IMPORTANT: in the installer, tick "Add python.exe to PATH",
    echo  then run this file again.
    echo.
    start https://www.python.org/downloads/windows/
    pause
    exit /b
)

echo  Python found. Installing packages (first run only)...
python -m pip install --quiet --disable-pip-version-check pywebview pycaw comtypes websockets qrcode pillow
if errorlevel 1 (
    echo.
    echo  Package install failed - check your internet connection and try again.
    pause
    exit /b
)

echo  Starting OneFader...
echo.
python onefader.py
if errorlevel 1 pause

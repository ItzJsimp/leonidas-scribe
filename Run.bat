@echo off
title Leonidas Scribe Launcher
cd /d "%~dp0"

REM Check if LeonidasScribe.exe exists and secrets.json is configured
if exist "LeonidasScribe.exe" (
    start "" "LeonidasScribe.exe"
    exit /b 0
)

REM If not built yet, run automated setup
echo Initializing Leonidas Scribe...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup.ps1"
pause

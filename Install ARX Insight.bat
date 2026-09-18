@echo off
cd /d "%~dp0"
title ARX Insight - Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1"
rem The app now runs in its own window. Keep THIS window open only if the setup failed,
rem so the message can be read - otherwise a dead console would stay behind.
if errorlevel 1 pause

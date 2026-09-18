@echo off
cd /d "%~dp0"
title ARX Insight - close this window to stop
set "APP=%LOCALAPPDATA%\ARXInsight"
set "ARX_DATA_DIR=%APP%"
set "ARX_FBCLIENT=%APP%\firebird\fbclient.dll"
set "FIREBIRD=%APP%\firebird"
"%APP%\.venv\Scripts\python.exe" arx_app.py

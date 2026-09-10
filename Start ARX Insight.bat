@echo off
cd /d "%~dp0"
set "APP=%LOCALAPPDATA%\ARXInsight"
set "ARX_DATA_DIR=%APP%"
set "ARX_FBCLIENT=%APP%\firebird\fbclient.dll"
set "FIREBIRD=%APP%\firebird"
"%APP%\.venv\Scripts\python.exe" arx_app.py

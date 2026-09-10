@echo off
cd /d "%~dp0"
set "ARX_FBCLIENT=%~dp0firebird\fbclient.dll"
set "FIREBIRD=%~dp0firebird"
".\.venv\Scripts\python.exe" arx_app.py

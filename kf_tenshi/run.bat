@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py >> wrapper.log 2>&1

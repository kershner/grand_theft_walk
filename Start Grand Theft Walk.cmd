@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" -B -m streaming.launch %*
if errorlevel 1 pause

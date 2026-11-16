@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" -B -m core.manager %*
if errorlevel 1 pause

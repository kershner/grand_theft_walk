@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" -B "%~dp0start_grand_theft_walk.py" %*
if errorlevel 1 pause

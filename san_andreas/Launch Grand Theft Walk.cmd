@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
set "PROJECT_PYTHON=%~dp0..\venv\Scripts\python.exe"
if not exist "%PROJECT_PYTHON%" (
  echo Project virtual environment is missing: "%PROJECT_PYTHON%"
  pause
  exit /b 1
)
"%PROJECT_PYTHON%" -B launch.py %*
if errorlevel 1 pause

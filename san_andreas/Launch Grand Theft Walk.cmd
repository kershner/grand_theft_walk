@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
where py.exe >nul 2>nul
if errorlevel 1 (
  python.exe -B launch.py %*
) else (
  py.exe -3 -B launch.py %*
)
if errorlevel 1 pause

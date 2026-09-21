@echo off
setlocal
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%.venv\Scripts\python.exe" (
  echo Run Setup.ps1 first.
  pause
  exit /b 1
)
"%APP_DIR%.venv\Scripts\python.exe" "%APP_DIR%main.py" %*


@echo off
chcp 65001 >nul
title FluxProxy - Setup
cd /d "%~dp0"

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo [OK] .env created. Add BOT_TOKEN and BOT_USERNAME.
  notepad ".env"
) else (
  echo [OK] .env already exists.
)

if not exist "venv\Scripts\python.exe" (
  echo [*] Creating virtual environment...
  python -m venv venv
)

echo [*] Installing dependencies...
venv\Scripts\python.exe -m pip install --quiet --upgrade pip
venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

echo.
echo [OK] Setup complete. Fill in .env, then run run.bat.
pause

@echo off
chcp 65001 >nul
title FluxProxy - Backend + Public URL + Bot
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo [!] Run setup.bat first.
  pause
  exit /b 1
)
echo.
echo  Starting backend, public HTTPS tunnel and Telegram bot...
echo  Wait for the "PUBLIC MINI APP URL READY" line.
echo  Keep this window OPEN while using the bot.
echo.
venv\Scripts\python.exe tunnel.py
pause

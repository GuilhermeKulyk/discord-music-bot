@echo off
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".env" (
  echo Falta o arquivo .env. Copie .env.example para .env e cole o DISCORD_TOKEN.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" bot.py
pause

@echo off
cd /d "%~dp0"
echo Atualizando yt-dlp e dependencias...
".venv\Scripts\python.exe" -m pip install -U -r requirements.txt
pause

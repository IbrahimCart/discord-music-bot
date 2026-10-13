@echo off
rem Starts the bot on Windows. Double-click this file or run "run.bat" in a terminal.
cd /d "%~dp0"

if not exist ".env" (
  echo No .env file found. Copy .env.example to .env and fill in DISCORD_TOKEN.
  pause
  exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg was not found. Install it with:  winget install Gyan.FFmpeg
  echo Then close this window and open a new one.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the virtual environment...
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Python was not found. Install it with:  winget install Python.Python.3.12
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
rem YouTube changes often; keeping yt-dlp current fixes most playback problems.
".venv\Scripts\python.exe" -m pip install -q -U yt-dlp
".venv\Scripts\python.exe" bot.py
pause

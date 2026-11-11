@echo off
chcp 65001 >nul
title Cortes Live
cd /d "%~dp0"

where ffmpeg >nul 2>nul
if errorlevel 1 (
  if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
    echo Instalando FFmpeg...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  )
)

python -c "import fastapi, faster_whisper, yt_dlp" 2>nul
if errorlevel 1 (
  echo Instalando dependencias...
  python -m pip install -r requirements.txt
)

echo.
echo  Cortes Live rodando em http://localhost:8765
echo  Deixe esta janela aberta. Feche para parar.
echo.
python run.py
pause

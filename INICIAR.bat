@echo off
chcp 65001 >nul
title Cortes Live
cd /d "%~dp0"

python -c "import fastapi, faster_whisper, yt_dlp, streamlink, google.genai, googleapiclient, google_auth_oauthlib, PIL" 2>nul
if errorlevel 1 (
  echo Instalando dependencias...
  python -m pip install -r requirements.txt
)

python -c "from cortes import config; config.ffmpeg()" 2>nul
if errorlevel 1 (
  echo Instalando FFmpeg...
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
)

echo.
echo  ==============================================
echo   Cortes Live rodando em http://localhost:8765
echo   Deixe esta janela aberta. Feche para parar.
echo  ==============================================
echo.

:loop
python run.py
rem Se o servidor cair por qualquer motivo, volta sozinho (sem abrir o navegador de novo)
set CORTES_SEM_NAVEGADOR=1
echo.
echo  [%date% %time%] O servidor parou. Reiniciando em 5 segundos...
echo  (para encerrar de vez, feche esta janela)
timeout /t 5 /nobreak >nul
goto loop

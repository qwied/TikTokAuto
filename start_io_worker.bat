@echo off
chcp 65001 >nul
title StreamClipper - io worker (naruzka io-games)
cd /d "%~dp0"
set PY=python
if exist venv\Scripts\python.exe set PY=venv\Scripts\python.exe

if not exist gh_token.txt (
  echo --------------------------------------------------------------
  echo  NUZHEN GitHub token: github.com -^> Settings -^> Developer
  echo  settings -^> Fine-grained tokens -^> Generate
  echo  (prava: Contents + Releases). Tot zhe, chto dlya stranicy
  echo  "YouTube-narezka" na sajte.
  echo --------------------------------------------------------------
  break > gh_token.txt
  notepad gh_token.txt
)
if not exist gh_token.txt notepad gh_token.txt

echo.
echo Zapusk io-worker: beret ochered iz repozitoriya i narzaet klipey
echo 45 sekund s nachala kazhdogo video (karaoke-subtitry, 9:16).
echo Ostavit kompyuter vklyuchennym - worker rabotaet poka ochered ne pusta.
echo Zakryt okno = ostanovit (prodolzhit mozhno v lyuboy moment).
echo.
%PY% io_worker.py %*
echo.
echo io-worker ostanovlen.
pause

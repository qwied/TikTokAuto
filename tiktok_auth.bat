@echo off
chcp 65001 >nul
rem Получение TikTok-токена: откроется браузер, войди в TikTok и нажми Разрешить.
rem Ключи client_key/client_secret заранее впиши в config.yaml (раздел tiktok:).
rem Результат: файл tiktok_tokens.json рядом с config.yaml (там refresh_token).
rem Если Python/библиотек нет — сначала один раз запустите install.bat.
cd /d "%~dp0"
if exist "venv\Scripts\activate.bat" call "venv\Scripts\activate.bat"
if exist "clipper\tiktok_auth.py" (
  python clipper\tiktok_auth.py
) else (
  python tiktok_auth.py
)
echo.
pause

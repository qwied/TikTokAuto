@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === StreamClipper: установка зависимостей ===
where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден! Установите его с https://www.python.org/downloads/
  echo и поставьте галочку "Add python.exe to PATH", затем запустите install.bat снова.
  pause
  exit /b 1
)
if not exist venv (
  python -m venv venv || (echo Не удалось создать venv & pause & exit /b 1)
)
call venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
echo.
echo === Готово! ===
echo Не забудьте установить ffmpeg: winget install Gyan.FFmpeg
echo Запуск программы: run.bat
pause

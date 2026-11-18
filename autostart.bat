@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === StreamClipper: автозапуск вместе с Windows ===
set BAT=%~dp0run.bat
schtasks /Create /F /TN "StreamClipper" /TR "\"%BAT%\"" /SC ONLOGON /RL LIMITED
if errorlevel 1 (
  echo.
  echo Не удалось создать задачу. Попробуйте запустить этот файл
  echo ПРАВОЙ КНОПКОЙ -> "Запуск от имени администратора".
  pause
  exit /b 1
)
echo.
echo Готово! Теперь StreamClipper стартует сам при каждом входе в Windows.
echo Настроили ОДИН раз на компьютере — дальше всё работает постоянно:
echo   * следит за каналами из links.txt;
echo   * клипы по хайпу: ровно 60 сек, субтитры внизу, баннер RubUP;
echo   * сам публикует в TikTok (если подключён токен);
echo   * телефон управляет через сайт.
echo.
echo Убрать автозапуск: schtasks /Delete /TN "StreamClipper" /F
pause

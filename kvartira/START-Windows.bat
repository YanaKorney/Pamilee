@echo off
chcp 65001 >nul
rem Запуск приложения «Моя квартира» на Windows.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run.py
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py
    goto :end
)

echo.
echo На этом компьютере не найден Python.
echo Скачайте его с сайта python.org, раздел Downloads.
echo При установке обязательно поставьте галочку "Add Python to PATH".
echo Потом запустите этот файл ещё раз.
echo.
pause

:end

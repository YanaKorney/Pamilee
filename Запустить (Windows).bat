@echo off
chcp 65001 >nul
title Аналитика рекламы Wildberries
cd /d "%~dp0"

echo.
echo   Аналитика рекламы Wildberries
echo   ----------------------------------------
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py
    goto done
)

echo   На компьютере не найден Python.
echo.
echo   Установите его с сайта https://www.python.org/downloads/
echo   При установке обязательно поставьте галочку
echo   "Add Python to PATH" на первом экране.
echo.
echo   После установки запустите этот файл ещё раз.

:done
echo.
echo   Окно можно закрыть.
pause >nul

@echo off
rem Sverka zakazov za odin den: chto otdayot WB, chto lezhit u nas
rem i kakoy sposob schyota sovpadaet s voronkoy prodazh v kabinete.
rem Vse russkie soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py sverka
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py sverka
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

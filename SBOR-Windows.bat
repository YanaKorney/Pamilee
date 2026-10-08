@echo off
rem Tolko sbor dannyh: zabiraet statistiku iz kabineta WB za 30 dney
rem i dopolnyaet bazu. Dashbord pri etom ne otkryvaetsya.
rem Vse russkie soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py collect --days 30
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py collect --days 30
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

@echo off
rem Perenosit zhurnal izmeneniy iz tablitsy Excel, kotoraya lezhit
rem ryadom s programmoy. Snachala pokazhet, chto prochitalos, i sprosit.
rem Vse russkie soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py import-changes
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py import-changes
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

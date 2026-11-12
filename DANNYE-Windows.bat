@echo off
rem Gde lezhat vashi dannye: baza, zhurnal, token. Ishchet bazy
rem prezhnih ustanovok i predlagaet perenesti iz nih zhurnal.
rem Vse russkie soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py data
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py data
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

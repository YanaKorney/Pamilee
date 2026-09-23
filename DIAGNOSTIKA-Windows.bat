@echo off
rem Polnaya diagnostika: proveryaet vse metody WB za odin zapusk
rem i pishet otchet v fail diagnostika.txt. Ne ostanavlivaetsya na
rem pervoy oshibke: sobiraet vse srazu. Soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py diagnose
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py diagnose
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

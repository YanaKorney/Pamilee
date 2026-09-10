@echo off
rem Zapusk analitiki reklamy Wildberries.
rem Vse russkie soobshcheniya pechataet Python: tak ih ne isportit kodirovka konsoli.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

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

echo.
echo   Python not found / Python ne naiden.
echo.
echo   Skachayte: https://www.python.org/downloads/
echo   Pri ustanovke otmette galochku "Add Python to PATH"
echo   na pervom ekrane - bez neyo zapusk ne srabotaet.
echo.
echo   Potom zapustite etot fail eshchyo raz.

:done
echo.
pause

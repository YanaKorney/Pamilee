@echo off
rem Tolko proverka dostupa k WB. Nuzhna, chtoby bystro pereproverit
rem soedinenie posle izmeneniy: data i vremya, antivirus, drugaya set.
rem Vse russkie soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py check
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py check
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

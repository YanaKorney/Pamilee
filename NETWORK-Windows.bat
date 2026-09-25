@echo off
rem Otkryvaet dashbord dlya drugih kompyuterov etoy zhe seti.
rem V okne pokazhet adres i kod dostupa. Sbor dannyh po-prezhnemu
rem idet na etom kompyutere. Soobshcheniya pechataet Python.
chcp 65001 >nul 2>&1
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py run.py serve --network
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python run.py serve --network
    goto done
)

echo.
echo   Python not found / Python ne naiden.
echo   Skachayte: https://www.python.org/downloads/

:done
echo.
pause

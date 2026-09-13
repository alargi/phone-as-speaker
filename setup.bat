@echo off
rem ============================================================
rem  phone-as-speaker - one-time setup
rem
rem  Creates a project-local virtual environment (.venv) and
rem  installs numpy / soundcard / aiohttp / qrcode into it.
rem  After this, run.bat will pick up .venv automatically.
rem ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul

set "BASE=python"
where py >nul 2>nul && set "BASE=py"

echo.
echo === phone-as-speaker : setup ===
echo.
echo Base interpreter:
%BASE% -V
echo.

if exist ".venv\Scripts\python.exe" (
    echo Local .venv already exists, reusing it.
) else (
    echo Creating local virtual environment in .venv ...
    %BASE% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create the virtual environment.
        echo         Please install Python 3.9 or newer first.
        echo.
        pause
        exit /b 1
    )
)

echo.
echo Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency installation failed. Check your network and retry.
    echo.
    pause
    exit /b 1
)

echo.
echo Verifying ...
".venv\Scripts\python.exe" -c "import numpy, soundcard, aiohttp, qrcode; print('  numpy     ok'); print('  aiohttp   ok'); print('  soundcard ok'); print('  qrcode    ok')"
if errorlevel 1 (
    echo.
    echo [ERROR] Verification failed.
    echo.
    pause
    exit /b 1
)

echo.
echo === Setup complete ===
echo Run run.bat to start (it opens the graphical console automatically),
echo or selftest.bat to verify the whole chain without a browser.
echo For Android pure-USB mode, install platform-tools and enable USB debugging.
echo.
pause

@echo off
rem ============================================================
rem  phone-as-speaker - end-to-end self test
rem  Starts a temporary server, runs the checks, then exits.
rem  No browser needed.
rem ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
        set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
    )
)
if not defined PY (
    where py >nul 2>nul && set "PY=py"
)
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo [ERROR] No Python interpreter found. Run setup.bat first.
    pause
    exit /b 1
)

"%PY%" -c "import numpy, soundcard, aiohttp" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Dependencies missing. Run setup.bat first.
    pause
    exit /b 1
)

"%PY%" selftest.py
echo.
pause

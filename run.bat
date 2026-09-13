@echo off
rem ============================================================
rem  phone-as-speaker - launcher
rem
rem  Usage:
rem    run.bat                     auto-detect link, start, open console
rem    run.bat --no-console        start without opening the browser
rem    run.bat --link adb          force ADB reverse forwarding
rem    run.bat --link wifi         force Wi-Fi only
rem    run.bat --frame-ms 20       override frame length
rem    run.bat --list-devices      list audio devices and exit
rem
rem  Silent mode (no terminal window):
rem    run-silent.vbs starts this script with --silent in a hidden
rem    window. With --silent we switch to pythonw.exe so that no
rem    console is ever created, and we skip pause so the caller can
rem    read our exit code and surface failures.
rem    To stop a silent instance: use "quit" in the console, or
rem    double-click stop.bat.
rem
rem  Interpreter lookup order:
rem    1) .venv\Scripts\python.exe           project-local venv
rem    2) %USERPROFILE%\.workbuddy\...       managed venv on this machine
rem    3) py launcher, then python on PATH
rem
rem  If dependencies are missing, run setup.bat once.
rem ============================================================

setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>nul

rem  Detect --silent anywhere in the argument list. find scans the whole
rem  argument string, so quoted values such as --device "speaker" are safe.
set "SPK_SILENT="
echo %*|find /i "--silent" >nul && set "SPK_SILENT=1"

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
    echo.
    echo [ERROR] No Python interpreter found.
    echo         Install Python 3.9+ and make sure it is on PATH, then retry.
    echo.
    if not defined SPK_SILENT pause
    exit /b 1
)

for /f "delims=" %%i in ('%PY% -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%i"
if not defined PYEXE set "PYEXE=%PY%"

"%PY%" -c "import numpy, soundcard, aiohttp" >nul 2>nul
if errorlevel 1 (
    echo.
    echo [ERROR] Missing dependencies for this interpreter:
    echo         %PYEXE%
    echo.
    echo         Run setup.bat once to create a local virtual environment
    echo         and install everything, or install manually:
    echo             pip install -r requirements.txt
    echo.
    if not defined SPK_SILENT pause
    exit /b 2
)

rem  In silent mode prefer pythonw.exe from the same folder as the
rem  resolved python.exe, so no console window is ever created.
set "RUNPY=%PY%"
if defined SPK_SILENT (
    if /i not "%PYEXE%"=="%PY%" (
        if exist "%PYEXE:python.exe=pythonw.exe%" set "RUNPY=%PYEXE:python.exe=pythonw.exe%"
    )
)

if not defined SPK_SILENT echo Using interpreter: %RUNPY%
echo.

rem  --open-console opens the graphical console; --no-console (in %*)
rem  overrides it inside server.py. server.py ignores --silent itself.
"%RUNPY%" server.py --open-console %*
set "RC=%ERRORLEVEL%"

if not defined SPK_SILENT (
    echo.
    if not "%RC%"=="0" echo [ERROR] Service exited with code %RC%
    pause
)

exit /b %RC%

@echo off
rem ============================================================
rem  phone-as-speaker - stop a running instance
rem
rem  Reads .speaker.pid (written by server.py at startup), asks
rem  the service to quit gracefully so that the link teardown
rem  (adb reverse) still runs, and force-kills it only if the
rem  graceful path does not work.
rem
rem  Works for both normal starts (run.bat) and hidden starts
rem  (run-silent.vbs). Deleting .speaker.pid by hand is also fine;
rem  the next start overwrites it.
rem ============================================================

setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul 2>nul

set "PIDFILE=%~dp0.speaker.pid"
set "PID="
set "PORT="

if not exist "%PIDFILE%" (
    echo.
    echo [i] No running external-speaker instance found.
    echo     ^(%PIDFILE% does not exist.^)
    echo.
    pause
    exit /b 0
)

rem  Line 1 is the pid, line 2 is the port. "if defined" reads the
rem  live environment, so this loop works without delayed expansion
rem  for the PID/PORT checks themselves.
for /f "usebackq delims=" %%a in ("%PIDFILE%") do (
    if not defined PID (
        set "PID=%%a"
    ) else (
        if not defined PORT set "PORT=%%a"
    )
)

if not defined PID (
    echo [i] The pid file looks malformed. Removing it.
    del /q "%PIDFILE%" >nul 2>nul
    pause
    exit /b 1
)

rem  A pid can be recycled by the system, so confirm that this pid
rem  is really our Python process before killing anything.
set "IMG="
for /f "tokens=1 delims=," %%a in ('tasklist /fi "PID eq %PID%" /fo csv /nh 2^>nul') do set "IMG=%%~a"

echo %IMG% | find /i "python" >nul
if errorlevel 1 (
    echo [i] Process %PID% is gone or is not Python; the service already stopped.
    del /q "%PIDFILE%" >nul 2>nul
    pause
    exit /b 0
)

echo [*] Stopping phone-as-speaker: pid=%PID% port=%PORT%

if defined PORT (
    rem  Try the graceful path first: this makes the service tear down
    rem  the adb reverse rule and close the audio device before exiting.
    powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:%PORT%/api/quit') -TimeoutSec 3 | Out-Null } catch { }" >nul 2>nul
)

rem  Give it up to ~5 seconds to exit on its own.
for /l %%i in (1,1,5) do (
    tasklist /fi "PID eq %PID%" /fo csv /nh 2>nul | find /i "python" >nul || goto :stopped
    ping -n 2 127.0.0.1 >nul 2>nul
)

echo [!] Graceful quit did not finish in time; forcing it to stop.
taskkill /f /pid %PID% >nul 2>nul

:stopped
del /q "%PIDFILE%" >nul 2>nul
echo [*] Stopped.
echo.
pause
exit /b 0

@echo off
setlocal
title Warehouse Backend - Stop

REM ============================================================
REM  One-click stopper. Finds every process listening on PORT and
REM  terminates it (and its child processes).
REM ============================================================

set "PORT=8000"
set "FOUND="

echo ============================================================
echo   Warehouse Backend - Stop
echo ============================================================

echo [INFO]  Looking for processes listening on port %PORT% ...

for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo [INFO]  Found PID %%P, terminating ...
    taskkill /PID %%P /T /F >nul 2>&1
)

if not defined FOUND (
    echo [INFO]  No server is listening on port %PORT%. Nothing to stop.
) else (
    echo [INFO]  Done.
)

echo.
echo Press any key to close this window...
pause >nul

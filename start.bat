@echo off
setlocal
title Warehouse Backend Server

REM ============================================================
REM  One-click launcher for the Warehouse recommendation backend.
REM  Double-click this file to start the FastAPI server.
REM ============================================================

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "VENV=%BACKEND%\.venv\Scripts\activate.bat"

set "HOST=0.0.0.0"
set "PORT=8000"

echo ============================================================
echo   Warehouse Backend Server
echo ============================================================

if not exist "%BACKEND%\app\main.py" (
    echo [ERROR] Backend directory not found: %BACKEND%
    echo         Please run this script from the repository root.
    echo Press any key to close this window...
    pause >nul
    exit /b 1
)

cd /d "%BACKEND%"

if exist "%VENV%" (
    call "%VENV%"
    echo [INFO]  Virtual environment activated.
) else (
    echo [WARN]  Virtual environment not found, falling back to system Python.
)

echo [INFO]  Starting: python -m uvicorn app.main:app --host %HOST% --port %PORT%
echo.
echo        Frontend : http://%HOST%:%PORT%/
echo        API docs : http://%HOST%:%PORT%/docs
echo        Health   : http://%HOST%:%PORT%/health
echo.
echo        Press Ctrl+C in this window to stop, or double-click stop.bat.
echo ============================================================
echo.

python -m uvicorn app.main:app --host %HOST% --port %PORT%

echo.
echo [INFO]  Server has stopped.
echo Press any key to close this window...
pause >nul

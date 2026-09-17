@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Intranet File Transfer

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYEXE="

where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py"

if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)

if not defined PYEXE (
    if exist "C:\msys64\ucrt64\bin\python.exe" set "PYEXE=C:\msys64\ucrt64\bin\python.exe"
)

if not defined PYEXE goto nopython

echo.
echo   Starting server, please wait...
echo.
"%PYEXE%" "%~dp0src\file_transfer_server.py"

echo.
echo   Server stopped.
pause
exit /b 0

:nopython
echo.
echo   Python 3 not found.
echo   Please install it from:  https://www.python.org/downloads/
echo   Remember to check "Add Python to PATH" during setup.
echo.
pause
exit /b 1

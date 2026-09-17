@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Intranet File Transfer

rem UTF-8 mode for the Python child process
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

set "PYEXE="
set "PYARG="

rem ---------- 1) py launcher (installed with python.org installer) ----------
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,7) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARG=-3"
    )
)

rem ---------- 2) python on PATH, skipping Microsoft Store stub ----------
if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE (
            echo %%P | findstr /i "WindowsApps" >nul
            if errorlevel 1 (
                "%%P" -c "import sys; sys.exit(0 if sys.version_info>=(3,7) else 1)" >nul 2>nul
                if not errorlevel 1 set "PYEXE=%%P"
            )
        )
    )
)

rem ---------- 3) common install locations ----------
if not defined PYEXE call :findpython "%LOCALAPPDATA%\Programs\Python"
if not defined PYEXE call :findpython "C:\Program Files"
if not defined PYEXE call :findpython "C:\Program Files (x86)"
if not defined PYEXE call :findpython "C:\"

if not defined PYEXE goto nopython

echo.
echo   Starting server, please wait...
echo.
"%PYEXE%" %PYARG% "%~dp0src\file_transfer_server.py"

echo.
echo   Server stopped.
pause
exit /b 0

:findpython
for /d %%D in (%~1\Python3*) do (
    if not defined PYEXE (
        if exist "%%D\python.exe" (
            "%%D\python.exe" -c "import sys; sys.exit(0 if sys.version_info>=(3,7) else 1)" >nul 2>nul
            if not errorlevel 1 set "PYEXE=%%D\python.exe"
        )
    )
)
goto :eof

:nopython
echo.
echo   [!] No usable Python 3 found on this computer.
echo   [!] 本机没有找到可用的 Python 3，请先安装：
echo.
echo       1. Open:  https://www.python.org/downloads/windows/
echo       2. Download the Python 3.x 64-bit installer
echo       3. IMPORTANT: on the first screen, CHECK
echo          "Add python.exe to PATH"
echo          安装时务必勾选第一屏的 "Add python.exe to PATH"
echo       4. Re-run this file  重新双击本文件
echo.
choice /c YN /m "Open the download page now? 现在打开下载页面吗? "
if not errorlevel 2 start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

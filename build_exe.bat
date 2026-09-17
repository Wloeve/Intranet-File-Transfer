@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Build EXE - Intranet File Transfer

rem ---------- find python (same logic as 启动服务器.bat) ----------
set "PYEXE="
set "PYARG="

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARG=-3"
    )
)

if not defined PYEXE (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined PYEXE (
            echo %%P | findstr /i "WindowsApps" >nul
            if errorlevel 1 (
                "%%P" -c "import sys" >nul 2>nul
                if not errorlevel 1 set "PYEXE=%%P"
            )
        )
    )
)

if not defined PYEXE (
    echo   [!] Python 3 not found. Install it first, then re-run this file.
    echo   [!] 未找到 Python 3，请先安装再运行本文件。
    pause
    exit /b 1
)

echo   Using Python: %PYEXE% %PYARG%
echo.
echo   [1/2] Checking PyInstaller (one-time download)...
%PYEXE% %PYARG% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 %PYEXE% %PYARG% -m pip install --user pyinstaller
if errorlevel 1 (
    echo   [!] PyInstaller install failed. 检查网络后重试。
    pause
    exit /b 1
)

echo.
echo   [2/2] Building standalone exe...
%PYEXE% %PYARG% -m PyInstaller --onefile --name IntranetFileTransfer --console --clean -y src\file_transfer_server.py
if errorlevel 1 (
    echo   [!] Build failed. 打包失败，请把上方报错信息截图反馈。
    pause
    exit /b 1
)

echo.
echo   ============================================
echo   Done! 完成！
echo   生成位置 Output:  dist\IntranetFileTransfer.exe
echo.
echo   把这一个 exe 复制到任何 Windows 10/11 电脑，
echo   双击即可运行，无需安装 Python。
echo   ============================================
pause
exit /b 0

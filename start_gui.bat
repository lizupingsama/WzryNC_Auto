@echo off
rem WzryNC_Auto GUI launcher: prepare env, then start GUI via pythonw (no console)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+
    pause
    exit /b 1
)

if defined WZRY_ADB (
    "%WZRY_ADB%" version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] WZRY_ADB is not executable: %WZRY_ADB%
        pause
        exit /b 1
    )
) else (
    for /f "delims=" %%A in ('where adb 2^>nul') do if not defined WZRY_ADB set "WZRY_ADB=%%A"
    if not defined WZRY_ADB (
        echo [ERROR] ADB not found. Add to PATH or set WZRY_ADB
        pause
        exit /b 1
    )
)

if not defined WZRY_VENV_DIR set "WZRY_VENV_DIR=%CD%\venv"
set "VENV_PYTHON=%WZRY_VENV_DIR%\Scripts\python.exe"
set "VENV_PYTHONW=%WZRY_VENV_DIR%\Scripts\pythonw.exe"

if not exist "%VENV_PYTHON%" (
    echo [INFO] Creating virtual environment...
    python -m venv "%WZRY_VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
)

"%VENV_PYTHON%" scripts\check_requirements.py requirements.txt >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies...
    "%VENV_PYTHON%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies
        pause
        exit /b 1
    )
)

start "" "%VENV_PYTHONW%" wzry_gui.py
exit /b 0

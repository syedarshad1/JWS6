@echo off
REM JWS6 Console - Double-Click to Run
REM Just run this file like you would run a JAR file

setlocal enabledelayedexpansion

REM Colors (for Windows 10+)
for /F %%A in ('echo prompt $H ^| cmd') do set "BS=%%A"

cls
echo.
echo ============================================================
echo                   JWS6 Console
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo ERROR: Python not found!
    echo.
    echo Install Python from: https://www.python.org
    echo Make sure to check "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

REM Check PyYAML
pip show PyYAML >nul 2>&1
if %errorlevel% neq 0 (
    echo Installing PyYAML...
    pip install PyYAML >nul 2>&1
    if %errorlevel% neq 0 (
        color 0C
        echo ERROR: Could not install PyYAML
        echo.
        pause
        exit /b 1
    )
)

REM Check files
if not exist "jws_console_web.py" (
    color 0C
    echo ERROR: jws_console_web.py not found
    echo.
    pause
    exit /b 1
)

if not exist "targets.yaml" (
    color 0C
    echo ERROR: targets.yaml not found
    echo.
    pause
    exit /b 1
)

echo ✓ All checks passed
echo ✓ Starting JWS6 Console...
echo.
echo Browser will open automatically in a moment...
echo.
echo Press Ctrl+C to stop the console
echo.
echo ============================================================
echo.

REM Run the console
python jws_console_web.py

pause

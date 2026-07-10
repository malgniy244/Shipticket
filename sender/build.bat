@echo off
REM ============================================================
REM  STS-Sender — Emergency local Windows build
REM  Canonical build path: GitHub Actions (see .github/workflows/)
REM  Use this only if Actions is unavailable.
REM ============================================================

echo.
echo  Ship Ticket Sender — Local Windows Build
echo  ==========================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found. Install Python 3.11+ and add to PATH.
    pause
    exit /b 1
)

REM Install/upgrade dependencies
echo  Installing dependencies...
pip install --upgrade msal requests pyinstaller
if errorlevel 1 (
    echo  [ERROR] pip install failed.
    pause
    exit /b 1
)

REM Build
echo.
echo  Building STS-Sender.exe...
pyinstaller ^
    --onefile ^
    --windowed ^
    --name "STS-Sender" ^
    --add-data "sender_core.py;." ^
    sender.py

if errorlevel 1 (
    echo.
    echo  [ERROR] PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo  ============================================================
echo   [OK]  Build complete.
echo   Output: dist\STS-Sender.exe
echo  ============================================================
echo.
pause

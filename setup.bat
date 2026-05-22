@echo off
setlocal

echo.
echo  Systemu — Environment Setup
echo  ============================
echo.

:: Check Python is available
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python not found on PATH. Install Python 3.10+ and try again.
    exit /b 1
)

:: Verify Python version is 3.10+
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Using Python %PYVER%

:: Create virtual environment
if exist .venv (
    echo [INFO] .venv already exists — skipping creation.
) else (
    echo [INFO] Creating virtual environment in .venv ...
    python -m venv .venv
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
)

:: Upgrade pip silently
echo [INFO] Upgrading pip ...
.venv\Scripts\python -m pip install --upgrade pip --quiet

:: Install project dependencies
echo [INFO] Installing dependencies from requirements.txt ...
.venv\Scripts\pip install -r requirements.txt --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Dependency installation failed.
    exit /b 1
)

:: Install the package itself in editable mode so 'sharing_on' CLI is available
echo [INFO] Installing Systemu package in editable mode ...
.venv\Scripts\pip install -e . --quiet
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Package installation failed.
    exit /b 1
)

echo.
echo  Setup complete.
echo.
echo  To start Systemu (venv mode — tools run as Python subprocesses):
echo    start.bat
echo.
echo  To start with Docker tool isolation (requires Docker Desktop):
echo    start_docker.bat
echo.

endlocal

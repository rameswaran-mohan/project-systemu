@echo off
:: Systemu — bootstrap installer (Windows).
::
:: Verifies a system Python 3.10+ is on PATH, then hands off to install.py.
setlocal enabledelayedexpansion

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found on PATH. Install Python 3.10+ and re-run. 1>&2
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ required.  1>&2
    python --version
    exit /b 1
)

python install.py %*
exit /b %ERRORLEVEL%

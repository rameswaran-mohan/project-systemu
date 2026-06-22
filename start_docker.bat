@echo off
setlocal

if not exist .venv (
    echo [ERROR] No virtual environment found. Run setup.bat first.
    exit /b 1
)

:: Verify Docker is available
where docker >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Docker not found on PATH. Install Docker Desktop and try again.
    exit /b 1
)

docker info >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Docker daemon is not running. Start Docker Desktop and try again.
    exit /b 1
)

call .venv\Scripts\activate

echo [INFO] Starting Systemu daemon ^(Docker tool-sandbox mode^) ...
set SYSTEMU_TOOL_BACKEND=docker
sharing_on daemon start %*

endlocal

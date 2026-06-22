@echo off
:: Systemu - one-command upgrade (Windows).
::
:: Stops the daemon, pulls latest code, reinstalls deps, runs alembic
:: migrations, restarts.  Refuses on a dirty tree or non-fast-forward pull.
::
:: Usage:
::   update.bat        Interactive (asks before stopping daemon)
::   update.bat /y     Non-interactive (CI / cron)
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "YES=0"
if /i "%~1"=="/y"     set "YES=1"
if /i "%~1"=="-y"     set "YES=1"
if /i "%~1"=="--yes"  set "YES=1"
if /i "%~1"=="/help"  goto :usage
if /i "%~1"=="-h"     goto :usage
if /i "%~1"=="--help" goto :usage

:: -- Pre-flight checks ---------------------------------------------------

if not exist .git (
    echo [ERROR] %CD% is not a git checkout - refusing to update.
    exit /b 1
)

git diff --quiet >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Working tree has uncommitted changes.
    echo         Commit / stash them first, then re-run update.bat.
    exit /b 1
)
git diff --cached --quiet >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Index has staged changes.
    echo         Commit / unstage them first, then re-run update.bat.
    exit /b 1
)

if not exist .venv (
    echo [ERROR] .venv missing - run install.bat first.
    exit /b 1
)

:: -- Confirmation --------------------------------------------------------

if "%YES%"=="0" (
    echo About to stop daemon + worker, pull latest, reinstall deps, migrate DB, restart.
    set /p "ANS=Continue? [y/N] "
    if /i not "!ANS!"=="y" if /i not "!ANS!"=="yes" (
        echo Aborted.
        exit /b 0
    )
)

:: -- Stop ----------------------------------------------------------------

if exist stop.bat (
    echo [INFO] Stopping daemon + worker ...
    call stop.bat
) else (
    echo [WARN] stop.bat not found; skipping stop.
)

:: -- Pull ----------------------------------------------------------------

echo [INFO] git pull --ff-only ...
git pull --ff-only
if errorlevel 1 (
    echo [ERROR] git pull failed ^(non-fast-forward or network error^).
    echo         Resolve manually, then re-run update.bat.
    exit /b 1
)

:: -- Reinstall deps ------------------------------------------------------

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] .venv\Scripts\python.exe missing - .venv looks corrupted.
    echo         Re-run install.bat.
    exit /b 1
)

echo [INFO] Upgrading pip + dependencies ...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements.txt --upgrade --quiet
"%PY%" -m pip install -e ".[local]" --quiet

:: -- Migrate -------------------------------------------------------------

echo [INFO] Running alembic upgrade head ...
"%PY%" scripts\upgrade_db.py
if errorlevel 1 (
    echo [WARN] DB migration failed - see logs\alembic.log.
    echo        The daemon may crash on schema mismatch.  Re-run install.bat
    echo        if the failure persists.
)

:: -- Restart -------------------------------------------------------------

if exist start.bat (
    echo [INFO] Starting daemon + worker ...
    call start.bat
) else (
    echo [WARN] start.bat not found; start manually.
)

echo.
echo [INFO] Update complete.
exit /b 0


:usage
echo Usage: update.bat [/y]
echo   /y    Skip the 'stop daemon' confirmation prompt.
exit /b 0

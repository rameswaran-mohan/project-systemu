@echo off
:: Systemu â€” mode-aware launcher (Windows).
:: Reads .systemu_mode and either spawns daemon+worker subprocesses (local)
:: or runs docker compose with the appropriate profile.
::
:: v0.6.1+: detaches via PowerShell Start-Process for reliable PID capture,
:: uses absolute paths to the venv python (no PATH-resolution surprises),
:: writes PID files so stop.bat can find what we started.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist .systemu_mode (
    echo [ERROR] .systemu_mode missing - run install.bat first.
    exit /b 1
)

set /p MODE=<.systemu_mode

if "%MODE%"=="local"              goto :start_local
if "%MODE%"=="docker-local"       goto :start_compose_local
if "%MODE%"=="docker-enterprise"  goto :start_compose_enterprise

echo [ERROR] Unknown mode '%MODE%' in .systemu_mode
exit /b 1


:start_local
if not exist .venv (
    echo [ERROR] .venv missing - run install.bat.
    exit /b 1
)
if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv\Scripts\python.exe missing - .venv looks corrupted.  Re-run install.bat.
    exit /b 1
)
if not exist logs mkdir logs

:: Absolute path to venv python â€” bypasses PATH-resolution issues
:: (e.g., stale mapped drives in PATH that produce "system cannot find
:: the drive specified" when `start /B` walks PATH internally).
set "PY=%~dp0.venv\Scripts\python.exe"

:: v0.6.1+: idempotent schema check.  Protects users who `git pull` a release
:: with new migrations but skip re-running install.bat.  Without this, the
:: daemon crashes on first DB read with `OperationalError: no such column`.
:: scripts\upgrade_db.py loads .env so alembic sees SYSTEMU_DATABASE_URL.
echo [INFO] Verifying DB schema ^(alembic upgrade head^) ...
"%PY%" scripts\upgrade_db.py 2>>logs\alembic.log
if errorlevel 1 (
    echo [WARN] DB schema upgrade failed - see logs\alembic.log.
    echo [WARN] Continuing anyway; daemon may crash on schema mismatch.
)

:: â”€â”€ Daemon â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
call :is_pid_alive .systemu_daemon.pid DAEMON_LIVE DAEMON_PID
if "!DAEMON_LIVE!"=="1" (
    echo [INFO] Daemon already running ^(PID !DAEMON_PID!^).
) else (
    echo [INFO] Starting daemon ^(NiceGUI + scheduler^) ...
    rem Use PowerShell Start-Process for reliable detach + PID capture.
    rem cmd's `start /B` doesn't expose the child PID; WMIC is removed in
    rem Windows 11 24H2+.  PowerShell -PassThru is the only portable way.
    rem (`::` comments inside (...) blocks emit "drive specified" noise.)
    call :spawn_detached "!PY!" "-m sharing_on daemon start --foreground" "logs\daemon.log" "logs\daemon.err" .systemu_daemon.pid
    if errorlevel 1 (
        echo [ERROR] Failed to start daemon.  See logs\daemon.err.
        exit /b 1
    )
    set /p DPID=<.systemu_daemon.pid
    echo [INFO] Daemon PID !DPID!
)

:: â”€â”€ Worker â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
call :is_pid_alive .systemu_worker.pid WORKER_LIVE WORKER_PID
if "!WORKER_LIVE!"=="1" (
    echo [INFO] Worker already running ^(PID !WORKER_PID!^).
) else (
    echo [INFO] Starting worker ^(Huey consumer^) ...
    call :spawn_detached "!PY!" "-m systemu.worker" "logs\worker.log" "logs\worker.err" .systemu_worker.pid
    if errorlevel 1 (
        echo [ERROR] Failed to start worker.  See logs\worker.err.
        exit /b 1
    )
    set /p WPID=<.systemu_worker.pid
    echo [INFO] Worker PID !WPID!
)

echo.
echo  Dashboard: http://localhost:8765/
echo  Logs:      logs\daemon.log  ^&  logs\worker.log
echo  Stop:      stop.bat
exit /b 0


:start_compose_local
where docker >nul 2>&1 || (echo [ERROR] docker not on PATH. & exit /b 1)
docker compose --profile local up -d
echo.
echo  Dashboard: http://localhost:8765/
echo  Logs:      docker compose --profile local logs -f
echo  Stop:      stop.bat
exit /b 0


:start_compose_enterprise
where docker >nul 2>&1 || (echo [ERROR] docker not on PATH. & exit /b 1)
docker compose --profile enterprise up -d
echo.
echo  Dashboard: http://localhost:8765/
echo  Logs:      docker compose --profile enterprise logs -f
echo  Stop:      stop.bat
exit /b 0


:: â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
:: Helpers
:: â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

:is_pid_alive
::   %1 = PID file path
::   %2 = output var name for live flag (0/1)
::   %3 = output var name for PID (echoed for messages)
:: Sets the named flag to 1 if the PID in the file is currently running,
:: 0 otherwise (or if file/PID is missing).  Deletes stale PID files.
setlocal
set "PIDFILE=%~1"
set "OUTFLAG=%~2"
set "OUTPID=%~3"
if not exist "%PIDFILE%" (
    endlocal & set "%OUTFLAG%=0" & set "%OUTPID%=" & goto :eof
)
set /p __PID=<"%PIDFILE%"
if "%__PID%"=="" (
    del /q "%PIDFILE%" >nul 2>&1
    endlocal & set "%OUTFLAG%=0" & set "%OUTPID%=" & goto :eof
)
tasklist /FI "PID eq %__PID%" 2>nul | find "%__PID%" >nul
if errorlevel 1 (
    del /q "%PIDFILE%" >nul 2>&1
    endlocal & set "%OUTFLAG%=0" & set "%OUTPID%=" & goto :eof
)
endlocal & set "%OUTFLAG%=1" & set "%OUTPID%=%__PID%"
goto :eof


:spawn_detached
::   %1 = python.exe path (quoted)
::   %2 = python args (quoted, single string)
::   %3 = stdout log path
::   %4 = stderr log path
::   %5 = pid file path to write
:: Spawns a detached, hidden Python process via PowerShell Start-Process,
:: captures the PID, writes it to the PID file.  Returns non-zero on failure.
setlocal
set "EXE=%~1"
set "PYARGS=%~2"
set "OUT=%~3"
set "ERR=%~4"
set "PIDOUT=%~5"

:: Build a tiny PowerShell command that launches the process and prints its PID.
:: -ArgumentList accepts a string-array; splitting on space here is OK because
:: our args are simple flags (no embedded spaces).
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { $p = Start-Process -FilePath '%EXE%' -ArgumentList ('%PYARGS%' -split ' ') -WorkingDirectory '%CD%' -WindowStyle Hidden -RedirectStandardOutput '%CD%\%OUT%' -RedirectStandardError '%CD%\%ERR%' -PassThru -ErrorAction Stop; Set-Content -Path '%CD%\%PIDOUT%' -Value $p.Id -NoNewline -Encoding ASCII; exit 0 } catch { Write-Error $_; exit 1 }"
if errorlevel 1 (
    endlocal & exit /b 1
)
endlocal
exit /b 0

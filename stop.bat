@echo off
:: Systemu â€” mode-aware shutdown (Windows).
::
:: v0.6.1+: PID-file based (matches the new start.bat).  Falls back to
:: window-title matching for any legacy daemons / workers that pre-date
:: the PID-file convention, plus a command-line sweep for orphans started
:: outside start.bat (e.g. directly via `python -m sharing_on daemon`).
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist .systemu_mode (
    echo [INFO] .systemu_mode missing - nothing to do.
    exit /b 0
)

set /p MODE=<.systemu_mode

if "%MODE%"=="local"              goto :stop_local
if "%MODE%"=="docker-local"       goto :stop_compose_local
if "%MODE%"=="docker-enterprise"  goto :stop_compose_enterprise

echo [ERROR] Unknown mode '%MODE%' in .systemu_mode
exit /b 1


:stop_local
:: 1. PID-file based shutdown (the canonical path for v0.6.1+ start.bat)
call :stop_by_pid .systemu_worker.pid worker
call :stop_by_pid .systemu_daemon.pid daemon

:: 2. Legacy window-title fallback â€” for daemons/workers started by older
::    start.bat versions that did `start "systemu-daemon" /B cmd /c "..."`.
::    No-op if nothing matches.
call :stop_by_title systemu-worker worker_legacy
call :stop_by_title systemu-daemon daemon_legacy

:: 3. Orphan sweep â€” kill any stray `python -m sharing_on daemon` or
::    `python -m systemu.worker` processes the user may have started
::    manually outside start.bat.  Uses WMIC-equivalent via PowerShell.
call :kill_orphans_by_cmdline

echo [INFO] Stopped.
exit /b 0


:stop_by_pid
::   %1 = PID file path
::   %2 = label for logging
setlocal
set "PIDFILE=%~1"
set "LABEL=%~2"
if not exist "%PIDFILE%" (
    endlocal & goto :eof
)
set /p PID=<"%PIDFILE%"
if "%PID%"=="" (
    del /q "%PIDFILE%" >nul 2>&1
    endlocal & goto :eof
)
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 (
    echo [INFO] %LABEL% PID %PID% not running ^(stale PID file^).
) else (
    echo [INFO] Stopping %LABEL% ^(PID %PID%^) ...
    taskkill /PID %PID% /T /F >nul 2>&1
)
del /q "%PIDFILE%" >nul 2>&1
endlocal & goto :eof


:stop_by_title
::   %1 = window title
::   %2 = label for logging
setlocal
set "TITLE=%~1"
set "LABEL=%~2"
tasklist /FI "WINDOWTITLE eq %TITLE%" 2>nul | find /I "cmd.exe" >nul
if errorlevel 1 (
    endlocal & goto :eof
)
echo [INFO] Stopping %LABEL% ^(legacy: window title %TITLE%^) ...
taskkill /FI "WINDOWTITLE eq %TITLE%" /T /F >nul 2>&1
endlocal & goto :eof


:kill_orphans_by_cmdline
:: Sweep any python process whose command-line matches our daemon/worker
:: invocations.  Quiet â€” emits one INFO line per kill, none if no orphans.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'sharing_on daemon|systemu\.worker' } | ForEach-Object { Write-Host ('[INFO] Stopping orphan ' + $_.ProcessId + ' (' + ($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length))) + '...)'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
goto :eof


:stop_compose_local
docker compose --profile local down
exit /b 0


:stop_compose_enterprise
docker compose --profile enterprise down
exit /b 0

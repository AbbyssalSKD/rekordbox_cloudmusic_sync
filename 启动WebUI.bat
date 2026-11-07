@echo off
rem ---------------------------------------------------------------------------
rem Double-click launcher for the local WebUI.
rem It only runs:  python -m rb_sync serve   (binds 127.0.0.1:8765, opens browser)
rem Close this window to stop the server.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [webui] .venv not found - falling back to "python" from PATH.
  set "PY=python"
)

rem Already running? Just reopen the browser instead of failing on a busy port.
netstat -ano -p tcp | findstr /c:"127.0.0.1:8765" | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [webui] already running - opening browser.
  start "" "http://127.0.0.1:8765/"
  ping -n 3 127.0.0.1 >nul 2>&1
  exit /b 0
)

echo [webui] starting... (keep this window open; press Ctrl+C to stop)
"%PY%" -m rb_sync serve

if errorlevel 1 (
  echo.
  echo [webui] exited with an error. Press any key to close.
  pause >nul
)
endlocal

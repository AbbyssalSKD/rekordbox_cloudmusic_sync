@echo off
rem ---------------------------------------------------------------------------
rem Double-click launcher for the local WebUI.
rem It only runs:  python -m rb_sync serve   (binds 127.0.0.1:8765, opens browser)
rem Close this window to stop the server.
rem
rem IMPORTANT: the interpreter must live OUTSIDE this repository.
rem Anything whose exe sits inside the workspace is confined by the local
rem sandbox and cannot write to F:\ - that is what produced the "moved 0"
rem plus "Permission denied" result. So D:\rb-sync-venv is preferred,
rem then .venv, then PATH. Override with RB_SYNC_PYTHON if needed.
rem ---------------------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=%RB_SYNC_PYTHON%"
if not defined PY (
  if exist "D:\rb-sync-venv\Scripts\python.exe" (
    set "PY=D:\rb-sync-venv\Scripts\python.exe"
  ) else (
    if exist "%~dp0.venv\Scripts\python.exe" (
      set "PY=%~dp0.venv\Scripts\python.exe"
      echo [webui] WARNING: using .venv inside the repo - it may not be able to write to F:\
    ) else (
      set "PY=python"
      echo [webui] no venv found - falling back to "python" from PATH.
    )
  )
)

netstat -ano -p tcp | findstr /c:"127.0.0.1:8765" | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [webui] already running - opening browser.
  start "" "http://127.0.0.1:8765/"
  ping -n 3 127.0.0.1 >nul 2>&1
  exit /b 0
)

echo [webui] python: %PY%
echo [webui] starting... (keep this window open; press Ctrl+C to stop)
"%PY%" -m rb_sync serve

if errorlevel 1 (
  echo.
  echo [webui] exited with an error. Press any key to close.
  pause >nul
)
endlocal

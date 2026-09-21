@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY="
set "PYW="
if defined WB_TASKS_PYTHON set "PY=%WB_TASKS_PYTHON%"
if not defined PYW if exist "%HERE%..\..\.venv\Scripts\pythonw.exe" set "PYW=%HERE%..\..\.venv\Scripts\pythonw.exe"
if not defined PY if exist "%HERE%..\..\.venv\Scripts\python.exe" set "PY=%HERE%..\..\.venv\Scripts\python.exe"
if not defined PYW for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
if not defined PY for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PYW set "PYW=%PY%"
if not defined PY goto nopython

echo ================================================================
echo  Account-switch watcher  [OBSOLETE / just a fallback]
echo ----------------------------------------------------------------
echo  The watcher is now BUILT INTO the service (tasks/service).
echo  Toggle it on the web page. Run this bat only when the
echo  service is not available.
echo.
echo  If the service-side watcher is already running, this one
echo  exits by itself to avoid double catch-up runs.
echo  (pass --force to override, e.g. for debugging)
echo  READ-ONLY on the desktop auth file - never switches accounts.
echo ================================================================
echo.
echo  Logs: %HERE%logs\watch-YYYYMMDD.log
echo.
echo  Press any key to start it in the background (no window)...
pause >nul

start "" /B "%PYW%" "%HERE%watch.py" %*
echo Started.
echo.
pause
exit /b 0

:nopython
echo [ERROR] Python not found.
echo   1. Install Python 3.10+ and tick "Add python.exe to PATH", or
echo   2. Create a venv at the repo root:  python -m venv .venv
echo      then:  .venv\Scripts\pip install -r tasks\requirements.txt
pause
exit /b 1

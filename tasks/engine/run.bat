@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY="
if defined WB_TASKS_PYTHON set "PY=%WB_TASKS_PYTHON%"
if not defined PY if exist "%HERE%..\..\.venv\Scripts\python.exe" set "PY=%HERE%..\..\.venv\Scripts\python.exe"
if not defined PY for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PY goto nopython

echo ================================================================
echo  LIVE RUN - tasks will be really submitted to the server.
echo  Type YES (uppercase) to continue, anything else cancels.
echo ================================================================
set "OK="
set /p "OK=Continue? "
if /i not "%OK%"=="YES" (
  echo.
  echo Cancelled.
  pause
  exit /b 0
)

echo.
"%PY%" "%HERE%engine.py" --live %*
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

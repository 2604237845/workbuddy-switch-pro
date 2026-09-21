@echo off
chcp 65001 >nul
setlocal
set "HERE=%~dp0"
set "PY="
if defined WB_TASKS_PYTHON set "PY=%WB_TASKS_PYTHON%"
if not defined PY if exist "%HERE%..\..\.venv\Scripts\python.exe" set "PY=%HERE%..\..\.venv\Scripts\python.exe"
if not defined PY for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PY goto nopython

echo DRY-RUN mode: all non-GET requests are intercepted, nothing is submitted.
echo.
"%PY%" "%HERE%engine.py" %*
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

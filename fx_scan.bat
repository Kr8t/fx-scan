@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

rem Pick a Python launcher (py.exe preferred, falls back to python on PATH).
where py >nul 2>&1 && (set "PY=py -3") || (set "PY=python")

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :fail
)

rem Reinstall dependencies only when requirements.txt changed.
fc /b requirements.txt .venv\requirements.installed >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -q --upgrade pip || goto :fail
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
    copy /y requirements.txt .venv\requirements.installed >nul
)

".venv\Scripts\python.exe" cli.py %*
set "RC=%ERRORLEVEL%"

rem Keep the window open when launched by double-click (no arguments).
if "%~1"=="" pause
exit /b %RC%

:fail
echo.
echo Setup failed. Check that Python 3.10+ is installed and on PATH.
pause
exit /b 1

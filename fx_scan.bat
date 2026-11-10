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

rem MetaTrader5 is optional (Windows only). Try once; Yahoo is used if it will not install.
if not exist ".venv\mt5.tried" (
    echo Installing MetaTrader5 package, optional...
    ".venv\Scripts\python.exe" -m pip install -q MetaTrader5 || echo MetaTrader5 package not available - Yahoo data will be used.
    echo tried> .venv\mt5.tried
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

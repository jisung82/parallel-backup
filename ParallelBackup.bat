@echo off
setlocal

title Parallel Backup

cd /d "%~dp0"

echo ==========================================
echo        Parallel Backup Launcher
echo ==========================================
echo.

where py >nul 2>&1
if %errorlevel%==0 goto RUN_PY

where python >nul 2>&1
if %errorlevel%==0 goto RUN_PYTHON

echo [ERROR] Python is not installed or not in PATH.
echo.
echo Install Python from:
echo https://www.python.org/downloads/windows/
echo.
pause
exit /b 1

:RUN_PY
echo Starting Parallel Backup...
py -3 "%~dp0app.py"
set "EXIT_CODE=%errorlevel%"
goto END

:RUN_PYTHON
echo Starting Parallel Backup...
python "%~dp0app.py"
set "EXIT_CODE=%errorlevel%"
goto END

:END
echo.
if not "%EXIT_CODE%"=="0" (
    echo Parallel Backup exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

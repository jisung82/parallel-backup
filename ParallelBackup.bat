@echo off
title Parallel Backup
cd /d "%~dp0"

echo ==========================================
echo        Parallel Backup Launcher
echo ==========================================
echo.

where py.exe >nul 2>nul
if %errorlevel%==0 goto USE_PY

where python.exe >nul 2>nul
if %errorlevel%==0 goto USE_PYTHON

echo [ERROR] Python was not found.
echo Install Python from:
echo https://www.python.org/downloads/windows/
echo.
goto END

:USE_PY
if "%PARALLEL_BACKUP_TEST%"=="1" goto TEST_PY
echo Python launcher found.
echo Starting Parallel Backup...
echo.
py -3 "%~dp0parallel_backup_launcher.py"
set "APP_EXIT=%errorlevel%"
goto SHOW_RESULT

:USE_PYTHON
if "%PARALLEL_BACKUP_TEST%"=="1" goto TEST_PYTHON
echo python.exe found.
echo Starting Parallel Backup...
echo.
python "%~dp0parallel_backup_launcher.py"
set "APP_EXIT=%errorlevel%"
goto SHOW_RESULT

:TEST_PY
echo Testing Python launcher...
py -3 --version
if errorlevel 1 goto TEST_FAILED
py -3 -m py_compile "%~dp0app.py"
if errorlevel 1 goto TEST_FAILED
py -3 -m py_compile "%~dp0parallel_backup_launcher.py"
if errorlevel 1 goto TEST_FAILED
py -3 -m pytest -q "%~dp0tests"
if errorlevel 1 goto TEST_FAILED
echo [PASS] BAT, app.py, launcher and regression tests passed.
exit /b 0

:TEST_PYTHON
echo Testing python.exe...
python --version
if errorlevel 1 goto TEST_FAILED
python -m py_compile "%~dp0app.py"
if errorlevel 1 goto TEST_FAILED
python -m py_compile "%~dp0parallel_backup_launcher.py"
if errorlevel 1 goto TEST_FAILED
python -m pytest -q "%~dp0tests"
if errorlevel 1 goto TEST_FAILED
echo [PASS] BAT, app.py, launcher and regression tests passed.
exit /b 0

:TEST_FAILED
echo [FAIL] BAT or Python regression test failed.
exit /b 1

:SHOW_RESULT
echo.
echo ==========================================
echo Exit code: %APP_EXIT%
echo ==========================================
echo.

:END
echo Press any key to close this window...
pause >nul
exit /b %APP_EXIT%

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
py -3 "%~dp0app.py"
goto SHOW_RESULT

:USE_PYTHON
if "%PARALLEL_BACKUP_TEST%"=="1" goto TEST_PYTHON
echo python.exe found.
echo Starting Parallel Backup...
echo.
python "%~dp0app.py"
goto SHOW_RESULT

:TEST_PY
echo Testing Python launcher...
py -3 --version
if errorlevel 1 goto TEST_FAILED
py -3 -m py_compile "%~dp0app.py"
if errorlevel 1 goto TEST_FAILED
echo [PASS] BAT and app.py test passed.
goto END

:TEST_PYTHON
echo Testing python.exe...
python --version
if errorlevel 1 goto TEST_FAILED
python -m py_compile "%~dp0app.py"
if errorlevel 1 goto TEST_FAILED
echo [PASS] BAT and app.py test passed.
goto END

:TEST_FAILED
echo [FAIL] BAT or app.py test failed.

:SHOW_RESULT
echo.
echo ==========================================
echo Exit code: %errorlevel%
echo ==========================================
echo.

:END
echo Press any key to close this window...
pause >nul
exit /b 0

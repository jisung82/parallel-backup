@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Parallel Backup

cd /d "%~dp0"

echo ==========================================
echo        Parallel Backup Launcher
echo ==========================================
echo.

set "PYTHON_EXE="
set "PYTHON_LAUNCHER="

rem 1. Prefer the real Python launcher when available.
for /f "delims=" %%P in ('where py.exe 2^>nul') do (
    "%%P" -3 --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_LAUNCHER=%%P"
        goto FOUND_LAUNCHER
    )
)

rem 2. Find a real python.exe and ignore the Microsoft Store WindowsApps alias.
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    echo %%P | findstr /I /C:"WindowsApps" >nul
    if errorlevel 1 (
        "%%P" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=%%P"
            goto FOUND_PYTHON
        )
    )
)

rem 3. Common per-user Python installation locations.
for /d %%P in ("%LocalAppData%ProgramsPythonPython*") do (
    if exist "%%~fPpython.exe" (
        "%%~fPpython.exe" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=%%~fPpython.exe"
            goto FOUND_PYTHON
        )
    )
)

rem 4. Common system-wide Python installation locations.
for /d %%P in ("%ProgramFiles%Python*") do (
    if exist "%%~fPpython.exe" (
        "%%~fPpython.exe" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=%%~fPpython.exe"
            goto FOUND_PYTHON
        )
    )
)

echo [ERROR] Python을 찾을 수 없습니다.
echo.
echo Python 설치 후 다시 실행하세요.
echo https://www.python.org/downloads/windows/
echo.
pause
exit /b 9009

:FOUND_LAUNCHER
echo Python Launcher: "!PYTHON_LAUNCHER!"
goto TEST_OR_RUN

:FOUND_PYTHON
echo Python: "!PYTHON_EXE!"
goto TEST_OR_RUN

:TEST_OR_RUN
if "%PARALLEL_BACKUP_TEST%"=="1" goto TEST

echo Starting Parallel Backup...
if defined PYTHON_LAUNCHER (
    "!PYTHON_LAUNCHER!" -3 "%~dp0app.py"
) else (
    "!PYTHON_EXE!" "%~dp0app.py"
)
set "EXIT_CODE=%errorlevel%"
goto END

:TEST
echo Testing Python launcher...
if defined PYTHON_LAUNCHER (
    "!PYTHON_LAUNCHER!" -3 --version
    if errorlevel 1 exit /b 1
    "!PYTHON_LAUNCHER!" -3 -m py_compile "%~dp0app.py"
) else (
    "!PYTHON_EXE!" --version
    if errorlevel 1 exit /b 1
    "!PYTHON_EXE!" -m py_compile "%~dp0app.py"
)
if errorlevel 1 (
    echo [FAIL] app.py syntax test failed.
    exit /b 1
)
echo [PASS] Python launcher and app.py syntax are valid.
exit /b 0

:END
echo.
if "%EXIT_CODE%"=="0" (
    echo Parallel Backup closed normally.
) else (
    echo Parallel Backup exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

@echo off
setlocal EnableExtensions

title Parallel Backup

cd /d "%~dp0"

echo ==========================================
echo        Parallel Backup Launcher
echo ==========================================
echo.

set "PYTHON_EXE="

rem 1. Prefer python.exe available on PATH.
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    set "PYTHON_EXE=%%P"
    goto RUN
)

rem 2. Try the Python launcher explicitly.
for /f "delims=" %%P in ('where py.exe 2^>nul') do (
    set "PYTHON_EXE=%%P"
    goto RUN_LAUNCHER
)

rem 3. Common per-user Python installation locations.
for /d %%P in ("%LocalAppData%\Programs\Python\Python*") do (
    if exist "%%~fP\python.exe" (
        set "PYTHON_EXE=%%~fP\python.exe"
        goto RUN
    )
)

rem 4. Common system-wide Python installation locations.
for /d %%P in ("%ProgramFiles%\Python*") do (
    if exist "%%~fP\python.exe" (
        set "PYTHON_EXE=%%~fP\python.exe"
        goto RUN
    )
)

echo [ERROR] Python을 찾을 수 없습니다.
echo.
echo 먼저 Python을 설치하거나 PATH에 추가해야 합니다.
echo https://www.python.org/downloads/windows/
echo.
pause
exit /b 9009

:RUN
echo Python: "%PYTHON_EXE%"
echo Starting Parallel Backup...
"%PYTHON_EXE%" "%~dp0app.py"
set "EXIT_CODE=%errorlevel%"
goto END

:RUN_LAUNCHER
echo Python Launcher: "%PYTHON_EXE%"
echo Starting Parallel Backup...
"%PYTHON_EXE%" -3 "%~dp0app.py"
set "EXIT_CODE=%errorlevel%"
goto END

:END
echo.
if "%EXIT_CODE%"=="0" (
    echo Parallel Backup closed normally.
) else (
    echo Parallel Backup exited with code %EXIT_CODE%.
    pause
)
exit /b %EXIT_CODE%

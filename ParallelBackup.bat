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

rem Find a real python.exe first. Ignore the Microsoft Store execution alias.
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    echo %%P | findstr /I /C:"\WindowsApps\" >nul
    if errorlevel 1 (
        "%%P" --version >nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_EXE=%%P"
            goto FOUND_PYTHON
        )
    )
)

rem Then try the Python launcher.
for /f "delims=" %%P in ('where py.exe 2^>nul') do (
    "%%P" -3 --version >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_LAUNCHER=%%P"
        goto FOUND_LAUNCHER
    )
)

echo [ERROR] 실제 Python 실행 파일을 찾지 못했습니다.
echo Python 설치: https://www.python.org/downloads/windows/
echo.
goto FINISH

:FOUND_PYTHON
echo Python: "!PYTHON_EXE!"
goto RUN_APP

:FOUND_LAUNCHER
echo Python Launcher: "!PYTHON_LAUNCHER!"
goto RUN_APP

:RUN_APP
echo Starting Parallel Backup...
echo.

if defined PYTHON_EXE (
    "!PYTHON_EXE!" "%~dp0app.py" 2>&1
    set "EXIT_CODE=!errorlevel!"
) else (
    "!PYTHON_LAUNCHER!" -3 "%~dp0app.py" 2>&1
    set "EXIT_CODE=!errorlevel!"
)

echo.
echo ==========================================
echo Program exit code: !EXIT_CODE!
echo ==========================================
echo.

if not "!EXIT_CODE!"=="0" (
    echo [ERROR] 프로그램이 오류와 함께 종료되었습니다.
    echo 위의 오류 내용을 확인하세요.
) else (
    echo 프로그램이 정상 종료되었습니다.
)

:FINISH
echo.
echo 이 창을 닫으려면 아무 키나 누르세요...
pause >nul
exit /b 0

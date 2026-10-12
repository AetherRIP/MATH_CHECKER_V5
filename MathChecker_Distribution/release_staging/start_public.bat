@echo off
rem Math Checker V5 - PUBLIC ACCESS launcher (the one authoritative mechanism)
rem
rem Starts Math Correct in public mode, waits until it is actually ready,
rem starts a Cloudflare tunnel against the EXACT running port, detects the
rem real public address, VERIFIES it end to end, then keeps the Remote Access
rem status truthful while it runs. Any failure is reported as Failed + reason.
rem
rem Usage:  start_public.bat [port]
setlocal
cd /d "%~dp0"

rem Keep the program folder clean (same rule as the normal launcher).
set "PYTHONDONTWRITEBYTECODE=1"

rem Use the SAME data directory as the normal launcher. Without this an
rem installed copy would create a second config/database inside Program Files
rem (unwritable) instead of reusing %LOCALAPPDATA%\MathChecker, and a portable
rem copy would not reuse its ./Data folder.
set "MC5_DATA_DIR="
if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0Data"
if not defined MC5_DATA_DIR if exist "%~dp0Data\config.toml" set "MC5_DATA_DIR=%~dp0Data"
if not defined MC5_DATA_DIR if exist "%~dp0runtime\python.exe" (
  set "MC5_DATA_DIR=%LOCALAPPDATA%\MathChecker"
  if not exist "%MC5_DATA_DIR%" mkdir "%MC5_DATA_DIR%" >nul 2>nul
)

set "PYEXE="
set "PYARGS="
if exist "%~dp0runtime\python.exe" set "PYEXE=%~dp0runtime\python.exe"
if not defined PYEXE if exist "%~dp0.venv\Scripts\python.exe" set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not defined PYEXE (
  where py >nul 2>nul && (set "PYEXE=py" & set "PYARGS=-3")
)
if not defined PYEXE (
  where python >nul 2>nul && set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo   Python was not found on this computer.
  echo   Install Python 3.12 or later from https://www.python.org/downloads/
  echo   and tick "Add Python to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

"%PYEXE%" %PYARGS% remote.py %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo   Remote access did not start. The reason is shown above.
  echo   Press any key to close.
  pause >nul
)
endlocal
exit /b %RC%

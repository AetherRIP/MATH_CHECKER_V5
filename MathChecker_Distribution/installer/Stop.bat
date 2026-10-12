@echo off
rem Math Checker — stop the running application.
rem
rem Use this when the app was started by a boot task, by another user or in a
rem window that is no longer visible: it proves the program on the port IS
rem Math Checker (health check) before stopping anything, and takes the
rem correction workers and any tunnel this copy started down with it. Your
rem data, settings, keys and reports are left untouched.
rem
rem For a normal foreground launch you can simply press Ctrl+C in the console
rem window or close it - that shuts the app down cleanly as well.
setlocal
cd /d "%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"

rem Same data directory as the launcher (see launcher.bat for why this is
rem decided before anything else, and why labels are used instead of blocks).
set "MC5_DATA_DIR="
if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0Data"
if defined MC5_DATA_DIR goto :data_decided
if exist "%~dp0Data\config.toml" set "MC5_DATA_DIR=%~dp0Data"
if defined MC5_DATA_DIR goto :data_decided
set "MC5_DATA_DIR=%LOCALAPPDATA%\MathChecker"
:data_decided

set "PYTHON="
if exist "%~dp0runtime\python.exe" set "PYTHON="%~dp0runtime\python.exe""
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON="%~dp0.venv\Scripts\python.exe""
if not defined PYTHON (
  where py >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
  where python >nul 2>nul && set "PYTHON=python"
)
if not defined PYTHON (
  echo.
  echo   Math Checker could not find its Python runtime, so it cannot look up
  echo   or stop the running instance. End "python.exe" in Task Manager.
  echo.
  pause
  exit /b 1
)

%PYTHON% main.py --stop %*
echo.
pause
endlocal

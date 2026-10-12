@echo off
rem Math Checker V5 - one-time Windows setup.
rem
rem Creates a NATIVE Windows virtual environment (.venv) and installs the
rem backend dependencies, then tells you how to start the app. Safe to re-run.
rem
rem Why this exists: a project folder prepared on Linux has its virtual
rem environment under .venv\bin with a Linux interpreter. That cannot run on
rem Windows, so start.bat ignores it and falls back to a system Python that
rem may not have the dependencies. This script fixes that: a foreign venv is
rem MOVED ASIDE (never deleted) and a proper Windows venv is created.
setlocal EnableExtensions
cd /d "%~dp0"

rem --- find Python ---------------------------------------------------------
set "PYEXE="
where py >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  echo.
  echo   Python was not found on this computer.
  echo   Install Python 3.12 or later from https://www.python.org/downloads/
  echo   and tick "Add Python to PATH", then run setup.bat again.
  echo.
  pause
  exit /b 1
)

rem --- reuse an existing Windows venv, or replace a foreign one ------------
if exist "%~dp0.venv\Scripts\python.exe" goto :have_venv
if exist "%~dp0.venv" (
  echo   Found a virtual environment that was not created on Windows
  echo   ^(.venv\Scripts\python.exe is missing^). Moving it aside to
  echo   .venv.foreign so nothing is lost ...
  if exist "%~dp0.venv.foreign" rmdir /s /q "%~dp0.venv.foreign"
  move "%~dp0.venv" "%~dp0.venv.foreign" >nul
)

echo   Creating the Windows virtual environment in .venv ...
%PYEXE% -m venv "%~dp0.venv"
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo.
  echo   Could not create the virtual environment. See the messages above.
  echo.
  pause
  exit /b 1
)

:have_venv
echo   Installing dependencies into .venv (this can take a minute) ...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0backend\requirements.txt"
if errorlevel 1 (
  echo.
  echo   Dependency installation failed. Check your internet connection and
  echo   run setup.bat again.
  echo.
  pause
  exit /b 1
)

rem --- register automatic startup (no manual commands after a reboot) -------
rem Preferred: a boot-time SYSTEM task (starts before sign-in, survives
rem logoff). That needs administrator rights, so Windows shows the normal
rem UAC prompt now. If it is declined or fails, fall back to a per-user
rem Startup entry so startup still works without any manual step.
echo.
echo   Installing automatic startup (Windows may ask for administrator permission - this lets Math Correct start before you sign in) ...
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\install_autostart.py" service
if errorlevel 1 (
  echo   Administrator startup was not registered; enabling per-user startup instead ...
  "%~dp0.venv\Scripts\python.exe" "%~dp0scripts\install_autostart.py" all
)
"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\install_autostart.py" verify

echo.
echo   Setup complete.
echo     * Start the app now with:  start.bat
rem (kept for compatibility; startup is already configured)
echo     * The frontend is prebuilt in frontend\dist; only run "npm install"
echo       and "npm run build" inside frontend\ if you change the UI code.
echo.
pause
endlocal

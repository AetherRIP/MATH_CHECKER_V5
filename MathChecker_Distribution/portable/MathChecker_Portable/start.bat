@echo off
rem Math Checker V5 - Windows launcher
rem
rem Runs the app without the user needing to understand Python or terminals.
rem Prefers a bundled runtime (portable install), then a bundled virtual
rem environment, then a system Python. Portable mode keeps all data next to
rem the application.
rem
rem The interpreter path is stored SEPARATELY from its arguments and always
rem quoted, so an install directory containing spaces (e.g. "Program Files")
rem works correctly.
setlocal
cd /d "%~dp0"

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
rem A virtual environment created on another operating system (for example
rem Linux, where the interpreter lives in .venv\bin) cannot run on Windows.
rem setup.bat replaces it with a native Windows venv; point the teacher there.
rem NOTE: labels (not parenthesised blocks) are used here on purpose. An
rem install folder may contain parentheses (e.g. "Math Checker (2026)"), and
rem cmd mis-parses a %~dp0 path containing "(" inside a ( ) block.
if not defined PYEXE goto :skip_venv_note
if not exist "%~dp0.venv" goto :skip_venv_note
if exist "%~dp0.venv\Scripts\python.exe" goto :skip_venv_note
echo   Note: the .venv folder was created on another operating system and
echo   cannot run on Windows. Run setup.bat once to build a Windows
echo   environment - your existing .venv is preserved, not deleted.
:skip_venv_note

if not defined PYEXE (
  echo.
  echo   Python was not found on this computer.
  echo   Install Python 3.12 or later from https://www.python.org/downloads/
  echo   and tick "Add Python to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

rem Portable mode: keep data beside the application when a portable.flag
rem file is present (a copied/extracted install), otherwise use the default
rem per-checkout data folder.
if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0data"

"%PYEXE%" %PYARGS% main.py %*
if errorlevel 1 (
  echo.
  echo   Math Checker stopped with an error. The details above can be copied
  echo   into a support message.
  echo   If the error mentions a missing module, run setup.bat once to install
  echo   the dependencies, then start the app again. Press any key to close.
  pause >nul
)
endlocal

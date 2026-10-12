@echo off
rem Math Checker — application launcher (portable and installed).
rem
rem Installed copy: user data lives in %LOCALAPPDATA%\MathChecker, so an
rem update or uninstall never destroys it.
rem Portable copy (this folder holds portable.flag and/or a Data folder):
rem data stays beside the application and the host machine is not written to.
rem
rem To stop the app again press Ctrl+C in this window, or run Stop.bat.
setlocal
cd /d "%~dp0"

rem Keep the program folder clean: never write .pyc bytecode beside the app
rem (it also avoids permission errors when installed in Program Files).
set "PYTHONDONTWRITEBYTECODE=1"

rem --- where does THIS copy keep its data? ----------------------------------
rem Decided before anything else touches the machine: a portable install must
rem not create folders in the user profile. Labels (not parenthesised blocks)
rem are used on purpose: an extraction folder may contain parentheses
rem (e.g. "Math Checker (v5)") and cmd mis-parses %~dp0 inside a ( ) block.
set "MC5_DATA_DIR="
if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0Data"
if defined MC5_DATA_DIR goto :data_decided
if exist "%~dp0Data\config.toml" set "MC5_DATA_DIR=%~dp0Data"
if defined MC5_DATA_DIR goto :data_decided
set "MC5_DATA_DIR=%LOCALAPPDATA%\MathChecker"
if not exist "%MC5_DATA_DIR%" mkdir "%MC5_DATA_DIR%" >nul 2>nul
:data_decided

rem --- which interpreter? ---------------------------------------------------
rem The bundled runtime path is quoted INSIDE the variable so a program folder
rem containing spaces (e.g. "C:\Program Files\Math Checker") works.
set "PYTHON="
if exist "%~dp0runtime\python.exe" set "PYTHON="%~dp0runtime\python.exe""
if not defined PYTHON if exist "%~dp0.venv\Scripts\python.exe" set "PYTHON="%~dp0.venv\Scripts\python.exe""
if not defined PYTHON (
  where py >nul 2>nul && set "PYTHON=py -3"
)
if not defined PYTHON (
  where python >nul 2>nul && set "PYTHON=python"
)
if defined PYTHON goto :run

echo.
echo   Math Checker could not find its Python runtime.
echo.
echo   The bundled "runtime" folder beside this file is missing, so this
echo   copy is incomplete. Extract MathChecker_Portable.zip again (or reinstall
echo   Math Checker) and start it from the fresh folder - do not copy single
echo   files out of the archive.
echo.
pause
exit /b 1

:run
%PYTHON% main.py %*
if errorlevel 1 (
  echo.
  echo   Math Checker stopped with an error. Copy the messages above into
  echo   a support message, or use Settings ^> Privacy ^> Export Diagnostics.
  echo.
  pause >nul
)
endlocal

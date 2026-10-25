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
rem
rem Two situations this launcher now pre-empts, because both look like "the app
rem is broken" while the files on disk are perfectly fine:
rem   * a copy is ALREADY RUNNING. It keeps serving the code and the built
rem     interface it was STARTED with, so a rebuild appears to do nothing and a
rem     page can fail against a newer interface than the server behind it (a
rem     Settings page that reads a field the running server does not send is
rem     exactly this). The launcher says so and offers a restart instead of
rem     silently attaching.
rem   * frontend\dist is missing. It is generated, never committed, so a fresh
rem     copy has no interface to serve and every page comes up empty.
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

rem --- which port does THIS copy use? -----------------------------------------
rem Precedence mirrors main.py's own resolve_stop_port(), because a check that
rem disagrees with the program it launches is worse than no check at all:
rem   1. data\instance.json - written by the launch that is actually running.
rem      It is the only source that identifies THIS copy when the configured
rem      port was already taken and main.py moved to another one; without it a
rem      launcher looks at the configured port, finds it free, and starts a
rem      SECOND copy while the first one keeps serving.
rem   2. the configured port in data\config.toml (the [network] section).
rem   3. the documented default (8642).
set "MC5_PORT="
set "MC5_PID="

rem An explicit port on the command line outranks all of the below: it is the
rem port main.py itself will bind, so the "already running" question has to be
rem asked about THAT port. Anything else as the first argument (--demo) is not a
rem port and leaves the resolution below untouched.
set "MC5_ARG1=%~1"
if not defined MC5_ARG1 goto :port_from_record
set "MC5_NOTNUM="
for /f "delims=0123456789" %%a in ("%MC5_ARG1%") do set "MC5_NOTNUM=%%a"
if defined MC5_NOTNUM goto :port_from_record
set "MC5_PORT=%MC5_ARG1%"
goto :port_decided

:port_from_record
if not exist "%~dp0data\instance.json" goto :port_from_config
rem A fixed-string findstr is enough: "port" appears on exactly one line of the
rem record. The record is only honoured while its process is still alive, so a
rem file left behind by a crashed copy cannot send us to a port nobody serves.
for /f "tokens=2 delims=:, " %%a in ('findstr /c:"port" "%~dp0data\instance.json" 2^>nul') do set "MC5_PORT=%%a"
for /f "tokens=2 delims=:, " %%a in ('findstr /c:"pid" "%~dp0data\instance.json" 2^>nul') do set "MC5_PID=%%a"
if not defined MC5_PORT goto :port_from_config
if not defined MC5_PID goto :port_from_config
tasklist /fi "PID eq %MC5_PID%" 2>nul | findstr /c:"%MC5_PID%" >nul || goto :port_from_config
goto :port_decided

:port_from_config
set "MC5_PID="
set "MC5_PORT="
if not exist "%~dp0data\config.toml" goto :port_default
rem "port = 8642" is written at the start of a line under [network]. Anchoring
rem there keeps an indented same-named key out of the answer, and delims="= "
rem makes token 2 the number for both "port = 8642" and "port=8642".
rem The FIRST match wins (a later section that happens to spell a line the same
rem way must not override the configured port). findstr cannot follow TOML
rem sections, so this stays a fallback: the instance record above is the source
rem that actually belongs to the running copy.
for /f "tokens=2 delims== " %%a in ('findstr /r /c:"^port *= *[0-9]" "%~dp0data\config.toml" 2^>nul') do if not defined MC5_PORT set "MC5_PORT=%%a"
:port_default
if not defined MC5_PORT set "MC5_PORT=8642"
:port_decided

rem --- is a copy already running on that port? ---------------------------------
rem Checked BEFORE launching and only ever REPORTED: this launcher never stops a
rem running app on its own, because that app may be mid-correction. main.py
rem always would have attached to a running copy and said so; the point of the
rem check here is to offer the RESTART, which is the only thing that makes a
rem rebuild or an edited file take effect.
netstat -ano | findstr ":%MC5_PORT% " | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto :not_running

echo.
echo   Port %MC5_PORT% is already in use, so a copy of Math Checker is probably
echo   still running - "Restart" below proves it before anything is stopped.
echo.
echo   A running copy keeps serving the code and the interface it was STARTED
echo   with. Anything changed since - a rebuilt interface, an edited file, a new
echo   setting field - is NOT part of it, so a page can look wrong while the
echo   files on disk are correct. Restarting is what loads what is on disk now.
echo.
set "REPLY="
set /p "REPLY=  Restart it? [R = stop it and start the updated copy, Enter = just open the running one] "
if /i not "%REPLY%"=="R" goto :not_running
echo   Stopping the running copy on port %MC5_PORT% ...
"%PYEXE%" %PYARGS% main.py --stop %MC5_PORT%
rem main.py proves the holder IS Math Checker over the health endpoint before it
rem kills anything, so a different program on this port is never touched. It
rem also returns only once the port is released, so the new copy cannot meet its
rem own predecessor; if a copy is still there, main.py reports that honestly.
netstat -ano | findstr ":%MC5_PORT% " | findstr "LISTENING" >nul 2>nul
if errorlevel 1 goto :restart_finished
rem Still held. The usual reason is that the copy was started AUTOMATICALLY at
rem boot by the Windows Task Scheduler (setup.bat registers that), which runs it
rem as SYSTEM: Windows then refuses an ordinary stop, and no amount of clicking
rem here changes that. Say so, with the one thing that does work.
echo.
echo   Math Checker could NOT be stopped from this window - something still
echo   holds port %MC5_PORT% (see the messages above).
echo.
echo   If it starts automatically at boot it is running as SYSTEM, and Windows
echo   only lets an administrator stop it:
echo.
echo       * right-click Stop.bat, choose "Run as administrator", then run
echo         start.bat again; or
echo       * restart the computer, then run start.bat.
echo.
echo   Starting on top of it would only add a second copy, so this launcher can
echo   attach to the running one (Enter above) but not replace it.
:restart_finished
echo.
:not_running

rem --- the interface must have been built (frontend\dist is generated) --------
if exist "%~dp0frontend\dist\index.html" goto :ui_ready
echo.
echo   Note: this folder has no built interface yet
echo   ^(frontend\dist\index.html is missing^). The server will start, but the
echo   browser will show an empty page. Build it once, then run this file again:
echo.
echo       cd frontend
echo       npm ci
echo       npm run build
echo.
:ui_ready

rem Portable mode: keep data beside the application when a portable.flag
rem file is present (a copied/extracted install), otherwise use the default
rem per-checkout data folder.
if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0data"

"%PYEXE%" %PYARGS% main.py %*
set "MC5_RC=%ERRORLEVEL%"
if not "%MC5_RC%"=="0" (
  echo.
  echo   Math Checker stopped with an error. The details above can be copied
  echo   into a support message.
  echo   If the error mentions a missing module, run setup.bat once to install
  echo   the dependencies, then start the app again. Press any key to close.
  pause >nul
)
rem The app's own exit code is passed on (0 = it ran, 2 = bad port argument,
rem 4 = port conflict, ...) so a script or a shortcut that ran this launcher can
rem tell success from failure. Pausing first used to reset it to 0.
endlocal & exit /b %MC5_RC%

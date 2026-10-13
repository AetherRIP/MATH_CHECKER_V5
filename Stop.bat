@echo off
rem Math Checker V5 - stop the running application.
rem
rem Use this when the app was started by a boot task, by another user or in a
rem console window that is no longer visible. It proves the program on the port
rem IS Math Checker (health check) before stopping anything, and takes the
rem correction workers and any tunnel this copy started down with it. Your
rem data, settings, keys and reports are left untouched.
rem
rem For a normal foreground launch you can simply press Ctrl+C in the console
rem window or close it - that shuts the app down cleanly as well.
rem
rem This file exists because main.py documents "python main.py --stop (Stop.bat)"
rem and the portable build ships Stop.bat, but this checkout had no Stop.bat:
rem the documented way to stop a copy was missing exactly where it was needed,
rem which is how an older instance kept serving the port after a rebuild.
rem
rem ONE CASE NEEDS ADMINISTRATOR RIGHTS: setup.bat registers a boot-time task
rem (Windows Task Scheduler, /RU SYSTEM), and Windows will not let an ordinary
rem window stop a process owned by SYSTEM. If the stop is refused for that
rem reason, main.py says so and this file points at the fix - right-click
rem Stop.bat and choose "Run as administrator". Nothing here pretends a stop
rem succeeded: the summary printed at the end follows main.py's own exit code
rem (0 = stopped or nothing was running, anything else = it did not stop).
setlocal
cd /d "%~dp0"

rem Same interpreter choice and the same data-directory rule as start.bat, so
rem the two files can never disagree about which copy they are talking about.
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
  echo   Math Checker could not find its Python runtime, so it cannot look up
  echo   or stop the running instance. End "python.exe" in Task Manager.
  echo.
  pause
  exit /b 1
)

if exist "%~dp0portable.flag" set "MC5_DATA_DIR=%~dp0data"

echo   Asking the running copy on this install's port to stop ...
echo.
"%PYEXE%" %PYARGS% main.py --stop %*
rem main.py proves the holder IS Math Checker over the health endpoint before it
rem kills anything, so a program that merely holds the port is never touched.
rem Its exit code is the honest summary: 0 means stopped (or nothing was
rem running), 5 means it refused or could not stop the process. The one thing
rem that needs an explanation is the failure, so it is the only one that gets
rem one - printing it after a clean stop would just be noise.
if errorlevel 1 goto :stop_failed
rem Fall through to the end on success: main.py has already said what it did.
echo.
pause
endlocal
exit /b 0

:stop_failed
echo.
echo   The stop did NOT succeed - read the messages above for the reason.
echo   A very common one: this copy starts automatically at boot (setup.bat
echo   registers a Windows Task Scheduler task that runs it as SYSTEM), and
echo   Windows will not let an ordinary window stop a process owned by SYSTEM.
echo.
echo   The fix is one right-click: choose "Run as administrator" on this file,
echo   then start Math Checker again with start.bat.
echo.
pause
endlocal
exit /b 1

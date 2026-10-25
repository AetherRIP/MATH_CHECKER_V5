@echo off
rem Math Checker V5 - Windows desktop launcher
rem
rem Starts the native WPF client in desktop\src\MathChecker.Desktop. That client
rem finds main.py itself, runs the backend with the checkout's .venv (or a
rem bundled runtime), waits for /api/health and then shows its window - so this
rem launcher never runs main.py, and start.bat is not needed alongside it.
rem
rem The window it opens must be the interface the code on disk describes RIGHT
rem NOW, never an older binary left behind by a previous build. Two things used
rem to break that:
rem
rem   * the client was only rebuilt when "dotnet" happened to be on PATH. A
rem     runtime-only .NET install (no SDK) satisfies "where dotnet" and then
rem     fails the build, so the launcher started the previous V5.exe - an old
rem     interface while the source had already moved on. The SDK is now probed
rem     with "dotnet --list-sdks", including the per-user install at
rem     %USERPROFILE%\.dotnet, and a failed build is reported instead of being
rem     papered over with an older copy.
rem   * an already-open window keeps the interface it was STARTED with. Starting
rem     V5.exe again only raises that old window, so a rebuild never becomes
rem     visible - the app looked stuck on the previous UI. The launcher now
rem     offers to close the open window and start the current build.
rem
rem What it starts:
rem   1. a release folder (main.py + desktop\V5.exe + runtime\python.exe beside
rem      this file): that copy IS the program, freshly published by
rem      desktop\build_release.ps1, so no build is attempted;
rem   2. otherwise the client built from the code on disk, rebuilding it first;
rem   3. with no .NET SDK, an earlier build on this machine, with its build date
rem      printed so it is clear the source was not rebuilt.
rem
rem NOTE: labels are used instead of parenthesised blocks on purpose. A folder
rem name may contain parentheses (e.g. "Math Checker (2026)") and cmd
rem mis-parses a %~dp0 path containing "(" inside a ( ) block.
setlocal
cd /d "%~dp0"

set "MC5_DESKTOP_PROJECT=%~dp0desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj"
set "MC5_DESKTOP_EXE=%~dp0desktop\src\MathChecker.Desktop\bin\Release\net10.0-windows\V5.exe"
set "MC5_PACKAGED_EXE=%~dp0desktop\artifacts\MATH_CHECKER_V5\desktop\V5.exe"

rem --- is this an extracted release instead of a source checkout? -------------
rem A release is self-contained by design: the client carries its own .NET
rem runtime and runtime\python.exe is the backend's interpreter, so nothing has
rem to be installed or looked up. A source checkout has no desktop\V5.exe, so
rem it never takes this branch.
set "MC5_RELEASE="
if exist "%~dp0main.py" if exist "%~dp0desktop\V5.exe" if exist "%~dp0runtime\python.exe" if exist "%~dp0backend\app\main.py" set "MC5_RELEASE=1"
if not defined MC5_RELEASE goto :source_layout

call :close_open_window
if "%MC5_CLOSED%"=="keep" goto :kept_open
if "%MC5_CLOSED%"=="timeout" goto :close_failed

echo   Starting Math Checker ...
start "" "%~dp0desktop\V5.exe"
endlocal & exit /b 0

rem --- a source checkout: build the current code, then start it ----------------
:source_layout
rem "where dotnet" only proves a host exists: a runtime-only install answers
rem that and still fails the build. Ask the host for its SDKs, and fall back to
rem the per-user install a machine without the system-wide SDK usually has.
set "MC5_DOTNET="
set "MC5_HAVE_SDK="

where dotnet >nul 2>nul
if errorlevel 1 goto :try_user_dotnet
set "MC5_DOTNET=dotnet"
call :probe_sdk
if defined MC5_HAVE_SDK goto :sdk_ready

:try_user_dotnet
if not exist "%USERPROFILE%\.dotnet\dotnet.exe" goto :try_local_dotnet
set "MC5_DOTNET=%USERPROFILE%\.dotnet\dotnet.exe"
call :probe_sdk
if defined MC5_HAVE_SDK goto :sdk_ready

:try_local_dotnet
if not exist "%LOCALAPPDATA%\Microsoft\dotnet\dotnet.exe" goto :no_sdk
set "MC5_DOTNET=%LOCALAPPDATA%\Microsoft\dotnet\dotnet.exe"
call :probe_sdk
if defined MC5_HAVE_SDK goto :sdk_ready
goto :no_sdk

:sdk_ready
call :close_open_window
if "%MC5_CLOSED%"=="keep" goto :kept_open
if "%MC5_CLOSED%"=="timeout" goto :close_failed

if not exist "%MC5_DESKTOP_PROJECT%" goto :build_failed
echo   Building the desktop client from the current source, then starting it.
echo   An up-to-date build is quick; the first one also restores its NuGet
echo   packages and can take a minute.
echo.
"%MC5_DOTNET%" build "%MC5_DESKTOP_PROJECT%" -c Release --nologo -v minimal
if errorlevel 1 goto :build_failed
if not exist "%MC5_DESKTOP_EXE%" goto :build_failed

rem The client searches for main.py and the .venv upward from where it is
rem started, so the current directory stays this project folder.
start "" "%MC5_DESKTOP_EXE%"
endlocal & exit /b 0

rem --- no SDK: start the newest copy that already exists -----------------------
:no_sdk
if exist "%MC5_DESKTOP_EXE%" goto :run_built
if exist "%MC5_PACKAGED_EXE%" goto :run_packaged
goto :missing

:run_packaged
call :close_open_window
if "%MC5_CLOSED%"=="keep" goto :kept_open
if "%MC5_CLOSED%"=="timeout" goto :close_failed
echo   Starting the packaged desktop build (this folder has no .NET SDK to
echo   build the current source with).
start "" "%MC5_PACKAGED_EXE%"
endlocal & exit /b 0

:run_built
call :close_open_window
if "%MC5_CLOSED%"=="keep" goto :kept_open
if "%MC5_CLOSED%"=="timeout" goto :close_failed
for %%f in ("%MC5_DESKTOP_EXE%") do set "MC5_BUILT=%%~tf"
echo.
echo   No .NET SDK was found, so the current source cannot be rebuilt here.
echo   Starting the desktop client built earlier in this folder:
echo     %MC5_DESKTOP_EXE%
echo     built %MC5_BUILT%
echo.
echo   If that window does not show the latest interface, install the .NET 10
echo   SDK from https://dotnet.microsoft.com/download and run this file again -
echo   it will rebuild first.
start "" "%MC5_DESKTOP_EXE%"
endlocal & exit /b 0

rem --- helpers ----------------------------------------------------------------
:close_open_window
rem An open window keeps the interface it was started with AND locks its
rem V5.exe, so a rebuild cannot replace it. Closing it (gracefully: taskkill
rem asks the app to exit, it does not kill it) is what lets the current build
rem become visible. Sets MC5_CLOSED to none / closed / keep / timeout.
set "MC5_CLOSED=none"
tasklist /fi "IMAGENAME eq V5.exe" 2>nul | findstr /i /c:"V5.exe" >nul
if errorlevel 1 goto :eof

echo.
echo   A Math Checker window is already open. It keeps the interface it was
echo   STARTED with, so anything changed since - including the current build -
echo   is not in it.
echo.
set "MC5_REPLY="
set /p "MC5_REPLY=  Close it and start the current build? [Enter = yes, K = keep it open] "
if /i "%MC5_REPLY%"=="K" goto :close_keep
echo   Closing the open window ...
taskkill /im V5.exe >nul 2>nul
set /a MC5_WAIT=0
:close_wait
tasklist /fi "IMAGENAME eq V5.exe" 2>nul | findstr /i /c:"V5.exe" >nul
if errorlevel 1 goto :close_done
set /a MC5_WAIT+=1
if %MC5_WAIT% GEQ 20 goto :close_timeout
ping -n 2 127.0.0.1 >nul
goto :close_wait
:close_done
set "MC5_CLOSED=closed"
goto :eof
:close_keep
set "MC5_CLOSED=keep"
goto :eof
:close_timeout
set "MC5_CLOSED=timeout"
goto :eof

:probe_sdk
rem Sets MC5_HAVE_SDK when "%MC5_DOTNET% --list-sdks" prints at least one SDK.
rem The output goes through a file so an interpreter path containing spaces
rem cannot break the quoting.
set "MC5_HAVE_SDK="
"%MC5_DOTNET%" --list-sdks >"%TEMP%\mc5_sdks.txt" 2>nul
if errorlevel 1 goto :probe_done
for /f "usebackq delims=" %%v in ("%TEMP%\mc5_sdks.txt") do set "MC5_HAVE_SDK=1"
:probe_done
del "%TEMP%\mc5_sdks.txt" >nul 2>nul
goto :eof

rem --- outcomes ---------------------------------------------------------------
:kept_open
echo   Keeping the open window. Nothing was rebuilt, because a rebuild cannot
echo   replace a running executable.
endlocal & exit /b 0

:close_failed
echo.
echo   The open window did not close by itself, so nothing was rebuilt or
echo   started. It may be waiting on a dialog inside the app. Close it by hand
echo   (File - Exit, or the window's close button) and run this file again.
echo.
pause
endlocal & exit /b 1

:build_failed
echo.
echo   The desktop client did NOT build (the compiler output is above), so
echo   nothing was started. An older desktop\artifacts build was NOT launched
echo   instead, because it would not contain the code you have now.
echo.
echo   The usual causes: the .NET 10 SDK is missing or older than the project,
echo   or a NuGet package could not be restored. Build it by hand to see the
echo   full output:
echo.
echo       dotnet build desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj
echo.
pause
endlocal & exit /b 1

:missing
echo.
echo   The .NET 10 SDK was not found on this computer, and this folder contains
echo   no built desktop client to start instead.
echo.
echo   Either install the SDK from https://dotnet.microsoft.com/download and run
echo   this file again, or build the packaged version once:
echo.
echo       powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1
echo.
echo   which produces desktop\artifacts\MATH_CHECKER_V5\desktop\V5.exe - that
echo   copy needs no .NET or Python install and is started by double-clicking.
echo.
pause
endlocal & exit /b 1

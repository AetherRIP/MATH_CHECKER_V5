@echo off
rem ============================================================================
rem  Math Checker — release builder (one command)
rem
rem  Builds the shareable distribution from the PRIVATE master project:
rem     1. clean release staging
rem     2. production frontend build
rem     3. copy allowlisted files only
rem     4. secret + privacy scan  (BLOCKS the build on any finding)
rem     5. dependency validation
rem     6. portable package (folder + zip)
rem     7. Windows installer (Inno Setup, themed)
rem     8. SHA-256 hashes
rem     9. release manifest
rem    10. final validation
rem
rem  A self-contained Python runtime is bundled automatically (one time), so
rem  recipients need no Python, Node or developer tools. The private project
rem  is never modified. Outputs land in .\release\.
rem
rem  Usage:
rem     build_release.bat
rem     build_release.bat --skip-frontend-build
rem     build_release.bat --with-runtime D:\MyRuntime   (override the runtime)
rem ============================================================================
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo.
  echo   Python 3.12+ is required to build the release. Install it from
  echo   https://www.python.org/downloads/ and tick "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

set "RUNTIME=%~dp0build\runtime"
if not exist "%RUNTIME%\python.exe" (
  echo.
  echo   Building the bundled Python runtime ^(one time, needs internet^)...
  %PY% "%~dp0scripts\build_runtime.py"
  if errorlevel 1 (
    echo   Could not build the bundled runtime. See the messages above.
    pause
    exit /b 1
  )
)

%PY% "%~dp0scripts\build_release.py" --with-runtime "%RUNTIME%" %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
  echo   Release build FAILED. Nothing was shipped.
) else (
  echo   Release build finished. See the "release" folder.
)
pause
exit /b %CODE%

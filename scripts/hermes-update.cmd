@echo off
setlocal

REM Hermes Agent Update for Windows (CMD wrapper)
REM Pulls upstream origin/main, re-applies the local pins, rebuilds.
REM
REM Usage (from CMD, PowerShell, or Git Bash):
REM   scripts\hermes-update.cmd
REM
REM Pre-condition: Hermes Desktop is closed (auto-killed at step 1 if open).
REM Reads: scripts\hermes-update.local-pins.json

REM Delegate execution to %TEMP% to prevent git operations from deleting/locking the running script
if /i "%~dp0" neq "%TEMP%\hermes-update-runner\" (
    if not exist "%TEMP%\hermes-update-runner" mkdir "%TEMP%\hermes-update-runner"
    copy /y "%~f0" "%TEMP%\hermes-update-runner\hermes-update.cmd" >nul
    copy /y "%~dp0hermes-update.local-pins.json" "%TEMP%\hermes-update-runner\" >nul
    copy /y "%~dp0update_from_pins.py" "%TEMP%\hermes-update-runner\" >nul
    call "%TEMP%\hermes-update-runner\hermes-update.cmd" "%~dp0.." "%~dp0" %*
    exit /b %errorlevel%
)

set "REPO=%~1"
set "ORIG_SCRIPTS_DIR=%~2"
if "%REPO%"=="" set "REPO=%~dp0.."
if "%ORIG_SCRIPTS_DIR%"=="" set "ORIG_SCRIPTS_DIR=%REPO%\scripts"
set "PIN=%~dp0hermes-update.local-pins.json"
set "LOG=%TEMP%\hermes-update-%RANDOM%.log"

echo === Hermes update starting ===
echo   repo:      %REPO%
echo   pins:      %PIN%
echo   log:       %LOG%
echo.

REM ===========================================================================
REM  Step 1: kill any Hermes Desktop / gateway processes
REM ===========================================================================
echo [1/8] stopping Hermes Desktop and gateway...
powershell -NoProfile -Command "$p = Get-Process -Name 'Hermes' -ErrorAction SilentlyContinue; if ($p) { $p | Stop-Process -Force }"
powershell -NoProfile -Command "$p = Get-Process -Name 'python' -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*hermes_cli.main*' }; if ($p) { $p | Stop-Process -Force }"
echo       done.

REM ===========================================================================
REM  Step 2: fetch upstream + fork
REM ===========================================================================
echo [2/8] fetching upstream...
pushd "%REPO%"
git fetch origin
if errorlevel 1 (
  echo       FAILED to fetch origin.
  popd
  exit /b 2
)
git fetch fork 2>nul
echo       done.

REM ===========================================================================
REM  Step 3: checkout main from origin/main
REM ===========================================================================
echo [3/8] checking out main branch from origin/main...
git checkout -B main origin/main
if errorlevel 1 (
  echo       FAILED to create or check out branch.
  popd
  exit /b 3
)
REM Ensure updater scripts are present in repo
copy /y "%~dp0*.*" "%ORIG_SCRIPTS_DIR%\" >nul
echo       on branch: main
popd

REM ===========================================================================
REM  Step 4: read pins and apply "keep" commits
REM ===========================================================================
echo [4/8] reading pins and applying kept commits...

python "%~dp0update_from_pins.py" "%REPO%" "%PIN%" 2>>"%LOG%"
if errorlevel 1 (
  echo       FAILED to apply kept commits - check log: %LOG%
  popd
  exit /b 4
)
echo       done.

REM ===========================================================================
REM  Step 5: typecheck
REM ===========================================================================
echo [5/8] running typecheck...
pushd "%REPO%\apps\desktop"
call npm run typecheck 2>>"%LOG%"
if errorlevel 1 (
  echo       typecheck FAILED - check log: %LOG%
  popd
  exit /b 5
)
echo       done.
popd

REM ===========================================================================
REM  Step 6: desktop tests
REM ===========================================================================
echo [6/8] running desktop tests...
pushd "%REPO%\apps\desktop"
call npm run test:desktop:platforms 2>>"%LOG%"
if errorlevel 1 (
  echo       warning: some platform tests failed on Windows (check log: %LOG%)
) else (
  echo       done.
)
popd

REM ===========================================================================
REM  Step 7: rebuild desktop app
REM ===========================================================================
echo [7/8] rebuilding desktop app...
pushd "%REPO%\apps\desktop"
call npm run dist:win 2>>"%LOG%"
if errorlevel 1 (
  echo       desktop rebuild FAILED - check log: %LOG%
  popd
  exit /b 7
)
echo       done.
popd

REM ===========================================================================
REM  Step 8: report results
REM ===========================================================================
echo.
echo === Update complete ===
echo   Branch: main
echo   Pins:   %PIN%
echo   Log:    %LOG%
echo.
echo   To install the new build, drop release\win-unpacked\ over your current install.
echo.
echo   Full log: %LOG%
echo.
if not defined HERMES_UPDATE_NO_PAUSE pause
endlocal

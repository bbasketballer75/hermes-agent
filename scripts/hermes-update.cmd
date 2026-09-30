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
REM 2026-09-28 FIX: this step used to fetch only `origin` and `fork`, which are
REM BOTH this user's own repo (bbasketballer75/hermes-agent). The real upstream
REM is the `upstream` remote (NousResearch/hermes-agent) and it was never
REM fetched, so `git checkout -B main origin/main` re-synced the fork with
REM itself and could never pull an upstream change. Fetch all three; `upstream`
REM is the one that matters.
git fetch upstream
if errorlevel 1 (
  echo       FAILED to fetch upstream.
  popd
  exit /b 2
)
git fetch origin
if errorlevel 1 (
  echo       FAILED to fetch origin.
  popd
  exit /b 2
)
git fetch fork 2>nul
echo       done.

REM ===========================================================================
REM  Step 3: pre-flight, then checkout main from upstream/main (NousResearch)
REM ===========================================================================
REM 2026-09-30 FIX: this checkout is destructive. Before running it, prove that
REM every commit living only on the local branch is reproducible from the pin
REM file. Without this guard an update silently dropped 4 Windows fixes plus the
REM PowerShell-quoting port and the updater itself, because the pin file had been
REM reconciled down to discard-only entries and re-applied nothing.
python "%~dp0update_from_pins.py" "%REPO%" "%PIN%" --verify --against upstream/main
if errorlevel 1 (
  echo.
  echo       ABORTED: local work is not covered by the pin file.
  echo       The checkout below would discard it. Fix the pin file or land the
  echo       commits upstream, then re-run. Nothing was changed.
  popd
  exit /b 2
)

echo [3/8] checking out main branch from upstream/main...
REM 2026-09-28 FIX: base must be upstream/main (NousResearch), not origin/main.
REM origin and fork both point at this user's own repo, so basing on origin/main
REM discarded the upstream history and left the pins cherry-picking onto a base
REM that already contained them - which is what produced the package-lock.json
REM conflicts. upstream/main is the real base; the pins and any unpinned local
REM work are then re-applied on top in step 4.
REM
REM 2026-09-28 FIX 2: the updater scripts are git-tracked AND locally modified
REM (that is how the fixes above get in), so git refuses the branch switch with
REM "Your local changes ... would be overwritten by checkout" and the whole
REM update dies at step 3. The authoritative copies were already staged in
REM %TEMP%\hermes-update-runner at the top of this file, so discarding the
REM working-tree copies here loses nothing - they are restored from %TEMP%
REM immediately after the checkout below. `--force` is what makes it succeed.
git checkout --force -B main upstream/main
if errorlevel 1 (
  echo       FAILED to create or check out branch.
  popd
  exit /b 3
)
REM Ensure updater scripts are present in repo (they are git-tracked, so the
REM checkout above can replace them with upstream's copy - restore ours)
copy /y "%~dp0*.*" "%ORIG_SCRIPTS_DIR%\" >nul
echo       on branch: main, based on upstream/main
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
  echo       warning: some platform tests failed on Windows ^(check log: %LOG%^)
) else (
  echo       done.
)
popd

REM ===========================================================================
REM  Step 7: rebuild desktop app
REM ===========================================================================
REM 2026-09-30 FIX: this step ran `npm run dist:win`, which builds the MSIX
REM installer only. This machine does not install from MSIX -- the desktop
REM shortcut launches the UNPACKED build at
REM   apps\desktop\release\win-unpacked\Hermes.exe
REM so `dist:win` never rebuilt the binary actually in use, and it also wiped
REM win-unpacked on its way to failing MSIX packaging, leaving the shortcut
REM pointing at a missing Hermes.exe. `npm run pack` (builder --dir) is the
REM target that produces win-unpacked.
echo [7/8] rebuilding desktop app (unpacked target)...
pushd "%REPO%\apps\desktop"
REM 2026-09-30: electron-builder must unlink release\win-unpacked before it can
REM repack. If a running Hermes app or an antivirus scan still holds
REM resources\app.asar, that unlink fails as a bare "EBUSY: resource busy or
REM locked" with no hint about the cause. Probe it first and say something useful.
pwsh -NoProfile -NonInteractive -Command "$p='release\win-unpacked\resources\app.asar'; if (Test-Path -LiteralPath $p) { try { $f=[IO.File]::Open($p,'Open','ReadWrite','None'); $f.Close() } catch { Write-Output ('LOCKED ' + `$args[0]); exit 3 } }" "%REPO%\apps\desktop\release\win-unpacked\resources\app.asar" >nul 2>&1
if errorlevel 3 (
  echo       BLOCKED: release\win-unpacked\resources\app.asar is locked.
  echo       Close any running Hermes desktop app, and if an antivirus scan is
  echo       in progress let it finish ^(Defender re-scans a tree for a long
  echo       time after an exclusion changes^). Then re-run the update.
  popd
  exit /b 7
)
call npm run pack 2>>"%LOG%"
if errorlevel 1 (
  echo       desktop rebuild FAILED - check log: %LOG%
  popd
  exit /b 7
)
if not exist "release\win-unpacked\Hermes.exe" (
  echo       desktop rebuild produced NO release\win-unpacked\Hermes.exe
  echo       the desktop shortcut would point at a missing binary - check log: %LOG%
  popd
  exit /b 7
)
echo       done ^(release\win-unpacked\Hermes.exe^)
popd

pushd "%REPO%"
REM 2026-09-28: main is now based on upstream/main, so track that, not fork/main.
git branch -u upstream/main main >nul 2>&1
echo   Base:   main tracks upstream/main
REM Pushing the rebased main to this user's own fork needs a force. Kept
REM --force-with-lease (not a bare --force) so it fails safely if the remote
REM moved or the branch is protected, rather than clobbering it. This is a
REM big change: the fork is thousands of commits behind upstream.
git push fork main --force-with-lease
if errorlevel 1 (
  echo   Fork:   PUSH FAILED - not overwritten ^(network, credentials, or branch protection^)
  echo           local main is intact and still has every pin; retry when ready
) else (
  echo   Fork:   pushed to fork/main ^(bbasketballer75/hermes-agent^)
)
popd

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

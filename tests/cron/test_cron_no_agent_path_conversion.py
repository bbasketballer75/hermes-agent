"""Regression tests for the Windows no-agent script execution path.

The historical failure (incident cron-execution:512e7921301b, observed
2026-08-10T09:00:56) was:

    /bin/bash: C:UsersbbaskAppDataLocalhermesscriptshermes-reflog-guard.sh:
    No such file or directory

Backslashes in the path were stripped, leaving a path with no separators.
The cause: cron passed a Windows-style path to bash as argv, and the bash
binary that was selected in the scheduled-task context did not auto-convert
Windows paths to POSIX form (as an interactive MSYS bash often does).

The carry-forward fix is ``_to_bash_argv_path`` in
``cron/scheduler.py``, which converts ``C:\\Users\\...`` to ``/c/Users/...``
before handing it to bash. This test locks in the conversion so a future
refactor cannot silently regress to passing a backslash path to bash.

It also verifies the conversion is a no-op on POSIX platforms (Linux/macOS)
and for paths that are already in MSYS form (so the function is total).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Importing the module triggers no side effects; the function is pure.
from cron.scheduler import _to_bash_argv_path  # noqa: E402


class TestToBashArgvPath:
    """Lock in the backslash-to-MSYS conversion contract."""

    def test_windows_drive_letter_lowercased_to_posix(self) -> None:
        """``C:\\Users\\...`` -> ``/c/Users/...``."""
        result = _to_bash_argv_path(Path(r"C:\Users\bbask\AppData\Local\hermes\scripts\foo.sh"))
        if sys.platform == "win32":
            assert result == "/c/Users/bbask/AppData/Local/hermes/scripts/foo.sh"
        else:
            # On POSIX, the path is returned as-is (as_posix form).
            assert result == "C:/Users/bbask/AppData/Local/hermes/scripts/foo.sh"

    def test_reflog_guard_incident_path(self) -> None:
        """The exact path from the 2026-08-10 incident must convert cleanly.

        The failure mode was backslashes-stripped: ``C:\\Users\\bbask\\...``
        arriving at bash as ``CUsersbbask...``. The function's job is to
        guarantee this never reaches bash.
        """
        path = Path(r"C:\Users\bbask\AppData\Local\hermes\scripts\hermes-reflog-guard.sh")
        result = _to_bash_argv_path(path)
        if sys.platform == "win32":
            # Critical assertion: NO backslashes in the result.
            assert "\\" not in result, (
                f"backslash leaked into bash argv: {result!r} — would re-trigger "
                "the cron-execution:512e7921301b incident"
            )
            assert result == "/c/Users/bbask/AppData/Local/hermes/scripts/hermes-reflog-guard.sh"
        else:
            assert "\\" not in result

    def test_drive_letter_d_also_converted(self) -> None:
        """The conversion is per-drive, not hardcoded to C:. (Regression: an
        earlier version only handled C: and let D: paths through with a
        leading slash, breaking backup-drive workflows.)"""
        if sys.platform != "win32":
            pytest.skip("Windows-only path format")
        result = _to_bash_argv_path(Path(r"D:\backups\hermes\scripts\foo.sh"))
        assert result == "/d/backups/hermes/scripts/foo.sh"
        assert "\\" not in result

    def test_already_posix_path_returned_unchanged(self) -> None:
        """MSYS-style input should be returned verbatim — no double conversion."""
        if sys.platform != "win32":
            pytest.skip("MSYS is Windows-specific")
        already_posix = "/c/Users/bbask/scripts/foo.sh"
        result = _to_bash_argv_path(Path(already_posix))
        assert result == already_posix

    def test_unc_path_forward_slashes(self) -> None:
        """UNC paths (``\\\\server\\share\\...``) cannot be cleanly converted to
        a single drive letter. The fallback is forward-slash form, which
        bash on Windows handles without backslash-stripping."""
        if sys.platform != "win32":
            pytest.skip("UNC conversion is Windows-specific")
        result = _to_bash_argv_path(Path(r"\\fileserver\share\hermes\foo.sh"))
        # Backslashes must NOT appear in the result either way.
        assert "\\" not in result, f"backslash leaked into UNC result: {result!r}"

    def test_relative_path_returned_as_posix(self) -> None:
        """Relative paths have no drive letter; should not be mangled."""
        result = _to_bash_argv_path(Path("scripts/foo.sh"))
        if sys.platform == "win32":
            assert result == "scripts/foo.sh"
            assert "\\" not in result
        else:
            assert result == "scripts/foo.sh"


class TestRunJobScriptIntegration:
    """End-to-end: _run_job_script with a .sh file must invoke bash with a
    POSIX path, not a backslash path.

    This is the test that would have failed on 2026-08-10 if the regression
    had been written then. It does not actually exec bash — it inspects the
    constructed argv via monkeypatch.
    """

    def test_run_job_script_uses_posix_argv_for_bash(self, tmp_path, monkeypatch) -> None:
        """Construct a fake .sh script in a Windows-style path and assert that
        when _run_job_script builds its argv, the script path is POSIX-form.
        """
        from cron import scheduler as scheduler_mod

        if sys.platform != "win32":
            pytest.skip("Windows-cron path conversion is Windows-specific")

        # Build a real .sh file in a Windows-style directory. Use the user's
        # real HERMES_HOME/scripts so the path-traversal guard passes.
        scripts_dir = scheduler_mod._get_hermes_home() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        test_script = scripts_dir / "_test_pathconv_probe.sh"
        test_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        try:
            # Capture the argv passed to Popen.
            captured: dict = {}

            class _FakePopen:
                def __init__(self, argv, **kwargs):
                    self._argv = list(argv)
                    self.args = self._argv
                    self.stdin = None
                    self.stdout = None
                    self.stderr = None
                    captured["argv"] = self._argv
                    captured["kwargs"] = kwargs

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def communicate(self, *args, **kwargs):
                    return ("", "")

                def kill(self):
                    pass

                def poll(self):
                    return 0

                def wait(self, timeout=None):
                    return 0

                @property
                def returncode(self):
                    return 0

            monkeypatch.setattr(scheduler_mod.subprocess, "Popen", _FakePopen)
            # Force bash to resolve.
            monkeypatch.setattr(scheduler_mod.shutil, "which", lambda x: "/bin/bash" if x == "bash" else None)

            ok, out = scheduler_mod._run_job_script(str(test_script))
            assert ok, f"_run_job_script failed: {out}"
            argv = captured.get("argv", [])
            assert len(argv) >= 2, f"argv too short: {argv}"
            # The second argv element is the script path. CRITICAL: it must
            # not contain a backslash, because the failure mode from
            # incident cron-execution:512e7921301b is precisely that.
            script_argv = argv[1]
            assert "\\" not in script_argv, (
                f"REGRESSION: backslash in bash argv: {script_argv!r} — "
                "this is the exact failure mode from the 2026-08-10 incident"
            )
            assert script_argv.startswith("/"), (
                f"expected POSIX-style absolute path, got: {script_argv!r}"
            )
        finally:
            test_script.unlink(missing_ok=True)

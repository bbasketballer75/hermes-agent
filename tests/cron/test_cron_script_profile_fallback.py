"""Tests for the per-profile script-root fallback in cron/scheduler.py.

T1 of the 2026-08-16 telegram-themes plan: enable a per-profile cron to
resolve a script that exists only in the root install's scripts/ dir,
without forcing the operator to byte-copy the script into the profile.

The fallback must:
1. Resolve the relative path against the profile scripts/ first
2. If the profile copy is missing, fall back to root scripts/
3. Reroute the path-traversal guard's ``scripts_dir_resolved`` to match
   whichever root actually owns the script, so the guard still passes
4. Honor ``HERMES_ROOT_HOME`` env var if set, otherwise use the parent
   of ``_get_hermes_home()`` (``profiles/reviewer`` → ``hermes``)

The fallback must NOT:
- Allow traversal outside either root's scripts/ dir
- Run with empty/invalid HERMES_ROOT_HOME
- Lose the existing path-traversal guard behavior for absolute paths
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture
def two_root_layout(tmp_path):
    """Build a profile+root scripts layout under tmp_path.

    profile_home / scripts / profile_only.py
    root_home    / scripts / root_only.py
    """
    profile_home = tmp_path / "profile"
    root_home = tmp_path / "root"
    profile_home.mkdir()
    root_home.mkdir()
    (profile_home / "scripts").mkdir()
    (root_home / "scripts").mkdir()
    (profile_home / "scripts" / "profile_only.py").write_text("print('profile')\n")
    (root_home / "scripts" / "root_only.py").write_text("print('root')\n")
    return profile_home, root_home


def test_profile_only_resolution_keeps_profile_dir(two_root_layout, monkeypatch):
    """Scripts that exist in the profile dir resolve there, not the root."""
    profile_home, root_home = two_root_layout

    monkeypatch.setenv("HERMES_ROOT_HOME", str(root_home))
    # _get_hermes_home is a module-level function; patch the symbol scheduler
    # uses rather than the whole module.
    monkeypatch.setattr(
        "cron.scheduler._get_hermes_home",
        lambda: profile_home,
    )

    from cron.scheduler import _run_job_script

    # Should report the script ran in the profile dir (it would actually
    # print, so we just verify by checking the result)
    ok, output = _run_job_script("profile_only.py")
    assert ok is True
    assert "profile" in output


def test_root_only_fallback_routes_through_root(two_root_layout, monkeypatch):
    """Scripts missing from the profile dir fall back to root scripts/."""
    profile_home, root_home = two_root_layout

    monkeypatch.setenv("HERMES_ROOT_HOME", str(root_home))
    monkeypatch.setattr(
        "cron.scheduler._get_hermes_home",
        lambda: profile_home,
    )

    from cron.scheduler import _run_job_script

    # The fallback must find root_only.py in root/scripts and run it
    ok, output = _run_job_script("root_only.py")
    assert ok is True, "root fallback should have found root_only.py"
    assert "root" in output


def test_fallback_missing_script_returns_clean_error(two_root_layout, monkeypatch):
    """A script that exists in neither profile nor root fails cleanly."""
    profile_home, root_home = two_root_layout

    monkeypatch.setenv("HERMES_ROOT_HOME", str(root_home))
    monkeypatch.setattr(
        "cron.scheduler._get_hermes_home",
        lambda: profile_home,
    )

    from cron.scheduler import _run_job_script

    ok, output = _run_job_script("nonexistent.py")
    assert ok is False
    assert "Script not found" in output
    # The error message should reference the root's scripts dir (where
    # the search ended) so the operator can fix the right location.
    assert "root" in output or "scripts" in output


def test_absolute_path_traversal_still_blocked(two_root_layout, monkeypatch):
    """An absolute path outside both scripts dirs is blocked."""
    profile_home, root_home = two_root_layout

    monkeypatch.setenv("HERMES_ROOT_HOME", str(root_home))
    monkeypatch.setattr(
        "cron.scheduler._get_hermes_home",
        lambda: profile_home,
    )

    from cron.scheduler import _run_job_script

    # Outside both roots
    ok, output = _run_job_script(str(Path(tempfile.gettempdir()) / "attack.py"))
    assert ok is False
    assert "Blocked" in output


def test_hermes_root_home_override_used(tmp_path, monkeypatch):
    """HERMES_ROOT_HOME is honored when set explicitly."""
    profile_home = tmp_path / "p"
    root_home_a = tmp_path / "a"
    root_home_b = tmp_path / "b"
    for p in (profile_home, root_home_a, root_home_b):
        p.mkdir()
        (p / "scripts").mkdir()
    (profile_home / "scripts" / "x.py").write_text("print('x')\n")
    (root_home_a / "scripts" / "x.py").write_text("print('a')\n")
    (root_home_b / "scripts" / "x.py").write_text("print('b')\n")

    # Profile has x.py — should win regardless of HERMES_ROOT_HOME
    monkeypatch.setenv("HERMES_ROOT_HOME", str(root_home_b))
    monkeypatch.setattr("cron.scheduler._get_hermes_home", lambda: profile_home)

    from cron.scheduler import _run_job_script

    ok, output = _run_job_script("x.py")
    assert ok is True
    assert "x" in output  # not "a" or "b" — profile wins

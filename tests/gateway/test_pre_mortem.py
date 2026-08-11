"""Tests for gateway.pre_mortem — UNCLEANLY-death forensics (Windows gateway).

The gateway has been dying UNCLEANLY every few hours on this Windows 11
install (gateway-exit-diag.log shows 22 such events since July 30), and the
lifecycle ledger's ``gateway.previous_unclean_exit`` records prove the
death is real but cannot identify the killer — ``TerminateProcess`` and
``SIGKILL`` bypass every Python handler.  :mod:`gateway.pre_mortem` is the
defensive instrument that captures the cause: a SIGTERM/SIGBREAK/SIGINT
handler that writes a JSONL record before re-raising the signal, plus a
parent-PID watcher thread that records re-parenting events.

These tests lock in the contracts:

* install is idempotent — a second call is a no-op (the module-level
  ``_INSTALLED`` guard).
* simulate_terminate writes a well-formed JSONL record with the keys
  the on-call triage script greps for.
* the path resolution follows ``HERMES_HOME`` env var with the
  ``hermes_constants.get_hermes_home()`` fallback.
* the parent-watcher thread is daemon=True so a forgotten stop can't
  block process exit.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------


def _import_module():
    """Lazy import — keeps pytest collection fast and avoids the import
    side effects of loading all of gateway/ at collection time."""
    from gateway import pre_mortem
    # Reset the idempotency guard so each test starts fresh.  This is a
    # *test-only* mutation; production never relies on resetting it.
    pre_mortem._INSTALLED = False
    pre_mortem._ORIGINAL_HANDLERS.clear()
    return pre_mortem


# ---------------------------------------------------------------------------
# JSONL record shape
# ---------------------------------------------------------------------------


def test_simulate_terminate_writes_expected_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthesized record must contain every key the triage script greps for."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()

    pre_mortem.simulate_terminate(reason="unit_test")

    records = _read_diag(tmp_path)
    assert len(records) == 1, "simulate_terminate should write exactly one record"
    rec = records[0]

    # Mandatory keys the on-call triage script reads
    assert rec["tag"] == "pre_mortem.simulated"
    assert rec["reason"] == "unit_test"
    assert rec["pid"] == os.getpid()
    assert rec["platform"] == sys.platform
    assert isinstance(rec["ts"], str) and rec["ts"]


def test_simulate_terminate_includes_process_context_when_psutil_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """psutil-derived context (ppid + current_user) shows up when the lib is importable."""
    pytest.importorskip("psutil")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()
    pre_mortem.simulate_terminate(reason="ctx_check")

    rec = _read_diag(tmp_path)[0]
    assert "ppid" in rec, "ppid missing — psutil ran but didn't populate"
    assert rec["ppid"] > 0
    # current_user is set best-effort; psutil on every supported OS can
    # resolve it for our own process.
    assert "current_user" in rec


def test_simulate_terminate_writes_to_hermes_home_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file path is <HERMES_HOME>/logs/gateway-exit-diag.log, not cwd."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()
    pre_mortem.simulate_terminate(reason="path_check")

    expected = tmp_path / "logs" / "gateway-exit-diag.log"
    assert expected.exists(), f"expected {expected}"
    # Nothing leaked into the HERMES_HOME root itself (or its siblings).
    assert list((tmp_path).glob("gateway-exit-diag.log*")) == []


def test_simulate_terminate_appends_to_existing_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing exit-diag log (from the lifecycle_ledger or the CLI) is appended, not overwritten."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    pre_mortem = _import_module()

    log = tmp_path / "logs" / "gateway-exit-diag.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        json.dumps({"ts": "2026-08-09T22:00:00+00:00", "tag": "gateway.start", "pid": 1, "platform": "win32"}) + "\n",
        encoding="utf-8",
    )

    pre_mortem.simulate_terminate(reason="append_check")

    records = _read_diag(tmp_path)
    tags = [r["tag"] for r in records]
    assert tags == ["gateway.start", "pre_mortem.simulated"], tags


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_install_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two install calls in the same process record exactly one ``installed`` line."""
    pre_mortem = _import_module()
    # Re-point HERMES_HOME so the install can write a real log.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", td)
        assert pre_mortem.install_pre_mortem_handlers(interval_s=0.5) is True
        assert pre_mortem.install_pre_mortem_handlers(interval_s=0.5) is False
        assert pre_mortem.is_installed() is True

        records = _read_diag(Path(td))
        install_records = [r for r in records if r["tag"] == "pre_mortem.installed"]
        assert len(install_records) == 1, "second install must not write another installed record"

        # Restore original SIGTERM handler so the test process is not
        # left wired for kill — pytest cleanup would be sad otherwise.
        # Skip on POSIX where SIGTERM was already the default; restore is
        # only safe when we actually replaced the handler.
        if signal.SIGTERM in pre_mortem._ORIGINAL_HANDLERS:
            signal.signal(signal.SIGTERM, pre_mortem._ORIGINAL_HANDLERS[signal.SIGTERM])
        if signal.SIGINT in pre_mortem._ORIGINAL_HANDLERS:
            signal.signal(signal.SIGINT, pre_mortem._ORIGINAL_HANDLERS[signal.SIGINT])


# ---------------------------------------------------------------------------
# Parent watcher thread
# ---------------------------------------------------------------------------


def test_parent_watcher_thread_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """A forgotten watcher must NOT block interpreter shutdown."""
    pytest.importorskip("psutil")
    pre_mortem = _import_module()
    # Reach into the thread class — we don't need to run start_gateway to
    # instantiate one, the contract is just that it's daemon=True.
    t = pre_mortem._ParentWatcher(interval_s=0.1)
    assert t.daemon is True, "watcher must be daemon so process exit is never blocked"
    assert t.name == "gateway-pre-mortem-parent-watcher"


def test_parent_watcher_records_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    """First sample establishes the baseline — even if no change ever happens, we get a record."""
    pytest.importorskip("psutil")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", td)
        pre_mortem = _import_module()
        watcher = pre_mortem._ParentWatcher(interval_s=0.1)
        watcher.start()
        try:
            # First sample is synchronous inside run(); give the thread
            # a brief moment to write.
            time.sleep(0.5)
        finally:
            watcher.stop()
            watcher.join(timeout=2.0)

        records = _read_diag(Path(td))
        baseline = [r for r in records if r["tag"] == "pre_mortem.parent_baseline"]
        assert len(baseline) == 1, f"expected exactly one baseline, got {len(baseline)}"
        assert baseline[0]["ppid"] > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_diag(home: Path) -> list[dict]:
    path = home / "logs" / "gateway-exit-diag.log"
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out
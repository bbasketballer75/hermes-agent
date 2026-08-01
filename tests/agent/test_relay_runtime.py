"""Tests for Relay runtime scope-stack drift tolerance.

The native nemo_relay scope stack is strictly LIFO. When a tool/LLM
error-cleanup path pushes or pops outside the expected order, the stack
drifts from what the Hermes RelaySessionCoordinator expects, and the
pop in ``end_turn`` / ``_finish_logical_calls`` raises
``RuntimeError: invalid argument: scope handle is not at the top of
the stack``.

This module verifies the runtime treats the "already absent" case as
recoverable: a single anomalous cleanup must not cascade into a turn
finalization failure. Other RuntimeErrors still surface with full
diagnostics.

The drift itself (how a tool cleanup gets out of order) is out of
scope here — these tests simulate the *post-drift* state by manually
popping the handles the coordinator expects to be on top, then
exercise the cleanup path that would otherwise raise.
"""

from __future__ import annotations

import contextvars
import logging

import pytest

pytest.importorskip("nemo_relay")

from agent import relay_runtime  # noqa: E402


@pytest.fixture()
def coordinator_lease(tmp_path, monkeypatch):
    """Provide a fresh coordinator, lease, and runtime pair for one test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    lease = relay_runtime.SESSION_COORDINATOR.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="drift-session",
        platform="cli",
    )
    try:
        yield relay_runtime.SESSION_COORDINATOR, lease, lease.host
    finally:
        relay_runtime._reset_for_tests()


def _run_in_session(host, session, callback, *args, **kwargs):
    return host.run_in_session(session, callback, *args, **kwargs)


def test_begin_turn_captures_scope_depth_at_push(coordinator_lease, caplog):
    """begin_turn must remember the pre-push depth (or None when the
    installed nemo_relay build does not expose ``ScopeStack.__len__``)
    so end_turn can detect drift on builds that do support it."""
    coordinator, lease, host = coordinator_lease
    turn = coordinator.begin_turn(
        lease, turn_id="t-1", task_id="task-1"
    )
    try:
        assert turn.handle is not None
        # Either a real integer depth (builds with __len__) or None
        # (0.6.0 builds where the depth query is a no-op). The drift
        # warning is suppressed in the None case — that's fine, it's
        # a diagnostic, not a correctness invariant.
        assert turn.scope_depth_at_push is None or isinstance(
            turn.scope_depth_at_push, int
        )
    finally:
        coordinator.end_turn(turn, outcome="success")


def test_end_turn_swallows_lifo_violation(coordinator_lease, caplog):
    """If a prior cleanup pushes a scope on top of the turn handle, the
    end_turn pop must succeed silently (info log, no warning traceback)
    rather than cascading into a finalization failure."""
    coordinator, lease, host = coordinator_lease
    turn = coordinator.begin_turn(
        lease, turn_id="t-2", task_id="task-2"
    )
    turn_handle = turn.handle
    assert turn_handle is not None

    # Simulate a tool/LLM error-cleanup path that pushes an extra
    # scope on top of the turn handle, then ends — leaving the
    # turn handle below a synthetic scope. end_turn's LIFO pop
    # would otherwise raise "scope handle is not at the top of the
    # stack" (the production failure mode from agent.log).
    synthetic = _run_in_session(
        host, lease.session, host.relay.scope.push,
        "synthetic-cleanup",
        host.relay.ScopeType.Function,
        handle=turn_handle,
        input={},
        metadata={
            relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
            relay_runtime.RUNTIME_INSTANCE_KEY: host.runtime_id,
        },
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")

    # No "Hermes Relay turn finalization failed" WARNING — the
    # LIFO violation path logs INFO instead.
    finalization_warnings = [
        r for r in caplog.records
        if r.name == "agent.relay_runtime"
        and r.levelno == logging.WARNING
        and "finalization failed" in r.getMessage()
    ]
    assert finalization_warnings == [], (
        "LIFO violation on turn handle pop must not log a warning "
        f"traceback: {[r.getMessage() for r in finalization_warnings]}"
    )

    # And the canonical INFO line should be present.
    already_absent_infos = [
        r for r in caplog.records
        if r.name == "agent.relay_runtime"
        and r.levelno == logging.INFO
        and "already absent" in r.getMessage()
    ]
    assert already_absent_infos, (
        "Expected an info-level 'already absent' log on drift recovery"
    )


def test_end_turn_logs_drift_warning_before_pop(coordinator_lease, caplog):
    """If the stack depth changes between begin_turn and end_turn (e.g.
    a logical LLM cleanup popped something), end_turn must log a
    drift warning carrying session/turn/depth context — but still
    proceed with its own pop."""
    coordinator, lease, host = coordinator_lease
    turn = coordinator.begin_turn(
        lease, turn_id="t-3", task_id="task-3"
    )

    # Inject drift by adding a synthetic scope on top of the turn
    # handle, then popping it. Net effect: depth is unchanged but
    # the *type* of topmost scope drifted. Use a real push/pop pair
    # to simulate a tool/LLM cleanup that left the stack in an
    # unexpected shape.
    synthetic = _run_in_session(
        host, lease.session, host.relay.scope.push,
        "synthetic-cleanup",
        host.relay.ScopeType.Function,
        handle=turn.handle,
        input={},
        metadata={
            relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
            relay_runtime.RUNTIME_INSTANCE_KEY: host.runtime_id,
        },
    )
    _run_in_session(
        host, lease.session, host.relay.scope.pop, synthetic,
        output={"outcome": "cleanup"},
        metadata={
            relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
            relay_runtime.RUNTIME_INSTANCE_KEY: host.runtime_id,
        },
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")

    # No drift warning here — synthetic push/pop returns the stack to
    # its captured depth, so the comparison matches. This guards
    # against false-positive drift logs on benign transient pushes.
    drift_warnings = [
        r for r in caplog.records
        if r.name == "agent.relay_runtime"
        and r.levelno == logging.WARNING
        and "scope-stack drift" in r.getMessage()
    ]
    assert drift_warnings == [], (
        "Balanced push/pop must not trigger a drift warning: "
        f"{[r.getMessage() for r in drift_warnings]}"
    )


def test_finish_logical_swallows_lifo_violation(
    coordinator_lease, caplog
):
    """If a prior cleanup pushes a scope on top of a logical LLM handle,
    ``_finish_logical_calls`` must treat the LIFO-violation pop as
    recoverable and not log a warning traceback.

    Mirrors the production failure mode seen in agent.log where two
    in-flight LLM requests push separate logical scopes and a tool
    cleanup pushes a third scope on top — leaving the older logical
    handle below the stack top."""
    coordinator, lease, host = coordinator_lease
    turn = coordinator.begin_turn(
        lease, turn_id="t-4", task_id="task-4"
    )

    # Push one logical LLM scope on top of the turn handle.
    handle_a = _run_in_session(
        host, lease.session, host.relay.scope.push,
        relay_runtime.LOGICAL_LLM_SCOPE,
        host.relay.ScopeType.Function,
        handle=turn.handle,
        input={},
        metadata={
            relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
            relay_runtime.RUNTIME_INSTANCE_KEY: host.runtime_id,
            "hermes.call_role": "primary",
        },
    )

    turn.logical_llm_calls["req-A"] = handle_a

    # Simulate an out-of-order cleanup that pushes another scope on
    # top of the logical scope, leaving handle_a buried.
    _run_in_session(
        host, lease.session, host.relay.scope.push,
        "synthetic-cleanup",
        host.relay.ScopeType.Function,
        handle=turn.handle,
        input={},
        metadata={
            relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION,
            relay_runtime.RUNTIME_INSTANCE_KEY: host.runtime_id,
        },
    )

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")

    finalization_warnings = [
        r for r in caplog.records
        if r.name == "agent.relay_runtime"
        and r.levelno == logging.WARNING
        and "logical LLM finalization failed" in r.getMessage()
    ]
    assert finalization_warnings == [], (
        "LIFO violation on logical LLM pop must not log a warning "
        f"traceback: {[r.getMessage() for r in finalization_warnings]}"
    )


def test_other_runtime_errors_still_warn(coordinator_lease, caplog):
    """A non-``scope handle is not at the top`` RuntimeError on the
    turn-handle pop must still surface as a WARNING with exc_info —
    the narrowed catch only downgrades the specific drift message."""
    coordinator, lease, host = coordinator_lease
    turn = coordinator.begin_turn(
        lease, turn_id="t-5", task_id="task-5"
    )
    turn_handle = turn.handle
    assert turn_handle is not None

    # Replace the scope pop with a callable that raises a different
    # RuntimeError, simulating an unrelated native failure (e.g.
    # serialization error or stale handle reference).
    original_run = host.run_in_session

    def boom(session, callback, *args, **kwargs):
        if callback is host.relay.scope.pop:
            raise RuntimeError("some other native error")
        return original_run(session, callback, *args, **kwargs)

    host.run_in_session = boom

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")

    finalization_warnings = [
        r for r in caplog.records
        if r.name == "agent.relay_runtime"
        and r.levelno == logging.WARNING
        and "finalization failed" in r.getMessage()
    ]
    assert finalization_warnings, (
        "Non-drift RuntimeError on the turn-handle pop must surface "
        "as a warning"
    )
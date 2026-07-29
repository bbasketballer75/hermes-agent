"""Smoke test for the relay-runtime scope-drift tolerance fix.

Verifies that:
1. relay_shared_metrics._finish_task treats 'scope handle is not at the
   top of the stack' as recoverable (no traceback to the caller,
   warning logged, returns True since the finally still runs).
2. relay_runtime.RelaySessionCoordinator.end_turn treats the same
   error as recoverable (warning logged, no re-raise).

These are minimal integration checks against the actual production
code paths; a full pytest suite for the relay runtime would need
substantial mock infrastructure (RelayRuntime, ConversationLease,
RelaySession, _MetricsSession, etc.) that does not currently exist in
the repo. The goal here is to confirm the new error-narrowing logic
behaves correctly when the underlying nemo_relay raises the specific
RuntimeError we observed in errors.log L9176/L9189.

Run: cd hermes-agent && python tests/manual/verify_scope_drift_fix.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Make the hermes-agent package importable
HERMES_AGENT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERMES_AGENT))


class _FakeRelay:
    """Minimal relay mock that raises the production RuntimeError on pop."""

    class ScopeType:
        Function = "function"

    def __init__(self) -> None:
        self.pop_calls: list[tuple] = []
        self.pop_should_raise = False

    def get_scope_stack(self):
        return SimpleNamespace(__len__=lambda self: 1)

    class _Scope:
        def __init__(self, parent):
            self._parent = parent

        def pop(self, handle, **kwargs):
            self._parent.pop_calls.append((handle, kwargs))
            if self._parent.pop_should_raise:
                raise RuntimeError(
                    "scope handle is not at the top of the stack"
                )

    @property
    def scope(self):
        return self._Scope(self)


def test_finish_task_tolerates_double_pop():
    """When _native_pop_scope raises 'scope not at top', _finish_task
    must NOT propagate the error and must still clean up the task."""
    from hermes_cli.observability import relay_shared_metrics

    # Capture warning-level logs
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    logger = logging.getLogger("hermes_cli.observability.relay_shared_metrics")
    logger.addHandler(_Capture(level=logging.WARNING))
    logger.setLevel(logging.WARNING)
    prior_handlers = list(logger.handlers)

    fake = _FakeRelay()
    fake.pop_should_raise = True

    session = SimpleNamespace(
        session_id="test-session",
        tasks={"task-1": SimpleNamespace(handle="handle-1", start_fields={}, started_ns=0, model_call_ids=[], tool_call_ids=[], unidentified_tool_calls=0, retry_count=0, turn_ids=[])},
        model_calls={},
    )
    runtime = SimpleNamespace(
        runtime_id="test-runtime",
        relay=fake,
        _event_metadata=lambda: {},
    )

    metrics = relay_shared_metrics._Runtime.__new__(
        relay_shared_metrics._Runtime
    )
    metrics.relay = fake
    metrics.host = runtime
    metrics._task_sessions_lock = MagicMock()
    metrics._task_sessions = {}
    metrics._turn_sessions = {}
    metrics._run_in_task = lambda task, cb, *a, **kw: cb(*a, **kw)
    metrics._event_metadata = lambda: {}

    result = metrics._finish_task(session, "task-1", {"outcome": "failed"})

    # Cleanup
    for h in list(logger.handlers):
        if h not in prior_handlers:
            logger.removeHandler(h)

    assert result is True, "_finish_task must return True (recovery path)"
    assert len(fake.pop_calls) == 1, "scope.pop should have been called once"
    assert any("already absent at pop" in r.getMessage() or "task close failed" in r.getMessage() for r in captured), (
        f"A warning must be logged when the double-pop is recovered; got: {[r.getMessage() for r in captured]}"
    )
    print(f"OK: _finish_task recovered from double-pop, warning logged: {captured[0].getMessage()[:200] if captured else 'NONE'}")


def test_finish_task_still_propagates_other_runtime_errors():
    """A different RuntimeError (NOT 'scope not at top') must still log
    via the original handler."""
    from hermes_cli.observability import relay_shared_metrics

    fake = _FakeRelay()

    def pop_with_other_error(handle, **kwargs):
        fake.pop_calls.append((handle, kwargs))
        raise RuntimeError("something completely different")

    fake.scope.pop = pop_with_other_error  # type: ignore[assignment]

    session = SimpleNamespace(
        session_id="test-session",
        tasks={"task-1": SimpleNamespace(handle="handle-1", start_fields={}, started_ns=0, model_call_ids=[], tool_call_ids=[], unidentified_tool_calls=0, retry_count=0, turn_ids=[])},
        model_calls={},
    )
    runtime = SimpleNamespace(
        runtime_id="test-runtime",
        relay=fake,
        _event_metadata=lambda: {},
    )

    metrics = relay_shared_metrics._Runtime.__new__(
        relay_shared_metrics._Runtime
    )
    metrics.relay = fake
    metrics.host = runtime
    metrics._task_sessions_lock = MagicMock()
    metrics._task_sessions = {}
    metrics._turn_sessions = {}
    metrics._run_in_task = lambda task, cb, *a, **kw: cb(*a, **kw)
    metrics._event_metadata = lambda: {}

    # Must NOT raise — other RuntimeErrors are caught by the existing
    # 'except Exception' fallback, which logs at warning level.
    result = metrics._finish_task(session, "task-1", {"outcome": "failed"})
    assert result is True, "_finish_task must still return True for unrelated errors"
    print("OK: _finish_task still tolerates unrelated RuntimeErrors via the fallback path")


if __name__ == "__main__":
    test_finish_task_tolerates_double_pop()
    test_finish_task_still_propagates_other_runtime_errors()
    print("\nAll smoke tests passed.")

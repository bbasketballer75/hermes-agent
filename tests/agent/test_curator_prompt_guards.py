"""Regression tests for the skill-curator background-review prompt guards.

These tests lock in the load-before-patch / no-retry invariants that were
added in response to the "1000 rejected tool calls per day" bug. The
bug: the curator's prompt did not enforce skill_view before skill_manage,
so the model would patch skills it had not loaded, and the skill_manage
tool would reject the patch — wasting a full LLM turn each time.

If you change ``_SKILL_REVIEW_PROMPT`` in agent/background_review.py, these
tests will fail. That is intentional. Re-read the test name and assertion
before relaxing any of these.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def reloaded_background_review(monkeypatch):
    """Force a fresh import of background_review so we read the on-disk
    prompt, not a stale cached version.
    """
    # Drop any cached import of background_review before re-importing.
    for mod_name in list(sys.modules):
        if mod_name == "agent.background_review":
            del sys.modules[mod_name]
    return importlib.import_module("agent.background_review")


def test_skill_review_prompt_instructs_load_before_patch(reloaded_background_review):
    """The prompt must require skill_view before any skill_manage or
    write_file call. Without this guard, the curator wastes an LLM turn
    on every rejected patch.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    assert "skill_view" in prompt, "prompt must mention skill_view"
    assert "skill_manage" in prompt, "prompt must mention skill_manage"
    # The guard must be an explicit MUST or similar imperative.
    lower = prompt.lower()
    assert (
        "must call skill_view" in lower
        or "must call `skill_view" in lower
        or "you must call skill_view" in lower
    ), "prompt must use imperative language to load before patch (MUST call skill_view)"


def test_skill_review_prompt_forbids_retry_on_provenance_refusal(reloaded_background_review):
    """When skill_manage returns a 'not curator-managed' refusal, the
    curator must NOT try a different action or call write_file as a
    fallback. The user must opt the skill in via ``hermes curator adopt``.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    lower = prompt.lower()
    # Must mention curator-managed provenance.
    assert "curator-managed" in lower or "curator managed" in lower, (
        "prompt must mention curator-managed provenance so the model "
        "knows the user opt-in is required"
    )
    # Must say not to retry, and not to fall back to write_file.
    assert "do not" in lower or "don't" in lower, (
        "prompt must use prohibitive language for the no-retry rule"
    )
    assert "write_file" in prompt, (
        "prompt must call out write_file as a forbidden fallback path"
    )


def test_skill_review_prompt_names_the_real_provenance_marker(
    reloaded_background_review,
):
    """The prompt must describe the marker the code actually writes.

    ``created_by: "agent"`` is what makes a skill curator-managed (see
    hermes_cli/curator.py and tools/skill_usage.py); ``"curator"`` is never
    a created_by value anywhere. An earlier revision said the refusal
    applied when created_by "is not 'curator'" — inverted, so it described
    every curator-managed skill as off-limits and none of the off-limits
    ones as such. The older assertions above all still passed on that text,
    which is why this one pins the value explicitly.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    assert "created_by" in prompt, "prompt must name the created_by marker"
    assert "agent" in prompt, (
        "prompt must identify 'agent' as the created_by value that marks a "
        "skill curator-managed"
    )
    assert "'curator'" not in prompt and '"curator"' not in prompt, (
        "'curator' is not a created_by value — quoting it as one inverts the "
        "rule the curator is supposed to follow"
    )


def test_skill_review_prompt_caps_retry_attempts(reloaded_background_review):
    """If a single skill_manage / write_file call fails, retry at most
    once, then give up. Without this cap, the curator can ping-pong on
    the same skill for the entire review pass.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    lower = prompt.lower()
    # Must mention a retry cap.
    assert (
        "retry at most once" in lower
        or "one retry" in lower
        or "retry at most 1" in lower
    ), "prompt must cap retries (e.g. 'retry at most once')"
    # Must say to give up after the cap.
    assert "give up" in lower, "prompt must say 'give up' after the retry cap"


def test_skill_review_prompt_skips_protected_skills(reloaded_background_review):
    """Bundled, hub-installed, pinned, and external_dir skills are
    off-limits. The prompt must instruct the curator to skip them
    rather than try to read or modify them directly.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    assert "bundled" in prompt, "prompt must list bundled as a protected category"
    assert "external_dirs" in prompt or "external dirs" in prompt, (
        "prompt must list skills.external_dirs as a protected category"
    )
    assert "pinned" in prompt, "prompt must list pinned as a protected category"


def test_skill_review_prompt_includes_hard_guards_section(reloaded_background_review):
    """The new 'HARD GUARDS' section is the umbrella for the rules above.
    If a future edit removes this section while keeping one of the
    individual rules, the rule is much more likely to be overlooked
    by readers; this test makes the section deletion itself fail.
    """
    prompt = reloaded_background_review._SKILL_REVIEW_PROMPT
    assert "HARD GUARDS" in prompt, (
        "the 'HARD GUARDS' umbrella section must remain in the prompt"
    )

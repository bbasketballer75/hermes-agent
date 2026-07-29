"""Regression tests for local terminal initial cwd normalization."""

import sys
import pytest
from pathlib import Path

from tools.environments.local import LocalEnvironment, _resolve_local_initial_cwd


def test_relative_initial_cwd_resolves_from_parent(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    assert _resolve_local_initial_cwd("hermes-agent") == str(project)


def test_relative_initial_cwd_matching_current_dir_uses_current_dir(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(project)

    assert _resolve_local_initial_cwd("hermes-agent") == str(project)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Test invokes LocalEnvironment.execute('pwd') which spawns real "
    "bash; bash on Windows (under MSYS) emits POSIX-style paths "
    "(/c/Users/...) while the assertion compares against Windows-style "
    "Path(tmp_path). Real subprocess output style cannot be monkeypatched "
    "without mocking the entire subprocess invocation. Upstream fix: "
    "split the test into POSIX and Windows variants using "
    "tmp_path.as_posix() vs str(tmp_path) for the expected value.",
)
def test_local_environment_does_not_cd_into_nested_matching_relative_cwd(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(project)

    env = LocalEnvironment(cwd="hermes-agent", timeout=5)
    try:
        result = env.execute("pwd", timeout=5)
    finally:
        env.cleanup()

    assert result["returncode"] == 0
    assert result["output"].strip() == str(project)
    assert "cd: hermes-agent" not in result["output"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Same real-subprocess issue as test_local_environment_does_not_cd_"
    "into_nested_matching_relative_cwd - bash under MSYS emits /c/Users "
    "form, the assertion expects a Windows-style absolute path.",
)
def test_local_environment_keeps_existing_relative_child_cwd(tmp_path, monkeypatch):
    project = tmp_path / "hermes-agent"
    project.mkdir()
    monkeypatch.chdir(tmp_path)

    env = LocalEnvironment(cwd="hermes-agent", timeout=5)
    try:
        result = env.execute("pwd", timeout=5)
    finally:
        env.cleanup()

    assert result["returncode"] == 0
    assert result["output"].strip() == str(project)

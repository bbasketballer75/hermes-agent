"""Regression test for #17929: AIAgent.__init__ should try fallback_model
when primary provider credentials are exhausted."""
import pytest
from unittest.mock import patch, MagicMock
from run_agent import AIAgent


def _make_tool_defs():
    return [{"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def test_init_tries_fallback_when_primary_returns_none():
    """When resolve_provider_client returns None for primary but succeeds for
    a fallback entry, __init__ should NOT raise RuntimeError."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "tencent-token-plan":
            return fb, "kimi2.5"
        return None, None  # primary exhausted

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        agent = AIAgent(
            provider="alibaba-coding-plan",
            model="qwen3.6-plus",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        assert agent.provider == "tencent-token-plan"
        assert agent.model == "kimi2.5"
        assert agent._fallback_activated is True


def test_init_raises_when_no_fallback_configured():
    """When primary returns None and no fallback is set, should raise."""
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError, match="no API key was found"):
            AIAgent(
                provider="alibaba-coding-plan",
                model="qwen3.6-plus",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=None,
            )


# ---------------------------------------------------------------------------
# Regression tests for the "misleading MINIMAX-OAUTH_API_KEY env var" bug.
# When the auxiliary client can't resolve credentials for an OAuth / device-
# code / PKCE provider, the error message must NOT point the user at an env
# var that does not exist (e.g. MINIMAX-OAUTH_API_KEY). It must instead point
# them at ``hermes model`` to re-authenticate.
# ---------------------------------------------------------------------------


def test_missing_credentials_error_oauth_provider():
    """OAuth / device-code / PKCE providers must get the 're-authenticate' hint,
    not a non-existent env var hint."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("minimax-oauth")
    assert "interactive-auth credentials" in msg
    assert "hermes model" in msg
    # Critical: must NOT suggest a non-existent env var
    assert "MINIMAX-OAUTH_API_KEY" not in msg
    assert "_API_KEY" not in msg


def test_missing_credentials_error_device_code_provider():
    """openai-codex uses device-code auth, not an API key."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("openai-codex")
    assert "interactive-auth credentials" in msg
    assert "OPENAI-CODEX_API_KEY" not in msg


def test_missing_credentials_error_pkce_provider():
    """anthropic uses PKCE auth, not an API key."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("anthropic")
    assert "interactive-auth credentials" in msg
    assert "ANTHROPIC_API_KEY" not in msg


def test_missing_credentials_error_api_key_provider():
    """Regular API-key providers still get the 'set X_API_KEY' guidance."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("openai")
    assert "OPENAI_API_KEY" in msg
    assert "hermes model" in msg


def test_missing_credentials_error_case_insensitive():
    """Provider name matching is case-insensitive and whitespace-trimmed."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("  MiniMax-OAuth  ")
    assert "interactive-auth credentials" in msg
    assert "MINIMAX-OAUTH_API_KEY" not in msg


def test_missing_credentials_error_unknown_provider_falls_through():
    """Providers not in the interactive-auth set still get the API-key hint,
    matching the pre-fix behavior for unrecognized names."""
    from agent.auxiliary_client import _missing_credentials_error

    msg = _missing_credentials_error("some-unknown-provider")
    assert "SOME-UNKNOWN-PROVIDER_API_KEY" in msg
    assert "interactive-auth" not in msg

"""Hard startup assertion: an explicit model.context_length must be honored.

A silent resolver fallback (e.g. get_model_context_length()'s 128K default)
makes sessions compact far earlier than configured — and the incompressible
prompt can then meet the compression threshold and block compaction forever
(2026-09-20 incident). The built-in compressor must fail loud instead.
"""

import pytest
from unittest.mock import patch


def _cfg(ctx):
    return {"model": {"default": "gpt5.4", "provider": "custom",
                      "base_url": "http://localhost:4000/v1",
                      "context_length": ctx}}


def _build(cfg, resolved):
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.context_compressor.get_model_context_length", return_value=resolved),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent
        return AIAgent(
            model="gpt5.4",
            api_key="test-key-1234567890",
            base_url="http://localhost:4000/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_fallback_below_configured_context_raises():
    with pytest.raises(RuntimeError, match="context_length"):
        _build(_cfg(256_000), resolved=128_000)


def test_matching_context_length_is_accepted():
    agent = _build(_cfg(256_000), resolved=256_000)
    assert agent.context_compressor.context_length == 256_000


def test_operator_opt_out_allows_mismatch():
    cfg = _cfg(256_000)
    cfg["compression"] = {"allow_context_length_mismatch": True}
    agent = _build(cfg, resolved=128_000)
    assert agent.context_compressor.context_length == 128_000

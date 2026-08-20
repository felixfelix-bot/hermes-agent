"""Per-turn cap on post-tool empty-response nudges (agent.max_post_tool_nudges).

Behavior contract: a model that alternates tool-call → empty responses gets
at most `agent._max_post_tool_nudges` full-context nudge resends per turn
(default 2).  Without the cap, the `_post_tool_empty_retried` flag re-arms
after every successful tool round, so the alternation loops until the
iteration budget dies — each cycle re-sending the FULL conversation context.
After the nudge budget is spent, the turn falls through to the existing
exhaustion machinery (empty-retries → fallback → "(empty)" terminal).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from run_agent import AIAgent


def _make_tool_defs(names):
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


@pytest.fixture()
def agent():
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        # Loop-level tool-name validation consults valid_tool_names, not the
        # patched get_tool_definitions — make the mock tool valid (same
        # pattern as test_codex_app_server_integration.py).
        a.valid_tool_names = set(getattr(a, "valid_tool_names", set()))
        a.valid_tool_names.add("web_search")
        return a


def _mock_tool_call(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model")


_NUDGE_PREFIX = "You just executed tool calls"


def _run_alternating(agent):
    """Run a turn where the model alternates tool-call / empty forever.

    Returns the run_conversation result dict.  If the nudge cap is broken
    the loop only stops at max_iterations (~90 calls); with the cap the
    turn terminates via the empty-exhaustion path in ~12 calls.
    """
    agent.base_url = "http://127.0.0.1:1234/v1"

    def _alternate(*_args, **_kwargs):
        # Alternate forever: tool call, then empty, tool, empty, ...
        if not hasattr(_alternate, "i"):
            _alternate.i = 0
        _alternate.i += 1
        if _alternate.i % 2 == 1:
            return _mock_response(
                content="",
                finish_reason="tool_calls",
                tool_calls=[_mock_tool_call()],
            )
        return _mock_response(content=None, finish_reason="stop")

    agent.client.chat.completions.create.side_effect = _alternate
    with (
        patch("run_agent.handle_function_call", return_value="search result"),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("do the thing")


def _count_nudges(result):
    return sum(
        1
        for m in result["messages"]
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and m["content"].startswith(_NUDGE_PREFIX)
    )


def test_alternating_tool_empty_model_gets_at_most_default_nudges(agent):
    """Default cap (2): nudges stop after 2 even though tool rounds re-arm
    the per-round flag; the turn terminates via the empty path instead of
    looping to the iteration budget."""
    assert agent._max_post_tool_nudges == 2
    result = _run_alternating(agent)
    assert _count_nudges(result) == 2
    # Bounded well below max_iterations (90) — the unbounded behavior would
    # emit ~44 nudges and only stop at the iteration ceiling.
    assert result["api_calls"] <= 15
    # Turn terminated via the empty-exhaustion path, not with content.
    assert result.get("turn_exit_reason") == "empty_response_exhausted"


def test_nudge_cap_zero_disables_nudging(agent):
    """Cap 0 (agent.max_post_tool_nudges: 0): no nudge resends at all."""
    agent._max_post_tool_nudges = 0
    result = _run_alternating(agent)
    assert _count_nudges(result) == 0
    assert result.get("turn_exit_reason") == "empty_response_exhausted"


def test_nudge_cap_configurable(agent):
    """The cap honors agent._max_post_tool_nudges (from config.yaml), bounded
    by the unified _max_empty_recovery_total budget."""
    agent._max_post_tool_nudges = 5
    agent._max_empty_recovery_total = 5
    result = _run_alternating(agent)
    assert _count_nudges(result) == 5
    assert result["api_calls"] <= 21


def test_unified_budget_bounds_all_ladders(agent):
    """The unified _max_empty_recovery_total (default 3) is the single source
    of truth: nudges + bare empty-retries + thinking-prefills together cannot
    exceed it per turn, even though each ladder has its own (larger) per-
    ladder counter. With the default 3, the alternating model emits exactly 2
    nudges (nudge cap) then 1 bare empty-retry (unified cap) then terminates."""
    assert agent._max_empty_recovery_total == 3
    result = _run_alternating(agent)
    # 2 nudges (per-ladder cap) + 1 bare empty-retry (unified cap hit) = 3
    assert _count_nudges(result) == 2
    assert agent._empty_recovery_count == 3
    assert result.get("turn_exit_reason") == "empty_response_exhausted"


def test_unified_budget_no_reset_on_fallback(agent):
    """A flaky primary cascading through fallback providers must NOT get a
    fresh recovery budget per provider. The _empty_recovery_count carries
    over, so the combined resend count per turn stays bounded regardless of
    how many fallbacks fire. Regression for the ~7N-resend burn."""
    agent._fallback_chain = [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        {"provider": "deepinfra", "model": "deepseek-ai/DeepSeek-V4-Pro"},
    ]
    agent._fallback_index = 0
    agent._fallback_activated = False
    fallback_calls = {"n": 0}

    def _mock_fallback():
        if agent._fallback_index >= len(agent._fallback_chain):
            return False
        agent._fallback_index += 1
        agent._fallback_activated = True
        agent.model = agent._fallback_chain[agent._fallback_index - 1]["model"]
        agent.provider = agent._fallback_chain[agent._fallback_index - 1]["provider"]
        fallback_calls["n"] += 1
        return True

    with patch.object(agent, "_try_activate_fallback", side_effect=_mock_fallback):
        result = _run_alternating(agent)
    # At least one fallback fired (else the test is trivial).
    assert fallback_calls["n"] >= 1, "fallback should have activated"
    # Unified budget (3) is shared across ALL providers — NOT reset on
    # fallback activation. Old code reset _empty_content_retries=0 each
    # fallback, giving ~7N resends; the unified counter carries over.
    assert agent._empty_recovery_count <= 3
    assert result.get("turn_exit_reason") == "empty_response_exhausted"


def test_nudge_counter_resets_per_turn(agent):
    """The counter is per-turn: a second turn gets a fresh nudge budget."""
    result1 = _run_alternating(agent)
    assert _count_nudges(result1) == 2
    assert agent._post_tool_nudge_count == 2

    result2 = _run_alternating(agent)
    assert _count_nudges(result2) == 2

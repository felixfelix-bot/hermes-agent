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
    """The cap honors agent._max_post_tool_nudges (from config.yaml)."""
    agent._max_post_tool_nudges = 5
    result = _run_alternating(agent)
    assert _count_nudges(result) == 5
    assert result["api_calls"] <= 21


def test_nudge_counter_resets_per_turn(agent):
    """The counter is per-turn: a second turn gets a fresh nudge budget."""
    result1 = _run_alternating(agent)
    assert _count_nudges(result1) == 2
    assert agent._post_tool_nudge_count == 2

    result2 = _run_alternating(agent)
    assert _count_nudges(result2) == 2

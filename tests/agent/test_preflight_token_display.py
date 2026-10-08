"""Preflight notice shows the real prompt-token count when the compressor knows it."""
from agent.turn_context_compaction import _preflight_token_display


def test_real_tokens_shown_when_measured():
    assert _preflight_token_display(123456, 100000) == "123,456 real"


def test_estimate_shown_when_no_real_measurement():
    assert _preflight_token_display(0, 100000) == "~100,000 est."
    assert _preflight_token_display(None, 500) == "~500 est."

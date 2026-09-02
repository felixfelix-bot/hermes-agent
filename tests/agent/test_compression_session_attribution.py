"""Session attribution for compression calls (2026-09-02, plan B1).

The context compressor's summarization call historically reached the
loopback proxy via auxiliary ``call_llm`` without any session
identifier — ``api_calls`` rows for ``task_type='compression'`` were
logged with ``session_id NULL``, so compaction events could not be
attributed to the profile/session that paid for them. The growth
governor's pressure-relief feedback and any compaction-rate incident
detection are blind without this signal.

Pinned here:

* ``ContextCompressor.on_session_start`` stashes the originating
  session id; ``on_session_end`` clears it (no stale attribution on a
  reused compressor instance).
* ``_generate_summary`` threads the stashed id into ``call_llm`` — and
  omits it cleanly when no session is known.
* ``call_llm`` attaches ``X-Hermes-Session`` only when a session id is
  supplied AND the effective endpoint is loopback — the internal id
  must never leak to an external provider (mirrors the zai provider
  plugin's loopback gate).
* ``_is_loopback_endpoint`` accepts the loopback spellings and rejects
  external hosts, empty urls, and garbage.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def compressor():
    from agent.context_compressor import ContextCompressor
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=200_000,
    ):
        return ContextCompressor(
            model="test/model",
            threshold_percent=0.5,
            protect_first_n=2,
            protect_last_n=2,
            quiet_mode=True,
        )


def _fake_call_llm_capture(captured):
    """Build a call_llm stub that records kwargs and returns a stub summary."""
    def _inner(**kwargs):
        captured.update(kwargs)
        resp = MagicMock()
        resp.choices[0].message.content = "[CONTEXT SUMMARY]: stub summary text"
        return resp
    return _inner


class TestCompressorSessionStash:
    def test_on_session_start_stashes(self, compressor):
        compressor.on_session_start("20260902_051500_ababab")
        assert compressor._session_id == "20260902_051500_ababab"

    def test_on_session_end_clears(self, compressor):
        compressor.on_session_start("20260902_051500_ababab")
        compressor.on_session_end("20260902_051500_ababab", [])
        assert compressor._session_id is None

    def test_generate_summary_passes_session_id(self, compressor):
        compressor.on_session_start("20260902_051500_ababab")
        captured = {}
        with patch(
            "agent.context_compressor.call_llm",
            side_effect=_fake_call_llm_capture(captured),
        ):
            compressor._generate_summary(
                [{"role": "user", "content": "turn content"}])
        assert captured.get("session_id") == "20260902_051500_ababab"
        assert captured.get("task") == "compression"

    def test_generate_summary_omits_session_when_unknown(self, compressor):
        # No on_session_start call — the id must be omitted, not None.
        captured = {}
        with patch(
            "agent.context_compressor.call_llm",
            side_effect=_fake_call_llm_capture(captured),
        ):
            compressor._generate_summary(
                [{"role": "user", "content": "turn content"}])
        assert "session_id" not in captured


class TestIsLoopbackEndpoint:
    def test_accepts_loopback_spellings(self):
        from agent.auxiliary_client import _is_loopback_endpoint
        assert _is_loopback_endpoint("http://localhost:9099/v1")
        assert _is_loopback_endpoint("http://127.0.0.1:9099/v1")
        assert _is_loopback_endpoint("http://[::1]:9099/v1")

    def test_rejects_external(self):
        from agent.auxiliary_client import _is_loopback_endpoint
        assert not _is_loopback_endpoint("https://api.z.ai/api/paas/v4")
        assert not _is_loopback_endpoint("https://api.openai.com/v1")

    def test_rejects_empty_and_garbage(self):
        from agent.auxiliary_client import _is_loopback_endpoint
        assert not _is_loopback_endpoint("")
        assert not _is_loopback_endpoint(None)
        assert not _is_loopback_endpoint("not a url")


class TestCallLlmSessionHeader:
    """call_llm attaches X-Hermes-Session only for known loopback lanes."""

    @staticmethod
    def _run(monkeypatch, session_id, base_url):
        from agent import auxiliary_client as ac

        client = MagicMock()
        client.base_url = base_url
        resp = MagicMock()
        resp.choices[0].message.content = "ok"
        client.chat.completions.create.return_value = resp

        monkeypatch.setattr(
            ac, "_resolve_task_provider_model",
            lambda *a, **k: ("zai", "m", base_url, "key", "chat_completions"))
        monkeypatch.setattr(
            ac, "_get_cached_client", lambda *a, **k: (client, "m"))
        monkeypatch.setattr(ac, "_get_task_extra_body", lambda task: {})
        monkeypatch.setattr(ac, "_get_task_timeout", lambda task: 30)
        monkeypatch.setattr(
            ac, "_validate_llm_response", lambda r, task: r)

        ac.call_llm(
            task="compression",
            messages=[{"role": "user", "content": "hi"}],
            session_id=session_id,
        )
        _, create_kwargs = client.chat.completions.create.call_args
        return create_kwargs.get("extra_headers") or {}

    def test_header_attached_on_loopback(self, monkeypatch):
        headers = self._run(
            monkeypatch,
            session_id="20260902_051500_ababab",
            base_url="http://localhost:9099/v1",
        )
        assert headers.get("X-Hermes-Session") == "20260902_051500_ababab"
        assert headers.get("X-Task-Type") == "compression"

    def test_no_header_on_external_endpoint(self, monkeypatch):
        headers = self._run(
            monkeypatch,
            session_id="20260902_051500_ababab",
            base_url="https://api.z.ai/api/paas/v4",
        )
        assert "X-Hermes-Session" not in headers
        assert headers.get("X-Task-Type") == "compression"

    def test_no_header_without_session_id(self, monkeypatch):
        headers = self._run(
            monkeypatch,
            session_id=None,
            base_url="http://localhost:9099/v1",
        )
        assert "X-Hermes-Session" not in headers

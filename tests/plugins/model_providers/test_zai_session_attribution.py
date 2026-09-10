"""Productivity-gate §1.4 — Z.AI profile session-attribution header tests.

The local zai-proxy (``~/.hermes/bot/zai_proxy.py``) records
``api_calls.session_id`` from an ``X-Hermes-Session`` request header.  Every
profile whose config points at the proxy (72 of them: ``provider: zai`` +
``base_url: http://localhost:9099``) reaches it through this profile, so the
header has to be injected here.

Two contracts are pinned:

* it is sent for loopback endpoints (localhost / 127.0.0.1 / ::1), resolved
  from the transport-supplied ``session_id`` with a ``HERMES_SESSION_ID``
  fallback;
* it is NEVER sent anywhere else — a leaked attribution header is worse than
  an unattributed call (design doc §7 risk #5).

Regression note: an earlier orphaned copy of this hook (which predated the
GLM thinking/reasoning wiring and therefore arrived with an early ``return``
that skipped every non-thinking model) was reset away by a fork sync on
2026-09-04, which is why attribution silently dropped to ~0% from 2026-09-06
until this landed.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def zai_profile():
    """Resolve the registered Z.AI profile through the real discovery path."""
    import model_tools  # noqa: F401  (trigger plugin discovery)
    import providers

    profile = providers.get_provider_profile("zai")
    assert profile is not None, "zai provider profile must be registered"
    return profile


def _headers(top_level: dict) -> dict:
    return dict(top_level.get("extra_headers") or {})


class TestSessionHeaderEmission:
    def test_loopback_endpoint_gets_session_header(self, zai_profile):
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="sess-abc", base_url="http://localhost:9099"
        )
        assert _headers(top_level).get("X-Hermes-Session") == "sess-abc"

    @pytest.mark.parametrize(
        "base_url",
        [
            "http://127.0.0.1:9099",
            "http://localhost:9099/v1",
            "http://[::1]:9099/v1/chat/completions",
            "localhost:9099",  # schemeless — must not slip past the guard
        ],
    )
    def test_all_loopback_spellings_get_header(self, zai_profile, base_url):
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="sess-abc", base_url=base_url
        )
        assert _headers(top_level).get("X-Hermes-Session") == "sess-abc"

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.z.ai/api/paas/v4",
            "https://api.z.ai/api/coding/paas/v4",
            "http://192.168.1.50:9099",  # LAN proxy is still not loopback
            "https://localhost.evil.example/v1",  # suffix spoof
            "https://proxy.example.com/localhost",
        ],
    )
    def test_non_loopback_endpoints_never_get_header(self, zai_profile, base_url):
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="sess-abc", base_url=base_url
        )
        assert "X-Hermes-Session" not in _headers(top_level)

    def test_base_url_none_falls_back_to_profile_default(self, zai_profile):
        """No endpoint context → the profile's own Z.AI base_url, so no header."""
        _, top_level = zai_profile.build_api_kwargs_extras(session_id="sess-abc")
        assert "X-Hermes-Session" not in _headers(top_level)

    def test_env_fallback_when_transport_passes_no_session(self, zai_profile, monkeypatch):
        """Aux-client path passes base_url but no session_id — env covers it."""
        monkeypatch.setenv("HERMES_SESSION_ID", "sess-env")
        _, top_level = zai_profile.build_api_kwargs_extras(base_url="http://localhost:9099")
        assert _headers(top_level).get("X-Hermes-Session") == "sess-env"

    def test_explicit_session_wins_over_env(self, zai_profile, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_ID", "sess-env")
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="sess-explicit", base_url="http://localhost:9099"
        )
        assert _headers(top_level).get("X-Hermes-Session") == "sess-explicit"

    def test_no_session_id_anywhere_means_no_header(self, zai_profile, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        _, top_level = zai_profile.build_api_kwargs_extras(base_url="http://localhost:9099")
        assert "X-Hermes-Session" not in _headers(top_level)

    def test_blank_session_id_means_no_header(self, zai_profile, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="   ", base_url="http://localhost:9099"
        )
        assert "X-Hermes-Session" not in _headers(top_level)


class TestSessionHeaderCoexistsWithReasoningWiring:
    """The header must not depend on the model-gated reasoning branch.

    The regression that killed attribution was an early ``return`` in the
    reasoning gate: any model that takes no ``thinking`` field (or any request
    with no reasoning preference) skipped the header entirely.
    """

    def test_thinking_capable_model_keeps_both(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model="glm-5.2",
            session_id="sess-both",
            base_url="http://localhost:9099",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level["reasoning_effort"] == "max"
        assert _headers(top_level)["X-Hermes-Session"] == "sess-both"

    def test_non_thinking_model_still_gets_header(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="glm-4-9b",
            session_id="sess-old-glm",
            base_url="http://localhost:9099",
        )
        assert extra_body == {}  # pre-4.5 GLM takes no thinking field
        assert _headers(top_level)["X-Hermes-Session"] == "sess-old-glm"

    def test_no_reasoning_config_still_gets_header(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config=None,
            model="glm-5",
            session_id="sess-plain",
            base_url="http://localhost:9099",
        )
        assert extra_body == {}
        assert _headers(top_level)["X-Hermes-Session"] == "sess-plain"

    def test_unknown_model_still_gets_header(self, zai_profile):
        _, top_level = zai_profile.build_api_kwargs_extras(
            model=None, session_id="sess-unknown", base_url="http://localhost:9099"
        )
        assert _headers(top_level)["X-Hermes-Session"] == "sess-unknown"


class TestTransportWiring:
    """The real transport must surface the header in its final api_kwargs."""

    def test_build_kwargs_carries_header_for_loopback(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            reasoning_config={"enabled": True, "effort": "max"},
            base_url="http://localhost:9099",
            provider_name="zai",
            session_id="sess-transport",
        )
        assert kwargs["extra_headers"]["X-Hermes-Session"] == "sess-transport"
        assert kwargs["reasoning_effort"] == "max"

    def test_build_kwargs_omits_header_for_real_zai(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            base_url="https://api.z.ai/api/paas/v4",
            provider_name="zai",
            session_id="sess-transport",
        )
        assert "X-Hermes-Session" not in dict(kwargs.get("extra_headers") or {})


class TestSessionHeaderOnTheWire:
    """Wire-level: a real OpenAI SDK POST carries the header (capture server).

    The dict-level tests above prove the hook's contract; this proves the SDK
    actually transmits it, which is the part the proxy depends on.
    """

    @staticmethod
    def _capture_server():
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        captured: dict = {}

        class _Capture(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                captured["headers"] = dict(self.headers)
                captured["body"] = json.loads(self.rfile.read(length) or b"{}")
                payload = json.dumps(
                    {
                        "id": "chatcmpl-test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "glm-5.2",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "ok"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args, **kwargs):  # silence
                pass

        server = HTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, captured

    def test_openai_sdk_transmits_the_header(self, zai_profile):
        from openai import OpenAI

        server, captured = self._capture_server()
        try:
            port = server.server_address[1]
            base_url = f"http://127.0.0.1:{port}/v1"
            _, top_level = zai_profile.build_api_kwargs_extras(
                session_id="sess-wire", base_url=base_url
            )
            # Exactly what transports/chat_completions.py does: merge the
            # hook's top-level kwargs into the request kwargs.
            api_kwargs = {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
            }
            api_kwargs.update(top_level)
            response = OpenAI(base_url=base_url, api_key="test-key").chat.completions.create(
                **api_kwargs
            )
        finally:
            server.shutdown()

        assert response.choices[0].message.content == "ok"
        assert captured["headers"].get("X-Hermes-Session") == "sess-wire"

    def test_openai_sdk_sends_no_header_for_real_zai(self, zai_profile):
        """Guard: pointed at a non-loopback host the kwargs carry no header.

        Asserted at the kwargs level (no live call) — the point is that
        nothing loopback-only can reach a real endpoint.
        """
        _, top_level = zai_profile.build_api_kwargs_extras(
            session_id="sess-wire", base_url="https://api.z.ai/api/paas/v4"
        )
        assert "extra_headers" not in top_level or "X-Hermes-Session" not in top_level[
            "extra_headers"
        ]

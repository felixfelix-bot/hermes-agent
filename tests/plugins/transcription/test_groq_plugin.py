#!/usr/bin/env python3
"""Tests for the groq transcription plugin (``plugins/transcription/groq``).

Audio-bridge AB-3: REST multipart client for Groq's OpenAI-compatible
``/audio/transcriptions`` endpoint (whisper-large-v3), selected via the
``transcription`` section in config.yaml.

All HTTP is faked — no network in CI, no live Groq calls (cost gate).
"""

from __future__ import annotations

import logging

import httpx
import pytest

import plugins.transcription.groq as groq_plugin
from agent.transcription_provider import TranscriptionProvider
from tools.transcription_tools import BUILTIN_STT_PROVIDERS


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload


class _FakeClientFactory:
    """Replaces ``httpx.Client`` in the plugin module.

    ``queue`` holds either ``_FakeResponse`` instances or exceptions to
    raise. Records every ``post()`` attempt for assertion.
    """

    def __init__(self, queue: list):
        self.queue = queue
        self.posts: list[dict] = []
        self.timeouts: list = []

    def __call__(self, timeout=None, **kw):
        self.timeouts.append(timeout)
        factory = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, **kwargs):
                factory.posts.append({"url": url, **kwargs})
                item = factory.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        return _Client()


@pytest.fixture()
def audio_file(tmp_path):
    path = tmp_path / "voice.mp3"
    path.write_bytes(b"ID3 fake-mp3-bytes-for-tests")
    return str(path)


@pytest.fixture()
def env(monkeypatch):
    values: dict[str, str] = {"GROQ_API_KEY": "gsk_test_secret"}
    monkeypatch.setattr(
        groq_plugin, "get_env_value", lambda name, default=None: values.get(name, default)
    )
    return values


def _provider(**kw) -> groq_plugin.GroqTranscriptionProvider:
    return groq_plugin.GroqTranscriptionProvider(**kw)


# ---------------------------------------------------------------------------
# Provider surface
# ---------------------------------------------------------------------------


class TestGroqProviderSurface:
    def test_implements_abc(self):
        assert isinstance(_provider(), TranscriptionProvider)

    def test_name_is_groq_bridge_not_builtin(self):
        """Built-ins-always-win: the registry rejects plugin names that
        shadow a built-in, so the audio-bridge groq client registers as
        ``groq_bridge`` (documented deviation from config value ``groq``)."""
        provider = _provider()
        assert provider.name == "groq_bridge"
        assert provider.name not in BUILTIN_STT_PROVIDERS

    def test_display_name(self):
        assert _provider().display_name == "Groq Whisper (audio bridge)"

    def test_default_model_is_whisper_large_v3(self):
        assert _provider().default_model() == "whisper-large-v3"

    def test_list_models_advertises_large_v3(self):
        ids = [m["id"] for m in _provider().list_models()]
        assert "whisper-large-v3" in ids

    def test_setup_schema_prompts_for_configured_env_var(self):
        schema = _provider(api_key_env="MY_TENANT_GROQ_KEY").get_setup_schema()
        keys = [e["key"] for e in schema["env_vars"]]
        assert "MY_TENANT_GROQ_KEY" in keys

    def test_is_available_depends_on_key(self, env):
        assert _provider().is_available() is True
        env.clear()
        assert _provider().is_available() is False

    def test_is_available_uses_configured_env_name(self, monkeypatch):
        monkeypatch.setattr(
            groq_plugin,
            "get_env_value",
            lambda name, default=None: "k" if name == "TENANT_KEY" else None,
        )
        assert _provider(api_key_env="TENANT_KEY").is_available() is True
        assert _provider().is_available() is False


# ---------------------------------------------------------------------------
# transcribe() — happy paths
# ---------------------------------------------------------------------------


class TestGroqTranscribeSuccess:
    def test_success_returns_envelope(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "hello world"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result == {
            "success": True,
            "transcript": "hello world",
            "provider": "groq_bridge",
        }
        assert len(factory.posts) == 1

    def test_multipart_request_shape(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "x"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        _provider().transcribe(audio_file)

        post = factory.posts[0]
        assert post["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
        assert post["headers"]["Authorization"] == "Bearer gsk_test_secret"
        assert post["data"]["model"] == "whisper-large-v3"
        fname, content, mime = post["files"]["file"]
        assert fname == "voice.mp3"
        assert content == b"ID3 fake-mp3-bytes-for-tests"
        assert mime == "audio/mpeg"

    def test_timeout_is_60s(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "x"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        _provider().transcribe(audio_file)

        assert factory.timeouts == [60.0]

    def test_api_key_env_indirection(self, monkeypatch, audio_file):
        monkeypatch.setattr(
            groq_plugin,
            "get_env_value",
            lambda name, default=None: "sk-custom" if name == "TENANT_STT_KEY" else None,
        )
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "x"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        _provider(api_key_env="TENANT_STT_KEY").transcribe(audio_file)

        assert factory.posts[0]["headers"]["Authorization"] == "Bearer sk-custom"

    def test_language_hint_forwarded(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "x"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        _provider().transcribe(audio_file, language="de")

        assert factory.posts[0]["data"]["language"] == "de"

    def test_model_override_forwarded(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(200, {"text": "x"})])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        _provider().transcribe(audio_file, model="whisper-large-v3-turbo")

        assert factory.posts[0]["data"]["model"] == "whisper-large-v3-turbo"


# ---------------------------------------------------------------------------
# transcribe() — failure modes
# ---------------------------------------------------------------------------


class TestGroqTranscribeFailures:
    def test_missing_key_error_envelope_without_http_call(self, monkeypatch, audio_file):
        monkeypatch.setattr(groq_plugin, "get_env_value", lambda name, default=None: None)
        factory = _FakeClientFactory([])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert result["transcript"] == ""
        assert result["provider"] == "groq_bridge"
        assert "GROQ_API_KEY" in result["error"]
        assert factory.posts == []

    def test_5xx_retried_once_then_succeeds(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory(
            [_FakeResponse(500, {"error": "boom"}), _FakeResponse(200, {"text": "second try"})]
        )
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is True
        assert result["transcript"] == "second try"
        assert len(factory.posts) == 2
        # Both attempts must carry the full multipart payload (file re-read).
        for post in factory.posts:
            assert post["files"]["file"][1] == b"ID3 fake-mp3-bytes-for-tests"

    def test_5xx_twice_fails_after_exactly_one_retry(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory(
            [_FakeResponse(500, text="upstream"), _FakeResponse(503, text="still down")]
        )
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert len(factory.posts) == 2
        assert "503" in result["error"]

    def test_4xx_not_retried(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([_FakeResponse(401, text="bad key")])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert len(factory.posts) == 1
        assert "401" in result["error"]

    def test_timeout_not_retried(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([httpx.TimeoutException("timed out")])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert len(factory.posts) == 1
        assert "timed out" in result["error"].lower()

    def test_connect_error_returns_envelope(self, monkeypatch, audio_file, env):
        factory = _FakeClientFactory([httpx.ConnectError("dns broke")])
        monkeypatch.setattr(groq_plugin.httpx, "Client", factory)

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert result["transcript"] == ""
        assert "dns broke" in result["error"]

    def test_missing_file_returns_envelope(self, env, tmp_path):
        result = _provider().transcribe(str(tmp_path / "nope.mp3"))

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ---------------------------------------------------------------------------
# register(ctx) — config gating
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self):
        self.providers = []

    def register_transcription_provider(self, provider):
        self.providers.append(provider)


class TestGroqRegister:
    def _register(self, monkeypatch, cfg) -> _FakeCtx:
        monkeypatch.setattr(groq_plugin, "_load_transcription_config", lambda: cfg)
        ctx = _FakeCtx()
        groq_plugin.register(ctx)
        return ctx

    def test_registers_when_selected(self, monkeypatch):
        ctx = self._register(monkeypatch, {"provider": "groq", "api_key_env": "T_KEY"})
        assert [p.name for p in ctx.providers] == ["groq_bridge"]
        assert ctx.providers[0].get_setup_schema()["env_vars"][0]["key"] == "T_KEY"

    def test_default_api_key_env(self, monkeypatch):
        ctx = self._register(monkeypatch, {"provider": "groq"})
        assert ctx.providers[0].get_setup_schema()["env_vars"][0]["key"] == "GROQ_API_KEY"

    def test_not_selected_registers_none(self, monkeypatch):
        for other in ("whisper_cpp", "local"):
            ctx = self._register(monkeypatch, {"provider": other})
            assert ctx.providers == []

    def test_unknown_provider_warns_and_registers_none(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger=groq_plugin.__name__):
            ctx = self._register(monkeypatch, {"provider": "deepgram"})
        assert ctx.providers == []
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "unknown transcription.provider must log a warning"
        assert "deepgram" in warnings[0].getMessage()

    def test_missing_section_registers_none_without_warning(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger=groq_plugin.__name__):
            ctx = self._register(monkeypatch, {})
        assert ctx.providers == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_config_load_failure_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(groq_plugin, "_load_transcription_config", _boom)
        ctx = _FakeCtx()
        groq_plugin.register(ctx)  # must not raise
        assert ctx.providers == []

    def test_groq_name_would_be_rejected_by_registry(self):
        """Contract guard: if someone renames the provider to ``groq`` the
        registry silently drops it (built-ins always win) — the plugin must
        never claim that name."""
        from agent import transcription_registry

        class _Named(_provider().__class__):
            @property
            def name(self):
                return "groq"

        before = {p.name for p in transcription_registry.list_providers()}
        transcription_registry.register_provider(_Named())
        after = {p.name for p in transcription_registry.list_providers()}
        assert "groq" not in after
        assert before == after

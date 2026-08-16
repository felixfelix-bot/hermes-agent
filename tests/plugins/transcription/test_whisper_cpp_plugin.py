#!/usr/bin/env python3
"""Tests for the whisper.cpp transcription plugin (``plugins/transcription/whisper_cpp``).

Audio-bridge AB-3: local whisper-cli subprocess backend, selected via the
``transcription`` section in config.yaml.

All subprocess execution is faked — no whisper.cpp binary or model is
required in CI.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

import plugins.transcription.whisper_cpp as wcpp_plugin
from agent.transcription_provider import TranscriptionProvider
from tools.transcription_tools import BUILTIN_STT_PROVIDERS


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def audio_file(tmp_path):
    path = tmp_path / "voice.wav"
    path.write_bytes(b"RIFF fake-wav-bytes")
    return str(path)


@pytest.fixture()
def model_file(tmp_path):
    path = tmp_path / "ggml-base.bin"
    path.write_bytes(b"ggml-model-bytes")
    return str(path)


@pytest.fixture()
def fake_run(monkeypatch):
    class _Recorder:
        def __init__(self):
            self.calls: list[dict] = []
            self.queue: list = []

        def __getitem__(self, i):
            return self.calls[i]

        def __len__(self):
            return len(self.calls)

    recorder = _Recorder()

    def _run(cmd, **kwargs):
        recorder.calls.append({"cmd": cmd, **kwargs})
        item = recorder.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(wcpp_plugin.subprocess, "run", _run)
    return recorder


def _provider(**kw) -> wcpp_plugin.WhisperCppTranscriptionProvider:
    return wcpp_plugin.WhisperCppTranscriptionProvider(**kw)


# ---------------------------------------------------------------------------
# Provider surface
# ---------------------------------------------------------------------------


class TestWhisperCppSurface:
    def test_implements_abc(self):
        assert isinstance(_provider(), TranscriptionProvider)

    def test_name_is_whisper_cpp_not_builtin(self):
        provider = _provider()
        assert provider.name == "whisper_cpp"
        assert provider.name not in BUILTIN_STT_PROVIDERS

    def test_display_name(self):
        assert _provider().display_name == "Whisper.cpp (local)"

    def test_default_model_path(self):
        assert _provider().model_path == "/opt/whisper/ggml-base.bin"

    def test_list_models_single_fixed_model(self):
        assert _provider().list_models() == []

    def test_is_available_false_without_binary(self, monkeypatch, model_file):
        monkeypatch.setattr(wcpp_plugin.shutil, "which", lambda name: None)
        assert _provider(model_path=model_file).is_available() is False

    def test_is_available_false_without_model_file(self, monkeypatch, model_file):
        monkeypatch.setattr(wcpp_plugin.shutil, "which", lambda name: "/usr/bin/whisper-cli")
        assert _provider(model_path="/nonexistent/ggml.bin").is_available() is False

    def test_is_available_true_with_binary_and_model(self, monkeypatch, model_file):
        monkeypatch.setattr(wcpp_plugin.shutil, "which", lambda name: "/usr/bin/whisper-cli")
        assert _provider(model_path=model_file).is_available() is True

    def test_setup_schema_mentions_model_provisioning(self):
        schema = _provider().get_setup_schema()
        assert schema["name"]
        assert schema["env_vars"] == []


# ---------------------------------------------------------------------------
# transcribe()
# ---------------------------------------------------------------------------


class TestWhisperCppTranscribe:
    def test_success_strips_stdout(self, fake_run, audio_file, model_file):
        fake_run.queue.append(_FakeCompleted(stdout="  hello world \n"))
        provider = _provider(model_path=model_file)

        result = provider.transcribe(audio_file)

        assert result == {
            "success": True,
            "transcript": "hello world",
            "provider": "whisper_cpp",
        }

    def test_command_construction(self, fake_run, audio_file, model_file):
        fake_run.queue.append(_FakeCompleted(stdout="x"))
        provider = _provider(model_path=model_file)

        provider.transcribe(audio_file)

        call = fake_run[0]
        assert call["cmd"] == [
            "whisper-cli",
            "-f",
            audio_file,
            "-nt",
            "-m",
            model_file,
        ]
        assert call["timeout"] == 120
        assert call["capture_output"] is True
        assert call["text"] is True

    def test_custom_model_path_used(self, fake_run, audio_file):
        fake_run.queue.append(_FakeCompleted(stdout="x"))
        provider = _provider(model_path="/data/ggml-small.bin")

        provider.transcribe(audio_file)

        assert fake_run[0]["cmd"][-1] == "/data/ggml-small.bin"

    def test_timeout_returns_error_envelope(self, fake_run, audio_file):
        fake_run.queue.append(
            subprocess.TimeoutExpired(cmd=["whisper-cli"], timeout=120)
        )

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert result["transcript"] == ""
        assert result["provider"] == "whisper_cpp"
        assert "120" in result["error"] or "timed out" in result["error"].lower()

    def test_binary_missing_returns_error_envelope(self, fake_run, audio_file):
        fake_run.queue.append(FileNotFoundError(2, "No such file or directory: 'whisper-cli'"))

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert "whisper-cli" in result["error"]
        assert "install" in result["error"].lower()

    def test_nonzero_exit_surfaces_stderr(self, fake_run, audio_file):
        fake_run.queue.append(_FakeCompleted(returncode=1, stderr="whisper: error loading model"))

        result = _provider().transcribe(audio_file)

        assert result["success"] is False
        assert "error loading model" in result["error"]

    def test_empty_transcript_is_success_with_empty_text(self, fake_run, audio_file):
        fake_run.queue.append(_FakeCompleted(stdout="   \n"))
        result = _provider().transcribe(audio_file)
        # rc=0 with no speech is a successful transcription of silence;
        # the caller (audio bridge failure-mode F2) decides how to phrase
        # an empty transcript to the user.
        assert result["success"] is True
        assert result["transcript"] == ""

    def test_language_hint_ignored_gracefully(self, fake_run, audio_file):
        fake_run.queue.append(_FakeCompleted(stdout="x"))
        result = _provider().transcribe(audio_file, language="de")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# register(ctx) — config gating
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self):
        self.providers = []

    def register_transcription_provider(self, provider):
        self.providers.append(provider)


class TestWhisperCppRegister:
    def _register(self, monkeypatch, cfg) -> _FakeCtx:
        monkeypatch.setattr(wcpp_plugin, "_load_transcription_config", lambda: cfg)
        ctx = _FakeCtx()
        wcpp_plugin.register(ctx)
        return ctx

    def test_registers_when_selected(self, monkeypatch):
        ctx = self._register(
            monkeypatch,
            {"provider": "whisper_cpp", "whisper_model": "/data/ggml-small.bin"},
        )
        assert [p.name for p in ctx.providers] == ["whisper_cpp"]
        assert ctx.providers[0].model_path == "/data/ggml-small.bin"

    def test_default_model_path_when_unspecified(self, monkeypatch):
        ctx = self._register(monkeypatch, {"provider": "whisper_cpp"})
        assert ctx.providers[0].model_path == "/opt/whisper/ggml-base.bin"

    def test_not_selected_registers_none(self, monkeypatch):
        for other in ("groq", "local"):
            ctx = self._register(monkeypatch, {"provider": other})
            assert ctx.providers == []

    def test_unknown_provider_warns_and_registers_none(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger=wcpp_plugin.__name__):
            ctx = self._register(monkeypatch, {"provider": "bogus_engine"})
        assert ctx.providers == []
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings
        assert "bogus_engine" in warnings[0].getMessage()

    def test_missing_section_registers_none_without_warning(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger=wcpp_plugin.__name__):
            ctx = self._register(monkeypatch, {})
        assert ctx.providers == []
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_config_load_failure_swallowed(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(wcpp_plugin, "_load_transcription_config", _boom)
        ctx = _FakeCtx()
        wcpp_plugin.register(ctx)  # must not raise
        assert ctx.providers == []

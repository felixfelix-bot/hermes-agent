#!/usr/bin/env python3
"""End-to-end wiring tests for the bundled transcription plugins (AB-3).

Exercises the REAL path the nostr audio bridge uses at runtime:

    PluginManager().discover_and_load()
      → bundled plugins/transcription/{groq,whisper_cpp} (kind: backend,
        auto-load) → register(ctx) reads the ``transcription`` section
        from $HERMES_HOME/config.yaml → selected provider lands in
        agent.transcription_registry
      → tools.transcription_tools.transcribe_audio() dispatches through
        _dispatch_to_plugin_provider to the registered plugin.

No network (groq client never invoked; whisper-cli subprocess faked).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLED = REPO_ROOT / "plugins" / "transcription"


def _write_config(hermes_home: Path, data: dict) -> None:
    cfg_path = hermes_home / "config.yaml"
    cfg: dict = {}
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            cfg = {}
    cfg.update(data)
    cfg_path.write_text(yaml.safe_dump(cfg))


def _reset_registry():
    from agent import transcription_registry

    transcription_registry._reset_for_tests()
    return transcription_registry


class TestBundledPluginWiring:
    def test_groq_selected_registers_groq_bridge(self, monkeypatch):
        hermes_home = Path(os.environ["HERMES_HOME"])
        monkeypatch.setenv("BRIDGE_GROQ_KEY", "sk-bridge-test")
        _write_config(
            hermes_home,
            {
                "transcription": {
                    "provider": "groq",
                    "api_key_env": "BRIDGE_GROQ_KEY",
                }
            },
        )
        registry = _reset_registry()

        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        mgr.discover_and_load()

        provider = registry.get_provider("groq_bridge")
        assert provider is not None, (
            f"groq plugin did not register; loaded plugins: "
            f"{ {k: (v.enabled, v.error) for k, v in mgr._plugins.items() if 'transcription' in k} }"
        )
        assert provider.is_available() is True
        # Only the selected backend registers.
        assert registry.get_provider("whisper_cpp") is None

        _reset_registry()

    def test_whisper_cpp_selected_and_dispatch(self, monkeypatch, tmp_path):
        hermes_home = Path(os.environ["HERMES_HOME"])
        model_bin = tmp_path / "ggml-base.bin"
        model_bin.write_bytes(b"ggml")
        _write_config(
            hermes_home,
            {
                "transcription": {
                    "provider": "whisper_cpp",
                    "whisper_model": str(model_bin),
                },
                "stt": {"provider": "whisper_cpp"},
            },
        )
        registry = _reset_registry()

        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        mgr.discover_and_load()

        provider = registry.get_provider("whisper_cpp")
        assert provider is not None, (
            f"whisper_cpp plugin did not register; loaded plugins: "
            f"{ {k: (v.enabled, v.error) for k, v in mgr._plugins.items() if 'transcription' in k} }"
        )
        assert provider.model_path == str(model_bin)

        # whisper-cli isn't installed in CI; the availability gate would
        # (correctly) refuse dispatch. Fake the binary lookup — the actual
        # subprocess execution is faked below.
        monkeypatch.setattr(
            shutil, "which", lambda name: "/usr/bin/whisper-cli" if name == "whisper-cli" else None
        )

        # Runtime dispatch: the exact call chain the nostr audio bridge
        # makes (AB-2 nostr.py → tools.transcription_tools.transcribe_audio).
        calls: list[dict] = []

        def _fake_run(cmd, **kwargs):
            calls.append({"cmd": cmd, **kwargs})

            class _R:
                returncode = 0
                stdout = "dispatched fine\n"
                stderr = ""

            return _R()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        audio = tmp_path / "note.wav"
        audio.write_bytes(b"RIFF test-bytes")

        from tools.transcription_tools import transcribe_audio

        result = transcribe_audio(str(audio))

        assert result["success"] is True, f"dispatch failed: {result.get('error')}"
        assert result["transcript"] == "dispatched fine"
        assert result["provider"] == "whisper_cpp"
        assert calls[0]["cmd"] == [
            "whisper-cli",
            "-f",
            str(audio),
            "-nt",
            "-m",
            str(model_bin),
        ]

        _reset_registry()

    def test_unknown_provider_registers_none_and_warns(self, caplog):
        hermes_home = Path(os.environ["HERMES_HOME"])
        _write_config(hermes_home, {"transcription": {"provider": "azure_stt"}})
        registry = _reset_registry()

        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        with caplog.at_level("WARNING"):
            mgr.discover_and_load()

        assert registry.get_provider("groq_bridge") is None
        assert registry.get_provider("whisper_cpp") is None
        assert "azure_stt" in caplog.text

        _reset_registry()

    def test_no_transcription_section_registers_none(self, caplog):
        registry = _reset_registry()

        from hermes_cli.plugins import PluginManager

        mgr = PluginManager()
        with caplog.at_level("WARNING"):
            mgr.discover_and_load()

        assert registry.get_provider("groq_bridge") is None
        assert registry.get_provider("whisper_cpp") is None
        # Silence is correct here: unconfigured means "use built-ins",
        # not a misconfiguration worth warning about.
        assert "transcription.provider" not in caplog.text

        _reset_registry()

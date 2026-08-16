"""Whisper.cpp transcription backend (audio bridge AB-3).

Shells out to ``whisper-cli`` (whisper.cpp) with no-timestamps output:

    whisper-cli -f <audio-file> -nt -m <model-path>

Selected via the ``transcription`` section in ``config.yaml``:

    transcription:
      provider: whisper_cpp           # selects this backend
      whisper_model: /opt/whisper/ggml-base.bin   # ggml model path

Semantics (P1 design §D):
- 120 s subprocess timeout (whisper.cpp runs ~1–2× realtime on 1 vCPU;
  a 60 s clip takes up to ~2 minutes).
- Registers as ``whisper_cpp`` — no built-in collision, so
  ``stt.provider: whisper_cpp`` dispatches here directly through
  :func:`tools.transcription_tools.transcribe_audio`.
- Audio never leaves the host (privacy fallback for tenants that can't
  use a third-party STT API).

OPS NOTE — the ggml model binary is NOT shipped with this plugin and
must be provisioned separately::

    mkdir -p /opt/whisper
    curl -L -o /opt/whisper/ggml-base.bin \
        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
    # (base ≈ 148 MB; small ≈ 488 MB is more accurate for multilingual)

The binary ``whisper-cli`` must be on PATH (``apt install whisper.cpp``
where packaged, or build from source per whisper.cpp README).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

from agent.transcription_provider import TranscriptionProvider

logger = logging.getLogger(__name__)

WHISPER_CLI_BINARY = "whisper-cli"
DEFAULT_MODEL_PATH = "/opt/whisper/ggml-base.bin"
SUBPROCESS_TIMEOUT_SECONDS = 120.0

#: See plugins/transcription/groq/__init__.py for why this set is
#: duplicated per plugin (bundled plugins are self-contained).
KNOWN_TRANSCRIPTION_PROVIDERS = {"groq", "whisper_cpp", "local"}


def _load_transcription_config() -> Dict[str, Any]:
    """Load the ``transcription`` section from user config (config.yaml)."""
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("transcription")
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001 — config problems must not crash plugin load
        return {}


class WhisperCppTranscriptionProvider(TranscriptionProvider):
    """Local whisper.cpp backend via the ``whisper-cli`` binary."""

    def __init__(
        self,
        *,
        model_path: str = DEFAULT_MODEL_PATH,
        timeout: float = SUBPROCESS_TIMEOUT_SECONDS,
        binary: str = WHISPER_CLI_BINARY,
    ) -> None:
        # Public read-only config: surfaced in diagnostics and asserted by
        # the wiring tests (which model bin a tenant actually deployed).
        self.model_path = model_path
        self._timeout = timeout
        self._binary = binary

    # -- ABC surface ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "whisper_cpp"

    @property
    def display_name(self) -> str:
        return "Whisper.cpp (local)"

    def list_models(self):
        # Single fixed ggml model chosen at deploy time via
        # transcription.whisper_model — no runtime model catalog.
        return []

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local",
            "tag": (
                "whisper-cli + ggml model (provision the model bin "
                "separately — see plugins/transcription/whisper_cpp/README.md)"
            ),
            "env_vars": [],  # no secrets; binary + model file instead
        }

    def is_available(self) -> bool:
        try:
            if shutil.which(self._binary) is None:
                return False
            return Path(self.model_path).is_file()
        except Exception:  # noqa: BLE001 — must not raise per ABC contract
            return False

    # -- transcription -------------------------------------------------------

    def transcribe(
        self,
        file_path: str,
        *,
        model: Any = None,  # ignored — the model is the provisioned ggml bin
        language: Any = None,  # ignored — pass -l to whisper-cli if ever needed
        **extra: Any,
    ) -> Dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            return self._error(f"audio file not found: {file_path}")

        cmd = [
            self._binary,
            "-f",
            str(path),
            "-nt",  # no timestamps — stdout is the bare transcript
            "-m",
            self.model_path,
        ]
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError:
            return self._error(
                f"'{self._binary}' not found on PATH — install whisper.cpp "
                f"(see plugins/transcription/whisper_cpp/README.md)"
            )
        except subprocess.TimeoutExpired:
            return self._error(
                f"whisper-cli timed out after {int(self._timeout)}s "
                f"(model={self.model_path})"
            )

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            return self._error(
                f"whisper-cli exited {completed.returncode}: {stderr[:500]}"
            )

        # rc=0 with empty stdout = silence transcribed as empty text; the
        # caller (audio bridge failure-mode F2) decides how to phrase it.
        transcript = (completed.stdout or "").strip()
        return {
            "success": True,
            "transcript": transcript,
            "provider": self.name,
        }

    def _error(self, message: str) -> Dict[str, Any]:
        return {
            "success": False,
            "transcript": "",
            "error": message,
            "provider": self.name,
        }


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:  # noqa: ANN001 — PluginContext, kept untyped like siblings
    """Register the whisper.cpp backend when ``transcription.provider``
    selects it. Gating rules mirror the groq plugin — see
    plugins/transcription/groq/__init__.py."""
    try:
        cfg = _load_transcription_config()
    except Exception:  # noqa: BLE001 — defensive; loader already swallows
        logger.debug(
            "transcription config unreadable — whisper_cpp plugin not registered",
            exc_info=True,
        )
        return

    selected = str(cfg.get("provider") or "").strip().lower()
    if not selected:
        logger.debug(
            "transcription.provider not set — whisper_cpp plugin not "
            "registered (built-in STT providers remain available)"
        )
        return
    if selected not in KNOWN_TRANSCRIPTION_PROVIDERS:
        logger.warning(
            "transcription.provider='%s' is not one of %s — no transcription "
            "plugin registered; audio-bridge voice notes will get the "
            "fallback reply until this is fixed",
            selected,
            "|".join(sorted(KNOWN_TRANSCRIPTION_PROVIDERS)),
        )
        return
    if selected != "whisper_cpp":
        return

    model_path = (
        str(cfg.get("whisper_model") or "").strip() or DEFAULT_MODEL_PATH
    )
    ctx.register_transcription_provider(
        WhisperCppTranscriptionProvider(model_path=model_path)
    )
    logger.info(
        "whisper_cpp transcription plugin registered (model: %s)", model_path
    )

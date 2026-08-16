"""Groq Whisper transcription backend (audio bridge AB-3).

REST multipart client for Groq's OpenAI-compatible transcription
endpoint (``POST /openai/v1/audio/transcriptions``, model
``whisper-large-v3``). Selected via the ``transcription`` section in
``config.yaml``:

    transcription:
      provider: groq          # selects this backend
      api_key_env: GROQ_API_KEY   # NAME of the env var holding the secret

Why ``groq_bridge`` and not ``groq``
------------------------------------
``groq`` is a *built-in* STT provider name — the registry
(:mod:`agent.transcription_registry`) rejects plugin names that shadow
built-ins (built-ins always win). This plugin therefore registers as
``groq_bridge``; point ``stt.provider: groq_bridge`` at it for dispatch
through :func:`tools.transcription_tools.transcribe_audio`. The config
value that *selects* the backend remains ``transcription.provider: groq``
per the P1 audio-bridge design (§D).

Semantics (P1 design §D):
- 60 s request timeout, no timeout retry.
- HTTP 5xx → exactly one retry, then fail (error envelope).
- API key read at call time from the env var NAMED by
  ``transcription.api_key_env`` (default ``GROQ_API_KEY``) — the key
  itself is never hardcoded or stored in config.yaml.

The nostr audio bridge (AB-2) consumes this via
``tools.transcription_tools.transcribe_audio``; failure envelopes map to
design failure-mode F#2 ("couldn't transcribe that, try again").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent.transcription_provider import TranscriptionProvider
from hermes_cli.config import get_env_value

logger = logging.getLogger(__name__)

GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_API_KEY_ENV = "GROQ_API_KEY"
DEFAULT_GROQ_STT_MODEL = "whisper-large-v3"
REQUEST_TIMEOUT_SECONDS = 60.0

#: Values ``transcription.provider`` may take (P1 design §B/§D). A value
#: outside this set is a config typo — warn and register nothing so the
#: bot replies with the fallback text instead of crashing (failure mode
#: F#1). Duplicated per plugin on purpose: bundled plugins are
#: self-contained (same convention as plugins/image_gen/*).
KNOWN_TRANSCRIPTION_PROVIDERS = {"groq", "whisper_cpp", "local"}

# Suffix → Content-Type for the multipart file part. Groq sniffs the
# format from the file extension + content type; octet-stream fallback
# still works (server-side magic-byte detection).
_MIME_BY_SUFFIX = {
    ".mp3": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".mpga": "audio/mpeg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}


def _load_transcription_config() -> Dict[str, Any]:
    """Load the ``transcription`` section from user config (config.yaml).

    Returns ``{}`` when config is unreadable or the section is absent —
    register() treats that as "not configured", never as an error.
    """
    try:
        from hermes_cli.config import load_config

        section = (load_config() or {}).get("transcription")
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001 — config problems must not crash plugin load
        return {}


class GroqTranscriptionProvider(TranscriptionProvider):
    """Whisper large-v3 on Groq, via raw REST multipart."""

    def __init__(
        self,
        *,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        model: str = DEFAULT_GROQ_STT_MODEL,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        base_url: str = GROQ_TRANSCRIPTIONS_URL,
    ) -> None:
        self._api_key_env = api_key_env
        self._model = model
        self._timeout = timeout
        self._url = base_url

    # -- ABC surface ---------------------------------------------------------

    @property
    def name(self) -> str:
        # NOT "groq" — that's a built-in name; the registry would silently
        # drop the registration (built-ins always win).
        return "groq_bridge"

    @property
    def display_name(self) -> str:
        return "Groq Whisper (audio bridge)"

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": "whisper-large-v3", "display": "Whisper Large v3"},
            {"id": "whisper-large-v3-turbo", "display": "Whisper Large v3 Turbo"},
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_GROQ_STT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "api",
            "tag": "Whisper large-v3 via Groq REST (audio bridge)",
            "env_vars": [
                {
                    "key": self._api_key_env,
                    "prompt": f"Groq API key (env var {self._api_key_env})",
                    "url": "https://console.groq.com/keys",
                }
            ],
        }

    def is_available(self) -> bool:
        try:
            return bool((get_env_value(self._api_key_env) or "").strip())
        except Exception:  # noqa: BLE001 — must not raise per ABC contract
            return False

    # -- transcription -------------------------------------------------------

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        api_key = (get_env_value(self._api_key_env) or "").strip()
        if not api_key:
            return self._error(
                f"{self._api_key_env} not set — set it in .env (secrets belong "
                f"there, not in config.yaml)"
            )

        path = Path(file_path)
        if not path.is_file():
            return self._error(f"audio file not found: {file_path}")

        model_id = model or self._model
        data: Dict[str, str] = {"model": model_id}
        if language:
            data["language"] = language
        headers = {"Authorization": f"Bearer {api_key}"}

        last_error = "unreachable"
        for attempt in (1, 2):
            # Fresh file bytes per attempt — a retried request must not
            # reuse a consumed file handle.
            files = {
                "file": (
                    path.name,
                    path.read_bytes(),
                    _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream"),
                )
            }
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self._url, headers=headers, files=files, data=data
                    )
            except httpx.TimeoutException as exc:
                return self._error(
                    f"groq request timed out after {self._timeout:g}s: {exc}"
                )
            except httpx.HTTPError as exc:  # connect/read errors — no retry
                return self._error(f"groq request failed: {exc}")

            status = response.status_code
            if 200 <= status < 300:
                try:
                    transcript = str((response.json() or {}).get("text") or "")
                except Exception as exc:  # noqa: BLE001 — malformed JSON body
                    return self._error(f"groq returned unparseable JSON: {exc}")
                return {
                    "success": True,
                    "transcript": transcript,
                    "provider": self.name,
                }

            body = (response.text or "").strip()[:500]
            last_error = f"groq returned HTTP {status}: {body}"
            if status >= 500 and attempt == 1:
                logger.warning(
                    "groq transcription got HTTP %s — retrying once", status
                )
                continue
            return self._error(last_error)

        return self._error(last_error)

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
    """Register the groq backend when ``transcription.provider`` selects it.

    Gating rules (P1 design §D):
    - ``transcription`` section missing → register nothing, stay quiet
      (built-in STT remains available; this is "unconfigured", not broken).
    - ``provider`` is a known value but not ``groq`` → register nothing.
    - ``provider`` is an unknown value → log a warning, register nothing
      (the bot falls back to the F#1 reply text instead of crashing).
    - Config unreadable → register nothing, never raise at plugin load.
    """
    try:
        cfg = _load_transcription_config()
    except Exception:  # noqa: BLE001 — defensive; loader already swallows
        logger.debug(
            "transcription config unreadable — groq plugin not registered",
            exc_info=True,
        )
        return

    selected = str(cfg.get("provider") or "").strip().lower()
    if not selected:
        logger.debug(
            "transcription.provider not set — groq plugin not registered "
            "(built-in STT providers remain available)"
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
    if selected != "groq":
        return

    api_key_env = str(cfg.get("api_key_env") or "").strip() or DEFAULT_API_KEY_ENV
    ctx.register_transcription_provider(GroqTranscriptionProvider(api_key_env=api_key_env))
    logger.info(
        "groq transcription plugin registered as '%s' (API key env var: %s)",
        "groq_bridge",
        api_key_env,
    )

"""ZAI / GLM provider profile.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Hermes' ``reasoning_config = {"enabled": False}``
was previously a silent no-op on this route — the base profile emits nothing,
so users who turned thinking off (desktop toggle, ``/reasoning none``,
``reasoning_effort: none``/``false`` in config.yaml) kept burning thinking
tokens on every turn.

:meth:`ZaiProfile.build_api_kwargs_extras` translates the Hermes reasoning
config into the wire shape Z.AI's OpenAI-compat endpoint expects:

    {"extra_body": {"thinking": {"type": "enabled" | "disabled"}}}

When no reasoning preference is set (``reasoning_config is None``) the field
is omitted so the server default applies, matching prior behavior.  GLM
models before 4.5 (e.g. ``glm-4-9b``) don't accept ``thinking`` and are left
untouched.

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with exactly
two enabled levels — ``high`` and ``max`` — on the OpenAI-compatible endpoint
(per Z.AI / BigModel docs).  Hermes' richer effort scale is collapsed onto
those two so the user's effort preference actually reaches the model instead
of being silently dropped.

The same hook also carries productivity-gate §1.4 session attribution: when
the effective endpoint is the loopback zai-proxy, the request gets an
``X-Hermes-Session: <session id>`` header so the proxy can stamp
``api_calls.session_id`` and token burn becomes attributable to a task/profile.
Loopback-only — real Z.AI endpoints never receive the header.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile

_GLM_VERSION_RE = re.compile(r"^glm-(\d+)(?:\.(\d+))?")

# ---------------------------------------------------------------------------
# Productivity-gate §1.4 — session attribution for the loopback zai-proxy.
#
# Provider-side half of the fix: the local zai-proxy
# (``~/.hermes/bot/zai_proxy.py``) logs ``api_calls.session_id`` from the
# ``X-Hermes-Session`` request header; this hook is what makes the client
# actually send it. Header injection replaces the earlier orphaned version of
# this patch that a fork sync on 2026-09-04 reset away (attribution had been
# live 2026-08-16 → 2026-09-05, then dropped to ~0%).
#
# Loopback-only by design (design doc §7 risk #5): the header is an internal
# attribution channel and must never leave the machine, so it is attached only
# when the *effective* endpoint host is literal loopback. Anything else —
# including the profile's own ``https://api.z.ai/api/paas/v4`` default and any
# relay/aggregator — gets no header. Mirrors the OpenRouter plugin's
# ``x-grok-conv-id`` precedent: a per-request header injected through the
# ``build_api_kwargs_extras`` hook, no core transport change.
# ---------------------------------------------------------------------------
_SESSION_HEADER = "X-Hermes-Session"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _endpoint_is_loopback(base_url: str | None) -> bool:
    """True when ``base_url``'s host is literal loopback (never DNS-resolved).

    A schemeless value (``localhost:9099``) is normalised first so it can't
    slip past the guard, and unparseable input is treated as non-loopback —
    fail closed, never leak the header.
    """
    raw = (base_url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "//" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def _model_supports_thinking(model: str | None) -> bool:
    """GLM thinking-capable model families: glm-4.5 and later (4.5, 4.6, 5…)."""
    m = (model or "").strip().lower()
    match = _GLM_VERSION_RE.match(m)
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    return (major, minor) >= (4, 5)


def _is_glm_5_2(model: str | None) -> bool:
    """Detect GLM-5.2 across the alias spellings providers use.

    Covers the canonical ``glm-5.2`` plus the ``glm-5-2`` / ``glm-5p2``
    variants seen on relays (Fireworks ``glm-5p2``, etc.) and any
    vendor-prefixed form (``z-ai/glm-5.2``, ``zai-org-glm-5-2``).
    """
    m = (model or "").strip().lower()
    if not m:
        return False
    return any(token in m for token in ("glm-5.2", "glm-5-2", "glm-5p2"))


def _glm_5_2_reasoning_effort(reasoning_config: dict | None) -> str | None:
    """Map Hermes reasoning effort onto GLM-5.2's native ``high``/``max``.

    GLM-5.2 only supports two enabled effort levels. ``xhigh``/``max``/``ultra``
    request the top tier; everything else that is enabled requests ``high``
    (its minimum thinking level). When reasoning is explicitly disabled, or
    no effort preference is supplied, the server default is left untouched.
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None

    effort = (reasoning_config.get("effort") or "").strip().lower()
    if not effort or effort == "none":
        return None

    if effort in {"xhigh", "max", "ultra"}:
        return "max"
    # low / medium / minimal / high all clamp to GLM-5.2's minimum: high.
    return "high"


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM — extra_body.thinking on/off + GLM-5.2 reasoning_effort.

    Also carries the productivity-gate §1.4 session-attribution header when —
    and only when — the effective endpoint is the loopback proxy.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        session_id: str | None = None,
        base_url: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Reasoning wiring — model-gated. NOTE: this block must not early-return
        # any more: the session header below applies to every model, including
        # ones that take no ``thinking`` field (glm-4-9b and earlier).
        if _model_supports_thinking(model) or _is_glm_5_2(model):
            # Only emit when the user expressed a preference; omitting the
            # field keeps the server default (enabled) exactly as before.
            if isinstance(reasoning_config, dict):
                enabled = reasoning_config.get("enabled") is not False
                extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}

            if _is_glm_5_2(model):
                effort = _glm_5_2_reasoning_effort(reasoning_config)
                if effort is not None:
                    top_level["reasoning_effort"] = effort

        # Session attribution (§1.4). Resolution: transport-supplied
        # ``session_id`` (``agent.session_id``) first, then the process-wide
        # ``HERMES_SESSION_ID`` export — that fallback covers paths which pass
        # only the endpoint (aux client) and any client that sets only the env
        # var. No session id, or a non-loopback endpoint → header omitted, so
        # an unattributed call is always preferable to a leaked one.
        sid = (session_id or os.environ.get("HERMES_SESSION_ID") or "").strip()
        if sid and _endpoint_is_loopback(base_url or self.base_url):
            headers = dict(top_level.get("extra_headers") or {})
            headers[_SESSION_HEADER] = sid
            top_level["extra_headers"] = headers

        return extra_body, top_level


zai = ZaiProfile(
    name="zai",
    aliases=("glm", "z-ai", "z.ai", "zhipu"),
    env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
    display_name="Z.AI (GLM)",
    description="Z.AI / GLM — Zhipu AI models",
    signup_url="https://z.ai/",
    fallback_models=(
        "glm-5.2",
        "glm-5",
        "glm-4-9b",
    ),
    base_url="https://api.z.ai/api/paas/v4",
    default_aux_model="glm-4.5-flash",
)

register_provider(zai)

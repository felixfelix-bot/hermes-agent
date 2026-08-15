"""ZAI / GLM provider profile."""

from __future__ import annotations

import os

from providers import register_provider
from providers.base import ProviderProfile

# Productivity-gate §1.4: endpoints whose host resolves to loopback get the
# session-attribution header. Real Z.AI endpoints never do — the header is
# loopback-only by design (risk #5 in the design doc: strip/never-send at the
# trust boundary).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ZaiProfile(ProviderProfile):
    """Z.AI / GLM profile with session-id attribution for the local proxy.

    When the effective endpoint is the loopback zai-proxy (e.g.
    ``http://localhost:9099``), attach ``X-Hermes-Session: <session_id>`` so
    the proxy can attribute token burn to the originating agent session
    (``api_calls.session_id``, productivity-gate Phase 1). Mirrors the
    OpenRouter ``x-grok-conv-id`` precedent: a per-request header injected via
    the ``build_api_kwargs_extras`` hook — no core transport change.

    Session id resolution: the transport passes ``session_id`` per request
    (``agent.session_id``); ``HERMES_SESSION_ID`` is a fallback for paths that
    only set the env var. No session id / non-loopback endpoint → no header.
    """

    def build_api_kwargs_extras(
        self, *, session_id: str | None = None, base_url: str | None = None,
        **context
    ) -> tuple[dict, dict]:
        extra_body, top_level = super().build_api_kwargs_extras(
            session_id=session_id, base_url=base_url, **context)

        sid = session_id or os.environ.get("HERMES_SESSION_ID") or ""
        effective = (base_url or self.base_url or "").strip()
        host = ""
        if effective:
            from urllib.parse import urlparse
            host = (urlparse(effective).hostname or "").lower()

        if sid and host in _LOOPBACK_HOSTS:
            top_level = dict(top_level or {})
            headers = dict(top_level.get("extra_headers") or {})
            headers["X-Hermes-Session"] = sid
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

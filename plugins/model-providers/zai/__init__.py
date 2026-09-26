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

:meth:`ZaiProfile.resolve_aux_model` keeps the auxiliary ("cheap") tier on an
id z.ai actually serves, by reading its live ``/models`` catalogue instead of
trusting the hardcoded ``default_aux_model``, which rots (``glm-4.5-flash``
was still pinned here long after z.ai retired it, so every aux task on this
provider spent a round-trip on a 404/429 before the retry net caught it).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Auxiliary ("cheap") model resolution.
#
# ``ProviderProfile.default_aux_model`` is a hardcoded id in source, so it rots:
# z.ai retired ``glm-4.5-flash`` and this profile kept advertising it, so every
# auxiliary task on this provider burned a round-trip on a model the catalogue
# no longer lists (and, behind this fleet's relay, a request that carried zero
# routable candidates). ``resolve_aux_model`` exists precisely so the cheap tier
# tracks the provider's machine-readable recommendation instead of a constant a
# human has to remember to bump — see ``providers/base.py`` for the contract.
#
# Cost model: the resolution runs on client-resolution paths, so the answer is
# memoized per process (6h for an id, 5min for "no answer") and a failure is
# never cached as an answer. The only network work is one authenticated
# ``/models`` GET plus, per candidate, at most one 1-token probe.
# ---------------------------------------------------------------------------
_AUX_MODEL_CACHE_TTL_SECONDS = 6 * 3600.0   # catalogue drift is a release-timescale event
_AUX_MODEL_MISS_TTL_SECONDS = 5 * 60.0      # no answer → retry soon, but don't hammer
_AUX_MODEL_FETCH_TIMEOUT_SECONDS = 4.0
_AUX_MODEL_PROBE_TIMEOUT_SECONDS = 6.0
# How many candidates the 1-token probe may walk past. Bounded so a host whose
# whole cheap tier is withdrawn pays two requests, not the catalogue's length.
_AUX_MODEL_PROBE_LIMIT = 2

# Non-chat siblings of a chat model satisfy a naive "flash" match the same way
# they satisfy OpenRouter's family rungs: a provider names its speech, image,
# embedding and rerank endpoints after the chat model they are paired with.
_AUX_MODEL_EXCLUDE = (
    "embed", "-tts", "transcribe", "-image", "audio", "-vl", "vision", "rerank",
)

# z.ai's DEFINITIVE "this id is not served" markers, live-verified 2026-09-23:
# an unserved id answers HTTP 400 ``{"code":"1211","message":"Unknown Model,
# please check the model code."}``, while a LISTED model whose account has no
# package answers HTTP 429 ``{"code":"1113","message":"Insufficient balance or
# no resource package..."}``. The second is a billing state, not a retirement —
# see :func:`_is_missing_model_response`.
_MISSING_MODEL_MARKERS = (
    "unknown model", "model not found", "model does not exist",
    "invalid model", "no such model",
)

_aux_model_cache: dict[str, tuple[float, str]] = {}
_aux_model_cache_lock = threading.Lock()


def _aux_model_cache_get(key: str) -> str | None:
    """The memoized answer for *key*, or None when there is none to serve."""
    entry = _aux_model_cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        return None
    return value


def _aux_model_cache_put(key: str, value: str) -> None:
    """Memoize *value*, giving a negative answer a much shorter life."""
    ttl = _AUX_MODEL_CACHE_TTL_SECONDS if value else _AUX_MODEL_MISS_TTL_SECONDS
    with _aux_model_cache_lock:
        _aux_model_cache[key] = (time.monotonic() + ttl, value)


def _glm_version_key(model: str) -> tuple[int, int]:
    """Sort key for a GLM id: ``glm-5.3`` → ``(5, 3)``, unknown → ``(0, 0)``.

    Numeric, so the 9-vs-10 cliff a string comparison walks off does not
    decide which generation the cheap tier lands on.
    """
    match = _GLM_VERSION_RE.match((model or "").strip().lower())
    if not match:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2) or 0))


def _aux_flash_candidates(model_ids: Any) -> list[str]:
    """Flash-family chat ids from *model_ids*, newest first.

    Ties on generation go to the plain ``-flash`` id over a variant such as
    ``-flashx``: the bare suffix is the tier z.ai documents as its cheap one.
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in model_ids or ():
        model_id = str(raw or "").strip()
        lowered = model_id.lower()
        if not model_id or lowered in seen:
            continue
        seen.add(lowered)
        if not lowered.startswith("glm-") or "flash" not in lowered:
            continue
        if any(bad in lowered for bad in _AUX_MODEL_EXCLUDE):
            continue
        candidates.append(model_id)
    candidates.sort(
        key=lambda m: (_glm_version_key(m), m.lower().endswith("-flash")),
        reverse=True,
    )
    return candidates


def _is_missing_model_response(status: int, body: str) -> bool:
    """True only for z.ai's DEFINITIVE "this id is gone" answer.

    404/410, or a 400 that carries z.ai's unknown-model code/message. Every
    other failure — 429 overload (code 1305), 429 no-package (code 1113),
    401/403 auth, 5xx, a timeout — is a *state of the moment*, not evidence
    about the catalogue, so the caller must keep the catalogue's pick rather
    than let a probe subtract a model the catalogue says is served.
    """
    if status in (404, 410):
        return True
    if status != 400:
        return False
    lowered = (body or "").lower()
    return '"1211"' in lowered or any(marker in lowered for marker in _MISSING_MODEL_MARKERS)


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
    and only when — the effective endpoint is the loopback proxy, and resolves
    the auxiliary tier from z.ai's live catalogue.
    """

    # ── Auxiliary (cheap) model ──────────────────────────────────────────

    def resolve_aux_model(self, *, vision: bool = False) -> str:
        """Return the newest flash-family id z.ai's live catalogue serves.

        Reads the provider's own ``/models`` (the authority on what z.ai
        serves — see :meth:`_aux_catalog_endpoint`) and returns the newest
        ``-flash`` chat id in it, so the aux tier follows the catalogue
        instead of the ``default_aux_model`` constant. A candidate that z.ai
        itself calls unknown is skipped in favour of the next one.

        Contract (``providers/base.py``): memoized per process, never raises,
        and ``""`` when there is no answer so the caller falls through to
        ``default_aux_model`` / the legacy fallback dict.

        ``vision`` stays unserved: the flash tier has no multimodal member, so
        vision keeps its own resolution path (``default_vision_model`` and
        ``_PROVIDER_VISION_MODELS``) rather than being handed a text-only id.
        """
        if vision:
            return ""
        cached = _aux_model_cache_get("flash")
        if cached is not None:
            return cached
        value = ""
        try:
            value = self._resolve_aux_model_live()
        except Exception:
            logger.debug("zai resolve_aux_model failed", exc_info=True)
            value = ""
        _aux_model_cache_put("flash", value)
        return value

    def _resolve_aux_model_live(self) -> str:
        """One catalogue fetch + bounded probing; ``""`` when nothing answers."""
        endpoint = self._aux_catalog_endpoint()
        if not endpoint:
            return ""
        for api_key in self._aux_api_keys():
            model_ids = self._fetch_catalog(api_key, endpoint)
            if not model_ids:
                continue
            candidates = _aux_flash_candidates(model_ids)
            if not candidates:
                return ""
            return self._first_candidate_served(candidates, api_key, endpoint)
        return ""

    def _aux_catalog_endpoint(self) -> str:
        """Endpoint whose model list is authoritative for this provider.

        A configured ``base_url`` wins when it is a real endpoint, but a
        LOOPBACK one is skipped in favour of the provider's own: a local relay
        publishes its own static subset of the catalogue (this fleet's proxy
        still advertises the retired ``glm-4.5-flash``, and omits the current
        ``glm-5.3-flash`` entirely), and trusting that list is how the aux tier
        rotted in the first place.
        """
        configured = ""
        try:
            from hermes_cli.auth import resolve_api_key_provider_credentials

            creds = resolve_api_key_provider_credentials(self.name) or {}
            configured = str(creds.get("base_url") or "").strip()
        except Exception:
            logger.debug("zai credential lookup failed", exc_info=True)
        if configured and not _endpoint_is_loopback(configured):
            return configured.rstrip("/")
        return (self.models_url or self.base_url or "").rstrip("/")

    def _aux_api_keys(self) -> list[str]:
        """Candidate credentials for the catalogue, best first, deduped.

        The profile's own credential resolution comes first, then the env vars
        this profile declares. An empty entry (anonymous fetch) is the last
        resort, so a catalogue that needs no key still resolves when nothing is
        configured yet — and on a host whose only key is a relay token, the 401
        from each candidate simply leaves the aux tier on its curated default.
        """
        keys: list[str] = []
        try:
            from hermes_cli.auth import resolve_api_key_provider_credentials

            creds = resolve_api_key_provider_credentials(self.name) or {}
            resolved = str(creds.get("api_key") or "").strip()
            if resolved:
                keys.append(resolved)
        except Exception:
            logger.debug("zai credential lookup failed", exc_info=True)
        for var in self.env_vars:
            value = (os.environ.get(var) or "").strip()
            if value:
                keys.append(value)
        deduped = list(dict.fromkeys(keys))
        deduped.append("")
        return deduped

    def _fetch_catalog(self, api_key: str, endpoint: str) -> list[str]:
        """Live model ids from *endpoint*, or ``[]`` — never raises."""
        try:
            return list(
                self.fetch_models(
                    api_key=api_key or None,
                    base_url=endpoint or None,
                    timeout=_AUX_MODEL_FETCH_TIMEOUT_SECONDS,
                )
                or ()
            )
        except Exception:
            logger.debug("zai aux catalogue fetch failed", exc_info=True)
            return []

    def _first_candidate_served(
        self, candidates: list[str], api_key: str, endpoint: str
    ) -> str:
        """First candidate the 1-token probe does not call definitively gone.

        Probing is advisory by design: z.ai advertises ids it then refuses, but
        it also refuses ids it still serves (a 429 no-package answer on a listed
        model), so only a definitive "unknown model" reply subtracts a
        candidate. When every probed candidate is gone we return ``""`` and let
        the caller fall through to the curated default instead of pinning the
        aux tier to an id the provider has withdrawn.
        """
        for candidate in candidates[:_AUX_MODEL_PROBE_LIMIT]:
            if self._probe_serves(candidate, api_key, endpoint) is not False:
                return candidate
        return ""

    def _probe_serves(self, model: str, api_key: str, endpoint: str) -> bool | None:
        """One-token liveness probe: True served, False definitively gone, None unknown."""
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
            }
        ).encode()
        request = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions", data=payload, method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", _profile_user_agent())
        if api_key:
            request.add_header("Authorization", f"Bearer {api_key}")
        try:
            from hermes_cli.urllib_security import open_credentialed_url

            with open_credentialed_url(
                request, timeout=_AUX_MODEL_PROBE_TIMEOUT_SECONDS
            ) as resp:
                resp.read()
            return True
        except Exception as exc:
            status = getattr(exc, "code", None)
            body = ""
            try:
                body = exc.read().decode(errors="replace")[:400]
            except Exception:
                body = ""
            if isinstance(status, int) and _is_missing_model_response(status, body):
                logger.debug("zai aux probe: %s is not served", model)
                return False
            logger.debug("zai aux probe for %s inconclusive: %r", model, exc)
            return None

    # ── Request shaping ──────────────────────────────────────────────────

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
    # Belt to :meth:`ZaiProfile.resolve_aux_model`'s braces: the resolver reads
    # z.ai's live catalogue, and this constant is only what a caller falls back
    # to when that lookup has no answer (offline, relay token, no key). It is
    # therefore pinned to a served id — the previous value, ``glm-4.5-flash``,
    # was retired upstream and made every fall-through aux call a guaranteed
    # 404/503. Keep it in step with ``_API_KEY_PROVIDER_AUX_MODELS_FALLBACK``
    # in ``agent/auxiliary_client.py``.
    default_aux_model="glm-5.3-flash",
)

register_provider(zai)

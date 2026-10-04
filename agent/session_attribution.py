"""Loopback-only session attribution for every provider path.

The live router (``~/.hermes/bot/zai_proxy.py``) stamps ``api_calls.session_id``
from the ``X-Hermes-Session`` request header — that is how token burn is
attributed to a session, profile, or task. Rows written without it are invisible
to per-session accounting and to the productivity gate.

Measured on the fleet box 2026-10-04: the emission existed in exactly ONE
provider profile (``zai``), while the fleet runs ``deepseek/deepseek-flash``.
The result was ~100 % of inference (~$5.5/h, 586 calls/h, avg 83k-token
prompts, 97 % cached) reaching the proxy with ``session_id`` NULL.

This module is the single source of truth so that *any* provider aimed at a
loopback endpoint emits the header, whatever profile it resolves to.

Loopback-only by design (design doc §7 risk #5): the header is an internal
attribution channel and must never leave the machine. A schemeless value is
normalised so it cannot slip past the guard, and unparseable input counts as
non-loopback — fail closed. An unattributed call is always preferable to a
leaked one.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional
from urllib.parse import urlparse

HERMES_SESSION_HEADER = "X-Hermes-Session"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def endpoint_is_loopback(base_url: Optional[str]) -> bool:
    """True when ``base_url``'s host is literal loopback (never DNS-resolved)."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = "//" + raw
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def current_session_id() -> str:
    """The context-local session id, or ``""`` when no session is bound.

    Read from the auxiliary client's runtime contextvar (``set_runtime_main``,
    refreshed per turn by ``agent.turn_context``). Deliberately NOT the
    process-wide ``HERMES_SESSION_ID`` export: a gateway process serves many
    sessions at once, so a whole-process value is either absent or belongs to
    whichever session exported it last — which is how auxiliary traffic became
    unattributable in the first place.
    """
    try:
        from agent.auxiliary_client import _runtime_main_value

        return str(_runtime_main_value("session_id") or "").strip()
    except Exception:
        return ""


def session_attribution_headers(
    headers: Optional[Mapping[str, Any]] = None,
    *,
    base_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Merge ``X-Hermes-Session`` onto *headers*, loopback-only.

    Returns a new dict; never mutates the input. Idempotent — an existing header
    wins, so repeated merges (profile hook, then transport) are safe. Pass
    ``session_id`` to override the ambient context value; pass an empty string
    to suppress the header entirely.
    """
    merged: dict = dict(headers or {})
    if merged.get(HERMES_SESSION_HEADER):
        return merged
    if not endpoint_is_loopback(base_url):
        return merged
    sid = current_session_id() if session_id is None else str(session_id or "")
    sid = sid.strip()
    if sid:
        merged[HERMES_SESSION_HEADER] = sid
    return merged


def apply_session_attribution(
    kwargs: dict,
    *,
    base_url: Optional[str] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Merge the attribution header into ``kwargs["extra_headers"]``.

    Deliberately per-request ``extra_headers`` and not client-level
    ``default_headers``: the session id changes per turn, so baking it into a
    long-lived client would pin the first session's id and misattribute every
    later one. No-op when nothing is configured.
    """
    merged = session_attribution_headers(
        kwargs.get("extra_headers"), base_url=base_url, session_id=session_id
    )
    if merged:
        kwargs["extra_headers"] = merged
    return kwargs

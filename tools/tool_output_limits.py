"""Configurable tool-output truncation limits.

Ported from anomalyco/opencode PR #23770 (``feat(truncate): allow
configuring tool output truncation limits``).

OpenCode hardcoded ``MAX_LINES = 2000`` and ``MAX_BYTES = 50 * 1024``
as tool-output truncation thresholds. Hermes-agent had the same
hardcoded constants in two places:

* ``tools/terminal_tool.py`` — ``MAX_OUTPUT_CHARS = 50000`` (terminal
  stdout/stderr cap)
* ``tools/file_operations.py`` — ``MAX_LINES = 2000`` /
  ``MAX_LINE_LENGTH = 2000`` (read_file pagination cap + per-line cap)

This module centralises those values behind a single config section
(``tool_output`` in ``config.yaml``) so power users can tune them
without patching the source. The existing hardcoded numbers remain as
defaults, so behaviour is unchanged when the config key is absent.

Example ``config.yaml``::

    tool_output:
      max_bytes: 100000        # terminal output cap (chars)
      max_lines: 5000          # read_file pagination + truncation cap
      max_line_length: 2000    # per-line length cap before '... [truncated]'

The limits reader is defensive: any error (missing config file, invalid
value type, etc.) falls back to the built-in defaults so tools never
fail because of a malformed config.
"""

from __future__ import annotations

from typing import Any, Dict

# Hardcoded defaults — these match the pre-existing values, so adding
# this module is behaviour-preserving for users who don't set
# ``tool_output`` in config.yaml.
DEFAULT_MAX_BYTES = 50_000       # terminal_tool.MAX_OUTPUT_CHARS
DEFAULT_MAX_LINES = 2000         # file_operations.MAX_LINES
DEFAULT_MAX_LINE_LENGTH = 2000   # file_operations.MAX_LINE_LENGTH

# Module-level cache — populated on first call.
# Avoids repeated config file I/O on every tool call.
_cached_limits: dict | None = None


def _coerce_positive_int(value: Any, default: int) -> int:
    """Return ``value`` as a positive int, or ``default`` on any issue."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return default
    if iv <= 0:
        return default
    return iv


def get_tool_output_limits() -> Dict[str, int]:
    """Return resolved tool-output limits, reading ``tool_output`` from config.

    Keys: ``max_bytes``, ``max_lines``, ``max_line_length``. Missing or
    invalid entries fall through to the ``DEFAULT_*`` constants. This
    function NEVER raises.

    Result is cached for the process lifetime to avoid repeated disk I/O
    on every tool call. Call ``_reset_tool_output_limits_cache()`` in
    tests that need a fresh read after config changes.
    """
    global _cached_limits
    if _cached_limits is not None:
        return _cached_limits
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("tool_output") if isinstance(cfg, dict) else None
        if not isinstance(section, dict):
            section = {}
    except Exception:
        section = {}

    _cached_limits = {
        "max_bytes": _coerce_positive_int(section.get("max_bytes"), DEFAULT_MAX_BYTES),
        "max_lines": _coerce_positive_int(section.get("max_lines"), DEFAULT_MAX_LINES),
        "max_line_length": _coerce_positive_int(
            section.get("max_line_length"), DEFAULT_MAX_LINE_LENGTH
        ),
    }
    return _cached_limits


def _reset_tool_output_limits_cache() -> None:
    """Reset the cached limits — for tests or after config hot-reload."""
    global _cached_limits
    _cached_limits = None


def get_max_bytes() -> int:
    """Shortcut for terminal-tool callers that only need the byte cap."""
    return get_tool_output_limits()["max_bytes"]


def get_max_lines() -> int:
    """Shortcut for file-ops callers that only need the line cap."""
    return get_tool_output_limits()["max_lines"]


def get_max_line_length() -> int:
    """Shortcut for file-ops callers that only need the per-line cap."""
    return get_tool_output_limits()["max_line_length"]


def cap_json_output(
    payload: str,
    *,
    max_chars: int | None = None,
    list_fields: tuple[str, ...] = ("results", "messages", "sessions", "jobs"),
    string_fields: tuple[str, ...] = (),
    truncation_message: str | None = None,
) -> str:
    """Bound a JSON tool response to ``tool_output.max_bytes``.

    Shared helper for tool families whose serialized responses can exceed the
    centralized ``tool_output`` cap (cron list, delegate results, skill
    dumps).  When the payload fits, it is returned unchanged (fast path is a
    length check).  When it exceeds the cap, entries are dropped from the tail
    of the largest list field named in ``list_fields`` (or the largest string
    field named in ``string_fields`` is tail-truncated) until the serialized
    response fits, and the truncation is marked in-band (``truncated`` +
    ``truncated_count``) so the model knows to narrow its request.

    Defensive: any parse or shrink failure returns the original payload
    unchanged, so a malformed or non-list-shaped response is never corrupted.
    """
    import json as _json

    try:
        cap = max_chars if max_chars is not None else get_max_bytes()
        if len(payload) <= cap:
            return payload
        resp = _json.loads(payload)
        if not isinstance(resp, dict):
            return payload

        # Prefer shrinking the largest list field (drop tail entries), then
        # fall back to tail-truncating the largest string field.
        biggest_key, biggest_len = None, 0
        for key in list_fields:
            val = resp.get(key)
            if isinstance(val, list) and len(val) > biggest_len:
                biggest_key, biggest_len = key, len(val)
        if biggest_key is not None:
            while resp[biggest_key] and len(_json.dumps(resp, ensure_ascii=False)) > cap:
                resp[biggest_key].pop()
            dropped = biggest_len - len(resp[biggest_key])
        else:
            biggest_key, biggest_len = None, 0
            for key in string_fields:
                val = resp.get(key)
                if isinstance(val, str) and len(val) > biggest_len:
                    biggest_key, biggest_len = key, len(val)
            if biggest_key is None:
                return payload
            original = resp[biggest_key]
            # Binary-search the largest prefix that fits under the cap.
            lo, hi = 0, len(original)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                resp[biggest_key] = original[:mid] + "\n… [truncated]"
                if len(_json.dumps(resp, ensure_ascii=False)) <= cap:
                    lo = mid
                else:
                    hi = mid - 1
            resp[biggest_key] = original[:lo] + "\n… [truncated]"
            dropped = biggest_len - lo

        resp["truncated"] = True
        resp["truncated_count"] = dropped
        if truncation_message is not None:
            resp["message"] = truncation_message.format(
                cap=cap, dropped=dropped, field=biggest_key
            )
        return _json.dumps(resp, ensure_ascii=False)
    except Exception:
        return payload

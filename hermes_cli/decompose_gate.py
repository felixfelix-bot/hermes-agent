"""decompose_gate.py — free, deterministic pre-gate for the kanban decomposer.

The gateway auto-decomposer (``gateway/kanban_watchers.py::_auto_decompose_tick``
-> ``hermes_cli.kanban_decompose.decompose_task``) calls an auxiliary LLM for
every card in the ``triage`` column, up to ``auto_decompose_per_tick`` per ~60 s
dispatcher tick. "Does it make sense?" was delegated entirely to the model and
nothing marked a card as already handled, so cards the model declined — or that
had already been specified/decomposed — were re-sent to the aux LLM on every
tick. On the fleet this produced thousands of cards (3,751 created; 1,687 still
open) and burned aux-LLM quota; see the orchestration repo's
``docs/PLAN-card-hygiene-and-decomposer-reform.md``.

``should_decompose()`` is the free gate that must pass before any LLM call. A
card is worth a decomposer call only when it is in ``triage``, is not already
specified/decomposed (no ``specified``/``decomposed`` event, no children), and
carries no explicit ``no-decompose`` marker.
"""
from __future__ import annotations

import re

NO_DECOMPOSE_RE = re.compile(
    r"(?i)\b(?:no[-_ ]?decompose|do[-_ ]?not[-_ ]?decompose|"
    r"single[-_ ]?task|keep[-_ ]?as[-_ ]?one)\b"
)


def has_no_decompose_marker(title: str, body: str = "") -> bool:
    """True when the card explicitly opts out of decomposition."""
    return bool(NO_DECOMPOSE_RE.search(f"{title or ''} {body or ''}"))


def should_decompose(
    *,
    status: str = "triage",
    has_children: bool = False,
    specified: bool = False,
    decomposed: bool = False,
    title: str = "",
    body: str = "",
) -> "tuple[bool, str]":
    """Return ``(eligible, reason)``. ``eligible`` gates a decomposer LLM call."""
    if status != "triage":
        return False, f"task is not in triage (status={status!r})"
    if has_children or specified or decomposed:
        return False, "already specified/decomposed"
    if has_no_decompose_marker(title, body):
        return False, "no-decompose marker"
    return True, "eligible"

"""WS-B — refuse spatial children delegated to a lane with no spatial candidate.

``delegate_task`` pins every child to ``delegation.model`` (the manager profile
uses ``tier/delegation``). That pool is market-priced general work, and its
first candidate ``hermes/workhorse`` carries **operator-directed placeholder**
spatial/vision axes while dispatching to a text-only DeepSeek model. A naive
"some candidate declares spatial" check therefore passes while the child is
blind — exactly the 2026-10-17 defect.

This guard:
  * triggers only on an **explicit** spatial declaration in the delegated
    goal/context (``spatial_tag: <non-none>`` or ``visual_artifact: <path>``);
  * counts a candidate only when its benchmark row declares every configured
    floor axis AND the id is a real model, not a fleet-internal alias
    (``hermes/*``). An alias's advertised axes describe the alias, not the
    served model: ``hermes/workhorse`` advertises spatial/vision "basic" while
    its lane dispatches DeepSeek (F6 read-back says served family deepseek), so
    it is exactly the poisoning the gate must ignore. Operator-directed
    placeholder axes on a REAL id (e.g. ``gpt-6-astra``) still count — that is
    the fleet's designated spatial lane;
  * fails closed on an unknown lane or unreadable config;
  * refuses BEFORE any child is spawned (no wasted spend) and appends a JSONL
    record (goal hash only — never the prompt) so the refusal volume can be
    measured before per-task routing exists.

The operator directive is "forbid + alert first": there is no per-task spatial
routing yet (WS-B stage 2), so the refusal message routes the caller to
``tier/spatial-worker`` / a concrete spatial model instead.

A guard *bug* must not brick all delegation: the call site logs and proceeds on
an unexpected exception. Unknown/missing capability data is a deliberate
refusal (fail closed), not a crash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is a repo dependency
    yaml = None

logger = logging.getLogger(__name__)

_SPATIAL_TAG = re.compile(
    r"^[ \t>*+\-#]*spatial_tag[ \t]*[:=][ \t]*(\S+)", re.I | re.M)
_VISUAL_ARTIFACT = re.compile(
    r"^[ \t>*+\-#]*visual_artifact[ \t]*[:=][ \t]*(\S+)", re.I | re.M)
_NONE_VALUES = {"none", "n/a", "na", "false", "off", "no", "-"}
_DEFAULT_FLOOR = {"spatial": "basic", "vision": "basic"}
_LEVELS = {"": 0, "none": 0, "basic": 1, "good": 2, "strong": 3,
           "excellent": 4, "advanced": 4}
DEFAULT_LOG = Path.home() / ".hermes" / "state" / "delegation-spatial-guard.jsonl"


def declares_spatial(text: str | None) -> bool:
    """True when the text carries a real (non-opt-out) spatial declaration."""
    text = text or ""
    for m in _SPATIAL_TAG.finditer(text):
        if m.group(1).strip().strip("\"'").lower() not in _NONE_VALUES:
            return True
    m = _VISUAL_ARTIFACT.search(text)
    if m and m.group(1).strip().strip("\"'").lower() not in _NONE_VALUES:
        return True
    return False


def _bot_dir() -> Path:
    return Path(os.environ.get("HERMES_BOT_DIR") or (Path.home() / ".hermes" / "bot"))


def _yaml(path: Path) -> dict:
    if yaml is None:
        return {}
    try:
        d = yaml.safe_load(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_capabilities() -> tuple[dict, dict, dict]:
    """(tiers, benchmark rows, floor axes) or ({}, {}, floor) when unreadable."""
    bot = _bot_dir()
    tiers = _yaml(bot / "model_tiers.yaml").get("tiers") or {}
    models = _yaml(bot / "model_benchmarks.yaml").get("models") or {}
    policy = _yaml(bot / "capability_routing.yaml")
    floor = _DEFAULT_FLOOR
    try:
        axes = ((policy.get("classes") or {}).get("spatial_required") or {}).get("axes")
        if isinstance(axes, dict) and axes:
            floor = {str(k): str(v) for k, v in axes.items()}
    except Exception:
        pass
    if not isinstance(tiers, dict):
        tiers = {}
    if not isinstance(models, dict):
        models = {}
    return tiers, models, floor


def _is_alias_id(model_id: str) -> bool:
    """Fleet-internal routing aliases never satisfy a capability floor."""
    return str(model_id or "").strip().lower().startswith("hermes/")


def _meets_floor(model_id: str, rec: dict, floor: dict) -> bool:
    if _is_alias_id(model_id):
        return False
    if not isinstance(rec, dict) or not rec:
        return False
    for axis, minimum in floor.items():
        declared = _LEVELS.get(str(rec.get(axis) or "").strip().lower())
        need = _LEVELS.get(str(minimum or "").strip().lower(), 1)
        if declared is None or declared < need:
            return False
    return True


def lane_has_spatial(ref: str) -> bool | None:
    """True/False for a resolvable lane; None when the lane is unknown.

    A ``tier/*`` reference is True when ANY candidate is a real floor-meeting
    claim (the router may serve any of them). A concrete id is checked directly;
    an id absent from the benchmark file is unknown (None), not a pass.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    tiers, models, floor = load_capabilities()
    if not tiers and not models:
        return None
    if ref.startswith("tier/"):
        cands = (tiers.get(ref) or {}).get("candidates")
        if not isinstance(cands, list) or not cands:
            return None
        return any(_meets_floor(str(m), models.get(str(m)) or {}, floor) for m in cands)
    rec = models.get(ref)
    if not isinstance(rec, dict):
        return None
    return _meets_floor(ref, rec, floor)


def _log_refusal(record: dict) -> None:
    path = Path(os.environ.get("HERMES_DELEGATION_GUARD_LOG") or DEFAULT_LOG)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:  # telemetry must never break delegation
        logger.debug("delegate spatial guard: could not write %s", path, exc_info=True)


def guard(task_list: list[dict], cfg: dict,
          parent_model: str = "") -> str | None:
    """Return a refusal message when a spatial child rides a non-spatial lane.

    Checks every task in a batch; the first offending task is reported (each is
    logged). Returns None when the delegation may proceed.
    """
    cfg = cfg or {}
    ref = str(cfg.get("model") or "").strip() or (parent_model or "").strip()
    for i, task in enumerate(task_list or []):
        if not isinstance(task, dict):
            continue
        text = "\n".join(str(task.get(k) or "") for k in ("goal", "context"))
        if not declares_spatial(text):
            continue
        has = lane_has_spatial(ref)
        if has is True:
            continue
        if has is None:
            why = (f"lane {ref or '<inherited>'} capability is UNKNOWN "
                   "(config missing or the lane/model is not declared)")
        else:
            why = f"lane {ref!r} has no spatial-capable candidate"
        goal = str(task.get("goal") or "")
        _log_refusal({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "refused",
            "task_index": i,
            "ref": ref,
            "reason": "unknown" if has is None else "no-spatial-candidate",
            "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "goal_chars": len(goal),
            "pid": os.getpid(),
            "profile": os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_PROFILE_NAME") or "",
        })
        logger.warning("delegate_task refused spatial child %d on %s (%s)", i, ref, why)
        floor = _DEFAULT_FLOOR
        try:
            floor = load_capabilities()[2]
        except Exception:
            pass
        axes = ", ".join(f"{k}>={v}" for k, v in floor.items())
        return (
            f"delegate_task refused (WS-B): task {i} is declared spatial, but "
            f"{why} (floor {axes}; fleet-internal aliases do not count). "
            "delegate_task cannot select a model per task, so do the spatial "
            "work in a spatial-capable lane: assign it to a worker on "
            "tier/spatial-worker (e.g. worker-pcb/worker-layout) or run it in "
            "a spatial parent, then retry without the spatial child. Refusals "
            "are recorded in ~/.hermes/state/delegation-spatial-guard.jsonl to "
            "measure the volume before per-task routing exists."
        )
    return None

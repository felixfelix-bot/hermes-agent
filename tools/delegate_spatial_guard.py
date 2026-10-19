"""WS-B — refuse spatial children delegated to a lane with no spatial candidate.

``delegate_task`` pins every child to ``delegation.model`` (the manager profile
uses ``tier/delegation``). That pool is market-priced general work, and its
first candidate ``hermes/workhorse`` carries **operator-directed placeholder**
spatial/vision axes while dispatching to a text-only DeepSeek model (the F6
read-back reports served family deepseek). A naive "some candidate declares
spatial" check therefore passes while the child is blind.

The guard:
  * triggers only on an **explicit** spatial declaration in the delegated
    goal/context — a line-anchored ``spatial_tag: <non-none>`` or
    ``visual_artifact: <path>`` (quoted values allowed; ``none``-values,
    empty values and ``#`` comments never count, and EVERY occurrence is
    checked so a later real declaration cannot be masked by an earlier
    opt-out);
  * counts a candidate only when its benchmark row declares every configured
    floor axis AND the id is a real model, not a routing alias: fleet-internal
    aliases (``hermes/*``, the documented alias namespace) never satisfy a
    capability floor, nor does a row whose ``reported_as`` names a different
    id — an alias's advertised axes describe the alias, not the served model
    (``hermes/workhorse`` is exactly this poisoning). Operator-directed
    placeholder axes on a REAL id (``gpt-6-astra``) still count: that is the
    fleet's designated spatial lane;
  * fails closed on an unknown lane, unreadable or malformed capability data
    (malformed entries are skipped, never raise);
  * refuses BEFORE any child is spawned (no wasted spend) and appends a JSONL
    record (goal hash only — never the prompt) for every refused task so the
    volume can be measured before per-task routing exists.

The operator directive is "forbid + alert first": there is no per-task spatial
routing yet, so the refusal message routes the caller to the spatial worker
lane instead. A guard *bug* must not brick ordinary delegation — but it must
not silently authorize a declared-spatial child either: an internal guard
error refuses declared-spatial tasks and allows the rest.
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

# Line-anchored declaration syntax: optional markdown decoration, the key, then
# the value — a double- or single-QUOTED string (may contain spaces) or a bare
# non-empty token. Values that are comments or opt-outs never trigger the guard.
_DECLARATION = re.compile(
    r"^[ \t>*+\-#]*(?:spatial_tag|visual_artifact)[ \t]*[:=][ \t]*"
    r"(?:\"([^\"\n]*)\"|'([^'\n]*)'|(\S+))", re.I | re.M)
_NONE_VALUES = {"none", "n/a", "na", "false", "off", "no", "-",
                "<path>", "<none>", "<value>"}
_DEFAULT_FLOOR = {"spatial": "basic", "vision": "basic"}
_LEVELS = {"": 0, "none": 0, "basic": 1, "good": 2, "strong": 3,
           "excellent": 4, "advanced": 4}
DEFAULT_LOG = Path.home() / ".hermes" / "state" / "delegation-spatial-guard.jsonl"


def _clean(value: str) -> str:
    v = (value or "").strip().strip("\"'").strip().lower()
    if v.startswith("#"):  # a comment token is not a declaration
        return ""
    return v


def declares_spatial(text: str | None) -> bool:
    """True when the text carries a real (non-opt-out) spatial declaration.

    EVERY occurrence is checked, so an earlier opt-out cannot mask a later
    real declaration. A quoted value keeps its inner whitespace (a quoted
    empty string is not a declaration).
    """
    for m in _DECLARATION.finditer(text or ""):
        raw = next(g for g in m.groups() if g is not None)
        v = _clean(raw)
        if v and v not in _NONE_VALUES:
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
    """(tiers, benchmark rows, floor axes); invalid entries are dropped.

    Shape-invalid tiers/models/candidates are skipped, never raised — a
    malformed config degrades to "unknown lane", which fails closed.
    """
    bot = _bot_dir()
    raw_tiers = _yaml(bot / "model_tiers.yaml").get("tiers") or {}
    raw_models = _yaml(bot / "model_benchmarks.yaml").get("models") or {}
    tiers = {}
    for name, spec in (raw_tiers.items() if isinstance(raw_tiers, dict) else []):
        if not isinstance(spec, dict):
            continue
        cands = spec.get("candidates")
        if isinstance(cands, list) and cands:
            tiers[str(name)] = {"candidates": [str(m) for m in cands]}
    models = {str(k): v for k, v in
              (raw_models.items() if isinstance(raw_models, dict) else [])
              if isinstance(v, dict) and v}
    floor = _policy_floor(bot / "capability_routing.yaml")
    return tiers, models, floor


# A PRESENT but invalid capability policy must not silently become a weaker
# default floor: nothing satisfies this sentinel axis (no benchmark row ever
# declares it at level "basic"), so a malformed policy refuses
# declared-spatial delegation (fail closed). An ABSENT policy file is a
# legitimate optional component -> default floor.
_UNSATISFIABLE = {"__malformed_policy__": "basic"}


def _policy_floor(path: Path) -> dict:
    if not path.is_file():
        return _DEFAULT_FLOOR
    doc = _yaml(path)
    if not doc:
        return _UNSATISFIABLE  # present but unreadable/invalid YAML
    classes = doc.get("classes")
    if not isinstance(classes, dict):
        return _UNSATISFIABLE
    cls = classes.get("spatial_required")
    if not isinstance(cls, dict):
        return _UNSATISFIABLE
    axes = cls.get("axes")
    if not isinstance(axes, dict) or not axes:
        return _UNSATISFIABLE
    floor = {}
    for axis, minimum in axes.items():
        if not str(axis).strip():
            return _UNSATISFIABLE
        level = str(minimum or "").strip().lower()
        if level not in _LEVELS:
            return _UNSATISFIABLE  # unknown minimum can never be a weaker floor
        floor[str(axis)] = level
    return floor


def _is_alias(model_id: str, rec: dict) -> bool:
    """A routing alias's axes describe the alias, not the served model.

    Fleet-internal aliases live in the ``hermes/`` namespace (the documented
    GENERAL_ALIAS namespace), and a row whose ``reported_as`` names a
    different id is a reported alias, not the id itself.
    """
    mid = str(model_id or "").strip().lower()
    if mid.startswith("hermes/"):
        return True
    rep = rec.get("reported_as") if isinstance(rec, dict) else None
    if isinstance(rep, str):
        rep = [rep]
    if isinstance(rep, Iterable):
        return any(str(x).strip().lower() and str(x).strip().lower() != mid
                   for x in rep)
    return False


def _meets_floor(model_id: str, rec: dict, floor: dict) -> bool:
    if _is_alias(model_id, rec):
        return False
    if not isinstance(rec, dict) or not rec:
        return False
    for axis, minimum in floor.items():
        declared = _LEVELS.get(str(rec.get(axis) or "").strip().lower())
        # An UNKNOWN configured minimum is not a weaker floor: nothing
        # satisfies it, so a malformed policy fails closed (refuses).
        need = _LEVELS.get(str(minimum or "").strip().lower())
        if need is None or declared is None or declared < need:
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
        if not cands:
            return None
        return any(_meets_floor(m, models.get(m) or {}, floor) for m in cands)
    rec = models.get(ref)
    if not isinstance(rec, dict):
        return None
    return _meets_floor(ref, rec, floor)


def _log_path() -> Path:
    return Path(os.environ.get("HERMES_DELEGATION_GUARD_LOG") or DEFAULT_LOG)


def _log_refusal(record: dict) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        logger.warning("delegate spatial guard: could not record a refusal "
                       "to %s", path, exc_info=True)


def _safe_meta(value: str, cap: int = 120) -> str:
    return str(value or "").strip()[:cap]


def _refusal_message(task_indices: list[int], ref: str, why: str, axes: str,
                     log_hint: str) -> str:
    if len(task_indices) == 1:
        which = f"task {task_indices[0]}"
    else:
        which = "tasks " + ", ".join(str(i) for i in task_indices)
    return (
        f"delegate_task refused (WS-B): {which} declare(s) spatial intent, but "
        f"{why} (floor {axes}; fleet-internal aliases do not count). "
        "delegate_task cannot select a model per task, so do the spatial work "
        "in a spatial-capable lane: assign it to a worker on tier/spatial-worker "
        "(e.g. worker-pcb/worker-layout) or run it in a spatial parent, then "
        f"retry without the spatial child. Refusals are recorded in {log_hint} "
        "to measure the volume before per-task routing exists."
    )


def _guard_inner(task_list: list, cfg: dict, parent_model: str) -> str | None:
    ref = str(cfg.get("model") or "").strip() or (parent_model or "").strip()
    log_path = _log_path()
    log_hint = str(log_path) if os.environ.get("HERMES_DELEGATION_GUARD_LOG") \
        else "~/.hermes/state/delegation-spatial-guard.jsonl"
    unknown = ref and lane_has_spatial(ref) is None
    refusals: list[int] = []
    for i, task in enumerate(task_list or []):
        if not isinstance(task, dict):
            continue
        text = "\n".join(str(task.get(k) or "") for k in ("goal", "context"))
        if not declares_spatial(text):
            continue
        if lane_has_spatial(ref) is True:
            continue
        goal = str(task.get("goal") or "")
        _log_refusal({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "refused",
            "task_index": i,
            "ref": _safe_meta(ref),
            "reason": "unknown" if unknown else "no-spatial-candidate",
            "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "goal_chars": len(goal),
            "pid": os.getpid(),
            "profile": _safe_meta(os.environ.get("HERMES_PROFILE")
                                  or os.environ.get("HERMES_PROFILE_NAME")),
        })
        logger.warning("delegate_task refused spatial child %d on %r",
                       i, ref or "<inherited>")
        refusals.append(i)
    if not refusals:
        return None
    if unknown:
        why = (f"lane {ref!r} capability is UNKNOWN (config missing or the "
               "lane/model is not declared)")
        floor = _DEFAULT_FLOOR
    else:
        why = f"lane {ref!r} has no spatial-capable candidate"
        try:
            floor = load_capabilities()[2]
        except Exception:
            floor = _DEFAULT_FLOOR
    axes = ", ".join(f"{k}>={v}" for k, v in floor.items())
    return _refusal_message(refusals, ref or "<inherited>", why, axes, log_hint)


def guard(task_list: list, cfg: dict, parent_model: str = "") -> str | None:
    """Return a refusal message when a spatial child rides a non-spatial lane.

    Checks every task in a batch; ALL refused tasks are logged and named in the
    message (the whole batch is refused before any spawn). Returns None when
    the delegation may proceed.

    Never raises. On an internal error, a task that DECLARES spatial intent is
    still refused (fail closed for spatial); everything else proceeds so a
    guard bug cannot brick ordinary delegation.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return _guard_inner(task_list, cfg, parent_model or "")
    except Exception:
        logger.exception("delegate spatial guard failed; failing closed for "
                         "declared-spatial tasks only")
        ref = _safe_meta(str(cfg.get("model") or "").strip()
                         or parent_model or "<inherited>")
        declared: list[int] = []
        for i, task in enumerate(task_list or []):
            if not isinstance(task, dict):
                continue
            goal = str(task.get("goal") or "")
            text = "\n".join(str(task.get(k) or "") for k in ("goal", "context"))
            if declares_spatial(text):
                declared.append(i)
                _log_refusal({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "event": "refused",
                    "task_index": i,
                    "ref": ref,
                    "reason": "guard-internal-error",
                    "goal_sha256": hashlib.sha256(
                        goal.encode("utf-8")).hexdigest(),
                    "goal_chars": len(goal),
                    "pid": os.getpid(),
                    "profile": _safe_meta(os.environ.get("HERMES_PROFILE")
                                          or os.environ.get("HERMES_PROFILE_NAME")),
                })
        if not declared:
            return None
        return _refusal_message(
            declared, ref,
            "lane capability could not be verified (guard internal error)",
            ", ".join(f"{k}>={v}" for k, v in _DEFAULT_FLOOR.items()),
            str(_log_path()))
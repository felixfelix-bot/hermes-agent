"""Fleet-aware placement gate for the embedded gateway dispatcher.

Config-as-code: reads the shared placement policy
(``~/.hermes/bot/placement_policy.json``, rendered by role 59-dispatch-health)
and the peer health files, then answers the one question the dispatcher must
ask before spawning a batch worker locally:

    "Is the node with the most *predicted* headroom someone else?"

When it is, the gateway host holds local dispatch (target 0) and the work is
placed by the cross-node fleet queue instead. This keeps the machine that runs
the Signal gateway from being clobbered by batch work while a peer idles
(2026-10-12 incident: six PCB autorouting workers on the 2-core gateway host,
which then fell off the FIPS mesh, while an 8-core peer sat idle).

Fail-open everywhere: a missing policy, missing peer telemetry, or an import
error leaves dispatch unchanged. The gate is coarse (node-level, not task-fit);
the fleet queue still does precise per-task fit and keeps its ``fallback_s``
window so a starved card is eventually claimed by any fitting node.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_log = logging.getLogger("gateway.fleet_placement")

# Fleet ops data lives under ~/.hermes/bot regardless of the active profile:
# the gateway repoints HERMES_HOME at a profile dir at runtime, so HERMES_HOME
# cannot be trusted for fleet co-ordination files.
FLEET_BOT = Path(os.environ.get(
    "HERMES_FLEET_BOT", str(Path.home() / ".hermes" / "bot")))
FLEET_SCRIPTS = FLEET_BOT.parent / "scripts"
POLICY_PATH = FLEET_BOT / "placement_policy.json"

SCOPE = "kanban_worker"


def _load_placement_module():
    """Import the fleet's placement_policy.py from the scripts dir."""
    for d in (str(FLEET_SCRIPTS),):
        if d not in sys.path:
            sys.path.insert(0, d)
    import placement_policy  # type: ignore
    return placement_policy


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return {}


def _peer_healths() -> list[dict]:
    """Peer health dicts, deduped by node preferring ``*.health.json``."""
    best: dict[str, dict] = {}
    peers = FLEET_BOT / "peers"
    if not peers.is_dir():
        return []
    for f in sorted(peers.glob("*.json")):
        if f.name.endswith(".ownership.json") or f.name.startswith("events-"):
            continue
        h = _read_json(f)
        if not isinstance(h, dict) or not h.get("node"):
            continue
        if h["node"] not in best or f.name.endswith(".health.json"):
            best[h["node"]] = h
    return list(best.values())


def select_peer() -> str | None:
    """Return the peer that should take batch work, or None.

    None means "this node may dispatch locally" — either it is not a gateway
    host, or no eligible peer clears the headroom floor (safe fallback).
    """
    try:
        pp = _load_placement_module()
        policy = pp.load_policy(POLICY_PATH)
        me = _read_json(FLEET_BOT / "fleet_health.json")
        node = me.get("node") or os.environ.get("FLEET_NODE")
        if not pp.is_gateway_host(node, policy):
            return None
        return pp.select_node(_peer_healths(), policy, scope=SCOPE)
    except Exception as exc:  # noqa: BLE001
        _log.warning("fleet-placement: gate error, failing open: %r", exc)
        return None


def local_dispatch_allowed() -> bool:
    """True if the dispatcher may spawn batch workers on THIS node."""
    peer = select_peer()
    _log.info("fleet-placement: peer=%s local_dispatch_allowed=%s",
              peer, peer is None)
    return peer is None

"""
Nostr NIP-29 platform adapter.

Connects to strfry relays running the strfry29 plugin for NIP-29 group chat.
Listens for kind 9 (text) events tagged with ``h=<group>`` and routes them to
the Hermes gateway. Outgoing messages are published as signed kind 9 events.

Requires: websockets, coincurve (both in the Hermes venv).
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Nostr crypto helpers (BIP-340 Schnorr via coincurve)
# ---------------------------------------------------------------------------

def _bech32_decode(s: str) -> bytes:
    """Decode a bech32 string (nsec/npub) to raw payload bytes."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    if not s or any(c not in CHARSET + "1" for c in s.lower()):
        return bytes.fromhex(s)  # assume raw hex
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError(f"invalid bech32: {s}")
    hrp = s[:pos]
    data = s[pos + 1:]
    decoded = []
    for c in data:
        decoded.append(CHARSET.index(c))
    # Drop last 6 checksum chars
    decoded = decoded[:-6]
    # 5-bit to 8-bit
    result = bytearray()
    buffer = 0
    bits = 0
    for val in decoded:
        buffer = (buffer << 5) | val
        bits += 5
        if bits >= 8:
            result.append((buffer >> (bits - 8)) & 0xFF)
            bits -= 8
    return bytes(result)


def _privkey_from_nsec(nsec_str: str) -> bytes:
    """Convert nsec bech32 or hex string to 32-byte private key."""
    nsec_str = nsec_str.strip()
    if nsec_str.startswith("nsec"):
        return _bech32_decode(nsec_str)
    return bytes.fromhex(nsec_str)


def _pubkey_from_privkey(privkey: bytes) -> str:
    """Derive x-only pubkey (32 bytes hex) from private key."""
    try:
        from coincurve import PrivateKey
        pk = PrivateKey(privkey)
        return pk.public_key.format(compressed=True)[1:33].hex()
    except ImportError:
        raise RuntimeError("coincurve not installed — required for Nostr signing")


def _compute_event_id(pubkey: str, created_at: int, kind: int,
                      tags: List, content: str) -> str:
    """Compute Nostr event ID = sha256 of canonical JSON array."""
    canonical = json.dumps([0, pubkey, created_at, kind, tags, content],
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _sign_event_id(privkey: bytes, event_id: str) -> str:
    """Sign a 32-byte event ID with BIP-340 Schnorr."""
    from coincurve import PrivateKey
    pk = PrivateKey(privkey)
    sig = pk.sign_schnorr(bytes.fromhex(event_id))
    return sig.hex()


def _build_event(privkey: bytes, kind: int, tags: List, content: str) -> Dict:
    """Build, sign, and return a Nostr event dict."""
    pubkey = _pubkey_from_privkey(privkey)
    created_at = int(time.time())
    event_id = _compute_event_id(pubkey, created_at, kind, tags, content)
    sig = _sign_event_id(privkey, event_id)
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------

def check_nostr_requirements() -> bool:
    """Check if Nostr adapter is configured."""
    nsec_path = os.getenv("NOSTR_NSEC_PATH", "")
    relays = os.getenv("NOSTR_RELAYS", "")
    return bool(nsec_path and relays)


# ---------------------------------------------------------------------------
# Nostr Adapter
# ---------------------------------------------------------------------------

class NostrAdapter(BasePlatformAdapter):
    """Nostr NIP-29 group chat adapter.

    Connects to strfry relays, subscribes to kind 9 events for configured
    groups, and routes them to the Hermes gateway. Outgoing messages are
    published as signed kind 9 events.
    """

    platform = Platform.NOSTR

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.NOSTR)

        extra = config.extra or {}
        self.relays: List[str] = extra.get("relays", [])
        if not self.relays:
            relays_env = os.getenv("NOSTR_RELAYS", "")
            self.relays = [r.strip() for r in relays_env.split(",") if r.strip()]

        self.groups: List[str] = extra.get("groups", [])
        if not self.groups:
            groups_env = os.getenv("NOSTR_GROUPS", "")
            self.groups = [g.strip() for g in groups_env.split(",") if g.strip()]

        # Load group mappings from nip29-groups.yaml if available
        self.group_mappings: Dict[str, str] = {}  # signal_chat_id -> nostr_group_id
        groups_config_path = extra.get("groups_config_path",
                                        os.path.expanduser("~/.hermes/profiles/manager/state/nip29-groups.yaml"))
        if os.path.exists(groups_config_path):
            try:
                import yaml
                with open(groups_config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                for mapping in cfg.get("groups", []):
                    signal_id = mapping.get("signal_chat_id", "")
                    nostr_id = mapping.get("nostr_group_id", "")
                    if signal_id and nostr_id:
                        self.group_mappings[signal_id] = nostr_id
                        if nostr_id not in self.groups:
                            self.groups.append(nostr_id)
                logger.info("Nostr: loaded %d group mappings from %s",
                            len(self.group_mappings), groups_config_path)
            except Exception as e:
                logger.warning("Nostr: failed to load group config from %s: %s",
                               groups_config_path, e)

        self.nsec_path: str = extra.get("nsec_path", os.getenv("NOSTR_NSEC_PATH", ""))
        self._privkey: Optional[bytes] = None
        self._pubkey: Optional[str] = None

        # WebSocket connections: relay_url -> websockets.WebSocketClientProtocol
        self._ws: Dict[str, Any] = {}
        self._listener_tasks: List[asyncio.Task] = []
        self._reconnect_task: Optional[asyncio.Task] = None
        self._running = False
        self._sub_id = f"hermes-{int(time.time())}"

        # Dedup: track seen event IDs to avoid processing duplicates
        self._seen_ids: Set[str] = set()
        self._max_seen = 5000

        logger.info("Nostr adapter initialized: relays=%s groups=%s",
                     self.relays, self.groups)

    def _load_key(self) -> bool:
        """Load private key from nsec file."""
        if not self.nsec_path or not os.path.exists(self.nsec_path):
            logger.error("Nostr: nsec file not found at %s", self.nsec_path)
            return False
        try:
            with open(self.nsec_path) as f:
                nsec_str = f.read().strip()
            self._privkey = _privkey_from_nsec(nsec_str)
            self._pubkey = _pubkey_from_privkey(self._privkey)
            logger.info("Nostr: loaded keypair, pubkey=%s", self._pubkey[:16] + "...")
            return True
        except Exception as e:
            logger.error("Nostr: failed to load nsec: %s", e)
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to all configured relays and start listening."""
        if not self.relays or not self.groups:
            logger.error("Nostr: relays and groups are required")
            return False

        if not self._load_key():
            return False

        try:
            import websockets
        except ImportError:
            logger.error("Nostr: websockets not installed")
            return False

        # Connect to each relay
        connected = 0
        for relay_url in self.relays:
            try:
                ws = await asyncio.wait_for(
                    websockets.connect(relay_url, ping_interval=30, ping_timeout=10),
                    timeout=10,
                )
                self._ws[relay_url] = ws
                connected += 1
                logger.info("Nostr: connected to %s", relay_url)

                # Send REQ subscription for NIP-29 event kinds with h tag filters
                # kinds: 9 (text), 10 (reaction), 11 (chat thread), 39000-39003 (metadata/admins/members/roles), 9000-9022 (moderation)
                nip29_kinds = [9, 10, 11, 39000, 39001, 39002, 39003, 9000, 9001, 9002, 9004, 9005, 9006, 9007, 9008]
                req = json.dumps([
                    "REQ",
                    self._sub_id,
                    {"kinds": nip29_kinds, "#h": self.groups},
                ])
                await ws.send(req)
                logger.info("Nostr: subscribed to kind 9 on %s for groups %s",
                            relay_url, self.groups)
            except Exception as e:
                logger.warning("Nostr: failed to connect to %s: %s", relay_url, e)

        if connected == 0:
            logger.error("Nostr: could not connect to any relay")
            return False

        self._running = True

        # Start listener tasks for each connected relay
        for relay_url in list(self._ws.keys()):
            task = asyncio.create_task(self._relay_listener(relay_url))
            self._listener_tasks.append(task)

        # Start reconnection watcher
        self._reconnect_task = asyncio.create_task(self._reconnect_watcher())

        logger.info("Nostr: connected to %d/%d relays", connected, len(self.relays))
        return True

    async def disconnect(self) -> None:
        """Disconnect from all relays and clean up."""
        self._running = False

        for task in self._listener_tasks:
            task.cancel()
        self._listener_tasks.clear()

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        for relay_url, ws in list(self._ws.items()):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws.clear()

        logger.info("Nostr: disconnected")

    @property
    def is_connected(self) -> bool:
        return self._running and bool(self._ws)

    # ------------------------------------------------------------------
    # Relay listener
    # ------------------------------------------------------------------

    async def _relay_listener(self, relay_url: str):
        """Listen for events from a single relay."""
        ws = self._ws.get(relay_url)
        if not ws:
            return

        try:
            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw_msg)
                    await self._handle_relay_message(msg, relay_url)
                except json.JSONDecodeError:
                    logger.warning("Nostr: invalid JSON from %s", relay_url)
                except Exception as e:
                    logger.warning("Nostr: error handling message from %s: %s",
                                   relay_url, e)
        except Exception as e:
            if self._running:
                logger.warning("Nostr: listener for %s disconnected: %s", relay_url, e)
                # Remove dead connection
                self._ws.pop(relay_url, None)

    async def _handle_relay_message(self, msg: list, relay_url: str):
        """Handle a message from the relay."""
        if not isinstance(msg, list) or len(msg) < 2:
            return

        msg_type = msg[0]

        if msg_type == "EVENT":
            # ["EVENT", sub_id, event_json]
            if len(msg) < 3:
                return
            event = msg[2]
            await self._process_event(event, relay_url)

        elif msg_type == "EOSE":
            # End of stored events — normal for REQ
            logger.debug("Nostr: EOSE from %s", relay_url)

        elif msg_type == "NOTICE":
            notice = msg[1] if len(msg) > 1 else ""
            logger.info("Nostr: NOTICE from %s: %s", relay_url, notice)

    async def _process_event(self, event: dict, relay_url: str):
        """Process a NIP-29 group event (kind 9 chat, or metadata/moderation events)."""
        event_id = event.get("id", "")
        if not event_id or event_id in self._seen_ids:
            return

        # Track for dedup
        self._seen_ids.add(event_id)
        if len(self._seen_ids) > self._max_seen:
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen:])

        kind = event.get("kind")

        # Extract group from h tag
        tags = event.get("tags", [])
        group = None
        for tag in tags:
            if len(tag) >= 2 and tag[0] == "h":
                group = tag[1]
                break

        if not group or group not in self.groups:
            return

        # Skip our own messages (published by this adapter)
        if event.get("pubkey") == self._pubkey:
            return

        content = event.get("content", "")

        # Handle kind 9 (text chat) — route to gateway
        if kind == 9:
            if not content:
                return

            pubkey = event.get("pubkey", "unknown")
            created_at = event.get("created_at", int(time.time()))

            logger.info("Nostr: incoming chat from %s in group %s: %s",
                         pubkey[:16], group, content[:80])

            # Build SessionSource
            source = SessionSource(
                platform=Platform.NOSTR,
                chat_id=group,
                chat_name=group,
                chat_type="group",
                user_id=pubkey,
                user_name=f"npub{pubkey[:12]}",
            )

            # Build MessageEvent
            msg_event = MessageEvent(
                text=content,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=event,
                message_id=event_id,
            )

            # Route to gateway
            await self.handle_message(msg_event)

            # Also inject via self-delivery for cross-platform bridge
            self._inject_self_delivery(group, content, pubkey, event_id)

        # Handle metadata/moderation events — log but don't route as messages
        elif kind in (39000, 39001, 39002, 39003):
            logger.info("Nostr: metadata event kind=%d in group %s", kind, group)
        elif kind in (9000, 9001, 9002, 9004, 9005, 9006, 9007, 9008):
            logger.debug("Nostr: moderation event kind=%d in group %s", kind, group)
        elif kind in (10, 11):
            logger.debug("Nostr: reaction/thread event kind=%d in group %s", kind, group)

    def _inject_self_delivery(self, group: str, content: str,
                               pubkey: str, event_id: str):
        """Inject message into /tmp/hermes-self-delivery/ for cross-platform bridging.

        This allows messages received on Nostr to be forwarded to Signal groups
        that are mapped to the same NIP-29 group.
        """
        delivery_dir = "/tmp/hermes-self-delivery"
        try:
            os.makedirs(delivery_dir, exist_ok=True)
            # Find Signal chat_id for this nostr group
            for signal_id, nostr_id in self.group_mappings.items():
                if nostr_id == group:
                    import json as _json
                    delivery = {
                        "platform": "signal",
                        "chat_id": signal_id,
                        "text": f"[Nostr/{group}] {content}",
                        "source": f"nostr:{pubkey[:12]}",
                        "event_id": event_id,
                    }
                    delivery_file = os.path.join(delivery_dir, f"nostr-{event_id[:16]}.json")
                    with open(delivery_file, "w") as f:
                        _json.dump(delivery, f)
                    logger.debug("Nostr: injected self-delivery for Signal %s", signal_id)
                    break
        except Exception as e:
            logger.warning("Nostr: self-delivery injection failed: %s", e)

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect_watcher(self):
        """Periodically check and reconnect to dropped relays with exponential backoff."""
        import websockets

        backoff = {}  # relay_url -> current backoff seconds
        initial_backoff = 1
        max_backoff = 300  # 5 minutes max

        while self._running:
            await asyncio.sleep(5)  # Check every 5 seconds

            for relay_url in self.relays:
                if relay_url in self._ws:
                    continue  # Already connected

                if not self._running:
                    break

                current_backoff = backoff.get(relay_url, initial_backoff)
                logger.info("Nostr: attempting reconnect to %s (backoff=%ds)",
                            relay_url, current_backoff)
                try:
                    ws = await asyncio.wait_for(
                        websockets.connect(relay_url, ping_interval=30, ping_timeout=10),
                        timeout=10,
                    )
                    self._ws[relay_url] = ws
                    backoff[relay_url] = initial_backoff  # Reset backoff on success

                    # Re-subscribe with broad NIP-29 kinds
                    nip29_kinds = [9, 10, 11, 39000, 39001, 39002, 39003, 9000, 9001, 9002, 9004, 9005, 9006, 9007, 9008]
                    req = json.dumps([
                        "REQ",
                        self._sub_id,
                        {"kinds": nip29_kinds, "#h": self.groups},
                    ])
                    await ws.send(req)

                    # Start listener
                    task = asyncio.create_task(self._relay_listener(relay_url))
                    self._listener_tasks.append(task)

                    logger.info("Nostr: reconnected to %s", relay_url)
                except Exception as e:
                    # Exponential backoff
                    backoff[relay_url] = min(current_backoff * 2, max_backoff)
                    logger.warning("Nostr: reconnect to %s failed (backoff=%ds): %s",
                                   relay_url, backoff[relay_url], e)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send a message by publishing a kind 9 event to all relays."""
        if not self._privkey:
            return SendResult(success=False, error="Nostr: no private key loaded")

        if not self._ws:
            return SendResult(success=False, error="Nostr: no relay connections")

        # Build and sign the event
        tags = [["h", chat_id]]
        event = _build_event(self._privkey, 9, tags, content)

        # Publish to all connected relays
        published = 0
        errors = []
        for relay_url, ws in list(self._ws.items()):
            try:
                msg = json.dumps(["EVENT", event])
                await ws.send(msg)
                published += 1
            except Exception as e:
                errors.append(f"{relay_url}: {e}")
                logger.warning("Nostr: failed to send to %s: %s", relay_url, e)

        if published == 0:
            return SendResult(
                success=False,
                error=f"Nostr: failed to publish to any relay: {'; '.join(errors)}",
                retryable=True,
            )

        logger.info("Nostr: sent message to group %s (%d/%d relays): %s",
                     chat_id, published, len(self.relays), content[:80])

        return SendResult(
            success=True,
            message_id=event["id"],
            raw_response={"published": published, "total": len(self.relays)},
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if at least one relay is reachable."""
        if not self._running:
            return False
        return bool(self._ws)

    # ------------------------------------------------------------------
    # Chat info (required by BasePlatformAdapter)
    # ------------------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """Return basic info about a Nostr group."""
        if chat_id not in self.groups:
            return None
        return {
            "id": chat_id,
            "name": chat_id,
            "type": "group",
            "members": [],  # NIP-29 membership is relay-side
        }
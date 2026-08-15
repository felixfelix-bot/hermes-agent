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
    """Compute Nostr event ID = sha256 of canonical JSON array.

    NIP-01 canonical form is compact JSON over the raw UTF-8 text:
    ensure_ascii MUST be False, otherwise non-ASCII content (emoji,
    etc.) is escaped as \\uXXXX before hashing and relays reject the
    event with "invalid event id".
    """
    canonical = json.dumps([0, pubkey, created_at, kind, tags, content],
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

        # Pending publish confirmations: event_id -> {relay_url: Future[(ok, reason)]}
        # Relays acknowledge every EVENT with ["OK", event_id, accepted, reason]
        # (NIP-01). The frames arrive on the listener task; the OK handler in
        # _handle_relay_message resolves the matching future so send() can
        # await relay acknowledgement instead of fire-and-forget.
        self._pending_oks: Dict[str, Dict[str, asyncio.Future]] = {}
        self._ok_timeout: float = float(extra.get("ok_timeout", 10.0))

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

    async def _do_nip42_auth(self, ws, relay_url: str) -> bool:
        """Perform NIP-42 AUTH challenge-response if the relay requests it.

        Some relays (e.g. Buzz) require NIP-42 authentication before accepting
        REQ subscriptions or EVENT publishes. The relay sends an AUTH challenge
        immediately on WebSocket connection. This reads the first message,
        responds with a signed kind 22242 event if needed, then subscribes.

        Returns True if auth succeeded or was not needed, False on failure.
        """
        # Read the first message — could be AUTH challenge or EOSE/EVENT.
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            # Relay said nothing on connect: no AUTH challenge required.
            # Treat as unauthenticated relay and subscribe directly.
            logger.debug("Nostr: %s sent no AUTH challenge — subscribing without auth",
                         relay_url)
            await ws.send(json.dumps([
                "REQ",
                self._sub_id,
                {"kinds": [9], "#h": self.groups},
            ]))
            return True
        except Exception as e:
            logger.warning("Nostr: connection to %s failed during auth probe: %s",
                           relay_url, e)
            return False

        msg_type = msg[0] if isinstance(msg, list) and msg else ""

        if msg_type != "AUTH":
            # No auth required — relay responded with EOSE/EVENT/etc.
            logger.debug("Nostr: %s does not require NIP-42 auth (got %s)",
                         relay_url, msg_type)
            # Process the initial message if it's an event
            if msg_type in ("EVENT", "EOSE"):
                await self._handle_relay_message(msg, relay_url)
            # Now send REQ subscription
            req = json.dumps([
                "REQ",
                self._sub_id,
                {"kinds": [9], "#h": self.groups},
            ])
            await ws.send(req)
            return True

        # We got an AUTH challenge
        challenge = msg[1] if len(msg) > 1 else ""
        if not challenge:
            logger.error("Nostr: AUTH challenge from %s has no challenge string",
                         relay_url)
            return False

        if not self._privkey:
            logger.error("Nostr: cannot respond to AUTH challenge from %s — no private key loaded",
                         relay_url)
            return False

        logger.info("Nostr: received NIP-42 AUTH challenge from %s", relay_url)

        # Build kind 22242 auth event
        auth_tags = [
            ["relay", relay_url],
            ["challenge", challenge],
        ]
        auth_event = _build_event(self._privkey, 22242, auth_tags, "")

        # Send AUTH response
        await ws.send(json.dumps(["AUTH", auth_event]))

        # Drain messages until we get OK for our AUTH event
        # (relay may have queued NOTICE/CLOSED from before auth)
        got_ok = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                drain_msg = json.loads(raw)
                drain_type = drain_msg[0] if isinstance(drain_msg, list) else ""
                if drain_type == "OK" and len(drain_msg) >= 3 and drain_msg[2] is True:
                    logger.info("Nostr: NIP-42 auth successful for %s", relay_url)
                    got_ok = True
                    break
                elif drain_type in ("NOTICE", "CLOSED"):
                    logger.debug("Nostr: draining pre-auth %s from %s: %s",
                                 drain_type, relay_url, str(drain_msg)[:100])
                else:
                    logger.debug("Nostr: draining unexpected msg from %s: %s",
                                 relay_url, str(drain_msg)[:100])
            except asyncio.TimeoutError:
                break

        if not got_ok:
            logger.warning("Nostr: no OK confirmation for NIP-42 auth on %s",
                           relay_url)

        # Now send REQ subscription — we're authenticated
        req = json.dumps([
            "REQ",
            self._sub_id,
            {"kinds": [9], "#h": self.groups},
        ])
        await ws.send(req)

        return True

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
                logger.info("Nostr: connected to %s", relay_url)

                # Perform NIP-42 auth if required, then subscribe
                authenticated = await self._do_nip42_auth(ws, relay_url)
                if not authenticated:
                    logger.warning("Nostr: NIP-42 auth failed for %s", relay_url)
                    await ws.close()
                    self._ws.pop(relay_url, None)
                    continue

                self._ws[relay_url] = ws
                connected += 1
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
                self._fail_pending_oks(
                    relay_url, RuntimeError(f"relay connection lost: {e}"))

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

        elif msg_type == "OK":
            # ["OK", event_id, accepted, message] — relay acknowledgement for
            # a published EVENT (or AUTH) frame. Correlate with pending sends;
            # log unsolicited rejections (e.g. AUTH events) for visibility.
            if len(msg) < 3:
                return
            event_id = msg[1] if isinstance(msg[1], str) else ""
            accepted = bool(msg[2])
            reason = msg[3] if len(msg) > 3 else ""
            waiters = self._pending_oks.get(event_id)
            fut = None
            if waiters is not None:
                fut = waiters.pop(relay_url, None)
                if not waiters:
                    self._pending_oks.pop(event_id, None)
            if fut is not None and not fut.done():
                fut.set_result((accepted, reason))
            elif not accepted:
                logger.warning("Nostr: %s rejected event %s: %s",
                               relay_url, str(event_id)[:12], reason)
            else:
                logger.debug("Nostr: %s accepted event %s (no pending send)",
                             relay_url, str(event_id)[:12])

        elif msg_type == "AUTH":
            # Relay requests auth during the session (e.g. after publishing)
            challenge = msg[1] if len(msg) > 1 else ""
            if challenge and self._privkey:
                logger.info("Nostr: AUTH challenge during session from %s",
                            relay_url)
                auth_tags = [
                    ["relay", relay_url],
                    ["challenge", challenge],
                ]
                auth_event = _build_event(self._privkey, 22242, auth_tags, "")
                ws = self._ws.get(relay_url)
                if ws:
                    try:
                        await ws.send(json.dumps(["AUTH", auth_event]))
                    except Exception as e:
                        logger.warning("Nostr: failed to send AUTH to %s: %s",
                                       relay_url, e)

    async def _process_event(self, event: dict, relay_url: str):
        """Process a kind 9 chat event."""
        event_id = event.get("id", "")
        if not event_id or event_id in self._seen_ids:
            return

        # Track for dedup
        self._seen_ids.add(event_id)
        if len(self._seen_ids) > self._max_seen:
            # Trim oldest (set is unordered but this is good enough for dedup)
            self._seen_ids = set(list(self._seen_ids)[-self._max_seen:])

        kind = event.get("kind")
        if kind != 9:
            return

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
        if not content:
            return

        pubkey = event.get("pubkey", "unknown")
        created_at = event.get("created_at", int(time.time()))

        logger.info("Nostr: incoming message from %s in group %s: %s",
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

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------

    async def _reconnect_watcher(self):
        """Periodically check and reconnect to dropped relays."""
        import websockets

        while self._running:
            await asyncio.sleep(30)  # Check every 30 seconds

            for relay_url in self.relays:
                if relay_url in self._ws:
                    continue  # Already connected

                if not self._running:
                    break

                logger.info("Nostr: attempting reconnect to %s", relay_url)
                try:
                    ws = await asyncio.wait_for(
                        websockets.connect(relay_url, ping_interval=30, ping_timeout=10),
                        timeout=10,
                    )

                    # Re-auth and re-subscribe
                    authenticated = await self._do_nip42_auth(ws, relay_url)
                    if not authenticated:
                        logger.warning("Nostr: NIP-42 auth failed on reconnect to %s",
                                       relay_url)
                        await ws.close()
                        self._ws.pop(relay_url, None)
                        continue

                    self._ws[relay_url] = ws

                    # Start listener
                    task = asyncio.create_task(self._relay_listener(relay_url))
                    self._listener_tasks.append(task)

                    logger.info("Nostr: reconnected to %s", relay_url)
                except Exception as e:
                    logger.warning("Nostr: reconnect to %s failed: %s", relay_url, e)

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, content: str,
                   reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Send a message by publishing a kind 9 event to all relays.

        Each EVENT is acknowledged by the relay with an OK message (NIP-01).
        We wait for that acknowledgement: success is reported only when at
        least one relay accepted the event. Rejections (e.g. NIP-42
        auth-required) are logged with the relay's reason and surfaced in
        the SendResult instead of being silently dropped.
        """
        if not self._privkey:
            return SendResult(success=False, error="Nostr: no private key loaded")

        if not self._ws:
            return SendResult(success=False, error="Nostr: no relay connections",
                              retryable=True)

        # Build and sign the event
        tags = [["h", chat_id]]
        event = _build_event(self._privkey, 9, tags, content)
        msg = json.dumps(["EVENT", event])

        # Publish to all connected relays, awaiting each relay's OK
        outcomes: Dict[str, Dict[str, Any]] = {}
        for relay_url, ws in list(self._ws.items()):
            accepted, reason = await self._publish_to_relay(
                relay_url, ws, event, msg)
            outcomes[relay_url] = {"ok": accepted, "reason": reason}

        published = [url for url, res in outcomes.items() if res["ok"]]
        failures = {url: res["reason"] for url, res in outcomes.items()
                    if not res["ok"]}

        if not published:
            return SendResult(
                success=False,
                error=f"Nostr: no relay accepted event: {failures}",
                retryable=True,
                raw_response={"outcomes": outcomes},
            )

        logger.info("Nostr: sent message to group %s (%d/%d relays accepted): %s",
                    chat_id, len(published), len(outcomes), content[:80])

        return SendResult(
            success=True,
            message_id=event["id"],
            raw_response={"accepted_by": published, "outcomes": outcomes},
        )

    async def _publish_to_relay(self, relay_url: str, ws,
                                event: Dict[str, Any], msg: str) -> tuple:
        """Publish one EVENT frame to one relay and await its OK.

        Returns (accepted, reason). On auth-required rejections the publish
        is retried once — by the time we see the rejection, the listener's
        AUTH handler has already answered the relay's challenge. Suspect
        connections (send failure, no acknowledgement) are dropped so the
        reconnect watcher can re-establish them.
        """
        try:
            accepted, reason = await self._publish_and_wait(relay_url, ws, event, msg)
        except asyncio.TimeoutError:
            logger.warning(
                "Nostr: %s did not acknowledge event %s within %.1fs — dropping connection",
                relay_url, str(event["id"])[:12], self._ok_timeout)
            await self._drop_relay(relay_url, ws)
            return (False, f"timeout: no OK from {relay_url}")
        except Exception as e:
            logger.warning("Nostr: failed to send to %s: %s", relay_url, e)
            await self._drop_relay(relay_url, ws)
            return (False, f"{relay_url}: {e}")

        if not accepted and "auth" in str(reason).lower():
            logger.warning(
                "Nostr: %s rejected event %s (%s) — re-authenticating and retrying once",
                relay_url, str(event["id"])[:12], reason)
            await asyncio.sleep(0.5)  # let the AUTH response settle
            try:
                accepted, reason = await self._publish_and_wait(relay_url, ws, event, msg)
            except asyncio.TimeoutError:
                logger.warning(
                    "Nostr: %s did not acknowledge retried event %s — dropping connection",
                    relay_url, str(event["id"])[:12])
                await self._drop_relay(relay_url, ws)
                return (False, f"timeout on auth retry: {relay_url}")
            except Exception as e:
                logger.warning("Nostr: auth retry to %s failed: %s", relay_url, e)
                await self._drop_relay(relay_url, ws)
                return (False, f"{relay_url} auth retry: {e}")

        if not accepted:
            logger.error("Nostr: %s rejected event %s: %s",
                         relay_url, str(event["id"])[:12], reason)
        return (accepted, reason)

    async def _publish_and_wait(self, relay_url: str, ws,
                                event: Dict[str, Any], msg: str) -> tuple:
        """Send an EVENT frame and wait for the relay's OK acknowledgement.

        Registers a future in _pending_oks keyed by (event_id, relay_url);
        the OK handler on the listener task resolves it. Returns
        (accepted, reason); raises asyncio.TimeoutError if the relay stays
        silent past ok_timeout.
        """
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        event_id = event["id"]
        waiters = self._pending_oks.setdefault(event_id, {})
        waiters[relay_url] = fut
        try:
            await ws.send(msg)
            return await asyncio.wait_for(fut, self._ok_timeout)
        finally:
            waiters.pop(relay_url, None)
            if not waiters:
                self._pending_oks.pop(event_id, None)

    async def _drop_relay(self, relay_url: str, ws) -> None:
        """Close a suspect connection; the reconnect watcher re-establishes it."""
        try:
            await ws.close()
        except Exception:
            pass
        if self._ws.get(relay_url) is ws:
            self._ws.pop(relay_url, None)
        self._fail_pending_oks(relay_url, RuntimeError("connection dropped"))

    def _fail_pending_oks(self, relay_url: str, exc: Exception) -> None:
        """Fail all pending publish confirmations for a relay."""
        for event_id, waiters in list(self._pending_oks.items()):
            fut = waiters.pop(relay_url, None)
            if fut is not None and not fut.done():
                fut.set_exception(exc)
            if not waiters:
                self._pending_oks.pop(event_id, None)

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
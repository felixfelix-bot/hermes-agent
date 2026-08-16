"""
Nostr NIP-29 platform adapter.

Connects to strfry relays running the strfry29 plugin for NIP-29 group chat.
Listens for kind 9 (text) events tagged with ``h=<group>`` and routes them to
the Hermes gateway. Outgoing messages are published as signed kind 9 events.

Requires: websockets, coincurve (both in the Hermes venv).
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

import httpx

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
# Audio-ingest bridge (Blossom media, design AB-2)
# ---------------------------------------------------------------------------

# Exact reply copy for the soft failure modes (design §F).
_F1_NO_PROVIDER = (
    "I received an audio clip but can't transcribe it yet — tell Felix to "
    "enable transcription."
)
_F2_TRANSCRIBE_FAILED = "couldn't transcribe that, try again"
_F5_FETCH_AUTH = (
    "I couldn't fetch that audio clip (authentication failed) — my relay "
    "membership may have lapsed; tell Felix."
)
_F6_MISSING = "that audio clip has expired"
_LONG_CLIP_WARNING = "(warning: long voice note)"

_BLOSSOM_GET_KIND = 24242
_BLOSSOM_AUTH_TTL_SECONDS = 300
_DEFAULT_AUDIO_MAX_BYTES = 26_214_400  # 25 MB
_DEFAULT_AUDIO_MAX_SECONDS = 300.0     # hard cap (design F#4)
_DEFAULT_AUDIO_SOFT_SECONDS = 600.0    # soft warn (dormant until the hard
                                       # cap is configured above it)

# Audio extensions the bridge is allowed to fetch (matches the relay's
# /media/<sha256>.<ext> route).
_AUDIO_EXTS = frozenset({"mp3", "m4a", "ogg", "opus", "wav", "flac", "aac"})

# Bare-URL fallback: an absolute or relative /media/<64-hex>.<ext> reference.
# The sha256 is embedded in the path, which is what makes the BUD-11
# x-scoped auth token possible — so nothing else is detected.
_AUDIO_MEDIA_URL_RE = re.compile(
    r"(?:(https?)://([^/\s\"'<>]+))?/media/([0-9a-f]{64})"
    r"\.(mp3|m4a|ogg|opus|wav|flac|aac)\b",
    re.IGNORECASE,
)

# NIP-92 imeta keys we understand (superset check keeps parsing tolerant).
_IMETA_KEYS = frozenset({
    "url", "m", "x", "size", "dim", "blurhash", "thumb", "image",
    "fallback", "duration", "bitrate", "alt", "filename",
})

_EXT_TO_MIME = {
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
    "opus": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
    "aac": "audio/aac",
}
_MIME_TO_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/ogg": "ogg", "audio/opus": "opus", "audio/wav": "wav",
    "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
}


def _is_sha256_hex(value: str) -> bool:
    return bool(value) and len(value) == 64 and all(
        c in "0123456789abcdef" for c in value.lower())


@dataclass
class AudioAttachment:
    """A detected audio blob reference on an incoming kind-9 event."""

    url: str                # absolute https URL to fetch
    sha256: str             # 64-hex blob hash (BUD-11 x tag)
    mime: str               # audio/* mime
    ext: str                # file extension without dot
    size: Optional[int] = None       # bytes, from imeta (absent on regex path)
    duration: Optional[float] = None  # seconds, from imeta
    bitrate: Optional[int] = None
    filename: Optional[str] = None


def _parse_imeta_pairs(payload: str) -> Dict[str, str]:
    """Parse an NIP-92 ``imeta`` payload into key -> value pairs.

    The payload is a space-separated list of key/value pairs. Unknown
    leading tokens are skipped rather than desyncing the pairing, and a
    duplicate key keeps its last occurrence.
    """
    tokens = payload.split()
    pairs: Dict[str, str] = {}
    i = 0
    while i < len(tokens):
        key = tokens[i]
        if key in _IMETA_KEYS and i + 1 < len(tokens):
            pairs[key] = tokens[i + 1]
            i += 2
        else:
            i += 1
    return pairs


def _attachment_from_imeta(payload: str) -> Optional[AudioAttachment]:
    """Build an :class:`AudioAttachment` from one imeta payload.

    Audio only (``m`` must start with ``audio/``); requires a usable
    ``url`` and a 64-hex ``x``. Anything else returns None (the message
    passes through untouched — design F#10).
    """
    pairs = _parse_imeta_pairs(payload)
    mime = pairs.get("m", "")
    if not mime.lower().startswith("audio/"):
        return None
    url = pairs.get("url", "")
    sha = pairs.get("x", "")
    if not url or not _is_sha256_hex(sha):
        return None

    ext = ""
    path = urlsplit(url).path.lower()
    for candidate in _AUDIO_EXTS:
        if path.endswith("." + candidate):
            ext = candidate
            break
    if not ext:
        ext = _MIME_TO_EXT.get(mime.lower().split(";")[0].strip(), "mp3")

    def _to_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return AudioAttachment(
        url=url,
        sha256=sha.lower(),
        mime=mime,
        ext=ext,
        size=_to_int(pairs.get("size")),
        duration=_to_float(pairs.get("duration")),
        bitrate=_to_int(pairs.get("bitrate")),
        filename=pairs.get("filename"),
    )


def _parse_imeta_audio_all(tags: List) -> List[AudioAttachment]:
    """Return every audio attachment declared via imeta tags, in order."""
    attachments: List[AudioAttachment] = []
    for tag in tags or []:
        if (isinstance(tag, (list, tuple)) and len(tag) >= 2
                and tag[0] == "imeta"):
            attachment = _attachment_from_imeta(str(tag[1]))
            if attachment is not None:
                attachments.append(attachment)
    return attachments


def _parse_imeta_audio(tags: List) -> Optional[AudioAttachment]:
    """Return the first audio attachment from imeta tags, if any."""
    attachments = _parse_imeta_audio_all(tags)
    return attachments[0] if attachments else None


def _relay_hosts_and_bases(relays: List[str]) -> Tuple[Set[str], List[str]]:
    """Derive (https hosts, base URLs) from the configured relay URLs.

    ``wss://relay.example`` yields the host ``relay.example`` and the media
    base ``https://relay.example``; plain-ws relays map to http. Used to
    resolve relative ``/media/...`` paths and to enforce the anti-exfil
    host allowlist on the bare-URL fallback.
    """
    hosts: Set[str] = set()
    bases: List[str] = []
    for relay in relays or []:
        parts = urlsplit(relay)
        if not parts.hostname:
            continue
        hosts.add(parts.hostname.lower())
        scheme = "https" if parts.scheme in ("ws", "wss", "https") else "http"
        bases.append(f"{scheme}://{parts.netloc}")
    return hosts, bases


def _find_audio_url_in_content(content: str, allowed_hosts: Set[str],
                               relay_bases: List[str]) -> Optional[AudioAttachment]:
    """Bare-URL fallback: find a ``/media/<sha256>.<ext>`` audio reference.

    Absolute URLs are only accepted on a configured relay host (anti-exfil:
    the bot must never fetch an arbitrary host from message text). Relative
    paths resolve against the first configured relay base.
    """
    for match in _AUDIO_MEDIA_URL_RE.finditer(content or ""):
        scheme, host, sha, ext = match.group(1), match.group(2), \
            match.group(3), match.group(4)
        if scheme:
            if (host or "").lower() not in allowed_hosts:
                continue
            url = match.group(0)
        else:
            if not relay_bases:
                continue
            url = relay_bases[0] + match.group(0)
        return AudioAttachment(
            url=url,
            sha256=sha.lower(),
            mime=_EXT_TO_MIME.get(ext.lower(), "audio/mpeg"),
            ext=ext.lower(),
        )
    return None


def _format_duration(seconds: Optional[float]) -> Optional[str]:
    """Render seconds as ``M:SS`` (or ``H:MM:SS``); None when unknown."""
    if seconds is None or seconds < 0:
        return None
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _build_blossom_get_auth_event(privkey: bytes, sha256_hex: str) -> Dict:
    """Build a BUD-11 kind-24242 'get' auth event for one blob hash."""
    tags = [
        ["t", "get"],
        ["x", sha256_hex],
        ["expiration", str(int(time.time()) + _BLOSSOM_AUTH_TTL_SECONDS)],
    ]
    return _build_event(privkey, _BLOSSOM_GET_KIND, tags, "get audio")


def _blossom_auth_header(auth_event: Dict) -> str:
    """Encode a signed auth event as an unpadded base64url header value."""
    raw = json.dumps(auth_event, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"Nostr {encoded}"


def _stt_provider_available() -> bool:
    """Return True when a transcription backend is configured.

    Dispatches through the same provider resolution the STT tool uses
    (``stt.provider`` in config.yaml, plugin registry included — AB-3).
    Only consulted when an audio attachment was detected, never on the
    plain-text hot path.
    """
    try:
        from tools.transcription_tools import _get_provider, _load_stt_config
        return _get_provider(_load_stt_config()) != "none"
    except Exception:
        return False


def _transcribe_file(path: str) -> Dict[str, Any]:
    """Transcribe a local audio file via the configured STT provider.

    Thin module-level indirection so tests (and future provider swaps)
    can patch one seam instead of the whole STT stack.
    """
    from tools.transcription_tools import transcribe_audio
    return transcribe_audio(path)


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

        # --- audio-ingest bridge (nostr.audio_ingest, design AB-2) ------
        # Read ONCE here at adapter init — per-message config reads are
        # forbidden (prompt-cache rule). Defaults leave the bridge off.
        self._audio_ingest_enabled = bool(extra.get("audio_ingest", False))
        try:
            self._audio_max_bytes = int(
                extra.get("audio_max_bytes", _DEFAULT_AUDIO_MAX_BYTES))
        except (TypeError, ValueError):
            self._audio_max_bytes = _DEFAULT_AUDIO_MAX_BYTES
        try:
            self._audio_max_seconds = float(
                extra.get("audio_max_seconds", _DEFAULT_AUDIO_MAX_SECONDS))
        except (TypeError, ValueError):
            self._audio_max_seconds = _DEFAULT_AUDIO_MAX_SECONDS
        try:
            self._audio_soft_seconds = float(
                extra.get("audio_soft_seconds",
                          _DEFAULT_AUDIO_SOFT_SECONDS))
        except (TypeError, ValueError):
            self._audio_soft_seconds = _DEFAULT_AUDIO_SOFT_SECONDS
        # Anti-exfil allowlist + relative-path resolution targets, derived
        # from the configured relay URLs (wss://host -> https://host).
        self._audio_relay_hosts, self._audio_relay_bases = \
            _relay_hosts_and_bases(self.relays)

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

        # --- audio-ingest bridge (``nostr.audio_ingest``, default false) ---
        # A pure content transform BEFORE handle_message: session keying,
        # system prompt, toolset, and routing are untouched, so an audio
        # message and a plain-text message from the same sender land on an
        # identical prompt prefix (prompt-cache rule, design §E).
        if self._audio_ingest_enabled:
            content = await self._maybe_extract_audio_attachment(content, tags)
            if not content:
                return
        # -------------------------------------------------------------------

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
    # Audio-ingest bridge (design AB-2)
    # ------------------------------------------------------------------

    async def _maybe_extract_audio_attachment(self, content: str,
                                              tags: List) -> str:
        """Replace voice-note content with its transcript (or a failure note).

        A pure content transform: never raises into ``_process_event`` and
        never mutates adapter state. All network I/O is the single Blossom
        GET (NIP-24242 auth); transcription dispatches through
        ``tools.transcription_tools`` (which consults the plugin
        transcription registry). Failure modes follow design §F.
        """
        prefix = "[voice note]"

        def _reply(text: str) -> str:
            return f"{prefix} {text}"

        # ---- detection: NIP-92 imeta first, regex fallback second (F#10)
        attachments = _parse_imeta_audio_all(tags)
        skipped = max(0, len(attachments) - 1)
        attachment = attachments[0] if attachments else None
        if attachment is None:
            attachment = _find_audio_url_in_content(
                content, self._audio_relay_hosts, self._audio_relay_bases)
        if attachment is None:
            return content  # not an audio message — byte-identical passthrough

        # ---- F#1: no STT provider configured — do not fetch at all
        if not _stt_provider_available():
            return _reply(_F1_NO_PROVIDER)

        # ---- F#3: size gate from imeta (skip the fetch entirely)
        if attachment.size is not None and attachment.size > self._audio_max_bytes:
            return _reply(
                f"clip too large — keep voice notes under "
                f"{self._audio_max_bytes // (1024 * 1024)} MB")

        # ---- F#4: hard duration cap from imeta
        if attachment.duration is not None and \
                attachment.duration > self._audio_max_seconds:
            return _reply(
                f"clip too long — keep voice notes under "
                f"{int(self._audio_max_seconds) // 60} minutes")
        soft_over = (attachment.duration is not None and
                     attachment.duration > self._audio_soft_seconds)

        # ---- F5 fetch: single GET with BUD-11 x-scoped Blossom auth
        if not self._privkey:
            # Keyless adapter cannot sign the kind-24242 GET event.
            return _reply(_F5_FETCH_AUTH)
        auth_header = _blossom_auth_header(
            _build_blossom_get_auth_event(self._privkey, attachment.sha256))
        try:
            async with httpx.AsyncClient(timeout=30.0,
                                         follow_redirects=False) as client:
                resp = await client.get(
                    attachment.url, headers={"Authorization": auth_header})
        except Exception as e:
            logger.warning("Nostr audio: fetch failed for %s: %s",
                           attachment.url, e)
            return _reply(_F2_TRANSCRIBE_FAILED)

        if resp.status_code in (401, 403):
            return _reply(_F5_FETCH_AUTH)
        if resp.status_code == 404:
            return _reply(_F6_MISSING)
        if resp.status_code != 200:
            logger.warning("Nostr audio: HTTP %s fetching %s",
                           resp.status_code, attachment.url)
            return _reply(_F2_TRANSCRIBE_FAILED)

        # ---- F#3: size gate from Content-Length / actual body
        try:
            declared = int(resp.headers.get("content-length", ""))
        except ValueError:
            declared = 0
        if declared > self._audio_max_bytes or \
                len(resp.content) > self._audio_max_bytes:
            return _reply(
                f"clip too large — keep voice notes under "
                f"{self._audio_max_bytes // (1024 * 1024)} MB")

        # ---- integrity: sha256 must match the imeta ``x`` / URL hash
        if hashlib.sha256(resp.content).hexdigest() != attachment.sha256:
            logger.warning("Nostr audio: sha256 mismatch for %s — "
                           "not transcribing", attachment.url)
            return _reply(_F2_TRANSCRIBE_FAILED)

        # ---- transcribe via tempfile (0600, unlinked in finally)
        fd = tempfile.NamedTemporaryFile(
            prefix="nostr-audio-", suffix=f".{attachment.ext}", delete=False)
        tmp_path = fd.name
        try:
            fd.write(resp.content)
            fd.close()
            os.chmod(tmp_path, 0o600)
            try:
                result = _transcribe_file(tmp_path)
            except Exception as e:
                logger.warning("Nostr audio: transcription raised: %s", e)
                result = None
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        transcript = ""
        if isinstance(result, dict) and result.get("success"):
            transcript = str(result.get("transcript") or "").strip()
        if not transcript:
            return _reply(_F2_TRANSCRIBE_FAILED)

        # ---- assemble replacement content (spec §F/#9 note appended)
        duration = _format_duration(attachment.duration)
        parts = [f"[voice note, {duration}]"] if duration else [prefix]
        if soft_over:
            parts.append(_LONG_CLIP_WARNING)
        parts.append(transcript)
        if skipped:
            parts.append(f"(+{skipped} more clips skipped)")
        return " ".join(parts)

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
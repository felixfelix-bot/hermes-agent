"""
E2E spike: Buzz relay <-> NIP-29 chain verification.

Tests the full NIP-29 chain against a running Buzz relay:
1. Generate a Nostr keypair (BIP-340 Schnorr via coincurve)
2. Connect via WebSocket to the relay
3. Complete NIP-42 AUTH challenge-response
4. Create a group (kind 9007)
5. Subscribe to kind 9 events for that group
6. Publish a kind 9 text message with h=<group> tag
7. Receive the message back via subscription
8. Verify round-trip content match

This test does NOT use the Hermes adapter — it exercises the raw Nostr
protocol against the Buzz relay to verify the relay side of the chain.
The Hermes adapter test follows separately.

Run:
    python3 tests/test_nostr_nip29_e2e.py

Requires: websockets, coincurve (both in Hermes venv)
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ─── Nostr crypto helpers (BIP-340 Schnorr via coincurve) ────────────────────

def _bech32_decode(s: str) -> bytes:
    """Decode a bech32 string (nsec/npub) to raw payload bytes."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    if not s or any(c not in CHARSET + "1" for c in s.lower()):
        return bytes.fromhex(s)  # assume raw hex
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        raise ValueError(f"invalid bech32: {s}")
    data = s[pos + 1:]
    decoded = [CHARSET.index(c) for c in data]
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


def _bech32_encode(hrp: str, data: bytes) -> str:
    """Encode bytes to a bech32 string (nsec/npub)."""
    CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    # 8-bit to 5-bit
    conv = []
    buffer = 0
    bits = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= 5:
            conv.append((buffer >> (bits - 5)) & 0x1F)
            bits -= 5
    if bits > 0:
        conv.append((buffer << (5 - bits)) & 0x1F)
    # Checksum
    def bech32_polymod(values):
        GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        chk = 1
        for v in values:
            top = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ v
            for i in range(5):
                chk ^= GEN[i] if ((top >> i) & 1) else 0
        return chk
    def bech32_hrp_expand(hrp):
        return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 0x1F for c in hrp]
    values = bech32_hrp_expand(hrp) + conv
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 0x1F for i in range(6)]
    return hrp + "1" + "".join(CHARSET[c] for c in conv + checksum)


def generate_keypair() -> Tuple[bytes, str]:
    """Generate a random Nostr keypair. Returns (privkey_bytes, npub_string)."""
    from coincurve import PrivateKey
    pk = PrivateKey()
    privkey = pk.secret
    pubkey_hex = pk.public_key.format(compressed=True)[1:33].hex()
    npub = _bech32_encode("npub", bytes.fromhex(pubkey_hex))
    return privkey, npub


def privkey_to_nsec(privkey: bytes) -> str:
    return _bech32_encode("nsec", privkey)


def compute_event_id(pubkey: str, created_at: int, kind: int,
                      tags: List, content: str) -> str:
    """Compute Nostr event ID = sha256 of canonical JSON array."""
    canonical = json.dumps([0, pubkey, created_at, kind, tags, content],
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def sign_event_id(privkey: bytes, event_id: str) -> str:
    """Sign a 32-byte event ID with BIP-340 Schnorr."""
    from coincurve import PrivateKey
    pk = PrivateKey(privkey)
    sig = pk.sign_schnorr(bytes.fromhex(event_id))
    return sig.hex()


def build_event(privkey: bytes, kind: int, tags: List, content: str) -> Dict:
    """Build, sign, and return a Nostr event dict."""
    from coincurve import PrivateKey
    pk = PrivateKey(privkey)
    pubkey = pk.public_key.format(compressed=True)[1:33].hex()
    created_at = int(time.time())
    event_id = compute_event_id(pubkey, created_at, kind, tags, content)
    sig = sign_event_id(privkey, event_id)
    return {
        "id": event_id,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


# ─── E2E test ───────────────────────────────────────────────────────────────

RELAY_URL = os.environ.get("NOSTR_RELAY_URL", "ws://localhost:3007")
TEST_GROUP_NAME = f"spike-test-{uuid.uuid4().hex[:8]}"
TEST_MESSAGE = f"Spike test message at {time.time()}"

# Track test results
results = []

def log_result(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    line = f"  [{status}] {name}" + (f": {detail}" if detail else "")
    results.append((name, passed, detail))
    print(line)


async def e2e_test():
    """Full NIP-29 chain test against Buzz relay."""
    import websockets

    print(f"\n{'='*60}")
    print(f"NIP-29 E2E Spike Test")
    print(f"Relay: {RELAY_URL}")
    print(f"{'='*60}\n")

    # ── Step 1: Generate keypair ──────────────────────────────────────────
    print("Step 1: Generate Nostr keypair")
    privkey, npub = generate_keypair()
    nsec = privkey_to_nsec(privkey)
    log_result("keypair generation", bool(privkey and npub),
               f"npub={npub[:20]}...")
    print(f"  nsec: {nsec[:25]}...")

    # ── Step 2: Connect to relay ───────────────────────────────────────────
    print("\nStep 2: Connect to relay")
    try:
        ws = await asyncio.wait_for(
            websockets.connect(RELAY_URL, ping_interval=30, ping_timeout=10),
            timeout=10,
        )
        log_result("WebSocket connection", True, RELAY_URL)
    except Exception as e:
        log_result("WebSocket connection", False, str(e))
        print("\nFATAL: Cannot connect to relay. Aborting.")
        return False

    try:
        # ── Step 3: NIP-42 AUTH ────────────────────────────────────────────
        print("\nStep 3: NIP-42 AUTH challenge-response")
        # Read first message — could be AUTH challenge or something else
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            msg_type = msg[0] if isinstance(msg, list) and msg else ""
            print(f"  First message type: {msg_type}")
        except asyncio.TimeoutError:
            log_result("initial message", False, "timeout waiting for first message")
            return False

        if msg_type == "AUTH":
            challenge = msg[1] if len(msg) > 1 else ""
            print(f"  AUTH challenge received: {challenge[:32]}...")
            # Build kind 22242 auth event
            auth_tags = [["relay", RELAY_URL], ["challenge", challenge]]
            auth_event = build_event(privkey, 22242, auth_tags, "")
            await ws.send(json.dumps(["AUTH", auth_event]))

            # Wait for OK
            auth_ok = False
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    resp = json.loads(raw)
                    rtype = resp[0] if isinstance(resp, list) else ""
                    print(f"  Auth response: {rtype} — {str(resp)[:100]}")
                    if rtype == "OK" and len(resp) >= 3 and resp[2] is True:
                        auth_ok = True
                        break
                    elif rtype in ("NOTICE", "CLOSED"):
                        print(f"  Draining: {resp}")
                except asyncio.TimeoutError:
                    break

            log_result("NIP-42 auth", auth_ok,
                       "authenticated" if auth_ok else "no OK confirmation")
        else:
            log_result("NIP-42 auth", True, "not required by relay")

        # ── Step 4: Create a group (kind 9007) ─────────────────────────────
        print("\nStep 4: Create group (kind 9007)")
        group_id = str(uuid.uuid4())
        create_tags = [
            ["h", group_id],
            ["name", TEST_GROUP_NAME],
        ]
        create_event = build_event(privkey, 9007, create_tags, "")
        await ws.send(json.dumps(["EVENT", create_event]))

        # Wait for OK
        group_created = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                resp = json.loads(raw)
                rtype = resp[0] if isinstance(resp, list) else ""
                print(f"  Create response: {rtype} — {str(resp)[:120]}")
                if rtype == "OK" and len(resp) >= 3 and resp[2] is True:
                    group_created = True
                    break
                elif rtype == "OK" and len(resp) >= 3 and resp[2] is False:
                    print(f"  FAILED: {resp[3] if len(resp) > 3 else 'no message'}")
                    break
            except asyncio.TimeoutError:
                break

        log_result("group creation", group_created,
                   f"group_id={group_id}")

        # ── Step 5: Subscribe to kind 9 events for the group ────────────────
        print("\nStep 5: Subscribe to kind 9 events")
        sub_id = f"spike-{int(time.time())}"
        req = json.dumps(["REQ", sub_id, {"kinds": [9], "#h": [group_id]}])
        await ws.send(req)

        # Wait for EOSE (end of stored events)
        got_eose = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                resp = json.loads(raw)
                rtype = resp[0] if isinstance(resp, list) else ""
                if rtype == "EOSE":
                    got_eose = True
                    print(f"  EOSE received for sub {resp[1]}")
                    break
                elif rtype == "EVENT":
                    print(f"  Pre-existing event (expected if group existed)")
                elif rtype == "CLOSED":
                    print(f"  CLOSED: {resp}")
                    break
            except asyncio.TimeoutError:
                break

        log_result("subscription", got_eose, f"sub_id={sub_id}")

        # ── Step 6: Publish a kind 9 text message ────────────────────────────
        print("\nStep 6: Publish kind 9 text message")
        msg_tags = [["h", group_id]]
        msg_event = build_event(privkey, 9, msg_tags, TEST_MESSAGE)
        await ws.send(json.dumps(["EVENT", msg_event]))

        # Wait for OK
        publish_ok = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                resp = json.loads(raw)
                rtype = resp[0] if isinstance(resp, list) else ""
                print(f"  Publish response: {rtype} — {str(resp)[:120]}")
                if rtype == "OK" and len(resp) >= 3 and resp[2] is True:
                    publish_ok = True
                    break
                elif rtype == "OK" and len(resp) >= 3 and resp[2] is False:
                    print(f"  FAILED: {resp[3] if len(resp) > 3 else 'no message'}")
                    break
            except asyncio.TimeoutError:
                break

        log_result("message publish", publish_ok,
                   f"event_id={msg_event['id'][:16]}...")

        # ── Step 7: Receive the message back via subscription ───────────────
        print("\nStep 7: Receive message back via subscription")
        received_msg = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                resp = json.loads(raw)
                rtype = resp[0] if isinstance(resp, list) else ""
                print(f"  Received: {rtype} — {str(resp)[:120]}")
                if rtype == "EVENT" and len(resp) >= 3:
                    event = resp[2]
                    if event.get("kind") == 9:
                        # Check h tag matches our group
                        for tag in event.get("tags", []):
                            if len(tag) >= 2 and tag[0] == "h" and tag[1] == group_id:
                                received_msg = event.get("content", "")
                                break
                        if received_msg is not None:
                            break
            except asyncio.TimeoutError:
                break

        round_trip_ok = received_msg == TEST_MESSAGE
        log_result("round-trip message", round_trip_ok,
                   f"content={'match' if round_trip_ok else 'MISMATCH'}")
        if received_msg:
            print(f"  Received: {received_msg[:80]}")
            print(f"  Expected: {TEST_MESSAGE[:80]}")

    finally:
        await ws.close()

    # ── Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    total = len(results)
    passed = sum(1 for _, p, _ in results if p)
    print(f"  {passed}/{total} checks passed")
    for name, p, detail in results:
        status = "PASS" if p else "FAIL"
        print(f"    [{status}] {name}")

    all_passed = all(p for _, p, _ in results)
    print(f"\n  VERDICT: {'ALL PASS — NIP-29 chain works with Buzz relay' if all_passed else 'FAILURES DETECTED'}")
    return all_passed


if __name__ == "__main__":
    ok = asyncio.run(e2e_test())
    sys.exit(0 if ok else 1)
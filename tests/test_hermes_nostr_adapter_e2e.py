"""
E2E spike: Hermes Nostr adapter <-> Buzz relay.

Tests that the Hermes NostrAdapter class (from gateway/platforms/nostr.py)
can successfully:
1. Load a private key from an nsec file
2. Connect to the Buzz relay via WebSocket
3. Complete NIP-42 auth
4. Subscribe to a group
5. Send a message (kind 9 event) and have it received back
6. Receive an incoming message from another client

This exercises the ACTUAL adapter code, not a reimplementation.

Run:
    python3 tests/test_hermes_nostr_adapter_e2e.py

Requires: websockets, coincurve (both in Hermes venv)
"""

import asyncio
import json
import os
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Add the worktree root to path so we can import the adapter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the adapter under test
from gateway.platforms.nostr import (
    NostrAdapter,
    _build_event,
    _privkey_from_nsec,
    _pubkey_from_privkey,
)
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import SessionSource

# Import crypto helpers from the e2e test for the second client
from tests.test_nostr_nip29_e2e import (
    generate_keypair,
    privkey_to_nsec,
    build_event as build_event_raw,
)

RELAY_URL = os.environ.get("NOSTR_RELAY_URL", "ws://localhost:3007")

# Track test results
results = []

def log_result(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f": {detail}" if detail else ""))


async def adapter_test():
    """Test the Hermes NostrAdapter against Buzz relay."""
    import websockets

    print(f"\n{'='*60}")
    print(f"Hermes NostrAdapter <-> Buzz Relay E2E Test")
    print(f"Relay: {RELAY_URL}")
    print(f"{'='*60}\n")

    # ── Setup: generate keypair, write nsec file, create group ────────────
    print("Setup: Generate keypair and nsec file")
    privkey, npub = generate_keypair()
    nsec = privkey_to_nsec(privkey)
    nsec_path = f"/tmp/spike-nsec-{uuid.uuid4().hex[:8]}.txt"
    with open(nsec_path, "w") as f:
        f.write(nsec)
    print(f"  nsec file: {nsec_path}")
    print(f"  npub: {npub[:25]}...")

    group_id = str(uuid.uuid4())
    print(f"  group_id: {group_id}")

    # ── Step 1: Create the group via a raw WebSocket client ─────────────────
    print("\nStep 1: Create group on relay (raw client)")
    try:
        ws = await asyncio.wait_for(
            websockets.connect(RELAY_URL, ping_interval=30, ping_timeout=10),
            timeout=10,
        )
    except Exception as e:
        log_result("raw client connection", False, str(e))
        return False

    try:
        # Auth
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        if msg[0] == "AUTH":
            challenge = msg[1]
            auth_tags = [["relay", RELAY_URL], ["challenge", challenge]]
            auth_event = build_event_raw(privkey, 22242, auth_tags, "")
            await ws.send(json.dumps(["AUTH", auth_event]))
            # Drain until OK
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    resp = json.loads(raw)
                    if resp[0] == "OK" and resp[2] is True:
                        break
                except asyncio.TimeoutError:
                    break

        # Create group (kind 9007)
        create_tags = [["h", group_id], ["name", f"adapter-test-{uuid.uuid4().hex[:8]}"]]
        create_event = build_event_raw(privkey, 9007, create_tags, "")
        await ws.send(json.dumps(["EVENT", create_event]))
        group_created = False
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                resp = json.loads(raw)
                if resp[0] == "OK" and resp[2] is True:
                    group_created = True
                    break
                elif resp[0] == "OK" and resp[2] is False:
                    break
            except asyncio.TimeoutError:
                break
        log_result("group creation", group_created, f"group_id={group_id}")
    finally:
        await ws.close()

    if not group_created:
        print("FATAL: Cannot create group. Aborting.")
        return False

    # ── Step 2: Initialize and connect the Hermes adapter ──────────────────
    print("\nStep 2: Initialize Hermes NostrAdapter")
    config = PlatformConfig(
        enabled=True,
        extra={
            "relays": [RELAY_URL],
            "groups": [group_id],
            "nsec_path": nsec_path,
        },
    )

    adapter = NostrAdapter(config)
    log_result("adapter initialization", adapter is not None, "NostrAdapter created")
    print(f"  relays: {adapter.relays}")
    print(f"  groups: {adapter.groups}")
    print(f"  nsec_path: {adapter.nsec_path}")

    # ── Step 3: Connect adapter to relay ───────────────────────────────────
    print("\nStep 3: Connect adapter to relay")
    try:
        connected = await asyncio.wait_for(adapter.connect(), timeout=15)
        log_result("adapter connect", connected,
                   f"running={adapter._running}, ws={len(adapter._ws)}")
    except Exception as e:
        log_result("adapter connect", False, str(e))
        return False

    if not connected:
        print("FATAL: Adapter failed to connect. Aborting.")
        return False

    # ── Step 4: Verify adapter is connected and healthy ────────────────────
    print("\nStep 4: Verify adapter health")
    healthy = await adapter.health_check()
    is_conn = adapter.is_connected
    log_result("health check", healthy, f"healthy={healthy}")
    log_result("is_connected", is_conn, f"is_connected={is_conn}")
    print(f"  private key loaded: {adapter._privkey is not None}")
    print(f"  pubkey: {adapter._pubkey[:16] if adapter._pubkey else 'None'}...")
    print(f"  websocket count: {len(adapter._ws)}")

    # ── Step 5: Send a message via adapter.send() ──────────────────────────
    print("\nStep 5: Send message via adapter.send()")
    test_msg = f"Adapter test at {time.time()}"
    try:
        send_result = await asyncio.wait_for(
            adapter.send(group_id, test_msg),
            timeout=10,
        )
        log_result("adapter.send()", send_result.success,
                   f"message_id={send_result.message_id[:16] if send_result.message_id else 'None'}..."
                   if send_result.message_id else f"error={send_result.error}")
    except Exception as e:
        log_result("adapter.send()", False, str(e))
        send_result = None

    # ── Step 6: Verify the message was published by reading it back ────────
    # NOTE: The adapter skips its own messages (pubkey == self._pubkey),
    # which is correct behavior. So we verify via a separate raw client
    # that the event is on the relay and has the right content.
    print("\nStep 6: Verify message was published (read back via raw client)")
    print("  (adapter correctly skips own messages — verifying via raw client)")
    try:
        ws2 = await asyncio.wait_for(
            websockets.connect(RELAY_URL, ping_interval=30, ping_timeout=10),
            timeout=10,
        )
    except Exception as e:
        log_result("verification connection", False, str(e))
        ws2 = None

    if ws2:
        try:
            # Auth
            raw = await asyncio.wait_for(ws2.recv(), timeout=5)
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                challenge = msg[1]
                auth_tags = [["relay", RELAY_URL], ["challenge", challenge]]
                auth_event = build_event_raw(privkey, 22242, auth_tags, "")
                await ws2.send(json.dumps(["AUTH", auth_event]))
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws2.recv(), timeout=2)
                        resp = json.loads(raw)
                        if resp[0] == "OK" and resp[2] is True:
                            break
                    except asyncio.TimeoutError:
                        break

            # Subscribe to the group's kind 9 events
            sub_id = f"verify-{int(time.time())}"
            await ws2.send(json.dumps(["REQ", sub_id, {"kinds": [9], "#h": [group_id]}]))

            # Collect events — the adapter's message should be in the store
            received_content = None
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws2.recv(), timeout=2)
                    resp = json.loads(raw)
                    rtype = resp[0] if isinstance(resp, list) else ""
                    if rtype == "EVENT" and len(resp) >= 3:
                        event = resp[2]
                        if event.get("kind") == 9:
                            content = event.get("content", "")
                            if content == test_msg:
                                received_content = content
                                break
                    elif rtype == "EOSE":
                        # Events may come after EOSE (realtime)
                        pass
                except asyncio.TimeoutError:
                    break

            log_result("message published to relay",
                       received_content == test_msg,
                       "content match" if received_content == test_msg
                       else "message not found in relay store")
        finally:
            await ws2.close()

    # ── Step 7: Simulate incoming message from another client ───────────────
    print("\nStep 7: Test incoming message handling")
    # The adapter subscribes to the group. If we publish from a separate
    # client, the adapter should receive it and route it to handle_message.
    # We'll intercept handle_message to capture the event.
    received_events = []

    async def mock_handle_message(msg_event: MessageEvent):
        """Capture incoming messages for verification."""
        received_events.append(msg_event)
        print(f"  [INTERCEPTED] text={msg_event.text[:60]}")
        print(f"  [INTERCEPTED] source.chat_id={msg_event.source.chat_id}")
        print(f"  [INTERCEPTED] source.user_id={msg_event.source.user_id[:20]}...")

    # Monkey-patch the adapter's handle_message method
    adapter.handle_message = mock_handle_message

    # Generate a second keypair for the "other" sender
    privkey2, npub2 = generate_keypair()
    pubkey2 = _pubkey_from_privkey(privkey2)

    # Publish a message from the second client
    try:
        ws3 = await asyncio.wait_for(
            websockets.connect(RELAY_URL, ping_interval=30, ping_timeout=10),
            timeout=10,
        )
    except Exception as e:
        log_result("second client connection", False, str(e))
        ws3 = None

    if ws3:
        try:
            # Auth with second key
            raw = await asyncio.wait_for(ws3.recv(), timeout=5)
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                challenge = msg[1]
                auth_tags = [["relay", RELAY_URL], ["challenge", challenge]]
                auth_event = build_event_raw(privkey2, 22242, auth_tags, "")
                await ws3.send(json.dumps(["AUTH", auth_event]))
                deadline = time.time() + 5
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws3.recv(), timeout=2)
                        resp = json.loads(raw)
                        if resp[0] == "OK" and resp[2] is True:
                            break
                    except asyncio.TimeoutError:
                        break

            # Add second user to the group first (kind 9000)
            add_tags = [["h", group_id], ["p", pubkey2]]
            add_event = build_event_raw(privkey, 9000, add_tags, "")
            await ws3.send(json.dumps(["EVENT", add_event]))
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws3.recv(), timeout=2)
                    resp = json.loads(raw)
                    if resp[0] == "OK" and resp[2] is True:
                        print(f"  Second user added to group")
                        break
                except asyncio.TimeoutError:
                    break

            # Give the relay a moment to process the membership
            await asyncio.sleep(1)

            # Publish a kind 9 message from the second user
            incoming_msg = f"Incoming test from second user at {time.time()}"
            msg_tags = [["h", group_id]]
            pub_event = build_event_raw(privkey2, 9, msg_tags, incoming_msg)
            await ws3.send(json.dumps(["EVENT", pub_event]))
            pub_ok = False
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws3.recv(), timeout=2)
                    resp = json.loads(raw)
                    if resp[0] == "OK" and resp[2] is True:
                        pub_ok = True
                        print(f"  Second user message published")
                        break
                    elif resp[0] == "OK" and resp[2] is False:
                        print(f"  Publish FAILED: {resp[3] if len(resp) > 3 else 'no msg'}")
                        break
                except asyncio.TimeoutError:
                    break

            # Wait for the adapter to process the incoming message
            # The adapter's listener task needs to receive and process it
            await asyncio.sleep(3)

            incoming_ok = len(received_events) > 0 and received_events[0].text == incoming_msg
            log_result("incoming message handling", incoming_ok,
                       f"captured {len(received_events)} events"
                       if received_events else "no events captured")
            if received_events:
                ev = received_events[0]
                print(f"  text: {ev.text[:60]}")
                print(f"  chat_id: {ev.source.chat_id}")
                print(f"  user_id: {ev.source.user_id[:20]}...")
                print(f"  platform: {ev.source.platform}")
            elif pub_ok:
                print(f"  Message was published OK but adapter didn't receive it")
                print(f"  (This may indicate a listener or subscription issue)")
        finally:
            await ws3.close()

    # ── Step 8: Disconnect cleanly ─────────────────────────────────────────
    print("\nStep 8: Disconnect adapter")
    try:
        await adapter.disconnect()
        log_result("adapter disconnect", True, "clean shutdown")
    except Exception as e:
        log_result("adapter disconnect", False, str(e))

    # Cleanup nsec file
    try:
        os.unlink(nsec_path)
    except OSError:
        pass

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
    print(f"\n  VERDICT: {'ALL PASS — Hermes NostrAdapter works with Buzz relay' if all_passed else 'FAILURES DETECTED'}")
    return all_passed


if __name__ == "__main__":
    ok = asyncio.run(adapter_test())
    sys.exit(0 if ok else 1)
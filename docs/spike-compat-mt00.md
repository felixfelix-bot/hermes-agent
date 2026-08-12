# MT-00: Compatibility Spike — Buzz → Hermes Nostr Adapter

## Status: PASS

The full NIP-29 chain works: **Buzz relay ↔ Hermes NostrAdapter** with NIP-42 authentication.

## Environment

- **Relay:** Buzz relay (Docker container `mt00-buzz-relay`), running at `ws://localhost:3007`
  - Postgres on :5433, Redis on :6380
  - NIP-11 confirmed: supports NIP-1, 2, 10, 11, 16, 17, 23, 25, 29, 33, 38, 42, 50, 56
- **Client:** Hermes `NostrAdapter` (`gateway/platforms/nostr.py`)
- **Crypto:** coincurve (BIP-340 Schnorr), websockets 15.0.1

## What Was Tested

### Test 1: Raw NIP-29 Protocol (7/7 PASS)
File: `tests/test_nostr_nip29_e2e.py`

Exercises the raw Nostr protocol against Buzz relay:
1. Generate Nostr keypair (BIP-340 Schnorr via coincurve)
2. WebSocket connect to relay
3. NIP-42 AUTH challenge-response (kind 22242)
4. Create group (kind 9007) with h-tag
5. Subscribe to kind 9 events for the group
6. Publish kind 9 text message with h-tag
7. Receive the message back via subscription (round-trip)

### Test 2: Hermes NostrAdapter E2E (9/9 PASS)
File: `tests/test_hermes_nostr_adapter_e2e.py`

Exercises the actual Hermes adapter class:
1. Create group on relay (raw client)
2. Initialize NostrAdapter with relay URL, groups, nsec path
3. Connect adapter to relay (includes NIP-42 auth)
4. Verify health check and is_connected
5. Send message via `adapter.send()` (publishes kind 9 event)
6. Verify message was published (read back via separate raw client)
   - Note: adapter correctly skips its own messages (pubkey == self._pubkey)
7. Test incoming message handling (second client publishes, adapter receives)
8. Clean disconnect

## Key Findings

### Bug Found: Committed nostr.py Lacks NIP-42 Auth
The committed version of `gateway/platforms/nostr.py` (commit 9cf6915378)
does NOT include the `_do_nip42_auth()` method. Without NIP-42 auth,
the Buzz relay rejects subscriptions with:
```
NOTICE: auth-required: authenticate before subscribing
CLOSED: auth-required
```

The fix exists as an **uncommitted change** in the main working tree.
This spike copied the fixed version into the worktree to verify it works.

**Recommendation:** Commit the NIP-42 auth changes from the main working tree
to the `nostr-adapter` branch and merge to `main`.

### Buzz Relay Behavior
- Buzz relay sends an AUTH challenge immediately on WebSocket connect
- After successful NIP-42 auth, subscriptions and event publishes work
- Groups are created with kind 9007 events (h-tag = group UUID, name tag)
- Kind 9 events with h-tag are accepted for any group (open channels)
- Messages are fanned out to all subscribed connections in realtime
- The relay echoes back the publisher's own events (the adapter correctly
  filters these by checking `pubkey == self._pubkey`)
- Membership (kind 9000) requires the event pubkey to match the authenticated
  connection identity (i.e., only the group owner can add members via their
  own connection)

### NostrAdapter Behavior
- `connect()`: loads nsec, connects WebSocket, performs NIP-42 auth, subscribes
- `send()`: builds and signs kind 9 event, publishes to all connected relays
- `_process_event()`: filters by kind (9), group (h-tag), and dedup (seen_ids)
- `handle_message()`: routes to gateway as MessageEvent with SessionSource
- `disconnect()`: clean shutdown of listener tasks and WebSocket connections
- Reconnection watcher runs every 30 seconds

## Test Commands

```bash
# Raw NIP-29 protocol test
python3 tests/test_nostr_nip29_e2e.py

# Hermes NostrAdapter e2e test
python3 tests/test_hermes_nostr_adapter_e2e.py
```

Both tests use `NOSTR_RELAY_URL` env var (default: `ws://localhost:3007`).

## Cold Review (Gate 2.5)

Cross-family review by Kimi (kimi-k2.7-code) found 2 major and 4 minor issues:

### Fixed:
1. **MAJOR:** `_do_nip42_auth` returned True even when no OK confirmation received — now returns False
2. **MAJOR:** Missing null-check on `self._privkey` before `_build_event` in initial auth path — added
3. **MINOR:** Duplicate log line in `connect()` — removed
4. **MINOR:** Session-time AUTH handler silently skipped when `_privkey` is None — added warning log

### Acknowledged but not blocking:
5. **MINOR:** Exception catch tuple `(asyncio.TimeoutError, json.JSONDecodeError, Exception)` is redundant — cosmetic, not fixed
6. **MINOR:** In the non-AUTH path, `_handle_relay_message` is called before `self._ws[relay_url]` is registered — this only affects relays that don't require auth (Buzz does, so this path is not exercised in production)

Post-fix re-run: 7/7 raw NIP-29 + 9/9 adapter e2e — all pass.

## Verdict

**Buzz relay + Hermes NostrAdapter = WORKING.** No need for alternative relays
(nostrord, Block Buzz relay). The only blocker was the missing NIP-42 auth in
the committed adapter code, which has an existing fix in the main working tree.
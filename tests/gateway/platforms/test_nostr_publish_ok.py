"""Unit tests for the Nostr adapter's publish-acknowledgement semantics.

Covers the fix for fire-and-forget publishing: send() must await each
relay's ["OK", event_id, accepted, reason] acknowledgement, surface
rejection reasons in SendResult, drop unresponsive relays, and retry
once on auth-required rejections (NIP-42).
"""

import asyncio
import json
from unittest import mock

import gateway.platforms.nostr as nostr_mod
from gateway.platforms.nostr import NostrAdapter

RELAY1 = "wss://relay-one.example"
RELAY2 = "wss://relay-two.example"


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, msg):
        if msg.startswith("RAISE"):
            raise RuntimeError("boom: simulated send failure")
        self.sent.append(msg)

    async def close(self):
        self.closed = True


def make_adapter(relays, ok_timeout=10.0):
    a = NostrAdapter.__new__(NostrAdapter)
    a.relays = list(relays)
    a.groups = ["grp"]
    a.nsec_path = ""
    a._privkey = b"\x01" * 32
    a._pubkey = "aa" * 32
    a._ws = {}
    a._listener_tasks = []
    a._reconnect_task = None
    a._running = True
    a._sub_id = "test-sub"
    a._seen_ids = set()
    a._max_seen = 5000
    a._pending_oks = {}
    a._ok_timeout = ok_timeout
    return a


def fake_build_event(privkey, kind, tags, content, _ctr=[0]):
    _ctr[0] += 1
    return {
        "id": f"evtid{_ctr[0]}",
        "pubkey": "aa" * 32,
        "created_at": 1,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": "ff" * 64,
    }


async def _wait_for(predicate, timeout=2.0, period=0.005):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(period)
    return False


def test_send_awaits_relay_ok_before_reporting_success(monkeypatch):
    """send() must block until the relay's OK arrives — no fire-and-forget."""
    monkeypatch.setattr(nostr_mod, "_build_event", fake_build_event)
    a = make_adapter([RELAY1])
    ws = FakeWS()
    a._ws[RELAY1] = ws

    async def scenario():
        task = asyncio.create_task(a.send("grp", "hello"))
        # The EVENT frame must go out, but send() must NOT finish before the OK.
        assert await _wait_for(lambda: bool(ws.sent)), "EVENT frame never sent"
        assert await _wait_for(lambda: bool(a._pending_oks)), "no pending OK waiter"
        await asyncio.sleep(0.05)
        assert not task.done(), "send() resolved before relay acknowledgement"
        frame = json.loads(ws.sent[0])
        assert frame[0] == "EVENT"
        event = frame[1]
        assert event["tags"] == [["h", "grp"]]
        assert event["content"] == "hello"
        await a._handle_relay_message(["OK", event["id"], True, ""], RELAY1)
        result = await asyncio.wait_for(task, 2)
        return result, event["id"]

    result, event_id = asyncio.run(scenario())
    assert result.success is True
    assert result.message_id == event_id
    assert result.raw_response["accepted_by"] == [RELAY1]


def test_send_surfaces_rejections_from_all_relays(monkeypatch):
    """All relays rejecting must yield success=False with every reason."""
    monkeypatch.setattr(nostr_mod, "_build_event", fake_build_event)
    a = make_adapter([RELAY1, RELAY2])
    ws1, ws2 = FakeWS(), FakeWS()
    a._ws[RELAY1] = ws1
    a._ws[RELAY2] = ws2

    async def canned_publish(relay_url, ws, event, msg):
        if relay_url == RELAY1:
            return (False, "auth-required: not authenticated")
        return (False, "blocked: duplicate")

    a._publish_to_relay = canned_publish

    result = asyncio.run(a.send("grp", "hello"))
    assert result.success is False
    assert result.retryable is True
    assert result.error is not None
    assert "auth-required" in result.error
    assert "blocked: duplicate" in result.error
    outcomes = result.raw_response["outcomes"]
    assert outcomes[RELAY1]["ok"] is False
    assert outcomes[RELAY2]["ok"] is False


def test_send_succeeds_if_any_relay_accepts(monkeypatch):
    monkeypatch.setattr(nostr_mod, "_build_event", fake_build_event)
    a = make_adapter([RELAY1, RELAY2])
    a._ws[RELAY1] = FakeWS()
    a._ws[RELAY2] = FakeWS()

    async def canned_publish(relay_url, ws, event, msg):
        return (relay_url == RELAY2, "nope" if relay_url == RELAY1 else "")

    a._publish_to_relay = canned_publish

    result = asyncio.run(a.send("grp", "hello"))
    assert result.success is True
    assert result.raw_response["accepted_by"] == [RELAY2]


def test_publish_retries_once_on_auth_required_rejection():
    """An auth-required rejection triggers exactly one re-authenticated retry."""
    a = make_adapter([RELAY1])
    calls = []

    async def sequence_publish(relay_url, ws, event, msg):
        calls.append(msg)
        if len(calls) == 1:
            return (False, "auth-required: you must auth")
        return (True, "")

    a._publish_and_wait = sequence_publish
    accepted, reason = asyncio.run(
        a._publish_to_relay(RELAY1, mock.Mock(), {"id": "evtX"}, '["EVENT",{}]'))
    assert accepted is True
    assert len(calls) == 2  # original + one retry


def test_publish_auth_retry_still_failing_reports_final_reason():
    a = make_adapter([RELAY1])
    calls = []

    async def sequence_publish(relay_url, ws, event, msg):
        calls.append(msg)
        return (False, "auth-required: still not authenticated")

    a._publish_and_wait = sequence_publish
    accepted, reason = asyncio.run(
        a._publish_to_relay(RELAY1, mock.Mock(), {"id": "evtX"}, '["EVENT",{}]'))
    assert accepted is False
    assert "still not authenticated" in reason
    assert len(calls) == 2


def test_publish_timeout_drops_relay(monkeypatch):
    """A relay that never acknowledges gets dropped for the reconnect watcher."""
    monkeypatch.setattr(nostr_mod, "_build_event", fake_build_event)
    a = make_adapter([RELAY1], ok_timeout=0.05)
    ws = FakeWS()
    a._ws[RELAY1] = ws
    event = fake_build_event(b"\x01", 9, [["h", "grp"]], "hello")
    accepted, reason = asyncio.run(
        a._publish_to_relay(RELAY1, ws, event, json.dumps(["EVENT", event])))
    assert accepted is False
    assert "timeout" in reason
    assert RELAY1 not in a._ws, "unresponsive relay must be dropped"
    assert ws.closed is True
    assert not a._pending_oks, "pending waiters must be cleaned up"


def test_publish_send_failure_drops_relay(monkeypatch):
    monkeypatch.setattr(nostr_mod, "_build_event", fake_build_event)
    a = make_adapter([RELAY1])
    ws = FakeWS()
    a._ws[RELAY1] = ws
    event = fake_build_event(b"\x01", 9, [["h", "grp"]], "hello")
    accepted, reason = asyncio.run(
        a._publish_to_relay(RELAY1, ws, event, "RAISE"))
    assert accepted is False
    assert "boom" in reason
    assert RELAY1 not in a._ws
    assert ws.closed is True


def test_ok_handler_resolves_pending_waiter():
    a = make_adapter([RELAY1])
    fut = asyncio.get_event_loop_policy().new_event_loop().create_future()

    async def scenario():
        running_fut = asyncio.get_running_loop().create_future()
        a._pending_oks["evtY"] = {RELAY1: running_fut}
        await a._handle_relay_message(["OK", "evtY", False, "unauthorized"], RELAY1)
        return running_fut.result()

    accepted, reason = asyncio.run(scenario())
    assert (accepted, reason) == (False, "unauthorized")
    assert "evtY" not in a._pending_oks


def test_unsolicited_ok_does_not_raise():
    a = make_adapter([RELAY1])
    asyncio.run(a._handle_relay_message(["OK", "unknown-event", False, "nope"], RELAY1))
    asyncio.run(a._handle_relay_message(["OK", "other-event", True, ""], RELAY2))


def test_fail_pending_oks_fails_waiters():
    a = make_adapter([RELAY1])

    async def scenario():
        fut = asyncio.get_running_loop().create_future()
        a._pending_oks["evtZ"] = {RELAY1: fut}
        a._fail_pending_oks(RELAY1, RuntimeError("relay connection lost"))
        return fut

    fut = asyncio.run(scenario())
    assert fut.done()
    assert isinstance(fut.exception(), RuntimeError)
    assert not a._pending_oks

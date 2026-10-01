"""Real-socket integration test for the Nostr adapter's publish path.

Unlike ``test_nostr_publish_ok.py`` (fakes in-process), this stands up an
actual WebSocket server that speaks NIP-01/NIP-42 the way the production
relay (``wss://relay.orangesync.tech``) does — it sends an ``AUTH`` challenge
on connect and answers every ``EVENT`` with ``["OK", id, accepted, reason]`` —
and drives the REAL :class:`NostrAdapter` over a loopback socket.

Why this test exists next to the unit tests: the original bug (fire-and-forget
publish) was invisible to a mocked send path, because the defect was that the
adapter never *read* the relay's reply at all. Exercising a real socket proves
the whole chain — challenge, AUTH answer, REQ subscribe, EVENT, OK
correlation — end to end, on the same code path the gateway runs.

Scenarios:
  1. relay accepts          -> send() == success True, message_id == event id
  2. relay rejects          -> send() == success False, the relay's reason is
                               surfaced (this is the regression: previously the
                               rejection was silently dropped and send()
                               reported success)
  3. relay never answers    -> send() == success False (timeout, no hang)
"""

import asyncio
import json
import os
import secrets

import pytest

import gateway.platforms.nostr as nostr_mod
from gateway.config import PlatformConfig
from gateway.platforms.nostr import NostrAdapter

GROUP = "integration-group"


class FakeRelay:
    """Minimal NIP-29-ish relay: AUTH challenge on connect, OK per EVENT."""

    def __init__(self, accept=True, reason="", silent=False):
        self.accept = accept
        self.reason = reason
        self.silent = silent
        self.published = []      # event dicts the relay accepted
        self.auth_seen = False
        self.subscribed = False
        self.port = None
        self.url = None
        self._server = None

    async def handler(self, conn):
        # 1. NIP-42 challenge, exactly like relay.orangesync.tech
        await conn.send(json.dumps(["AUTH", "challenge-abc123"]))
        try:
            async for raw in conn:
                msg = json.loads(raw)
                kind = msg[0]
                if kind == "AUTH":
                    # verify the auth event is the right shape (kind 22242,
                    # relay + challenge tags) before acknowledging
                    ev = msg[1]
                    tags = {t[0]: t[1] for t in ev["tags"]}
                    assert ev["kind"] == 22242
                    assert tags.get("challenge") == "challenge-abc123"
                    assert "relay" in tags
                    self.auth_seen = True
                    await conn.send(json.dumps(["OK", ev["id"], True, ""]))
                elif kind == "REQ":
                    self.subscribed = True
                    await conn.send(json.dumps(["EOSE", msg[1]]))
                elif kind == "EVENT":
                    ev = msg[1]
                    if not self.auth_seen:
                        # the pre-fix adapter's wire behaviour on this relay
                        await conn.send(json.dumps(
                            ["OK", ev["id"], False,
                             "auth-required: not authenticated"]))
                        continue
                    if self.silent:
                        continue          # never acknowledge
                    if self.accept:
                        self.published.append(ev)
                    await conn.send(json.dumps(
                        ["OK", ev["id"], self.accept, self.reason]))
        except Exception:
            pass

    async def __aenter__(self):
        from websockets.asyncio.server import serve
        self._server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def make_adapter(url, ok_timeout=5.0):
    """A real adapter pointed at the loopback relay, with a throwaway key."""
    path = f"/tmp/int-nostr-nsec-{os.getpid()}-{secrets.token_hex(4)}"
    with open(path, "w") as f:
        f.write(secrets.token_bytes(32).hex())
    os.chmod(path, 0o600)
    cfg = PlatformConfig(enabled=True, extra={
        "relays": [url],
        "groups": [GROUP],
        "nsec_path": path,
        "ok_timeout": ok_timeout,
    })
    return NostrAdapter(cfg)


def test_real_socket_publish_awaits_ok_and_succeeds():
    """Happy path over a real socket: AUTH, subscribe, EVENT, OK=True."""

    async def scenario():
        async with FakeRelay(accept=True) as relay:
            a = make_adapter(relay.url)
            assert await a.connect() is True
            assert relay.auth_seen, "adapter never answered the AUTH challenge"
            await asyncio.sleep(0.2)   # let the listener task start
            res = await asyncio.wait_for(a.send(GROUP, "hello over a real socket"),
                                         timeout=30)
            await a.disconnect()
            return res, relay

    res, relay = asyncio.run(scenario())
    assert res.success is True, res.error
    assert res.message_id is not None
    assert res.raw_response["accepted_by"] == [relay.url]
    # the relay really stored the event we think we published
    assert [e["id"] for e in relay.published] == [res.message_id]
    assert relay.published[0]["content"] == "hello over a real socket"
    assert relay.published[0]["tags"] == [["h", GROUP]]
    # ...and the event id is self-consistent per NIP-01
    ev = relay.published[0]
    assert ev["id"] == nostr_mod._compute_event_id(
        ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"])


def test_real_socket_publish_surfaces_relay_rejection():
    """The regression: a relay refusal must NOT read as a successful send."""

    async def scenario():
        async with FakeRelay(
                accept=False,
                reason="invalid: channel-scoped events must include an h tag") as relay:
            a = make_adapter(relay.url)
            assert await a.connect() is True
            await asyncio.sleep(0.2)
            res = await asyncio.wait_for(a.send(GROUP, "rejected content"), timeout=30)
            await a.disconnect()
            return res

    res = asyncio.run(scenario())
    assert res.success is False, "a rejected event was reported as delivered"
    assert res.retryable is True
    assert "channel-scoped events must include an h tag" in (res.error or "")
    outcomes = res.raw_response["outcomes"]
    (only_relay,) = outcomes.values()
    assert only_relay["ok"] is False
    assert "channel-scoped" in only_relay["reason"]


def test_real_socket_publish_never_hangs_on_silent_relay():
    """A relay that never acknowledges fails fast instead of hanging send()."""

    async def scenario():
        async with FakeRelay(silent=True) as relay:
            a = make_adapter(relay.url, ok_timeout=1.5)
            assert await a.connect() is True
            await asyncio.sleep(0.2)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            res = await asyncio.wait_for(a.send(GROUP, "silence"), timeout=20)
            elapsed = loop.time() - t0
            await a.disconnect()
            return res, elapsed

    res, elapsed = asyncio.run(scenario())
    assert res.success is False
    assert "timeout" in (res.error or "")
    assert elapsed < 15, f"send() hung for {elapsed:.1f}s on a silent relay"

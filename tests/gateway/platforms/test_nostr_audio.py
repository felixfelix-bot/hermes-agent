"""Tests for the Nostr audio-ingest bridge (design AB-2).

Covers (design doc §G, items 1-4 and 7):
  1. ``_parse_imeta_audio*`` — NIP-92 imeta parsing, audio-only filter,
     malformed-pair rejection.
  2. Regex fallback detection — relay-host allowlist (anti-exfil), relative
     ``/media/<sha256>.<ext>`` resolution.
  3. Blossom kind-24242 GET auth event construction + base64url header.
  4. Download path with mocked httpx — success, each failure mode F#1-F#6,
     F#9/F#10 handling, tempfile hygiene (0600, unlinked).
  7. Prompt-cache safety — the hook is a pure content transform; everything
     reaching ``handle_message`` except ``text`` is identical between an
     audio message and a plain-text message from the same sender, and with
     ``nostr.audio_ingest: false`` the extractor is never invoked.

All network and STT surfaces are mocked; nothing here touches a relay.
"""

import asyncio
import base64
import hashlib
import json
import os
import stat as statmod
import time
import uuid

import pytest

from gateway.platforms import nostr as nostr_mod
from gateway.platforms.base import MessageType
from gateway.config import PlatformConfig

# Deterministic test keypair (never a real key).
TEST_PRIVKEY = bytes(range(1, 33))
TEST_PUBKEY = nostr_mod._pubkey_from_privkey(TEST_PRIVKEY)
# Remote sender pubkey (must differ from the adapter's own pubkey).
SENDER_PUBKEY = "aa" * 32
RELAY = "wss://relay.example"
RELAY_HOST = "relay.example"
GROUP = "friends"

BLOB = b"fake-audio-bytes"
BLOB_SHA = hashlib.sha256(BLOB).hexdigest()

F1_COPY = "I received an audio clip but can't transcribe it yet — tell Felix to enable transcription."
F2_COPY = "couldn't transcribe that, try again"
F3_COPY = "clip too large — keep voice notes under 25 MB"
F4_COPY = "clip too long — keep voice notes under 5 minutes"
F5_COPY = "authentication failed"
F6_COPY = "that audio clip has expired"
LONG_WARN = "(warning: long voice note)"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def _make_adapter(extra=None, audio=True):
    merged = {
        "relays": [RELAY],
        "groups": [GROUP],
        "audio_ingest": audio,
    }
    merged.update(extra or {})
    cfg = PlatformConfig(enabled=True, extra=merged)
    adapter = nostr_mod.NostrAdapter(cfg)
    adapter._privkey = TEST_PRIVKEY
    adapter._pubkey = TEST_PUBKEY
    assert adapter._pubkey != SENDER_PUBKEY

    captured = []

    async def _capture(event):
        captured.append(event)

    adapter.handle_message = _capture
    return adapter, captured


def _imeta(sha=None, mime="audio/mp4", size: "int | None" = 18342,
           duration: "float | None" = 42.0, ext="m4a", url=None):
    sha = sha or BLOB_SHA
    url = url or f"https://{RELAY_HOST}/media/{sha}.{ext}"
    pairs = ["url", url, "m", mime, "x", sha]
    if size is not None:
        pairs += ["size", str(size)]
    if duration is not None:
        pairs += ["duration", str(duration)]
    return " ".join(pairs)


def _kind9_event(content, tags=None, pubkey=SENDER_PUBKEY):
    return {
        "id": uuid.uuid4().hex,
        "pubkey": pubkey,
        "created_at": int(time.time()),
        "kind": 9,
        "tags": tags if tags is not None else [["h", GROUP]],
        "content": content,
        "sig": "00" * 64,
    }


class _FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def _install_fake_httpx(monkeypatch, responder):
    """Patch httpx.AsyncClient with a fake capturing every GET."""
    calls = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            calls.append({"url": url, "headers": headers or {}})
            return responder(url, headers or {})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    return calls


def _install_transcriber(monkeypatch, transcript="hello world", ok=True,
                         error=None):
    seen = []

    def fake_transcribe(path):
        seen.append({
            "path": path,
            "suffix": os.path.splitext(path)[1],
            "mode": statmod.S_IMODE(os.stat(path).st_mode),
            "content": open(path, "rb").read(),
        })
        if ok:
            return {"success": True, "transcript": transcript}
        return {"success": False, "transcript": "", "error": error or "boom"}

    monkeypatch.setattr(nostr_mod, "_transcribe_file", fake_transcribe)
    return seen


def _provider(monkeypatch, available=True):
    monkeypatch.setattr(nostr_mod, "_stt_provider_available",
                        lambda: available)


def _decode_auth_header(value):
    assert value.startswith("Nostr "), value
    payload = value[len("Nostr "):]
    assert "=" not in payload, "base64url payload must be unpadded"
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _bip340_verify(pubkey_x32: bytes, sig64: bytes, msg32: bytes) -> bool:
    """Pure-python BIP-340 Schnorr verification (secp256k1).

    Independent of coincurve: this venv's coincurve build exposes
    ``sign_schnorr`` but not ``verify_schnorr``, so the test suite carries
    the reference verification math instead.
    """
    P = 2**256 - 2**32 - 977
    N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
         0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
    if len(pubkey_x32) != 32 or len(msg32) != 32 or len(sig64) != 64:
        return False
    px = int.from_bytes(pubkey_x32, "big")
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if px == 0 or px >= P or r >= P or s >= N:
        return False
    # lift_x: y = sqrt(x^3 + 7), choose the even root (x-only convention)
    y_sq = (pow(px, 3, P) + 7) % P
    py = pow(y_sq, (P + 1) // 4, P)
    if pow(py, 2, P) != y_sq:
        return False  # x is not a curve point
    if py % 2:
        py = P - py

    def add(p1, p2):
        x1, y1 = p1
        x2, y2 = p2
        if x1 == x2 and (y1 + y2) % P == 0:
            return None  # point at infinity
        if p1 == p2:
            lam = (3 * x1 * x1) * pow(2 * y1, P - 2, P) % P
        else:
            lam = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
        x3 = (lam * lam - x1 - x2) % P
        return (x3, (lam * (x1 - x3) - y1) % P)

    def mul(k, pt):
        acc = None
        for bit in bin(k)[2:]:
            acc = add(acc, acc) if acc is not None else None
            if bit == "1":
                acc = add(acc, pt) if acc is not None else pt
        return acc

    tag = hashlib.sha256(b"BIP0340/challenge").digest()
    e = int.from_bytes(hashlib.sha256(
        tag + tag + sig64[:32] + pubkey_x32 + msg32).digest(), "big") % N
    sG = mul(s, G)
    eP = mul(e, (px, py))
    neg_eP = (eP[0], (P - eP[1]) % P) if eP is not None else None
    R = add(sG, neg_eP) if neg_eP is not None else sG
    if R is None or R[1] % 2 or R[0] != r:
        return False
    return True


async def _process(adapter, event):
    await adapter._process_event(event, RELAY)


# ---------------------------------------------------------------------------
# G-item 1: imeta parsing
# ---------------------------------------------------------------------------

class TestImetaParsing:
    def test_basic_fields(self):
        att = nostr_mod._parse_imeta_audio([
            ["h", GROUP],
            ["imeta", _imeta(size=18342, duration=42.0)],
        ])
        assert att is not None
        assert att.url == f"https://{RELAY_HOST}/media/{BLOB_SHA}.m4a"
        assert att.sha256 == BLOB_SHA
        assert att.mime == "audio/mp4"
        assert att.size == 18342
        assert att.duration == 42.0
        assert att.ext == "m4a"

    def test_non_audio_mime_ignored(self):
        sha = "cd" * 32
        tags = [["imeta",
                 f"url https://{RELAY_HOST}/media/{sha}.png m image/png x {sha}"]]
        assert nostr_mod._parse_imeta_audio(tags) is None

    def test_missing_url_rejected(self):
        sha = "cd" * 32
        tags = [["imeta", f"m audio/mp4 x {sha} size 10"]]
        assert nostr_mod._parse_imeta_audio(tags) is None

    def test_missing_or_malformed_x_rejected(self):
        tags = [["imeta",
                 f"url https://{RELAY_HOST}/media/x.m4a m audio/mp4 x not-hex"]]
        assert nostr_mod._parse_imeta_audio(tags) is None
        tags = [["imeta",
                 f"url https://{RELAY_HOST}/media/x.m4a m audio/mp4"]]
        assert nostr_mod._parse_imeta_audio(tags) is None

    def test_duplicate_keys_last_wins(self):
        sha_a, sha_b = "ab" * 32, "cd" * 32
        payload = (
            f"url https://{RELAY_HOST}/media/{sha_a}.m4a "
            f"url https://{RELAY_HOST}/media/{sha_b}.mp3 "
            f"m audio/mp4 x {sha_a} x {sha_b}"
        )
        att = nostr_mod._parse_imeta_audio([["imeta", payload]])
        assert att is not None
        assert att.sha256 == sha_b
        assert att.url.endswith(f"{sha_b}.mp3")

    def test_malformed_odd_tokens_tolerated(self):
        att = nostr_mod._parse_imeta_audio([
            ["imeta", "garbage " + _imeta() + " trailing"],
        ])
        assert att is not None
        assert att.sha256 == BLOB_SHA

    def test_multiple_audio_imetas_all_returned_in_order(self):
        sha_a, sha_b = "ab" * 32, "cd" * 32
        all_atts = nostr_mod._parse_imeta_audio_all([
            ["imeta", _imeta(sha=sha_a, duration=10.0)],
            ["imeta", _imeta(sha=sha_b, duration=20.0)],
        ])
        assert [a.sha256 for a in all_atts] == [sha_a, sha_b]
        # singular helper returns the first
        first = nostr_mod._parse_imeta_audio(
            [["imeta", _imeta(sha=sha_a)], ["imeta", _imeta(sha=sha_b)]])
        assert first is not None
        assert first.sha256 == sha_a

    def test_optional_numeric_fields_absent(self):
        att = nostr_mod._parse_imeta_audio([["imeta", _imeta(size=None,
                                                             duration=None)]])
        assert att is not None
        assert att.size is None
        assert att.duration is None


# ---------------------------------------------------------------------------
# G-item 2: regex fallback (anti-exfil host allowlist)
# ---------------------------------------------------------------------------

class TestRegexFallback:
    def _find(self, content, relays=(RELAY,)):
        hosts, bases = nostr_mod._relay_hosts_and_bases(list(relays))
        return nostr_mod._find_audio_url_in_content(content, hosts, bases)

    def test_absolute_url_on_relay_host_matches(self):
        att = self._find(f"[voice note] https://{RELAY_HOST}/media/{BLOB_SHA}.m4a")
        assert att is not None
        assert att.sha256 == BLOB_SHA
        assert att.url == f"https://{RELAY_HOST}/media/{BLOB_SHA}.m4a"

    def test_relative_media_path_resolves_to_relay_base(self):
        att = self._find(f"check this /media/{BLOB_SHA}.mp3 out")
        assert att is not None
        assert att.url == f"https://{RELAY_HOST}/media/{BLOB_SHA}.mp3"

    def test_foreign_host_rejected(self):
        assert self._find(
            f"https://evil.example/media/{BLOB_SHA}.m4a") is None
        assert self._find(f"https://evil.example/x.mp3") is None

    def test_non_media_audio_url_not_matched(self):
        # Only /media/<64-hex>.<ext> paths carry the sha256 needed for the
        # BUD-11 x-scoped auth token, so nothing else is detected.
        assert self._find(f"https://{RELAY_HOST}/clips/foo.mp3") is None

    def test_plain_text_untouched(self):
        assert self._find("hello world, no audio here") is None

    def test_subdomain_is_not_the_relay_host(self):
        assert self._find(
            f"https://x.{RELAY_HOST}/media/{BLOB_SHA}.m4a") is None


# ---------------------------------------------------------------------------
# G-item 3: Blossom kind-24242 GET auth
# ---------------------------------------------------------------------------

class TestBlossomGetAuth:
    def test_event_shape_and_tags(self):
        event = nostr_mod._build_blossom_get_auth_event(TEST_PRIVKEY, BLOB_SHA)
        assert event["kind"] == 24242
        assert event["pubkey"] == TEST_PUBKEY
        tags = event["tags"]
        assert ["t", "get"] in tags
        assert ["x", BLOB_SHA] in tags
        exp = None
        for tag in tags:
            if tag[0] == "expiration":
                exp = int(tag[1])
        assert exp is not None
        now = int(time.time())
        assert now + 240 <= exp <= now + 360
        # canonical id + signature wiring
        expected_id = nostr_mod._compute_event_id(
            event["pubkey"], event["created_at"], 24242, tags, event["content"])
        assert event["id"] == expected_id
        # BIP-340 schnorr signatures are randomized (coincurve injects fresh
        # aux randomness on every sign_schnorr call), so the contract is
        # *verifiability*, not byte equality with a re-sign.
        assert len(event["sig"]) == 128
        assert int(event["sig"], 16) >= 0  # 64 bytes of hex
        # independent verification of the signature with pure-python
        # BIP-340 math (see _bip340_verify — this coincurve build lacks
        # PublicKey.verify_schnorr, so the suite carries its own checker)
        assert _bip340_verify(
            bytes.fromhex(TEST_PUBKEY),
            bytes.fromhex(event["sig"]),
            bytes.fromhex(event["id"]),
        ), "schnorr signature does not verify against the test key"

    def test_auth_header_is_unpadded_base64url(self):
        event = nostr_mod._build_blossom_get_auth_event(TEST_PRIVKEY, BLOB_SHA)
        header = nostr_mod._blossom_auth_header(event)
        decoded = _decode_auth_header(header)
        assert decoded["kind"] == 24242
        assert decoded["id"] == event["id"]


# ---------------------------------------------------------------------------
# G-item 4: download + failure modes (mocked httpx / STT)
# ---------------------------------------------------------------------------

class TestDownloadAndTranscribe:
    @pytest.mark.asyncio
    async def test_success_replaces_content(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        seen_t = _install_transcriber(monkeypatch, transcript="hello world")
        calls = _install_fake_httpx(
            monkeypatch,
            lambda url, headers: _FakeResponse(200, content=BLOB))
        tags = [["h", GROUP], ["imeta", _imeta(duration=42.0)]]
        await _process(adapter, _kind9_event("[voice note] ignored", tags))

        assert len(captured) == 1
        assert captured[0].text == "[voice note, 0:42] hello world"
        assert captured[0].message_type == MessageType.TEXT
        # request carried the Blossom auth token for the right blob
        assert len(calls) == 1
        assert calls[0]["url"] == f"https://{RELAY_HOST}/media/{BLOB_SHA}.m4a"
        decoded = _decode_auth_header(calls[0]["headers"]["Authorization"])
        assert ["x", BLOB_SHA] in decoded["tags"]
        # tempfile hygiene: 0600, correct suffix, real bytes, unlinked after
        assert len(seen_t) == 1
        assert seen_t[0]["mode"] == 0o600
        assert seen_t[0]["suffix"] == ".m4a"
        assert seen_t[0]["content"] == BLOB
        assert not os.path.exists(seen_t[0]["path"])

    @pytest.mark.asyncio
    async def test_f1_no_provider_no_fetch(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=False)
        calls = _install_fake_httpx(
            monkeypatch, lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F1_COPY}"
        assert calls == []  # never fetched

    @pytest.mark.asyncio
    async def test_f2_transcribe_failure_copy(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_transcriber(monkeypatch, ok=False)
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F2_COPY}"

    @pytest.mark.asyncio
    async def test_f3_imeta_size_gate_no_fetch(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        calls = _install_fake_httpx(
            monkeypatch, lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x",
            [["h", GROUP], ["imeta", _imeta(size=26214401)]]))
        assert captured[0].text == f"[voice note] {F3_COPY}"
        assert calls == []

    @pytest.mark.asyncio
    async def test_f3_content_length_gate_no_body(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        calls = _install_fake_httpx(monkeypatch, lambda url, headers:
            _FakeResponse(200, content=b"x" * 10,
                          headers={"content-length": "99999999"}))
        # regex fallback path (no imeta size) — gated on Content-Length
        await _process(adapter, _kind9_event(
            f"https://{RELAY_HOST}/media/{BLOB_SHA}.m4a"))
        assert captured[0].text == f"[voice note] {F3_COPY}"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_f4_hard_duration_reject_no_fetch(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        calls = _install_fake_httpx(
            monkeypatch, lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x",
            [["h", GROUP], ["imeta", _imeta(duration=301.0)]]))
        assert captured[0].text == f"[voice note] {F4_COPY}"
        assert calls == []

    @pytest.mark.asyncio
    async def test_f4_soft_warn_prefixes_transcript(self, monkeypatch):
        # thresholds configured so the soft warn is reachable:
        # hard cap 400s, soft warn 300s, clip is 350s.
        adapter, captured = _make_adapter(
            extra={"audio_max_seconds": 400, "audio_soft_seconds": 300})
        _provider(monkeypatch, available=True)
        _install_transcriber(monkeypatch, transcript="long talk")
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x",
            [["h", GROUP], ["imeta", _imeta(duration=350.0)]]))
        assert captured[0].text == f"[voice note, 5:50] {LONG_WARN} long talk"

    @pytest.mark.asyncio
    async def test_f9_multiple_clips_note_skipped(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_transcriber(monkeypatch, transcript="first clip")
        sha2 = "cd" * 32
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x",
            [["h", GROUP],
             ["imeta", _imeta(duration=10.0)],
             ["imeta", _imeta(sha=sha2, duration=20.0)]]))
        assert captured[0].text == "[voice note, 0:10] first clip (+1 more clips skipped)"

    @pytest.mark.asyncio
    async def test_f10_non_audio_imeta_passthrough(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=False)  # would F#1 if it detected
        sha = "cd" * 32
        content = f"[photo] https://{RELAY_HOST}/media/{sha}.png"
        tags = [["h", GROUP],
                ["imeta", f"url https://{RELAY_HOST}/media/{sha}.png m image/png x {sha}"]]
        await _process(adapter, _kind9_event(content, tags))
        assert captured[0].text == content  # untouched

    @pytest.mark.asyncio
    async def test_f5_401_auth_failure(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(401))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert F5_COPY in captured[0].text
        assert captured[0].text.startswith("[voice note]")

    @pytest.mark.asyncio
    async def test_f5_403_is_auth_failure_too(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(403))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert F5_COPY in captured[0].text

    @pytest.mark.asyncio
    async def test_f6_404_expired(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(404))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F6_COPY}"

    @pytest.mark.asyncio
    async def test_sha_mismatch_is_soft_failure(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        seen_t = _install_transcriber(monkeypatch, transcript="never")
        _install_fake_httpx(monkeypatch, lambda url, headers:
            _FakeResponse(200, content=b"tampered-bytes"))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F2_COPY}"
        assert seen_t == []  # never handed a mismatched blob to STT

    @pytest.mark.asyncio
    async def test_f2_fetch_exception_is_soft_failure(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)

        def _boom(url, headers):
            raise RuntimeError("connection reset")

        _install_fake_httpx(monkeypatch, _boom)
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F2_COPY}"

    @pytest.mark.asyncio
    async def test_f2_http_500_is_soft_failure(self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(500))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert captured[0].text == f"[voice note] {F2_COPY}"

    @pytest.mark.asyncio
    async def test_f5_keyless_adapter_cannot_sign_auth(self, monkeypatch):
        adapter, captured = _make_adapter()
        adapter._privkey = None
        _provider(monkeypatch, available=True)
        calls = _install_fake_httpx(
            monkeypatch, lambda url, headers: _FakeResponse(200, content=BLOB))
        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        assert F5_COPY in captured[0].text
        assert calls == []  # never attempted without a signing key

    def test_garbage_config_values_fall_back_to_defaults(self):
        adapter, _ = _make_adapter(extra={
            "audio_max_bytes": "lots",
            "audio_max_seconds": "forever",
            "audio_soft_seconds": None,
        })
        assert adapter._audio_max_bytes == nostr_mod._DEFAULT_AUDIO_MAX_BYTES
        assert adapter._audio_max_seconds == nostr_mod._DEFAULT_AUDIO_MAX_SECONDS
        assert adapter._audio_soft_seconds == nostr_mod._DEFAULT_AUDIO_SOFT_SECONDS


# ---------------------------------------------------------------------------
# G-item 7: prompt-cache safety + feature-flag gating
# ---------------------------------------------------------------------------

class TestCacheSafety:
    @pytest.mark.asyncio
    async def test_disabled_flag_is_pure_passthrough(self, monkeypatch):
        adapter, captured = _make_adapter(audio=False)

        async def _explode(content, tags):
            raise AssertionError("extractor must not run when audio_ingest=false")

        monkeypatch.setattr(adapter, "_maybe_extract_audio_attachment", _explode)
        content = f"[voice note] https://{RELAY_HOST}/media/{BLOB_SHA}.m4a"
        await _process(adapter, _kind9_event(content))
        assert captured[0].text == content  # byte-identical

    @pytest.mark.asyncio
    async def test_audio_then_text_same_sender_cache_invariant(
            self, monkeypatch):
        adapter, captured = _make_adapter()
        _provider(monkeypatch, available=True)
        _install_transcriber(monkeypatch, transcript="hello world")
        _install_fake_httpx(monkeypatch,
                            lambda url, headers: _FakeResponse(200, content=BLOB))

        snapshot = {k: v for k, v in vars(adapter).items()
                    if k.startswith("_audio_") or k in ("relays", "groups",
                                                       "config")}

        await _process(adapter, _kind9_event(
            "[voice note] x", [["h", GROUP], ["imeta", _imeta()]]))
        await _process(adapter, _kind9_event("just plain text"))

        assert len(captured) == 2
        audio_ev, text_ev = captured
        # The ONLY differences between the two routed events are the text
        # payload, the message id, and the raw platform event — everything
        # that feeds session keying / prompt construction is identical.
        assert audio_ev.source.__dict__ == text_ev.source.__dict__
        assert audio_ev.message_type == MessageType.TEXT
        assert text_ev.message_type == MessageType.TEXT
        assert audio_ev.platform_update_id == text_ev.platform_update_id
        # adapter state that feeds prompt construction is untouched
        after = {k: v for k, v in vars(adapter).items()
                 if k.startswith("_audio_") or k in ("relays", "groups",
                                                     "config")}
        assert after == snapshot

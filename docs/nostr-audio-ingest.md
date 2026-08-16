# Nostr audio ingest (voice notes)

Voice notes arrive as kind-9 messages with audio attachments hosted on the
group's relay (Blossom, `GET /media/<sha256>.<ext>`). The audio-ingest bridge
detects them, downloads the clip with a BUD-11 scoped auth token, transcribes
it via the configured STT provider, and replaces the message text with the
transcript before it reaches the model — so sessions stay text-only and
prompt-cache-safe. (Design: audio-bridge P1, task AB-2.)

## Enable

Default **off**. Turn it on under `platforms.nostr` in config.yaml:

```yaml
platforms:
  nostr:
    enabled: true
    relays: ["wss://relay.example"]
    groups: ["friends"]
    audio_ingest: true          # master flag (default false)
    audio_max_bytes: 26214400   # hard size cap, default 25 MB
    audio_max_seconds: 300      # hard duration cap, default 300 s (5 min)
    audio_soft_seconds: 600     # soft warn threshold (dormant while >= hard cap)
```

All keys are read **once at adapter init** (per-message config reads are
forbidden by the prompt-cache rule); garbage values fall back to the defaults
shown above.

## Detection (in priority order)

1. **NIP-92 `imeta` tags** with an audio MIME type (`m audio/*`) — url,
   sha256 (`x`), `size`, and `duration` all come from the tag.
2. **Regex fallback** over the message content: absolute or relative
   `/media/<64-hex>.<ext>` URLs. Relative paths resolve against the
   configured relays. Only hosts matching the configured relay list are
   honored (anti-exfiltration: a third-party URL pasted into a message never
   triggers a fetch).
3. Non-audio attachments (e.g. images) pass through untouched.

With multiple audio attachments in one message, the first is transcribed and
`(+N more clips skipped)` is appended.

## Fetch & transcription

- A single `GET` with `Authorization: Nostr <base64url event>` — a kind-24242
  Blossom auth event (`t=get`, `x=<blob sha256>`, ~5 min expiration) signed
  with the adapter's nsec. Keyless adapters never fetch.
- The sha256 of the response body must match the `x` hash / URL hash; a
  mismatch discards the blob without transcribing.
- The clip is written to a 0600 tempfile, transcribed through the bot's
  configured STT provider, and unlinked immediately.
- The transcript replaces the message text as `[voice note, m:ss]
  <transcript>`.

## Failure modes (user-facing replies)

| Case | Reply |
|---|---|
| No STT provider configured (F#1) | "I received an audio clip but can't transcribe it yet — tell Felix to enable transcription." (no fetch attempted) |
| Download/transcription failed (F#2) | "couldn't transcribe that, try again" |
| Clip too large, > `audio_max_bytes` (F#3) | "clip too large — keep voice notes under 25 MB" (no fetch when the size is known from imeta; otherwise gated on Content-Length/body size) |
| Clip too long, > `audio_max_seconds` (F#4) | "clip too long — keep voice notes under 5 minutes" (no fetch) |
| Fetch auth failed, HTTP 401/403 (F#5) | "I couldn't fetch that audio clip (authentication failed) — my relay membership may have lapsed; tell Felix." |
| Blob missing, HTTP 404 (F#6) | "that audio clip has expired" |

Durations above `audio_soft_seconds` (when configured below the hard cap)
prefix `(warning: long voice note)` to the transcript.

## Cache safety

The hook is a pure content transform inside `_process_event`: apart from the
message text, nothing differs between an audio message and a plain-text
message from the same sender — no adapter-state mutation, no per-message
config reads. Everything else that feeds session keying (sender, chat,
platform ids) is byte-identical, so the shared prompt cache stays valid.

## Rollback

Set `audio_ingest: false` (or remove the key) and restart the gateway:
messages then pass through byte-identical, with no fetches, signing, or STT
calls.

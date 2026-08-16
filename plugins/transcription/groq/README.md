# Groq Whisper Transcription Plugin (audio bridge)

Whisper **large-v3** on [Groq](https://console.groq.com) via raw REST
multipart — `POST https://api.groq.com/openai/v1/audio/transcriptions`.
Built for the P1 nostr audio bridge (voice notes in Buzz groups →
transcript → LLM), usable by anything that dispatches through
`tools.transcription_tools.transcribe_audio`.

## Selection & config (config.yaml — never env for behavior)

```yaml
transcription:
  provider: groq            # selects this backend (groq | whisper_cpp | local)
  api_key_env: GROQ_API_KEY # NAME of the env var holding the secret

stt:
  provider: groq_bridge     # dispatch key (see "Why groq_bridge" below)
```

The API key **value** lives in `.env` (secrets-only per repo rule):

```
GROQ_API_KEY=gsk_...
```

`transcription.api_key_env` just renames which env var is consulted, so
each tenant can hold its own key (`TENANT_A_GROQ_KEY`, …). The key is
read at call time — rotating it needs no restart.

## Why `groq_bridge` (not `groq`)

`groq` is a **built-in** STT provider name (native OpenAI-SDK
implementation in `tools/transcription_tools.py`). The plugin registry
rejects plugin names that shadow built-ins — *built-ins always win* — so
this backend registers as **`groq_bridge`**. Point `stt.provider` at
`groq_bridge` to dispatch here; `transcription.provider: groq` remains
the config value that *selects* the backend (P1 design §D).

Differences from the built-in `groq`: REST multipart (no SDK), model
pinned to `whisper-large-v3` (not turbo), 60 s timeout, exactly one
retry on HTTP 5xx, key-env indirection via config.

## Semantics

| case | behavior |
|---|---|
| HTTP 200 | `{"success": true, "transcript": text, "provider": "groq_bridge"}` |
| HTTP 5xx | one retry; second failure → error envelope |
| HTTP 4xx | no retry, error envelope with status + body |
| timeout (60 s) | no retry, error envelope |
| key env unset | no HTTP call, error envelope naming the env var |
| `transcription.provider` unknown value | plugin load logs a warning, registers nothing (bot replies fallback text) |
| `transcription` section absent | registers nothing, quiet |

Failures surface as the standard error envelope, never an exception —
the audio bridge maps them to failure-mode F#2 ("couldn't transcribe
that, try again").

## Trust

Third-party SaaS STT: plaintext audio leaves the host toward Groq.
Approved for the friends-relay P1 deployment; use the
[whisper_cpp](../whisper_cpp/) plugin when audio must stay on-host.

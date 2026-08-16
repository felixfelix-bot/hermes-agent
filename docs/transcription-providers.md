# Transcription Providers (audio bridge config)

Per-tenant speech-to-text backends for the nostr audio bridge (P1 design
§D). Two bundled plugins ship in `plugins/transcription/`:

| backend | plugin dir | registers as | audio stays local | cost |
|---|---|---|---|---|
| Groq Whisper large-v3 (REST) | `plugins/transcription/groq/` | `groq_bridge` | no (SaaS) | ~$0.03/min |
| whisper.cpp (`whisper-cli`) | `plugins/transcription/whisper_cpp/` | `whisper_cpp` | yes | free (CPU) |
| built-in local/others | `tools/transcription_tools.py` | n/a (built-ins) | yes | free |

## Config schema (`config.yaml` — behavior lives in config, secrets in `.env`)

```yaml
transcription:
  provider: groq              # groq | whisper_cpp | local   (default: unset → built-ins)
  api_key_env: GROQ_API_KEY   # groq only: NAME of the env var holding the API key
  whisper_model: /opt/whisper/ggml-base.bin   # whisper_cpp only: ggml model path

stt:
  provider: groq_bridge       # dispatch key consumed by transcription_tools
```

- `transcription.provider` **selects** the backend at plugin load. Each
  bundled plugin self-gates: only the selected one registers.
- `stt.provider` is the **dispatch** key (`transcribe_audio()` routes on
  it). Plugin dispatch keys: `groq_bridge`, `whisper_cpp`. (The groq
  plugin cannot register as `groq` — that name is reserved by the
  built-in; built-ins always win.)
- `transcription.provider: local` (or section absent) → no plugin
  registers; built-in STT (`stt.provider: local`, …) applies.

## Load-time validation

If `transcription.provider` holds a typo (`deepgram`, `groqq`, …), both
plugins log a warning at load and register **nothing** — the bridge then
replies with the F#1 fallback text ("…can't transcribe it yet — tell
Felix to enable transcription") instead of crashing. A missing section is
silence, not a warning: unconfigured means "use built-ins".

## Secrets

The API key **value** never goes in config.yaml. `.env` holds it under
the env var **named** by `transcription.api_key_env` (default
`GROQ_API_KEY`); per-tenant installs can point it at e.g.
`SITARANI_GROQ_KEY`. Read at call time — rotation needs no restart.

## whisper.cpp ops prerequisite

`whisper-cli` on PATH + ggml model bin provisioned separately (default
path `/opt/whisper/ggml-base.bin`) — commands in
`plugins/transcription/whisper_cpp/README.md`. `is_available()` is False
until both exist.

## See also

- `plugins/transcription/groq/README.md` — semantics tables (retry,
  timeout, error envelopes)
- `plugins/transcription/whisper_cpp/README.md` — subprocess contract
- `docs/nostr-audio-ingest.md` (AB-2) — the consumer: failure modes F#1–F#2
- `agent/transcription_provider.py` — the `TranscriptionProvider` ABC

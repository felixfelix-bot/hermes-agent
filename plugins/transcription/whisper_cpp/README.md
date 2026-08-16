# Whisper.cpp Transcription Plugin (audio bridge)

Local [whisper.cpp](https://github.com/ggml-org/whisper.cpp) backend —
audio **never leaves the host**. Shells out to `whisper-cli`:

```
whisper-cli -f <audio-file> -nt -m <model-path>
```

`-nt` = no timestamps, so stdout is the bare transcript (whisper.cpp
log spam goes to stderr and is only surfaced on failure).

## Selection & config (config.yaml — never env for behavior)

```yaml
transcription:
  provider: whisper_cpp                          # selects this backend
  whisper_model: /opt/whisper/ggml-base.bin      # ggml model path

stt:
  provider: whisper_cpp                          # dispatch key (no built-in collision)
```

## OPS NOTE — the model binary is NOT shipped with this plugin

Provision it separately on the host (here: VPS2):

```sh
mkdir -p /opt/whisper
curl -L -o /opt/whisper/ggml-base.bin \
    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
chmod 0644 /opt/whisper/ggml-base.bin
# base ≈ 148 MB (fast); small ≈ 488 MB (better multilingual accuracy)
```

The `whisper-cli` binary must be on `PATH` (distro package where
available, otherwise build from source). `is_available()` reports False
until **both** the binary and the model file exist; dispatch through
`transcribe_audio` then returns a clean "plugin not available" envelope
instead of a stack trace.

## Semantics

| case | behavior |
|---|---|
| exit 0 | transcript = stdout, stripped (`""` for silence — caller phrases it) |
| exit ≠ 0 | error envelope with exit code + stderr (first 500 chars) |
| timeout (120 s) | error envelope naming timeout + model path |
| binary missing | error envelope with install hint (see above) |
| `transcription.provider` unknown value | plugin load logs a warning, registers nothing (bot replies fallback text) |
| `transcription` section absent | registers nothing, quiet |

Latency: ~1–2× realtime on 1 vCPU (a 30 s clip ≈ 30–60 s). The 120 s
timeout covers clips up to roughly a minute; raise it for longer voice
notes.

No secrets, no network. Companion cloud backend:
[groq](../groq/) (`transcription.provider: groq`).

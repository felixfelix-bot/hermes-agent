# Request attribution (`X-Hermes-Session`)

The loopback zai-proxy (`~/.hermes/bot/zai_proxy.py`) stamps
`api_calls.session_id` from the `X-Hermes-Session` request header, which is how
token burn is attributed to a session, profile, or task. Rows written without it
land as `caller='ua:OpenAI/Python 2.24.0'` with `session_id=NULL` — spend that
no per-session report and no productivity gate can see.

## Contract

| Path | How the header is set |
|------|----------------------|
| Main agent turn | `plugins/model-providers/zai` `build_api_kwargs_extras` hook, loopback-only |
| Auxiliary calls (compression, title, vision, memory) | same hook, fed with the **context-local** session id from `set_runtime_main` by `agent/auxiliary_client._build_call_kwargs`, plus `_session_attribution_headers` as a belt-and-braces fill |

Both paths resolve the id from the context-local runtime
(`agent/auxiliary_client._RUNTIME_MAIN_CONTEXT`), never from the process-wide
`HERMES_SESSION_ID` export. A gateway process serves many sessions at once, so a
whole-process value is either absent or belongs to whichever session exported it
last — that is precisely how auxiliary traffic became unattributable.

Helper `extra_headers` merges are non-destructive
(`_apply_attribution_headers`): a caller passing its own headers (e.g. Copilot's
`x-initiator: user`, see #60293) merges instead of overwriting, so the session
header survives retries and rebuilt-client paths.

## Safety: loopback only

The header is an internal attribution channel and must never leave the machine
(design doc §7 risk #5). Both the profile hook and the auxiliary fill attach it
only when the *effective* endpoint host is literal loopback
(`localhost` / `127.0.0.1` / `::1`). A schemeless URL is normalised so it cannot
slip past the guard, and unparseable input counts as non-loopback — fail closed.
An unattributed call is always preferable to a leaked one.

## Regression guard

`tests/agent/test_auxiliary_session_attribution.py` pins the contract: the header
reaches `_build_call_kwargs` output on loopback, is never sent to a remote
endpoint, never gets dropped by caller `extra_headers`, and matches the name the
proxy reads. The provider-side half is covered by
`tests/plugins/model_providers/test_zai_session_attribution.py`.

## Background

Attribution went live 2026-08-16, was reset away by a fork sync on 2026-09-04,
and was re-landed as the profile hook. The auxiliary half stayed broken because
it depended on the env fallback: measured 2026-10-03/04 at ~2 600 calls /
213 Mtok over 6 h with `session_id=NULL`, avg 81k-token prompts, 97 % cached.

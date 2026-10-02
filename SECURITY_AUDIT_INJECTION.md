# Hermes Agent — Prompt Injection Guardrails Audit
**Date:** 2026-07-02 (re-landed 2026-09-11 via t_b36e7d7f from stash `8c58d6a8a3`)
**Scope:** Hermes↔Plebeian Market interaction surface

## Risk Summary

| Vector | Risk | Status |
|--------|------|--------|
| Signal admin filter + buffer | LOW | Solid — non-admin messages never reach LLM |
| Nostr events | LOW | Hermes core doesn't read Nostr events |
| Cron job prompts | LOW | All operator-authored |
| Kanban task bodies | MEDIUM | Enter LLM as instructions, 8KB truncated |
| gh CLI / git diff | MEDIUM | Untrusted contributor content unfiltered |
| GitHub webhooks | HIGH→MITIGATED | See t_d7bfe502 resolution below |
| Browser/scraping output | HIGH→MITIGATED | See t_b616c06a resolution below |

## Recommended Fixes (kanban tasks created)

### t_d7bfe502 — Webhook Sanitization (HIGH) — RESOLVED
Wrap external content (PR titles, issue bodies) in UNTRUSTED markers.
Strip instruction-like patterns before entering agent prompt.

**Resolution (2026-08-15 re-land):** `gateway/platforms/webhook.py` now runs
every value resolved from an external payload through `_sanitize_untrusted`
(neutralizes chat-template escape tokens — `<|im_start|>`, `[INST]`,
`</system>` — instruction-override phrases, and our own `<untrusted>`
delimiter into a visible `[BLOCKED]` marker) before it is interpolated into a
prompt. In agent mode (the default) the *rendered prompt as a whole* is
additionally enclosed in a single `<untrusted>…</untrusted>` frame with a
preamble instructing the model to treat everything inside it strictly as data.
Framing the rendered prompt once (rather than wrapping each substituted value)
keeps legitimate template prose readable — `"Action: opened, PR: 7"` instead of
`"Action: <untrusted>opened</untrusted>, PR: <untrusted>7</untrusted>"` — and
costs nothing in coverage: every substituted token is payload-derived, so there
is nothing an attacker can reach outside the frame. `deliver_only` routes and
`_render_delivery_extra`
render with `wrap_untrusted=False` — those outputs are user-facing messages
or delivery routing parameters, not agent prompts — but sanitization still
applies (defense-in-depth). The regex layer is justified here (unlike the
tool-result path) because webhook values are spliced into a *user-role*
prompt where framing delimiters alone cannot neutralize chat-template tokens.

### t_b616c06a — Browser Output Sanitization (HIGH) — RESOLVED
Add content sanitizer between browser_navigate/snapshot output and LLM.
Wrap page content in UNTRUSTED markers.

**Resolution (2026-07-02):** All four seams where scraped/page content
reaches an LLM now apply the codebase's established framing defense
(`<untrusted_tool_result>` / `BEGIN UNTRUSTED CONTENT` delimiters — the
*architectural* approach, not brittle regex pattern-stripping which the
codebase explicitly rejects):

1. **Main agent path** (pre-existing, committed): `make_tool_result_message`
   → `_maybe_wrap_untrusted` wraps `browser_*` / `web_*` / `mcp_*` results.
2. **Auxiliary LLM — browser snapshot extraction** (attempt 1):
   `tools/browser_tool.py::_extract_relevant_content` frames the snapshot via
   `frame_untrusted_content` before the extraction model sees it.
3. **MCP transport → Codex seam** (this run): the `codex_app_server` runtime
   exposes Hermes tools over stdio MCP; Codex builds its own tool-result
   messages from the raw strings this server returns and does NOT apply
   Hermes' wrapper. `agent/transports/hermes_tools_mcp_server.py::_dispatch`
   now frames browser/web/mcp results via `_frame_untrusted_mcp_result`
   before they cross the MCP boundary.
4. **Auxiliary LLM — web_extract summarizer** — NOT re-landed, seam is gone:
   the stash (`8c58d6a8a3`) also framed
   `tools/web_tools.py::_call_summarizer_llm`. That function does not exist on
   main any more: `web_extract_tool` now returns provider-extracted content
   directly and performs **no** LLM summarization (see its docstring), so
   there is no secondary model on that path to inject into. The surviving
   `web_extract` flow reaches the model as a normal tool result and is
   therefore covered by seam 1. Nothing to carry over.

Tests: 27 new — 7 in `tests/agent/transports/test_hermes_tools_mcp_server.py`,
2 in `tests/tools/test_browser_tool_untrusted.py` (added during the re-land;
the original stash had no direct browser-seam coverage), and 18 in
`tests/gateway/test_webhook_adapter.py` — all passing.

## Cold-review round (2026-09-11, t_b36e7d7f)

The re-landed series was put through a COLD cross-family review (kimi-k2.7-code,
no shared context with the implementer). Verdict: **NO-GO** with three blocking
findings. All three were real defects in the re-land, not false alarms, and all
three are fixed in follow-up commits on the same branch:

| Finding | Disposition |
|---------|-------------|
| MCP boundary failed **open**: `_frame_untrusted_mcp_result` returned the raw result on any exception from the shared framer | Fixed — the exceptional path now logs a warning and applies an equivalent *local* frame with defanged delimiters. Least-framed content still carries the untrusted marker; the call still succeeds |
| Auxiliary frame did **not defang its own delimiters**: content containing `END UNTRUSTED CONTENT` could close the trust boundary early | Fixed — embedded `BEGIN/END UNTRUSTED CONTENT` markers have their spaces rewritten to hyphens (`_neutralize_aux_delimiters`), the same neutralize-don't-delete approach the tool-result path uses |
| Reported broken assertion in `test_sanitize_strips_chat_template_tokens` | **Not reproducible** — a diff-rendering artifact of the reviewer's input: the angle-bracket/piped token (`<|im_end|>`) survives into the review prompt as bare `end_of_text`. The committed assertion is `assert "<|im_end|>" not in _sanitize_untrusted("end<|im_end|>")` and passes |

Non-blocking findings were also acted on: the browser seam now imports the
shared framer lazily and falls back to a local frame if the import fails (the
previous top-level import broke a module-stub test); the webhook preamble now
describes the single outer frame instead of per-value markers; and the review's
coverage gaps (exception path, delimiters embedded in scraped text, the frame
itself) are now pinned by tests in `tests/agent/test_auxiliary_untrusted_framing.py`,
`tests/tools/test_browser_tool_untrusted.py`, and
`tests/agent/transports/test_hermes_tools_mcp_server.py`.

Known limits, deliberately not fixed here (documented rather than silently
claimed away): the webhook regex layer is *defense-in-depth only* — it matches
literal patterns, so encoded/novel forms evade it, and it will replace benign
text (`"You are now a member"`, `"system: foo"`) with `[BLOCKED]`. Framing is
the real control at every seam; the regex exists because webhook values are
spliced into a *user-role* prompt, where delimiters alone cannot neutralize
chat-template tokens. Entity/Unicode normalization is out of scope for this
card and tracked separately.

### t_e9285c64 — Fix 4 flaky e2e tests (Plebeian)
### t_18b6f82f — Fix 26 failing e2e-infra smoke tests (Plebeian)

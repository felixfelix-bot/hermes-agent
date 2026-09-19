# Stable-first prompt ordering & byte-stability (T4)

Cost-reduction-sprint task **T4** (`t_1c18af98`). This doc records the ordering
invariant that keeps provider prompt caches warm, the per-lane measurement that
says how much of the win is still on the table, and the tests that guard it.

## Why ordering matters

A provider prompt cache only reuses a prefix that is **byte-identical** across
turns. At the time of writing the proxy's own DeepSeek price table
(`zai_proxy.py`, `DEEPSEEK_PRICES`) prices

| lane | input $/M | cached input $/M | ratio |
|------|-----------|------------------|-------|
| `deepseek/deepseek-flash` | 0.14 | 0.03 | **4.7x** |
| `deepseek/deepseek-v4-flash` | 0.14 | 0.03 | 4.7x |
| `deepseek/deepseek-v4-pro` | 1.00 | 0.10 | 10x |

so a single volatile byte rendered **above** the stable scaffold does not cost
one line — it costs the whole prefix, on every rebuild, at 4.7-10x the cached
rate. (NeuralWatt bills real prefill compute, so the same byte costs wall-clock
there.) The z.ai *quota* lanes are sunk-cost and unaffected.

## The three-tier layout

`agent/system_prompt.py` assembles the system prompt as three ordered cache
tiers, joined with `\n\n`:

| Tier       | Content                                                              | Stability                |
|------------|----------------------------------------------------------------------|--------------------------|
| `stable`   | identity (SOUL.md / DEFAULT_AGENT_IDENTITY), tool guidance, computer-use, nous subscription, tool-use enforcement, model operational guidance, environment hints, coding posture, platform hints | cross-session byte-stable |
| `context`  | caller `system_message`, context files (AGENTS.md / .cursorrules), coding-workspace snapshot | session-stable |
| `volatile` | skills index, memory snapshot, USER.md profile, external memory block, timestamp line | may differ on rebuild    |

`build_system_prompt()` joins `stable + "\n\n" + context + "\n\n" + volatile`,
stores the stable tier on `agent._cached_system_prompt_static`, and caches the
whole string on `agent._cached_system_prompt` for the lifetime of the AIAgent —
rebuilt only after context compression. `reconstruct_static_prefix()` rebuilds
just the stable tier on session restore (and only when the stored prompt
literally starts with it), so the two-block `[static, volatile]` wire layout
survives a restore.

## Byte-stability rules

1. **Stable content first.** Identity, tool/memory/skills guidance and
   instructions render before any volatile state.
2. **Volatile state last.** Skills index, memory snapshot, USER.md, external
   memory block and the timestamp line are appended after the stable prefix.
3. **Timestamp is date-only** (`%A, %B %d, %Y`), not minute-precision, so the
   prompt is byte-stable for a full day; the agent can query exact wall-clock
   time with tools when it needs it.
4. **Skills index leads the volatile band.** Skills are runtime-mutable (the
   agent patches them mid-session). With the index at the FRONT of the volatile
   band, an unchanged index still falls inside the reused prefix on an implicit
   longest-prefix backend, and a changed one only re-prefills from there on.
5. **Ephemeral per-turn content is appended at API-call time**, after the cached
   system prompt (`agent/chat_completion_helpers.py`), so it never enters the
   cached/stored prompt.

## Per-lane measurement (the CONDITION on this card)

The card made this work conditional: *"only worth doing on lanes that actually
discount cached input … Measure the per-lane hit rate first."* Measured on the
live proxy DB (`~/.hermes/bot/zai_usage.db`, 7-day window ending 2026-09-19,
`cached_tokens` is the T4 instrumentation from Part A):

```sql
SELECT key_name, COUNT(*) calls, SUM(prompt_tokens) pt, SUM(cached_tokens) ct,
       ROUND(100.0*SUM(cached_tokens)/NULLIF(SUM(prompt_tokens),0),1) pct
FROM api_calls WHERE ts > strftime('%s','now')-7*86400
GROUP BY key_name ORDER BY pt DESC;
```

| key / lane | calls | prompt tokens | cached tokens | hit rate | cost $ (7d) |
|------------|-------|---------------|---------------|----------|-------------|
| `deepseek` (direct, discounted) | 49,821 | 4,928,647,502 | 4,796,698,459 | **97.3%** | 177.46 |
| `ollama_cloud` | 14,022 | 793,755,393 | 710,761,939 | 89.5% | 49.66 |
| `neuralwatt` (prefill-priced) | 8,224 | 733,294,141 | 673,875,632 | 91.9% | 5.91 |
| `ours` (z.ai quota, sunk cost) | 17,240 | 578,984,134 | 541,520,824 | 93.5% | 0.00 |
| `routstrd` | 2,921 | 167,377,722 | 164,809,864 | 98.5% | 89.47 |

Per-model on the two lanes that actually bill the 4.7x delta:

| model | calls | prompt tokens | hit rate | uncached prompt tokens |
|-------|-------|---------------|----------|------------------------|
| `deepseek/deepseek-flash` | 40,543 | 3,988,225,162 | 96.9% | 122,727,336 |
| `deepseek/deepseek-v4-flash` | 13,821 | 1,432,862,010 | 96.9% | 43,791,681 |

**Verdict.** The discounted lanes are already at ~97% cached prompt tokens, i.e.
the stable-first ordering is in place and saturated. Quantifying the two sides:

- already harvested by prefix reuse on those two lanes: `(3.866e9 + 1.389e9) /
  1e6 * (0.14 - 0.03) ≈ $578 / 7 days` (≈ $83/day) against the same tokens
  billed at the uncached rate;
- still uncached: `166.5M` prompt tokens / 7 days ≈ **$23/week** at the uncached
  rate — and that residue is overwhelmingly the genuinely NEW tail of each call
  (the newest user turn and its tool results), which no reordering can cache.

So **no reordering of the assembly is warranted**, and none is made here: the
ordering was already implemented on `main` by `9fdadf0cd7` (cache static
prefixes), `9b9cbdd7eb` (move skills index to the volatile band) and
`6a9340d40a` (date-only timestamp). What was missing was a **guard** — nothing
failed if the tiers were ever re-ordered again. That is what this change adds,
plus this record of the measurement. Two lanes with a low/zero reported rate are
provider-side, not ordering: `qwen3.5:397b` on `ollama_cloud` reports no cache
stats at all (313 calls, 0 cached, $0.55/7d) and `ours`/z.ai quota lanes are
sunk-cost.

## Verification

`tests/agent/test_system_prompt.py::TestLongestPrefixStability` guards the
invariant with a controllable volatile tail (skills index patched, memory/USER/
external-memory blocks present, frozen clock):

- `test_stable_prefix_byte_identical_when_volatile_tail_moves` — two rebuilds
  whose volatile tail genuinely differs (skills patched + day rolled) still share
  a byte-identical stable tier. The tail-differs assertion keeps it non-vacuous.
- `test_full_prompt_keeps_the_identical_leading_prefix` — the two full prompts
  share their first `len(stable)` bytes: the provider-visible cached prefix.
- `test_band_order_is_stable_then_context_then_volatile` — the join is exactly
  `stable` then `context` then `volatile`.
- `test_stable_band_carries_no_volatile_state` — skills/memory/USER/external
  memory/timestamp/session-model-provider-platform lines appear in the volatile
  band only, never in the stable band.
- `test_timestamp_is_date_only_so_one_day_is_byte_stable` — 00:05 and 23:55 on
  the same day produce identical volatile bytes; a rebuild mid-day still hits.
- `test_volatile_tail_moves_only_at_the_date_boundary` — the day roll does move
  the tail, so the assertion above is not passing by accident.

RED is reconstructed in two ways because the ordering itself predates this card
(script: `t4-red-evidence.sh`, transcript `t4-red-evidence.txt`):

| phase | source under test | result |
|-------|-------------------|--------|
| RED-1 | `agent/system_prompt.py` at `9b9cbdd7eb^` (skills still in the stable band) | 3 failed, 3 passed |
| RED-2a | current source mutated to render the timestamp line inside the stable band | 4 failed, 2 passed |
| RED-2b | current source mutated to a minute-precision timestamp | 1 failed, 5 passed |
| GREEN | untouched source (md5 unchanged after the mutations were reverted) | 6 passed |

Regression suites run for this change (same worktree, main-checkout interpreter,
which resolves the worktree's own `agent/` package):

| run | result | failed set |
|-----|--------|------------|
| `tests/agent` at HEAD (`6f7101a593`, base worktree) | 3944 passed, 116 failed | pre-existing |
| `tests/agent` with this change | 3950 passed, 116 failed | byte-identical to base |

The 116 failures are pre-existing on `main` in this environment (dotfiles/HOME
guards, network-cached metadata probes, and two `test_system_prompt.py` tests
that pass in isolation but are order-sensitive in the full run — all identical
at base, and none in the new class). The `+6` passed is exactly
`TestLongestPrefixStability`.

The restore path is additionally guarded by
`tests/agent/test_system_prompt_restore.py::TestPromptStabilityInvariant`
(restored prompt must equal stored bytes exactly) and
`TestStaticPrefixReconstructionOnRestore` (static prefix reconstructed only when
the stored prompt literally starts with it).

## Instrumentation (Part A)

The proxy side (`hermes-bot` repo, branch `worker-admin/cached-tokens`) persists
the upstream cached-token split to `zai_usage.api_calls.cached_tokens` so cost
analytics can price cached vs uncached prompt tokens differently; see
`docs/cached-tokens-instrumentation.md` in that repo. The measurement table
above is a consumer of that column — the same data source QS-6 (`t_98711530`)
was opened to expose.

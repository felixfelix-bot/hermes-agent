# Stable-first prompt ordering & byte-stability (T4)

Cost-reduction-sprint task T4 (t_1c18af98). This doc records the design
invariant that keeps provider prompt caches warm and the verification that
guards it.

## Why ordering matters

Provider prompt caches only hit when consecutive turns share a **byte-
identical prefix**:

- DeepSeek discounts cached prefixes ~10x.
- NeuralWatt charges real compute for prefill.

If volatile state (dates, memory %, quota numbers) sat near the TOP of the
system prompt, the prefix would churn every turn, the cache would never hit,
and the full prefill would be paid on every call.

## The three-tier layout

`agent/system_prompt.py` assembles the system prompt as three ordered cache
tiers, joined with `\n\n`:

| Tier      | Content                                                              | Stability |
|-----------|----------------------------------------------------------------------|-----------|
| `stable`  | identity (SOUL.md / DEFAULT_AGENT_IDENTITY), tool guidance, computer-use, nous subscription, tool-use enforcement, model operational guidance, environment hints, coding posture, platform hints | cross-session byte-stable |
| `context` | caller `system_message`, context files (AGENTS.md / .cursorrules), coding-workspace snapshot | session-stable |
| `volatile`| skills index, memory snapshot, USER.md profile, external memory block, timestamp line | may differ on rebuild |

The joined prompt is cached on `agent._cached_system_prompt` for the lifetime
of the AIAgent and only rebuilt after context compression. The stable tier is
additionally cached on `agent._cached_system_prompt_static` and reconstructed
on session restore (`reconstruct_static_prefix`) so the two-block
`[static, volatile]` wire layout survives restore.

## Byte-stability rules

1. **Stable content FIRST.** Identity, skills guidance, memory guidance,
   instructions all render before any volatile state.
2. **Volatile state LAST.** Skills index, memory snapshot, USER.md, external
   memory block, and the timestamp line are appended after the stable prefix.
3. **Timestamp is date-only** (`%A, %B %d, %Y`), not minute-precision, so the
   prompt is byte-stable for the full day. The model can query exact wall-clock
   time via tools when it needs it.
4. **Skills index leads the volatile band.** Skills are runtime-mutable (the
   agent patches them mid-session). Rendering them at the FRONT of the volatile
   band means an unchanged index still falls inside the reused prefix on an
   implicit longest-prefix backend; a changed one only re-prefills from there.
5. **Ephemeral per-turn content is appended at API-call time**, after the
   cached system prompt, so it never enters the cached/stored prompt.

## Verification

`tests/agent/test_system_prompt.py::TestLongestPrefixStability` guards the
invariant:

- `test_stable_prefix_byte_identical_across_builds` — two builds share a
  byte-identical stable prefix.
- `test_volatile_tail_can_differ_without_touching_stable` — a volatile-tail
  change does not propagate into the stable prefix; the full joined prompt
  still starts with the identical stable prefix.
- `test_stable_prefix_is_leading_not_trailing` — the stable tier is the
  PREFIX of the joined prompt, not interleaved after volatile content.

The restore path is additionally guarded by
`tests/agent/test_system_prompt_restore.py::TestPromptStabilityInvariant`
(restored prompt must equal stored bytes exactly) and
`TestStaticPrefixReconstructionOnRestore` (static prefix reconstructed only
when the stored prompt literally starts with it).

## Instrumentation (Part A)

The proxy side (`hermes-bot` repo, branch `worker-admin/cached-tokens`)
persists the upstream cached-token split to `zai_usage.api_calls.cached_tokens`
so cost analytics can price cached vs uncached prompt tokens differently. See
`docs/cached-tokens-instrumentation.md` in that repo.

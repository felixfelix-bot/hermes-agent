# PROGRESS — state.db write patience (pr/state-write-patience)

## Status: SAVEPOINT — partial, DO NOT MERGE

Worker `deleg_0186f694` died with HTTP 503 after 47 api_calls / 42 min.
Its work survived UNCOMMITTED in this worktree; salvaged by the manager.

## What is DONE (verified by manager)

`hermes_state.py` +102/-2 — real, substantial, and correct in shape:

- `_WRITE_PATIENCE_S` 20.0 -> 45.0, `_TRANSCRIPT_WRITE_PATIENCE_S` 60.0 -> 180.0
- `_PATIENCE_MAX_S = 900.0` upper clamp (typo protection: `1e12` cannot wedge writers)
- `_coerce_patience(raw, default, label)` — rejects non-numeric / non-finite /
  non-positive / absurd values, logs, returns default. NaN takes this path
  because `0 < NaN` is False.
- `_resolve_write_patience()` — reads `database.write_patience_s` /
  `database.transcript_write_patience_s` from config.yaml via
  `hermes_cli.config.cfg_get` + `load_config_readonly`, best-effort (any config
  failure falls back to shipped defaults). Enforces
  `transcript = max(transcript, routine)`.
- `__init__` resolves the pair ONCE into INSTANCE attributes, which shadow the
  class defaults so all **21** budget-reading call sites honour config.yaml
  without touching them.

- `python3 -m py_compile hermes_state.py` -> SYNTAX_OK

## What is BROKEN / INCOMPLETE (verified by manager)

1. **REGRESSION — breaks the repo's own test.**
   `pytest tests/state/test_write_lock_patience.py` -> **1 failed, 5 passed**:

   ```
   tests/state/test_write_lock_patience.py:96:
   Failed: DID NOT RAISE OperationalError
   FAILED tests/state/test_write_lock_patience.py::TestTranscriptWritePatience::
          test_exhausted_patience_names_the_real_cause
   ```

   **Cause:** that test does `monkeypatch.setattr(SessionDB, "_WRITE_PATIENCE_S", 0.2)`
   (line 87) — patching the **CLASS** attribute. The new `__init__` resolves
   **INSTANCE** attributes from config, which then shadow the patched class
   value, so the writer waits out the 2 s hold instead of raising.

   So instance-attribute shadowing is a genuine design flaw, not just a test
   problem: it defeats ALL class-level overrides, including the repo's own.

   **Fix direction (choose one, justify in the PR):**
   (a) only set the instance attribute when config actually supplies a value
       (leave `None` -> fall through to the class attribute), or
   (b) have `_execute_write` read config-aware lazily, or
   (c) keep instance attrs but make the resolution consult the CURRENT class
       attribute as its default rather than capturing it at import/init time.

2. **DEAD CODE — the open path is not wired.**
   `self._open_patience_s` is SET at `hermes_state.py:2755` but **never read**
   anywhere (`grep -n _open_patience_s` -> line 2755 only). The diff claims
   "a failed open rides the transcript budget", but the open path
   (`_connect_and_init_with_lock_patience`, ~line 2966) does not consume it.
   Either wire it or delete the attribute + the `_OPEN_PATIENCE_IS_TRANSCRIPT`
   flag + the comment claiming it.

3. **NO NEW TEST.** The brief required a regression test that holds a write lock
   and asserts the second writer WAITS and then SUCCEEDS, which MUST fail on
   current head. None was added (`git ls-files -o` empty; only hermes_state.py
   modified). Also owed: a test asserting the config knob's default, and
   coverage for `_coerce_patience` junk/NaN/clamp paths.

4. **UNCOMMITTED + no PR.** Nothing was committed or pushed by the worker.

5. **NOT assessed:** whether bounding the WAL (`wal_autocheckpoint` /
   `journal_size_limit`) belongs in this change — measured `wal_checkpoint(PASSIVE)`
   = `0|2607|590`, i.e. only 22% of frames fold. Report it with evidence either way.

## Next steps (in order)

1. Fix the class-vs-instance override regression (item 1) — prove with the
   existing test green again AND a new test that a config-supplied value IS
   honoured.
2. Wire or delete `_open_patience_s` (item 2).
3. Add the required regression test (fails on head, proven by stashing).
4. Decide + evidence the WAL question (item 5).
5. Run the repo's relevant suites; paste output.
6. Push to the `fork` remote; open a `fix(...)` PR; do NOT merge.

## Evidence / paths

- Worktree: `/home/c03rad0r/worktrees/state-write-patience` (branch `pr/state-write-patience`)
- Diff: `git -C <wt> diff HEAD -- hermes_state.py` (+102/-2)
- Failing test output: `pytest-output.txt` (this dir)
- Venv: `/home/c03rad0r/.hermes/hermes-agent/venv/bin/python`
- Remotes: `origin` = NousResearch/hermes-agent, `fork` = felixfelix-bot/hermes-agent
- Worker transcript: `~/.hermes/profiles/manager/cache/delegation/live/deleg_0186f694/task-0.log`

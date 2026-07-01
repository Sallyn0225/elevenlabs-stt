# Batch transcription with pool-aware allocation & top-up registration

## Goal

Let one `stt transcribe a.mp3 b.mp3 c.mp3 …` run transcribe **many** audio files
in a single invocation. Before touching the network, compute each file's credit
cost, **allocate** the files across the existing account pool (packing multiple
files into one account when its remaining quota allows), **register only the
shortfall** of new accounts, then transcribe — existing accounts first, newly
registered accounts last. Print the allocation plan up-front (and support
`--dry-run`), run files sequentially, skip failures and continue, and print a
success/failure summary at the end.

Target: point the CLI at a folder-worth of clips and walk away — it figures out
how many free accounts it needs, mints exactly that many, and produces one
subtitle file per clip.

## Background / Current State

Builds directly on the shipped account-pool system (task
`07-01-account-pool-auto-register`). Today `stt transcribe` handles **one** file:

- `estimate_required(duration)` → per-file credits from ffprobe duration
  (`CREDITS_PER_SEC`), or `None` if duration unknown (`stt.py:519`).
- `select_account(accounts, required, margin, store)` → best-fit + margin: the
  smallest-remaining account that covers `required × margin` (`stt.py:700`).
- If none sufficient and temp-email is configured → `register_one()` mints one
  account on demand (`stt.py:575`); otherwise best-effort on the active account.
- After the run: `account_remaining(force=True)` refreshes credits, then
  `refill_pool(store, config)` tops the pool back to `pool_target` (`stt.py:687`).
- `cmd_transcribe` (`stt.py:774`) is the single-file orchestrator; the transcribe
  subparser takes one positional `audio` (`stt.py:1022`).

Reused helpers: `fresh_count`, `cached_remaining`, `account_remaining`,
`has_temp_email_config`, `authed_client`, `create_task`/`poll_task`/`export_task`.

## Confirmed Decisions (brainstorm, 2026-07-01)

1. **Allocation granularity — bin-packing.** One account may hold multiple files
   as long as its remaining quota (× margin) covers their combined cost. Files are
   indivisible (a single file never spans two accounts — per the stated
   assumption, every file fits within one fresh account's quota).
2. **Registration timing — register up-front.** Compute the shortfall from the
   plan, register all needed new accounts *before* transcribing, then run.
3. **Failure handling — skip & continue, summarize at end.** A file whose
   registration or transcription fails is recorded and skipped; remaining files
   still run; a final summary lists per-file success/failure.
4. **Execution — sequential.** One file at a time (reuse the existing
   upload→poll→export path; no concurrency in MVP).
5. **Interface — extend `transcribe`.** `audio` becomes variadic (`nargs="+"`).
   A single file is just a batch of length 1 (current behavior preserved).
6. **Plan preview — always print the plan; add `--dry-run`.** Print
   `file → account, need new: N` before executing; `--dry-run` prints the plan and
   exits without registering or transcribing.

## Requirements

### R1 — Variadic transcribe input
- `transcribe` positional `audio` → `audios` (`nargs="+"`). Accept multiple paths;
  the shell expands globs. Validate each exists / under `MAX_FILE_BYTES`; a missing
  or oversize file is reported and dropped from the batch (not a hard abort),
  consistent with skip-and-continue.
- `-o/--output` is only valid with a single input; with >1 file, error out and
  tell the user outputs default to each `audio.with_suffix(.<fmt>)`.
- Per-file config resolution (lang/events/subs/format/…) stays as today, applied
  to every file in the batch.

### R2 — Pre-flight cost per file
- For each file: `duration = audio_duration(f)`; `required = estimate_required(duration)`.
- **Duration unknown** (`required is None`, ffprobe missing / unreadable): treat the
  file as needing a **whole fresh account** — it cannot share a bin. Conservative so
  a batch never silently under-provisions. `# ponytail:` note the ceiling
  (size-based estimate if this proves wasteful).

### R3 — Allocation (bin-packing, existing-first, best-fit)
- Build bins from existing non-invalid accounts; capacity = live remaining
  (`account_remaining`, refreshing JWT+subscription like `select_account` does).
- Sort files by `need = required × selection_margin` **descending**
  (First-Fit-Decreasing). Place each file into the existing account whose
  *residual capacity after already-assigned files* is the **smallest that still
  covers** `need` (best-fit → drains near-empty accounts first, preserves big ones,
  matching the single-file policy).
- Files that fit no existing account go into **virtual fresh bins** of capacity
  `fresh_threshold` (× margin). The number of virtual bins = **accounts to
  register**. (Files needing a whole fresh account per R2 each take their own bin.)
- Output: an allocation plan = ordered list of `(file, account_or_"NEW#k")` plus
  `register_count`.

### R4 — Top-up registration (up-front, exact shortfall)
- If `register_count > 0` and temp-email is configured: call `register_one()`
  exactly `register_count` times (reusing frequency control / jitter inside
  `register_one`), `upsert_account` each into the store, and bind the freshly
  registered accounts to the `NEW#k` slots in the plan.
- If `register_count > 0` and temp-email is **not** configured: error before doing
  any work, naming how many accounts are short and pointing at `[temp_email]`
  config — do not partially transcribe then fail.

### R5 — Execution & summary
- Print the resolved plan (`file → email`, `need new: N`) before any network work.
- `--dry-run`: print the plan and exit 0 (no registration, no transcription).
- Otherwise register (R4), then transcribe files **sequentially** in plan order,
  each on its assigned account via `authed_client` (upload → poll → export →
  write output), reusing `cmd_transcribe`'s per-file body.
- **Skip & continue:** wrap each file; on exception record `(file, error)` and
  proceed. A file's failure must not consume/lock the batch.
- After all files: refresh consumed accounts' credits (`force=True`) and call
  `refill_pool` **once** (Ctrl-C-able, as today).
- Print a final summary table: per file → account used → OK (+output path) / FAIL
  (+reason). Exit non-zero if any file failed, zero if all succeeded.

### R6 — Backward compatibility
- `stt transcribe one.mp3` behaves exactly as before (batch of 1; plan printed but
  degenerate). No temp-email config ⇒ single-account best-effort path preserved
  when the one file fits the pool; only errors if it genuinely can't provision.
- `selfcheck` extended: offline test of the allocation packer — given fake
  accounts + file costs, assert existing-first best-fit placement and the correct
  `register_count` (incl. an unknown-duration file forcing a fresh bin). No network.

## Acceptance Criteria

- [ ] AC1: `stt transcribe a.mp3 b.mp3 c.mp3` with a pool that already covers all
      three → packs them across existing accounts (some sharing one account),
      registers 0, writes 3 outputs; plan printed first.
- [ ] AC2: Same command with a pool short by 2 accounts → plan shows `need new: 2`,
      registers exactly 2 up-front, assigns the leftover files to them, transcribes
      all, writes outputs.
- [ ] AC3: Existing accounts are filled **before** newly registered ones
      (plan/log shows leftover files mapped to `NEW#k` only after existing bins are
      packed).
- [ ] AC4: One file fails (e.g. transcription error) → it is skipped, the rest
      complete, final summary marks it FAIL with reason, exit code non-zero.
- [ ] AC5: `--dry-run` prints the full plan incl. `register_count` and exits 0
      without registering any account or uploading anything.
- [ ] AC6: `register_count > 0` with temp-email unconfigured → errors up-front
      naming the shortfall, transcribes nothing.
- [ ] AC7: Bin-packing respects `selection_margin` and never over-commits an
      account beyond `remaining` for its assigned files; a file with unknown
      duration is given its own fresh account.
- [ ] AC8: `stt transcribe one.mp3` still works identically to today; `-o` with
      multiple files errors clearly.
- [ ] AC9: `selfcheck` covers the allocation packer offline (existing-first,
      best-fit, register_count, unknown-duration fresh bin).

## Out of Scope (MVP)

- Files larger than one free account's quota (splitting a file across accounts) —
  explicitly deferred per the stated assumption.
- Concurrent/parallel transcription and multi-file-per-account concurrency.
- Retry/backoff on a failed file (skip once, report; re-run the batch to retry).
- Global optimal bin-packing (FFD heuristic is enough; no ILP).
- Resuming a partially-completed batch from a checkpoint file.

## Open Questions (block implementation, not planning)

- **Q1 — margin scope:** apply `selection_margin` per file (`need_i = req_i × m`)
  or once to the batch total? Plan assumes per-file (safer, matches single-file
  path). Confirm at implementation if it over-provisions on many tiny files.
- **Q2 — unknown-duration frequency:** if ffprobe is commonly missing, R2's
  "whole fresh account per file" wastes accounts. Fallback size-based estimate is
  the upgrade path (already noted in the shipped R2). Revisit only if it bites.

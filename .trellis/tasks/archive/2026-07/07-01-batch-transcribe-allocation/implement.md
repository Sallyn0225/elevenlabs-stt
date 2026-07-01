# Implement — Batch transcription with pool-aware allocation

All work is in `stt.py` (single-file CLI). Additive + one refactor. No new deps,
no schema changes. Reuse `estimate_required`, `account_remaining`,
`cached_remaining`, `fresh_count`, `has_temp_email_config`, `register_one`,
`upsert_account`, `refill_pool`, `authed_client`, `create_task`/`poll_task`/`export_task`.

## Blockers / prerequisites

- None for coding. Live end-to-end AC2/AC6 need `[temp_email]` configured (Q5 from
  the pool task) — offline packer tests (AC9) and dry-run (AC5) need nothing.

## Ordered checklist

### Step 1 — Refactor per-file body out of `cmd_transcribe` (R6, no behavior change)
- Extract everything from `authed_client(...)` through `out.write_bytes(...)` in
  `cmd_transcribe` (`stt.py:839-859`) into
  `transcribe_one(audio, cfg, account, store, config_path) -> pathlib.Path`.
- Keep the `save=lambda _s: save_accounts(store)` callback; return the output path.
- Temporarily call it from the existing single-file flow; run `selfcheck` +
  a real single-file `transcribe` → identical output. This step alone must not
  change any behavior.

### Step 2 — `allocate` packer + offline selfcheck (R2, R3, AC7, AC9)
- Add `allocate(costs, accounts, margin, fresh_threshold, store)` per design
  (FFD + best-fit, existing bins → virtual fresh bins, unknown-duration = own bin,
  `n > fresh_cap` guard → SystemExit). Return `(plan, register_count)` with stable
  `NEW#k` slot labels and existing-first run order.
- Extend `_selfcheck` (`stt.py:878`) with the design's cases: best-fit placement,
  `register_count`, unknown-duration fresh bin, over-size guard raises.
- Validate: `python stt.py selfcheck` passes.

### Step 3 — Variadic arg + arg guards (R1, AC8)
- Parser: `audio` → `audios` with `nargs="+"` (`stt.py:1023`). Update help text.
- In `cmd_transcribe`: iterate `args.audios`; validate each (exists,
  `MAX_FILE_BYTES`), warn+drop bad ones. Guard `len(files) > 1 and args.output` →
  SystemExit naming default per-file output.
- Validate: `stt transcribe one.mp3` unchanged; `stt transcribe a b -o x` errors.

### Step 4 — Orchestrator: plan print, dry-run, up-front register, loop, summary (R3, R4, R5)
- Refresh non-invalid accounts (`account_remaining`) before `allocate`; save store.
- `print_plan(plan, register_count)` to stderr (design format).
- `--dry-run` flag on the parser → after printing plan, `return 0`.
- If `register_count` and not `has_temp_email_config` → SystemExit with shortfall.
- Register `register_count` accounts via `register_one`, `upsert_account`, save;
  bind `NEW#k` slots → registered accounts by index.
- Loop `plan` in order: set `store.active`, `transcribe_one`, collect
  `(file, email, OK|FAIL, out|err)`; catch per-file exceptions (skip & continue).
- Post-loop: `account_remaining(force=True)` for used accounts; `refill_pool` once
  (guard `KeyboardInterrupt`); `print_summary`; return `0` if all OK else `1`.

### Step 5 — Docs
- README.md / README.en.md: batch usage, `--dry-run`, allocation/"缺多少注册多少"
  behavior, `-o` single-file restriction. Mirror both languages.

## Validation commands (cumulative)

```
python stt.py selfcheck                              # Step 2+ (offline packer)
python stt.py transcribe clip.mp3                    # Step 1,3 backward-compat
python stt.py transcribe a.mp3 b.mp3 c.mp3 --dry-run # Step 4 plan only, no network (AC5)
python stt.py transcribe a.mp3 b.mp3 c.mp3           # full batch (AC1/AC2/AC3/AC4)
python stt.py accounts                               # verify credits consumed + refill
```

## Risky files / rollback points

- `stt.py` `cmd_transcribe` — the refactor (Step 1) is the only behavior-risk;
  keep the diff mechanical and diff single-file output before/after.
- Parser change (`nargs="+"`) — the one backward-compat surface; verify a bare
  single-file call still parses and runs.

## Follow-up checks before finish

- `selfcheck` green; single-file `transcribe` output byte-identical to pre-change.
- Dry-run performs zero network calls (no registration, no upload).
- Batch with a deliberately short pool registers exactly the shortfall and orders
  existing accounts before new ones in the plan/log.
- One intentionally-broken file → FAIL in summary, others OK, exit code 1.

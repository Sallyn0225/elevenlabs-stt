# Design — Batch transcription with pool-aware allocation

## Architecture

Pure extension of the shipped single-file path. Nothing new server-side, no new
deps. Three moves:

1. **Refactor** the per-file body out of `cmd_transcribe` into
   `transcribe_one(audio, cfg, account, store, config_path)` — everything from
   "upload" to "write output", returning the output path. `cmd_transcribe` becomes
   an orchestrator: parse args → build cfg → **allocate** → (register) → loop
   `transcribe_one` → summary → refill.
2. **New pure function** `allocate(files_costs, accounts, margin, fresh_threshold,
   store)` → an ordered plan + `register_count`. Offline, unit-testable, no network
   beyond the credit refresh done *before* it (accounts arrive with live remaining).
3. **Plumbing**: variadic arg, plan printer, `--dry-run`, up-front registration to
   fill `NEW#k` slots, summary table, single end-of-run `refill_pool`.

Selection stays best-fit + margin — `allocate` is the multi-file, multi-bin
generalization of `select_account` (`stt.py:700`), and reuses the same
`account_remaining` / `cached_remaining` / `fresh_count` helpers.

## Data Flow — transcribe (batch)

```
cmd_transcribe(args):
  cfg = build_cfg(args)                      # unchanged resolution, applied to all
  files = [validate(p) for p in args.audios] # drop missing/oversize with a warning
  guard: len(files) > 1 and args.output  -> SystemExit("-o only with one file")

  store = load_accounts(); acfg = accounts_config(cfg_path)
  costs = [(f, estimate_required(audio_duration(f))) for f in files]   # None = unknown

  accounts = [a for a in store.accounts if not a.invalid]
  for a in accounts: account_remaining(a, store)          # refresh JWT+subscription
  save_accounts(store)                                    # persist rotated tokens/credits

  plan, register_count = allocate(costs, accounts, acfg.selection_margin,
                                  acfg.fresh_threshold, store)
  print_plan(plan, register_count)                        # always

  if args.dry_run: return 0
  if register_count and not has_temp_email_config(cfg_path):
      raise SystemExit(f"need {register_count} more account(s); configure [temp_email]")

  new = [register_one() for _ in range(register_count)]   # up-front, exact shortfall
  for a in new: upsert_account(store, a)
  save_accounts(store)
  bind NEW#k slots in plan -> new[k]                      # by slot index

  results = []
  for (file, account) in plan:                            # sequential, plan order
      store.active = account.email; save_accounts(store)
      try:    out = transcribe_one(file, cfg, account, store, cfg_path)
              results.append((file, account.email, "OK", out))
      except Exception as e:
              results.append((file, account.email, "FAIL", repr(e)))   # skip & continue

  for a in used-accounts: account_remaining(a, store, force=True)       # post-use credits
  try: refill_pool(store, cfg_path)                       # once, Ctrl-C-able
  except KeyboardInterrupt: warn("refill skipped")
  print_summary(results)
  return 0 if all OK else 1
```

## Allocation algorithm (`allocate`)

Bin = an account (or a virtual fresh bin) with a running `residual`. First-Fit-
Decreasing with a best-fit placement rule, existing bins before virtual ones.

```
def allocate(costs, accounts, margin, fresh_threshold, store):
    # need per file; unknown duration -> whole fresh account (own bin)
    def need(req): return None if req is None else int(req * margin) + 1
    files = sorted(costs, key=lambda fc: (-1 if fc[1] is None else need(fc[1])),
                   reverse=True)                 # unknown first, then largest need

    bins = [Bin(account=a, residual=account_remaining(a, store) or 0) for a in accounts]
    fresh_cap = int(fresh_threshold * margin)    # a virtual bin's usable capacity
    virtual = []                                 # list[Bin] with account=None (NEW#k)
    plan = []                                    # [(file, account_or_slot)] in run order

    for file, req in files:
        if req is None:                          # unknown -> dedicated fresh bin
            b = Bin(account=None, residual=0); virtual.append(b)
            plan.append((file, slot(b))); continue
        n = need(req)
        # existing first, best-fit: smallest residual that still covers n
        cand = [b for b in bins if b.residual >= n]
        if cand:
            b = min(cand, key=lambda b: b.residual)
        else:
            # existing virtual fresh bins next, best-fit
            vc = [b for b in virtual if b.residual >= n]
            if vc: b = min(vc, key=lambda b: b.residual)
            else:                                # open a new virtual fresh bin
                if n > fresh_cap:                # single file bigger than a fresh acct
                    raise SystemExit(f"{file}: needs {n} > one free account ({fresh_cap}); out of scope")
                b = Bin(account=None, residual=fresh_cap); virtual.append(b)
        b.residual -= n
        plan.append((file, b.account or slot(b)))

    register_count = len(virtual)
    # run order: existing-account files first, NEW#k files last (stable within)
    plan.sort(key=lambda pa: (pa[1] is a NEW slot, ...))
    return plan, register_count
```

Notes:
- **Existing-first** is guaranteed twice: candidate search tries real bins before
  virtual ones, and the final `plan.sort` emits existing-account files ahead of
  `NEW#k` files (AC3).
- **Best-fit** (`min residual that covers`) drains near-empty accounts first and
  preserves big ones — identical intent to `select_account`.
- **Unknown duration** files sort first and each claim a fresh bin (R2/AC7).
- The `n > fresh_cap` guard is the only place the "file bigger than one account"
  out-of-scope case surfaces — explicit error, not silent misallocation.
- `slot(b)` yields a stable `NEW#k` label from the virtual bin's index for the plan
  print and for binding registered accounts back after registration.

## Contracts

### CLI surface (changed)
```
stt transcribe AUDIO [AUDIO ...] [--dry-run] [-o OUT]   # -o only if one AUDIO
```
All existing flags unchanged; applied to every file.

### Plan print (stderr)
```
plan (margin 1.2, fresh 10000):
  clipA.mp3   -> a@x.mom        (need 961,  rem 1000)
  clipB.mp3   -> b@x.mom        (need 5200, rem 10000)
  clipC.mp3   -> NEW#0          (need 8000)
need new: 1
```

### Summary print (stdout)
```
summary: 2 ok, 1 failed
  OK    clipA.mp3  -> a@x.mom   clipA.srt
  OK    clipB.mp3  -> b@x.mom   clipB.srt
  FAIL  clipC.mp3  -> new0@x.mom  RuntimeError('poll timeout')
```

No new persisted files/state; `accounts.json` schema unchanged.

## Tradeoffs

- **FFD heuristic, not optimal packing.** O(files × accounts); fine for tens of
  files. `# ponytail: FFD, revisit only if a real batch mis-packs.`
- **Refresh all accounts up-front** (one subscription GET each) so `allocate` is
  offline and the plan is accurate. Cost = N cheap GETs; acceptable, and mirrors
  what `select_account` already does per call.
- **Register up-front, exact count** (user decision). Risk: bursty registration →
  IP ban. `register_one` already jitters; keep it, no extra knobs in MVP.
- **`transcribe_one` reuse** means single-file and batch can never diverge — the
  laziest correctness guarantee.

## Risks & Rollback

- **R1 — over-provision on many tiny files** (per-file margin rounding). Low
  impact (extra fresh account sits in pool for reuse). Revisit margin scope (PRD
  Q1) only if observed.
- **R2 — unknown-duration wastes accounts.** Mitigated by size-based estimate
  upgrade path (PRD Q2); MVP stays conservative.
- **Rollback**: the change is additive + one refactor. Reverting `nargs="+"` →
  single positional and inlining `transcribe_one` restores prior behavior.

## Self-check additions (AC9)

Offline `allocate` test in `_selfcheck` — assert the invariants (AC7/AC9), not a
brittle per-file split (FFD-descending processes the largest need first, so the
exact a-vs-b mapping is an implementation detail, not a contract):
- fake accounts `a`(rem 1000), `b`(rem 6000); files needing [800, 5000, 7000, 400].
  Assert: the 7000 file → `NEW#0` (fits no existing account); `register_count==1`;
  the three fitting files all land on existing accounts; no account over-committed
  (assigned ≤ remaining); existing-account files ordered before the NEW file.
- one file with `req=None` (unknown) → its own `NEW#0`, `register_count` increments.
- a file needing > fresh_cap → raises (out-of-scope guard).

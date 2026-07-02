# Journal - Sallyn (Part 1)

> AI development session journal
> Started: 2026-07-01

---



## Session 1: ElevenLabs Web STT CLI

**Date**: 2026-07-01
**Task**: ElevenLabs Web STT CLI
**Branch**: `main`

### Summary

Brainstormed + 抓包-captured ElevenLabs web STT internal API (Firebase JWT auth, multipart create, poll, export), wrote PRD/design/implement, implemented single-file stt.py CLI (login/transcribe/list-languages/selfcheck) with TOML config. End-to-end validated: SRT+VTT export, toggles, auto-detect(zho), vocab (fixed char-split bug), auth refresh. Wrote CN(default)+EN READMEs.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `0b4823f` | (see git log) |
| `fb63a71` | (see git log) |
| `38ac297` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 2: Batch transcription with pool-aware allocation

**Date**: 2026-07-01
**Task**: Batch transcription with pool-aware allocation
**Branch**: `main`

### Summary

Added multi-file batch mode to stt transcribe: pre-compute per-file credits, bin-pack across the existing account pool (best-fit, existing-first), register only the shortfall up-front, transcribe sequentially with skip-and-continue + end summary. --dry-run prints the plan. Extracted transcribe_one(); dropped dead select_account/active_account. Planned via trellis (prd/design/implement), implemented in 5 steps by trellis-implement, quality-checked by trellis-check (zero correctness defects), verified end-to-end on 3 real m4a files (9916->8239 credits). Noted ElevenLabs async credit-debit lag as a real-world behavior.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `37d9f8e` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 3: Fix WebUI entrance jitter

**Date**: 2026-07-02
**Task**: Fix WebUI entrance jitter
**Branch**: `main`

### Summary

Fixed WebUI first-render positional jitter by making entrance animations opacity-only, added webui*.log ignore coverage, documented the frontend animation convention, validated selfcheck and local /api/state, committed and pushed the fix.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `7e4f73a` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete


## Session 4: CLI selective account credit refresh

**Date**: 2026-07-02
**Task**: CLI selective account credit refresh
**Branch**: `main`

### Summary

Added repeatable -e/--email to 'stt accounts' so CLI can refresh/list specific account(s) by email, matching WebUI's selective refresh. Single-file ~9-line change in stt.py: wanted=set(args.email) filters both --refresh scope (no network for non-matches) and the listing; unmatched --email with --refresh emits one stderr line each. Backward-compatible when --email absent. selfcheck + manual behavior checks pass.

### Main Changes

(Add details)

### Git Commits

| Hash | Message |
|------|---------|
| `84499b6` | (see git log) |

### Testing

- [OK] (Add test results)

### Status

[OK] **Completed**

### Next Steps

- None - task complete

# elevenlabs-stt

**English** | [简体中文](./README.md)

> Turn ElevenLabs' web Speech-to-Text feature into a CLI script, using your own logged-in account to reuse the free quota.

> [!IMPORTANT]
> This project is not affiliated with ElevenLabs. It works by replaying the web app's internal API, which can change without notice. Use your own account and respect ElevenLabs' terms of service.

## What it does

Turns the web flow at `elevenlabs.io/app/speech-to-text` (log in → upload audio → pick language/toggles → wait → export subtitles) into a single command. The script replays the web internal API (not the official paid API), so it inherits the free account's credit quota (~10,000 credits ≈ 12 min of audio/month).

## Features

- Upload a local audio file for transcription (the web "Upload" path)
- Four toggles: mark audio events / include subtitles / no verbatim / use speaker library
- High-frequency proper-noun vocabulary (key terms)
- Main language selection or auto-detect (ISO 639-3)
- Export SRT / VTT / TXT / JSON / HTML / PDF / DOCX, default SRT
- Pre-upload checks: file size (1000 MB hard), duration (optional ffprobe, 10-min soft warn)
- One-time browser login with automatic 1-hour token refresh
- Optional multi-account pool: auto-register temp-mail accounts, choose by remaining credits, and refill after transcription

## Install

```bash
pip install -r requirements.txt
playwright install chrome   # only needed for login
```

Optional: install `ffprobe` (from ffmpeg) to enable audio-duration checks.

## Quick start

```bash
# 1) One-time login (opens Chrome; log into ElevenLabs any way you like)
python stt.py login

# 2) Transcribe (defaults to out.srt)
python stt.py transcribe audio.m4a -o out.srt

# 3) Batch transcribe (each file writes its own <name>.srt)
python stt.py transcribe a.mp3 b.mp3 c.mp3

# 4) Preview the allocation plan only — no registration, no upload
python stt.py transcribe a.mp3 b.mp3 c.mp3 --dry-run

# 5) Force language + vocab + export VTT
python stt.py transcribe audio.m4a --lang eng --vocab "V社,Major" --format vtt -o out.vtt

# 6) Long audio: split on silence, transcribe each part, merge back into one file
python stt.py transcribe long.m4a --split -o long.srt

# Optional: inspect/warm the multi-account pool
python stt.py pool status
python stt.py pool warm --target 3
```

Login saves a Firebase **refresh token** to `session.json` (gitignored). The 1-hour JWT is auto-refreshed on each run; re-run `login` only when the refresh token itself expires or you get an auth error.

## Configuration

Copy `config.example.toml` → `config.toml` for persistent defaults; CLI flags override config:

| Field | Description | Default |
|---|---|---|
| `language` | `auto`, a language name, or an ISO 639-3 code | `auto` |
| `tag_audio_events` | Mark audio events | `true` |
| `include_subtitles` | Include subtitles (web default off; script forces on) | `true` |
| `no_verbatim` | No verbatim | `false` |
| `use_speaker_library` | Use speaker library | `false` |
| `vocab` | Key terms, e.g. `["Maj3r", "V社"]` | `[]` |
| `export_format` | `srt`/`vtt`/`txt`/`json`/`html`/`pdf`/`docx` | `srt` |
| `poll_timeout_secs` | Poll timeout in seconds | `600` |
| `show_cost` | Print estimated credit cost before upload | `false` |
| `max_concurrency` | Max concurrent transcriptions across accounts; `1` = serial fallback | `4` |
| `stagger_secs` | Minimum gap (seconds) between consecutive upload starts; `0` = no staggering | `2.0` |

## Local Web UI (optional)

Beyond the CLI, `web.py` spins up a local web page using the standard library, reusing `stt.py`'s transcription and account logic:

```bash
python web.py            # opens http://127.0.0.1:8756
```

- **Transcribe page**: drag/multi-select audio → the browser measures duration and estimates credits → auto-allocates accounts by remaining credits (best-fit) → "Start" does the real upload, polling, and export, auto-downloading subtitles when done. The accounts section includes a collapsed-by-default **"Advanced · manually pick accounts"** panel: checking accounts restricts allocation to the selected set (accounts too small for the batch are greyed out); manual mode errors out instead of auto-registering when credits fall short, and clearing the selection returns to auto-allocation. While transcribing, the bottom bar shows **real step-by-step progress** (done/total counter, a multi-line **active-task list** with each in-flight file and stage, latest log line), with an expandable full scrolling log.
- **Accounts page**: reads `accounts.json`, with search/sort/multi-select/pagination and back-to-top, real credit refresh (selected accounts only, fetched concurrently), delete, and JSON export.
- **Log in**: a dialog takes email + password, logs in via Firebase REST directly and saves the token to `accounts.json` (no browser needed).
- **Start the registrar**: a dialog exposes the full parameters (mapping to `config.toml → [temp_email]` and the target full-account count); "Save" writes back to `config.toml` (two-way sync with the config file); "Start batch create" saves the config first, then runs a real `pool warm` batch registration. Registration emits step-by-step progress logs: the CLI (`stt pool warm` and transcribe-triggered auto-registration) prints them to stderr, and the Web UI register dialog shows them live with a done/target counter.
- **Tools · long-audio split page**: upload long audio → the backend computes a greedy silence-based cut plan (each part fits one full free account's quota) → run the split and losslessly export the parts to local `out/` for later transcription; shares `audio_split.py` with the CLI `--split`.
- When the pool can't cover all files, the transcribe page prompts you to `pool warm` first — it never registers silently.

Zero extra dependencies, zero build; must be run from the project root (needs `accounts.json`, `config.toml`).

## Command reference

```
stt login              one-time browser login → session.json
stt transcribe <audio> [<audio> ...]  transcribe one or more audio files (batch)
stt accounts           show accounts and remaining credits
stt pool status        show fresh/usable/depleted/invalid account counts
stt pool warm          register accounts until the fresh target is met
stt list-languages     print supported language names + codes
stt selfcheck          offline self-check (no network)
```

`transcribe` flags:

| Flag | Description |
|---|---|
| `-c, --config` | Config file path (default `config.toml`) |
| `--lang` | `auto` / language name / ISO 639-3 code |
| `--events / --no-events` | Mark audio events (default on) |
| `--subs / --no-subs` | Include subtitles (default on) |
| `--verbatim / --no-verbatim` | No verbatim (default off) |
| `--voice-lib / --no-voice-lib` | Use speaker library (default off) |
| `--vocab` | Comma-separated key terms |
| `--format` | Export format |
| `-o, --output` | Output file path (**single file only**; errors with multiple files, which each default to `<name>.<fmt>`) |
| `--show-cost` | Print estimated credit cost |
| `--poll-timeout` | Poll timeout in seconds |
| `--account` | Restrict allocation to this account email (repeatable); errors out instead of auto-registering when the selection can't cover the batch |
| `--dry-run` | Print the allocation plan and exit — no registration, no upload |
| `--split` | Split over-long audio on silence into quota-sized parts, transcribe each, merge back into one file (`srt`/`vtt`/`txt` only) |
| `--chunk-secs` | Per-part target duration cap (seconds); default auto-derived from quota (~569s) |
| `--keep-chunks` | Keep temporary part files after a successful merge (cleaned up by default) |
| `--silence-db` | Silence threshold (dB, default `-30`) |
| `--silence-min` | Minimum silence duration (seconds, default `0.5`) |

`accounts` flags:

| Flag | Description |
|---|---|
| `-c, --config` | Config file path (default `config.toml`) |
| `--refresh` | Force-refresh credits from the API (8 threads in parallel; stderr progress in completion order) |
| `-e, --email` | Only act on this account (repeatable); filters both `--refresh` scope and listing by email |

## Languages

`--lang` accepts `auto` (auto-detect, default), a language name (e.g. `english`), or any ISO 639-3 code (e.g. `eng`/`zho`/`jpn`). Run `python stt.py list-languages` to see the built-in language table.

## Multi-account pool (optional)

With `[temp_email]` and `[accounts]` configured, the script can maintain multiple free accounts: it chooses the smallest account that can cover each transcription and refills back to `pool_target` after success. See [`docs/account-pool.en.md`](./docs/account-pool.en.md); deploying the temp-mail backend that `[temp_email]` depends on is covered in [`docs/temp-email-backend.en.md`](./docs/temp-email-backend.en.md). Without `[temp_email]`, it stays in normal single-account mode.

## Batch transcription & auto-allocation

`stt transcribe a.mp3 b.mp3 c.mp3` transcribes several files in one run. Before any upload the script:

- estimates each file's credit cost (from duration; a file with unknown duration conservatively claims its own fresh account);
- **packs onto existing accounts first**: using best-fit bin-packing, multiple files share one account as long as its remaining credits (× `selection_margin`) cover them — draining near-empty accounts first and preserving the big ones;
- **registers only the shortfall** (缺多少注册多少): only files that fit no existing account trigger new registrations, and exactly that many — existing accounts come first, newly registered accounts last;
- prints the plan first (`file → account/NEW#k`, `need new: N`), then transcribes **concurrently across accounts**: tasks on the same account run serially, different accounts run in parallel (capped by `max_concurrency`, default 4; consecutive upload starts are staggered by `stagger_secs` seconds). Any shortfall is handled as a **register-transcribe pipeline**: tasks bound to existing accounts start immediately, and each newly registered account's tasks start as soon as that registration finishes; a mid-run registration failure only FAILs the tasks bound to still-unregistered slots while in-flight tasks finish normally. A failed file is skipped and the run continues; an OK/FAIL summary prints at the end, exit code non-zero if anything failed. `max_concurrency = 1` restores the old serial behaviour.

`--dry-run` prints the plan and exits without registering or uploading. If `register_count > 0` but `[temp_email]` is not configured, it errors up-front (naming how many accounts are short) before doing any work.

**Manual candidate set**: `--account x@y.z` (repeatable) restricts the allocation candidates to the listed accounts — best-fit then runs only within that set; one file + one account means "this audio uses exactly this account". Unknown or invalid emails error out before any work; if the selection can't hold the whole batch, the run **errors out instead of auto-registering** (add more accounts or drop `--account` to return to auto-allocation). Composes with `--dry-run` and `--split`.

`-o/--output` is single-file only; in a batch each file defaults to its own `<name>.<fmt>`.

## Splitting long audio on silence (`--split`)

A single free account only covers ~10 minutes. For longer audio, add `--split` and the script will:

- detect silences with ffmpeg `silencedetect` and cut at the **latest silence midpoint** within each per-part duration cap (cuts land inside silence, never mid-word); if a window has no silence it hard-cuts at the cap and warns;
- default the per-part cap from your quota (`fresh_threshold / (credits_per_sec × selection_margin) × 0.95`, ≈569s ≈ 9.5 min) so each part fits one full account; override with `--chunk-secs`;
- feed the parts as ordinary files into the existing **multi-account allocation + per-part transcription** flow (each part takes a sufficient account, registering the shortfall automatically);
- once all parts succeed, **offset-correct the timestamps and merge** into one `<name>.<fmt>` (`srt`/`vtt` renumbered, `txt` concatenated in order), then clean up the temporary parts (`--keep-chunks` keeps them).

```bash
# a 1-hour file → ~7 parts, allocated across the pool, merged into one srt
python stt.py transcribe interview.m4a --split -o interview.srt

# preview the split + allocation plan without uploading
python stt.py transcribe interview.m4a --split --dry-run

# tune the per-part cap and silence sensitivity
python stt.py transcribe interview.m4a --split --chunk-secs 480 --silence-db -35 --silence-min 0.8
```

Constraints & behavior:

- Merge is supported only for `srt`/`vtt`/`txt`; combining with `json`/`html`/`pdf`/`docx` errors out.
- **Fail = no merge**: if any part fails, no merged file is written, but the successful parts' outputs and the cut part files are kept, and the summary flags the failed part (non-zero exit) for a manual re-run.
- Temporary parts live in `out/<name>-chunks/`; requires ffmpeg/ffprobe.
- Cuts use `-c copy` (lossless), snapping to the nearest keyframe at millisecond scale (inside silence, imperceptible).

## Limits

- **File size**: 1000 MB hard limit; rejected before upload.
- **Duration**: if `ffprobe` is installed, audio longer than 10 min (free-account soft limit) triggers a warning; not hard-blocked. Over-long audio can use `--split` to auto-split on silence and merge back.
- **Credits**: free tier ~10,000 credits (~13.9 cr/s, ≈12 min/month). If insufficient, the server rejects the upload and the CLI shows the error; `--show-cost` prints a pre-upload estimate.

> [!NOTE]
> Script defaults: mark audio events=on, include subtitles=on (web default is off; script forces on), others off, language=auto, export=SRT.

## How it works

The script calls the web app's internal API (`api.us.elevenlabs.io`) directly with `httpx`, authenticating with a Firebase JWT bearer token. The login step uses Playwright to launch the system Chrome, read the Firebase session from localStorage, and export the refresh token; the runtime never needs a browser. The captured API contract lives in [`docs/api-contract.en.md`](./docs/api-contract.en.md).

## Files

| File | Description |
|---|---|
| `stt.py` | CLI + client |
| `audio_split.py` | Silence detection + split module (shared by CLI `--split` and the Web UI) |
| `web.py` | Local Web UI server (stdlib) |
| `webui.html` | Web UI single-page frontend |
| `config.example.toml` | Config sample |
| `requirements.txt` | Dependencies |
| `session.json` | Generated by `login` (credentials, gitignored) |
| `accounts.json` | Generated by account-pool mode (credentials, gitignored) |
| `docs/account-pool.en.md` | Multi-account pool and auto-registration guide |
| `docs/temp-email-backend.en.md` | Temp-mail backend (cloudflare_temp_email) integration guide |
| `docs/api-contract.en.md` | Captured internal API contract |

## Self-check

```bash
python stt.py selfcheck
```

## Acknowledgements

- [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) — temp-mail backend used by the multi-account auto-registration
- [Playwright](https://playwright.dev) — browser automation for the one-time login step
- [httpx](https://www.python-httpx.org) — HTTP client
- [FFmpeg](https://ffmpeg.org) — optional audio-duration pre-check via `ffprobe`

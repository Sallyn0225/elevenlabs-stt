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

# 3) Force language + vocab + export VTT
python stt.py transcribe audio.m4a --lang eng --vocab "V社,Major" --format vtt -o out.vtt

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

## Command reference

```
stt login              one-time browser login → session.json
stt transcribe <audio> transcribe an audio file
stt accounts           show accounts and remaining credits
stt pool status        show fresh/usable/depleted account counts
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
| `-o, --output` | Output file path |
| `--show-cost` | Print estimated credit cost |
| `--poll-timeout` | Poll timeout in seconds |

## Languages

`--lang` accepts `auto` (auto-detect, default), a language name (e.g. `english`), or any ISO 639-3 code (e.g. `eng`/`zho`/`jpn`). Run `python stt.py list-languages` to see the built-in language table.

## Multi-account pool (optional)

With `[temp_email]` and `[accounts]` configured, the script can maintain multiple free accounts: it chooses the smallest account that can cover each transcription and refills back to `pool_target` after success. See [`docs/account-pool.en.md`](./docs/account-pool.en.md). Without `[temp_email]`, it stays in normal single-account mode.

## Limits

- **File size**: 1000 MB hard limit; rejected before upload.
- **Duration**: if `ffprobe` is installed, audio longer than 10 min (free-account soft limit) triggers a warning; not hard-blocked.
- **Credits**: free tier ~10,000 credits (~13.9 cr/s, ≈12 min/month). If insufficient, the server rejects the upload and the CLI shows the error; `--show-cost` prints a pre-upload estimate.

> [!NOTE]
> Script defaults: mark audio events=on, include subtitles=on (web default is off; script forces on), others off, language=auto, export=SRT.

## How it works

The script calls the web app's internal API (`api.us.elevenlabs.io`) directly with `httpx`, authenticating with a Firebase JWT bearer token. The login step uses Playwright to launch the system Chrome, read the Firebase session from localStorage, and export the refresh token; the runtime never needs a browser. The captured API contract lives in [`docs/api-contract.md`](./docs/api-contract.md).

## Files

| File | Description |
|---|---|
| `stt.py` | CLI + client |
| `config.example.toml` | Config sample |
| `requirements.txt` | Dependencies |
| `session.json` | Generated by `login` (credentials, gitignored) |
| `accounts.json` | Generated by account-pool mode (credentials, gitignored) |
| `docs/account-pool.en.md` | Multi-account pool and auto-registration guide |

## Self-check

```bash
python stt.py selfcheck
```

# PRD: ElevenLabs Web Speech-to-Text API Script

## Goal

Convert ElevenLabs' **web** Speech-to-Text feature
(`https://elevenlabs.io/app/speech-to-text`) into a Python CLI that performs
the same operations programmatically, using the user's own logged-in account.
This replays the web app's internal API (NOT the official ElevenLabs SaaS API)
to inherit the free-account behavior (credit-based quota, ≈12 min/month).

## User Value

- Drive speech-to-text from a script/CLI without clicking the web UI.
- Reuse the free web quota instead of paying for the official API.
- Configurable transcription options; default export to SRT.

## Background & Confirmed Facts (抓包-verified 2026-07-01)

Full API contract: `research/api-contract.md`. UI structure:
`research/modal-ui-structure.md`. Summary:

- Web flow: log in → Speech-to-Text page → "转录文件" modal → 4 upload
  methods (Upload/Record/YouTube/URL). **Only Upload is in scope.**
- Auth: Firebase JWT bearer (`Authorization: Bearer <jwt>`), 1h expiry;
  refreshable via long-lived Firebase refresh token. Login helper reads the
  persisted Firebase user from browser localStorage.
- Create task: `POST /v1/speech-to-text/tasks` (multipart, audio inline,
  no presigned upload). Toggle/request field names confirmed:
  `tag_audio_events`, `include_subtitles`, `no_verbatim`,
  `use_speaker_library`, `language_code` (null=auto, ISO 639-3 when set),
  `keyterms` (vocab; repeated field per word; response field `biased_keywords`),
  `model_id` ("scribe_v2" fixed), `task_name`, `file`.
- Poll: `GET /v1/speech-to-text/tasks/{_id}`; state
  `not_processing`→`processing`→`processed`.
- Export: `POST /v1/speech-to-text/tasks/{_id}/editor/export/{format}`
  (format ∈ txt|pdf|docx|json|html|srt|vtt), body
  `{"language_code": <result.language_code>}`, response = rendered file bytes.
- Cost: `GET /v1/speech-to-text/cost?duration=<secs>` → `{credits}` (≈13.9 cr/s;
  free tier ≈10,000 credits ≈12 min). Hard file limit 1000 MB.
- Web defaults observed: tag_audio_events=on, include_subtitles=off,
  no_verbatim=off, use_speaker_library=off, language=auto.

## Decisions

- **D1 Runtime + stack**: Python + HTTP API replay (httpx). Playwright used
  only for the one-time `login` helper (and dev-time 抓包). No browser at
  transcribe runtime.
- **D2 Auth**: One-time Playwright `login` (channel="chrome" to bypass this
  machine's TLS interception of bundled Chromium) → user logs in via any
  method → helper exports Firebase refresh token to `session.json`. Runtime
  auto-refreshes the 1h JWT via Firebase securetoken. Re-run `login` only when
  the refresh token expires.
- **D3 Interface**: CLI-first. Single `stt.py`, argparse subcommands
  `login` / `transcribe` / `list-languages`. TOML config (`config.toml`) for
  persistent defaults, read via stdlib `tomllib`. CLI flags override config.
- **D4 Vocab input**: BOTH — `--vocab w1,w2,w3` flag AND `vocab=[...]` in
  config; flag merges with config.
- **D5 Quota pre-check**: file SIZE always checked (stdlib, reject >1000 MB);
  DURATION best-effort via `ffprobe` (optional dep; warn if >600s, no hard
  block). Credits insufficient → server rejects → script surfaces the error.
  `--show-cost` (optional) prints a pre-upload credit estimate via /cost.

## Requirements

1. `stt login` establishes a reusable session (refresh token) without storing
   the user's password.
2. `stt transcribe <audio>` uploads via the web Upload path and returns a
   completed transcription file on disk.
3. Toggles configurable per run + via config; script defaults:
   `tag_audio_events=true`, `include_subtitles=true` (forced on; web default
   is off), `no_verbatim=false`, `use_speaker_library=false`.
4. Language selectable: `auto` (default) or ISO 639-3 code; `list-languages`
   prints the name→code table for reference.
5. Vocab accepted via flag and/or config; submitted as repeated `keyterms`.
6. Poll until `processed`; surface progress + errors (incl. quota/size limits).
7. Export as SRT by default; format configurable (srt|vtt|txt|json|html|pdf|docx).

## Out of Scope

- Account registration / signup automation.
- The other 3 upload methods (Record / YouTube / URL).
- Voice-library assignment beyond passing the `use_speaker_library` toggle.
- Multi-file batch orchestration.
- The official ElevenLabs SaaS API (this targets the web internal API only).

## Acceptance Criteria

- [ ] `stt login` writes `session.json` with a refresh token; a subsequent
      `transcribe` needs no further manual login.
- [ ] `stt transcribe _tmp-sample.m4a --lang auto --subs -o out.srt` produces
      a non-empty `out.srt` whose first block starts with `1\n00:00:`.
- [ ] Defaults (no flags) = tag_audio_events on, include_subtitles on, others
      off, language auto, export srt — overridable by flags/config.
- [ ] `--lang eng` produces a task whose poll result reports English.
- [ ] `--vocab "Maj3r,V社"` is submitted (task `user_params.biased_keywords`
      contains both).
- [ ] `--format vtt` produces a non-empty WebVTT file.
- [ ] `stt list-languages` prints a table containing `english → eng`.
- [ ] A >1000 MB file is rejected before upload with a clear message.
- [ ] When ffprobe is installed, a >600s file triggers a warning (not a block).
- [ ] An expired refresh token yields a clear "re-run stt login" message.

## Open Questions

None blocking. (Language name→code table extraction is an impl task with a
raw-code-passthrough fallback; not a product decision.)

# Implement: ElevenLabs Web STT API Script

Ordered checklist. Validate after each milestone. Read `prd.md`, `design.md`,
and `research/api-contract.md` before coding.

## M1 — Skeleton + config (no network)
- [ ] `stt.py`: argparse with `login` / `transcribe` / `list-languages` subcommands.
- [ ] `load_config(path)`: read TOML via stdlib `tomllib`; merge CLI flags over
      config over built-in defaults. Return a dataclass or plain dict.
- [ ] `config.example.toml` with the schema in design.md (commented).
- [ ] `.gitignore`: add `session.json`, `config.toml`, `_tmp-sample.m4a`, `.playwright-mcp/`.
- [ ] `requirements.txt`: `httpx`; comment line for `playwright` (login).
- Validate: `python stt.py --help` and `python stt.py transcribe --help` print;
      `python -c "import tomllib,sys; tomllib.load(open('config.example.toml','rb'))"`.

## M2 — Auth (login + JWT refresh)
- [ ] `cmd_login()`: lazy `import playwright.sync_api`; launch with
      `channel="chrome"` (fallback `channel="msedge"`); goto
      `https://elevenlabs.io/app/speech-to-text`; wait for user to land on an
      app page (not /sign-in); `page.evaluate` to read
      `localStorage["firebase:authUser:<apiKey>:[DEFAULT]"]`; parse JSON;
      extract `stsTokenManager.refreshToken`, `localId`, `email`; write
      `session.json`.
- [ ] `load_session()` / `save_session()`.
- [ ] `get_jwt(session)`: if cached `jwt` and `jwt_exp > now+60` return it;
      else POST `securetoken.googleapis.com/v1/token?key=<apiKey>` form
      `grant_type=refresh_token&refresh_token=<rt>` → parse `id_token`,
      `expires_in`; cache; save; return jwt. On 400 → print "re-run stt login".
- [ ] `client(session)` → `httpx.Client` with Bearer header.
- Validate: `python stt.py login` (do it for real against the logged-in
      Chrome profile); then a tiny `python -c` that calls `get_jwt` and
      GETs `/v1/speech-to-text/tasks?page_size=1` → expects 200 + tasks list.
      NOTE during dev: the chrome-devtools MCP Chrome is already logged in as
      annahorwin@edu.misuzu.mom and can be used to cross-check requests.

## M3 — Transcribe core
- [ ] `validate_audio(path)`: size via `os.path.getsize`; reject >1000MB;
      duration via `ffprobe` if present (`shutil.which("ffprobe")`), else notice.
- [ ] `create_task(client, audio_path, opts)`: `POST /v1/speech-to-text/tasks`
      multipart. Fields per design.md §Transcribe. Use `httpx` multipart with
      the file streamed (`files={"file": (name, open_rb, content_type)}` and
      `data={...}` for text fields). `keyterms` = list of values → httpx sends
      repeated fields via `data={"keyterms": [w1, w2]}`.
- [ ] `poll_task(client, _id, timeout)`: `GET /v1/speech-to-text/tasks/{_id}`
      every 3s; stop on `state=="processed"`; print progress; raise on
      `last_error` or timeout.
- [ ] `export_task(client, _id, fmt, lang_code)`: `POST .../editor/export/{fmt}`
      body `{"language_code": lang_code}` (lang_code from poll result);
      return response bytes; Content-Disposition filename for the default name.
- [ ] `cmd_transcribe()`: wire validate→create→poll→export→write file.
- Validate: `python stt.py transcribe _tmp-sample.m4a --lang auto --subs -o out.srt`;
      assert `out.srt` exists, non-empty, starts with "1\n00:00:".

## M4 — Language table
- [ ] `LANGUAGES`: extract name→ISO639-3 from web JS bundle (re-open
      chrome-devtools browser → speech-to-text → 转录文件 → open combobox →
      `evaluate_script` to read each option's React fiber/value for the code;
      or grep the loaded chunk for the languages array). Fallback: best-effort
      common-language table + raw-code passthrough.
- [ ] `cmd_list_languages()`: print the table.
- Validate: `python stt.py list-languages | grep -i english` shows eng;
      `python stt.py transcribe _tmp-sample.m4a --lang eng` succeeds and poll
      result.language_code starts with "en"/"eng".

## M5 — Polish + self-check
- [ ] `--show-cost`: GET /cost?duration=<secs>, print estimate.
- [ ] README.md: install, login, transcribe, config, language reference.
- [ ] One `__main__` self-check: `python stt.py --version`; an `assert`-based
      `demo()` exercising config-load + multipart-field-builder (no network).
- Validate: `python stt.py --help`; `python -m stt` (if package) or
      `python stt.py selfcheck`.

## Validation commands (run before declaring done)
```bash
python stt.py --help
python stt.py transcribe --help
python stt.py list-languages | head
python stt.py login            # real one-time login
python stt.py transcribe _tmp-sample.m4a --lang auto --subs -o out.srt
test -s out.srt && head -6 out.srt
python stt.py transcribe _tmp-sample.m4a --lang eng --vocab "Maj3r,V社" --format vtt -o out.vtt
test -s out.vtt && head -4 out.vtt
```

## Risky points / rollback
- `language_code` ISO 639-3 vs the response's ISO 639-1 normalization — send
  ISO 639-3 on create, pass `result.language_code` to export. If export 400s,
  try null/omitting language_code in the export body.
- Multipart `keyterms` repeated-field format — if server rejects a list,
  try comma-joined string as fallback (verify against req1146 raw body).
- Playwright `channel="chrome"` may be unavailable on some machines — fall
  back to `msedge`; document manual refresh-token paste as last resort.
- 1 GB inline upload may be slow / memory-heavy — stream the file via
  `httpx` `files=` with an open file handle (don't read into memory).

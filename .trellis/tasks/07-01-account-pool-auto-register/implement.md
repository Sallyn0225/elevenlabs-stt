# Implement — Multi-account quota pool & auto-registration

> **STATUS (2026-07-01):** Steps 1, 2, 7 DONE & committed (offline, `stt selfcheck` green,
> real account migrated, `stt accounts`/`pool status` work). Step 3 (temp-email) de-risked.
> Steps 4–6 BLOCKED on captcha/login-verification gate — see design.md §Captcha Research.
> The signup flow is Email+Password+hCaptcha (NOT email-link); pyautogui+real-Chrome passes
> hCaptcha silently, but the post-signup login is rejected by ElevenLabs's beforeSignIn blocking
> fn (reads server-side verified state, not Firebase emailVerified). Next session: try the
> ordered experiments in design.md §Captcha Research → "Next experiments to try" (wait-for-
> propagation retry first). Q5 creds are in local config.toml (gitignored).

Inline workflow (Pi): the main session edits `stt.py` directly. Each step ends
with a runnable validation. Do not `task.py start` until the user reviews the
planning artifacts.

## Blockers / prerequisites

- **Q5 credentials OBTAINED** (temp-email base URL + admin password + domain; in
  local `config.toml`, gitignored). `/open_api/settings` probed: admin path viable,
  no Turnstile, no site password.
- **CAPTCHA GATE (new blocker):** see design.md §Captcha Research. Resolving the
  post-signup login verification gate is required before steps 4–6 can complete.

## Ordered checklist

### Step 1 — Config + account store + migration (R1, R5, AC4)
- Add `[temp_email]` and `[accounts]` sections to `load_config` / `config.example.toml`.
- New `ACCOUNTS_PATH = pathlib.Path("accounts.json")`. `load_accounts()` /
  `save_accounts()` (0o600). Schema per design.md.
- `migrate_session()`: if `session.json` exists and no `accounts.json`, read it →
  append one account (source=manual, credits_known unknown until first fetch) →
  save `accounts.json`. Keep `session.json` as backup (do not delete).
- `cmd_login`: write to `accounts.json` (append, source=manual) instead of
  overwriting `session.json`.
- **Validate:** `python stt.py selfcheck` (add config-parse + migration asserts);
  `python stt.py accounts` lists the migrated account (credits unknown/—).

### Step 2 — Credits fetch + selection (R2, AC1, AC6)
- `get_remaining(client)` → `GET /v1/user/subscription` → `limit - count`; cache
  into `account.credits_known` with `fetched_at`.
- `select_account(accounts, required, margin)`: filter sufficient (≥ required×margin,
  non-invalid), return `min` by remaining; else return None.
- Extend `cmd_transcribe`: load accounts → migrate if needed → compute required
  (cost endpoint; size-based fallback if no ffprobe) → select → set active → use.
  Log selected email + remaining + required. If single account + no pool config,
  behavior unchanged (R6).
- **Validate:** `python stt.py selfcheck` (selection best-fit+margin asserts);
  `python stt.py transcribe _tmp-sample.m4a --show-cost` uses migrated account,
  prints selected/remaining.

### Step 3 — Temp-email integration (R4 part 1)
- `temp_email_create(name=None)`: `POST {base}/admin/new_address`
  (`x-admin-auth`) → `{jwt, address}`. Fallback `POST /api/new_address` (user path
  + site password) on 403/401.
- `poll_parsed_mails(addr_jwt, timeout)`: `GET {base}/api/parsed_mails` (Bearer)
  every `poll_interval_secs`, dedupe by id, return first mail matching
  `oobCode=…` regex → return oobCode.
- **Validate (needs creds):** `python -c "from stt import temp_email_create;
  print(temp_email_create())"` creates a live address; manually send a test mail
  and confirm `poll_parsed_mails` reads it.

### Step 4 — Firebase email-link signup + provision (R4 part 2, AC2)
- `firebase_send_signin_link(email)`: `POST identitytoolkit/accounts:sendOobCode`
  `{requestType:EMAIL_SIGN_IN, email, continueUrl}`.
- `firebase_signin_with_link(email, oobCode)`: `POST .../accounts:signInWithEmailLink`
  → `{idToken, refreshToken, localId}`.
- `register_one()`: temp_email_create → send link → poll oobCode → signInWithEmailLink
  → `GET /v1/user` (provision) → `GET /v1/user/subscription` (record) → append to
  store (source=auto, temp_address) → return account. Frequency control (min 30s +
  jitter) between calls.
- **CAPTCHA GATE:** if `sendOobCode` returns `CAPTCHA_CHECK_FAILED`/AppCheck,
  stop and implement Step 8 (Playwright fallback) before continuing.
- **Validate (needs creds):** `python -c "from stt import register_one;
  print(register_one()['email'])"` produces a working account; `stt accounts`
  shows it with ~10000 remaining; `stt transcribe` can use it.

### Step 5 — On-demand register fallback in transcribe (AC2)
- In `cmd_transcribe`, if `select_account` returns None: call `register_one()`;
  on failure `SystemExit("registration failed: <reason>")`. Use the new account.
- **Validate (needs creds):** deplete/clear accounts so none sufficient →
  `stt transcribe _tmp-sample.m4a` auto-registers and completes.

### Step 6 — Pool warming + auto-refill (R3, AC5)
- `cmd_pool_warm(target)`: `while fresh_count() < target: register_one()`
  (frequency-controlled).
- `cmd_pool_status`: print fresh/usable/depleted/invalid counts.
- Auto-refill: at end of `cmd_transcribe`, if `auto_refill` and
  `fresh_count() < pool_target`, register replacements (wrapped so Ctrl+C skips
  cleanly with a message).
- **Validate (needs creds):** `stt pool warm --target 2` → 2 fresh accounts;
  `stt pool status` counts correct; after a transcription, refill runs.

### Step 7 — CLI subcommands + selfcheck (AC3, AC7, AC8)
- Wire `accounts` (with `--refresh`), `pool` (`warm`/`status`) subcommands in
  `build_parser`.
- Extend `_selfcheck`: config parse, selection best-fit+margin, credit arithmetic,
  oobCode regex, session→accounts migration — all offline.
- **Validate:** `python stt.py selfcheck` green; `python stt.py accounts`;
  `python stt.py pool status`.

### Step 8 — Playwright signup fallback (only if Step 4 captcha gate hit)
- `register_one_browser()`: Playwright → elevenlabs signup → fill email → submit
  → poll inbox → `page.goto(link)` → `wait_for_function` localStorage firebase
  user → read refreshToken (reuse `cmd_login` extraction) → provision + record.
- `register_one()` tries REST, falls back to browser on captcha/AppCheck.
- **Validate:** `register_one()` succeeds when REST is blocked.

### Step 9 — Docs
- Update `README.md`/`README.en.md`: new commands, `[temp_email]`/`[accounts]`
  config, ToS/risk note, `accounts.json` mention.
- Update `config.example.toml` with commented new sections.
- Update `docs/api-contract.md` with `/v1/user/subscription` + signup endpoints.

## Validation commands (cumulative)
```bash
python stt.py selfcheck                       # offline, every step
python stt.py accounts                        # list + credits
python stt.py accounts --refresh              # re-fetch credits
python stt.py pool status                     # pool counts
python stt.py pool warm --target 2            # warm (needs creds)
python stt.py transcribe _tmp-sample.m4a --show-cost   # end-to-end
```

## Risky files / rollback points
- `stt.py` — all logic; single file, largest risk surface. Rollback:
  `git checkout stt.py`.
- `accounts.json` — new file (gitignored). Rollback: delete it; `session.json`
  is preserved so single-account behavior restores.
- `config.example.toml` — additive only.
- `session.json` — **read-only migration source; never overwritten after step 1**.
- Each step is independently revertible; commit after steps 1, 2, 4, 6, 7.

## Follow-up checks before finish
- `python stt.py selfcheck` green.
- One full end-to-end: pool warm → transcribe (auto-select) → auto-refill.
- Verify no secrets/JWTs printed in `accounts`/logs (redact).
- `trellis-check` pass + spec update (capture Firebase email-link + temp-email
  integration notes into `.trellis/spec/`).

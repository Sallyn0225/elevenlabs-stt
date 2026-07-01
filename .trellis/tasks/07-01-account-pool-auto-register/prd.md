# Multi-account quota pool & auto-registration

> **CORRECTION (2026-07-01, live research):** The signup flow is **Email+Password +
> hCaptcha**, NOT email-link (Q1 answer was wrong). And hCaptcha is **always enforced on
> signup** via a Firebase blocking Cloud Function (Q2 "risk-triggered" was wrong for signup;
> it IS risk-triggered for the challenge widget but the reCAPTCHA Enterprise check is always
> present). See design.md §Captcha Research for the verified flow + current blocker.

## Goal

Eliminate the manual "register a new ElevenLabs account → log in → grab token"
cycle when the free 10k-credit monthly quota runs out. The CLI maintains a
pre-warmed pool of accounts, auto-selects a sufficient one (best-fit + safety
margin) before each transcription, automatically refills the pool when an account
is consumed, and — when the pool is exhausted mid-task — registers a fresh account
on demand via the user's self-hosted temp-email service. Target: unattended
`stt transcribe` that never fails on "out of credits" as long as the temp-email
infra is reachable.

## Background / Current State

- `stt.py`: single-file Python CLI. `stt login` opens Chrome via Playwright, reads
  the Firebase user object from `localStorage`, stores `refreshToken`
  (+ email/localId/jwt cache) in `session.json`. `stt transcribe` refreshes the 1h
  JWT via Firebase `securetoken` and calls `api.us.elevenlabs.io` internal STT
  endpoints (`docs/api-contract.md`).
- Single free account ≈ 10,000 credits (≈12 min audio/month, ≈13.9 credits/sec).
  Out of credits ⇒ server rejects upload, CLI errors. Today the user must manually
  register + log in to a new account.
- User deployed `dreamhunter2333/cloudflare_temp_email` on their own Cloudflare
  (own domain). The existing `session.json` email (`annahorwin@edu.misuzu.mom`) is
  already a temp-email address on that domain — manual temp-email registration is
  proven. ElevenLabs signup uses an **emailed sign-in link** (Firebase Email Link,
  no password); captcha is risk-triggered only (rare with frequency control).

## Confirmed Facts (research + user, 2026-07-01)

### ElevenLabs auth & quota
- Auth = Firebase (apiKey `AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys`, project
  `xi-labs`). JWT (Bearer) 1h, refreshed via `securetoken.googleapis.com/v1/token`.
- **Remaining credits (CONFIRMED):** `GET /v1/user/subscription` (Bearer JWT) →
  `subscription.character_limit` (10000 free) − `subscription.character_count`.
  STT and TTS share this pool. (`/v1/user` also nests `subscription.*`.)
- **Signup = Firebase Email Link (user-confirmed):**
  1. `POST identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=<apiKey>`
     `{requestType:"EMAIL_SIGN_IN", email, continueUrl}` → Firebase emails a link
     containing `oobCode`.
  2. Extract `oobCode` from the link URL in the temp inbox (regex `oobCode=…`).
  3. `POST identitytoolkit.googleapis.com/v1/accounts:signInWithEmailLink?key=<apiKey>`
     `{email, oobCode}` → `idToken`(JWT) + `refreshToken` + `localId`
     (create-or-sign-in; email verified by the link).
  4. `GET api.us.elevenlabs.io/v1/user` (Bearer JWT) → backend auto-provisions
     workspace + free subscription; `workspace_id` minted into JWT claims.
  - **RISK:** Firebase REST `sendOobCode` may enforce reCAPTCHA/AppCheck even when
    the web UI's captcha is risk-triggered-only. If REST is blocked, fall back to a
    Playwright browser-driven signup that reuses the existing `localStorage`
    extraction (see design.md §Risks). To be verified at first implementation.
- **Captcha:** risk-triggered only (user-confirmed). Control registration frequency
  (jitter + small delays) to avoid triggering; no solver in MVP.
- **Risk:** rapid account creation from one IP ⇒ ElevenLabs temp-bans the IP.

### Temp-email service (cloudflare_temp_email, source-verified)
- Create address: `POST /admin/new_address` (admin path, header
  `x-admin-auth: <ADMIN_PASSWORD>`; bypasses user-create gate, Turnstile, PREFIX)
  → `{jwt, address, address_id, password}`. Fallback `POST /api/new_address`
  (user path; needs `ENABLE_USER_CREATE_EMAIL`, site password `x-custom-auth`,
  Turnstile `cf_token`, honors `PREFIX`) — same response shape.
- Read mail: `GET /api/parsed_mails?limit=20&offset=0` with
  `Authorization: Bearer <address_jwt>` → `{results:[{id, subject, text, html,
  ...}], count}`. Regex `oobCode` from `subject`/`text`/`html`. Poll ~3s, dedupe
  by `id`.
- Address JWT: HS256, no `exp` (won't expire unless `JWT_SECRET` rotated). Site
  password header only if `/open_api/settings` → `needAuth:true`. Idle addresses
  purged by scheduled cleanup; polling refreshes `updated_at`. CORS wide open.

## Requirements

### R1 — Multi-account store
- Persist a pool of accounts in `accounts.json`: `{email, refreshToken, localId,
  jwt, jwt_exp, source:"manual"|"auto", created_at, credits_known:{limit,count,
  fetched_at}, invalid:bool}`. Active-account pointer = the one selected per task.
- Migrate existing `session.json` into `accounts.json` on first run (source=manual).
- `stt accounts` lists all accounts with remaining credits + active marker.

### R2 — Pre-flight credit check & selection (best-fit + safety margin)
- Before upload, compute required credits via `GET /v1/speech-to-text/cost?duration`
  (ffprobe duration); if duration unknown, estimate from file size as a fallback.
- For each candidate account, refresh JWT (if expired) and fetch remaining via
  `GET /v1/user/subscription`. Skip `invalid` accounts.
- Selection: among accounts with `remaining ≥ required × margin` (margin default
  1.2, configurable), pick the one with the **smallest remaining** that still
  covers (best-fit: consumes near-empty accounts first, preserves big accounts).
- If the active account is insufficient but another sufficient account exists →
  auto-fallback (switch active, no user action). Log selected account + reason.
- If NO existing account is sufficient → trigger R4 (on-demand register), then use.

### R3 — Pre-warmed pool maintenance
- Config `pool_target` (default 3): desired count of fresh (full-quota) accounts.
- `stt pool warm [--target N]`: register accounts until N fresh exist.
- Auto-refill: after a transcription consumes an account, if fresh-count <
  pool_target, register replacements (synchronous, at end of run, before exit;
  configurable `auto_refill=true`, Ctrl+C-able). Realizes "用掉就补" without a
  daemon. No background daemon in MVP (YAGNI).
- An account is "fresh" when remaining ≥ `fresh_threshold` (default 10000, i.e.
  untouched); "depleted" when remaining < next-task need; "invalid" when auth
  fails 3× (quarantine, skip selection).

### R4 — Auto-registration via temp-email (on-demand fallback + pool warming)
- Create a temp address (admin path primary; user path fallback) on the user's
  cloudflare_temp_email instance.
- Drive ElevenLabs email-link signup end-to-end: `sendOobCode(EMAIL_SIGN_IN)` →
  poll temp inbox → extract `oobCode` → `signInWithEmailLink` → `GET /v1/user`
  (provision workspace) → `GET /v1/user/subscription` (record credits).
- Add the new account (source=auto) to the store, then use it (on-demand) or count
  it toward pool_target (warming).
- If REST `sendOobCode` is captcha-blocked → Playwright browser fallback (drive the
  signup page, read `localStorage` Firebase user — reuses existing login path).
- Frequency control: min interval + jitter between registrations.

### R5 — Config & secrets
- New `[temp_email]` config section: `base_url`, `admin_password`, `site_password`
  (optional), `domain`, `use_admin_path` (default true), `poll_interval_secs`,
  `poll_timeout_secs`. New `[accounts]` section: `pool_target`, `fresh_threshold`,
  `selection_margin`, `auto_refill`. Secrets stay in `config.toml` (gitignored).

### R6 — Backward compatibility / graceful degradation
- `stt transcribe` works with a single account if no pool/temp-email config
  present (degrades to current behavior).
- `stt login` retained as manual-import path for pre-existing accounts.

## Acceptance Criteria

- [ ] AC1: `stt transcribe <audio>` with active account out-of-credits and ≥1
      existing sufficient account → transcribes via best-fit account, no manual
      step, output written; logs selected account + remaining credits.
- [ ] AC2: `stt transcribe <audio>` with NO existing sufficient account →
      auto-registers via temp-email, adds to store, completes transcription.
- [ ] AC3: `stt accounts` lists all accounts with remaining credits, source, and
      active marker; `stt accounts refresh` re-fetches credits.
- [ ] AC4: Existing `session.json` migrated into `accounts.json` on first run;
      `stt transcribe` still works.
- [ ] AC5: `stt pool warm --target 3` registers until 3 fresh accounts exist;
      after a transcription drops fresh-count below target, auto-refill registers
      a replacement before exit.
- [ ] AC6: Selection uses best-fit + margin: picks smallest-sufficient account;
      never selects an account below `required × margin`.
- [ ] AC7: `selfcheck` covers config parsing, selection policy, credit arithmetic,
      and oobCode regex extraction — all offline (no network).
- [ ] AC8: `stt login` still imports a pre-existing account into the store.

## Out of Scope (MVP)

- Background daemon / always-on pool warmer (use `stt pool warm` / cron instead).
- IP rotation / proxy pooling, captcha solver service, paid-tier/billing automation.
- Cross-machine account sync.
- Pre-provisioning more than `pool_target` accounts.

## Open Questions (block implementation, not planning)

- **Q5 — Temp-email deployment credentials:** base URL, admin password, site
  password (if `needAuth`), a domain, and flag confirmations
  (`ENABLE_USER_CREATE_EMAIL`/`PREFIX`/`DISABLE_CUSTOM_ADDRESS_NAME`/Turnstile).
  Minimum for the admin path = base URL + admin password + domain. Required before
  implementation/testing; not needed for planning docs.

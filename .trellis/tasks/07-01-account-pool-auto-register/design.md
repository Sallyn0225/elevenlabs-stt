# Design — Multi-account quota pool & auto-registration

## Architecture

Stay single-file (`stt.py`) to match the repo convention and the "fewest files"
rule. Add new sections with header banners. New logical components:

```
stt.py
  ├─ config          (existing + [temp_email] + [accounts] sections)
  ├─ firebase auth   (existing refresh + new sendOobCode / signInWithEmailLink)
  ├─ elevenlabs api  (existing STT + new /v1/user, /v1/user/subscription)
  ├─ temp-email api  (NEW: admin/new_address, parsed_mails polling)
  ├─ account store   (NEW: accounts.json load/save/select/migrate)
  ├─ registration    (NEW: register_one() = temp-email + firebase-link flow)
  ├─ pool            (NEW: warm / refill / state machine)
  └─ subcommands     (login, transcribe, accounts, pool, list-languages, selfcheck)
```

No new runtime dependencies beyond `httpx` (already used) and `playwright`
(already used for login, reused for signup fallback). stdlib `json/secrets/time/
re/pathlib` for the rest.

## Data Flow

### transcribe (extended)
```
load accounts.json (migrate session.json if present & no accounts.json)
compute duration (ffprobe) → required = GET /cost?duration (or size-based est)
for each non-invalid account:
    refresh JWT if expired (securetoken)
    remaining = GET /v1/user/subscription → limit - count   (cache w/ fetched_at)
candidates = [a for a if remaining >= required * margin]
if candidates:
    chosen = min(candidates, key=remaining)          # best-fit + margin
    log "selected <email> remaining=<r> required=<req>"
else:
    log "no sufficient account; registering on-demand"
    chosen = register_one()                          # R4
    if chosen is None: SystemExit("registration failed: <reason>")
use chosen (authed_client) → create_task → poll → export → write file
save accounts.json (updated credits)
if auto_refill and fresh_count() < pool_target:
    register replacements until fresh_count == pool_target   # Ctrl+C-able
save accounts.json
```

### register_one() — R4 (REVISED 2026-07-01 after live captcha research)
Signup is Email+Password with hCaptcha (NOT email-link as PRD assumed). hCaptcha
detects CDP/Playwright automation → only pyautogui on a real Chrome (no CDP
attached during form submits) passes silently. Full flow:
```
addr = temp_email_create()              # POST /admin/new_address → {jwt, address}  [VERIFIED]
password = random meeting ≥8 letters + ≥1 number + ≥1 special
# 1. pyautogui launch REAL Chrome (no CDP) → goto /app/sign-up            [VERIFIED: hCaptcha silent pass]
#    click email field (window center-x, 56% height) → pyperclip paste → Tab → paste password → Enter
# 2. poll temp inbox for verify email                                    [VERIFIED]
#    subject "Verify your email for ElevenLabs", link .../app/action?mode=verifyEmail&oobCode=<code>
oobCode = regex oobCode=([A-Za-z0-9_-]+) from email html/text
# 3. REST verify Firebase side                                            [VERIFIED]
POST identitytoolkit/v1/accounts:update {oobCode} → emailVerified:true   (no captcha)
# 4. pyautogui LOGIN on /app/sign-in (CDP NOT attached)                   [BLOCKED — see below]
#    Ctrl+L → paste sign-in URL → Enter; click email (62% height) → paste → Tab → paste → Enter
# 5. attach CDP ONLY NOW → read localStorage firebase:authUser:... → refreshToken  [UNTESTED]
# 6. REST refresh JWT + GET /v1/user/subscription → record 10000 credits   [UNTESTED]
account = {email, password, refreshToken, localId, jwt, jwt_exp, source:"auto",
           temp_address:addr, created_at, credits_known:{limit,count,fetched_at}}
save to store; return account
```
**CURRENT BLOCKER (step 4):** login submit re-triggers "send verify email" instead of
signing in. The ElevenLabs beforeSignIn blocking function rejects with "email has not
been verified" EVEN THOUGH Firebase `accounts:update` returned `emailVerified:true`.
So the blocking fn reads ElevenLabs SERVER-SIDE state (Firestore doc / custom claim
written by a Firebase Auth `onUpdate:emailVerified` trigger), NOT the Firebase
emailVerified field directly. REST `signInWithPassword` is also blocked the same way.
Next experiments for the next session (see design.md §Captcha Research).

Dependencies added (pip, kept): pyautogui, pygetwindow, pyperclip,
playwright-stealth (2.0.3, not effective vs hCaptcha Enterprise but installed).

### pool warm
```
fresh = count(accounts with remaining >= fresh_threshold)
while fresh < target: register_one(); fresh += 1   (frequency-controlled)
```

### login (unchanged mechanism, new output)
Playwright reads localStorage Firebase user → build account record
(source="manual") → append to accounts.json (instead of overwriting session.json).
Keep writing session.json too for backward-compat? No — migrate to accounts.json;
leave a `session.json` symlink/compat shim only if R6 degrades. Decision:
`stt login` writes to accounts.json; `session.json` is read-only migration source.

## Contracts

### accounts.json
```json
{
  "accounts": [
    {
      "email": "x@edu.misuzu.mom",
      "refreshToken": "...", "localId": "...",
      "jwt": "...", "jwt_exp": 1782889811.0,
      "source": "auto",            // "manual" | "auto"
      "temp_address": "x@edu.misuzu.mom",  // for re-sign-in recovery
      "created_at": 1782889811.0,
      "credits_known": {"limit": 10000, "count": 1234, "fetched_at": 1782889811.0},
      "invalid": false, "invalid_reason": null
    }
  ],
  "active": "x@edu.misuzu.mom"     // last-used; selection overrides per-task
}
```
File perms 0o600 (same as session.json). gitignored.

### config.toml (new sections)
```toml
[temp_email]
base_url = "https://mail.example.com"   # temp-email backend
admin_password = "..."                    # x-admin-auth (preferred path)
site_password = ""                        # x-custom-auth, only if needAuth
domain = "edu.misuzu.mom"                 # from /open_api/settings domains[]
use_admin_path = true                     # /admin/new_address vs /api/new_address
poll_interval_secs = 3
poll_timeout_secs = 120

[accounts]
pool_target = 3
fresh_threshold = 10000
selection_margin = 1.2
auto_refill = true
```

### Temp-email HTTP
- `POST {base_url}/admin/new_address` header `x-admin-auth: <admin_password>`
  body `{"name":"", "domain":"<domain>", "cf_token":"", "enableRandomSubdomain":false}`
  → `{"jwt","address","address_id"}`. (Random name: `name=""`. Custom local-part:
  `name="abc12"` — only if PREFIX empty / admin path.)
- `GET {base_url}/api/parsed_mails?limit=20&offset=0` header
  `Authorization: Bearer <address_jwt>` → `{"results":[{"id","subject","text","html"}]}`.

### Firebase HTTP (identitytoolkit, key=FIREBASE_API_KEY)
- `sendOobCode`: `POST .../accounts:sendOobCode?key=K`
  `{"requestType":"EMAIL_SIGN_IN","email":"<addr>","continueUrl":"https://elevenlabs.io"}`
- `signInWithEmailLink`: `POST .../accounts:signInWithEmailLink?key=K`
  `{"email":"<addr>","oobCode":"<code>"}` → `{"idToken","refreshToken","localId","emailVerified"}`

### ElevenLabs HTTP (api.us.elevenlabs.io, Bearer JWT)
- `GET /v1/user/subscription` → `{"subscription":{"character_limit","character_count",...}}`
- `GET /v1/user` → user object (nests `subscription`; auto-provisions workspace)
- existing: `/v1/speech-to-text/cost`, `/tasks`, `/tasks/{id}`, export.

### CLI surface
```
stt login                     import pre-existing account → accounts.json
stt transcribe <audio>        select/register/use + auto-refill (R1-R4)
stt accounts [--refresh]      list accounts + remaining credits + active
stt pool warm [--target N]    register until N fresh accounts
stt pool status               fresh/depleted/invalid counts
stt list-languages            (existing)
stt selfcheck                 (existing + new offline checks)
```

## Selection algorithm (R2)
```
required = cost(duration) or size_estimate
margin = config.selection_margin (default 1.2)
needed = ceil(required * margin)
sufficient = [a for a in accounts if not a.invalid and remaining(a) >= needed]
chosen = min(sufficient, key=lambda a: remaining(a))   # best-fit
```
Rationale: best-fit consumes near-empty accounts first (don't burn a fresh 10k
account on a 500-credit job); margin guards against estimate underflow causing
mid-task exhaustion (server rejects mid-upload if credits hit 0 → wasted upload).

## Pool state machine (R3)
- `fresh`: remaining ≥ fresh_threshold (10000) — full quota, preferred for warming.
- `usable`: 0 < remaining < fresh_threshold — has some credits, selectable.
- `depleted`: remaining == 0 — not selectable, candidate for replacement.
- `invalid`: auth refresh failed 3× → quarantined (invalid=true), skipped.
Warm target counts `fresh + usable-with-credits`? No — `pool_target` counts
accounts with remaining ≥ fresh_threshold (truly fresh). After refill, depleted
accounts remain in store (history) but aren't counted; `stt pool prune` (future)
could drop them. MVP: keep all, count fresh for target.

## Compatibility / Migration
- First run with `session.json` present and no `accounts.json`: migrate the single
  account into `accounts.json` (source=manual, fetch credits on first transcribe).
  Keep `session.json` untouched (backup) — don't delete user data.
- No `temp_email` config + single account → `stt transcribe` behaves exactly as
  today (R6 degradation). Multi-account/register features no-op gracefully.
- `config.example.toml` updated with new sections (commented examples).

## Tradeoffs
- **REST signup vs Playwright fallback:** REST is preferred (no browser, no
  selectors, fast). Risk: Firebase may enforce reCAPTCHA/AppCheck on the REST
  `sendOobCode`. Fallback reuses the proven Playwright+localStorage path. We
  implement REST first, test once, add fallback only if REST is blocked. Avoids
  building two paths unless needed (ponytail).
- **Synchronous auto-refill vs daemon:** refill at end-of-transcribe (sync) keeps
  the pool warm without a daemon process. Cost: a few seconds at exit. Acceptable
  for a personal CLI; Ctrl+C skips it.
- **Single-file vs modules:** single-file matches repo + fewest-files rule. If
  stt.py exceeds ~900 lines, extract `tempmail`/`accounts`/`register` into a
  `lib/` — deferred (ponytail: don't pre-split).
- **Admin vs user temp-email path:** admin path bypasses gates/Turnstile/prefix →
  primary. User path kept as fallback for deployments without admin password.

## Risks & Rollback
- **R1 Firebase REST captcha block** → Playwright fallback (above). Detect by
  `sendOobCode` 400 with `CAPTCHA_CHECK_FAILED`/AppCheck message.
- **R2 IP temp-ban** → frequency control (min 30s + jitter between registrations);
  on repeated 429/ban signals, back off and surface to user. No proxy rotation MVP.
- **R3 ElevenLabs changes signup flow** → email-link flow is Firebase-standard and
  stable; if it breaks, Playwright fallback is more resilient (drives real UI).
- **R4 Temp-email address expiry after signup** → irrelevant: we keep the Firebase
  refreshToken, not the address. For re-sign-in recovery, re-create the address
  (same local-part via admin path) and send a fresh link. Store `temp_address`.
- **R5 ToS** → mass account creation likely violates ElevenLabs ToS; documented in
  README; rate-limited; user accepts risk (their infra, their accounts).
- **R6 Secrets in config.toml** → gitignored, 0o600 not required for config but
  documented; never log passwords/JWTs (redact in `stt accounts`/logs).
- **Rollback:** feature is additive; `accounts.json` is a new file, `session.json`
  preserved. Reverting the code restores single-account behavior. No schema
  migration needed to roll back.

## Self-check additions (AC7)
Offline `selfcheck` asserts:
- `[temp_email]`/`[accounts]` config parsing + defaults.
- selection: given fake accounts + required, returns best-fit+margin pick.
- credit arithmetic: remaining = limit - count.
- `oobCode` regex extracts from a sample Firebase link HTML.
- accounts.json migration: a fake session.json → accounts.json record.
No network.

## Captcha Research (2026-07-01, live experiments)

### Real signup flow (reality vs PRD assumptions)
- **NOT email-link.** Signup = Email+Password form at `/app/sign-up`. After submit,
  ElevenLabs emails a **verification link** `https://elevenlabs.io/app/action?mode=verifyEmail&oobCode=<code>`
  (subject "Verify your email for ElevenLabs"). This is Firebase `sendOobCode(VERIFY_EMAIL)`
  triggered server-side after signup; `mode=verifyEmail` (EMAIL_VERIFY), NOT EMAIL_SIGNIN.
- hCaptcha Enterprise gates the signup submit. Manual normal Chrome (no automation)
  passes **silently** (user-confirmed). CDP/Playwright automation triggers the
  9-grid image challenge regardless of headed/headless/stealth.

### What was VERIFIED working
1. **pyautogui + real Chrome (NO CDP attached) signup → hCaptcha silent pass**
   Reproduced 5×. Flow: temp_email_create(admin) → launch Chrome (no debug port,
   default profile) → pyautogui click email field (window center-x, **56% height**)
   → pyperclip paste email (clipboard paste, NOT type() — bypasses IME) → Tab →
   paste password → Enter → poll `/api/parsed_mails` ~3s → verify email arrives.
   `--remote-debugging-port` open-but-unconnected did NOT trigger hCaptcha on signup.
2. **Firebase REST `accounts:update{oobCode}` → `emailVerified:true`** (no captcha).
3. temp-email admin path fully working (see Q5 creds below).

### What FAILED (5 attempts)
1. Firebase REST `accounts:signUp{email,password}` → `BLOCKING_FUNCTION_ERROR: Invalid recaptcha received`.
2. Firebase REST `accounts:sendOobCode{EMAIL_SIGNIN}` → `OPERATION_NOT_ALLOWED`.
3. Firebase REST `accounts:signInWithPassword` (after update set emailVerified:true)
   → `BLOCKING_FUNCTION_ERROR: email has not been verified`. **The ElevenLabs beforeSignIn
   blocking fn checks SERVER-SIDE state, NOT Firebase emailVerified.** Background agent
   confirmed (Next.js bundle `25vtur_8muxmr.js` `handleVerifyEmail`): the frontend
   `/app/action` handler calls ONLY Firebase `applyActionCode`(=accounts:update) + a
   PostHog event — NO second ElevenLabs-backend HTTP call. The server-side verified
   state is written by a Firebase Auth `onUpdate:emailVerified` trigger → Firestore/custom claim.
4. Plain Playwright headed chrome, playwright-stealth 2.0.3, CDP-instant, CDP-human-like
   → ALL triggered hCaptcha on signup.
5. CDP-attached Chrome (even with pyautogui driving) → hCaptcha on login submit.
   Signup does NOT auto-sign-in (post-signup localStorage firebase user = null).

### Current blocker (step 4 of register_one)
Login submit via pyautogui (no CDP) **re-triggers "send verify email"** instead of
signing in — same blocking-fn rejection. The server-side verified state has not been
flipped by REST `accounts:update` alone.

### Next experiments to try (ordered, for next session)
1. **Wait for server-side propagation.** `accounts:update` returns immediately but the
   `onUpdate:emailVerified` trigger → Firestore/claim write may be async. Retry
   `signInWithPassword` at +10s, +30s, +60s, +120s. If it eventually passes → no browser
   login needed; REST sign-in after a wait. CHEAPEST FIX, TRY FIRST.
2. **Let the frontend verify link run in the real Chrome.** After REST `accounts:update`,
   pyautogui-navigate the SAME Chrome to the verify link (`/app/action?mode=verifyEmail&oobCode=...`)
   and let the Next.js `handleVerifyEmail` handler execute (it may call an
   ElevenLabs API we haven't found, or the trigger fires on the client visit). Then
   pyautogui-login. This is what a real user does.
3. **Login WITHOUT prior REST update** — maybe REST `accounts:update` flips Firebase
   emailVerified but does NOT fire the ElevenLabs onUpdate trigger (trigger fires on
   genuine client applyActionCode only). So skip REST update, navigate the verify link
   in real Chrome instead, THEN login.
4. If all REST/browser-login paths stay blocked: semi-auto fallback (user solves the
   one hCaptcha on login) OR paid hCaptcha solver. User prefers A (full auto).

### Credentials (Q5, for next session — DO NOT COMMIT)
- temp-email base_url: `https://apimail.misuzu.mom`, admin_password: `<ADMIN_PASSWORD>` (redacted; in local config.toml),
  domain: `edu.hmhnk.com`. `/open_api/settings`: prefix="", needAuth=false,
  enableUserCreateEmail=true, no Turnstile, version v1.9.0. Domains: misuzu.mom,
  sallyn.top, edu.misuzu.mom, edu.mamimi.site, hmhnk.com, edu.hmhnk.com.
- Already in local `config.toml` (gitignored).
- Test accounts created (disposable, in temp-email, no ElevenLabs workspace confirmed):
  el1782894750/5775/6088/6204/6462/6538/6825@edu.hmhnk.com (password redacted).

### Dependencies (pip, kept)
pyautogui, pygetwindow, pyperclip, playwright-stealth 2.0.3 (installed, not effective
vs hCaptcha Enterprise but kept for potential later use). Chrome path:
`C:\Program Files\Google\Chrome\Application\chrome.exe`.

### Coordinate calibration (measured via Playwright evaluate on /app/sign-in)
- Login page email input center: cx=457, cy=493 in a 929×865 viewport → content
  y-fraction ≈0.57. With ~100px chrome bar on a 900px window → window-height fraction ≈**0.62**.
- Signup page email field ≈ **0.56** of window height (empirically verified).
- Use clipboard paste (pyperclip + Ctrl+V), NOT pyautogui.write() — IME intercepts type().

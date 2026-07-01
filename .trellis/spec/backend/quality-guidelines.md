# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

<!--
Document your project's quality standards here.

Questions to answer:
- What patterns are forbidden?
- What linting rules do you enforce?
- What are your testing requirements?
- What code review standards apply?
-->

This repo is a small single-file Python CLI (`stt.py`). Prefer the smallest direct
function change over adding layers or services.

---

## Scenario: Captcha-gated account auto-registration

### 1. Scope / Trigger
- Trigger: implementing or changing ElevenLabs auto-registration, pool warming, or
  transcribe auto-refill.
- This is an infra integration: real Chrome UI automation, Firebase auth, temp-mail,
  and ElevenLabs subscription quota must agree before an account enters `accounts.json`.

### 2. Signatures
- CLI: `python stt.py pool warm [--target N]`
- CLI: `python stt.py transcribe <audio> [--show-cost]`
- Code: `register_one() -> dict[str, Any]`
- Code: `refill_pool(store: dict[str, Any], config_path: pathlib.Path) -> None`

### 3. Contracts
- `register_one()` must create a fresh temporary Chrome profile using
  `--user-data-dir=<tempdir>` for each account.
- It must activate only the newly-created Chrome window. Never pick an arbitrary
  existing Chrome window; that can operate on the user's personal logged-in profile.
- It must close the launched Chrome process tree and delete the temporary profile
  directory in a `finally` block. Pool warming 10 accounts must not leave 10 browsers open.
- Working signup order:
  1. temp-mail address
  2. real Chrome `/app/sign-up` email/password submit
  3. poll verification link
  4. open `/app/action?...oobCode=...`
  5. click Email Verification **Continue**
  6. sign in on the web page with the same email/password
  7. REST `accounts:signInWithPassword`
  8. `GET /v1/user/subscription` and store credits
- `refill_pool()` must preserve the transcribe-selected `store["active"]`; refill
  registrations must not steal the active marker.

### 4. Validation & Error Matrix
- No newly-created Chrome window detected -> abort before any clicks.
- Existing Chrome only -> abort; do not operate on it.
- Registration success or failure -> launched Chrome process tree is killed and temp profile is removed.
- Verify modal not completed -> REST sign-in returns `email has not been verified`.
- Temp-mail unavailable -> registration fails; single-account mode should still work
  when no temp-email config is present.

### 5. Good/Base/Bad Cases
- Good: `pool warm --target 3` creates three fresh accounts with 10000 remaining.
- Base: one usable account plus three fresh accounts; best-fit picks the usable one.
- Bad: refill calls `upsert_account()` and leaves the new refill account active.

### 6. Tests Required
- Offline: `python -m py_compile stt.py`, `python stt.py selfcheck`, `git diff --check`.
- Live when credentials are available: `stt pool warm --target 1`, then `--target 2`
  or `--target 3`, plus one short `stt transcribe ... --show-cost` to confirm
  best-fit selection and auto-refill.

### 7. Wrong vs Correct

#### Wrong
```python
wins = [w for w in gw.getAllWindows() if "Google Chrome" in w.title]
wins[0].activate()  # may be the user's personal Chrome profile
```

#### Correct
```python
before = {window_key(w) for w in gw.getAllWindows() if "Google Chrome" in w.title}
# launch temp-profile Chrome
new_window = next(w for w in gw.getAllWindows() if window_key(w) not in before)
new_window.activate()
```

---

## Forbidden Patterns

<!-- Patterns that should never be used and why -->

(To be filled by the team)

---

## Required Patterns

<!-- Patterns that must always be used -->

(To be filled by the team)

---

## Testing Requirements

<!-- What level of testing is expected -->

(To be filled by the team)

---

## Code Review Checklist

<!-- What reviewers should check -->

(To be filled by the team)

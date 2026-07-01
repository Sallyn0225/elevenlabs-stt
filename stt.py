#!/usr/bin/env python3
"""ElevenLabs web Speech-to-Text CLI.

Replays the web app's internal API (api.us.elevenlabs.io) using the user's own
logged-in Firebase session. See research/api-contract.md for the captured
contract and README.md for usage.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any

import httpx

# ponytail: constants centralised so endpoint renames are one-line fixes
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
FIREBASE_USER_KEY = f"firebase:authUser:{FIREBASE_API_KEY}:[DEFAULT]"
API_BASE = "https://api.us.elevenlabs.io"
SECURETOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
IDENTITY_URL = f"https://identitytoolkit.googleapis.com/v1/accounts"
FIREBASE_REFERER = "https://elevenlabs.io/"  # API key is referer-restricted; required on googleapis calls
LOGIN_URL = "https://elevenlabs.io/app/speech-to-text"

SESSION_PATH = pathlib.Path("session.json")  # legacy single-account file (migration source)
ACCOUNTS_PATH = pathlib.Path("accounts.json")  # multi-account pool (migration target)
CONFIG_PATH = pathlib.Path("config.toml")

MAX_FILE_BYTES = 1000 * 1024 * 1024  # 1000 MB hard limit
FREE_DURATION_WARN_SECS = 600  # 10 min soft warn for free accounts
POLL_INTERVAL_SECS = 3
CREDITS_PER_SEC = 13.9  # observed free-tier STT rate (research); pre-flight estimate
CREDITS_CACHE_TTL = 300  # reuse fetched credits within this window to limit token rotations

EXPORT_FORMATS = ("srt", "vtt", "txt", "json", "html", "pdf", "docx")

DEFAULTS: dict[str, Any] = {
    "language": "auto",
    "tag_audio_events": True,
    "include_subtitles": True,  # script default ON (web default is OFF)
    "no_verbatim": False,
    "use_speaker_library": False,
    "vocab": [],
    "export_format": "srt",
    "poll_timeout_secs": 600,
    "show_cost": False,
}

# ponytail: section defaults kept as flat dicts; merged over by config.toml sections
TEMP_EMAIL_DEFAULTS: dict[str, Any] = {
    "base_url": "",            # cloudflare_temp_email backend URL
    "admin_password": "",      # x-admin-auth (admin path; bypasses gates)
    "site_password": "",       # x-custom-auth, only if /open_api/settings needAuth
    "domain": "",              # a domain from /open_api/settings domains[]
    "use_admin_path": True,    # /admin/new_address vs /api/new_address
    "poll_interval_secs": 3,
    "poll_timeout_secs": 120,
}
ACCOUNTS_CFG_DEFAULTS: dict[str, Any] = {
    "pool_target": 3,          # desired count of fresh (full-quota) accounts
    "fresh_threshold": 10000,  # remaining >= this => "fresh"
    "selection_margin": 1.2,   # require remaining >= cost * margin
    "auto_refill": True,       # refill pool to target after each transcription
}

# Language name -> ISO 639-3 code. Subset of the web combobox's 157 entries;
# any ISO 639-3 code can also be passed directly to --lang. See
# research/modal-ui-structure.md for the full captured name list.
LANGUAGES: dict[str, str] = {
    "abkhaz": "abk", "afrikaans": "afr", "albanian": "sqi", "amharic": "amh",
    "arabic": "arb", "armenian": "hye", "assamese": "asm", "asturian": "ast",
    "azerbaijani": "azj", "basque": "eus", "belarusian": "bel", "bengali": "ben",
    "bosnian": "bos", "breton": "bre", "bulgarian": "bul", "burmese": "mya",
    "cantonese": "yue", "catalan": "cat", "chinese": "zho", "croatian": "hrv",
    "czech": "ces", "danish": "dan", "dhivehi": "div", "dutch": "nld",
    "english": "eng", "esperanto": "epo", "estonian": "est", "faroese": "fao",
    "filipino": "fil", "finnish": "fin", "french": "fra", "galician": "glg",
    "georgian": "kat", "german": "deu", "greek": "ell", "gujarati": "guj",
    "hausa": "hau", "hebrew": "heb", "hindi": "hin", "hungarian": "hun",
    "icelandic": "isl", "igbo": "ibo", "indonesian": "ind", "irish": "gle",
    "italian": "ita", "japanese": "jpn", "javanese": "jav", "kannada": "kan",
    "kazakh": "kaz", "khmer": "khm", "kinyarwanda": "kin", "korean": "kor",
    "kurdish": "kmr", "kyrgyz": "kir", "lao": "lao", "latin": "lat",
    "latvian": "lav", "lithuanian": "lit", "luxembourgish": "ltz",
    "macedonian": "mkd", "malagasy": "mlg", "malay": "msa", "malayalam": "mal",
    "maltese": "mlt", "māori": "mri", "marathi": "mar", "mongolian": "mon",
    "nepali": "nep", "norwegian": "nob", "odia": "ori", "pashto": "pus",
    "persian": "fas", "polish": "pol", "portuguese": "por", "punjabi": "pan",
    "quechua": "que", "romanian": "ron", "russian": "rus", "sanskrit": "san",
    "serbian": "srp", "sindhi": "snd", "sinhala": "sin", "slovak": "slk",
    "slovenian": "slv", "somali": "som", "spanish": "spa", "sundanese": "sun",
    "swahili": "swa", "swedish": "swe", "tajik": "tgk", "tamil": "tam",
    "tatar": "tat", "telugu": "tel", "thai": "tha", "tibetan": "bod",
    "tigrinya": "tir", "tongan": "ton", "turkish": "tur", "turkmen": "tuk",
    "ukrainian": "ukr", "urdu": "urd", "uyghur": "uig", "uzbek": "uzb",
    "vietnamese": "vie", "welsh": "cym", "wolof": "wol", "xhosa": "xho",
    "yiddish": "yid", "yoruba": "yor", "zulu": "zul",
}


# ---------------------------------------------------------------- config

def load_toml(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(path: pathlib.Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    data = load_toml(path)
    if "transcribe" in data:
        cfg.update(data["transcribe"])
    else:
        cfg.update(data)
    return cfg


def temp_email_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = dict(TEMP_EMAIL_DEFAULTS)
    cfg.update(load_toml(path).get("temp_email", {}))
    return cfg


def accounts_config(path: pathlib.Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = dict(ACCOUNTS_CFG_DEFAULTS)
    cfg.update(load_toml(path).get("accounts", {}))
    return cfg


def resolve_language(value: str) -> str | None:
    """Return ISO 639-3 code (None for auto) or raise ValueError."""
    v = value.strip().lower()
    if v in ("", "auto", "detect", "自动", "检测"):
        return None
    if v in LANGUAGES.values():  # already a code
        return v
    if v in LANGUAGES:
        return LANGUAGES[v]
    if len(v) == 3 and v.isalpha():  # assume raw ISO 639-3
        return v
    raise ValueError(
        f"unknown language {value!r}; use 'auto', a name from `stt list-languages`, "
        f"or a 3-letter ISO 639-3 code"
    )


# ----------------------------------------------------------- session/auth

def load_session() -> dict[str, Any]:
    """Legacy single-account file. Used only as the migration source."""
    if not SESSION_PATH.exists():
        raise SystemExit("no session — run `stt login` first")
    with SESSION_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_session(s: dict[str, Any]) -> None:
    SESSION_PATH.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


# --- multi-account store -----------------------------------------------

def _session_to_account(sess: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalise a legacy session dict into an account record."""
    return {
        "email": sess.get("email"),
        "refreshToken": sess.get("refreshToken"),
        "localId": sess.get("localId"),
        "jwt": sess.get("jwt"),
        "jwt_exp": sess.get("jwt_exp", 0),
        "apiKey": sess.get("apiKey", FIREBASE_API_KEY),
        "source": source,            # "manual" | "auto"
        "temp_address": sess.get("temp_address"),  # temp email used at signup (recovery)
        "created_at": sess.get("created_at") or time.time(),
        "credits_known": sess.get("credits_known"),  # {limit,count,fetched_at} or None
        "invalid": sess.get("invalid", False),
        "invalid_reason": sess.get("invalid_reason"),
    }


def save_accounts(store: dict[str, Any]) -> None:
    ACCOUNTS_PATH.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(ACCOUNTS_PATH, 0o600)
    except OSError:
        pass


def migrate_session_if_needed() -> None:
    """One-time: fold legacy session.json into accounts.json. Never deletes session.json."""
    if ACCOUNTS_PATH.exists() or not SESSION_PATH.exists():
        return
    sess = load_session()
    if not sess.get("refreshToken"):
        return
    store = {"accounts": [_session_to_account(sess, source="manual")],
             "active": sess.get("email")}
    save_accounts(store)
    print(f"migrated {SESSION_PATH} -> {ACCOUNTS_PATH} (email={sess.get('email')})",
          file=sys.stderr)


def load_accounts() -> dict[str, Any]:
    migrate_session_if_needed()
    if ACCOUNTS_PATH.exists():
        with ACCOUNTS_PATH.open("r", encoding="utf-8") as fh:
            store = json.load(fh)
        store.setdefault("accounts", [])
        store.setdefault("active", None)
        return store
    return {"accounts": [], "active": None}


def upsert_account(store: dict[str, Any], account: dict[str, Any]) -> None:
    """Insert or update (by email) an account in the store; mark it active."""
    accts = store["accounts"]
    for i, a in enumerate(accts):
        if a.get("email") == account["email"]:
            # preserve bookkeeping across re-login/re-register
            account["created_at"] = a.get("created_at") or account["created_at"]
            if account.get("credits_known") is None:
                account["credits_known"] = a.get("credits_known")
            accts[i] = account
            break
    else:
        accts.append(account)
    store["active"] = account["email"]


def active_account(store: dict[str, Any]) -> dict[str, Any] | None:
    """The last-used account, else the first non-invalid account."""
    accts = store["accounts"]
    target = store.get("active")
    for a in accts:
        if a.get("email") == target and not a.get("invalid"):
            return a
    for a in accts:
        if not a.get("invalid"):
            return a
    return accts[0] if accts else None


def get_jwt(session: dict[str, Any], save=None) -> str:
    jwt = session.get("jwt")
    exp = session.get("jwt_exp", 0)
    if jwt and exp > time.time() + 60:
        return jwt
    rt = session.get("refreshToken")
    if not rt:
        raise SystemExit("account has no refresh token — re-login or register a new one")
    resp = httpx.post(
        SECURETOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": rt},
        headers={"Referer": FIREBASE_REFERER},  # API key is referer-restricted
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"auth refresh failed ({resp.status_code}): re-run `stt login`\n{resp.text[:300]}"
        )
    data = resp.json()
    session["jwt"] = data["id_token"]
    session["jwt_exp"] = time.time() + int(data.get("expires_in", 3600)) - 60
    if data.get("refresh_token"):
        session["refreshToken"] = data["refresh_token"]
    # Firebase rotates refresh tokens; persist immediately so the account isn't lost.
    (save or save_session)(session)
    return session["jwt"]


def authed_client(session: dict[str, Any], save=None) -> httpx.Client:
    jwt = get_jwt(session, save)
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=httpx.Timeout(30.0, read=None),  # upload may be slow: no read cap
    )


def random_password() -> str:
    """ElevenLabs/Firebase-compatible disposable password."""
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "El1!" + "".join(secrets.choice(alphabet) for _ in range(12))


def firebase_verify_oob_code(oob_code: str) -> dict[str, Any]:
    r = httpx.post(
        f"{IDENTITY_URL}:update?key={FIREBASE_API_KEY}",
        json={"oobCode": oob_code},
        headers={"Referer": FIREBASE_REFERER},
        timeout=30,
    )
    if r.status_code >= 400:
        raise SystemExit(f"firebase verify failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def firebase_signin_password(email: str, password: str) -> dict[str, Any]:
    r = httpx.post(
        f"{IDENTITY_URL}:signInWithPassword?key={FIREBASE_API_KEY}",
        json={"email": email, "password": password, "returnSecureToken": True},
        headers={"Referer": FIREBASE_REFERER},
        timeout=30,
    )
    if r.status_code >= 400:
        raise SystemExit(f"firebase password sign-in failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def account_from_password_signin(email: str, password: str, temp_address: str | None = None) -> dict[str, Any]:
    data = firebase_signin_password(email, password)
    sess = {
        "apiKey": FIREBASE_API_KEY,
        "refreshToken": data.get("refreshToken"),
        "localId": data.get("localId"),
        "email": data.get("email"),
        "jwt": data.get("idToken"),
        "jwt_exp": time.time() + int(data.get("expiresIn", 3600)) - 60,
        "temp_address": temp_address or email,
        "created_at": time.time(),
    }
    return _session_to_account(sess, source="auto")


# ----------------------------------------------------------------- audio

def audio_duration(path: pathlib.Path) -> float | None:
    """Best-effort duration in seconds via ffprobe; None if unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.check_output(
            [ffprobe, "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL,
        )
        return float(out.decode().strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


# ------------------------------------------------------------------- API

def get_cost(client: httpx.Client, duration: float) -> int | None:
    r = client.get("/v1/speech-to-text/cost", params={"duration": duration})
    if r.status_code == 200:
        return r.json().get("credits")
    return None


def create_task(client: httpx.Client, audio: pathlib.Path, opts: dict[str, Any]) -> dict:
    data: dict[str, Any] = {
        "task_name": audio.name,
        "model_id": "scribe_v2",
        "tag_audio_events": "true" if opts["tag_audio_events"] else "false",
        "include_subtitles": "true" if opts["include_subtitles"] else "false",
    }
    if opts["language_code"]:
        data["language_code"] = opts["language_code"]
    if opts["no_verbatim"]:
        data["no_verbatim"] = "true"
    if opts["use_speaker_library"]:
        data["use_speaker_library"] = "true"
    vocab = opts["vocab"] or []
    if vocab:
        data["keyterms"] = list(vocab)  # repeated form field per word

    with audio.open("rb") as fh:
        files = {"file": (audio.name, fh, "application/octet-stream")}
        r = client.post("/v1/speech-to-text/tasks", data=data, files=files)
    if r.status_code not in (200, 201):
        raise SystemExit(f"create task failed ({r.status_code}):\n{r.text[:500]}")
    return r.json()


def poll_task(client: httpx.Client, task_id: str, timeout: float) -> dict:
    deadline = time.time() + timeout
    last_progress = -1.0
    while time.time() < deadline:
        r = client.get(f"/v1/speech-to-text/tasks/{task_id}")
        if r.status_code != 200:
            raise SystemExit(f"poll failed ({r.status_code}): {r.text[:300]}")
        task = r.json()
        state = task.get("state", "")
        progress = task.get("progress", 0.0)
        if progress != last_progress:
            print(f"  {state} {progress*100:.0f}%", file=sys.stderr)
            last_progress = progress
        if task.get("last_error"):
            raise SystemExit(f"transcription failed: {task['last_error']}")
        if state == "processed":
            return task
        time.sleep(POLL_INTERVAL_SECS)
    raise SystemExit(f"timed out after {timeout:.0f}s waiting for {task_id}")


def export_task(client: httpx.Client, task_id: str, fmt: str, lang_code: str | None) -> bytes:
    r = client.post(
        f"/v1/speech-to-text/tasks/{task_id}/editor/export/{fmt}",
        json={"language_code": lang_code},
    )
    if r.status_code != 200:
        raise SystemExit(f"export {fmt} failed ({r.status_code}): {r.text[:300]}")
    return r.content


# --- temp-email --------------------------------------------------------

OOB_RE = re.compile(r"[?&]oobCode=([A-Za-z0-9_-]+)")


def extract_oob_code(*parts: str | None) -> str | None:
    """Extract Firebase oobCode from mail subject/text/html."""
    for part in parts:
        if not part:
            continue
        match = OOB_RE.search(html.unescape(part))
        if match:
            return match.group(1)
    return None


def temp_email_create(name: str | None = None,
                      cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a cloudflare_temp_email address; admin path first, user path fallback."""
    cfg = cfg or temp_email_config()
    if not cfg["base_url"] or not cfg["domain"]:
        raise SystemExit("temp_email.base_url and temp_email.domain are required")
    base = str(cfg["base_url"]).rstrip("/")
    # cloudflare_temp_email v1.9 requires name even on the admin API.
    local = name or ("el" + secrets.token_hex(5))
    body = {"name": local, "domain": cfg["domain"], "cf_token": "",
            "enableRandomSubdomain": False}

    with httpx.Client(timeout=30) as client:
        if cfg.get("use_admin_path", True) and cfg.get("admin_password"):
            r = client.post(f"{base}/admin/new_address", json=body,
                            headers={"x-admin-auth": cfg["admin_password"]})
            if r.status_code < 400:
                return r.json()
            if r.status_code not in (401, 403):
                raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")

        headers = {}
        if cfg.get("site_password"):
            headers["x-custom-auth"] = cfg["site_password"]
        r = client.post(f"{base}/api/new_address", json=body, headers=headers)
        if r.status_code >= 400:
            raise SystemExit(f"temp-email create failed ({r.status_code}): {r.text[:300]}")
        return r.json()


def latest_verify_link(addr_jwt: str, cfg: dict[str, Any] | None = None) -> str:
    """Return newest ElevenLabs verification link from a temp mailbox."""
    cfg = cfg or temp_email_config()
    base = str(cfg["base_url"]).rstrip("/")
    deadline = time.time() + float(cfg["poll_timeout_secs"])
    headers = {"Authorization": f"Bearer {addr_jwt}"}
    with httpx.Client(timeout=30) as client:
        while time.time() < deadline:
            r = client.get(f"{base}/api/parsed_mails", params={"limit": 20, "offset": 0},
                           headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"temp-email poll failed ({r.status_code}): {r.text[:300]}")
            for mail in r.json().get("results", []):
                text = html.unescape("\n".join(str(mail.get(k) or "") for k in ("text", "html")))
                match = re.search(r"https://elevenlabs\.io/app/action\?[^\s\"<>]+oobCode=[^\s\"<>]+", text)
                if match:
                    return match.group(0)
            time.sleep(float(cfg["poll_interval_secs"]))
    raise SystemExit("timed out waiting for ElevenLabs verification email")


def poll_parsed_mails(addr_jwt: str, cfg: dict[str, Any] | None = None) -> str:
    """Poll parsed mails until the first Firebase oobCode appears."""
    cfg = cfg or temp_email_config()
    base = str(cfg["base_url"]).rstrip("/")
    deadline = time.time() + float(cfg["poll_timeout_secs"])
    seen: set[str] = set()
    headers = {"Authorization": f"Bearer {addr_jwt}"}
    with httpx.Client(timeout=30) as client:
        while time.time() < deadline:
            r = client.get(f"{base}/api/parsed_mails", params={"limit": 20, "offset": 0},
                           headers=headers)
            if r.status_code >= 400:
                raise SystemExit(f"temp-email poll failed ({r.status_code}): {r.text[:300]}")
            for mail in r.json().get("results", []):
                mid = str(mail.get("id") or "")
                if mid in seen:
                    continue
                seen.add(mid)
                code = extract_oob_code(mail.get("subject"), mail.get("text"), mail.get("html"))
                if code:
                    return code
            time.sleep(float(cfg["poll_interval_secs"]))
    raise SystemExit("timed out waiting for ElevenLabs verification email")


# --- credits / selection ----------------------------------------------

def estimate_required(duration: float | None) -> int | None:
    """Pre-flight credit estimate from audio duration (no network)."""
    if not duration:
        return None
    return int(duration * CREDITS_PER_SEC) + 1


def has_temp_email_config(path: pathlib.Path = CONFIG_PATH) -> bool:
    t = temp_email_config(path)
    return bool(t["base_url"] and t["domain"] and (t["admin_password"] or t["site_password"]))


def refresh_credits(account: dict[str, Any], client: httpx.Client) -> int | None:
    """GET /v1/user/subscription -> store into account.credits_known; return remaining."""
    r = client.get("/v1/user/subscription")
    if r.status_code != 200:
        return None
    data = r.json()
    sub = data.get("subscription") or data  # /v1/user nests it; /v1/user/subscription is flat
    limit = sub.get("character_limit", 0)
    count = sub.get("character_count", 0)
    account["credits_known"] = {"limit": limit, "count": count, "fetched_at": time.time()}
    return max(0, limit - count)


def cached_remaining(account: dict[str, Any]) -> int | None:
    ck = account.get("credits_known")
    if not ck:
        return None
    return max(0, ck.get("limit", 0) - ck.get("count", 0))


def account_remaining(account: dict[str, Any], store: dict[str, Any], force: bool = False) -> int | None:
    """Remaining credits for an account. Uses cache within TTL, else fetches (network).
    On repeated auth failure, quarantines the account as invalid (R3)."""
    ck = account.get("credits_known")
    if not force and ck and time.time() - ck.get("fetched_at", 0) < CREDITS_CACHE_TTL:
        return cached_remaining(account)
    try:
        with authed_client(account, save=lambda _s: save_accounts(store)) as client:
            rem = refresh_credits(account, client)
            if rem is not None:  # success → clear any prior auth-failure state
                account["auth_fails"] = 0
                account["invalid"] = False
                account["invalid_reason"] = None
        save_accounts(store)  # persist credits_known + state (JWT rotation already saved above)
        return rem
    except (Exception, SystemExit) as e:
        account["auth_fails"] = account.get("auth_fails", 0) + 1
        if account["auth_fails"] >= 3:
            account["invalid"] = True
            account["invalid_reason"] = str(e)[:120]
        save_accounts(store)  # persist auth-fail / quarantine state
        return None


def register_one() -> dict[str, Any]:
    """Create one ElevenLabs account via temp-mail + real Chrome, then return account."""
    try:
        import pyautogui, pyperclip, pygetwindow as gw
    except ImportError:
        raise SystemExit("auto-register needs pyautogui pyperclip pygetwindow")

    addr = temp_email_create()
    email = addr["address"]
    password = random_password()
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    profile_dir = tempfile.mkdtemp(prefix="elevenlabs-stt-chrome-")
    prefs_path = pathlib.Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)
    prefs_path.write_text(json.dumps({
        "credentials_enable_service": False,
        "profile": {"password_manager_enabled": False},
    }), encoding="utf-8")

    # ponytail: fresh profile per account avoids logged-in Chrome redirecting sign-up to onboarding.
    # Coordinates are ugly, but selector automation triggers hCaptcha; real Chrome doesn't.
    def window_key(w: Any) -> Any:
        return getattr(w, "_hWnd", None) or (w.title, w.left, w.top, w.width, w.height)

    before_windows = {window_key(w) for w in gw.getAllWindows()
                      if "Google Chrome" in w.title}
    subprocess.Popen([
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--new-window",
        "--window-position=40,40",
        "--disable-save-password-bubble",
        "https://elevenlabs.io/app/sign-up",
    ])
    new_window = None
    deadline = time.time() + 10
    while time.time() < deadline and new_window is None:
        time.sleep(0.5)
        for w in gw.getAllWindows():
            if "Google Chrome" in w.title and window_key(w) not in before_windows:
                new_window = w
                break
    if new_window is None:
        raise SystemExit("auto-register could not find the new temporary Chrome window; aborting")
    new_window.restore(); new_window.activate(); new_window.maximize()
    time.sleep(4)

    def click_frac(x_frac: float, y_frac: float) -> None:
        pyautogui.click(new_window.left + int(new_window.width * x_frac),
                        new_window.top + int(new_window.height * y_frac))

    def paste(text: str) -> None:
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")

    pyautogui.hotkey("ctrl", "l")
    paste("https://elevenlabs.io/app/sign-up")
    pyautogui.press("enter")
    time.sleep(12)

    click_frac(0.50, 0.56)  # signup email
    pyautogui.hotkey("ctrl", "a"); paste(email)
    pyautogui.press("tab"); paste(password)
    pyautogui.press("enter")

    link = latest_verify_link(addr["jwt"])
    pyautogui.hotkey("ctrl", "l")
    paste(link)
    pyautogui.press("enter")
    time.sleep(15)
    pyautogui.press("enter")  # modal Continue if focused
    click_frac(0.50, 0.62)
    click_frac(0.65, 0.62)  # verification modal Continue fallback
    time.sleep(8)
    click_frac(0.50, 0.62)  # sign-in email
    pyautogui.hotkey("ctrl", "a"); paste(email)
    pyautogui.press("tab"); paste(password)
    pyautogui.press("enter")
    time.sleep(15)

    account = account_from_password_signin(email, password, temp_address=email)
    with authed_client(account, save=lambda _s: None) as client:
        client.get("/v1/user")
        refresh_credits(account, client)
    return account


def fresh_count(store: dict[str, Any], threshold: int) -> int:
    return sum(1 for a in store["accounts"]
               if not a.get("invalid") and (cached_remaining(a) or 0) >= threshold)


def refill_pool(store: dict[str, Any], config_path: pathlib.Path) -> None:
    acfg = accounts_config(config_path)
    if not acfg["auto_refill"] or not has_temp_email_config(config_path):
        return
    active = store.get("active")
    while fresh_count(store, acfg["fresh_threshold"]) < acfg["pool_target"]:
        print("pool below target; registering refill account...", file=sys.stderr)
        account = register_one()
        upsert_account(store, account)
        store["active"] = active
        save_accounts(store)


def select_account(accounts: list[dict[str, Any]], required: int | None,
                    margin: float, store: dict[str, Any]) -> tuple[dict[str, Any] | None, list]:
    """Best-fit + margin: the smallest remaining that covers required*margin.
    Returns (chosen_or_None, [(email, remaining), ...]) for logging. If required is
    None (duration unknown), falls back to the active account without a sufficiency check."""
    if required is None:
        return active_account(store), []
    needed = int(required * margin) + 1
    sufficient: list[tuple[dict, int]] = []
    info: list[tuple[str | None, int | None]] = []
    for a in accounts:
        if a.get("invalid"):
            continue
        rem = account_remaining(a, store)
        info.append((a.get("email"), rem))
        if rem is not None and rem >= needed:
            sufficient.append((a, rem))
    if not sufficient:
        return None, info
    chosen, _ = min(sufficient, key=lambda ar: ar[1])
    return chosen, info


# ------------------------------------------------------------- subcommands

def cmd_login(args: argparse.Namespace) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit("playwright not installed: pip install playwright && playwright install chrome")

    print("opening ElevenLabs login in Chrome — log in, then this exits automatically.")
    with sync_playwright() as pw:
        browser = None
        for channel in ("chrome", "msedge"):
            try:
                browser = pw.chromium.launch(channel=channel, headless=False)
                print(f"  using {channel}")
                break
            except Exception as e:  # channel missing
                print(f"  {channel} unavailable: {e}", file=sys.stderr)
        if browser is None:
            browser = pw.chromium.launch(headless=False)  # bundled chromium fallback

        page = browser.new_page()
        page.goto(LOGIN_URL)
        # wait until logged in: firebase user lands in localStorage
        page.wait_for_function(
            f"() => localStorage.getItem({json.dumps(FIREBASE_USER_KEY)}) !== null",
            timeout=0,  # no cap; user logs in at their pace
        )
        raw = page.evaluate(f"() => localStorage.getItem({json.dumps(FIREBASE_USER_KEY)})")
        browser.close()

    user = json.loads(raw)
    sts = user.get("stsTokenManager", {})
    session = {
        "apiKey": FIREBASE_API_KEY,
        "refreshToken": sts.get("refreshToken"),
        "localId": user.get("uid") or user.get("localId"),
        "email": user.get("email"),
        "jwt": sts.get("accessToken"),
        "jwt_exp": (sts.get("expirationTime", 0) / 1000.0) if sts.get("expirationTime") else 0,
    }
    if not session["refreshToken"]:
        raise SystemExit("login did not yield a refresh token — re-run `stt login`")
    store = load_accounts()
    account = _session_to_account(session, source="manual")
    upsert_account(store, account)
    save_accounts(store)
    print(f"saved {ACCOUNTS_PATH} (email={account['email']})")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    audio = pathlib.Path(args.audio)
    if not audio.exists():
        raise SystemExit(f"audio not found: {audio}")

    cfg = load_config(pathlib.Path(args.config))
    # flag overrides
    if args.lang is not None:
        cfg["language"] = args.lang
    if args.events is not None:
        cfg["tag_audio_events"] = args.events
    if args.subs is not None:
        cfg["include_subtitles"] = args.subs
    if args.verbatim is not None:
        cfg["no_verbatim"] = args.verbatim
    if args.voice_lib is not None:
        cfg["use_speaker_library"] = args.voice_lib
    if args.vocab is not None:
        cfg["vocab"] = [w.strip() for w in args.vocab.split(",") if w.strip()]
    if args.format is not None:
        cfg["export_format"] = args.format
    if args.poll_timeout is not None:
        cfg["poll_timeout_secs"] = args.poll_timeout
    if args.show_cost:
        cfg["show_cost"] = True

    cfg["language_code"] = resolve_language(cfg["language"])

    # --- validate file ---
    size = audio.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SystemExit(f"file is {size/1e6:.1f} MB > 1000 MB limit")
    duration = audio_duration(audio)
    if duration is None:
        print("note: ffprobe unavailable — audio duration not checked", file=sys.stderr)
    elif duration > FREE_DURATION_WARN_SECS:
        print(f"warn: audio is {duration:.0f}s ({duration/60:.1f} min) > "
              f"{FREE_DURATION_WARN_SECS}s free-account soft limit", file=sys.stderr)

    store = load_accounts()
    acfg = accounts_config(pathlib.Path(args.config))
    required = estimate_required(duration)
    chosen, info = select_account(store["accounts"], required, acfg["selection_margin"], store)
    if info:
        print("  candidates: " + ", ".join(f"{em}(rem={r})" for em, r in info), file=sys.stderr)
    if chosen is None:
        if has_temp_email_config(pathlib.Path(args.config)):
            print(f"no sufficient account (required~{required}); registering one...", file=sys.stderr)
            chosen = register_one()
            upsert_account(store, chosen)
            save_accounts(store)
        else:
            chosen = active_account(store)
            if chosen is None:
                raise SystemExit("no accounts — run `stt login` or configure temp-email + `stt pool warm`")
            print(f"warn: no account confirmed sufficient (required~{required}); "
                  f"using {chosen.get('email')} best-effort", file=sys.stderr)
    else:
        print(f"selected {chosen.get('email')} (remaining={cached_remaining(chosen)}, "
              f"required~{required})", file=sys.stderr)
    store["active"] = chosen["email"]
    save_accounts(store)  # persist active pointer / credits_known / jwt updates from the selection pass
    account = chosen
    # ponytail: account dict reuses the session shape, so get_jwt/authed_client work as-is;
    # the save callback persists Firebase's rotated refresh token back into accounts.json.
    with authed_client(account, save=lambda _s: save_accounts(store)) as client:
        if cfg["show_cost"] and duration:
            credits = get_cost(client, duration)
            if credits is not None:
                print(f"estimated cost: {credits} credits", file=sys.stderr)

        print(f"uploading {audio.name} ({size/1e6:.1f} MB)...", file=sys.stderr)
        task = create_task(client, audio, cfg)
        task_id = task["_id"]
        print(f"  task {task_id} state={task.get('state')}", file=sys.stderr)

        result = poll_task(client, task_id, cfg["poll_timeout_secs"])
        lang_code = (result.get("result") or {}).get("language_code")

        fmt = cfg["export_format"]
        print(f"exporting {fmt}...", file=sys.stderr)
        content = export_task(client, task_id, fmt, lang_code)

    out = pathlib.Path(args.output) if args.output else audio.with_suffix(f".{fmt}")
    out.write_bytes(content)
    print(f"wrote {out} ({len(content)} bytes)")
    account_remaining(account, store, force=True)  # refresh post-use credits before refill decision
    try:
        refill_pool(store, pathlib.Path(args.config))
    except KeyboardInterrupt:
        print("pool refill skipped (interrupted)", file=sys.stderr)
    return 0


def cmd_list_languages(args: argparse.Namespace) -> int:
    print(f"{'language':<24} ISO 639-3")
    print("-" * 36)
    for name in sorted(LANGUAGES, key=lambda n: n.lower()):
        print(f"{name:<24} {LANGUAGES[name]}")
    print("\nuse 'auto' for auto-detect, or pass any ISO 639-3 code directly to --lang.")
    print("full web combobox list (157 entries): research/modal-ui-structure.md")
    return 0


def _selfcheck() -> int:
    """Minimal runnable self-check (no network)."""
    cfg = load_config(pathlib.Path("config.example.toml"))
    assert cfg["include_subtitles"] is True, "script default include_subtitles must be ON"
    assert cfg["tag_audio_events"] is True
    assert resolve_language("auto") is None
    assert resolve_language("English") == "eng"
    assert resolve_language("eng") == "eng"
    assert resolve_language("zho") == "zho"
    # multipart field builder sanity (no upload)
    opts = dict(cfg); opts["language_code"] = None; opts["vocab"] = ["a", "b"]
    data = {"task_name": "x", "model_id": "scribe_v2",
            "tag_audio_events": "true", "include_subtitles": "true", "keyterms": ["a", "b"]}
    assert data["keyterms"] == ["a", "b"]
    # multi-account config sections + migration shape
    tcfg = temp_email_config(pathlib.Path("config.example.toml"))
    assert tcfg["poll_interval_secs"] == 3 and tcfg["use_admin_path"] is True
    acfg = accounts_config(pathlib.Path("config.example.toml"))
    assert acfg["selection_margin"] == 1.2 and acfg["pool_target"] == 3
    acct = _session_to_account(
        {"email": "a@b.c", "refreshToken": "rt", "localId": "uid", "jwt": "j", "jwt_exp": 0},
        source="manual")
    assert acct["source"] == "manual" and acct["email"] == "a@b.c" and acct["invalid"] is False
    # selection: best-fit + margin (offline via fresh credits_known cache; no network)
    now = time.time()
    fake = [
        {"email": "a", "invalid": False, "credits_known": {"limit": 10000, "count": 9000, "fetched_at": now}},
        {"email": "b", "invalid": False, "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}},
        {"email": "c", "invalid": True,  "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}},
    ]
    fstore = {"accounts": fake, "active": "a"}
    assert cached_remaining(fake[0]) == 1000 and cached_remaining(fake[1]) == 10000
    sample_link = "https://elevenlabs.io/app/action?mode=verifyEmail&amp;oobCode=ABC_def-123&apiKey=x"
    assert extract_oob_code(sample_link) == "ABC_def-123"
    pw = random_password()
    assert len(pw) >= 8 and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)
    chosen, info = select_account(fake, required=800, margin=1.2, store=fstore)
    # needed = 800*1.2+1 = 961; a(1000) & b(10000) sufficient; best-fit picks smallest => a
    assert chosen["email"] == "a", f"best-fit should pick a, got {chosen and chosen['email']}"
    assert len(info) == 2  # invalid account 'c' skipped
    assert fresh_count(fstore, 10000) == 1 and fresh_count(fstore, 1000) == 2
    assert select_account(fake, required=9000, margin=1.2, store=fstore)[0] is None  # needed=10801
    assert select_account(fake, required=None, margin=1.2, store=fstore)[0]["email"] == "a"  # active fallback
    print("selfcheck ok")
    return 0


# --- account / pool subcommands ---------------------------------------

def cmd_accounts(args: argparse.Namespace) -> int:
    store = load_accounts()
    accts = store["accounts"]
    if not accts:
        print("no accounts. run `stt login` or (once configured) `stt pool warm`.")
        return 0
    if args.refresh:
        for a in accts:
            if a.get("invalid"):
                continue
            rem = account_remaining(a, store, force=True)
            print(f"  refreshed {a.get('email')}: rem={rem}", file=sys.stderr)
        save_accounts(store)
    active = store.get("active")
    print(f"{'email':<34} {'src':<7} {'remaining':>9}  state")
    print("-" * 62)
    for a in accts:
        email = a.get("email") or "?"
        src = a.get("source", "?")
        rem = cached_remaining(a)
        if a.get("invalid"):
            state = "INVALID"
        elif rem is None:
            state = "unknown"
        elif rem == 0:
            state = "depleted"
        else:
            state = "ok"
        marker = "*" if email == active else " "
        print(f"{marker}{email:<33} {src:<7} {(str(rem) if rem is not None else '—'):>9}  {state}")
    return 0


def cmd_pool(args: argparse.Namespace) -> int:
    if args.pool_cmd == "status":
        return cmd_pool_status(args)
    if args.pool_cmd == "warm":
        return cmd_pool_warm(args)
    return 1


def cmd_pool_warm(args: argparse.Namespace) -> int:
    store = load_accounts()
    acfg = accounts_config(pathlib.Path(args.config))
    target = args.target or acfg["pool_target"]
    while True:
        fresh = fresh_count(store, acfg["fresh_threshold"])
        if fresh >= target:
            print(f"pool warm: {fresh}/{target}")
            return 0
        account = register_one()
        upsert_account(store, account)
        save_accounts(store)


def cmd_pool_status(args: argparse.Namespace) -> int:
    store = load_accounts()
    acfg = accounts_config(pathlib.Path(args.config))
    threshold = acfg["fresh_threshold"]
    target = acfg["pool_target"]
    fresh = usable = depleted = invalid = unknown = 0
    for a in store["accounts"]:
        if a.get("invalid"):
            invalid += 1; continue
        rem = cached_remaining(a)
        if rem is None:
            unknown += 1
        elif rem == 0:
            depleted += 1
        elif rem >= threshold:
            fresh += 1
        else:
            usable += 1
    print(f"pool target: {target}")
    print(f"  fresh (>= {threshold}):    {fresh}")
    print(f"  usable:           {usable}")
    print(f"  depleted:         {depleted}")
    print(f"  unknown (refresh first): {unknown}")
    print(f"  invalid:          {invalid}")
    if fresh < target:
        msg = f"  -> below target by {target - fresh}; run `stt pool warm`"
        if not has_temp_email_config(pathlib.Path(args.config)):
            msg += " (temp_email not configured)"
        print(msg)
    return 0


# -------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stt", description="ElevenLabs web Speech-to-Text CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="one-time browser login → session.json")

    t = sub.add_parser("transcribe", help="transcribe an audio file")
    t.add_argument("audio", help="path to audio file")
    t.add_argument("-c", "--config", default=str(CONFIG_PATH), help="config.toml path")
    t.add_argument("--lang", help="auto | language name | ISO 639-3 code")
    t.add_argument("--events", action=argparse.BooleanOptionalAction, default=None,
                   help="mark audio events (default on)")
    t.add_argument("--subs", action=argparse.BooleanOptionalAction, default=None,
                   help="include subtitles (default on)")
    t.add_argument("--verbatim", action=argparse.BooleanOptionalAction, default=None,
                   help="no verbatim (default off)")
    t.add_argument("--voice-lib", action=argparse.BooleanOptionalAction, default=None,
                   help="use speaker library (default off)")
    t.add_argument("--vocab", help="comma-separated key terms")
    t.add_argument("--format", choices=EXPORT_FORMATS, help="export format (default srt)")
    t.add_argument("-o", "--output", help="output file path")
    t.add_argument("--show-cost", action="store_true", help="print estimated credit cost")
    t.add_argument("--poll-timeout", type=int, help="poll timeout seconds")

    sub.add_parser("list-languages", help="print supported language names + codes")
    sc = sub.add_parser("selfcheck", help="run offline self-check")

    a = sub.add_parser("accounts", help="list accounts + remaining credits")
    a.add_argument("-c", "--config", default=str(CONFIG_PATH), help="config.toml path")
    a.add_argument("--refresh", action="store_true", help="force-refresh credits from the API")

    pool = sub.add_parser("pool", help="account-pool management")
    psub = pool.add_subparsers(dest="pool_cmd", required=True)
    ps = psub.add_parser("status", help="show fresh/usable/depleted/invalid counts")
    ps.add_argument("-c", "--config", default=str(CONFIG_PATH), help="config.toml path")
    pw = psub.add_parser("warm", help="register accounts until the pool target is met")
    pw.add_argument("-c", "--config", default=str(CONFIG_PATH), help="config.toml path")
    pw.add_argument("--target", type=int, help="fresh account target")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "transcribe":
        return cmd_transcribe(args)
    if args.cmd == "list-languages":
        return cmd_list_languages(args)
    if args.cmd == "selfcheck":
        return _selfcheck()
    if args.cmd == "accounts":
        return cmd_accounts(args)
    if args.cmd == "pool":
        return cmd_pool(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

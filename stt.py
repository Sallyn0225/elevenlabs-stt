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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

import audio_split

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
    "domains": [],             # all usable domains; UI dropdown candidates
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
        "password": sess.get("password"),  # plaintext, so re-login is possible from the pool
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
        "password": password,
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
            _tlog(f"{state} {progress*100:.0f}%")
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


def account_remaining(account: dict[str, Any], store: dict[str, Any], force: bool = False,
                      save=save_accounts) -> int | None:
    """Remaining credits for an account. Uses cache within TTL, else fetches (network).
    On repeated auth failure, quarantines the account as invalid (R3).
    `save` lets callers batch persistence (parallel refresh passes a no-op, saves once at the end)."""
    ck = account.get("credits_known")
    if not force and ck and time.time() - ck.get("fetched_at", 0) < CREDITS_CACHE_TTL:
        return cached_remaining(account)
    try:
        with authed_client(account, save=lambda _s: save(store)) as client:
            rem = refresh_credits(account, client)
            if rem is not None:  # success → clear any prior auth-failure state
                account["auth_fails"] = 0
                account["invalid"] = False
                account["invalid_reason"] = None
        save(store)  # persist credits_known + state (JWT rotation already saved above)
        return rem
    except (Exception, SystemExit) as e:
        account["auth_fails"] = account.get("auth_fails", 0) + 1
        if account["auth_fails"] >= 3:
            account["invalid"] = True
            account["invalid_reason"] = str(e)[:120]
        save(store)  # persist auth-fail / quarantine state
        return None


# ---- register progress log hook --------------------------------------------
# web.py 注册期间替换为收集函数以驱动 WebUI 进度轮询；None → 默认打 stderr。
REGISTER_LOG = None


def _rlog(msg: str) -> None:
    """Register-flow progress line: routed to the REGISTER_LOG hook or stderr."""
    (REGISTER_LOG or (lambda m: print(f"[register] {m}", file=sys.stderr)))(msg)


# web.py 转录期间替换为收集函数以驱动 WebUI 进度轮询；None → 默认打 stderr。
TRANSCRIBE_LOG = None


def _tlog(msg: str) -> None:
    """Transcribe-flow progress line: routed to the TRANSCRIBE_LOG hook or stderr."""
    (TRANSCRIBE_LOG or (lambda m: print(f"[stt] {m}", file=sys.stderr)))(msg)


def register_one() -> dict[str, Any]:
    """Create one ElevenLabs account via temp-mail + real Chrome, then return account."""
    try:
        import pyautogui, pyperclip, pygetwindow as gw
    except ImportError:
        raise SystemExit("auto-register needs pyautogui pyperclip pygetwindow")

    _rlog("创建临时邮箱...")
    addr = temp_email_create()
    email = addr["address"]
    _rlog(f"临时邮箱已创建: {email}")
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
    def profile_window_handles() -> set[int]:
        if not shutil.which("powershell"):
            return set()
        profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
        ps = (
            f"$profile = '{profile_name}'; "
            "$pids = @(Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
            "Select-Object -ExpandProperty ProcessId); "
            "if ($pids.Count -gt 0) { "
            "Get-Process -Id $pids -ErrorAction SilentlyContinue | "
            "Where-Object { $_.MainWindowHandle -ne 0 } | "
            "ForEach-Object { $_.MainWindowHandle } "
            "}"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=3)
        except Exception:
            return set()
        handles: set[int] = set()
        for line in out.stdout.splitlines():
            try:
                handles.add(int(line.strip()))
            except ValueError:
                pass
        return handles

    chrome_startup_kwargs = {}
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL: do not inherit a hidden Web UI process state.
        chrome_startup_kwargs = {
            "startupinfo": startupinfo,
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    proc = None
    try:
        _rlog("启动临时 Chrome...")
        proc = subprocess.Popen([
            chrome,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--new-window",
            "--window-position=40,40",
            "--disable-save-password-bubble",
            "--do-not-de-elevate",
            "https://elevenlabs.io/app/sign-up",
        ], **chrome_startup_kwargs)
        new_window = None
        _rlog("等待临时 Chrome 窗口出现（最长 30s）...")
        # 30s: a brand-new profile cold-starts slowly (profile init + AV scan); 10s
        # missed the window on busy machines and the finally-block killed late Chrome.
        deadline = time.time() + 30
        next_profile_probe = 0.0
        while time.time() < deadline and new_window is None:
            time.sleep(0.5)
            profile_handles = set()
            if time.time() >= next_profile_probe:
                profile_handles = profile_window_handles()
                next_profile_probe = time.time() + 1.0
            for w in gw.getAllWindows():
                hwnd = getattr(w, "_hWnd", None)
                if hwnd in profile_handles:
                    new_window = w
                    break
        if new_window is None:
            raise SystemExit("auto-register could not find the new temporary Chrome window; aborting")

        def window_op(name: str) -> None:
            try:
                getattr(new_window, name)()
            except Exception as e:
                # pygetwindow/pywin32 can report Windows error code 0 ("success")
                # after the window operation actually completed. Treat only that
                # wrapper bug as non-fatal; real focus/window errors should abort.
                if "Error code from Windows: 0" not in str(e):
                    raise

        def ensure_window_foreground() -> None:
            hwnd = getattr(new_window, "_hWnd", None)
            if not hwnd or os.name != "nt":
                window_op("activate")
                return
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            # Fast path: already foreground. Forcing anyway is what caused the
            # constant restore/maximize flicker between every automation action.
            if user32.GetForegroundWindow() == hwnd:
                return
            SW_RESTORE = 9
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
            for attempt in range(8):
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                # AttachThreadInput to the current foreground thread satisfies
                # Windows' foreground-lock rules. A synthetic Alt tap also works
                # but toggles Chrome's menu-accelerator mode, breaking in-window
                # keyboard focus for the very keys we send next.
                fg = user32.GetForegroundWindow()
                fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
                cur_tid = kernel32.GetCurrentThreadId()
                attached = fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                    if attempt >= 4:  # last resort: topmost toggle
                        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
                        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
                finally:
                    if attached:
                        user32.AttachThreadInput(cur_tid, fg_tid, False)
                time.sleep(0.2)
                if user32.GetForegroundWindow() == hwnd:
                    return
            raise SystemExit("auto-register could not focus the temporary Chrome window; aborting before sending keys")

        # Foreground the temp Chrome immediately, before any page-load waits.
        # pygetwindow's activate() is silently denied when we are a background
        # process, which left the window behind for ~16s until the first click
        # forced it forward and the key/click sequence landed out of sync.
        _rlog("窗口已找到，置顶并等待页面渲染...")
        window_op("restore")
        window_op("maximize")
        ensure_window_foreground()
        time.sleep(4)

        def hotkey(*keys: str) -> None:
            ensure_window_foreground()
            pyautogui.hotkey(*keys)

        def press(key: str) -> None:
            ensure_window_foreground()
            pyautogui.press(key)

        def click_frac(x_frac: float, y_frac: float) -> None:
            ensure_window_foreground()
            x = new_window.left + int(new_window.width * x_frac)
            y = new_window.top + int(new_window.height * y_frac)
            if x < 0 or y < 0:
                # A minimized window reports -32000 geometry; pyautogui clamps the
                # click to (0,0), which hits Chrome's tab-search chevron.
                raise SystemExit("auto-register got bad temp Chrome window geometry; aborting")
            pyautogui.click(x, y)
            time.sleep(0.1)

        def paste(text: str) -> None:
            ensure_window_foreground()
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")

        # Chrome already opened /app/sign-up from its command line; just wait
        # for the app to render instead of re-navigating (visible reload).
        time.sleep(12)

        _rlog("填写注册表单...")
        click_frac(0.50, 0.56)  # signup email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")

        _rlog(f"等待验证邮件（最长 {temp_email_config()['poll_timeout_secs']}s）...")
        link = latest_verify_link(addr["jwt"])
        _rlog("打开验证链接并确认...")
        hotkey("ctrl", "l")
        paste(link)
        press("enter")
        time.sleep(15)
        press("enter")  # modal Continue if focused
        click_frac(0.50, 0.62)
        click_frac(0.65, 0.62)  # verification modal Continue fallback
        time.sleep(8)
        _rlog("用新账号登录...")
        click_frac(0.50, 0.62)  # sign-in email
        hotkey("ctrl", "a"); paste(email)
        press("tab"); paste(password)
        press("enter")
        time.sleep(15)

        _rlog("拉取账号积分...")
        account = account_from_password_signin(email, password, temp_address=email)
        with authed_client(account, save=lambda _s: None) as client:
            client.get("/v1/user")
            refresh_credits(account, client)
        _rlog(f"注册完成: {email}，剩余积分 {cached_remaining(account)}")
        return account
    finally:
        if shutil.which("powershell"):
            profile_name = pathlib.Path(profile_dir).name.replace("'", "''")
            subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f"$profile = '{profile_name}'; "
                "Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif proc is not None:
            proc.terminate()
        for _ in range(5):
            shutil.rmtree(profile_dir, ignore_errors=True)
            if not pathlib.Path(profile_dir).exists():
                break
            time.sleep(0.5)


def fresh_count(store: dict[str, Any], threshold: int) -> int:
    return sum(1 for a in store["accounts"]
               if not a.get("invalid") and (cached_remaining(a) or 0) >= threshold)


def refill_pool(store: dict[str, Any], config_path: pathlib.Path) -> None:
    acfg = accounts_config(config_path)
    if not acfg["auto_refill"] or not has_temp_email_config(config_path):
        return
    active = store.get("active")
    while fresh_count(store, acfg["fresh_threshold"]) < acfg["pool_target"]:
        _rlog("账号池低于目标，注册补充账号...")
        account = register_one()
        upsert_account(store, account)
        store["active"] = active
        save_accounts(store)


def filter_accounts(store: dict[str, Any], emails: list[str]) -> tuple[list[dict[str, Any]], bool]:
    """Candidate accounts for allocate(). emails 非空时限定为所列邮箱（手动模式）。
    Returns (accounts, manual). Unknown / invalid emails raise SystemExit up front."""
    accounts = [a for a in store["accounts"] if not a.get("invalid")]
    if not emails:
        return accounts, False
    by_email = {a.get("email"): a for a in accounts}
    bad = [e for e in emails if e not in by_email]
    if bad:
        raise SystemExit(f"unknown or invalid account(s): {', '.join(bad)}")
    return [by_email[e] for e in emails], True


class _Bin:
    """A packing bin: an existing account (account set) or a virtual fresh bin (account None)."""
    __slots__ = ("account", "residual", "slot")

    def __init__(self, account, residual, slot=None):
        self.account = account
        self.residual = residual
        self.slot = slot  # "NEW#k" label for virtual bins


def allocate(costs, accounts, margin, fresh_threshold, store):
    """Bin-pack files across existing accounts (best-fit) then virtual fresh bins.

    costs: [(audio_path, required_or_None)]; accounts: non-invalid account dicts.
    Returns (plan, register_count) where plan is an ordered list of
    (audio_path, account_dict_or_"NEW#k"), existing-account files before NEW ones.
    """
    def need(req):
        return None if req is None else int(req * margin) + 1

    # FFD: largest need first. Unknown-duration files (key -1) sort last but their
    # order is irrelevant — the loop gives each its own dedicated fresh bin regardless.
    files = sorted(costs, key=lambda fc: (-1 if fc[1] is None else need(fc[1])),
                   reverse=True)

    bins = [_Bin(a, account_remaining(a, store) or 0) for a in accounts]
    fresh_cap = int(fresh_threshold * margin)
    virtual: list[_Bin] = []
    plan: list[tuple[Any, Any]] = []

    def open_virtual(residual):
        b = _Bin(None, residual, slot=f"NEW#{len(virtual)}")
        virtual.append(b)
        return b

    for file, req in files:
        if req is None:  # unknown duration -> dedicated fresh bin
            b = open_virtual(0)
            plan.append((file, b.slot))
            continue
        n = need(req)
        cand = [b for b in bins if b.residual >= n]
        if cand:
            b = min(cand, key=lambda b: b.residual)  # best-fit: smallest residual that covers
        else:
            vc = [b for b in virtual if b.residual >= n]
            if vc:
                b = min(vc, key=lambda b: b.residual)
            else:
                if n > fresh_cap:
                    raise SystemExit(f"{file}: needs {n} > one free account ({fresh_cap}); out of scope")
                b = open_virtual(fresh_cap)
        b.residual -= n
        plan.append((file, b.account if b.account is not None else b.slot))

    register_count = len(virtual)
    # existing-account files first, NEW#k files last; stable within each group.
    plan.sort(key=lambda pa: isinstance(pa[1], str))
    return plan, register_count


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


def transcribe_one(audio, cfg, account, store, config_path, output=None) -> pathlib.Path:
    """Upload → poll → export → write one file on the given account. Returns the output path."""
    size = audio.stat().st_size
    duration = audio_duration(audio)
    # ponytail: account dict reuses the session shape, so get_jwt/authed_client work as-is;
    # the save callback persists Firebase's rotated refresh token back into accounts.json.
    with authed_client(account, save=lambda _s: save_accounts(store)) as client:
        if cfg["show_cost"] and duration:
            credits = get_cost(client, duration)
            if credits is not None:
                print(f"estimated cost: {credits} credits", file=sys.stderr)

        _tlog(f"上传 {audio.name} ({size/1e6:.1f} MB)…")
        task = create_task(client, audio, cfg)
        task_id = task["_id"]
        _tlog(f"任务 {task_id} 已创建 (state={task.get('state')})")

        result = poll_task(client, task_id, cfg["poll_timeout_secs"])
        lang_code = (result.get("result") or {}).get("language_code")

        fmt = cfg["export_format"]
        _tlog(f"导出 {fmt}…")
        content = export_task(client, task_id, fmt, lang_code)

    out = pathlib.Path(output) if output else audio.with_suffix(f".{fmt}")
    out.write_bytes(content)
    print(f"wrote {out} ({len(content)} bytes)")  # stdout 结果行契约不变
    _tlog(f"完成 {out.name}")
    return out


def print_plan(plan, register_count, margin, fresh_threshold) -> None:
    """Print the allocation plan to stderr (design format)."""
    print(f"plan (margin {margin}, fresh {fresh_threshold}):", file=sys.stderr)
    for audio, target in plan:
        if isinstance(target, str):
            print(f"  {audio.name:<20} -> {target}", file=sys.stderr)
        else:
            rem = cached_remaining(target)
            print(f"  {audio.name:<20} -> {target.get('email')} (rem {rem})", file=sys.stderr)
    print(f"need new: {register_count}", file=sys.stderr)


def print_summary(results) -> None:
    """Print the per-file success/failure summary to stdout (design format)."""
    ok = sum(1 for r in results if r[2] == "OK")
    failed = len(results) - ok
    print(f"summary: {ok} ok, {failed} failed")
    for audio, email, status, detail in results:
        print(f"  {status:<4}  {audio.name:<20} -> {email}   {detail}")


def cmd_transcribe(args: argparse.Namespace) -> int:
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

    # --- validate files: drop missing/oversize with a warning (skip & continue) ---
    files: list[pathlib.Path] = []
    for p in args.audios:
        audio = pathlib.Path(p)
        if not audio.exists():
            print(f"warn: audio not found, skipping: {audio}", file=sys.stderr)
            continue
        size = audio.stat().st_size
        if size > MAX_FILE_BYTES:
            print(f"warn: {audio} is {size/1e6:.1f} MB > 1000 MB limit, skipping", file=sys.stderr)
            continue
        files.append(audio)
    if not files:
        raise SystemExit("no valid audio files to transcribe")
    if len(files) > 1 and args.output:
        raise SystemExit("-o/--output only valid with a single file; "
                         "multiple files default to each <name>.<fmt>")

    config_path = pathlib.Path(args.config)
    store = load_accounts()
    acfg = accounts_config(config_path)

    if getattr(args, "split", False):
        return _transcribe_split(args, cfg, files, config_path, store, acfg)

    # per-file cost; warn on long clips (soft free-account limit)
    costs: list[tuple[pathlib.Path, int | None]] = []
    for audio in files:
        duration = audio_duration(audio)
        if duration is None:
            print(f"note: ffprobe unavailable — {audio.name} duration unknown "
                  f"(needs its own fresh account)", file=sys.stderr)
        elif duration > FREE_DURATION_WARN_SECS:
            print(f"warn: {audio.name} is {duration:.0f}s ({duration/60:.1f} min) > "
                  f"{FREE_DURATION_WARN_SECS}s free-account soft limit", file=sys.stderr)
        costs.append((audio, estimate_required(duration)))

    # refresh live remaining for candidate accounts so the plan is accurate
    accounts, manual = filter_accounts(store, args.account)
    _tlog(f"刷新 {len(accounts)} 个账号额度…")
    for a in accounts:
        account_remaining(a, store)
    save_accounts(store)

    plan, register_count = allocate(costs, accounts, acfg["selection_margin"],
                                    acfg["fresh_threshold"], store)
    print_plan(plan, register_count, acfg["selection_margin"], acfg["fresh_threshold"])

    if args.dry_run:
        return 0

    # manual mode never registers: fail before any side effect (R1/R3)
    if manual and register_count:
        raise SystemExit(f"所选账号额度不足，还差 {register_count} 个新账号才能装下全部文件；"
                         f"请增选账号或去掉 --account 回到自动分配")

    if register_count and not has_temp_email_config(config_path):
        raise SystemExit(f"need {register_count} more account(s) but [temp_email] not configured")

    # register the exact shortfall up-front, then bind NEW#k slots -> registered accounts
    new: list[dict[str, Any]] = []
    for i in range(register_count):
        _rlog(f"注册缺口账号 {i + 1}/{register_count}")
        acct = register_one()
        upsert_account(store, acct)
        new.append(acct)
    if register_count:
        save_accounts(store)
    resolved = [(audio, new[int(t.split("#")[1])] if isinstance(t, str) else t)
                for audio, t in plan]

    results = []
    used: list[dict[str, Any]] = []
    single = len(files) == 1
    for i, (audio, account) in enumerate(resolved, 1):
        email = account.get("email")
        _tlog(f"[{i}/{len(resolved)}] {audio.name} → {email}")
        store["active"] = email
        save_accounts(store)
        if account not in used:
            used.append(account)
        try:
            out = transcribe_one(audio, cfg, account, store, config_path,
                                 args.output if single else None)
            results.append((audio, email, "OK", str(out)))
        except Exception as e:  # skip & continue
            _tlog(f"warn: {audio.name} failed: {e!r}")
            results.append((audio, email, "FAIL", repr(e)))

    for a in used:
        # ponytail: ElevenLabs debits credits asynchronously — this refresh can race
        # the debit and record a still-high remaining; margin + next-run refresh absorb the lag.
        account_remaining(a, store, force=True)  # refresh post-use credits before refill decision
    try:
        refill_pool(store, config_path)
    except KeyboardInterrupt:
        print("pool refill skipped (interrupted)", file=sys.stderr)
    print_summary(results)
    return 0 if all(r[2] == "OK" for r in results) else 1


_MERGERS = {"srt": audio_split.merge_srt, "vtt": audio_split.merge_vtt,
            "txt": audio_split.merge_txt}


def _print_split_plan(parents: list[dict[str, Any]]) -> None:
    """Print the per-input silence-split plan to stderr (AC1)."""
    print("split plan:", file=sys.stderr)
    for p in parents:
        audio = p["audio"]
        if p.get("error"):
            print(f"  {audio.name}: ERROR {p['error']}", file=sys.stderr)
            continue
        if not p["split"]:
            total = p["segs"][0][1]
            print(f"  {audio.name}: 1 segment (<= chunk_secs, no split), {total:.0f}s",
                  file=sys.stderr)
            continue
        print(f"  {audio.name}: {len(p['segs'])} segments", file=sys.stderr)
        for i, (s, e) in enumerate(p["segs"]):
            tag = " HARD-CUT" if p["hard"][i] else ""
            print(f"      part{i:02d}  {s:8.1f}s -> {e:8.1f}s  ({e - s:5.1f}s){tag}",
                  file=sys.stderr)


def _transcribe_split(args, cfg, files, config_path, store, acfg) -> int:
    """--split flow: silence-split each long input, transcribe chunks via allocate,
    then merge per input into one <stem>.<fmt>. See design.md data-flow."""
    fmt = cfg["export_format"]
    if fmt not in _MERGERS:  # R7 / AC4: html/pdf/docx/json cannot be merged
        raise SystemExit(f"--split only supports srt/vtt/txt output, not {fmt!r}")

    chunk_secs = args.chunk_secs or audio_split.default_chunk_secs(
        acfg["fresh_threshold"], CREDITS_PER_SEC, acfg["selection_margin"])
    noise_db = args.silence_db if args.silence_db is not None else audio_split.SILENCE_DB_DEFAULT
    min_silence = (args.silence_min if args.silence_min is not None
                   else audio_split.SILENCE_MIN_DEFAULT)
    single = len(files) == 1

    # --- plan: per input build its segment list + flat chunk entries -----------
    parents: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []  # one per chunk (input+output paths, offset)
    for audio in files:
        final = (pathlib.Path(args.output) if single and args.output
                 else audio.with_suffix(f".{fmt}"))
        total = audio_duration(audio)
        if total is None:
            print(f"note: ffprobe unavailable — {audio.name} cannot be split; "
                  f"treating as a single segment", file=sys.stderr)
        try:
            if total is not None and total > chunk_secs:
                mids = [(s + e) / 2 for (s, e) in
                        audio_split.detect_silences(audio, noise_db, min_silence)]
                segs, hard = audio_split.plan_cuts(total, chunk_secs, mids)
                workdir = pathlib.Path("out") / f"{audio.stem}-chunks"
                p = {"audio": audio, "final": final, "split": True, "workdir": workdir,
                     "segs": segs, "hard": hard, "chunks": []}
            else:
                p = {"audio": audio, "final": final, "split": False, "workdir": None,
                     "segs": [(0.0, total or 0.0)], "hard": [False], "chunks": []}
        except Exception as e:  # ffmpeg/split failure: skip this input, continue (R-rollback)
            print(f"warn: cannot split {audio.name}: {e!r}", file=sys.stderr)
            parents.append({"audio": audio, "final": final, "error": repr(e), "chunks": []})
            continue

        for ci, (s, e) in enumerate(p["segs"]):
            if p["split"]:
                cin = p["workdir"] / f"{audio.stem}.part{ci:02d}{audio.suffix}"
                cout = cin.with_suffix(f".{fmt}")
            else:
                cin, cout = audio, final  # no split: chunk output IS the final file
            entry = {"parent": p, "index": ci, "input": cin, "output": cout,
                     "start": s, "dur": (e - s) if total is not None else None,
                     "ok": None}
            p.setdefault("entries", []).append(entry)
            entries.append(entry)
        parents.append(p)

    _print_split_plan(parents)

    live = [p for p in parents if not p.get("error")]
    if not entries:
        raise SystemExit("no inputs could be split/transcribed")

    # --- allocate all chunks across accounts (reuses the multi-file packer) -----
    costs = [(e["input"], estimate_required(e["dur"])) for e in entries]
    accounts, manual = filter_accounts(store, args.account)
    _tlog(f"刷新 {len(accounts)} 个账号额度…")
    for a in accounts:
        account_remaining(a, store)
    save_accounts(store)
    plan, register_count = allocate(costs, accounts, acfg["selection_margin"],
                                    acfg["fresh_threshold"], store)
    print_plan(plan, register_count, acfg["selection_margin"], acfg["fresh_threshold"])

    if args.dry_run:  # AC1: plan only, no cut, no upload
        return 0

    # manual mode never registers: fail before any cutting/upload (R1/R3)
    if manual and register_count:
        raise SystemExit(f"所选账号额度不足，还差 {register_count} 个新账号才能装下全部分段；"
                         f"请增选账号或去掉 --account 回到自动分配")

    # fail fast before any cutting if the shortfall cannot be registered
    if register_count and not has_temp_email_config(config_path):
        raise SystemExit(f"need {register_count} more account(s) but [temp_email] not configured")

    # --- materialise chunk files for split inputs ------------------------------
    for p in list(live):
        if p["split"]:
            try:
                p["chunks"] = audio_split.cut_segments(p["audio"], p["segs"],
                                                       p["workdir"], p["hard"])
            except Exception as e:  # cut failed: drop this input's chunks, keep others
                print(f"warn: cutting {p['audio'].name} failed: {e!r}", file=sys.stderr)
                p["error"] = repr(e)
                for entry in p.get("entries", []):
                    entry["ok"] = False
                    entry["skipped"] = True

    # register the exact shortfall, bind NEW#k slots -> registered accounts
    new: list[dict[str, Any]] = []
    for i in range(register_count):
        _rlog(f"注册缺口账号 {i + 1}/{register_count}")
        acct = register_one()
        upsert_account(store, acct)
        new.append(acct)
    if register_count:
        save_accounts(store)
    resolved = [(inp, new[int(t.split("#")[1])] if isinstance(t, str) else t)
                for inp, t in plan]
    entry_by_input = {e["input"]: e for e in entries}

    # --- transcribe each chunk -------------------------------------------------
    results = []
    used: list[dict[str, Any]] = []
    for i, (inp, account) in enumerate(resolved, 1):
        entry = entry_by_input[inp]
        if entry["ok"] is False:  # cut failed upstream; skip upload
            continue
        email = account.get("email")
        _tlog(f"[{i}/{len(resolved)}] {inp.name} → {email}")
        store["active"] = email
        save_accounts(store)
        if account not in used:
            used.append(account)
        try:
            out = transcribe_one(inp, cfg, account, store, config_path,
                                 output=str(entry["output"]))
            entry["ok"] = True
            results.append((inp, email, "OK", str(out)))
        except Exception as e:  # skip & continue; parent won't merge (R8)
            entry["ok"] = False
            _tlog(f"warn: {inp.name} failed: {e!r}")
            results.append((inp, email, "FAIL", repr(e)))

    for a in used:
        account_remaining(a, store, force=True)
    try:
        refill_pool(store, config_path)
    except KeyboardInterrupt:
        print("pool refill skipped (interrupted)", file=sys.stderr)

    # --- merge per input, or report failure (R6 / R8) --------------------------
    merger = _MERGERS[fmt]
    parent_ok = True
    print_summary(results)
    print("merge summary:")
    for p in parents:
        audio = p["audio"]
        if p.get("error"):
            parent_ok = False
            print(f"  FAIL   {audio.name}   split error: {p['error']}")
            continue
        pentries = p.get("entries", [])
        failed = [e for e in pentries if not e["ok"]]
        if failed:  # R8: don't write merge; keep chunks + successful outputs
            parent_ok = False
            segs = ", ".join(f"part{e['index']:02d}" for e in failed)
            print(f"  FAIL   {audio.name}   segment(s) {segs} failed; merge skipped, "
                  f"chunks kept for retry")
            continue
        _tlog(f"合并 {audio.name} ({len(pentries)} 段)…")
        merged = merger([(e["start"], pathlib.Path(e["output"]).read_text(encoding="utf-8"))
                         for e in sorted(pentries, key=lambda e: e["start"])])
        pathlib.Path(p["final"]).write_text(merged, encoding="utf-8")
        print(f"  OK     {audio.name} -> {p['final']} ({len(pentries)} segment(s))")
        # R9 / AC6: clean temp chunks only on success unless --keep-chunks
        if p["split"] and p["workdir"] and not args.keep_chunks:
            shutil.rmtree(p["workdir"], ignore_errors=True)

    return 0 if (parent_ok and all(r[2] == "OK" for r in results)) else 1


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
    pw = random_password()
    assert len(pw) >= 8 and any(c.isdigit() for c in pw) and any(not c.isalnum() for c in pw)
    assert fresh_count(fstore, 10000) == 1 and fresh_count(fstore, 1000) == 2
    # allocate: bin-packing best-fit, existing-first, exact shortfall (offline via credits_known)
    aa = {"email": "a", "invalid": False, "credits_known": {"limit": 1000, "count": 0, "fetched_at": now}}
    # b sized so 7000 can't fit it (forces a NEW bin) while 5000+400 do (design mapping).
    bb = {"email": "b", "invalid": False, "credits_known": {"limit": 6000, "count": 0, "fetched_at": now}}
    astore = {"accounts": [aa, bb], "active": "a"}
    # margin 1.0 so need == required; files needing [800, 5000, 7000, 400]
    plan, reg = allocate([("f800", 800), ("f5000", 5000), ("f7000", 7000), ("f400", 400)],
                         [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
    pd = {f: acct for f, acct in plan}
    # 7000 fits no existing account (a=1000, b=6000) -> its own fresh bin, exactly one shortfall.
    assert pd["f7000"] == "NEW#0", "7000 -> fresh bin"
    assert reg == 1, f"register_count should be 1, got {reg}"
    # the three that fit are packed onto existing accounts (best-fit, existing-first).
    assert all(pd[f] in (aa, bb) for f in ("f800", "f5000", "f400")), "small files packed onto existing"
    # no account over-committed beyond its remaining (with margin 1.0, need == required).
    used = {"a": 0, "b": 0}
    for f, req in [("f800", 800), ("f5000", 5000), ("f400", 400)]:
        used[pd[f]["email"]] += req
    assert used["a"] <= 1000 and used["b"] <= 6000, f"over-commit: {used}"
    # existing-account files ordered before the NEW file (AC3)
    new_idx = next(i for i, (f, acct) in enumerate(plan) if isinstance(acct, str))
    assert all(not isinstance(acct, str) for f, acct in plan[:new_idx]), "existing before NEW"
    # unknown-duration file -> own fresh bin, increments register_count
    plan2, reg2 = allocate([("fu", None)], [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
    assert reg2 == 1 and plan2[0][1] == "NEW#0", "unknown duration -> own NEW bin"
    # a single file bigger than one fresh account -> out-of-scope guard raises
    try:
        allocate([("big", 20000)], [aa, bb], margin=1.0, fresh_threshold=10000, store=astore)
        assert False, "oversize file should raise"
    except SystemExit:
        pass

    # --- audio_split pure logic (offline; AC7) --------------------------------
    # default_chunk_secs: chunk fits one fresh account within margin
    assert audio_split.default_chunk_secs(10000, 13.9, 1.2) == 569
    # timestamp parse/fmt round-trip (srt comma-ms, vtt dot-ms)
    assert audio_split.fmt_ts_srt(3661.5) == "01:01:01,500"
    assert audio_split.fmt_ts_vtt(3661.5) == "01:01:01.500"
    assert abs(audio_split.parse_ts_srt("01:01:01,500") - 3661.5) < 1e-6
    assert abs(audio_split.parse_ts_vtt("01:01:01.500") - 3661.5) < 1e-6
    # plan_cuts: latest silence midpoint in window; MIN_SEG(5s) filters near-start cand
    segs, hard = audio_split.plan_cuts(500.0, 300.0, [3.0, 100.0, 250.0, 280.0])
    assert segs == [(0.0, 280.0), (280.0, 500.0)], segs  # 280 = latest cand <= 300; 3 filtered
    assert hard == [False, False]
    assert all(e - s <= 300.0 + 1e-9 for s, e in segs)
    # plan_cuts hard-cut fallback when no silence candidate
    segs2, hard2 = audio_split.plan_cuts(700.0, 300.0, [])
    assert segs2 == [(0.0, 300.0), (300.0, 600.0), (600.0, 700.0)], segs2
    assert hard2 == [True, True, False]
    # merge_srt: offset correction + start-sort + contiguous renumber from 1
    c0 = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    c1 = "1\n00:00:00,500 --> 00:00:01,500\nworld\n"
    m = audio_split.merge_srt([(0.0, c0), (100.0, c1)])
    assert m.startswith("1\n"), m
    assert "\n2\n" in m and m.count("-->") == 2
    assert "00:01:40,500 --> 00:01:41,500" in m  # world offset by 100s
    assert "hello" in m and "world" in m
    # merge_vtt: single WEBVTT header, offset + renumber
    v0 = "WEBVTT\n\ncue-1\n00:00:00.000 --> 00:00:01.000\nhi\n"
    v1 = "WEBVTT\n\ncue-1\n00:00:02.000 --> 00:00:03.000\nbye\n"
    mv = audio_split.merge_vtt([(0.0, v0), (10.0, v1)])
    assert mv.count("WEBVTT") == 1, mv
    assert "00:00:12.000 --> 00:00:13.000" in mv  # bye offset by 10s
    assert "\n1\n" in mv and "\n2\n" in mv
    # merge_txt: segment-order concatenation
    assert audio_split.merge_txt([(0.0, "alpha"), (5.0, "beta")]) == "alpha\n\nbeta\n"

    # REGISTER_LOG hook: collector receives _rlog lines; None default restored
    global REGISTER_LOG
    got: list[str] = []
    REGISTER_LOG = got.append
    try:
        _rlog("hook-test")
    finally:
        REGISTER_LOG = None
    assert got == ["hook-test"], got

    # TRANSCRIBE_LOG hook: same contract as REGISTER_LOG
    global TRANSCRIBE_LOG
    tgot: list[str] = []
    TRANSCRIBE_LOG = tgot.append
    try:
        _tlog("t-hook-test")
    finally:
        TRANSCRIBE_LOG = None
    assert tgot == ["t-hook-test"], tgot

    # filter_accounts: empty emails → all non-invalid; manual limits; unknown raises
    cand, manual = filter_accounts(fstore, [])
    assert manual is False and [a["email"] for a in cand] == ["a", "b"]
    cand, manual = filter_accounts(fstore, ["b"])
    assert manual is True and [a["email"] for a in cand] == ["b"]
    try:
        filter_accounts(fstore, ["b", "nobody@x.y"])
        assert False, "unknown email should raise"
    except SystemExit as e:
        assert "nobody@x.y" in str(e)
    try:
        filter_accounts(fstore, ["c"])  # invalid account is not a candidate
        assert False, "invalid email should raise"
    except SystemExit:
        pass

    print("selfcheck ok")
    return 0


# --- account / pool subcommands ---------------------------------------

def cmd_accounts(args: argparse.Namespace) -> int:
    store = load_accounts()
    accts = store["accounts"]
    wanted = set(args.email or [])
    if not accts:
        print("no accounts. run `stt login` or (once configured) `stt pool warm`.")
        return 0
    if args.refresh:
        targets = [a for a in accts
                   if not a.get("invalid") and (not wanted or a.get("email") in wanted)]
        # parallel fetch, no per-thread saves; single save_accounts below (same pattern as web.do_refresh)
        def _one(a):
            rem = account_remaining(a, store, force=True, save=lambda _s: None)
            print(f"  refreshed {a.get('email')}: rem={rem}", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_one, targets))
        if wanted:
            known = {a.get("email") for a in accts}
            for e in sorted(wanted - known):
                print(f"  no matching account: {e}", file=sys.stderr)
        save_accounts(store)
    active = store.get("active")
    print(f"{'email':<34} {'src':<7} {'remaining':>9}  state")
    print("-" * 62)
    for a in accts:
        if wanted and a.get("email") not in wanted:
            continue
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
    k = 0
    while True:
        fresh = fresh_count(store, acfg["fresh_threshold"])
        if fresh >= target:
            print(f"pool warm: {fresh}/{target}")
            return 0
        k += 1
        _rlog(f"账号池 {fresh}/{target}，开始注册第 {k} 个账号")
        try:
            account = register_one()
        except (Exception, SystemExit) as e:
            _rlog(f"注册失败: {e}")
            raise
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

    t = sub.add_parser("transcribe", help="transcribe one or more audio files")
    t.add_argument("audios", nargs="+", help="path(s) to audio file(s); multiple run as one batch")
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
    t.add_argument("--account", action="append", default=[], metavar="EMAIL",
                   help="限定只用这些账号分配（可重复）；额度不足时报错而不注册")
    t.add_argument("--dry-run", action="store_true",
                   help="print the allocation plan and exit (no registration, no upload)")
    t.add_argument("--split", action="store_true",
                   help="silence-split long audio into per-account chunks, then merge "
                        "(srt/vtt/txt only)")
    t.add_argument("--chunk-secs", type=int,
                   help="max chunk length in seconds (default derived from account quota)")
    t.add_argument("--keep-chunks", action="store_true",
                   help="keep temporary chunk files after a successful merge")
    t.add_argument("--silence-db", type=float,
                   help=f"silencedetect noise floor in dB (default {audio_split.SILENCE_DB_DEFAULT})")
    t.add_argument("--silence-min", type=float,
                   help=f"silencedetect min silence seconds (default {audio_split.SILENCE_MIN_DEFAULT})")

    sub.add_parser("list-languages", help="print supported language names + codes")
    sc = sub.add_parser("selfcheck", help="run offline self-check")

    a = sub.add_parser("accounts", help="list accounts + remaining credits")
    a.add_argument("-c", "--config", default=str(CONFIG_PATH), help="config.toml path")
    a.add_argument("--refresh", action="store_true", help="force-refresh credits from the API")
    a.add_argument("-e", "--email", action="append", default=[], metavar="EMAIL",
                   help="only act on this account (repeatable); filters both --refresh scope and listing by email")

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

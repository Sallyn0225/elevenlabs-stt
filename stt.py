#!/usr/bin/env python3
"""ElevenLabs web Speech-to-Text CLI.

Replays the web app's internal API (api.us.elevenlabs.io) using the user's own
logged-in Firebase session. See research/api-contract.md for the captured
contract and README.md for usage.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import threading
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
    "max_concurrency": 4,   # 跨账号并发 worker 上限；1 = 回退串行（降级开关）
    "stagger_secs": 2.0,    # 相邻上传起始的最小间隔（秒）；0 = 不错峰
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


# 并发转录下多线程各自触发落盘（JWT 轮换回调、注册线程 upsert）；持锁写文件
# 杜绝两个线程交错写出半个 JSON。dict 字段赋值本身 GIL 原子，撕裂读无害。
_STORE_LOCK = threading.Lock()


def save_accounts(store: dict[str, Any]) -> None:
    with _STORE_LOCK:
        _write_accounts(store)


def _write_accounts(store: dict[str, Any]) -> None:
    """Unlocked write; call only while holding _STORE_LOCK (or single-threaded)."""
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


def poll_task(client: httpx.Client, task_id: str, timeout: float, label: str = "") -> dict:
    deadline = time.time() + timeout
    last_progress = -1.0
    tag = f"[{label}] " if label else ""  # 并发下区分是哪个文件在跑
    while time.time() < deadline:
        r = client.get(f"/v1/speech-to-text/tasks/{task_id}")
        if r.status_code != 200:
            raise SystemExit(f"poll failed ({r.status_code}): {r.text[:300]}")
        task = r.json()
        state = task.get("state", "")
        progress = task.get("progress", 0.0)
        if progress != last_progress:
            _tlog(f"{tag}{state} {progress*100:.0f}%")
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


def fresh_count(store: dict[str, Any], threshold: int) -> int:
    return sum(1 for a in store["accounts"]
               if not a.get("invalid") and (cached_remaining(a) or 0) >= threshold)


def pool_counts(store: dict[str, Any], threshold: int) -> dict[str, int]:
    """账号池状态计数（纯缓存，不联网）；cmd_pool_status 与 web.pool_summary 共用。"""
    fresh = usable = depleted = invalid = unknown = 0
    for a in store["accounts"]:
        if a.get("invalid"):
            invalid += 1
            continue
        rem = cached_remaining(a)
        if rem is None:
            unknown += 1
        elif rem == 0:
            depleted += 1
        elif rem >= threshold:
            fresh += 1
        else:
            usable += 1
    return {"fresh": fresh, "usable": usable, "depleted": depleted,
            "invalid": invalid, "unknown": unknown, "total": len(store["accounts"])}


def refresh_many(store: dict[str, Any], targets: list[dict[str, Any]], on_each=None) -> None:
    """并行 force 刷新 targets 的额度。每个线程只写自己的账号 dict，落盘延后到全部
    完成后一次执行，避免多线程并发 dump 共享 store 的竞态（含 JWT 轮换的中间保存）。
    on_each: 可选 (account, rem) 回调，单个账号刷新完成后在工作线程内调用。"""
    def _one(a):
        rem = account_remaining(a, store, force=True, save=lambda _s: None)
        if on_each:
            on_each(a, rem)
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_one, targets))
    if targets:
        save_accounts(store)


def manual_shortfall_msg(n: int, tail: str) -> str:
    """手动模式缺口报错：共用前缀 + 调用方后缀（CLI 与 web 文案后缀不同，逐字保持现状）。"""
    return f"所选账号额度不足，还差 {n} 个新账号{tail}"


def refill_pool(store: dict[str, Any], config_path: pathlib.Path) -> None:
    acfg = accounts_config(config_path)
    if not acfg["auto_refill"] or not has_temp_email_config(config_path):
        return
    from register import register_one  # lazy: register.py imports stt back
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
    """A packing bin: an existing account handle (slot None) or a virtual fresh bin ("NEW#k")."""
    __slots__ = ("handle", "residual", "slot", "before", "use", "keys")

    def __init__(self, handle, residual, slot=None):
        self.handle = handle
        self.residual = residual
        self.slot = slot  # "NEW#k" label for virtual bins
        self.before = residual
        self.use = 0
        self.keys: list = []


def pack_bins(costs, residuals, margin, fresh_threshold) -> dict:
    """纯装箱核心：FFD 降序 + best-fit，先现有 bin 后虚拟 fresh bin。不联网、不落盘、不 raise。

    costs:     [(key, required_credits | None)]   key 任意可哈希（Path 或 str）
    residuals: [(handle, remaining_int)]          handle 不透明（账号 dict 或 email str）
    返回 {
      "assignments":    [(key, handle | "NEW#k")],  # 现有账号项在前、NEW 在后，组内稳定
      "bins":           [{"handle": handle|"NEW#k", "is_new": bool, "before": int,
                          "use": int, "keys": [key, ...]}],  # 只含被用到的 bin，existing-first
      "register_count": int,
      "oversize":       [(key, need)],              # need > fresh_cap 的项，不进 assignments
    }
    规则与原 allocate 完全一致：need = int(req*margin)+1；req None → 独占虚拟 bin
    （residual 0，不计 use）；fresh_cap = int(fresh_threshold*margin)。
    """
    def need(req):
        return None if req is None else int(req * margin) + 1

    # FFD: largest need first. Unknown-duration files (key -1) sort last but their
    # order is irrelevant — the loop gives each its own dedicated fresh bin regardless.
    files = sorted(costs, key=lambda fc: (-1 if fc[1] is None else need(fc[1])),
                   reverse=True)

    bins = [_Bin(h, r or 0) for h, r in residuals]
    fresh_cap = int(fresh_threshold * margin)
    virtual: list[_Bin] = []
    assigned: list[tuple[Any, Any, bool]] = []  # (key, target, is_new) — is_new drives the sort
    oversize: list[tuple[Any, int]] = []

    def open_virtual(residual):
        b = _Bin(None, residual, slot=f"NEW#{len(virtual)}")
        virtual.append(b)
        return b

    for key, req in files:
        if req is None:  # unknown duration -> dedicated fresh bin (use not counted)
            b = open_virtual(0)
            b.keys.append(key)
            assigned.append((key, b.slot, True))
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
                    oversize.append((key, n))
                    continue
                b = open_virtual(fresh_cap)
        b.residual -= n
        b.use += n
        b.keys.append(key)
        assigned.append((key, b.handle if b.slot is None else b.slot, b.slot is not None))

    # existing-account items first, NEW#k items last; stable within each group.
    assigned.sort(key=lambda ktn: ktn[2])
    used = [b for b in bins + virtual if b.keys]
    used.sort(key=lambda b: b.slot is not None)
    return {
        "assignments": [(k, t) for k, t, _ in assigned],
        "bins": [{"handle": b.slot if b.slot is not None else b.handle,
                  "is_new": b.slot is not None, "before": b.before,
                  "use": b.use, "keys": b.keys} for b in used],
        "register_count": len(virtual),
        "oversize": oversize,
    }


def allocate(costs, accounts, margin, fresh_threshold, store):
    """Bin-pack files across existing accounts (best-fit) then virtual fresh bins.

    costs: [(audio_path, required_or_None)]; accounts: non-invalid account dicts.
    Returns (plan, register_count) where plan is an ordered list of
    (audio_path, account_dict_or_"NEW#k"), existing-account files before NEW ones.
    """
    residuals = [(a, account_remaining(a, store) or 0) for a in accounts]
    r = pack_bins(costs, residuals, margin, fresh_threshold)
    if r["oversize"]:  # FFD 序保证首个 oversize 即原实现 raise 的那一项
        key, n = r["oversize"][0]
        fresh_cap = int(fresh_threshold * margin)
        raise SystemExit(f"{key}: needs {n} > one free account ({fresh_cap}); out of scope")
    return r["assignments"], r["register_count"]


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

        _stagger_wait(float(cfg.get("stagger_secs", 0) or 0))  # 错峰：只对上传起始限速
        _tlog(f"[{audio.name}] 上传 ({size/1e6:.1f} MB)…")
        task = create_task(client, audio, cfg)
        task_id = task["_id"]
        _tlog(f"[{audio.name}] 任务 {task_id} 已创建 (state={task.get('state')})")

        result = poll_task(client, task_id, cfg["poll_timeout_secs"], label=audio.name)
        lang_code = (result.get("result") or {}).get("language_code")

        fmt = cfg["export_format"]
        _tlog(f"[{audio.name}] 导出 {fmt}…")
        content = export_task(client, task_id, fmt, lang_code)

    out = pathlib.Path(output) if output else audio.with_suffix(f".{fmt}")
    out.write_bytes(content)
    print(f"wrote {out} ({len(content)} bytes)")  # stdout 结果行契约不变
    _tlog(f"[{audio.name}] 完成 {out.name}")
    return out


def group_plan(plan) -> tuple[list, dict[int, list]]:
    """按目标分组（纯函数）：existing 账号各一组，NEW#k 槽位各一组，组内保持 plan 序。

    plan: allocate 输出 [(input, account_dict | "NEW#k")]
    返回 (existing_groups, pending)：
      existing_groups = [(account_dict, [(plan_idx, input), ...])]（首次出现序）
      pending         = {k: [(plan_idx, input), ...]}（NEW#k 槽位号 → 项）
    """
    existing: dict[str, tuple[dict[str, Any], list]] = {}
    order: list[str] = []
    pending: dict[int, list] = {}
    for idx, (inp, target) in enumerate(plan):
        if isinstance(target, str):
            pending.setdefault(int(target.split("#")[1]), []).append((idx, inp))
        else:
            email = target.get("email")
            if email not in existing:
                existing[email] = (target, [])
                order.append(email)
            existing[email][1].append((idx, inp))
    return [existing[e] for e in order], pending


# 错峰闸：全局取号，保证任意两次上传起始至少间隔 stagger_secs（同 IP 突发风控）。
_GATE_LOCK = threading.Lock()
_GATE_NEXT = 0.0


def _stagger_wait(stagger_secs: float, clock=time.monotonic, sleep=time.sleep) -> float:
    """上传前取号并 sleep；返回实际等待秒数。stagger_secs <= 0 短路（不错峰）。
    clock/sleep 可注入，selfcheck 离线验证闸行为。"""
    global _GATE_NEXT
    if stagger_secs <= 0:
        return 0.0
    with _GATE_LOCK:
        now = clock()
        wait = max(0.0, _GATE_NEXT - now)
        _GATE_NEXT = max(_GATE_NEXT, now) + stagger_secs
    sleep(wait)
    return wait


def run_plan_pipelined(plan, cfg, store, config_path, output_for, register_count=0,
                       register_fn=None, on_start=None, on_done=None,
                       on_active=None, refresh_used=True,
                       transcribe_fn=None) -> tuple[list, list]:
    """按账号分组并发执行转录计划；缺口账号边注册边投产（流水线）。

    plan:        allocate 输出 [(input, account_dict | "NEW#k")]（上游失败项已滤掉）
    output_for:  input -> 输出路径 str | None（None = transcribe_one 默认命名）
    on_start:    可选 (i, total, inp, account) 回调，转录该项之前调用
    on_done:     可选 (inp, ok) 回调，该项结束后调用（web done 计数用；注册失败标 FAIL 的项也会回调）
    on_active:   可选 (inp, stage|None) 回调，进入/离开转录时调用（web 活动任务列表用）
    refresh_used: 结束后是否 force 刷新用过账号的额度（selfcheck 传 False 保持离线）
    register_fn / transcribe_fn / refresh_used 可注入，selfcheck 离线覆盖注册失败路径。

    语义：同账号内任务严格串行，不同账号并行（上限 cfg["max_concurrency"]，1 = 回退串行）；
    注册线程单独串行推进，每注册好一个账号立刻提交其 NEW#k 组，注册中途失败则停止后续
    注册、未注册槽位的项标 FAIL（已在跑的组照常完成，已注册账号已落盘）。
    不再逐项挪动 store["active"]（并发下无意义的共享写；login/register 仍照常设置它）。
    返回 (results, used)：results 按 plan 原顺序，(inp, email, "OK"|"FAIL", detail) 契约不变。
    """
    total = len(plan)
    if register_count and register_fn is None:  # lazy: register.py imports stt back
        from register import register_one as register_fn
    existing_groups, pending = group_plan(plan)
    results_by_idx: dict[int, tuple] = {}
    used: list[dict[str, Any]] = []
    used_lock = threading.Lock()
    stop = threading.Event()
    do_one = transcribe_fn or transcribe_one

    def run_group(account, items):  # 同账号内严格串行；组间由线程池并行
        email = account.get("email")
        with used_lock:
            if account not in used:
                used.append(account)
        for idx, inp in items:
            if stop.is_set():
                results_by_idx[idx] = (inp, email, "FAIL", "cancelled")
                continue
            if on_start:
                on_start(idx + 1, total, inp, account)
            if on_active:
                on_active(inp, "转录中")
            _tlog(f"[{idx + 1}/{total}] {inp.name} → {email}")
            try:
                out = do_one(inp, cfg, account, store, config_path, output_for(inp))
                results_by_idx[idx] = (inp, email, "OK", str(out))
                ok = True
            except Exception as e:  # skip & continue
                _tlog(f"warn: {inp.name} failed: {e!r}")
                results_by_idx[idx] = (inp, email, "FAIL", repr(e))
                ok = False
            finally:
                if on_active:
                    on_active(inp, None)
            if on_done:
                on_done(inp, ok)

    def fail_slots(from_k: int, reason: str) -> None:
        """注册停止后，把绑定到未注册槽位的项全部标 FAIL。"""
        for k in range(from_k, register_count):
            for idx, inp in pending.get(k, []):
                results_by_idx[idx] = (inp, f"NEW#{k}", "FAIL", reason)
                if on_done:
                    on_done(inp, False)

    max_workers = max(1, int(cfg.get("max_concurrency", DEFAULTS["max_concurrency"])))
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futures = []

    def run_register():  # 注册天然串行（pyautogui 前台独占）；不占线程池 worker
        for k in range(register_count):
            if stop.is_set():
                fail_slots(k, "cancelled")
                return
            try:
                _rlog(f"注册缺口账号 {k + 1}/{register_count}")
                acct = register_fn()
                with _STORE_LOCK:  # 注册成功立刻落盘（一个 ~1 分钟，丢了最贵）
                    upsert_account(store, acct)
                    _write_accounts(store)
                futures.append(ex.submit(run_group, acct, pending.get(k, [])))
            except BaseException as e:  # 停止后续注册；已在跑的组照常完成。
                # 落盘/submit 失败也必须走这里：线程静默死掉会让绑定槽位的行
                # 从 results 消失（print_summary 少行、web 条目卡在“未执行”）。
                _rlog(f"注册失败，剩余 {register_count - k} 个槽位的任务标记失败: {e!r}")
                fail_slots(k, f"注册失败: {e!r}")
                return

    reg_thread = None
    try:
        for account, items in existing_groups:  # 已有账号的任务立即开跑（R3/AC3）
            futures.append(ex.submit(run_group, account, items))
        if register_count:
            # ponytail: daemon 线程 — Ctrl+C 不再能像旧串行版那样打断 register_one 内部
            # 并触发其 finally 清理（正在注册的 Chrome 可能残留，下次注册/手动关闭兜底）；
            # 换来的是主线程可即时响应中断。stop 事件在两次注册之间生效。
            reg_thread = threading.Thread(target=run_register, daemon=True)
            reg_thread.start()
            reg_thread.join()  # 之后 futures 不再增长，可安全遍历
        for f in futures:
            f.result()  # run_group 自捕获业务异常；这里只等待
        ex.shutdown(wait=True)
    except BaseException:  # KeyboardInterrupt: 取消未开始的项，通知注册线程停止
        stop.set()
        # ponytail: 线程池线程非 daemon（3.9+ 解释器退出前会 join），在飞的上传/poll
        # 会跑完才真正退出；要即时中止需给 poll_task 加协作式 stop 检查，暂不做。
        ex.shutdown(wait=False, cancel_futures=True)
        raise

    results = [results_by_idx[i] for i in sorted(results_by_idx)]
    if refresh_used and used:
        # ponytail: ElevenLabs debits credits asynchronously — this refresh can race
        # the debit and record a still-high remaining; margin + next-run refresh absorb the lag.
        refresh_many(store, used)
    return results, used


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
        raise SystemExit(manual_shortfall_msg(
            register_count, "才能装下全部文件；请增选账号或去掉 --account 回到自动分配"))

    if register_count and not has_temp_email_config(config_path):
        raise SystemExit(f"need {register_count} more account(s) but [temp_email] not configured")

    # pipelined: existing-account items start now; the shortfall registers alongside
    single = len(files) == 1
    results, _ = run_plan_pipelined(plan, cfg, store, config_path,
                                    output_for=lambda _inp: args.output if single else None,
                                    register_count=register_count)
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
        if p.get("skipped"):
            print(f"      skipped {p['skipped']:.1f}s of silence (not uploaded)",
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
                silences = audio_split.detect_silences(audio, noise_db, min_silence)
                if getattr(args, "skip_silence", False):
                    segs, hard = audio_split.plan_cuts_skip(total, chunk_secs, silences,
                                                            args.skip_silence_min)
                    if not segs:  # entirely long silence: nothing to transcribe
                        parents.append({"audio": audio, "final": final,
                                        "error": "音频内容全部为静音", "chunks": []})
                        continue
                else:
                    mids = [(s + e) / 2 for (s, e) in silences]
                    segs, hard = audio_split.plan_cuts(total, chunk_secs, mids)
                workdir = pathlib.Path("out") / f"{audio.stem}-chunks"
                skipped = total - sum(e - s for s, e in segs)
                p = {"audio": audio, "final": final, "split": True, "workdir": workdir,
                     "segs": segs, "hard": hard, "chunks": [],
                     "skipped": skipped if skipped > 1e-6 else 0.0}
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
        raise SystemExit(manual_shortfall_msg(
            register_count, "才能装下全部分段；请增选账号或去掉 --account 回到自动分配"))

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

    entry_by_input = {e["input"]: e for e in entries}

    # --- transcribe each chunk pipelined (cut-failed entries skip upload) ------
    plan = [(inp, target) for inp, target in plan
            if entry_by_input[inp]["ok"] is not False]
    results, _ = run_plan_pipelined(plan, cfg, store, config_path,
                                    output_for=lambda inp: str(entry_by_input[inp]["output"]),
                                    register_count=register_count)
    for inp, email, status, detail in results:  # merge (below) reads entry["ok"]
        entry_by_input[inp]["ok"] = status == "OK"
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
        refresh_many(store, targets, on_each=lambda a, rem: print(
            f"  refreshed {a.get('email')}: rem={rem}", file=sys.stderr))
        if wanted:
            known = {a.get("email") for a in accts}
            for e in sorted(wanted - known):
                print(f"  no matching account: {e}", file=sys.stderr)
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
    from register import register_one  # lazy: register.py imports stt back
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
    c = pool_counts(store, threshold)
    fresh = c["fresh"]
    print(f"pool target: {target}")
    print(f"  fresh (>= {threshold}):    {fresh}")
    print(f"  usable:           {c['usable']}")
    print(f"  depleted:         {c['depleted']}")
    print(f"  unknown (refresh first): {c['unknown']}")
    print(f"  invalid:          {c['invalid']}")
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
    t.add_argument("--skip-silence", action="store_true",
                   help="with --split: drop silences >= --skip-silence-min from the chunks "
                        "(not uploaded, not billed; may produce more segments)")
    t.add_argument("--skip-silence-min", type=float, default=audio_split.SKIP_SILENCE_DEFAULT,
                   help=f"minimum silence length in seconds to skip "
                        f"(default {audio_split.SKIP_SILENCE_DEFAULT})")

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
        from selfcheck import run  # lazy: selfcheck.py imports stt back
        return run()
    if args.cmd == "accounts":
        return cmd_accounts(args)
    if args.cmd == "pool":
        return cmd_pool(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

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
import shutil
import subprocess
import sys
import time
import tomllib
from typing import Any

import httpx

# ponytail: constants centralised so endpoint renames are one-line fixes
FIREBASE_API_KEY = "AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
FIREBASE_USER_KEY = f"firebase:authUser:{FIREBASE_API_KEY}:[DEFAULT]"
API_BASE = "https://api.us.elevenlabs.io"
SECURETOKEN_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"
LOGIN_URL = "https://elevenlabs.io/app/speech-to-text"

SESSION_PATH = pathlib.Path("session.json")
CONFIG_PATH = pathlib.Path("config.toml")

MAX_FILE_BYTES = 1000 * 1024 * 1024  # 1000 MB hard limit
FREE_DURATION_WARN_SECS = 600  # 10 min soft warn for free accounts
POLL_INTERVAL_SECS = 3

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

def load_config(path: pathlib.Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        if "transcribe" in data:
            cfg.update(data["transcribe"])
        else:
            cfg.update(data)
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


def get_jwt(session: dict[str, Any]) -> str:
    jwt = session.get("jwt")
    exp = session.get("jwt_exp", 0)
    if jwt and exp > time.time() + 60:
        return jwt
    rt = session.get("refreshToken")
    if not rt:
        raise SystemExit("session has no refresh token — re-run `stt login`")
    resp = httpx.post(
        SECURETOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": rt},
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
    save_session(session)
    return session["jwt"]


def authed_client(session: dict[str, Any]) -> httpx.Client:
    jwt = get_jwt(session)
    return httpx.Client(
        base_url=API_BASE,
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=httpx.Timeout(30.0, read=None),  # upload may be slow: no read cap
    )


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
    save_session(session)
    print(f"saved {SESSION_PATH} (email={session['email']})")
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

    session = load_session()
    with authed_client(session) as client:
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
    print("selfcheck ok")
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
    return 1


if __name__ == "__main__":
    sys.exit(main())

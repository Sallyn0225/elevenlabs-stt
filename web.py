#!/usr/bin/env python3
"""Local web UI for elevenlabs-stt.

Thin stdlib HTTP layer over stt.py. Serves webui.html and a small JSON API that
reuses stt's real functions (allocate / transcribe_one / account_remaining).
Run from the project root (needs accounts.json + config.toml alongside stt.py):

    python web.py           # http://127.0.0.1:8756

ponytail: single-user local tool — one global lock around store mutations,
uploads kept in one temp dir, no auth. Not meant to face the internet.
"""
from __future__ import annotations

import base64
import datetime
import json
import pathlib
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import stt

HERE = pathlib.Path(__file__).resolve().parent
HTML = HERE / "webui.html"
CONFIG_PATH = stt.CONFIG_PATH
_LOCK = threading.Lock()          # serialise accounts.json read/modify/write
_UPLOAD_DIR = pathlib.Path(tempfile.mkdtemp(prefix="elevenlabs-stt-web-"))
_UPLOADS: dict[str, dict] = {}    # id -> {"path": Path, "name": str, "duration": float|None}


# ------------------------------------------------------------- view helpers

def _created_str(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def account_view(a: dict) -> dict:
    """Shape one account for the UI (no network)."""
    ck = a.get("credits_known") or {}
    limit = ck.get("limit", 10000)
    used = ck.get("count")
    rem = stt.cached_remaining(a)
    if a.get("invalid"):
        status = "invalid"
    elif rem is None:
        status = "unknown"
    elif rem == 0:
        status = "depleted"
    elif used == 0:
        status = "fresh"
    else:
        status = "partial"
    return {
        "email": a.get("email"),
        "password": a.get("password"),          # usually absent; UI masks/omits
        "source": a.get("source", "?"),
        "limit": limit,
        "used": used,
        "rem": rem,
        "created": a.get("created_at"),
        "createdStr": _created_str(a.get("created_at")),
        "invalid": bool(a.get("invalid")),
        "status": status,
    }


def pool_summary(store: dict, acfg: dict) -> dict:
    thr = acfg["fresh_threshold"]
    fresh = usable = depleted = 0
    for a in store["accounts"]:
        if a.get("invalid"):
            continue
        rem = stt.cached_remaining(a)
        if rem is None or rem == 0:
            depleted += rem == 0
        elif rem >= thr:
            fresh += 1
        else:
            usable += 1
    return {"total": len(store["accounts"]), "fresh": fresh,
            "usable": usable, "target": acfg["pool_target"]}


def build_state() -> dict:
    store = stt.load_accounts()
    acfg = stt.accounts_config(CONFIG_PATH)
    langs = [{"name": n, "code": c} for n, c in
             sorted(stt.LANGUAGES.items(), key=lambda kv: kv[0])]
    return {
        "accounts": [account_view(a) for a in store["accounts"]],
        "pool": pool_summary(store, acfg),
        "languages": langs,
        "formats": list(stt.EXPORT_FORMATS),
        "defaults": {k: stt.DEFAULTS[k] for k in
                     ("tag_audio_events", "include_subtitles", "no_verbatim",
                      "use_speaker_library", "export_format", "show_cost")},
        "margin": acfg["selection_margin"],
        "freshThreshold": acfg["fresh_threshold"],
        "cps": stt.CREDITS_PER_SEC,
    }


def compute_plan(items: list[dict]) -> dict:
    """items: [{name, duration}] -> allocation preview.

    ponytail: mirrors stt.allocate's best-fit but reads cached remaining only —
    a preview fires on every file add and must not network or write accounts.json.
    The authoritative allocation at transcribe time still uses stt.allocate.
    """
    acfg = stt.accounts_config(CONFIG_PATH)
    margin, thr = acfg["selection_margin"], acfg["fresh_threshold"]
    fresh_cap = int(thr * margin)
    store = stt.load_accounts()
    accounts = [a for a in store["accounts"] if not a.get("invalid")]

    need = lambda req: None if req is None else int(req * margin) + 1
    costs = [(it["name"], stt.estimate_required(it.get("duration"))) for it in items]
    # largest need first; unknown-duration (None) sorts last (dedicated fresh bin)
    ordered = sorted(costs, key=lambda fc: (-1 if fc[1] is None else need(fc[1])),
                     reverse=True)

    class Bin:
        def __init__(self, email, residual, is_new):
            self.email, self.residual, self.before = email, residual, residual
            self.is_new, self.files, self.use = is_new, [], 0

    bins = [Bin(a.get("email"), stt.cached_remaining(a) or 0, False) for a in accounts]
    virtual: list[Bin] = []
    register_count = 0
    total_need = total_credits = 0

    def open_virtual(residual):
        nonlocal register_count
        b = Bin("（新账号）", residual, True)
        virtual.append(b); register_count += 1
        return b

    for name, req in ordered:
        if req is None:
            b = open_virtual(0)
        else:
            n = need(req)
            cand = [b for b in bins if b.residual >= n]
            if cand:
                b = min(cand, key=lambda b: b.residual)
            else:
                vc = [b for b in virtual if b.residual >= n]
                b = min(vc, key=lambda b: b.residual) if vc else open_virtual(fresh_cap)
            b.residual -= n
            b.use += n
            total_need += n
            total_credits += req
        b.files.append(name)

    used = [b for b in bins + virtual if b.files]
    used.sort(key=lambda b: b.is_new)  # existing accounts first
    alloc = [{"email": b.email, "files": b.files, "count": len(b.files),
              "useSum": b.use, "remBefore": b.before, "remAfter": b.before - b.use,
              "isNew": b.is_new} for b in used]
    return {"alloc": alloc, "registerCount": register_count,
            "totalNeed": total_need, "totalCredits": total_credits}


# ---------------------------------------------------------------- actions

def do_refresh(emails: list[str]) -> dict:
    with _LOCK:
        store = stt.load_accounts()
        wanted = set(emails)
        for a in store["accounts"]:
            if a.get("email") in wanted and not a.get("invalid"):
                stt.account_remaining(a, store, force=True)   # network
        stt.save_accounts(store)
        return build_state()


def do_delete(emails: list[str]) -> dict:
    with _LOCK:
        store = stt.load_accounts()
        wanted = set(emails)
        store["accounts"] = [a for a in store["accounts"]
                             if a.get("email") not in wanted]
        if store.get("active") in wanted:
            store["active"] = None
        stt.save_accounts(store)
        return build_state()


def do_transcribe(upload_ids: list[str], params: dict) -> dict:
    """Reuse stt.allocate + transcribe_one on the uploaded temp files."""
    uploads = [_UPLOADS[i] for i in upload_ids if i in _UPLOADS]
    if not uploads:
        return {"error": "没有可转录的文件"}

    cfg = dict(stt.DEFAULTS)
    cfg["language"] = params.get("language", "auto")
    for k in ("tag_audio_events", "include_subtitles", "no_verbatim",
              "use_speaker_library", "show_cost"):
        if k in params:
            cfg[k] = bool(params[k])
    cfg["export_format"] = params.get("format", "srt")
    cfg["vocab"] = [w for w in params.get("vocab", []) if w.strip()]
    if params.get("pollTimeout"):
        cfg["poll_timeout_secs"] = int(params["pollTimeout"])
    try:
        cfg["language_code"] = stt.resolve_language(cfg["language"])
    except ValueError as e:
        return {"error": str(e)}

    with _LOCK:
        store = stt.load_accounts()
        acfg = stt.accounts_config(CONFIG_PATH)
        accounts = [a for a in store["accounts"] if not a.get("invalid")]
        for a in accounts:
            stt.account_remaining(a, store)          # accurate remaining
        stt.save_accounts(store)

        costs = [(u["path"], stt.estimate_required(u["duration"])) for u in uploads]
        try:
            plan, register_count = stt.allocate(
                costs, accounts, acfg["selection_margin"], acfg["fresh_threshold"], store)
        except SystemExit as e:
            return {"error": str(e)}
        if register_count:
            return {"error": f"当前账号池不足,还需 {register_count} 个新账号。"
                             f"请先在命令行运行 `python stt.py pool warm` 预热账号池后重试。"}

        results = []
        used = []
        for audio, account in plan:                  # plan targets are real accounts here
            email = account.get("email")
            store["active"] = email
            stt.save_accounts(store)
            if account not in used:
                used.append(account)
            try:
                out = stt.transcribe_one(audio, cfg, account, store, CONFIG_PATH)
                data = out.read_bytes()
                results.append({
                    "name": audio.name, "ok": True, "email": email,
                    "download": out.name,
                    "content": base64.b64encode(data).decode("ascii"),
                })
            except Exception as e:  # skip & continue, like cmd_transcribe
                results.append({"name": audio.name, "ok": False,
                                "email": email, "error": repr(e)})
        for a in used:
            stt.account_remaining(a, store, force=True)
        stt.save_accounts(store)

    return {"results": results, "state": build_state()}


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _read_json(self) -> dict:
        raw = self._read_body()
        return json.loads(raw) if raw else {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/webui.html"):
            body = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send_json(build_state())
            return
        self.send_error(404)

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/api/upload":
                data = self._read_body()
                name = qs.get("name", ["audio"])[0]
                uid = uuid.uuid4().hex
                dest = _UPLOAD_DIR / f"{uid}_{pathlib.Path(name).name}"
                dest.write_bytes(data)
                dur = stt.audio_duration(dest)
                if dur is None and qs.get("duration"):
                    try:
                        dur = float(qs["duration"][0])
                    except ValueError:
                        dur = None
                _UPLOADS[uid] = {"path": dest, "name": pathlib.Path(name).name,
                                 "duration": dur}
                credits = stt.estimate_required(dur)
                self._send_json({"id": uid, "name": pathlib.Path(name).name,
                                 "duration": dur, "credits": credits,
                                 "size": len(data)})
                return
            if path == "/api/plan":
                self._send_json(compute_plan(self._read_json().get("files", [])))
                return
            if path == "/api/transcribe":
                body = self._read_json()
                self._send_json(do_transcribe(body.get("uploads", []),
                                              body.get("params", {})))
                return
            if path == "/api/accounts/refresh":
                self._send_json(do_refresh(self._read_json().get("emails", [])))
                return
            if path == "/api/accounts/delete":
                self._send_json(do_delete(self._read_json().get("emails", [])))
                return
            self.send_error(404)
        except Exception as e:  # never let a handler crash the thread silently
            self._send_json({"error": repr(e)}, code=500)


def main(host="127.0.0.1", port=8756):
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"elevenlabs-stt web UI → http://{host}:{port}  (uploads: {_UPLOAD_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8756
    main(port=p)

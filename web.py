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

import audio_split
import stt

HERE = pathlib.Path(__file__).resolve().parent
HTML = HERE / "webui.html"
FAVICON = HERE / "favicon.webp"
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
    c = stt.pool_counts(store, acfg["fresh_threshold"])
    return {"total": c["total"], "fresh": c["fresh"],
            "usable": c["usable"], "target": acfg["pool_target"]}


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
        # default chunk length for the 长音频切分 tool: largest span a single fresh
        # account can transcribe within margin (audio_split derives it).
        "chunkSecs": audio_split.default_chunk_secs(
            acfg["fresh_threshold"], stt.CREDITS_PER_SEC, acfg["selection_margin"]),
        # temp_email/[accounts] config for the 启动注册机 modal (local single-user tool,
        # so exposing the backend secrets to the localhost UI matches accounts.json).
        "tempEmail": stt.temp_email_config(CONFIG_PATH),
    }


def compute_plan(items: list[dict], allowed: list[str] | None = None,
                 skip_silence: bool = False) -> dict:
    """items: [{id, name, duration}] -> allocation preview.

    allowed 非空时把候选账号限定为所列邮箱（手动模式,未知邮箱静默忽略——预览是弱校验,
    /api/transcribe 里的 stt.filter_accounts 才是强校验）。

    与转录路径共享 stt.pack_bins 装箱核心，但余额只读缓存（cached_remaining）—
    预览在每次加文件时触发，不允许联网或写 accounts.json。转录时的权威分配
    仍走 stt.allocate（live 余额）。

    Files longer than chunk_secs expand into their real silence-planned segments
    (same _plan_one the transcribe path uses, silences cached per upload), so the
    preview never shows a whole long file crammed into one negative virtual bin.
    """
    acfg = stt.accounts_config(CONFIG_PATH)
    margin, thr = acfg["selection_margin"], acfg["fresh_threshold"]
    chunk_secs = audio_split.default_chunk_secs(thr, stt.CREDITS_PER_SEC, margin)
    store = stt.load_accounts()
    accounts = [a for a in store["accounts"] if not a.get("invalid")]
    manual = bool(allowed)
    if manual:
        allowed_set = set(allowed)
        accounts = [a for a in accounts if a.get("email") in allowed_set]

    costs: list[tuple[str, int | None]] = []
    finfo: list[dict] = []  # per input file: name + segCount / splitError
    for it in items:
        name, dur, uid = it["name"], it.get("duration"), it.get("id")
        if dur is not None and dur > chunk_secs:
            plan = _plan_one(uid, chunk_secs, audio_split.SILENCE_DB_DEFAULT,
                             audio_split.SILENCE_MIN_DEFAULT,
                             skip_min=audio_split.SKIP_SILENCE_DEFAULT if skip_silence
                             else None) if uid else \
                {"error": "缺少上传标识，无法切分"}
            if plan.get("error"):
                finfo.append({"name": name, "splitError": plan["error"]})
                continue  # surfaced as an error row, never a negative bin
            segs = plan["segments"]
            fi = {"name": name, "segCount": len(segs)}
            if plan.get("skipped"):
                fi["skipped"] = plan["skipped"]   # UI shows saved silence time (R6)
            finfo.append(fi)
            costs.extend((f"{name} · part{i:02d}", stt.estimate_required(e - s))
                         for i, (s, e) in enumerate(segs))
        else:
            finfo.append({"name": name})
            costs.append((name, stt.estimate_required(dur)))
    # 与实际转录同一份装箱核心（stt.pack_bins），只是余额喂缓存值（预览不联网）
    residuals = [(a.get("email"), stt.cached_remaining(a) or 0) for a in accounts]
    r = stt.pack_bins(costs, residuals, margin, thr)
    alloc = [{"email": b["handle"] if not b["is_new"] else "（新账号）",
              "files": b["keys"], "count": len(b["keys"]),
              "useSum": b["use"], "remBefore": b["before"],
              "remAfter": b["before"] - b["use"], "isNew": b["is_new"]}
             for b in r["bins"]]  # pack_bins 已按 existing-first 排序
    register_count = r["register_count"]
    oversize = [k for k, _n in r["oversize"]]
    total_need = sum(b["use"] for b in r["bins"])
    oversize_keys = set(oversize)
    total_credits = sum(req for k, req in costs
                        if req is not None and k not in oversize_keys)
    # 本批最小分配单元的 need：前端灰显"装不下最小段"的账号（弱判据,plan.error 兜底）
    min_need = min((int(req * margin) + 1 for _, req in costs if req is not None),
                   default=None)
    out = {"alloc": alloc, "registerCount": register_count,
           "totalNeed": total_need, "totalCredits": total_credits,
           "files": finfo, "oversize": oversize, "minNeed": min_need}
    if manual and register_count:
        out["error"] = stt.manual_shortfall_msg(
            register_count, "；手动模式不会自动注册，请增选账号或清空选择回到自动分配")
    return out


# ---------------------------------------------------------------- actions

def do_refresh(emails: list[str]) -> dict:
    with _LOCK:
        store = stt.load_accounts()
        wanted = set(emails)
        targets = [a for a in store["accounts"]
                   if a.get("email") in wanted and not a.get("invalid")]
        stt.refresh_many(store, targets)
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


def do_export(emails: list[str]) -> dict:
    """Full raw records (incl. refreshToken/jwt) for the selected emails, so the
    downloaded file is importable on another machine — the UI's masked view isn't."""
    with _LOCK:
        store = stt.load_accounts()
        wanted = set(emails)
        return {"accounts": [a for a in store["accounts"]
                             if a.get("email") in wanted]}


def do_import(accounts: list) -> dict:
    """Merge records into accounts.json by email: new → append, duplicate or
    malformed (no dict / no '@' email) → skip. Never overwrites existing entries."""
    with _LOCK:
        store = stt.load_accounts()
        existing = {a.get("email") for a in store["accounts"]}
        added = skipped = 0
        for rec in accounts:
            email = rec.get("email") if isinstance(rec, dict) else None
            if not isinstance(email, str) or "@" not in email or email in existing:
                skipped += 1
                continue
            store["accounts"].append(rec)
            existing.add(email)
            added += 1
        if added:
            stt.save_accounts(store)
        state = build_state()
        state.update(imported=added, skipped=skipped)
        return state


def do_login(email: str, password: str) -> dict:
    """Log in with email+password via ElevenLabs' Firebase REST endpoint (no browser),
    fetch credits, and save the account — the design's in-modal login.

    ponytail: reuse stt.account_from_password_signin + refresh_credits (the same REST
    sign-in tail register_one uses); no Playwright, no browser redirect. Marked source
    'manual' since a human typed the credentials.
    """
    with _LOCK:
        account = stt.account_from_password_signin(email, password)
        account["source"] = "manual"
        with stt.authed_client(account, save=lambda _s: None) as client:
            client.get("/v1/user")
            stt.refresh_credits(account, client)
        store = stt.load_accounts()
        stt.upsert_account(store, account)
        stt.save_accounts(store)
        return build_state()


# In-memory register progress, polled by GET /api/accounts/register/progress.
# ponytail: plain dict + GIL-atomic appends; _REG_LOCK only guards the active
# check-and-set. Single-user tool — one register run at a time is the contract.
_REG_PROGRESS: dict = {"active": False, "lines": [], "done": 0, "total": 0, "error": None}
_REG_LOCK = threading.Lock()


def _reg_log(msg: str) -> None:
    _REG_PROGRESS["lines"].append(msg)
    if len(_REG_PROGRESS["lines"]) > 500:   # defensive cap; a run is ~dozens of lines
        del _REG_PROGRESS["lines"][:-500]


def do_register(target: int | None) -> dict:
    """Warm the pool to `target` fresh accounts via the real register flow,
    streaming stt's step logs into _REG_PROGRESS for the UI to poll.

    ponytail: inlines cmd_pool_warm's small loop instead of delegating, so `done`
    counts accurately and accounts already registered survive a mid-run failure
    (saved every round). target=None falls back to the configured pool_target.
    """
    with _REG_LOCK:
        if _REG_PROGRESS["active"]:
            return {"error": "注册已在进行中"}
        _REG_PROGRESS.update(active=True, lines=[], done=0, total=0, error=None)
    stt.REGISTER_LOG = _reg_log
    try:
        acfg = stt.accounts_config(CONFIG_PATH)
        tgt = target or acfg["pool_target"]
        store = stt.load_accounts()
        fresh = stt.fresh_count(store, acfg["fresh_threshold"])
        _REG_PROGRESS["total"] = max(0, tgt - fresh)
        while fresh < tgt:
            _reg_log(f"账号池 {fresh}/{tgt}，开始注册第 {_REG_PROGRESS['done'] + 1} 个账号")
            account = stt.register_one()
            with _LOCK:
                stt.upsert_account(store, account)
                stt.save_accounts(store)
            _REG_PROGRESS["done"] += 1
            fresh = stt.fresh_count(store, acfg["fresh_threshold"])
        _reg_log(f"注册结束：账号池 {fresh}/{tgt}")
        return build_state()
    except (Exception, SystemExit) as e:
        msg = f"注册失败: {e}" if isinstance(e, SystemExit) else f"注册失败: {e!r}"
        _REG_PROGRESS["error"] = msg   # UI 把 error 单独渲染成红色行,不重复进 lines
        return {"error": msg, "state": build_state()}
    finally:
        stt.REGISTER_LOG = None
        _REG_PROGRESS["active"] = False


def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump_toml(data: dict) -> str:
    """Serialise a dict-of-tables (our config.toml shape) back to TOML text.

    ponytail: no stdlib TOML writer and config.toml is fully ours (flat [section] tables
    of str/bool/int/float/list values), so a ~10-line dumper beats adding tomli_w. Round-
    tripping drops the file's comment header — acceptable for a tool-managed config.
    """
    out = ["# Managed by web.py 启动注册机 — edits here sync with the UI.\n"]
    for section, body in data.items():
        out.append(f"[{section}]")
        for k, v in body.items():
            out.append(f"{k} = {_toml_scalar(v)}")
        out.append("")
    return "\n".join(out)


def do_save_config(temp_email: dict, pool_target) -> dict:
    """Persist the 启动注册机 params into config.toml [temp_email] + [accounts].pool_target,
    preserving every other section/key. Keeps config.toml and the UI in sync."""
    with _LOCK:
        data = stt.load_toml(CONFIG_PATH)                 # full existing config (or {})
        te = dict(stt.TEMP_EMAIL_DEFAULTS)
        te.update(data.get("temp_email", {}))
        for k, default in stt.TEMP_EMAIL_DEFAULTS.items():
            if k in temp_email:
                val = temp_email[k]
                if isinstance(default, bool):
                    te[k] = bool(val)
                elif isinstance(default, int):
                    try:
                        te[k] = int(val)
                    except (TypeError, ValueError):
                        te[k] = default
                elif isinstance(default, list):
                    te[k] = [str(x) for x in val] if isinstance(val, list) else te[k]
                else:
                    te[k] = str(val)
        # 输入即新建: a domain typed in the UI joins the dropdown candidates
        if te["domain"] and te["domain"] not in te["domains"]:
            te["domains"] = list(te["domains"]) + [te["domain"]]
        data["temp_email"] = te
        if pool_target:
            acc = dict(data.get("accounts", {}))
            acc["pool_target"] = int(pool_target)
            data["accounts"] = acc
        CONFIG_PATH.write_text(_dump_toml(data), encoding="utf-8")
    return build_state()


# In-memory transcribe progress, polled by GET /api/transcribe/progress.
# ponytail: same pattern as _REG_PROGRESS — plain dict + GIL-atomic appends;
# _TRANS_LOCK guards the running check-and-set plus the concurrent bits
# (active task list mutation, done += 1). One transcribe run at a time.
# "active" is the concurrent task list [{file, stage}]; top-level "stage"
# covers the serial phases (刷新额度/切分/合并).
_TRANS_PROGRESS: dict = {"running": False, "lines": [], "done": 0, "total": 0,
                         "active": [], "stage": None, "error": None}
_TRANS_LOCK = threading.Lock()


def _trans_log(msg: str) -> None:
    _TRANS_PROGRESS["lines"].append(msg)
    if len(_TRANS_PROGRESS["lines"]) > 1000:  # 转录日志含逐 pct 行,上限比注册高
        del _TRANS_PROGRESS["lines"][:-1000]


def do_transcribe(upload_ids: list[str], params: dict,
                  confirm_register: bool = False,
                  allowed: list[str] | None = None) -> dict:
    """Single-flight guard + progress hooks around _transcribe_impl."""
    with _TRANS_LOCK:
        if _TRANS_PROGRESS["running"]:
            return {"error": "转录已在进行中"}
        _TRANS_PROGRESS.update(running=True, lines=[], done=0, total=0,
                               active=[], stage=None, error=None)
    # 转录期间借用 REGISTER_LOG：缺口注册的 _rlog 行也进同一日志流
    stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = _trans_log
    try:
        try:
            res = _transcribe_impl(upload_ids, params, confirm_register, allowed)
        except SystemExit as e:  # stt 的 fatal = web 的报错消息(如 poll_task 转录失败)
            res = {"error": str(e)}
        if res.get("error"):
            _TRANS_PROGRESS["error"] = res["error"]
        return res
    except Exception as e:  # do_POST 的兜底还会接手;这里先把错误进进度流
        _TRANS_PROGRESS["error"] = repr(e)
        raise
    finally:
        stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = None
        _TRANS_PROGRESS["running"] = False


def _transcribe_impl(upload_ids: list[str], params: dict,
                     confirm_register: bool, allowed: list[str] | None) -> dict:
    """Web version of stt._transcribe_split: silence-split long uploads into chunks,
    allocate chunks + short files across accounts, register the user-confirmed
    shortfall, transcribe each piece, then merge split files back into one subtitle.

    Short uploads (duration <= chunk_secs) keep the original whole-file path.
    """
    uploads = [(i, _UPLOADS[i]) for i in upload_ids if i in _UPLOADS]
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

    acfg = stt.accounts_config(CONFIG_PATH)
    chunk_secs = audio_split.default_chunk_secs(
        acfg["fresh_threshold"], stt.CREDITS_PER_SEC, acfg["selection_margin"])
    fmt = cfg["export_format"]
    needs_split = any(u["duration"] and u["duration"] > chunk_secs for _, u in uploads)
    if needs_split and fmt not in stt._MERGERS:  # backend guard; UI greys the button
        return {"error": f"{fmt} 格式不支持长音频切分合并，请改用 SRT / VTT / TXT"}

    with _LOCK:
        store = stt.load_accounts()
        try:
            accounts, manual = stt.filter_accounts(store, allowed or [])
        except SystemExit as e:      # unknown/invalid email in the manual set
            return {"error": str(e)}
        _TRANS_PROGRESS["stage"] = "刷新账号额度"
        for a in accounts:
            stt.account_remaining(a, store)          # accurate remaining
        stt.save_accounts(store)

        # --- plan & cut: expand long uploads into chunk entries ------------
        _TRANS_PROGRESS["stage"] = "切分音频" if needs_split else "分配账号"
        parents: list[dict] = []
        entries: list[dict] = []
        for uid, up in uploads:
            name = up["name"]
            stem = pathlib.Path(name).stem
            dur = up["duration"]
            p = {"name": name, "split": False, "entries": [], "error": None,
                 "final": f"{stem}.{fmt}"}
            parents.append(p)
            if dur is not None and dur > chunk_secs:
                sp = _plan_one(uid, chunk_secs, audio_split.SILENCE_DB_DEFAULT,
                               audio_split.SILENCE_MIN_DEFAULT,
                               skip_min=audio_split.SKIP_SILENCE_DEFAULT
                               if params.get("skip_silence") else None)
                if sp.get("error"):
                    p["error"] = sp["error"]
                    continue
                segs = [(s, e) for s, e in sp["segments"]]
                try:
                    chunks = audio_split.cut_segments(
                        up["path"], segs, _UPLOAD_DIR / f"{uid}-chunks",
                        sp["hard"], stem=stem)
                except Exception as e:  # cut failed: skip this file, keep others
                    p["error"] = repr(e)
                    continue
                p["split"] = True
                for ch in chunks:
                    s, e = segs[ch.index]
                    entry = {"parent": p, "input": ch.path, "start": ch.start,
                             "dur": e - s, "output": ch.path.with_suffix(f".{fmt}"),
                             "ok": None}
                    p["entries"].append(entry)
                    entries.append(entry)
            else:
                entry = {"parent": p, "input": up["path"], "start": 0.0,
                         "dur": dur, "output": None, "ok": None}
                p["entries"].append(entry)
                entries.append(entry)

        if not entries:
            errs = "；".join(f"{p['name']}: {p['error']}" for p in parents if p["error"])
            return {"error": errs or "没有可转录的文件"}

        # --- allocate all pieces across accounts ---------------------------
        _TRANS_PROGRESS["total"] = len(entries)
        costs = [(e["input"], stt.estimate_required(e["dur"])) for e in entries]
        try:
            plan, register_count = stt.allocate(
                costs, accounts, acfg["selection_margin"], acfg["fresh_threshold"], store)
        except SystemExit as e:
            return {"error": str(e)}

        # manual mode never registers (强校验,防止预览与实际余额漂移后误注册)
        if manual and register_count:
            return {"error": stt.manual_shortfall_msg(
                register_count, "；手动模式不会自动注册，请增选账号或清空选择回到自动分配")}

        # --- confirmation gates before any registration side effect --------
        if register_count:
            if not confirm_register:  # UI confirms first; stale previews bounce here
                return {"needRegister": register_count}
            if not stt.has_temp_email_config(CONFIG_PATH):
                return {"error": f"当前账号池不足，还需 {register_count} 个新账号，"
                                 f"但 [temp_email] 未配置。请先在「启动注册机」中完成配置。"}
        entry_by_input = {e["input"]: e for e in entries}

        # --- transcribe pipelined: existing accounts start now, the shortfall
        # registers alongside; registration failures surface as FAIL rows ----
        _TRANS_PROGRESS["stage"] = "转录中"

        def _on_active(inp, stage):
            entry = entry_by_input[inp]
            # split chunks show their partNN name; whole files show the upload name
            fname = inp.name if entry["parent"]["split"] else entry["parent"]["name"]
            with _TRANS_LOCK:
                acts = _TRANS_PROGRESS["active"]
                acts[:] = [t for t in acts if t.get("key") != str(inp)]
                if stage:
                    acts.append({"key": str(inp), "file": fname, "stage": stage})

        def _on_done(inp, ok):
            with _TRANS_LOCK:  # += 非 GIL 原子，并发 worker 都会回调
                _TRANS_PROGRESS["done"] += 1

        results_raw, _ = stt.run_plan_pipelined(
            plan, cfg, store, CONFIG_PATH,
            output_for=lambda inp: (str(entry_by_input[inp]["output"])
                                    if entry_by_input[inp]["output"] else None),
            register_count=register_count, on_done=_on_done, on_active=_on_active)
        for inp, email, status, detail in results_raw:  # merge 段读 ok/out/err/email
            entry = entry_by_input[inp]
            entry.update(ok=status == "OK", email=email)
            if status == "OK":
                entry["out"] = pathlib.Path(detail)
            else:
                entry["err"] = detail
        stt.save_accounts(store)

    # --- merge per parent, or report failure (mirrors _transcribe_split R8) ---
    _TRANS_PROGRESS["stage"] = "合并字幕"
    results = []
    for p in parents:
        if p["error"]:
            results.append({"name": p["name"], "ok": False, "error": p["error"]})
            continue
        ents = p["entries"]
        emails = ", ".join(dict.fromkeys(e.get("email") or "?" for e in ents))
        bad = [i for i, e in enumerate(ents) if not e["ok"]]
        if bad:
            err = ents[bad[0]].get("err", "未执行")
            prefix = f"分段 {', '.join(f'part{i:02d}' for i in bad)} 失败：" if p["split"] else ""
            results.append({"name": p["name"], "ok": False, "email": emails,
                            "error": prefix + err})
            continue
        if p["split"]:
            merged = stt._MERGERS[fmt](
                [(e["start"], pathlib.Path(e["out"]).read_text(encoding="utf-8"))
                 for e in sorted(ents, key=lambda e: e["start"])])
            data, download = merged.encode("utf-8"), p["final"]
        else:
            out = ents[0]["out"]
            data, download = out.read_bytes(), out.name
        results.append({"name": p["name"], "ok": True, "email": emails,
                        "download": download,
                        "content": base64.b64encode(data).decode("ascii")})

    return {"results": results, "state": build_state()}


# ------------------------------------------------------- 长音频切分 (local)

def _split_params(body: dict) -> tuple[int, float, float, float | None]:
    """Resolve (chunk_secs, silence_db, silence_min, skip_min) from a request.

    chunk_secs defaults to the account-derived value and is clamped to [30, 1800]
    (matches the UI stepper); bad/blank values fall back to the default.
    skip_min is None unless the request enables skip_silence (None = feature off,
    original plan_cuts path); when on it's clamped to [2, 600], default 10.
    """
    acfg = stt.accounts_config(CONFIG_PATH)
    default_chunk = audio_split.default_chunk_secs(
        acfg["fresh_threshold"], stt.CREDITS_PER_SEC, acfg["selection_margin"])
    raw = body.get("chunk_secs")
    try:
        chunk = int(raw) if raw not in (None, "") else default_chunk
    except (TypeError, ValueError):
        chunk = default_chunk
    chunk = max(30, min(1800, chunk))

    def _num(key, default):
        try:
            return float(body[key])
        except (KeyError, TypeError, ValueError):
            return default

    skip_min = None
    if body.get("skip_silence"):
        skip_min = max(2.0, min(600.0, _num("skip_min", audio_split.SKIP_SILENCE_DEFAULT)))
    return chunk, _num("silence_db", audio_split.SILENCE_DB_DEFAULT), \
        _num("silence_min", audio_split.SILENCE_MIN_DEFAULT), skip_min


def _plan_one(uid: str, chunk_secs: int, silence_db: float, silence_min: float,
              skip_min: float | None = None) -> dict:
    """Compute one file's greedy split plan against the real ffmpeg silence detection.

    duration <= chunk_secs short-circuits to a single segment (no decode, same
    semantics as stt's _transcribe_split). Silence results are cached per
    (db, min) on the upload entry so re-planning after a chunk_secs change is free.
    """
    up = _UPLOADS.get(uid)
    if not up:
        return {"id": uid, "name": uid, "error": "上传已失效,请重新上传"}
    name, duration = up["name"], up.get("duration")
    base = {"id": uid, "name": name, "duration": duration}
    if duration is None:
        return {**base, "error": "无法确定音频时长"}
    if duration <= chunk_secs:
        return {**base, "segments": [[0.0, duration]], "hard": [False]}
    cache = up.setdefault("silences", {})
    key = (silence_db, silence_min)
    if key not in cache:
        try:
            cache[key] = audio_split.detect_silences(up["path"], silence_db, silence_min)
        except Exception as e:  # ffmpeg missing / decode failure → surface, don't 500
            return {**base, "error": repr(e)}
    if skip_min is not None:  # skip mode: same cached silences, different planner
        segs, hard = audio_split.plan_cuts_skip(duration, chunk_secs, cache[key], skip_min)
        if not segs:
            return {**base, "error": "音频内容全部为静音"}
        skipped = duration - sum(e - s for s, e in segs)
        return {**base, "segments": [[s, e] for s, e in segs], "hard": hard,
                "skipped": skipped if skipped > 1e-6 else 0.0}
    mids = [(s + e) / 2 for s, e in cache[key]]
    segs, hard = audio_split.plan_cuts(duration, chunk_secs, mids)
    return {**base, "segments": [[s, e] for s, e in segs], "hard": hard}


def do_split_plan(body: dict) -> dict:
    chunk, db, smin, skipm = _split_params(body)
    return {"files": [_plan_one(uid, chunk, db, smin, skip_min=skipm)
                      for uid in body.get("uploads", [])]}


def do_split_run(body: dict) -> dict:
    """Slice each upload into its planned segments under output_dir/<stem>-chunks/.

    output_dir is resolved relative to the project root (HERE), default 'out'.
    Per-file failures are recorded and skipped, matching the CLI's tolerance.
    """
    chunk, db, smin, skipm = _split_params(body)
    out_base = (body.get("output_dir") or "").strip() or "out"
    base_dir = HERE / out_base
    results = []
    for uid in body.get("uploads", []):
        up = _UPLOADS.get(uid)
        if not up:
            results.append({"name": uid, "ok": False, "error": "上传已失效"})
            continue
        name = up["name"]
        stem = pathlib.Path(name).stem
        rel_out = f"{out_base}/{stem}-chunks"
        plan = _plan_one(uid, chunk, db, smin, skip_min=skipm)
        if plan.get("error"):
            results.append({"name": name, "ok": False, "error": plan["error"],
                            "outDir": rel_out})
            continue
        segs = [(s, e) for s, e in plan["segments"]]
        try:
            chunks = audio_split.cut_segments(
                up["path"], segs, base_dir / f"{stem}-chunks",
                plan["hard"], stem=stem)
            results.append({"name": name, "ok": True, "segCount": len(chunks),
                            "outDir": rel_out})
        except Exception as e:  # ffmpeg failure on this file → skip, continue
            results.append({"name": name, "ok": False, "error": repr(e),
                            "outDir": rel_out})
    return {"results": results}


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
        if path == "/favicon.webp":
            body = FAVICON.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/webp")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/state":
            self._send_json(build_state())
            return
        if path == "/api/accounts/register/progress":
            self._send_json(_REG_PROGRESS)
            return
        if path == "/api/transcribe/progress":
            self._send_json(_TRANS_PROGRESS)
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
                body = self._read_json()
                self._send_json(compute_plan(body.get("files", []),
                                             body.get("allowedEmails") or None,
                                             bool(body.get("skipSilence"))))
                return
            if path == "/api/transcribe":
                body = self._read_json()
                self._send_json(do_transcribe(body.get("uploads", []),
                                              body.get("params", {}),
                                              bool(body.get("confirmRegister")),
                                              body.get("allowedEmails") or None))
                return
            if path == "/api/split/plan":
                self._send_json(do_split_plan(self._read_json()))
                return
            if path == "/api/split/run":
                self._send_json(do_split_run(self._read_json()))
                return
            if path == "/api/accounts/login":
                body = self._read_json()
                email = (body.get("email") or "").strip()
                password = body.get("password") or ""
                if not email or not password:
                    self._send_json({"error": "请输入邮箱和密码"})
                    return
                try:
                    self._send_json(do_login(email, password))
                except SystemExit as e:      # bad credentials / firebase rejection
                    self._send_json({"error": str(e)})
                return
            if path == "/api/config/save":
                body = self._read_json()
                self._send_json(do_save_config(body.get("temp_email", {}),
                                               body.get("pool_target")))
                return
            if path == "/api/accounts/register":
                target = self._read_json().get("target")
                target = int(target) if target else None
                try:
                    self._send_json(do_register(target))
                except SystemExit as e:      # temp_email/deps missing, etc.
                    self._send_json({"error": str(e)})
                return
            if path == "/api/accounts/export":
                self._send_json(do_export(self._read_json().get("emails", [])))
                return
            if path == "/api/accounts/import":
                body = self._read_json()
                accounts = body.get("accounts") if isinstance(body, dict) else body
                if not isinstance(accounts, list):
                    self._send_json({"error": "文件格式无效：应为账号数组或 {\"accounts\": [...]}"})
                    return
                self._send_json(do_import(accounts))
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

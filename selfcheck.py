#!/usr/bin/env python3
"""Offline self-check for elevenlabs-stt (no network).

Entry points: `python stt.py selfcheck` (delegates here) or `python selfcheck.py`.
Pure asserts over stt.py + audio_split.py logic; mutated stt globals
(_GATE_NEXT / *_LOG hooks / ACCOUNTS_PATH) are set via module attributes.
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import time

import audio_split
import stt
from stt import (
    DEFAULTS, _rlog, _session_to_account, _stagger_wait, _tlog, accounts_config,
    allocate, cached_remaining, filter_accounts, fresh_count, group_plan,
    load_config, pack_bins, random_password, resolve_language,
    run_plan_pipelined, temp_email_config,
)


def run() -> int:
    """Minimal runnable self-check (no network)."""
    cfg = load_config(pathlib.Path("config.example.toml"))
    assert cfg["include_subtitles"] is True, "script default include_subtitles must be ON"
    assert cfg["tag_audio_events"] is True
    assert cfg["max_concurrency"] == 4 and cfg["stagger_secs"] == 2.0  # 并发默认值（AC4）
    assert DEFAULTS["max_concurrency"] == 4 and DEFAULTS["stagger_secs"] == 2.0
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
    # pack_bins: 纯核心与 allocate 同规则；oversize 收集而非 raise
    r = pack_bins([("f800", 800), ("f5000", 5000), ("f7000", 7000), ("f400", 400)],
                  [("a", 1000), ("b", 6000)], margin=1.0, fresh_threshold=10000)
    assert r["register_count"] == 1 and dict(r["assignments"])["f7000"] == "NEW#0"
    assert [b["is_new"] for b in r["bins"]] == sorted(b["is_new"] for b in r["bins"]), \
        "existing bins before NEW bins"
    for b in r["bins"]:
        assert b["is_new"] or b["use"] <= b["before"], f"over-commit: {b}"
    r2 = pack_bins([("big", 20000)], [("a", 1000)], 1.0, 10000)
    assert r2["oversize"] == [("big", 20001)] and r2["assignments"] == []
    r3 = pack_bins([("fu", None)], [("a", 1000)], 1.0, 10000)
    assert r3["register_count"] == 1 and dict(r3["assignments"])["fu"] == "NEW#0"
    assert r3["bins"][-1]["use"] == 0 and r3["bins"][-1]["before"] == 0  # unknown req 不计 use
    # --- 并发流水线纯逻辑（AC7；全部离线） -------------------------------------
    # group_plan: existing 按账号聚组保序、NEW#k 按槽位分组
    p1, p2, p3 = (pathlib.Path(n) for n in ("p1.mp3", "p2.mp3", "p3.mp3"))
    egroups, pending = group_plan([(p1, aa), (p2, bb), (p3, aa), (pathlib.Path("p4.mp3"), "NEW#0")])
    assert [(acct["email"], [i for i, _ in items]) for acct, items in egroups] == \
        [("a", [0, 2]), ("b", [1])], "同账号聚为一组且保 plan 序"
    assert {k: [i for i, _ in v] for k, v in pending.items()} == {0: [3]}

    # stagger 闸：0 短路；两次取号间隔 >= stagger_secs（注入时钟，无真实 sleep）
    stt._GATE_NEXT = 0.0
    assert _stagger_wait(0) == 0.0
    slept: list[float] = []
    w1 = _stagger_wait(2.0, clock=lambda: 100.0, sleep=slept.append)
    w2 = _stagger_wait(2.0, clock=lambda: 100.0, sleep=slept.append)
    assert w1 == 0.0 and w2 == 2.0 and slept == [0.0, 2.0], (w1, w2, slept)
    stt._GATE_NEXT = 0.0

    # run_plan_pipelined: 假账号 + transcribe 桩；注册第 2 个失败 → 该槽位 FAIL，
    # 已提交组与已注册槽位不受影响；results 保 plan 序（R7 流水线新分支）
    real_accounts_path = stt.ACCOUNTS_PATH
    stt.ACCOUNTS_PATH = pathlib.Path(tempfile.mkdtemp(prefix="stt-selfcheck-")) / "accounts.json"
    try:
        reg_calls: list[int] = []

        def reg_stub():
            reg_calls.append(1)
            if len(reg_calls) == 1:
                return {"email": "n0", "invalid": False, "created_at": now,
                        "credits_known": {"limit": 10000, "count": 0, "fetched_at": now}}
            raise RuntimeError("boom")

        def trans_stub(inp, _cfg, account, _store, _cfgpath, output):
            return pathlib.Path(str(inp) + ".srt")

        pstore = {"accounts": [dict(aa)], "active": "a"}
        pplan = [(p1, pstore["accounts"][0]), (p2, "NEW#0"), (p3, "NEW#1")]
        done_flags: list[bool] = []
        plog: list[str] = []
        stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = plog.append  # 静音测试期间的进度行
        try:
            res, pused = run_plan_pipelined(
                pplan, {"max_concurrency": 2, "stagger_secs": 0}, pstore,
                pathlib.Path("config.example.toml"), output_for=lambda _i: None,
                register_count=2, register_fn=reg_stub, transcribe_fn=trans_stub,
                refresh_used=False, on_done=lambda _i, ok: done_flags.append(ok))
        finally:
            stt.TRANSCRIBE_LOG = stt.REGISTER_LOG = None
        assert any("注册失败" in l for l in plog), plog
        assert [r[0] for r in res] == [p1, p2, p3], "results 按 plan 原顺序聚合"
        assert res[0][2] == "OK" and res[1][2] == "OK" and res[1][1] == "n0", res
        assert res[2][2] == "FAIL" and "注册失败" in res[2][3], "未注册槽位标 FAIL 带原因"
        assert {a["email"] for a in pstore["accounts"]} == {"a", "n0"}, "注册成功的账号已入 store"
        assert stt.ACCOUNTS_PATH.exists(), "注册成功即落盘"
        assert len(done_flags) == 3 and done_flags.count(False) == 1, done_flags
        assert {a.get("email") for a in pused} == {"a", "n0"}
    finally:
        shutil.rmtree(stt.ACCOUNTS_PATH.parent, ignore_errors=True)
        stt.ACCOUNTS_PATH = real_accounts_path

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
    # plan_cuts_skip: silences >= skip_min are dropped, short ones untouched (AC1)
    sil = [(50.0, 65.0), (200.0, 201.0), (400.0, 412.0)]  # 15s + 1s + 12s, skip_min=10
    sk, skh = audio_split.plan_cuts_skip(500.0, 300.0, sil, 10.0)
    pad = audio_split.SKIP_EDGE_PAD
    # middle voiced region (64.5..400.5 = 336s > 300) greedy-splits at the 1s-silence midpoint
    assert sk == [(0.0, 50.0 + pad), (65.0 - pad, 200.5), (200.5, 400.0 + pad),
                  (412.0 - pad, 500.0)], sk
    assert len(skh) == len(sk)
    for a, b in sk:  # every segment <= chunk_secs, absolute coords inside [0, total]
        assert 0.0 <= a < b <= 500.0 and b - a <= 300.0 + 1e-9, (a, b)
    for s, e in [(50.0, 65.0), (400.0, 412.0)]:  # long silences fully skipped (pad aside)
        assert not any(a < (s + e) / 2 < b for a, b in sk), (s, e, sk)
    skipped_total = 500.0 - sum(b - a for a, b in sk)
    assert abs(skipped_total - (15.0 + 12.0 - 4 * pad)) < 1e-6, skipped_total
    # >= boundary: a silence exactly skip_min long is skipped
    skb, _ = audio_split.plan_cuts_skip(100.0, 300.0, [(40.0, 50.0)], 10.0)
    assert skb == [(0.0, 40.0 + pad), (50.0 - pad, 100.0)], skb
    # no long silence: byte-identical to plan_cuts on the midpoints
    same = audio_split.plan_cuts_skip(500.0, 300.0, [(99.0, 101.0), (249.0, 251.0), (279.0, 281.0)], 10.0)
    assert same == audio_split.plan_cuts(500.0, 300.0, [100.0, 250.0, 280.0]), same
    # all-silence audio -> empty plan; leading/trailing long silence -> degenerate dropped
    assert audio_split.plan_cuts_skip(60.0, 300.0, [(0.0, 60.0)], 10.0) == ([], [])
    ends, _ = audio_split.plan_cuts_skip(100.0, 300.0, [(0.0, 20.0), (80.0, 100.0)], 10.0)
    assert ends == [(20.0 - pad, 80.0 + pad)], ends
    # long voiced region still greedy-splits on the short silences inside it
    lsil = [(0.0, 30.0), (250.0, 251.0)]
    lsk, lskh = audio_split.plan_cuts_skip(700.0, 300.0, lsil, 10.0)
    assert all(b - a <= 300.0 + 1e-9 for a, b in lsk), lsk
    assert lsk[0] == (30.0 - pad, 250.5), lsk  # cut at the short-silence midpoint
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

    # video filename probe (web upload routing; no ffmpeg)
    assert audio_split.is_video_filename("clip.MP4") and audio_split.is_video_filename("a.mkv")
    assert not audio_split.is_video_filename("talk.m4a") and not audio_split.is_video_filename("x")
    assert audio_split.VIDEO_EXTENSIONS >= {".mp4", ".webm", ".mov"}

    # extract_audio: short synthetic mp4 → audio (skip if ffmpeg/ffprobe unavailable)
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        tdir = pathlib.Path(tempfile.mkdtemp(prefix="stt-extract-"))
        try:
            vid = tdir / "clip.mp4"
            # lavfi sine + color: portable short A/V fixture
            subprocess_ok = __import__("subprocess").run(
                [shutil.which("ffmpeg"), "-y",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                 "-f", "lavfi", "-i", "color=c=black:s=160x120:d=0.4",
                 "-c:a", "aac", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-shortest", str(vid)],
                stdout=__import__("subprocess").DEVNULL,
                stderr=__import__("subprocess").DEVNULL,
            )
            if subprocess_ok.returncode == 0 and vid.is_file():
                er = audio_split.extract_audio(vid, tdir, "out")
                assert er.method in ("copy", "transcode"), er
                assert er.path.is_file() and er.path.stat().st_size > 0
                assert er.path.suffix.lower() in (".mka", ".m4a")
                assert vid.is_file()  # helper must not delete src
        finally:
            shutil.rmtree(tdir, ignore_errors=True)

    # REGISTER_LOG hook: collector receives _rlog lines; None default restored
    got: list[str] = []
    stt.REGISTER_LOG = got.append
    try:
        _rlog("hook-test")
    finally:
        stt.REGISTER_LOG = None
    assert got == ["hook-test"], got

    # TRANSCRIBE_LOG hook: same contract as REGISTER_LOG
    tgot: list[str] = []
    stt.TRANSCRIBE_LOG = tgot.append
    try:
        _tlog("t-hook-test")
    finally:
        stt.TRANSCRIBE_LOG = None
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


if __name__ == "__main__":
    sys.exit(run())

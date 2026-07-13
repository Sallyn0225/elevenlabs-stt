#!/usr/bin/env python3
"""Silence-aware long-audio splitting + subtitle merging for the STT CLI.

Pure logic (chunk sizing, greedy cut planning, timestamp parse/format, srt/vtt/txt
merge) is decoupled from the network/account layer so it is offline-unit-testable
(see selfcheck.py). ffmpeg is only touched by detect_silences/cut_segments/
extract_audio.

Design: .trellis/tasks/07-02-audio-silence-split/design.md
        .trellis/tasks/07-13-webui-video-audio-extract/design.md
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

# ponytail: tuned so a chunk always fits one fresh free account *including* the
# allocate() selection margin. default_chunk_secs() derives the number; safety
# shaves a further 5% off the theoretical ceiling to absorb credit-rate drift.
SPLIT_SAFETY = 0.95
SILENCE_DB_DEFAULT = -30.0   # ffmpeg silencedetect noise floor (dB)
SILENCE_MIN_DEFAULT = 0.5    # ffmpeg silencedetect minimum silence duration (s)
MIN_SEG = 5.0                # reject cut candidates < this after a segment start
SKIP_SILENCE_DEFAULT = 10.0  # silences >= this (s) are skipped when skip mode is on
SKIP_EDGE_PAD = 0.5          # silence buffer kept on each side of a voiced region (s)

# WebUI video accept / server-side extract: extension-based (no MIME trust).
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
    ".mpeg", ".mpg", ".ts", ".flv", ".wmv", ".3gp",
})


@dataclass
class Chunk:
    """One cut segment: its ordinal, the sliced audio file, and its start offset (s)."""
    index: int
    path: pathlib.Path
    start: float


@dataclass
class ExtractResult:
    """Result of extract_audio: output path and which strategy succeeded."""
    path: pathlib.Path
    method: str  # "copy" | "transcode"


def is_video_filename(name: str) -> bool:
    """True when the filename suffix is a known video container (case-insensitive)."""
    return pathlib.Path(name).suffix.lower() in VIDEO_EXTENSIONS


# ------------------------------------------------------------ chunk sizing

def default_chunk_secs(fresh_threshold: int, credits_per_sec: float,
                       margin: float, safety: float = SPLIT_SAFETY) -> int:
    """Largest chunk (seconds) that a single fresh account can transcribe within margin."""
    return int(fresh_threshold / (credits_per_sec * margin) * safety)


# --------------------------------------------------------- timestamp tools

def _parse_hms(ts: str) -> float:
    """Parse [HH:]MM:SS.mmm (dot-separated) into seconds."""
    parts = [float(p) for p in ts.strip().split(":")]
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = 0.0, parts[0], parts[1]
    else:
        h, m, s = 0.0, 0.0, parts[0]
    return h * 3600 + m * 60 + s


def _fmt_hms(t: float, sep: str) -> str:
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def parse_ts_srt(ts: str) -> float:
    """SRT timestamp 'HH:MM:SS,mmm' -> seconds."""
    return _parse_hms(ts.replace(",", "."))


def fmt_ts_srt(t: float) -> str:
    """seconds -> SRT timestamp 'HH:MM:SS,mmm'."""
    return _fmt_hms(t, ",")


def parse_ts_vtt(ts: str) -> float:
    """VTT timestamp 'HH:MM:SS.mmm' -> seconds."""
    return _parse_hms(ts)


def fmt_ts_vtt(t: float) -> str:
    """seconds -> VTT timestamp 'HH:MM:SS.mmm'."""
    return _fmt_hms(t, ".")


# ------------------------------------------------------- greedy cut planner

def plan_cuts(total_secs: float, chunk_secs: float,
              silence_mids: list[float]) -> tuple[list[tuple[float, float]], list[bool]]:
    """Greedy split into <= chunk_secs segments, cutting at the latest silence midpoint.

    Pure function. In window (start, start+chunk_secs] pick the latest silence
    midpoint > start+MIN_SEG; if none exists, hard-cut at start+chunk_secs.
    Returns (segments, hard_flags) where hard_flags[i] marks a no-silence hard cut.
    """
    cuts = sorted(silence_mids)
    segments: list[tuple[float, float]] = []
    hard_flags: list[bool] = []
    start = 0.0
    while total_secs - start > chunk_secs:
        window_end = start + chunk_secs
        cands = [c for c in cuts if start + MIN_SEG < c <= window_end]
        if cands:
            cut = max(cands)
            hard = False
        else:
            cut = window_end
            hard = True
        segments.append((start, cut))
        hard_flags.append(hard)
        start = cut
    segments.append((start, total_secs))
    hard_flags.append(False)
    return segments, hard_flags


def plan_cuts_skip(total_secs: float, chunk_secs: float,
                   silences: list[tuple[float, float]],
                   skip_min: float) -> tuple[list[tuple[float, float]], list[bool]]:
    """plan_cuts variant that drops silences >= skip_min entirely (not uploaded).

    `silences` are raw detect_silences (start, end) intervals. Returns the same
    contract as plan_cuts: (segments, hard_flags) in absolute audio time — segments
    are no longer contiguous, but cut_segments/merge work per-segment so nothing
    downstream changes. All-silence audio returns ([], []).
    """
    # skip_min below 2*pad would let adjacent padded regions overlap (duplicated
    # audio in the output); clamp here so every caller (CLI has no arg clamp) is safe
    skip_min = max(skip_min, 2 * SKIP_EDGE_PAD)
    long_sil = sorted((s, e) for s, e in silences if e - s >= skip_min)
    if not long_sil:  # nothing to skip: byte-identical to the plain path
        return plan_cuts(total_secs, chunk_secs, [(s + e) / 2 for s, e in silences])
    # complement of the long silences = voiced regions; drop degenerate ones
    # (audio starting/ending in a long silence), then pad survivors so -c copy
    # keyframe drift can't eat word edges (SKIP_EDGE_PAD << skip_min, no overlap).
    voiced: list[tuple[float, float]] = []
    pos = 0.0
    for s, e in long_sil + [(total_secs, total_secs)]:
        if s - pos > 0:
            voiced.append((max(0.0, pos - SKIP_EDGE_PAD), min(total_secs, s + SKIP_EDGE_PAD)))
        pos = max(pos, e)
    segments: list[tuple[float, float]] = []
    hard_flags: list[bool] = []
    for a, b in voiced:
        # all silence midpoints inside the region (short ones too) feed the greedy core
        rel_mids = [(s + e) / 2 - a for s, e in silences if a < (s + e) / 2 < b]
        segs, hard = plan_cuts(b - a, chunk_secs, rel_mids)
        segments.extend((a + s, a + e) for s, e in segs)
        hard_flags.extend(hard)
    return segments, hard_flags


# ------------------------------------------------------------------ ffmpeg

def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"{tool} not found on PATH")
    return path


def extract_audio(src: pathlib.Path, dest_dir: pathlib.Path,
                  stem: str) -> ExtractResult:
    """Extract the first audio stream from a media file via system ffmpeg.

    Strategy (design D3): try stream copy into Matroska audio (.mka) for wide
    codec compatibility; on failure re-encode to AAC in .m4a. Does not delete
    `src` — the caller owns lifecycle (web deletes the video after success).

    Raises RuntimeError when ffmpeg is missing, there is no audio stream, or
    both strategies fail.
    """
    ffmpeg = _require("ffmpeg")
    dest_dir = pathlib.Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", stem or "audio", flags=re.UNICODE).strip("._") or "audio"
    copy_out = dest_dir / f"{safe}.mka"
    trans_out = dest_dir / f"{safe}.m4a"
    for stale in (copy_out, trans_out):
        if stale.exists():
            stale.unlink()

    def _ok(path: pathlib.Path) -> bool:
        return path.is_file() and path.stat().st_size > 0

    def _no_audio(stderr: str) -> bool:
        low = stderr.lower()
        return (
            "matches no streams" in low
            or "does not contain any stream" in low
            or "stream map '0:a:0'" in low
            or "output file does not contain any stream" in low
        )

    copy_proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "copy",
         str(copy_out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    copy_err = (copy_proc.stderr or b"").decode("utf-8", "replace")
    if copy_proc.returncode == 0 and _ok(copy_out):
        return ExtractResult(copy_out, "copy")
    if copy_out.exists():
        copy_out.unlink()

    if _no_audio(copy_err):
        raise RuntimeError("no audio stream")

    trans_proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-vn", "-map", "0:a:0",
         "-c:a", "aac", "-b:a", "192k", str(trans_out)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    trans_err = (trans_proc.stderr or b"").decode("utf-8", "replace")
    if trans_proc.returncode == 0 and _ok(trans_out):
        return ExtractResult(trans_out, "transcode")
    if trans_out.exists():
        trans_out.unlink()

    if _no_audio(trans_err) or _no_audio(copy_err):
        raise RuntimeError("no audio stream")
    tail = [ln for ln in (trans_err or copy_err).strip().splitlines() if ln.strip()][-3:]
    raise RuntimeError("ffmpeg extract failed: " + " | ".join(tail)[:300])


def detect_silences(path: pathlib.Path, noise_db: float,
                    min_silence: float) -> list[tuple[float, float]]:
    """Run ffmpeg silencedetect and parse (start, end) silence intervals from stderr."""
    ffmpeg = _require("ffmpeg")
    proc = subprocess.run(
        [ffmpeg, "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    stderr = proc.stderr.decode("utf-8", "replace")
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for m in re.finditer(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)", stderr):
        kind, val = m.group(1), float(m.group(2))
        if kind == "start":
            start = val
        elif start is not None:
            silences.append((start, val))
            start = None
    return silences


def cut_segments(path: pathlib.Path, segments: list[tuple[float, float]],
                 workdir: pathlib.Path,
                 hard_flags: list[bool] | None = None,
                 stem: str | None = None) -> list[Chunk]:
    """Losslessly slice `path` into segments via ffmpeg -c copy. Returns Chunk list.

    tradeoff: -c copy is fast and lossless but can only cut on keyframes; since cuts
    land inside detected silence (and audio keyframes are dense) the drift is inaudible.

    `stem` overrides the output filename stem (part files are `<stem>.partNN<ext>`);
    defaults to `path.stem`. The web layer passes the original name here because its
    uploads are stored under a uuid-prefixed temp filename.
    """
    ffmpeg = _require("ffmpeg")
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stem, suffix = (stem or path.stem), path.suffix
    # 清残留:重切段数变少时,旧的高序号 partNN 会冒充新产物。用 regex 而非 glob,
    # 否则 stem 里的 [ ] 等 glob 元字符会漏删/误删别的文件。
    stale = re.compile(re.escape(stem) + r"\.part\d{2}" + re.escape(suffix))
    for old in workdir.iterdir():
        if stale.fullmatch(old.name):
            old.unlink()
    chunks: list[Chunk] = []
    for i, (start, end) in enumerate(segments):
        out = workdir / f"{stem}.part{i:02d}{suffix}"
        # -ss/-to as input options: fast input seek, absolute timestamps (see design).
        subprocess.run(
            [ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
             "-i", str(path), "-c", "copy", str(out)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if hard_flags and i < len(hard_flags) and hard_flags[i]:
            print(f"warn: {out.name} hard-cut at {end:.1f}s (no silence in window); "
                  f"a word may be split", file=sys.stderr)
        chunks.append(Chunk(i, out, start))
    return chunks


# -------------------------------------------------------------- merge cues

def _parse_cues(text: str, parse_ts) -> list[tuple[float, float, str]]:
    """Parse an srt/vtt body into (start, end, text) cues; non-cue blocks are skipped."""
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        ti = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ti is None:  # WEBVTT header / NOTE / STYLE blocks have no timing line
            continue
        left, right = lines[ti].split("-->", 1)
        start = parse_ts(left.strip())
        end = parse_ts(right.strip().split()[0])  # drop any trailing cue settings
        body = "\n".join(lines[ti + 1:])
        cues.append((start, end, body))
    return cues


def _merge_cues(chunks: list[tuple[float, str]], parse_ts, fmt_ts,
                header: str | None) -> str:
    """Offset each chunk's cues, sort by start, renumber from 1, render."""
    cues: list[tuple[float, float, str]] = []
    for offset, text in chunks:
        for start, end, body in _parse_cues(text, parse_ts):
            cues.append((start + offset, end + offset, body))
    cues.sort(key=lambda c: c[0])
    lines: list[str] = []
    if header:
        lines += [header, ""]
    for i, (start, end, body) in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{fmt_ts(start)} --> {fmt_ts(end)}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def merge_srt(chunks: list[tuple[float, str]]) -> str:
    """Merge per-chunk SRT texts (each with a start offset) into one renumbered SRT."""
    return _merge_cues(chunks, parse_ts_srt, fmt_ts_srt, header=None)


def merge_vtt(chunks: list[tuple[float, str]]) -> str:
    """Merge per-chunk VTT texts into one VTT with a single WEBVTT header."""
    return _merge_cues(chunks, parse_ts_vtt, fmt_ts_vtt, header="WEBVTT")


def merge_txt(chunks: list[tuple[float, str]]) -> str:
    """Concatenate per-chunk plain-text transcripts in segment order (blank-line joined)."""
    parts = [text.strip() for _, text in chunks]
    return "\n\n".join(p for p in parts if p) + "\n"

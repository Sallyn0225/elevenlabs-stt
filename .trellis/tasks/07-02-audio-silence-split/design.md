# Design: 按静音智能切分长音频

## 架构与边界
新增后端模块 **`audio_split.py`**(纯逻辑,不做网络/账号),被 `stt.py` 的 `cmd_transcribe` 在 `--split` 分支调用。
`stt.py` 只负责:解析开关 → 调 `audio_split` 切分 → 复用现有 allocate/transcribe 逐段转录 → 调 `audio_split` 合并 → 清理。

`audio_split.py` 对外接口(全部可离线单测的纯函数 + 少量 ffmpeg 包装):
- `default_chunk_secs(fresh_threshold, credits_per_sec, margin, safety=SPLIT_SAFETY) -> int`
  返回 `int(fresh_threshold / (credits_per_sec * margin) * safety)`。`SPLIT_SAFETY = 0.95`。
- `detect_silences(path, noise_db, min_silence) -> list[tuple[float, float]]`(ffmpeg silencedetect,解析 stderr)。
- `plan_cuts(total_secs, chunk_secs, silence_mids) -> list[tuple[float, float]]`
  **纯函数**:输入总时长、片段上限、静音中点列表 → 返回 `[(start, end), ...]` 段列表。贪心算法见下。
- `cut_segments(path, segments, workdir) -> list[Chunk]`(ffmpeg 切割;`Chunk` = {index, path, start})。
- `merge_srt(chunks) -> str` / `merge_vtt(chunks) -> str` / `merge_txt(chunks) -> str`
  输入 `[(start_offset, text)]`(按段序),返回合并后的完整文本。
- 时间戳工具:`parse_ts_srt`/`fmt_ts_srt`(`HH:MM:SS,mmm`)、`parse_ts_vtt`/`fmt_ts_vtt`(`HH:MM:SS.mmm`)。

## 数据流
```
cmd_transcribe(--split)
  ├─ 校验 format ∈ {srt,vtt,txt}          (R7: 否则 SystemExit)
  ├─ for each 输入音频:
  │    total = audio_duration()
  │    if total <= chunk_secs: 作为单段(不切),否则:
  │      mids   = [ (s+e)/2 for (s,e) in detect_silences() ]
  │      segs   = plan_cuts(total, chunk_secs, mids)          (R4)
  │      chunks = cut_segments(path, segs, workdir)           (R9 临时目录)
  │    记录 parent→[chunk...],每个 chunk 带 start offset
  ├─ 把所有 chunk 文件喂给 allocate() → 注册补齐 → 逐个 transcribe_one(export=fmt)  (R5)
  │    每个 chunk 导出为 fmt 到 chunk 输出路径
  ├─ for each parent:
  │    if 所有 chunk OK: merge_<fmt>(按 start 排序) → 写 <stem>.<fmt>;清理临时(除非 --keep-chunks)  (R6)
  │    else:            不写合并;保留成功片段输出 + 片段文件;summary 标失败段                       (R8)
  └─ print_summary(含每 parent 的合并/失败状态)
```

## 贪心切点算法 (plan_cuts)
```
segs=[]; start=0
cuts = sorted(m for m in silence_mids)
while total - start > chunk_secs:
    window_end = start + chunk_secs
    cands = [c for c in cuts if start + MIN_SEG < c <= window_end]   # MIN_SEG 防零/极短段
    cut = max(cands) if cands else window_end                         # 无候选→硬切(调用方 stderr 告警)
    segs.append((start, cut)); start = cut
segs.append((start, total))
return segs
```
- 在静音中点切,静音被两段各分一半,不丢语音,无需 overlap。
- `MIN_SEG`(如 5s)避免把开头/紧邻静音选成切点导致极短段。
- 硬切告警由 `cut_segments`/调用方检测 `(end-start)==chunk_secs` 且非静音时输出。

## 片段切割 (cut_segments)
- `ffmpeg -y -ss {start} -to {end} -i {input} -c copy {workdir}/{stem}.part{NN}.{ext}`
  - `-c copy` 无损、快;切点在静音处,关键帧漂移落在静音内,可接受(音频关键帧密集)。tradeoff 记于代码注释。
- `ext` 沿用输入扩展名(容器不变,STT 接受)。
- `workdir` 默认 `out/<stem>-chunks/`(与现有 `out/` 惯例一致);合并成功后 `shutil.rmtree`,`--keep-chunks` 跳过。

## 合并契约
- **SRT**:逐 cue 解析 `idx / start --> end / text...`;每段所有时间戳 `+= offset`;跨段汇总后按 start 升序、重排 idx 从 1 连续。
- **VTT**:保留单个 `WEBVTT` 头;cue 时间戳 `+= offset`;拼接(可保留原 cue 设置串)。
- **TXT**:各段文本按段序 `"\n".join`(段间空行),无时间戳。
- offset 来源:片段 `start`(秒),秒→时间戳用格式化工具。
- 每个 chunk 的转录导出文本从其 chunk 输出文件读回(`transcribe_one` 已写盘)。

## 复用与兼容
- 复用 `audio_duration`、`estimate_required`、`allocate`、`register_one`、`transcribe_one`、`account_remaining`、`refill_pool`、`print_summary`。
- `allocate` 已支持"多个文件分配到多账号/开虚拟 fresh bin 注册补齐",片段即普通文件,无需改分配逻辑。
- 不改现有非 `--split` 路径行为;`--split` 为新增分支。
- 常量复用 `CREDITS_PER_SEC`、`accounts_config` 的 `fresh_threshold`/`selection_margin`;新增 `SPLIT_SAFETY`、`SILENCE_DB_DEFAULT=-30`、`SILENCE_MIN_DEFAULT=0.5`、`MIN_SEG` 于 `audio_split.py`。

## 失败与回滚
- 切分/ffmpeg 失败:该输入跳过并计 FAIL,不影响其它输入(沿用 skip & continue)。
- 转录中途失败:见 R8。
- 临时片段仅在合并成功后清理,失败保留,便于用户手动重跑合并。
- 回滚点:改动集中在新文件 `audio_split.py` + `stt.py` 的 `cmd_transcribe`/parser;删 `--split` 分支与 import 即回到原状。

## 单测 (selfcheck 扩展,离线)
- `plan_cuts`:给定 total/chunk_secs/mids,断言段数、每段 ≤ chunk_secs、切点取窗口内最靠后候选、无候选硬切。
- `merge_srt`/`merge_vtt`:两段带 offset 合并后时间戳单调、序号连续、头唯一。
- `merge_txt`:段序拼接。
- 时间戳 `parse/fmt` 往返一致。

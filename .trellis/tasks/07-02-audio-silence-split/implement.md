# Implement: 按静音智能切分长音频

## 顺序清单
1. **新建 `audio_split.py` 纯逻辑层**(不引入网络/账号依赖):
   - 常量 `SPLIT_SAFETY=0.95`、`SILENCE_DB_DEFAULT=-30`、`SILENCE_MIN_DEFAULT=0.5`、`MIN_SEG=5.0`。
   - `default_chunk_secs(fresh_threshold, credits_per_sec, margin, safety=SPLIT_SAFETY)`。
   - 时间戳工具 `parse_ts_srt/fmt_ts_srt`(逗号毫秒)、`parse_ts_vtt/fmt_ts_vtt`(点毫秒)。
   - `plan_cuts(total, chunk_secs, silence_mids)`(design 贪心算法;返回段列表 + 标记哪些是硬切)。
   - `merge_srt/merge_vtt/merge_txt(chunks)`,`chunks=[(offset, text)]`。
2. **ffmpeg 包装(仍在 `audio_split.py`)**:
   - `detect_silences(path, noise_db, min_silence)`:跑 `ffmpeg -i path -af silencedetect=noise={db}dB:d={min} -f null -`,解析 stderr 的 `silence_start`/`silence_end`。ffmpeg 缺失时抛清晰错误。
   - `cut_segments(path, segments, workdir)`:`ffmpeg -y -ss -to -i -c copy`;返回 `Chunk(index,path,start)`;硬切段 stderr 告警。
3. **接线 `stt.py`**:
   - import `audio_split`;`transcribe` 子解析器加 `--split`、`--chunk-secs`(int)、`--keep-chunks`(store_true)、`--silence-db`(float)、`--silence-min`(float)。
   - `cmd_transcribe` 新增 `--split` 分支(见 design 数据流):校验 format∈{srt,vtt,txt}(R7);逐输入切分;把片段汇入 costs/allocate;逐段 `transcribe_one` 导出 fmt;按 parent 合并或按 R8 失败处理;清理临时(R9)。
   - `--dry-run` 下打印切分计划 + allocate 计划,不上传(AC1)。
   - 保持非 `--split` 路径不变。
4. **扩展 `_selfcheck()`**:加 `plan_cuts`/`merge_srt`/`merge_vtt`/`merge_txt`/时间戳往返 的离线断言(AC7)。
5. **文档**:README(中/英)与 `config.example.toml`(如需)补 `--split` 用法;非本步硬性门槛,收尾统一做。

## 验证命令
- 离线单测:`python stt.py selfcheck`(必须含新断言且通过)。
- 计划预览:`python stt.py transcribe <long.m4a> --split --dry-run`(AC1)。
- 端到端(需账号/额度):`python stt.py transcribe <long.m4a> --split --format srt`,核对单份合并 srt 时间戳连续(AC3)。
- 负路径:`python stt.py transcribe x.m4a --split --format pdf` 应报错退出(AC4)。
- 语法/类型:`python -c "import audio_split, stt"`。

## 风险文件 / 回滚点
- `stt.py::cmd_transcribe`(高风险:勿破坏现有多文件/allocate 路径)——改动限制在新增 `--split` 分支与 parser 新参数。
- 新文件 `audio_split.py`(低风险,可独立删除)。
- 回滚:移除 `--split` 分支、parser 新参数、`import audio_split`,删 `audio_split.py`。

## 复查门槛(start 前确认)
- [ ] 片段上限 ≤ 599s 语义(能被 fresh 账号含 margin 装下)已在 `default_chunk_secs` 体现。
- [ ] 合并仅 srt/vtt/txt,其余报错。
- [ ] 失败不产合并、保留片段、报告(R8)。
- [ ] selfcheck 覆盖纯函数且离线。

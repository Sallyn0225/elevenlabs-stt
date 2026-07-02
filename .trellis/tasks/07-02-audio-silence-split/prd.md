# PRD: 按静音智能切分长音频

## Goal / User Value
超长音频(如 1 小时)超出单个免费 ElevenLabs 账号的转录额度(约 10 分钟/账号)。
为 CLI 增加 `transcribe --split` 全流程:在音频静音处智能切分,把长音频切成每段都在单账号配额内的片段,
逐段转录(复用现有多账号分配),再按时间偏移合并回一份完整 srt/vtt/txt。切点落在静音处,避免把词切断。

## Background / Confirmed Facts (from codebase)
- `stt.py` 是 CLI 入口;已有 `audio_duration()`(ffprobe)、`estimate_required()`、多账号 `allocate()` 分配、
  `transcribe_one()`、`transcribe`/`accounts`/`pool` 子命令。
- 单免费账号在 `allocate()` margin 内的可用时长 ≈ `fresh_threshold(10000) / (CREDITS_PER_SEC 13.9 × selection_margin 1.2)`
  ≈ 599 秒;对应现有常量 `FREE_DURATION_WARN_SECS = 600`。片段目标时长必须 ≤ 该值,片段才能被现有 fresh 账号(剩余 10000)装下。
- 导出格式:srt / vtt / txt / json / html / pdf / docx(`export_task()`)。本任务仅合并 srt/vtt/txt。
- 现有多文件批处理:把多个文件用 `allocate()` 分配到不同账号并逐个 `transcribe_one()`,产出各自独立输出文件,无合并。
- 切分产物(多个片段文件)天然可复用现有 allocate + 逐文件转录流程。
- 平台:Windows;已依赖 ffmpeg/ffprobe(ffprobe 已用于时长探测)。导出接口契约见 `docs/api-contract.md`。

## Approach (选定方案)
ffmpeg `silencedetect` 检测静音区间 → 贪心选点(在片段时长上限内选**最靠后**的静音中点切分)→
ffmpeg `-ss/-to -c copy` 切出片段 → 逐段转录 → 按每段起始 offset 合并 srt/vtt/txt。
区间内无静音时在上限处硬切兜底并告警。

## Requirements
- R1 新增后端模块(如 `audio_split.py`),对外提供:静音检测 + 贪心切点、片段切割、srt/vtt/txt 合并三组能力,逻辑与 CLI 解耦、可离线单测。
- R2 `transcribe` 子命令新增 `--split` 开关;开启后对超过片段上限的输入自动切分、逐段转录、合并为一份 `<stem>.<fmt>`。
- R3 片段目标时长:默认由配置推导 `int(fresh_threshold / (CREDITS_PER_SEC × selection_margin) × SPLIT_SAFETY)`(默认约 569s),
  可用 `--chunk-secs` 覆盖;保证单片段能被一个 fresh 账号(含 allocate margin)装下。
- R4 切点选择:静音区间中点为候选;窗口 `(start, start+chunk_secs]` 内选最靠后候选;无候选则在 `start+chunk_secs` 硬切并 stderr 告警。
- R5 片段转录复用现有 `allocate()` + 注册 + `transcribe_one()` 流程(N 段视作 N 个待分配文件),不新造账号分配逻辑。
- R6 合并:srt/vtt 按每段起始 offset 校正时间戳并重排序号/去重复头;txt 按段序拼接。仅支持 srt/vtt/txt。
- R7 `--split` 搭配 html/pdf/docx/json 时直接 `SystemExit` 报错说明不支持合并。
- R8 失败处理:任一片段转录失败则**不生成**该输入的合并文件,保留已成功片段的转录输出与已切片段文件,并在 summary 报告哪一段失败。
- R9 临时片段文件写入工作目录(如 `out/<stem>-chunks/` 或临时目录);合并成功后清理,`--keep-chunks` 保留;失败时保留供重跑。
- R10 静音参数默认 `noise=-30dB`、`d=0.5s`,分别可用 `--silence-db`、`--silence-min` 覆盖。
- R11 `-o/--output` 仅在单输入时可用(沿用现有约束)。

## Acceptance Criteria
- [ ] AC1 `python stt.py transcribe <long.m4a> --split --dry-run` 打印切分计划(段数、每段 start/end、目标账号分配),不上传。
- [ ] AC2 对一段 > `chunk_secs` 的音频,产出的片段数 = ceil,且每段时长 ≤ `chunk_secs`,切点落在检测到的静音中点(无静音窗口除外)。
- [ ] AC3 全流程成功后仅得到一份合并 `<stem>.srt`(或 vtt/txt),时间戳连续、单调递增、序号从 1 连续重排;各段衔接处时间偏移正确(= 该段 start)。
- [ ] AC4 `--split --format pdf`(或 docx/html/json)立即报错退出,提示仅支持 srt/vtt/txt。
- [ ] AC5 任一片段失败时:不产出合并文件;已成功片段的输出与片段文件保留;summary 标明失败段;退出码非 0。
- [ ] AC6 合并成功后默认清理临时片段;`--keep-chunks` 时保留。
- [ ] AC7 `python stt.py selfcheck` 覆盖纯函数:贪心选点(给定静音候选列表)、srt/vtt 时间戳 offset 校正与重排、txt 拼接;全部离线通过。

## Out of Scope
- WebUI 集成(后续任务)。
- json/html/pdf/docx 的合并。
- 失败片段的自动重试(本次采用"不合并 + 保留 + 报告",用户手动重跑)。

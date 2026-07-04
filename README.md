# elevenlabs-stt

[English](./README.en.md) | **简体中文**

> 将 ElevenLabs 网页版「语音转文本」功能转为命令行脚本，使用你自己的登录账号，复用免费额度。

> [!IMPORTANT]
> 本项目非 ElevenLabs 官方，也未与之关联。它通过复用网页应用的内部 API 实现，该接口可能随时变更。请使用你自己的账号，并遵守 ElevenLabs 的服务条款。

## 它做什么

把 `elevenlabs.io/app/speech-to-text` 的网页操作流程（登录 → 上传音频 → 选语言/开关 → 等待转录 → 导出字幕）变成一条命令。脚本复用网页内部 API（而非官方付费 API），因此继承免费账号的积分额度（约 1 万积分 ≈ 12 分钟音频/月）。

## 功能

- 上传本地音频文件转文本（网页「上传」路径）
- 四个开关：标记音频事件 / 包含字幕 / 无逐字记录 / 从声音库分配声音
- 高频专有词汇增强识别（关键术语）
- 主语言选择或自动检测（ISO 639-3）
- 导出 SRT / VTT / TXT / JSON / HTML / PDF / DOCX，默认 SRT
- 上传前校验：文件大小（1000MB 硬限制）、时长（ffprobe 可选，10 分钟软提醒）
- 一次性浏览器登录，自动续签 1 小时令牌
- 可选多账号额度池：自动注册临时邮箱账号、按剩余额度选择账号、转录后自动补池

## 安装

```bash
pip install -r requirements.txt
playwright install chrome   # 仅登录步骤需要
```

可选：安装 `ffprobe`（随 ffmpeg）以启用音频时长预检。

## 快速开始

```bash
# 1) 一次性登录（弹出 Chrome，用任意方式登录 ElevenLabs）
python stt.py login

# 2) 转录（默认输出 out.srt）
python stt.py transcribe audio.m4a -o out.srt

# 3) 批量转录（每个文件各输出 <名字>.srt）
python stt.py transcribe a.mp3 b.mp3 c.mp3

# 4) 只看分配计划，不注册也不上传
python stt.py transcribe a.mp3 b.mp3 c.mp3 --dry-run

# 5) 指定语言 + 词汇 + 导出 VTT
python stt.py transcribe audio.m4a --lang eng --vocab "V社,Major" --format vtt -o out.vtt

# 6) 超长音频：按静音智能切分，逐段转录后合并回一份字幕
python stt.py transcribe long.m4a --split -o long.srt

# 可选：查看/预热多账号额度池
python stt.py pool status
python stt.py pool warm --target 3
```

登录会把 Firebase **refresh token** 存到 `session.json`（已 gitignore）。1 小时的 JWT 每次运行自动续签；仅在 refresh token 本身过期或报鉴权错误时重跑 `login`。

## 配置

复制 `config.example.toml` → `config.toml` 编辑持久默认值，CLI 参数覆盖配置：

| 字段 | 说明 | 默认 |
|---|---|---|
| `language` | `auto` 或语言名或 ISO 639-3 代码 | `auto` |
| `tag_audio_events` | 标记音频事件 | `true` |
| `include_subtitles` | 包含字幕（网页默认关，脚本强制开） | `true` |
| `no_verbatim` | 无逐字记录 | `false` |
| `use_speaker_library` | 从声音库分配声音 | `false` |
| `vocab` | 关键术语列表，如 `["Maj3r", "V社"]` | `[]` |
| `export_format` | `srt`/`vtt`/`txt`/`json`/`html`/`pdf`/`docx` | `srt` |
| `poll_timeout_secs` | 轮询超时秒数 | `600` |
| `show_cost` | 上传前打印预估积分成本 | `false` |
| `max_concurrency` | 跨账号并发转录上限；`1` = 回退串行（降级开关） | `4` |
| `stagger_secs` | 相邻两次上传起始的最小间隔（秒），降低同 IP 突发；`0` = 关闭错峰 | `2.0` |

## 本地 Web UI（可选）

命令行之外，`web.py` 用标准库起一个本地网页，复用 `stt.py` 的转录与账号逻辑：

```bash
python web.py            # 打开 http://127.0.0.1:8756
```

- **转录页**：拖拽/多选音频 → 浏览器测时长并预估积分 → 按剩余额度自动分配账号（best-fit）→ 「开始转录」真实上传、轮询、导出，完成后自动下载字幕。「使用的账号」区提供默认折叠的**「高级 · 手动指定账号」**面板：勾选账号即把分配限定在所选集合内（额度不足的账号灰显禁选），手动模式额度不足会直接报错、不会自动注册；清空勾选回到自动分配。转录期间底栏显示**真实分步进度**（已完成/总数计数、并发**活动任务列表**多行展示各文件与阶段、最新日志行），可展开「详情」查看完整滚动日志。
- **账号管理页**：读取 `accounts.json`，支持搜索/排序/多选/分页与回顶、真实刷新额度（仅刷新勾选账号，多账号并发）、删除、导出 JSON。
- **登录账号**：弹窗输入邮箱+密码，走 Firebase REST 直接登录并把令牌保存到 `accounts.json`（无需浏览器）。
- **启动注册机**：弹窗提供完整参数（对应 `config.toml → [temp_email]` 与目标满额账号数），「保存」会写回 `config.toml`（与配置文件双向同步）；「开始批量创建」先保存配置再跑真实 `pool warm` 批量注册。注册期间有分步进度日志：CLI（`stt pool warm` 及转录触发的自动注册）打印到 stderr，WebUI 注册弹窗内实时滚动显示并带 已完成/目标 计数。
- **功能管理 · 长音频切分页**：上传长音频 → 后端按静音点计算贪心切分方案（每段落在单个满额账号额度内）→ 执行切分并无损导出片段到本地 `out/`，供后续转录；与 CLI `--split` 共用 `audio_split.py` 的切分逻辑。转录页与本页均提供**「跳过长静音」开关**（默认关）：打开后 ≥10s 的静音区间不切进片段、不上传不计费（本页高级参数可调阈值），方案处显示省下的静音时长。
- 账号池不足以覆盖所有文件时，转录页会提示先 `pool warm`，不会静默注册。

零额外依赖、零构建；须在项目根目录运行（需 `accounts.json`、`config.toml`）。

## 命令参考

```
stt login              一次性浏览器登录 → session.json
stt transcribe <audio> [<audio> ...]  转录一个或多个音频文件（批量）
stt accounts           查看账号与剩余额度
stt pool status        查看 fresh/usable/depleted/invalid 账号数量
stt pool warm          注册账号直到达到 fresh 目标
stt list-languages     打印支持的语言名 + 代码
stt selfcheck          离线自检（不联网）
```

`transcribe` 参数：

| 参数 | 说明 |
|---|---|
| `-c, --config` | 配置文件路径（默认 `config.toml`） |
| `--lang` | `auto` / 语言名 / ISO 639-3 代码 |
| `--events / --no-events` | 标记音频事件（默认开） |
| `--subs / --no-subs` | 包含字幕（默认开） |
| `--verbatim / --no-verbatim` | 无逐字记录（默认关） |
| `--voice-lib / --no-voice-lib` | 使用声音库（默认关） |
| `--vocab` | 逗号分隔的关键术语 |
| `--format` | 导出格式 |
| `-o, --output` | 输出文件路径（**仅单文件可用**；多文件时会报错，各文件默认输出 `<名字>.<格式>`） |
| `--show-cost` | 打印预估积分成本 |
| `--poll-timeout` | 轮询超时秒数 |
| `--account` | 限定只用该邮箱的账号分配（可重复传多个）；额度不足时直接报错，不会自动注册 |
| `--dry-run` | 只打印分配计划后退出，不注册账号也不上传 |
| `--split` | 按静音把超长音频切成配额内的片段，逐段转录后合并回一份字幕（仅 `srt`/`vtt`/`txt`） |
| `--chunk-secs` | 每段目标时长上限（秒）；默认由额度自动推导（约 569s） |
| `--keep-chunks` | 合并成功后保留临时片段文件（默认清理） |
| `--silence-db` | 静音判定阈值（dB，默认 `-30`） |
| `--silence-min` | 最短静音时长（秒，默认 `0.5`） |
| `--skip-silence` | 配合 `--split`：跳过超长静音区间，不上传不计费（段数可能变多，默认关） |
| `--skip-silence-min` | 触发跳过的最短静音时长（秒，默认 `10`） |

`accounts` 参数：

| 参数 | 说明 |
|---|---|
| `-c, --config` | 配置文件路径（默认 `config.toml`） |
| `--refresh` | 强制从 API 刷新额度（8 线程并发，进度按完成先后输出到 stderr） |
| `-e, --email` | 只作用于该账号（可重复）；同时过滤 `--refresh` 范围与列表显示 |

## 语言

`--lang` 接受 `auto`（自动检测，默认）、语言名（如 `english`），或任意 ISO 639-3 代码（如 `eng`/`zho`/`jpn`）。运行 `python stt.py list-languages` 查看已内置的语言表。

## 多账号额度池（可选）

配置 `[temp_email]` 和 `[accounts]` 后，脚本可以维护多个免费账号：转录时自动选择够用且剩余额度最小的账号，成功后按 `pool_target` 自动补池。详见 [`docs/account-pool.md`](./docs/account-pool.md)；`[temp_email]` 依赖的临时邮箱后端部署见 [`docs/temp-email-backend.md`](./docs/temp-email-backend.md)。不配置 `[temp_email]` 时仍是普通单账号模式。

## 批量转录与自动分配

`stt transcribe a.mp3 b.mp3 c.mp3` 可一次转录多个文件。上传前脚本会：

- 预估每个文件的积分成本（按时长；时长未知的文件保守地独占一个全新账号）；
- **优先用现有账号装箱**：在剩余额度足够（× `selection_margin`）的现有账号里，用「最佳适配」把多个文件塞进同一个账号，先排空快用完的账号、保留额度大的；
- **缺多少注册多少**：装不下的文件才触发注册新账号，注册数量正好等于缺口——现有账号排在前面，新注册账号排在最后；
- 先打印分配计划（`文件 → 账号/NEW#k`，`need new: N`），再**跨账号并发转录**：同一账号内的任务串行、不同账号并行（上限 `max_concurrency`，默认 4；相邻上传起始至少间隔 `stagger_secs` 秒错峰）。缺口账号采用**注册-转录流水线**：已分配到现有账号的任务立即开跑，注册每完成一个新账号，其任务立刻投入转录，不等剩余注册；注册中途失败只影响未注册槽位的任务（标 FAIL），在跑任务照常完成。单个文件失败会跳过并继续，最后打印成功/失败汇总；有任一失败则退出码非零。`max_concurrency = 1` 即回到旧的串行行为。

`--dry-run` 只打印计划后退出，不注册也不上传。`register_count > 0` 但未配置 `[temp_email]` 时会在动手前直接报错（说明还差几个账号）。

**手动限定候选集**：`--account x@y.z`（可重复）把分配候选集限定为所列账号，best-fit 只在这些账号里进行——单文件 + 单账号即「这个音频就用这个账号」。邮箱不存在或账号已失效会在开始前报错；所选账号装不下全部文件时**直接报错退出，不会自动注册新账号**（增选账号或去掉 `--account` 回到自动分配）。可与 `--dry-run`、`--split` 组合使用。

`-o/--output` 只在单文件时可用；批量时每个文件默认输出到各自的 `<名字>.<格式>`。

## 超长音频按静音切分（`--split`）

单个免费账号约只够转录 10 分钟。对更长的音频加 `--split`，脚本会：

- 用 ffmpeg `silencedetect` 检测静音区间，在每段目标时长上限内**选最靠后的静音中点**切开（切点落在静音处，不切断词）；窗口内没有静音则在上限处硬切并告警；
- 片段目标时长默认由额度自动推导（`fresh_threshold / (积分每秒 × selection_margin) × 0.95`，约 569s ≈ 9.5 分钟），保证每段能被一个满额账号装下；可用 `--chunk-secs` 覆盖；
- 把切出的片段当作普通文件汇入既有的**多账号分配 + 逐段转录**流程（每段各占一个够用的账号，缺口自动注册补齐）；
- 全部片段成功后，按每段起始偏移**校正时间戳并合并**成一份 `<名字>.<格式>`（`srt`/`vtt` 重排序号、`txt` 顺序拼接），随后清理临时片段（`--keep-chunks` 保留）。

```bash
# 1 小时音频 → 自动切成约 7 段，分配到账号池转录，合并为一份 srt
python stt.py transcribe interview.m4a --split -o interview.srt

# 先看切分与分配计划，不上传
python stt.py transcribe interview.m4a --split --dry-run

# 自定义片段上限与静音灵敏度
python stt.py transcribe interview.m4a --split --chunk-secs 480 --silence-db -35 --silence-min 0.8

# 跳过 ≥10s 的长静音：静音不上传、不计费（段数可能变多）
python stt.py transcribe interview.m4a --split --skip-silence
```

可选加 `--skip-silence`（默认关）：≥ `--skip-silence-min`（默认 10s）的静音区间整段跳过，不切进片段、不消耗额度；有声区间两侧各保留 0.5s 缓冲，字幕时间轴仍与原音频对齐（被跳过处为空白）。由于 `-c copy` 无法无损拼接不连续音频，每个有声区间独立成段，段数可能比不跳时多。

约束与行为：

- 仅支持合并 `srt`/`vtt`/`txt`；搭配 `json`/`html`/`pdf`/`docx` 会直接报错。
- **失败即不合并**:任一片段转录失败时不生成合并文件，但保留已成功片段的输出与切出的片段文件，汇总里标明失败段（退出码非零），便于手动重跑。
- 临时片段写在 `out/<名字>-chunks/`；需要 ffmpeg/ffprobe 可用。
- 用 `-c copy` 无损切割,切点毫秒级对齐关键帧(落在静音内,无感)。

## 限制

- **文件大小**：1000 MB 硬限制，超出在上传前拒绝。
- **时长**：超过 10 分钟（免费账号软限制）时，若 `ffprobe` 可用则提醒，不强制阻止；超长音频可用 `--split` 自动按静音切分再合并。
- **积分**：免费约 1 万积分（≈13.9 积分/秒，≈12 分钟/月）。不足时服务端拒绝上传，CLI 显示错误；`--show-cost` 可在上传前打印预估。

> [!NOTE]
> 脚本默认开关为：标记音频事件=开、包含字幕=开（网页默认是关，脚本强制开）、其余关、语言=自动、导出=SRT。

## 工作原理

脚本用 `httpx` 直接调用网页应用的内部 API（`api.us.elevenlabs.io`），鉴权用 Firebase JWT bearer 令牌。登录步骤用 Playwright 启动本机 Chrome 读取 localStorage 中的 Firebase 会话，导出 refresh token；运行时不依赖浏览器。抓包契约见 [`docs/api-contract.md`](./docs/api-contract.md)。

## 文件

| 文件 | 说明 |
|---|---|
| `stt.py` | CLI 与客户端主程序 |
| `audio_split.py` | 静音检测与切分模块（CLI `--split` 与 Web UI 共用） |
| `web.py` | 本地 Web UI 服务端（标准库） |
| `webui.html` | Web UI 单页前端 |
| `config.example.toml` | 配置示例 |
| `requirements.txt` | 依赖 |
| `session.json` | `login` 生成（凭证，已 gitignore） |
| `accounts.json` | 多账号池生成（凭证，已 gitignore） |
| `docs/account-pool.md` | 多账号池与自动注册说明 |
| `docs/temp-email-backend.md` | 临时邮箱后端（cloudflare_temp_email）集成说明 |
| `docs/api-contract.md` | 抓包得到的内部 API 契约 |

## 自检

```bash
python stt.py selfcheck
```

## 致谢

- [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) — 多账号自动注册依赖的临时邮箱后端
- [Playwright](https://playwright.dev) — 一次性登录步骤的浏览器自动化
- [httpx](https://www.python-httpx.org) — HTTP 客户端
- [FFmpeg](https://ffmpeg.org) — 可选的音频时长预检（`ffprobe`）

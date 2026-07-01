# elevenlabs-stt

[English](./README.en.md) | **简体中文**

> 将 ElevenLabs 网页版「语音转文本」功能转为命令行脚本，使用你自己的登录账号，复用免费额度。

[!IMPORTANT]
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

# 3) 指定语言 + 词汇 + 导出 VTT
python stt.py transcribe audio.m4a --lang eng --vocab "V社,Major" --format vtt -o out.vtt
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

## 命令参考

```
stt login              一次性浏览器登录 → session.json
stt transcribe <audio> 转录音频文件
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
| `-o, --output` | 输出文件路径 |
| `--show-cost` | 打印预估积分成本 |
| `--poll-timeout` | 轮询超时秒数 |

## 语言

`--lang` 接受 `auto`（自动检测，默认）、语言名（如 `english`），或任意 ISO 639-3 代码（如 `eng`/`zho`/`jpn`）。运行 `python stt.py list-languages` 查看已内置的语言表。

## 限制

- **文件大小**：1000 MB 硬限制，超出在上传前拒绝。
- **时长**：超过 10 分钟（免费账号软限制）时，若 `ffprobe` 可用则提醒，不强制阻止。
- **积分**：免费约 1 万积分（≈13.9 积分/秒，≈12 分钟/月）。不足时服务端拒绝上传，CLI 显示错误；`--show-cost` 可在上传前打印预估。

> [!NOTE]
> 脚本默认开关为：标记音频事件=开、包含字幕=开（网页默认是关，脚本强制开）、其余关、语言=自动、导出=SRT。

## 工作原理

脚本用 `httpx` 直接调用网页应用的内部 API（`api.us.elevenlabs.io`），鉴权用 Firebase JWT bearer 令牌。登录步骤用 Playwright 启动本机 Chrome 读取 localStorage 中的 Firebase 会话，导出 refresh token；运行时不依赖浏览器。抓包契约见 `.trellis/tasks/07-01-elevenlabs-stt-script/research/api-contract.md`。

## 文件

| 文件 | 说明 |
|---|---|
| `stt.py` | CLI 与客户端主程序 |
| `config.example.toml` | 配置示例 |
| `requirements.txt` | 依赖 |
| `session.json` | `login` 生成（凭证，已 gitignore） |

## 自检

```bash
python stt.py selfcheck
```

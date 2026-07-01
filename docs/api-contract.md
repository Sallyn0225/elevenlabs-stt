# ElevenLabs Web STT — 内部 API 契约（抓包 2026-07-01）

[English](./api-contract.en.md) | **简体中文**

Base: `https://api.us.elevenlabs.io`
每个请求都需要鉴权头：`Authorization: Bearer <JWT>`。
JWT 是 **Firebase ID token**（RS256，`iss: https://securetoken.google.com/xi-labs`，
`aud: xi-labs`），包含 `workspace_id`、`workspace_user_id`、`user_id`、`email`。
**有效期 1 小时**（`exp = iat + 3600`）。登录使用 Firebase Auth：
`identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<firebaseApiKey>`，
返回 `idToken`（JWT）、`refreshToken`（长期有效）和 `localId`。
因此脚本必须保存 **refresh token**，并通过 Firebase 刷新 JWT：
`https://securetoken.googleapis.com/v1/token?key=<apiKey>`（`grant_type=refresh_token`，
`refresh_token=...`）。否则会每小时掉登录态。
抓到的 Firebase apiKey：`AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys`。
浏览器也会把包含 refresh token 的 Firebase user object 存在 localStorage：
`firebase:authUser:<apiKey>:[DEFAULT]`，所以一次性登录 helper 可以直接从这里读取，
不必拦截登录响应。

## Endpoints

### 1. 列出任务
`GET /v1/speech-to-text/tasks?page_size=20&filter_by_creator=all`
→ `{"tasks":[<task>...], "cursor":null}`。
列表投影会省略 `user_params`/`result`；完整对象用 `GET /tasks/{id}` 获取。

### 2. 成本预检
`GET /v1/speech-to-text/cost?duration=<seconds>`
→ `{"credits": <int>}`（132.81925s ⇒ 1845 credits ≈ 13.9 credits/sec）。
免费层约 10,000 credits，总计约 12 分钟音频/月。UI 显示
“<cost> 积分 / <remaining> 积分”。上传前用这个接口预估成本。

### 3. 创建转录任务 ★ 核心请求
`POST /v1/speech-to-text/tasks`
- Headers: `Authorization: Bearer <JWT>`，
  `Content-Type: multipart/form-data; boundary=<boundary>`
- Body: multipart。**音频文件就在这个 POST 里内联上传**
  （3.07MB 文件对应 content-length 3,077,834 = 文件 + 表单开销）。
  没有单独的 presigned/S3 上传步骤。网页里“正在上传 50%”就是这个 POST 的
  XHR upload progress。
- 表单字段名（通过解析保存的 multipart body 确认：
  `research/raw/req624-create-task.network-request`）。只发送非默认字段；省略字段走服务端默认：
  - `task_name` = `_tmp-sample.m4a`（展示名字符串）
  - `file` = 音频文件，filename `_tmp-sample.m4a`
  - `model_id` = `scribe_v2`（固定）
  - `tag_audio_events` = `"true" | "false"`（字符串）
  - `include_subtitles` = `"true" | "false"`
  - `keyterms` = `ElevenLabs`（词汇增强；请求字段叫 `keyterms`，响应映射到
    `biased_keywords:[{keyword,bias}]`）
  - 默认时省略：`language_code`（`null` = 自动检测）、`no_verbatim`（`false`）、
    `use_speaker_library`（`false`）、`num_speakers`（`null`）。需要设置时显式添加字段。
  - 多个词汇：✅ 已确认 = **重复多个 `keyterms` 表单字段**，每词一个
    （`req1146`: `keyterms=Maj3r` + `keyterms=V社` ⇒ 响应
    `biased_keywords:[{keyword:"Maj3r",bias:1.0},{keyword:"V社",bias:1.0}]`）。
- 响应（已创建任务）：
```json
{
  "_id":"yX2edhTvZcKRj7NYc3A4",
  "task_name":"_tmp-sample.m4a",
  "state":"not_processing",
  "progress":0.0,
  "created_at":"2026-07-01T06:15:20.169531Z",
  "finished_at":null,
  "file":{"file_name":"_tmp-sample.m4a","content_type":"audio/x-m4a","duration_secs":0.0,"size_mb":2.93,"signed_url":null},
  "last_error":null,
  "review_request":null,
  "productions_description_id":null,
  "access_info":{"is_creator":true,"creator_name":"...","creator_email":"...","role":"admin","anonymous_access_level_override":null,"access_source":"creator"},
  "user_params":{
    "language_code": null,
    "tag_audio_events": true,
    "include_subtitles": true,
    "num_speakers": null,
    "model_id":"scribe_v2",
    "biased_keywords":[{"keyword":"ElevenLabs","bias":1.0}],
    "no_verbatim": false,
    "use_speaker_library": false
  },
  "result": null,
  "preview_file": null
}
```

### 4. 轮询任务状态
`GET /v1/speech-to-text/tasks/{_id}`
→ 同一个 task object。状态机：
`not_processing` → `processing` → `processed`（`progress` 0.0→1.0，设置 `finished_at`）。
进入 `processed` 后，`result` 会被填充：
```json
"result": {
  "language_code": "zho",
  "language_probability": 0.997,
  "words": [
    {"text":"Team","start":0.08,"end":0.22,"type":"word","speaker_id":"speaker_0"}
  ]
}
```
处理完成后 `file.signed_url` 可能变成非 null（用于预览播放）。
另有 `GET /v1/speech-to-text/tasks/{_id}/file/waveform`（音频预览；脚本不需要）。

### 5. 导出 / 下载 ✅ 已确认
`POST /v1/speech-to-text/tasks/{_id}/editor/export/{format}`
- `{format}` ∈ `txt|pdf|docx|json|html|srt|vtt`（小写，匹配菜单项）。
- Headers: `Authorization: Bearer <JWT>`，`Content-Type: application/json`。
- Request body: `{"language_code":"<code>"}`（如 `{"language_code":"zho"}`，23 bytes）。
  使用 `result.language_code`（检测出的代码）或用户指定的代码。
- 响应：直接返回渲染后的文件内容（`Content-Type: text/plain`，
  `Content-Disposition: attachment; filename="<task_name>_<lang>.srt"`）。
  脚本直接把响应 bytes 保存到磁盘。
- 导出 UI 菜单项（中文）：文本(TXT)/PDF/DOCX/JSON/HTML/SRT/VTT。默认 = SRT。

### 6. Keywords lists（已保存词表，可选）
`GET /v1/speech-to-text/keywords-lists` → `[]`（测试账号没有词表）。
本流程不需要；脚本对每个任务内联传 `biased_keywords`。

## 字段映射（网页 UI ↔ API）

| 网页开关 | API 字段 | 网页默认 | 脚本默认 |
|---|---|---|---|
| 主要语言 = 检测 | `language_code` | `null` (=auto) | `null` (auto) |
| 标记音频事件 | `tag_audio_events` | `true` | `true` |
| 包含字幕 | `include_subtitles` | `false` | **`true`**（强制开启） |
| 无逐字记录 | `no_verbatim` | `false` | `false` |
| 从声音库分配声音 | `use_speaker_library` | `false` | `false` |
| 关键术语 | `biased_keywords` | `[]` | 来自配置/参数 |
| (model) | `model_id` | `scribe_v2`（固定） | `scribe_v2` |

## 语言代码映射 ✅ 已确认
Combobox option 的 `value` = 语言**展示名**（例如 `English`）；`检测` = 自动。
Combobox 内部 name→code 映射产出 **ISO 639-3** 代码。
- 省略 `language_code` 或传 `null` = 自动检测（已确认，`req624`）。
- 用户指定语言：创建任务时表单字段 `language_code` = ISO 639-3。
  English ⇒ **`eng`**（已确认，`req1146` 请求体）。
- 自动检测结果：`result.language_code` = ISO 639-3（中文 ⇒ `zho`，已确认）。
  导出文件名：`..._zho.srt`。
- ⚠️ 怪点：创建任务响应里的 `user_params.language_code` 会规范化成 ISO 639-1
  （请求 English `eng` ⇒ 响应 `en`）。这只是服务端响应；脚本发送 ISO 639-3，
  导出时透传 `result.language_code`。不要信响应里的 `user_params.language_code` 来导出。
- 脚本内置 combobox 语言展示名→ISO639-3 映射，同时接受原始 ISO 639-3 代码
  （如 `eng`、`zho`）和 `auto`（默认）。

## 剩余抓包 TODO
- 无 — 2026-07-01 已完整抓完契约。

## 实现阶段后续（无需继续抓包）
- 维护展示名→ISO639-3 语言映射；Cnh、Basa、Dyula、Võro 等特殊名称要小心。
- 创建任务时发送 ISO 639-3 `language_code`；导出时透传 `result.language_code`。

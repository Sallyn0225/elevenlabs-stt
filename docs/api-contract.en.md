# ElevenLabs Web STT — Internal API Contract (captured 2026-07-01)

**English** | [简体中文](./api-contract.md)

Base: `https://api.us.elevenlabs.io`
Auth: `Authorization: Bearer <JWT>` on every request.
JWT is a **Firebase ID token** (RS256, `iss: https://securetoken.google.com/xi-labs`,
`aud: xi-labs`). It contains `workspace_id`, `workspace_user_id`, `user_id`, and `email`.
**Expiry: 1 hour** (`exp = iat + 3600`). Login uses Firebase Auth
(`identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<firebaseApiKey>`),
which returns `idToken` (the JWT), `refreshToken` (long-lived), and `localId`.
Therefore the script MUST store the **refresh token** and refresh the JWT through Firebase:
`https://securetoken.googleapis.com/v1/token?key=<apiKey>` (`grant_type=refresh_token`,
`refresh_token=...`). Otherwise the session dies every hour.
Firebase apiKey observed: `AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys`.
The Firebase user object, including the refresh token, is also persisted by the browser in
localStorage key `firebase:authUser:<apiKey>:[DEFAULT]`, so the one-time login helper can
read it there instead of intercepting the sign-in response.

## Endpoints

### 1. List tasks
`GET /v1/speech-to-text/tasks?page_size=20&filter_by_creator=all`
→ `{"tasks":[<task>...], "cursor":null}`
The list projection omits `user_params`/`result`; use `GET /tasks/{id}` for the full object.

### 2. Cost pre-check
`GET /v1/speech-to-text/cost?duration=<seconds>`
→ `{"credits": <int>}` (132.81925s ⇒ 1845 credits ≈ 13.9 credits/sec).
The free tier is about 10,000 credits total, roughly 12 minutes of audio per month.
The UI shows "<cost> 积分 / <remaining> 积分". Use this endpoint to pre-check before upload.

### 3. Create transcription task ★ key request
`POST /v1/speech-to-text/tasks`
- Headers: `Authorization: Bearer <JWT>`,
  `Content-Type: multipart/form-data; boundary=<boundary>`
- Body: multipart. **The audio file is uploaded inline in this single POST**
  (content-length 3,077,834 for a 3.07MB file = file + form overhead).
  There is no separate presigned/S3 upload step. The UI "uploading 50%" state is this
  POST's XHR upload progress.
- Form field names (CONFIRMED by parsing saved multipart body,
  `research/raw/req624-create-task.network-request`). Only non-default fields are sent;
  omitted fields fall back to server defaults:
  - `task_name` = `_tmp-sample.m4a` (display name string)
  - `file` = audio file, filename `_tmp-sample.m4a`
  - `model_id` = `scribe_v2` (fixed)
  - `tag_audio_events` = `"true" | "false"` (string)
  - `include_subtitles` = `"true" | "false"`
  - `keyterms` = `ElevenLabs` (vocab; request field is `keyterms`, response maps to
    `biased_keywords:[{keyword,bias}]`)
  - Omitted when default: `language_code` (`null` = auto), `no_verbatim` (`false`),
    `use_speaker_library` (`false`), `num_speakers` (`null`). To set them, add the field.
  - Multiple vocab words: ✅ CONFIRMED = **repeated `keyterms` form fields**,
    one per word (`req1146`: `keyterms=Maj3r` + `keyterms=V社` ⇒ response
    `biased_keywords:[{keyword:"Maj3r",bias:1.0},{keyword:"V社",bias:1.0}]`).
- Response (created task):
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

### 4. Poll task status
`GET /v1/speech-to-text/tasks/{_id}`
→ same task object. State machine:
`not_processing` → `processing` → `processed` (`progress` 0.0→1.0, `finished_at` set).
When `processed`, `result` is populated:
```json
"result": {
  "language_code": "zho",
  "language_probability": 0.997,
  "words": [
    {"text":"Team","start":0.08,"end":0.22,"type":"word","speaker_id":"speaker_0"}
  ]
}
```
`file.signed_url` may become non-null when processed (preview playback).
Also available: `GET /v1/speech-to-text/tasks/{_id}/file/waveform` (audio preview; not needed).

### 5. Export / download ✅ CONFIRMED
`POST /v1/speech-to-text/tasks/{_id}/editor/export/{format}`
- `{format}` ∈ `txt|pdf|docx|json|html|srt|vtt` (lowercase, matches menu items).
- Headers: `Authorization: Bearer <JWT>`, `Content-Type: application/json`.
- Request body: `{"language_code":"<code>"}` (e.g. `{"language_code":"zho"}`, 23 bytes).
  Use `result.language_code` (detected code) or the user's selected code.
- Response: rendered file content directly (`Content-Type: text/plain`,
  `Content-Disposition: attachment; filename="<task_name>_<lang>.srt"`).
  The script saves the response bytes to disk.
- Export UI menu items (CN): 文本(TXT)/PDF/DOCX/JSON/HTML/SRT/VTT. Default = SRT.

### 6. Keyword lists (saved lists, optional)
`GET /v1/speech-to-text/keywords-lists` → `[]` (the tested user had none).
Not needed for this flow; we pass `biased_keywords` inline per task.

## Field mapping (web UI ↔ API)

| Web toggle (CN) | API field | Web default | Script default |
|---|---|---|---|
| 主要语言 = 检测 | `language_code` | `null` (=auto) | `null` (auto) |
| 标记音频事件 | `tag_audio_events` | `true` | `true` |
| 包含字幕 | `include_subtitles` | `false` | **`true`** (force) |
| 无逐字记录 | `no_verbatim` | `false` | `false` |
| 从声音库分配声音 | `use_speaker_library` | `false` | `false` |
| 关键术语 | `biased_keywords` | `[]` | from config/flag |
| (model) | `model_id` | `scribe_v2` (fixed) | `scribe_v2` |

## Language code mapping ✅ CONFIRMED
Combobox option `value` = language **display name** (e.g. `English`); `检测` = auto.
The combobox's internal name→code map yields **ISO 639-3** codes.
- `language_code` omitted or `null` = auto-detect (CONFIRMED, `req624`).
- User-selected language: create form field `language_code` = ISO 639-3 code.
  English ⇒ **`eng`** (CONFIRMED, `req1146` request body).
- Auto-detect result: `result.language_code` = ISO 639-3 (Chinese ⇒ `zho`, CONFIRMED).
  Export filename: `..._zho.srt`.
- ⚠️ Quirk: create response `user_params.language_code` normalizes to ISO 639-1
  (English `eng` request ⇒ response `en`). This is server-side only; the script SENDS
  ISO 639-3 and PASSES THROUGH `result.language_code` for export. Do not trust response
  `user_params.language_code` for the export body.
- The script ships a display-name→ISO639-3 map for the combobox languages. It also accepts
  a raw ISO 639-3 code (e.g. `eng`, `zho`) directly, plus `auto` (default).

## Remaining capture TODO
- None — contract fully captured on 2026-07-01.

## Impl-time follow-ups (no capture needed)
- Maintain the display-name→ISO639-3 language map; odd names like Cnh, Basa, Dyula, and Võro need care.
- Script sends ISO 639-3 in create `language_code`; export passes through `result.language_code`.

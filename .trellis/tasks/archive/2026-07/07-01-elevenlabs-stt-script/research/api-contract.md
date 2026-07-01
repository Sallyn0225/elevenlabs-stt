# ElevenLabs Web STT — Internal API Contract (抓包 2026-07-01)

Base: `https://api.us.elevenlabs.io`
Auth: `Authorization: Bearer <JWT>` on every request.
JWT is a **Firebase ID token** (RS256, `iss: https://securetoken.google.com/xi-labs`,
`aud: xi-labs`). Contains `workspace_id`, `workspace_user_id`, `user_id`, `email`.
**Expiry: 1 hour** (`exp = iat + 3600`). Login uses Firebase Auth
(`identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<firebaseApiKey>`),
which returns `idToken` (the JWT) + `refreshToken` (long-lived) + `localId`.
⇒ Script MUST store the **refresh token** and refresh the JWT via Firebase
`https://securetoken.googleapis.com/v1/token?key=<apiKey>` (`grant_type=refresh_token`,
`refresh_token=...`) — otherwise the session dies every hour.
Firebase apiKey seen: `AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys`.
The Firebase user object (incl. refreshToken) is also persisted by the browser in
localStorage key `firebase:authUser:<apiKey>:[DEFAULT]` → the one-time login helper
can read it from there instead of intercepting the sign-in response.

## Endpoints

### 1. List tasks
`GET /v1/speech-to-text/tasks?page_size=20&filter_by_creator=all`
→ `{"tasks":[<task>...], "cursor":null}`
(list projection omits `user_params`/`result`; use GET /tasks/{id} for full).

### 2. Cost pre-check
`GET /v1/speech-to-text/cost?duration=<seconds>`
→ `{"credits": <int>}`  (132.81925s ⇒ 1845 credits ≈ 13.9 credits/sec)
Free tier ≈ 10,000 credits total ⇒ ~12 min of audio/month. UI shows
"<cost> 积分 / <remaining> 积分". Use this to pre-check before uploading.

### 3. Create transcription task  ★ THE key request
`POST /v1/speech-to-text/tasks`
- Headers: `Authorization: Bearer <JWT>`,
  `Content-Type: multipart/form-data; boundary=<boundary>`
- Body: multipart. **The audio file is uploaded inline in this single POST**
  (content-length 3,077,834 for a 3.07MB file = file + form overhead).
  No separate presigned/S3 upload step. The "正在上传 50%" UI = this POST's
  xhr upload progress.
- Form field names (CONFIRMED by parsing saved multipart body,
  research/raw/req624-create-task.network-request). Only non-default fields
  are sent; omitted fields fall back to server defaults:
  - `task_name`      = "_tmp-sample.m4a"        (display name string)
  - `file`           = <audio file, filename="_tmp-sample.m4a">  (the blob)
  - `model_id`       = "scribe_v2"             (fixed)
  - `tag_audio_events`    = "true" | "false"   (string)
  - `include_subtitles`   = "true" | "false"
  - `keyterms`       = "ElevenLabs"            (vocab! request field is
                                               `keyterms`, response maps to
                                               `biased_keywords:[{keyword,bias}])
  - OMITTED when default: `language_code` (null=auto), `no_verbatim`(false),
    `use_speaker_library`(false), `num_speakers`(null). To set them, add
    the field explicitly.
  - Multiple vocab words: ✅ CONFIRMED = **repeated `keyterms` form fields**,
    one per word (req1146: `keyterms=Maj3r` + `keyterms=V社` ⇒ response
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
`not_processing` → `processing` → `processed` (progress 0.0→1.0, finished_at set).
On `processed`, `result` is populated:
```json
"result": {
  "language_code": "zho",            // detected language, ISO 639-3
  "language_probability": 0.997,     // confidence
  "words": [                          // word-level timestamps
    {"text":"Team","start":0.08,"end":0.22,"type":"word","speaker_id":"speaker_0"}, ...
  ]
}
```
`file.signed_url` may become non-null when processed (for preview playback).
Also: `GET /v1/speech-to-text/tasks/{_id}/file/waveform` (audio preview, not needed).

### 5. Export / download  ✅ CONFIRMED
`POST /v1/speech-to-text/tasks/{_id}/editor/export/{format}`
- `{format}` ∈ `txt|pdf|docx|json|html|srt|vtt` (lowercase, matches menu items).
- Headers: `Authorization: Bearer <JWT>`, `Content-Type: application/json`.
- Request body: `{"language_code":"<code>"}` (e.g. `{"language_code":"zho"}`, 23 bytes).
  Use `result.language_code` (the detected code) or the user's chosen code.
- Response: the rendered file content directly (`Content-Type: text/plain`,
  `Content-Disposition: attachment; filename="<task_name>_<lang>.srt"`).
  Script just saves response bytes to disk.
- Export UI menu items (CN): 文本(TXT)/PDF/DOCX/JSON/HTML/SRT/VTT. Default = SRT.

### 6. Keywords lists (saved lists, optional)
`GET /v1/speech-to-text/keywords-lists` → `[]` (user has none).
Not needed for our flow — we pass `biased_keywords` inline per task.

## Field mapping (web UI ↔ API)

| Web toggle (CN) | API field | Default (web) | Script default |
|---|---|---|---|
| 主要语言 = 检测 | `language_code` | `null` (=auto) | `null` (auto) |
| 标记音频事件 | `tag_audio_events` | `true` | `true` |
| 包含字幕 | `include_subtitles` | `false` | **`true`** (force) |
| 无逐字记录 | `no_verbatim` | `false` | `false` |
| 从声音库分配声音 | `use_speaker_library` | `false` | `false` |
| 关键术语 | `biased_keywords` | `[]` | from config/flag |
| (model) | `model_id` | `scribe_v2` (fixed) | `scribe_v2` |

## Language code mapping ✅ CONFIRMED
Combobox options' `value` = language **display name** (e.g. "English"),
"检测"=auto. The combobox's internal name→code map yields **ISO 639-3** codes.
- `language_code` field OMITTED (or null) = auto-detect (CONFIRMED, req624).
- User-selected language: create form field `language_code` = ISO 639-3 code.
  English ⇒ **"eng"** (CONFIRMED, req1146 request body).
- Auto-detect RESULT: `result.language_code` = ISO 639-3 (Chinese ⇒ "zho",
  CONFIRMED). Export filename `..._zho.srt`.
- ⚠️ QUIRK: the create RESPONSE `user_params.language_code` normalizes to
  ISO 639-1 (English "eng" request ⇒ response "en"). This is server-side only;
  the script SENDS ISO 639-3 and PASS-THROUGHS `result.language_code` for export.
  Do not trust response `user_params.language_code` for the export body — use
  `result.language_code`.
- Script will ship a display-name→ISO639-3 map for the ~157 combobox languages
  (build at impl: extract from JS bundle OR curate from ISO 639-3 reference;
  odd names like Cnh/Basa/Dyula/Võro need care). Script also accepts a raw
  ISO 639-3 code (e.g. "eng","zho") directly, plus "auto" (default).

## Remaining capture TODO
- (none — contract fully captured 2026-07-01)

## Impl-time follow-ups (no capture needed)
- Build display-name→ISO639-3 language map (extract from JS bundle or curate
  from ISO 639-3 reference). Verify a couple of odd names (Cnh, Basa, Dyula, Võro).
- Script sends ISO 639-3 in create `language_code`; export passes through
  `result.language_code`.

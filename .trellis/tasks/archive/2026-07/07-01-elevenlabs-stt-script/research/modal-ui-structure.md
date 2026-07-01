# Speech-to-Text Modal — UI Structure (抓包 2026-07-01)

Source: chrome-devtools snapshot of `https://elevenlabs.io/app/speech-to-text` →
click "转录文件" (uid=2_78).

## Modal tabs
- 上传 (Upload) — IN SCOPE
- 录制 (Record) — out of scope
- YouTube — out of scope
- URL — out of scope

## Upload tab controls
- Drop area: "点击或拖动文件到此处上传" / "音频和视频文件，最大 1000MB"
- "选择文件" button (file input) — uid=3_9
- 主要语言 combobox (uid=3_11) — default value "检测" (= auto-detect)
- Four switches (web defaults observed):
  - 标记音频事件 (Mark audio events) — **checked by default** (web default ON)
  - 包含字幕 (Include subtitles) — **unchecked by default** (web default OFF;
    SCRIPT must force this ON per user requirement)
  - 无逐字记录 (No verbatim / diarization?) — unchecked
  - 从声音库分配声音 (Assign voice from voice library) — unchecked
- 关键术语 (Key terms / vocab) textbox (uid=3_21), placeholder "添加关键词..."
  - "清除标签" (clear tags) button, "关键词选项" (keyword options) menu button
- "上传文件" submit button (uid=3_24) — disabled until a file is selected
- "关闭" (close) button

## Language list (combobox options, display value = textContent)

"检测" (auto-detect) + ~157 languages. Option `value` attribute = language
display name (no separate `data-value` code). The API code mapping is NOT
visible from the DOM — must be captured from the create-job request body.

Languages (display names):
Abkhaz, Afrikaans, Albanian, Amharic, Arabic, Armenian, Assamese, Asturian,
Azerbaijani, Basa, Bashkir, Basque, Belarusian, Bengali, Bosnian, Brahui,
Breton, Bulgarian, Burmese, Cantonese, Catalan, Central Kurdish, Chinese,
Chuvash, Cnh, Cree, Croatian, Czech, Danish, Dhivehi, Dutch, Dyula, Eastern
Mari, English, Erzya, Esperanto, Estonian, Faroese, Filipino, Finnish,
French, Frisian, Galician, Georgian, German, Greek, Gujarati, Haitian Creole,
Hausa, Hebrew, Hindi, Hungarian, Icelandic, Igbo, Indonesian, Interlingua,
Irish, Italian, Japanese, Javanese, Kabyle, Kalenjin, Kannada, Kashmiri,
Kazakh, Khmer, Kinyarwanda, Korean, Kurdish, Kurmanji, Kyrgyz, Lao,
Latgalian, Latin, Latvian, Ligurian, Lithuanian, Luganda, Luxembourgish,
Macedonian, Malagasy, Malay, Malayalam, Maltese, Māori, Marathi, Min Nan,
Moksha, Mongolian, Nepali, Northern Hindko, Northern Sotho, Norwegian,
Occitan, Odia, Oromo, Ossetian, Pashto, Persian, Polish, Portuguese, Punjabi,
Quechua, Romanian, Romansh, Russian, Sakha (Yakut), Samoan, Sanskrit,
Santali, Saraiki, Sardinian, Scottish Gaelic, Serbian, Shona, Sindhi,
Sinhala, Slovak, Slovenian, Somali, Southern Sotho, Spanish, Standard
Moroccan Tamazight, Sundanese, Swahili, Swedish, Taita, Tajik, Tamil, Tatar,
Telugu, Thai, Tibetan, Tigre, Tigrinya, Toki Pona, Tongan, Tswana, Turkish,
Turkmen, Twi, Ukrainian, Upper Sorbian, Urdu, Uyghur, Uzbek, Vietnamese,
Võro, Votic, Welsh, Western Mari, Wolof, Xhosa, Yiddish, Yoruba, Zazaki, Zulu.

## TODO (capture next)
- Upload `_tmp-sample.m4a` → capture network: presigned upload, create-job
  request (language code + toggle field names + vocab field), poll status,
  result/download + export formats.
- Auth: confirm cookie vs JWT bearer in request headers.

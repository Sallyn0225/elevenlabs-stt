# Temp-mail backend (cloudflare_temp_email)

**English** | [简体中文](./temp-email-backend.md)

Multi-account auto-registration needs a **temp-mail backend** to receive ElevenLabs verification emails. This project calls the API of [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email) over HTTP to request a mailbox address and read the verification mail that arrives.

> [!IMPORTANT]
> Self-host the backend; do not abuse other people's public instances. This project is not affiliated with `cloudflare_temp_email` — it only reuses its API.

## What it is

`cloudflare_temp_email` is a temporary-mailbox service built on Cloudflare's free tier (Workers + D1 + Pages + Email Routing). It runs at zero cost and exposes APIs to create addresses and read parsed mail. This project uses only two of those capabilities: **create an address** and **read the inbox** — it does not depend on the upstream frontend, Telegram, SMTP, etc.

## Prerequisite: deploy the backend

Follow the upstream [deployment docs](https://temp-mail-docs.awsl.uk) (or the [GitHub Action one-click deploy](https://temp-mail-docs.awsl.uk/zh/guide/actions/github-action.html)) to stand up the backend, then:

1. Bind one or more domains on Cloudflare and enable **Email Routing** so mail to those domains is routed into D1;
2. Set an **admin password** in the backend (used for `/admin/new_address`);
3. (Optional) If you enable the site access password (needAuth), note the **site password**.

After deploy, gather the following into `config.toml`'s `[temp_email]`:

| You need | Meaning | How to get |
|---|---|---|
| `base_url` | Backend API URL, e.g. `https://mail-api.example.com` | The Worker / backend domain from deploy |
| `domain` | Domain used in mailbox addresses | Any one from `GET /open_api/settings` → `domains[]` |
| `admin_password` | Admin password | Set in the backend admin console |
| `site_password` | Site access password (optional) | Only if `/open_api/settings` reports `needAuth: true` |

## How this project calls the backend

The registrar calls only two endpoints (from `stt.py`'s `temp_email_create` and `poll_parsed_mails`):

**1. Create a mailbox address**

```
POST {base_url}/admin/new_address      # preferred; needs admin_password
POST {base_url}/api/new_address        # fallback path
```

- Body: `{"name": "<random 8 chars>", "domain": "<domain>", "cf_token": "", "enableRandomSubdomain": false}`
- Admin path header: `x-admin-auth: <admin_password>` (bypasses prefix/rate gates; recommended)
- User path header: `x-custom-auth: <site_password>` (only if backend has needAuth on)
- The response carries a JWT used to read the inbox afterwards

With `use_admin_path = true` the admin path is tried first; on 401/403 (missing/invalid admin_password) the script falls back to `/api/new_address`.

**2. Read the verification mail**

```
GET {base_url}/api/parsed_mails?limit=20&offset=0
Authorization: Bearer <JWT returned by create>
```

The script polls every `poll_interval_secs` seconds up to `poll_timeout_secs`, extracting the ElevenLabs verification link (`oobCode`) from `subject`/`text`/`html` and completing verification.

## `[temp_email]` field mapping

| Field | Purpose | Default |
|---|---|---|
| `base_url` | Backend host for all calls | (required) |
| `domain` | Mailbox domain; must be in backend `domains[]` | (required) |
| `admin_password` | `x-admin-auth`, drives `/admin/new_address` | `""` |
| `site_password` | `x-custom-auth`, fallback `/api/new_address` | `""` |
| `use_admin_path` | Prefer the admin path | `true` |
| `poll_interval_secs` | Seconds between inbox polls | `3` |
| `poll_timeout_secs` | Max seconds to wait for the verification mail | `120` |

> [!NOTE]
> `cf_token` is always left empty: this project authenticates via admin/site password and does not trigger the backend's Turnstile gate. `cloudflare_temp_email` v1.9+ requires `name` even on the admin API; the script generates a random one, so you never set it manually.

## Config example

```toml
[temp_email]
base_url = "https://mail-api.example.com"
admin_password = "your-admin-password"
site_password = ""                       # only if backend has needAuth on
domain = "example.com"                   # one of backend /open_api/settings domains[]
use_admin_path = true
poll_interval_secs = 3
poll_timeout_secs = 120
```

## Troubleshooting

- **Admin path 401/403**: wrong `admin_password` or admin not enabled → the script auto-falls back to `/api/new_address`; if both fail, check the password and backend settings.
- **`domain not allowed` / create fails**: `domain` is not in the backend `domains[]` → call `GET /open_api/settings` to see real domains and fill one of those.
- **Backend has needAuth on but `site_password` is empty**: `/api/new_address` also 401s → fill `site_password`, or use `admin_password` via the admin path.
- **Address created but no verification mail arrives**: check backend Email Routing routes that domain's mail into D1; bump `poll_timeout_secs` if the network is slow.
- **`temp_email.base_url and temp_email.domain are required`**: `[temp_email]` is incomplete, or switch to single-account mode via `stt login`.

## Notes

- This project uses only the upstream "create address + read parsed mail" capabilities — it does **not** depend on the upstream frontend, Telegram, SMTP proxy, S3 attachments, etc.
- Self-host the backend; respect the upstream project's and ElevenLabs' terms, and only use domains and accounts you are allowed to use.
- Backend version v1.9+ recommended (the admin API also requires the `name` field).

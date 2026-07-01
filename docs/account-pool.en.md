# Account pool & auto-registration

**English** | [简体中文](./account-pool.md)

This optional mode keeps several ElevenLabs free accounts available and picks the smallest account that can cover each transcription.

> This uses ElevenLabs web flows and a temp-mail backend. Use accounts you are allowed to create, follow service terms, and keep `config.toml` / `accounts.json` private.

## Files

| File | Purpose |
|---|---|
| `config.toml` | Local settings and temp-mail credentials; gitignored |
| `accounts.json` | Account tokens and remaining-credit cache; gitignored |
| `config.example.toml` | Safe example config |

## Configure

Copy `config.example.toml` to `config.toml`, then fill these sections:

```toml
[temp_email]
base_url = "https://mail.example.com"
admin_password = ""
site_password = ""
domain = "example.com"
use_admin_path = true

[accounts]
pool_target = 3
fresh_threshold = 10000
selection_margin = 1.2
auto_refill = true
```

Leave `[temp_email]` empty/commented if you only want single-account mode via `python stt.py login`.

## Commands

```bash
# Show stored accounts and cached remaining credits
python stt.py accounts

# Force-refresh remaining credits from the API
python stt.py accounts --refresh

# Show pool counts
python stt.py pool status

# Register accounts until there are N fresh accounts
python stt.py pool warm --target 10

# Transcribe using best-fit account selection; auto-refill runs after success
python stt.py transcribe audio.m4a --show-cost
```

## Selection rules

- `fresh`: remaining credits >= `fresh_threshold`.
- `usable`: valid account with remaining credits below `fresh_threshold`.
- Transcription estimates the required credits, multiplies by `selection_margin`, then chooses the smallest account that can cover it.
- After a successful transcription, if `auto_refill = true`, the script warms the pool back to `pool_target`.

## Browser cleanup

Auto-registration opens a real Chrome window because selector automation can trigger hCaptcha. For each account, the script:

1. creates a fresh temporary Chrome profile (`--user-data-dir`),
2. activates only the newly-created Chrome window,
3. completes signup + email verification + web sign-in,
4. captures tokens via REST,
5. closes Chrome processes whose command line contains that temp profile name,
6. deletes the temp profile directory.

So a 10-account warm should not leave 10 browsers open. If a run is externally killed, cleanup may not run; remove leftovers with:

```powershell
$p = Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
  Where-Object { $_.CommandLine -like '*elevenlabs-stt-chrome-*' }
$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-ChildItem $env:TEMP -Directory -Filter 'elevenlabs-stt-chrome-*' |
  Remove-Item -Recurse -Force
```

## Troubleshooting

- `temp_email.base_url and temp_email.domain are required`: fill `[temp_email]` or use single-account mode.
- `email has not been verified`: verification modal was not completed; retry `pool warm`.
- Browser opens on your personal account: stop the run; auto-registration should only use a temporary profile. Report/fix before continuing.
- Low memory during large warm: warm one step at a time, e.g. `--target 4`, then `--target 5`.

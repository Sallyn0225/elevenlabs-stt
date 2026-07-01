# Web UI: accounts pagination, back-to-top, real login/register buttons

## Goal

Bring the local web UI (`webui.html` + `web.py`) closer to the Claude Design
handoff for the **账号管理 (accounts)** page:

1. **Pagination** — the accounts list currently renders every row. Add a pager
   (page size 5/10/20/50, "显示第 X–Y 条 · 共 N 个账号", 上一页 / 页码 / 下一页) as
   in the design reference `音频转录字幕.dc.html`.
2. **Back-to-top button** — a fixed round button (bottom-right) on the accounts
   page that appears after scrolling and smooth-scrolls to the top.
3. **Make 登录账号 / 启动注册机 buttons do real work.** Today both open a modal that
   only prints a "run this CLI command" note. Wire them to the real backend flows.

## Decisions (user, 2026-07-02)

- **登录账号** → **REVISED 2026-07-02 (respect the original design):** in-modal
  email+password form → backend logs in via ElevenLabs' Firebase REST endpoint
  (`stt.account_from_password_signin` + `refresh_credits`, the same sign-in tail
  `register_one` uses), fetches credits, and saves the account (`source=manual`) to
  `accounts.json`. No Playwright, no browser popup/redirect. (The initial plan used
  `stt.cmd_login`'s Playwright flow; dropped because it opened a fresh-profile
  browser that redirected through `/app/speech-to-text` and could hang — the REST
  path is faster and matches the design's intent.)
- **启动注册机** → runs the real `pool warm` (`stt.cmd_pool_warm`, i.e. `register_one`
  via the `config.toml → [temp_email]` config).
  - **REVISED 2026-07-02 (respect the original design):** the modal shows the FULL
    design parameter form (base_url / admin_password / site_password / domain /
    use_admin_path /admin|/api / 目标账号数 / poll_interval_secs / poll_timeout_secs),
    pre-loaded from the live `config.toml`. A 保存 button and 开始批量创建 both
    persist the form to `config.toml` (`[temp_email]` + `[accounts].pool_target`)
    via `POST /api/config/save`, so the UI and `config.toml` stay in sync both
    directions. `config.toml` is round-tripped with a minimal in-repo TOML writer
    (`web.py:_dump_toml`, no new dependency), preserving all other sections/keys.
    This supersedes the earlier "no in-browser config form" non-goal below.
- **Blocking UX** → synchronous wait + spinner. Both are long/blocking; the
  ThreadingHTTPServer serves each request on its own thread so `/api/state` polling
  still works. Frontend shows an in-modal spinner until the call returns.

## Scope / Non-goals

- Reuse `stt.cmd_login` and `stt.cmd_pool_warm` as-is; do **not** touch the
  registration/login internals (see backend/quality-guidelines.md contracts).
- No background-task/progress-streaming endpoint (rejected in favor of sync wait).
- Pager renders all page numbers (no ellipsis) — accounts are few for a single
  local user.

## Acceptance criteria

- Accounts page paginates; changing page size / page updates the visible rows and
  the "显示第 X–Y 条 · 共 N" summary; 全选本页 selects only the current page.
- Back-to-top appears on the accounts page after scrolling and scrolls to top.
- Clicking 登录账号 → "打开浏览器登录" opens the real browser flow; on success the
  new account appears in the list. Missing Playwright surfaces a clear toast.
- Clicking 启动注册机 → set target → runs real `pool warm`; new accounts appear.
  Missing `[temp_email]` config surfaces a clear toast.
- `python -m py_compile web.py stt.py` passes; `git diff --check` clean.

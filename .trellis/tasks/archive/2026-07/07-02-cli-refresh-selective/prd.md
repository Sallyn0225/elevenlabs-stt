# CLI selective account credit refresh

## Goal

让命令行 `stt accounts` 支持指定单个或多个账号刷新额度，对齐 WebUI 复选框"只刷新选中账号"的能力。当前 CLI 只能 `stt accounts --refresh` 全局刷新全部非 invalid 账号。

## Background

- WebUI：`refreshSel()` → POST `/api/accounts/refresh {emails}` → `do_refresh(emails)`，`if a.get("email") in wanted` 只刷新选中（`web.py:175`、`webui.html:580/613`）。
- CLI：`stt accounts --refresh` 遍历全部非 invalid 账号 `account_remaining(..., force=True)`（`stt.py:1050` `cmd_accounts`、`stt.py:1163` 参数定义）。无按账号过滤的参数。
- 两个入口不对称：WebUI 精确，CLI 只能全刷。

## Requirements

- 给 `stt accounts` 子命令新增可重复的 `--email`（短选项 `-e`）参数，取值为账号 email。
- `--email` 限定作用范围：
  - 与 `--refresh` 同用时：只对匹配 email 的非 invalid 账号强制刷新额度。
  - 不带 `--refresh` 时：`--email` 过滤列表显示，仅展示匹配账号（用缓存额度，无网络）。
- 不传 `--email` 时：行为与现状完全一致（全刷 / 全列），向后兼容。
- `--refresh --email` 指定了不存在的 email：每个未匹配的 email 在 stderr 打印一行提示，不影响已匹配账号的刷新与退出码。
- `cmd_accounts` 复用现有 `account_remaining(a, store, force=True)` + `cached_remaining(a)`，不新写刷新逻辑。

## Acceptance Criteria

- [ ] `stt accounts --refresh --email a@x.com --email b@x.com` 仅刷新这两个账号（非 invalid），其余账号无网络请求。
- [ ] `stt accounts --email a@x.com` 只列出该账号一行（缓存额度，无网络）。
- [ ] `stt accounts --refresh` 仍刷新全部非 invalid 账号（行为不变）。
- [ ] `stt accounts` 仍列出全部账号（行为不变）。
- [ ] `stt accounts --refresh --email nobody@x.com` 打印未匹配提示到 stderr，正常退出。
- [ ] `stt accounts --help` 显示新增的 `-e/--email` 说明。
- [ ] 改动仅限 `stt.py`，无新增依赖、无新文件。

## Out of Scope

- 不改 WebUI（已具备选择性刷新）。
- 不改 `web.py` 路由。
- 不新增独立子命令（沿用 `accounts` + 参数）。
- 不引入按 email 删除/登录等其它操作（本任务只做额度刷新范围过滤）。

## Notes

- Lightweight task：仅 `stt.py` 单文件、约 3-5 行改动（参数定义 + 过滤判断 + 未匹配提示），PRD-only。

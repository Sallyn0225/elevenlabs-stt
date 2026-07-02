# 临时邮箱后端（cloudflare_temp_email）

[English](./temp-email-backend.en.md) | **简体中文**

多账号自动注册需要一个**临时邮箱后端**来接收 ElevenLabs 的验证邮件。本项目通过 HTTP 调用 [`dreamhunter2333/cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email) 的接口来申请邮箱地址并读取收到的验证邮件。

> [!IMPORTANT]
> 请自部署该后端，不要滥用他人公开实例。本项目与 `cloudflare_temp_email` 无关联，仅复用其 API。

## 它是什么

`cloudflare_temp_email` 是一个基于 Cloudflare 免费服务（Workers + D1 + Pages + Email Routing）搭建的临时邮箱服务，零成本运行，对外提供创建邮箱地址、读取已解析邮件等 API。本项目只用到其中两个能力：**创建地址**和**读取收件箱**，不依赖其前端、Telegram、SMTP 等功能。

## 前置：部署后端

按上游的[官方部署文档](https://temp-mail-docs.awsl.uk)（或 [GitHub Action 一键部署](https://temp-mail-docs.awsl.uk/zh/guide/actions/github-action.html)）搭好后端，完成：

1. 在 Cloudflare 上绑定一个或多个域名，开启 **Email Routing** 把该域名的邮件路由进 D1；
2. 在后端设置一个**管理员密码**（admin password），用于 `/admin/new_address`；
3.（可选）若开启了站点访问密码（needAuth），记下**站点密码**（site password）。

部署完成后，你需要从后端拿到以下信息填入 `config.toml` 的 `[temp_email]`：

| 需要拿到 | 说明 | 怎么获取 |
|---|---|---|
| `base_url` | 后端 API 地址，如 `https://mail-api.example.com` | 部署时的 Worker / 后端域名 |
| `domain` | 用于邮箱地址的域名 | 后端 `GET /open_api/settings` 返回的 `domains[]` 里任选一个 |
| `admin_password` | 管理员密码 | 部署时在后台设置的 admin password |
| `site_password` | 站点访问密码（可选） | 仅当 `/open_api/settings` 的 `needAuth` 为 true 时需要 |

## 本项目如何调用后端

注册机只调用两个端点（均来自 `stt.py` 的 `temp_email_create` 与 `poll_parsed_mails`）：

**1. 创建邮箱地址**

```
POST {base_url}/admin/new_address      # 优先，需 admin_password
POST {base_url}/api/new_address        # 回退路径
```

- 请求体：`{"name": "<随机8位>", "domain": "<domain>", "cf_token": "", "enableRandomSubdomain": false}`
- admin 路径请求头：`x-admin-auth: <admin_password>`（绕过前缀/限流闸门，推荐）
- 用户路径请求头：`x-custom-auth: <site_password>`（仅当后端开启 needAuth）
- 返回里含一个 JWT，后续用它读收件箱

`use_admin_path = true` 时优先走 admin 路径；若返回 401/403（admin_password 缺失或无效），脚本自动回退到 `/api/new_address`。

**2. 读取验证邮件**

```
GET {base_url}/api/parsed_mails?limit=20&offset=0
Authorization: Bearer <创建地址返回的 JWT>
```

脚本每 `poll_interval_secs` 秒轮询一次，直到超时 `poll_timeout_secs`，从 `subject`/`text`/`html` 里提取 ElevenLabs 的验证链接（`oobCode`）并完成验证。

## `[temp_email]` 字段映射

| 字段 | 用途 | 默认 |
|---|---|---|
| `base_url` | 后端地址，所有调用的 host | （必填） |
| `domain` | 邮箱域名，须来自后端 `domains[]` | （必填） |
| `admin_password` | `x-admin-auth`，走 `/admin/new_address` | `""` |
| `site_password` | `x-custom-auth`，回退走 `/api/new_address` | `""` |
| `use_admin_path` | 是否优先走 admin 路径 | `true` |
| `poll_interval_secs` | 轮询验证邮件间隔秒数 | `3` |
| `poll_timeout_secs` | 等待验证邮件的最长秒数 | `120` |

> [!NOTE]
> `cf_token` 固定留空：本项目走 admin/site 密码鉴权，不触发后端的 Turnstile 人机验证闸门。`cloudflare_temp_email` v1.9+ 要求创建地址时必须带 `name`，脚本已自动生成随机名，无需手动填写。

## 配置示例

```toml
[temp_email]
base_url = "https://mail-api.example.com"
admin_password = "your-admin-password"
site_password = ""                       # 仅当后端开启 needAuth 才填
domain = "example.com"                   # 后端 /open_api/settings 里 domains[] 之一
use_admin_path = true
poll_interval_secs = 3
poll_timeout_secs = 120
```

## 排错

- **admin 路径 401/403**：`admin_password` 错或后端未启用 admin → 脚本会自动回退 `/api/new_address`；若两条路都不通，检查密码与后端设置。
- **`domain not allowed` / 创建失败**：`domain` 不在后端 `domains[]` 里 → 调 `GET /open_api/settings` 看实际可用域名，改填其中一个。
- **后端开启 needAuth 但没填 `site_password`**：`/api/new_address` 也会 401 → 填 `site_password`，或改用 `admin_password` 走 admin 路径。
- **地址创建成功但收不到验证邮件**：检查后端 Email Routing 是否把该域名的邮件正确路由进 D1；网络慢时调大 `poll_timeout_secs`。
- **`temp_email.base_url and temp_email.domain are required`**：`[temp_email]` 没填完整，或改用 `stt login` 单账号模式。

## 注意事项

- 本项目只用上游「创建地址 + 读已解析邮件」两个能力，**不依赖**其前端、Telegram、SMTP proxy、S3 附件等功能。
- 请自部署后端，尊重上游项目与 ElevenLabs 的服务条款，只用你有权使用的域名与账号。
- 后端版本建议 v1.9 及以上（admin API 也要求 `name` 字段）。

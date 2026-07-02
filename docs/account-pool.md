# 多账号额度池与自动注册

[English](./account-pool.en.md) | **简体中文**

这个可选模式会维护多个 ElevenLabs 免费账号，并在转录时选择“刚好够用、剩余额度最小”的账号，减少浪费。

> 该模式会复用 ElevenLabs 网页流程和临时邮箱后端。请只创建你有权使用的账号，遵守服务条款，并保护好 `config.toml` / `accounts.json`。

## 文件

| 文件 | 说明 |
|---|---|
| `config.toml` | 本地配置和临时邮箱凭证；已 gitignore |
| `accounts.json` | 账号 token 与额度缓存；已 gitignore |
| `config.example.toml` | 安全示例配置 |

## 邮箱后端

自动注册需要一个自部署的 [`cloudflare_temp_email`](https://github.com/dreamhunter2333/cloudflare_temp_email) 后端来接收验证邮件。部署步骤与本项目调用哪些端点见 [`temp-email-backend.md`](./temp-email-backend.md)。下方 `[temp_email]` 的 `base_url` / `domain` / `admin_password` / `site_password` 都来自该后端。

## 配置

复制 `config.example.toml` 为 `config.toml`，填写：

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

如果只想使用 `python stt.py login` 导入的单账号模式，留空或注释掉 `[temp_email]` 即可。

## 常用命令

```bash
# 查看账号和缓存额度
python stt.py accounts

# 强制从 API 刷新额度
python stt.py accounts --refresh

# 只刷新指定账号（可重复 -e 指定多个）；其余账号不发网络请求
python stt.py accounts --refresh -e a@example.com -e b@example.com

# 不带 --refresh 时，--email 只按邮箱过滤列表（用缓存额度，不联网）
python stt.py accounts -e a@example.com

# 查看 fresh / usable / depleted 数量
python stt.py pool status

# 注册账号直到 fresh 数达到 N
python stt.py pool warm --target 10

# 转录时自动选择账号；成功后按配置自动补池
python stt.py transcribe audio.m4a --show-cost
```

## 选择规则

- `fresh`：剩余额度 >= `fresh_threshold`。
- `usable`：账号有效，但剩余额度低于 `fresh_threshold`。
- 转录前会估算所需积分，乘以 `selection_margin`，再选择满足条件且剩余额度最小的账号。
- 转录成功后，如果 `auto_refill = true`，脚本会把 fresh 数补回 `pool_target`。

## 浏览器清理

自动注册必须打开真实 Chrome，因为纯 selector 自动化容易触发 hCaptcha。每注册一个账号，脚本会：

1. 创建新的临时 Chrome profile（`--user-data-dir`）；
2. 按临时 profile 的进程句柄识别新窗口并将其带到前台（不会碰你已打开的个人 Chrome，个人浏览器可以保持打开）；
3. 完成注册、邮件验证、网页登录；
4. 用 REST 捕获 token；
5. 关闭命令行中包含该临时 profile 名的 Chrome 进程；
6. 删除临时 profile 目录。

注册过程通过真实键鼠输入驱动临时窗口，期间请不要使用本机键盘鼠标；若临时窗口无法确认获得焦点，脚本会在发送任何按键前直接中止。

所以预热 10 个账号不应该留下 10 个浏览器。如果运行被外部强杀，Python 的 `finally` 可能来不及执行，可手动清理：

```powershell
$p = Get-CimInstance Win32_Process -Filter "name='chrome.exe'" |
  Where-Object { $_.CommandLine -like '*elevenlabs-stt-chrome-*' }
$p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-ChildItem $env:TEMP -Directory -Filter 'elevenlabs-stt-chrome-*' |
  Remove-Item -Recurse -Force
```

## 排错

- `temp_email.base_url and temp_email.domain are required`：填写 `[temp_email]`，或改用单账号模式。
- `email has not been verified`：验证弹窗没有完成；重试 `pool warm`。
- `auto-register could not focus the temporary Chrome window`：Windows 拒绝把临时窗口切到前台（如有全屏应用占用时）；脚本已在发送按键前安全中止，关闭全屏程序后重试即可。
- 浏览器打开了你的个人账号：立刻停止；自动注册应该只使用临时 profile。
- 大量注册导致内存压力：逐步预热，例如先 `--target 4`，再 `--target 5`。

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

# 强制从 API 刷新额度（8 线程并发，全部完成后一次性写盘；
# stderr 的 refreshed 进度行按完成先后输出，最终列表顺序不变）
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

## 并发与注册流水线

- 分配完成后按账号分组执行：**同一账号内的任务串行，不同账号并行**，并发上限为 `config.toml → [transcribe].max_concurrency`（默认 4，设为 `1` 即回退串行）。
- 相邻两次上传起始至少间隔 `[transcribe].stagger_secs` 秒（默认 2.0，`0` 关闭），降低同 IP 突发并发被风控的概率。
- 缺口账号走**注册-转录流水线**：已分配到现有账号的任务立即开始转录；注册（天然串行，独占键鼠）在旁路推进，每注册好一个账号，绑定其槽位的任务立刻投入转录，不等剩余注册完成。
- 注册中途失败：停止后续注册，未注册槽位的任务标 FAIL（汇总里带原因），已在跑的任务照常完成，已注册成功的账号照常落盘。

### 手动限定候选集（`--account` / WebUI 高级面板）

- `stt transcribe ... --account x@y.z`（可重复）或 WebUI 转录页的「高级 · 手动指定账号」面板，会把分配候选集限定为所选账号，best-fit 只在集合内进行；单文件 + 单账号即精确指定。
- 邮箱不存在或账号已失效会在开始前报错。
- **手动模式不触发自动注册**：所选账号装不下全部文件（含切分段）时直接报错退出，而不是注册新账号——增选账号或回到自动分配即可。转录后的 `auto_refill` 补池行为不受影响，照常执行。

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

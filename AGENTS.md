<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

# Design Context

前端设计工作前先读 `PRODUCT.md`（战略/语气）与 `DESIGN.md`（视觉体系）。要点：

- **Register**：`product`——工具型 UI，设计服务于转录/账号/切分等操作，密度紧凑、以效率与可读性为先。
- **气质**：工程师终端感（精确、克制、功能优先）。等宽标签 + 单色值阶 + hairline 边框，把命令行的可信感搬进网页。
- **颜色即状态**：色彩预算只留给成功(绿)/失败(红)/警告(琥珀)三态，其余交给 `#171717→#666` 的值阶；渐变、彩色仪表盘、装饰性色彩视为噪声。
- **默认收起复杂度**：手动指定账号、切分高级参数等专家开关默认折叠。
- **无障碍**：基础达标(WCAG AA)——正文对比度 ≥4.5:1、全键盘可用、`prefers-reduced-motion` 降级；深浅两套主题各自校验对比度。
- **视觉体系**：见 `DESIGN.md`（Vercel 单色终端风，Geist Sans/Mono，6px 圆角，hairline 边框而非阴影）。`webui.html` 即此体系的实现。

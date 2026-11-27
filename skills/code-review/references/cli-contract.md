# `code_review.py` CLI 契约与多代理映射说明

脚本位于技能目录 `scripts/code_review.py`。

## 1. 三端本地代理与权限映射（`--agent`）

| `--agent` 取值 | 本机可执行文件自动定位 | 只读模式（`plan` / `code` / `discuss`） | 写入执行模式（`exec`） | 默认配置来源（未传 `--model`/`--effort` 时） |
| :--- | :--- | :--- | :--- | :--- |
| **`codex`（默认）** | `@openai/codex-win32-*/.../codex.exe` | `codex exec -s read-only` | `codex exec -s workspace-write` | `~/.codex/config.toml` |
| **`claude`** | `@anthropic-ai/claude-code/bin/claude.exe` | `claude -p --tools Read,Glob,Grep,Bash --disallowedTools Edit,Write,NotebookEdit` | `claude -p --permission-mode acceptEdits` | `~/.claude/settings.json` |
| **`agy`** | `%LOCALAPPDATA%/agy/bin/agy.exe` 或 `~/.gemini/bin/agy.exe`（Windows 下若未设代理环境变量自动检测系统 `Internet Settings` 代理） | `agy --mode plan --sandbox -p=...` | `agy --mode accept-edits --dangerously-skip-permissions -p=...` | `~/.gemini/antigravity-cli/settings.json` |

---

## 2. 六大子命令速查

```powershell
# 1) 确定性 6 门文件筛选预览（不调用模型）
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" preview --repo . [--from main] [-c <sha>]

# 2) 讨论或起草方案（只读，支持 --resume-id 多轮追问、--out-file 保存方案初稿）
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" discuss --repo . --agent <codex|claude|agy> -p "<主题>" [--out-file <plan.md>] [--resume-id <id>]

# 3) 结构化方案审查（只读，默认 codex 1 轮，最高 5 轮）
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" plan --repo . --plan-file <plan.md> --goal "<需求>" [--agent codex|claude|agy] [--max-rounds 1]

# 4) 委派执行 TODO 计划（允许工作区写入，返回 changed_files 列表）
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" exec --repo . --agent <agy|claude|codex> --plan-file <plan.md> [-g "<约束>"]

# 5) 结构化代码与计划落实验收（只读，默认 codex 1 轮，最高 5 轮）
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" code --repo . [--from main] [-c <sha>] [--plan-file <plan.md>] -b "<背景>" [--agent codex|claude|agy] [--max-rounds 1]

# 6) 导出审查会话总结
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" summary --repo . --session <session_id> [-f markdown|json]
```

---

## 3. 审查门禁动作（`gate_action`）

- **`DELIVER_PASS`**：`issue_count == 0`，直接进入下一阶段或交付总结。
- **`BATCH_FIX_AND_DELIVER`**：默认单轮模式（`max_rounds == 1`）检出问题 → 一次性全部修复并通过本地验证后直接交付。
- **`BATCH_FIX_AND_RETRY`**：多轮模式（`max_rounds > 1`）未满轮 → 一次性全部修复后带 `--session <session_id>` 继续下一轮。
- **`DELIVER_AT_MAX_ROUNDS`**：多轮模式达到轮次上限仍有未决项 → 停止循环并交付遗留清单。

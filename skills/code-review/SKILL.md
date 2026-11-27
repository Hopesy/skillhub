---
name: code-review
description: "本地三端代理（Codex 默认 / Claude Code / Antigravity CLI agy）协同闭环技能：支持跨代理讨论与起草方案（discuss）、结构化方案审查（plan）、委派执行 TODO 计划（exec）及代码漏洞验收（code）。未指定模型时直接沿用目标代理的本地默认配置；审查默认 1 轮并将检出问题一次性全部修复交付（支持最高 5 轮复审），调用其他 Agent 过程中必须实时监控并随时汇报其进度。"
license: MIT
metadata:
  author: Hopesy
  version: "2.1.0"
---

# 本地多代理协同闭环（Codex × Claude Code × Antigravity `agy`）

通过 [code_review.py](scripts/code_review.py) 在三个本地 CLI 代理之间按各自模型专长分工协同（均无需额外配置 API Key，直接复用本机登录态与默认模型配置）：

| 本地代理 (`--agent`) | 本机默认定位 | 最擅长的角色分工 |
| :--- | :--- | :--- |
| **`codex`（默认）** | 高强度深层推理（如 `gpt-6.2-astra` `xhigh`） | **首席审查与找茬验收**（`plan` / `code`）、复杂并发/边界死角推演（`discuss`） |
| **`agy`（Antigravity CLI）** | 宏观架构与干净编码（如 `Gemini 4 Carbon (High)`） | **起草清爽架构方案**（`discuss`）、**按 TODO 编写模块化非过度防御代码**（`exec`） |
| **`claude`（Claude Code）** | 均衡工程推理与交叉验证 | **备选审查官**、跨视角方案对撞（`discuss`）或编码委派（`exec`） |

详细审查红线见 [review-rubric.md](references/review-rubric.md)；完整子命令与 JSON 契约见 [cli-contract.md](references/cli-contract.md)。

---

## 全局硬性守则

1. **全流程与跨代理调用期间随时汇报进度（严禁静默干等）**：
   - **关键节点汇报**：启动方案讨论/起草、发起代理审查、执行一次性批量修复或委派 `exec`、以及完成交付等每个节点，**必须随工具调用用 1–2 句话向用户简明同步当前动作、问题数与下一步**。
   - **调用其他 Agent 过程中随时监控汇报**：跨代理调用（`discuss` / `plan` / `exec` / `code`）通常耗时数十秒至数分钟，**严禁同步长阻塞静默等待直至结束**。主 Agent 必须以异步/后台方式启动该命令并持续监控运行状态（如设置周期通知唤醒或轮询查看后台任务状态、`.<项目名>-temp/code-review/` 下的过程日志及工作区改动），在等待期间**随时向用户简明总结汇报被调 Agent 当前的实时进度**（如正在推进的阶段、正在阅读或修改的模块、已耗时情况等），直至子代理执行完毕。
2. **默认后端与默认模型**：
   - 未指定 `--agent` 时默认调用 **`codex`**；需要调用 Claude Code 或 Antigravity CLI 时传 `--agent claude` 或 `--agent agy`（如当前已在 Codex 会话内工作，想让 Gemini 出方案或写代码避免过度防御，即可调 `--agent agy`）。
   - 用户未点名具体模型/强度时，**不传 `--model` / `--effort`**，直接沿用目标代理自身的本地默认配置。
3. **默认 1 轮审查 + 一次性全部修复（按需最高 5 轮）**：
   - 默认 `--max-rounds 1`：单轮审查返回 `DELIVER_PASS`（0 问题）直接放行；返回 `BATCH_FIX_AND_DELIVER` 时，执行方必须在**单次批次内一次性修完所有有效问题并通过本地验证后直接交付**。
   - 用户明确要求多轮清零复审时传 `--max-rounds 2..5`，按 `BATCH_FIX_AND_RETRY` 循环复审直至 0 问题或满轮。
4. **反过度防御裁剪**：审查阶段的假想防御噪音已自动分流至 `suppressed_over_defensive_notes`（无需修改）；执行或修复代码时保持简洁模块化，禁止堆砌内部调用链冗余判空与吞错 `try/except`。
5. **静默无弹窗与过程目录**：统一调用本技能内的 `scripts/code_review.py`（自动定位当前技能目录，已封装 `CREATE_NO_WINDOW`、只读/写入沙箱隔离及超时进程树清理）；过程记录统一保存在 `.<项目名>-temp/code-review/`。

---

## 四大协同子命令与典型编排

以下 `<SKILL_DIR>` 指本 `SKILL.md` 所在目录（例如 `~/.gemini/config/skills/code-review`）。调用任一跨代理子命令时，均须按守则 1 后台监控并随时向用户同步被调 Agent 的执行进展。

### 1. 跨代理讨论或起草方案（`discuss`，只读不改文件）
当需要调用另一代理参与技术对撞，或在 Codex/Claude 会话中委派 `agy` 起草精简方案时：
```powershell
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" discuss --repo <工作区> --agent <codex|claude|agy> -p "<讨论主题或需求>" [--out-file <方案保存路径>] [--resume-id <上轮agent_session_id>]
```

### 2. 结构化方案深度审查（`plan`，只读）
方案初稿就绪后，调用审查代理（默认 `codex`）核查契约冲突、遗漏边界与过度设计：
```powershell
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" plan --repo <工作区> --plan-file <方案路径> --goal "<原始需求与约束>" [--agent codex|claude|agy]
```
收到 `BATCH_FIX_AND_DELIVER` 后**一次性修订方案中所有有效问题**并定稿 TODO 清单。

### 3. 执行 TODO 计划（宿主直接实现，或 `exec` 跨代理委派实现）
- **在 Antigravity（Gemini）内工作时**：由当前宿主直接按定稿 TODO 模块化编码并跑通自检。
- **在 Codex 等其他终端内工作、希望委派 `agy`（或 `claude`）写代码以避免过度防御时**：
  ```powershell
  python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" exec --repo <工作区> --agent agy --plan-file <定稿方案路径> [-g "<补充约束>"]
  ```
  命令返回实际修改的 `changed_files` 列表与执行报告。

### 4. 代码修改与计划落实验收（`code`，只读）
编码与本地自检完成后，执行确定性 6 门过滤与只读漏洞验收（默认 `codex`）：
```powershell
python -B -X utf8 "<SKILL_DIR>/scripts/code_review.py" code --repo <仓库目录> [--from <base>] [-c <sha>] [--plan-file <定稿方案路径>] -b "<任务目标>" [--agent codex|claude|agy]
```
- `DELIVER_PASS`（0 问题）：直接交付最终总结。
- `BATCH_FIX_AND_DELIVER`（默认单轮检出问题）：**一次性修复全部有效 `findings` 并跑通本地测试**，确认通过后交付最终总结（若跨代理委派修复，可再次用 `exec` 一次性传入修复清单）。

---

## 最终总结交付格式

向用户交付最终总结时，简明包含：
1. **协同与收敛概况**：各阶段参与的本地代理（`codex` / `claude` / `agy`）及生效模型、执行轮次与最终状态。
2. **核心修正清单**：方案与代码验收中抓出并一次性解决的关键漏洞或设计缺口（附源码文件点击链接），以及已过滤的过度防御噪音。
3. **验证证据与待决项**：本地构建/单测结果及任何需人工拍板的边界取舍。

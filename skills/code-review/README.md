# code-review

驱动本地三端 CLI 代理（**Codex** 默认、**Claude Code**、**Antigravity CLI `agy`**）按各自模型专长分工协同：支持方案讨论起草（`discuss`）、结构化方案审查（`plan`）、跨代理执行 TODO 计划（`exec`）及确定性代码漏洞验收（`code`）。无需额外配置 API Key，直接复用本机已有登录态与默认模型配置；在调用其他 Agent 过程中由主 Agent 全程后台监控并随时汇总汇报进度。

## 典型用法

### 1. 在 Antigravity（Gemini）中开发 + 调用 Codex 审查验收

```text
使用 code-review 开发并验收当前需求：
先起草精简方案交由 Codex 审查，一次性修订后执行 TODO 编码实现，最后用 Codex 验收抓漏洞并一次性修完所有问题后交付。
```

仅审查当前工作区改动或指定分支：

```text
使用 code-review 审查当前未提交改动（或对比 main 分支），把审出来的问题一次性全部修复并通过测试。
```

### 2. 在 Codex 中工作 + 委派 `agy`（Gemini）出方案/写代码避免过度防御

输入 `$code-review`，将方案起草与编码委派给 `agy`，由 Codex 专注深审把关：

```text
使用 $code-review 协同完成本次改造：
先通过 --agent agy 讨论起草精简方案，审查修订后用 exec --agent agy 执行 TODO 实现，最后完成代码漏洞验收。
```

### 3. 切换 Claude Code 或开启多轮清零复审

- **切换审查/执行后端**：默认不传 `--agent` 即调用 `codex`，按需指定 `--agent claude` 或 `--agent agy`；未指定模型时直接沿用目标代理本地默认配置。
- **审查轮次策略**：默认 **1 轮深度审查（`--max-rounds 1`）+ 一次性全量修复验证交付**；对高风险模块可要求“开启最多 3–5 轮复审直至 0 问题”（`--max-rounds 2..5`）。
- **跨代理进度随时同步**：调用其他 Agent 期间主 Agent 以异步/后台方式持续监控子代理运行状态与过程日志，随时向用户简明汇报被调 Agent 的进展，避免长时间黑盒等待。

## 核心工程机制

- **前置确定性 6 门过滤**：基于 `git diff --raw -z` 与 `--literal-pathspecs` 采集暂存、未暂存与未跟踪文件，自动硬隔离 `.env`/密钥文件、符号链接越界读取、`.*-temp` 过程目录、锁文件与构建产物。
- **物理只读隔离与无窗口调度**：审查与讨论阶段强制锁定只读沙箱（`codex -s read-only` / `claude` 只读工具白名单 / `agy --mode plan --sandbox`），直连原生 `.exe` 配合 `CREATE_NO_WINDOW` 与超时进程树回收，零终端弹窗。
- **反过度防御分流 + 滑窗行号重定位**：通过独立 `suppressed_over_defensive_notes` 字段隔离内部调用链冗余判空与吞错兜底冲动，保留连续整行精确匹配修正偏移行号。

## 参考

- [技能执行协议](SKILL.md)
- [双模型分工与反过度防御红线](references/review-rubric.md)
- [CLI 子命令与 JSON 契约](references/cli-contract.md)

回归测试在仓库根目录执行：

```bash
python -B -X utf8 -m unittest discover -s skills/code-review/scripts -p "test_*.py" -v
```

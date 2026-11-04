# 按需检索与上下文恢复

找历史依据、核对决定变化或恢复上下文时读取。优先顺序：当前计划和相关代码 → 现行主题文档 → 相关 history → 原始会话。已有充分证据时直接推进。

## 两跳读取

使用本 skill 的 `scripts/memory.py`（Python 3.10+，标准库）。以下 `<skill>` 替换为实际安装目录，`<project>` 为项目根；脚本随 skill 使用。

```text
python <skill>/scripts/memory.py --project <project> recall "导出 对象 ID"
python <skill>/scripts/memory.py --project <project> read "<返回的ref>"
```

接入后从标准架构、design、当前计划和 history 召回依据，completed 计划也参与检索。脚本可发现根目录指令、docs/doc 及已有 .trellis 内容；遗留文档按 [existing-project.md](existing-project.md) 收拢。

自定义文档用全局参数 `--source 相对路径` 补充；recall/read 使用相同来源参数。扫描优先处理显式来源；大项目出现截断提示时，用 `--source` 指定相关主题或模块缩小定位。Git 忽略规则生效，符号链接限定项目内，扫描有文件数、字节数和深度上限，部分扫描会给出 warnings。

recall 默认三条、每条最多 400 字符摘录、总输出预算 3,000 字符；read 最多接收 30 个引用，整批预算 4,000 字符。`--max-chars` 调整预算。预算计算整个 JSON 输出，结果携带路径、行号、状态和引用。证据足够就使用摘录；缺少条件或上下文时展开。

引用绑定项目、来源位置和 SHA-256 内容指纹。文档按整篇校验，对话按命中轮次及邻轮校验。来源变化返回 `ref-changed`，重新 recall；哈希校验内容一致性，采纳决定仍核对用户要求、来源和条件。`needs-review` 项先查明争议。排序先匹配标识符、正文和主题，再结合状态；对话同等证据优先用户及后续轮次。明确后继在截断前提升，文件 mtime 不参与规则判定。

## 原始会话与最近约束

优先使用宿主提供的会话历史工具，核对真实项目和会话身份。已有 mini-trellis 时也可使用它的 `mem recall/read/context`。按真实工具能力选用入口。

需要离线回放或行为评测时，脚本接受显式 `--transcript 项目内路径.jsonl`。这是可选的脱敏交换数据，由调用者按需准备：首行 `{"project":"项目绝对路径"}`；其后逐行 `{"role":"user|assistant","text":"原文"}`；压缩位置用 `{"kind":"compact"}`。只处理显式指定的文件，所有行均需符合约定。

```text
python <skill>/scripts/memory.py --project <project> --transcript tmp/session.jsonl recall "对象 ID"
python <skill>/scripts/memory.py --project <project> --transcript tmp/session.jsonl context
```

context 选最近的有限轮次，跨压缩标记保留最新消息，并为最后一条用户消息预留位置；长用户消息优先保留末尾补充，摘录的 ref 指向相同位置。按轮预留预算，优先保住最新约束。查询中单纯复述问题的用户消息降低权重，纠正和确认保留。回放文本作为证据核对，使用后按项目产物规则处理。

## 事件入口和去重

```text
python <skill>/scripts/memory.py --project <project> entry --host generic --session <id> --event startup --plan docs/exec-plans/active/current.md
```

返回短导航和查找入口，正文按需读取。事件可选 startup/compact/clear/resume/fork：同一项目、宿主、会话的相同 startup 内容跳过；其余事件重新提供入口；会话身份缺失或缓存损坏时提供入口。`--plan` 由调用者提供真实当前计划，省略时提示从项目导航定位。

去重账本位于用户缓存目录的 harness-run/context/，只写事件、状态、长度和哈希；`--state-dir` 可指定隔离缓存。缓存写入失败仍返回入口及警告。该缓存仅服务入口去重，项目事实继续由现有文档维护。

`scripts/session_start.py --host <宿主名> --project <project>` 接收 stdin 的 `cwd/session_id/source`，输出 SessionStart 的 `hookSpecificOutput.additionalContext`；去重时输出 `{}`。注册绑定项目根，从子目录启动仍导航到同一项目；cwd 在其他项目时输出 `{}`。

## 首次注册项目 hook

完成项目 AGENTS.md 的导航与记录约定后，使用当前宿主对应的 skill 安装目录执行：

```text
python "<skill>/scripts/install_hooks.py" --project "<project>" --host codex --session-cwd "<宿主会话目录>"
```

Claude Code 将 `codex` 改为 `claude`。会话目录从宿主环境或任务元数据取得；工具执行命令时的 cwd 单独指定。脚本采用当前 Python 解释器和共享 skill 脚本的绝对路径，合并下列项目文件：

| 宿主 | 项目配置 |
|---|---|
| Codex | `.codex/hooks.json` |
| Claude Code | `.claude/settings.json` |

保留其他设置和 hooks，以固定 `statusMessage` 标识自身条目；重复执行复用，路径变化时定点更新。已有配置解析异常时保留原文件并报告原因。用户级宿主配置保持原样。Codex 已有 TOML hooks 时会提示两份来源合并加载，核对已有条目即可。

可选 `--plan 项目内相对路径` 绑定已确定的当前计划；通常省略，沿用 AGENTS/README 的动态导航。`--check` 只核对注册，输出 `unchanged` 表示配置一致，`needs-update` 表示需更新；`session_scope` 单独报告会话目录是否位于项目内。目录不同返回 `outside-project`，检查以非零退出；省略目录为 `unverified`。安装目录、Python 或项目位置变化后重新注册。

注册后回读配置并执行 `--check`，核对项目和脚本路径；升级后再次显式调用 skill 时检查现有注册，更新有差异的条目。用实际注册命令输入 stdin 事件，核对首次注入、相同 startup 去重、恢复重发及 stderr。出现 `state-write-failed` 时核查该 Python 对缓存的写入能力；必要时用已验证可写的解释器重新注册，保留现有计划和其他 hook，再复测。随后在宿主 SessionStart 核对实际注入。Codex 项目层与 hook 定义需要宿主信任，CLI 用 `/hooks` 查看和完成审核；Claude Code 按项目配置加载与信任提示启用。报告分别写明配置、脚本测试和真实触发状态。

只注册 SessionStart 的 startup/resume/clear/compact/fork。Hook 输出短导航，agent 按 AGENTS.md 在用户修正和重要节点维护文档；下一会话据此读取接续摘要。Claude Code 使用直接执行的 command/args；Codex 的 Windows 命令使用编码 PowerShell 传参，兼容路径中的空格和特殊字符。

宿主契约参考：[Codex hooks](https://developers.openai.com/codex/hooks)、[Claude Code hooks](https://code.claude.com/docs/en/hooks)。

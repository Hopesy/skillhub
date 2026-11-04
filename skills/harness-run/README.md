# harness-run

自主实现、测试和真实验收，以简洁文档保留记忆，持续推进长程任务。在目标项目中调用，说明目标和约束即可。

## 新建项目

```text
使用 harness-run 新建项目。
开发一个本地数据编辑工具，支持导入、编辑保存、导出并重新打开。
请初始化项目，自主规划、实现和验收，保持文档简洁并保存重要决定。
```

仅需初始化时：`使用 harness-run 初始化新项目，本次完成基础文档、导航和模板。`

## 现有项目

只需接入当前项目时，输入 `$harness-run`（Claude Code 使用 `/harness-run`）。Agent 会结合代码、测试和最新要求，将旧文档的有效内容整合到标准文档，更新引用并清理旧文件，再注册当前宿主的项目级 hook。已接入项目增量维护并收拢遗留文档，中断后从当前计划继续。

```text
使用 harness-run 完善当前项目。
修复导入导出流程，保持现有格式兼容、对象 ID 和关联关系完整。
请保留有效实现和项目约定，整理已有资料，补齐必要测试，执行真实验收并维护接续信息。
```

默认文档位置：

```text
AGENTS.md                         # 短导航与记录约定
docs/
├── ARCHITECTURE.md                # 实际架构、模块、依赖与数据流
├── design/                       # 现行专题方案、业务规则、接口契约
├── exec-plans/
│   ├── README.md                  # 计划索引
│   ├── active/                   # 当前目标、TODO、进度、接续摘要
│   ├── completed/                # 已完成计划
│   └── tech-debt-tracker.md       # 当前目标外的已证实问题
└── histories/                    # 已交付变更与验收摘要
```

同主题内容合并，专题放进 `design/`，TODO 和进度放进执行计划，交付结果归入 history。更新链接、脚本和配置并核验后，清理旧文件、跳转入口及迁移副本，统一维护新体系；接入过程不创建 `archive/` 或独立迁移账本。用户约束、未完成事项和重要决定完整承接，历史版本通过 Git 追溯。

用户要求单独记录时可直接说：`把导出兼容规则单独写到 docs/design/export-compatibility.md，并在架构中链接。` 指定名称和路径优先。

## 接续任务

```text
使用 harness-run 接续当前项目的已有任务。
读取当前计划，核对最新约束、已验证结果和工作区变化，继续下一项工作。
```

知道计划位置时直接附上路径。Codex 可显式调用 `$harness-run`；长程任务可补充“请创建持续目标”，由宿主提供原生 goal 能力。

接入后，日常开发可直接描述需求；agent 按项目 `AGENTS.md` 持续保存用户修正、重要决定和已验证进度。新会话、恢复或压缩后，hook 提供短导航，agent 再读取当前计划。切换宿主时在该宿主触发一次 skill，补齐它的项目 hook。

配置位置：Claude Code 为 `.claude/settings.json`，Codex 为 `.codex/hooks.json`。注册脚本保留已有设置并复用自身条目；首次加载按宿主提示完成项目/hook 信任，Codex CLI 可用 `/hooks` 检查。详见 [注册说明](references/memory.md#首次注册项目-hook)。

安装或升级后核对宿主技能列表和实际版本。默认允许按任务相关性发现 skill，也支持 `$harness-run` 显式调用。排查清单缺项时核对安装位置和 `agents/openai.yaml` 的调用策略；`allow_implicit_invocation: false` 会让技能默认从模型上下文清单中省略。

## 参考

- [执行协议](SKILL.md)
- [记录与接续](references/conventions.md) · [记忆工具](references/memory.md)
- [测试和真实验收](references/verification.md)
- [项目初始化](references/project-setup.md) · [基础模板](references/init-files.md)
- [现有项目资料整理](references/existing-project.md)
- [原生持续执行](references/native-runtime.md)

工具回归测试在仓库根目录执行：

```text
python -m unittest discover -s skills/harness-run/tests -v
```

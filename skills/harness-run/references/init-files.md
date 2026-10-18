# 初始化文件内容参考

本文件提供 harness-run 的基础文件与可选专题模板，按项目缺口选取。先按 [project-setup.md](project-setup.md) 判断需要哪些文件，已有等价内容不重复生成。将 `{{PROJECT}}` 等项目字段按真实信息实例化；`{{YEAR}}` 和 `{{AUTHOR}}` 仅在项目许可已确定时填写。

模板不预置 Makefile 或业务脚本；实施时按实际项目需要建立构建、测试与验收入口。模板文件中的填写提示供复用，生成的项目说明不得残留占位。按下方文件名定位对应章节即可，无需每次通读。

协作指南通常写一次，当前计划、架构和同一任务 history 按重要变化维护。

---

## 根目录文件

### AGENTS.md

```markdown
# {{PROJECT}}

本文件是项目导航；沿用当前任务的计划和已读上下文，按需读取：

- `docs/REPO_COLLAB_GUIDE.md`：首次接手或协作规则变化时读。
- `docs/ARCHITECTURE.md`：了解模块和数据流；只读当前任务涉及部分。
- `docs/PLANS_GUIDE.md`：需要建立或恢复复杂任务计划时读；现有计划在 `docs/exec-plans/active/`。
- `docs/HISTORY_GUIDE.md`：实质变更任务交付或阶段留档时读。

复杂任务沿用一份执行计划；同一任务沿用一份 history。进度在里程碑、重要阻塞和交接时更新，架构随结构事实变化更新。无需每轮重读指南、改写计划或追加历史。
```

### CLAUDE.md

```markdown
项目协作入口见 [AGENTS.md](AGENTS.md)，按其中导航读取当前任务需要的内容。
```

### README.md

````markdown
# {{PROJECT}}

## 简介

{{项目面向的用户、解决的问题和当前可用能力}}

## 快速开始

{{真实运行条件、安装/启动命令与最小使用示例；尚未实现的入口明确说明，首个切片后补实测结果}}

## 验证

{{真实测试命令、关键用户路径验收入口与必要环境条件}}

## 项目结构

{{当前源码和测试入口；不要根据模板猜测 apps/packages/infra 等不存在的目录}}

- `AGENTS.md`：协作导航，按需阅读。
- `docs/ARCHITECTURE.md`：模块边界和数据流。
- `docs/exec-plans/`：当前目标、里程碑与接续信息。
- `docs/histories/`：已交付变更摘要。

## 许可证

{{已有或已确定的许可证；未确定时省去本节}}
````

### CODEOWNERS

只有真实负责人已知时才实例化；保留已有规则，不能把示例团队写成项目负责人。

```text
* @example-org/example-team
```

### LICENSE

以下是保留的 MIT 模板，仅在项目已选择 MIT 时使用。已有许可证沿用，不能因模板默认值替换项目许可。

```text
MIT License

Copyright (c) {{YEAR}} {{AUTHOR}}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### .editorconfig

```ini
root = true

[*]
charset = utf-8
end_of_line = lf
insert_final_newline = true
indent_style = space
indent_size = 2
trim_trailing_whitespace = true

[*.md]
trim_trailing_whitespace = false
```

### .gitattributes

```text
* text=auto eol=lf

*.png binary
*.jpg binary
*.jpeg binary
*.gif binary
*.webp binary
*.pdf binary
```

### .gitignore

```text
# OS / Editor
.DS_Store
Thumbs.db
*.swp
*.swo
*~
.idea/
.vscode/
*.code-workspace

# Logs / temp
*.log
tmp/
temp/

# Env
.env
.env.*
!.env.example

# Node
node_modules/
.pnpm-store/
.yarn/
.turbo/
.next/
dist/
build/
coverage/

# Rust / Go / .NET
target/
bin/
obj/

# Python
__pycache__/
*.pyc
.venv/
*.egg-info/

# Terraform
.terraform/
*.tfstate
*.tfstate.*

# Cache
.cache/
```

### .markdownlint.json

```json
{
  "default": true,
  "MD013": false,
  "MD033": false,
  "MD041": false
}
```

---

## docs/ 文件

### docs/REPO_COLLAB_GUIDE.md

```markdown
# 仓库协作约定

## 开发与验证

- 按用户目标、模块依赖和验收缺口选择工作，优先简单、可运行的实现。
- 复用现有构建与测试；缺少关键验证时随实现补齐，检查真实输入输出。
- 涉及用户界面时，用可用浏览器、Computer Use 或领域工具验收真实路径；没有实际执行的检查明确标为未验证。
- 同类失败反复出现时调查环境、实现或约定，不能只重复同一尝试。

## 文档维护

- `AGENTS.md` 保持导航；本指南和模板通常不随普通代码改动更新。
- 复杂任务先建立或接续现有 plan；一个持续目标用一份进度清单，不另建运行日志或交接文档。
- 里程碑、重要方向变化、阻塞或交接时更新计划；结构事实变化时更新架构。
- 任务交付前同步受影响的使用说明，并按 `HISTORY_GUIDE.md` 汇总同一任务的 history。
- 已有内容能承载的信息直接补在原处。确有独立且长期维护的主题时才新增文档，不为模板齐全创建占位文件。

## 提交与配置

- 提交范围清晰，引用必要计划和验证结论；不为留记录而强行提交。
- 保留用户和其他执行者的改动，配置只改本任务相关部分。
- 启动依赖、环境变量名称和默认值在实际使用入口说明，不写入密钥或敏感日志。
```

### docs/ARCHITECTURE.md

```markdown
# 架构总览

{{当前产品用途与运行方式。未实现的部分明确标为拟定设计。}}

## 模块与依赖

| 模块 / 真实路径 | 职责与接口 | 依赖 | 当前状态 |
| --- | --- | --- | --- |
| {{模块}} | {{输入输出或公开契约}} | {{上游/下游}} | {{已实现/拟定}} |

## 关键数据流

{{从用户入口到处理、存储及输出的实际路径；必要时简述数据格式与持久化边界。}}

## 约束与重要取舍

{{兼容要求、依赖方向和关键设计理由。只保留长期影响实现的内容。}}

验证和推进状态见当前执行计划；本文件只在结构事实变化时更新。
```

### docs/HISTORY_GUIDE.md

```markdown
# 变更历史记录规范

history 汇总已交付的实质变更，不记录逐轮工作日志。

- 默认在实质变更任务交付时记录；长任务需要阶段留档时更新同一文件。
- 同一任务跨轮次、会话或多个修复切片，沿用同一份 history，不按循环或里程碑重复建文件。
- 纯问答、研究、普通测试运行不单独记录。
- 保留用户诉求、主要改动、设计理由、必要验证结论和关键文件；已有计划和测试报告直接引用，不整份复制。
- 不包含密钥、私人本地路径、敏感信息和原始日志；执行环境字段只填确实已知且有用的内容。

默认目录 `docs/histories/YYYY-MM/`，文件名 `YYYYMMDD-HHmm-task-slug.md`，模板见 `docs/histories/template.md`。仓库已有等价变更历史机制时直接沿用。
```

### docs/PLANS_GUIDE.md

```markdown
# Execution Plan 使用说明

长期、跨模块、多阶段或高风险任务需要可恢复的计划。同一目标沿用已有计划；小任务可使用现有清单，不必额外建 plan。

## 存放位置

- 当前计划：`docs/exec-plans/active/YYYY-MM-DD-<slug>.md`
- 完成归档：`docs/exec-plans/completed/`
- 空白模板：`docs/exec-plans/templates/execution-plan.md`
- 目标外且值得保留的已证实问题：`docs/exec-plans/tech-debt-tracker.md`

## 内容与维护

- 记录目标与范围、关键模块依赖、用户可验收的里程碑、风险和验证入口。
- 在里程碑完成、重要方向/约束变化、阻塞、交接或交付时更新状态与下一步；普通尝试不触发更新。
- 交接信息放回当前计划，不另外创建 handoff 或逐轮报告。
- 只有原计划目标达成才归档；部分完成、预算结束或外部阻塞保留真实状态。
- 长日志和测试报告仅引用位置。目标内未完成项留在计划，不复制到技术债清单。
```

### docs/exec-plans/README.md

```markdown
# Execution Plans

这个目录用于存放长期任务的执行计划。

- 进行中的计划放在 `active/`
- 已完成的计划移到 `completed/`
- 新计划从 `templates/execution-plan.md` 开始
- 暂时不做但需要持续跟踪的问题放到 `tech-debt-tracker.md`
```

### docs/exec-plans/tech-debt-tracker.md

```markdown
# 技术债追踪

这里记录那些暂时不阻塞当前任务、但已经值得留档的技术债。

| 日期 | 区域 | 债务描述 | 为什么会存在 | 计划中的后续动作 |
| --- | --- | --- | --- | --- |
```

### docs/exec-plans/templates/execution-plan.md

```markdown
# <目标的执行计划>

## 目标与范围

- 用户要达成的结果：
- 包含 / 不包含：
- 必须保持的约束：

## 模块、依赖与风险

- 相关架构 / 代码入口：
- 推进顺序与关键依赖：
- 主要风险及处理方式（无则省略）：

## 里程碑与进度

使用可验收的用户能力作为里程碑。状态、验收结论和下一步只在重要检查点更新。

| 状态 | 用户能力 / 模块切片 | 前置依赖 | 验收条件与证据 |
| --- | --- | --- | --- |
| 待实施 | <具体能力> | <依赖或无> | <如何证明可用> |

## 验证入口

- 自动化检查：
- 必要的真实操作验收：
- 尚未验证的缺口：

## 接续

- 当前切片 / 下一步：
- 重要阻塞（无则省略）：

## 重要决策（有变化时补充）

- YYYY-MM-DD：决定、理由与影响。
```

### docs/histories/template.md

```markdown
## [YYYY-MM-DD HH:mm] | Task: <简短任务名>

### Execution Context（仅保留确实可得且有用的字段）

- Agent ID:
- Base Model:
- Runtime:

### User Query

> <用户诉求的脱敏摘要>

### Changes Overview

- <主要变更与影响范围>

### Design Intent (Why)

<关键设计理由；已有计划可直接链接。>

### Validation

<关键检查的实际结论、未验证限制和必要证据位置，不复制完整日志。>

### Files Modified

- <关键文件或计划链接，无需复制整个 diff 清单>
```

### docs/exec-plans/active/.gitkeep 和 docs/exec-plans/completed/.gitkeep

空文件，仅用于让 git 跟踪空目录。

---

## 按项目类型新增的专题文档

以下文档不在默认骨架里。只有对应主题已有实际内容、需要独立维护且现有文档不适合承载时，才选取并实例化；项目类型本身不是新增文件的理由。

### docs/SECURITY.md（Web/API/桌面端项目可选）

```markdown
# 安全默认约束

把安全默认值讲清楚，避免实现逐步演进时越走越散。

建议维护的内容：

- 认证与授权约束。
- 密钥和环境变量管理方式。
- 依赖治理与供应链安全要求。
- 数据分级、脱敏与保留策略。
- 对外 API、Webhook、文件上传和沙箱执行的规则。
```

### docs/RELIABILITY.md（Web/API/前后端/桌面端项目可选）

```markdown
# 稳定性与可运维性

定义项目的运行质量底线。

建议维护的内容：

- 启动、健康检查和基本可用性要求。
- 日志、指标、链路的采集和访问约定。
- timeout、retry、backoff 的默认策略。
- 本地和 CI 的关键路径验证方式。
- 常见故障、排查路径和恢复步骤。
```

### docs/CICD.md（Web/API/前后端/CLI/SDK 项目可选）

```markdown
# CI/CD 说明

定义本项目的构建、测试、发布流程。

建议维护的内容：

- 构建命令和产物。
- 测试分层（unit / integration / e2e）以及怎么跑。
- 发布渠道、版本策略、回滚路径。
- 远端 CI 的 job 列表和触发条件。
```

### docs/FRONTEND.md（前后端分离/含前端项目可选）

```markdown
# 前端协作说明

仓库里有前端界面时，把这份文档补完整。

建议在这里维护：

- 本地启动、构建和联调方式。
- 浏览器驱动的验收流程。
- 共享组件边界。
- 设计系统、样式变量和 CSS 规范。
- 前端测试策略。
```

### docs/DESIGN.md（桌面端/CLI/SDK 项目可选）

```markdown
# 设计原则

沉淀产品层面相对稳定的设计原则。

建议维护：

- 整体交互目标、命令面或公共 API 边界。
- 关键用户路径或主流程。
- 复用与例外的判定标准。
```

只有实际内容确需独立组织时才新建专题目录；优先补充既有文档，不铺空文件夹或强制增加目录 README。

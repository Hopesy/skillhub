# SkillHub (`Hopesy/skillhub`)

面向实战工程闭环的 AI Agent 技能包集合，兼容 [Skills.sh](https://skills.sh) 生态，支持 **Claude Code、Codex、Antigravity（桌面端 & `agy` CLI）、Cursor、Windsurf、Gemini CLI** 等主流开发代理。

---

## 快速安装

```bash
# 交互式选择技能与目标 Agent 安装
npx skills add Hopesy/skillhub

# 查看仓库内所有可用技能
npx skills add Hopesy/skillhub --list

# 安装单个指定技能（例如安装到全局）
npx skills add Hopesy/skillhub --skill harness-run -g
npx skills add Hopesy/skillhub --skill code-review -g

# 指定安装到特定 Agent（如 claude-code / codex）
npx skills add Hopesy/skillhub --skill revit-api -a claude-code

# 卸载已安装技能
npx skills remove
```

---

## 技能总览与选型速查

| 分类 | 技能 | 一句话定位 | 主要运行依赖 |
| :--- | :--- | :--- | :--- |
| **研发与协同** | [harness-run](skills/harness-run/) | 新老项目标准化接入、跨会话可追溯记忆留存与“编码→测试→真实验收”自主推进 | Python 3、Git |
| **研发与协同** | [code-review](skills/code-review/) | 本地三端代理（Codex / Claude Code / `agy`）分工协同：方案讨论起草、确定性 6 门过滤审查、跨代理执行 TODO 与漏洞验收 | Python 3、Git、本机 `codex` / `claude` / `agy` |
| **Revit 开发** | [saury-revit](skills/saury-revit/) | 基于 `Saury.Revit.Template` 交互式创建 Revit 2026 插件工程（MVVM + DI）并按规范扩展功能 | .NET 8.0+ SDK、Revit 2026 |
| **Revit 开发** | [revit-api](skills/revit-api/) | 基于 `RevitAPI.dll v26.0.4.0` 的离线精准 API 查询库（30 个命名空间 / 2,724 个类型 + 11 套核心模式） | Python 3 |
| **Revit 开发** | [revit-automate-test](skills/revit-automate-test/) | Revit 插件真机验收：程序集预加载冒烟、`.addin`/MSI 部署核验、UI Automation 驱动 WPF 与 Journal 取证 | Windows PowerShell、Revit 真机环境 |
| **文档与发布** | [pdf-patch](skills/pdf-patch/) | 无文本层的扫描件/图片型 PDF 局部文字无痕修补（底色噪点采样、多字体对比匹配、基线灰度对齐与截图核验） | Python 3、PyMuPDF、Pillow |
| **文档与发布** | [typos-publish](skills/typos-publish/) | 自动提取补全 Markdown 文章 Frontmatter（标题、语义英文 `slug`、摘要、分类），确认后单篇或批量发布到 Typos 博客 | Node.js、站点 `config.json` 密钥 |

> **常用组合推荐**
> - **日常全栈/长程软件研发**：`harness-run`（管架构文档、执行计划与跨会话记忆） + `code-review`（让擅长清爽编码的模型写实现，调用深层推理模型只读抓漏洞并一次性修完）。
> - **Revit 插件从零开发到交付**：`saury-revit`（初始化 MVVM+DI 工程） → `revit-api`（查准 2026 真实 API 签名写业务） → `revit-automate-test`（拉起 Revit 真机跑 WPF UI 自动化与 Journal 验收）。

---

## 各技能详细用途与触发方式

### 一、通用研发工程与多代理协同

#### 1. [`harness-run`](skills/harness-run/) — 项目持续开发与跨会话记忆框架
- **解决什么问题**：中大型或跨多天开发的项目容易出现文档散乱、上下文压缩后遗忘用户约束、以及 Agent 只写完代码就停手而未做真实端到端验收。
- **核心能力**：
  - **新老项目统一收拢**：新建项目自动搭建最小可运行骨架；存量项目自动将散落旧资料整合进标准目录（`AGENTS.md` 短导航、`docs/ARCHITECTURE.md`、`docs/design/` 专题、`docs/exec-plans/` 活跃计划与 `docs/histories/` 交付归档）并清理冗余旧件。
  - **跨会话记忆接续**：自动为 Claude Code / Codex / Antigravity 注册项目级 Hook，持久化记录关键决策、被否决方案原因与当前待验收切片，新开会话或压缩后无缝续接。
  - **真实证据验收**：围绕用户路径逐个切片推进编译、运行与真实交互验证，拒绝仅凭静态阅读宣布完成。
- **常用调用示例**：
  - `使用 harness-run 接入当前项目，整理现有文档并跑通构建测试基线。`
  - `使用 harness-run 接续当前项目的活跃计划，继续完成下一个待验收切片。`

#### 2. [`code-review`](skills/code-review/) — 本地多代理分工协同与确定性审查闭环
- **解决什么问题**：单一模型既做设计又写代码容易产生盲区（如宏观编码模型偶漏极端并发/边界条件，而高强度推理模型直接下场改码又容易写出层层判空的过度防御样板代码）；同时免去传统审查工具复杂的 API Key 配置。
- **核心能力**：
  - **三端本地 CLI 零配置互通 + 过程实时监控汇报**：默认启动本机 `codex`，也可通过 `--agent claude` 或 `--agent agy` 切换 Claude Code 或 Antigravity CLI，默认原样沿用各代理自身的本地模型配置；调用期间主 Agent 后台监控并随时向用户汇报被调代理的实时进度。
  - **覆盖研发全流程四类动作**：支持 `discuss`（跨代理只读讨论/起草方案）、`plan`（结构化方案审查）、`exec`（委派目标代理执行 TODO 编码，如在 Codex 中委派 `agy` 编写清爽代码）、`code`（代码改动与需求落实验收）。
  - **确定性护栏 + 反过度防御**：前置 6 门过滤自动剔除二进制、`.env` 等密钥路径、符号链接越界读取与构建噪音；后置滑动窗口精确修正漂移行号，并通过独立 `suppressed_over_defensive_notes` 分流槽拦截冗余判空和吞错兜底建议。默认 **1 轮深审 + 一次性全量修复交付**（支持按需开启最高 5 轮清零复审）。
- **常用调用示例**：
  - `用 code-review 先审查当前设计方案，修订后执行 TODO 实现，最后用 Codex 验收并一次性修完所有问题。`
  - `用 $code-review（--agent agy）先起草并执行重构计划，再对当前工作区改动做代码漏洞审查。`

---

### 二、BIM / Autodesk Revit 二次开发套件

#### 3. [`saury-revit`](skills/saury-revit/) — Revit 2026 插件工程脚手架与扩展器
- **解决什么问题**：手动新建 Revit 插件容易配错 `.csproj` 框架目标、`.addin` GUID、Host 依赖注入容器或 `Debug_R26` 附加调试路径。
- **核心能力**：
  - 自动检查并引导安装 `.NET 8.0+ SDK` 与 `Saury.Revit.Template` 模板。
  - 交互式确认合法 C# 命名空间与目标目录，一键生成完整的 MVVM + DI 架构工程（含 `Application.cs`、`Host.cs`、Ribbon 图标样式、自动部署至 `Addins\2026` 的 `Debug_R26` / `Release_R26` 构建配置）。
  - 支持在既有模板项目中按统一分层规范新增 Command、WPF View、ViewModel 与 Service。
- **常用调用示例**：
  - `用 saury-revit 创建一个名为 Acme.WallTools 的 Revit 2026 插件项目并验证编译。`

#### 4. [`revit-api`](skills/revit-api/) — Revit 2026 离线精准 API 查询参考
- **解决什么问题**：LLM 直接凭预训练记忆写 Revit 二次开发代码时，常会幻觉出不存在的方法重载或混用旧版已废弃接口。
- **核心能力**：
  - 内置基于 `RevitAPI.dll v26.0.4.0` 构建的完整离线检索库，覆盖 **30 个命名空间、2,724 个类型**。
  - 针对不含明确类名的模糊业务诉求（如“实现 DMU 动态模型更新”、“监听链接文件加载阶段”），强制先做技术概念拆解预研，再通过 `search_api.py` 与 `extract_page.py` 精确提取类成员、构造函数重载签名、继承关系与 Remarks 注意事项。
  - 附带 11 套高频 C# 核心开发模式代码范例（元素过滤、事务与事务组、几何提取、Ribbon 按钮等）。
- **常用调用示例**：
  - `查一下 Revit 2026 里 FilteredElementCollector 和 IUpdater 的准确签名与用法示例。`

#### 5. [`revit-automate-test`](skills/revit-automate-test/) — Revit 插件真机部署与 UI 自动化测试
- **解决什么问题**：插件编译成功并不代表交付可用——代码混淆（如 Confuser）破坏元数据、MSI 安装遗漏依赖、`.addin` 路径错误、WPF 参数窗口交互异常等问题只能在真实宿主环境中暴露。
- **核心能力**：
  - **分层前置门禁**：启动重量级 Revit 前，先用 `Test-DotNetAssemblyLoad.ps1` 做程序集加载冒烟（秒级捕获 `BadImageFormatException` 与混淆元数据损坏），再用 `Get-RevitAddinDeployment.ps1` 核验 `.addin`、文件版本与哈希。
  - **真机宿主冒烟与日志取证**：安全拉起独立 Revit 进程，自动采集最新 Revit Journal 中的 `API_SUCCESS` 证据与插件业务日志。
  - **WPF / Ribbon UI Automation 驱动**：通过 `Invoke-RevitWpfUiAutomation.ps1` 导出控件树、点击功能区按钮、修改 WPF 输入框/单选复选框，并在模型视图指定坐标执行鼠标拾取，完成端到端闭环验证。
- **常用调用示例**：
  - `用 revit-automate-test 验证当前打包后的插件在 Revit 真机中的加载状态，并跑通参数窗口创建构件的 E2E 测试。`

---

### 三、文档处理与内容发布工具

#### 6. [`pdf-patch`](skills/pdf-patch/) — 扫描版 / 图片型 PDF 局部文字无痕修补
- **解决什么问题**：纯图像扫描版 PDF（如扫描公文、合同、图纸说明）没有可编辑文本层，直接用常规 PDF 编辑器覆盖会留下刺眼白底色块、字体字重失真或多次保存后整页模糊。
- **核心能力**：
  - 使用 `inspect_pdf.py` 与 `find_dark_runs.py` 在高倍渲染图上按暗像素分布精确测定单字/短语像素边界。
  - 自动采样目标周围真实纸张亮部底色并注入微量确定性噪点自然遮盖原字，使用 `font_variants.py` 生成仿宋、宋体、楷体等多字号实景对比样张，微调基线、描边字重、灰度与高斯柔化以匹配扫描质感。
  - 每次迭代均从原始 PDF 单次重绘合成（杜绝累积模糊），自动保留其余未改页面并输出局部放大截图对比验视。
- **常用调用示例**：
  - `用 pdf-patch 把这份扫描版 PDF 第 1 页第 3 行的错别字修补为“为”，保持原公文仿宋字体和纸张底色一致。`

#### 7. [`typos-publish`](skills/typos-publish/) — Markdown 博客元数据生成与一键发布
- **解决什么问题**：本地撰写的原始 Markdown 技术文章缺少博客系统所需的规范 Frontmatter，手动翻译英文 URL `slug`、归纳摘要并逐篇上传效率低。
- **核心能力**：
  - 在不改动原文件正文的前提下，自动提取文章标题、生成简短语义化英文 `kebab-case slug`（非拼音或日期）、提炼单句摘要与 1–3 个分类标签。
  - 先向用户展示完整 YAML Frontmatter 与目标访问链接（`<baseUrl>/posts/<slug>`），经确认后通过临时文件调用 `/api/v1/posts` 接口发布并自动清理临时件；支持多篇文档汇总成表一次性确认后批量发布。
  - *前置配置*：首次使用将技能目录下的 `config.example.json` 复制为本地 `config.json`，填入站点 `baseUrl` 与后台生成的 API Token。
- **常用调用示例**：
  - `把这篇 Markdown 文章发布到我的 Typos 博客（或批量发布 docs/posts/ 下的三篇文章）。`

---

## 仓库目录规范与开发新技能

```text
skills/
└── <skill-name>/             # 小写字母 + 连字符，与 frontmatter name 一致
    ├── SKILL.md              # 必需：技能触发描述（description）与核心执行指令
    ├── README.md             # 可选：面向人类阅读的用法示例与测试说明
    ├── agents/openai.yaml    # 可选：UI 面板展示名称与默认提示词
    ├── references/           # 可选：按需加载的详细规范、API 索引或契约文档
    ├── scripts/              # 可选：确定性可执行脚本与单元测试
    └── data/                 # 可选：离线结构化数据包
```

新增或修改技能后：
1. 确保 `SKILL.md` 顶部包含合法的 YAML Frontmatter（`name`、`description`、`license`、`metadata`），且正文保持精简、将大段参考资料拆入 `references/` 按需读取。
2. 若包含脚本与测试（如 `harness-run`、`code-review`），在提交前运行对应的 `unittest` 套件并使用 `quick_validate.py` 核验技能格式。
3. 推送到 GitHub 后即可通过 `npx skills add Hopesy/skillhub --skill <skill-name>` 分发安装。

## 许可

MIT

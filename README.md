# Skills

AI Agent 技能包集合，兼容 [Skills.sh](https://skills.sh) 生态。

## 安装

```bash
npx skills add Hopesy/skills
```

支持 Claude Code、Cursor、Codex、Windsurf、Gemini CLI 等 Agent。

### 安装指定技能

```bash
# 列出所有可用技能
npx skills add Hopesy/skills --list

# 安装指定技能
npx skills add Hopesy/skills --skill saury-revit

# 安装到指定 Agent
npx skills add Hopesy/skills -a claude-code

# 全局安装
npx skills add Hopesy/skills -g
```

### 卸载

```bash
npx skills remove
```

## 技能列表

| 技能 | 说明 |
|---|---|
| [saury-revit](skills/saury-revit/) | 基于 Saury.Revit.Template 创建 Revit 2026 插件项目（MVVM + DI 架构），支持交互式项目创建和功能扩展 |
| [revit-api](skills/revit-api/) | Revit 2026 API 文档查询与参考，覆盖 30 个命名空间、2724 个类型，支持搜索、浏览和模式速查 |
| [pdf-patch](skills/pdf-patch/) | 扫描版或图片型 PDF 的局部文字修补，支持单字/短语替换、字体匹配、像素定位和渲染验证 |
| [revit-automate-test](skills/revit-automate-test/) | Revit 插件真机自动化测试：部署验证、.addin 加载、UI Automation 驱动 WPF 界面、Journal 日志取证 |
| [harness-run](skills/harness-run/) | 统一初始化与自主开发：按需补齐基础模板，配合原生 goal 推进实现、测试及真实验收 |
| [typos-publish](skills/typos-publish/) | 补全 Markdown 文章 frontmatter 并通过 API 发布到 Typos 博客，支持批量发布 |

## 目录结构

```
skills/
└── <技能名>/
    ├── SKILL.md              # 技能定义（必需）
    ├── references/           # 参考文档（按需加载）
    ├── scripts/              # 辅助脚本（可选）
    └── data/                 # 数据文件（可选）
```

## 创建新技能

1. 在 `skills/` 下新建目录，目录名即技能名（小写 + 连字符）
2. 编写 `SKILL.md`，包含 YAML frontmatter：

```yaml
---
name: my-skill
description: 技能描述，包含触发关键词。
license: MIT
metadata:
  author: Hopesy
  version: "1.0.0"
---

# 技能标题

具体指引内容...
```

3. 推送到 GitHub，用户即可通过 `npx skills add Hopesy/skills` 安装

## 许可

MIT

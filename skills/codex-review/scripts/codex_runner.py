"""Silent headless runner supporting local `codex` (default), `claude` (Claude Code), and `agy` (Antigravity CLI)."""

from __future__ import annotations

from collections import Counter
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from schemas import build_review_schema
from selection import DiffFile, SelectionResult

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
DEFAULT_AGENT = "codex"
SUPPORTED_AGENTS = ("codex", "claude", "agy")
DEFAULT_TIMEOUT_SEC = 900
MAX_PER_FILE_PATCH_CHARS = 80_000
TOTAL_INLINE_PATCH_BUDGET = 240_000
MAX_OVERVIEW_SELECTED_LINES = 400
_BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

CLAUDE_READONLY_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Bash(git --no-pager show *)",
    "Bash(git --no-pager grep *)",
    "Bash(git --no-pager log *)",
    "Bash(git --no-pager diff *)",
    "Bash(git show *)",
    "Bash(git grep *)",
    "Bash(git log *)",
    "Bash(git diff *)",
]


def normalize_agent_name(agent: str) -> str:
    cleaned = (agent or DEFAULT_AGENT).strip().lower()
    if cleaned in {"", "codex"}:
        return "codex"
    if cleaned in {"claude", "claude-code", "claudecode"}:
        return "claude"
    if cleaned in {"agy", "antigravity", "gemini"}:
        return "agy"
    raise ValueError(f"Unsupported agent {agent!r}; choose 'codex', 'claude', or 'agy'.")


def resolve_codex_command() -> list[str]:
    """Locate the native `codex` binary to avoid console-wrapper flashes on Windows."""
    override = os.environ.get("CODEX_REVIEW_BIN", "").strip()
    if override and Path(override).is_file():
        return [override]

    if sys.platform == "win32":
        arch = platform.machine().lower()
        triple = (
            "aarch64-pc-windows-msvc"
            if arch in {"arm64", "aarch64"}
            else "x86_64-pc-windows-msvc"
        )
        pkg_name = (
            "codex-win32-arm64" if arch in {"arm64", "aarch64"} else "codex-win32-x64"
        )
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            npm_root = Path(appdata) / "npm" / "node_modules" / "@openai"
            candidates = [
                npm_root / "codex" / "node_modules" / "@openai" / pkg_name / "vendor" / triple / "bin" / "codex.exe",
                npm_root / pkg_name / "vendor" / triple / "bin" / "codex.exe",
            ]
            for cand in candidates:
                if cand.is_file():
                    return [str(cand)]

        which_ps1 = shutil.which("codex.ps1")
        pwsh = shutil.which("pwsh")
        if which_ps1 and pwsh:
            return [pwsh, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", which_ps1]

    found = shutil.which("codex")
    if found:
        return [found]
    raise FileNotFoundError(
        "Could not locate `codex` CLI executable. Install `@openai/codex` or set CODEX_REVIEW_BIN."
    )


def resolve_claude_command() -> list[str]:
    """Locate the native `claude` binary to avoid console-wrapper flashes on Windows."""
    override = os.environ.get("CLAUDE_REVIEW_BIN", "").strip()
    if override and Path(override).is_file():
        return [override]

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            native_exe = (
                Path(appdata)
                / "npm"
                / "node_modules"
                / "@anthropic-ai"
                / "claude-code"
                / "bin"
                / "claude.exe"
            )
            if native_exe.is_file():
                return [str(native_exe)]
        local_bin = Path.home() / ".local" / "bin" / "claude.exe"
        if local_bin.is_file():
            return [str(local_bin)]
        which_ps1 = shutil.which("claude.ps1")
        pwsh = shutil.which("pwsh")
        if which_ps1 and pwsh:
            return [pwsh, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", which_ps1]

    found = shutil.which("claude")
    if found:
        return [found]
    raise FileNotFoundError(
        "Could not locate `claude` CLI executable. Install `@anthropic-ai/claude-code` or set CLAUDE_REVIEW_BIN."
    )


def resolve_agy_command() -> list[str]:
    """Locate the native Antigravity CLI (`agy.exe` / `agy`)."""
    override = os.environ.get("AGY_REVIEW_BIN", "").strip()
    if override and Path(override).is_file():
        return [override]

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates = []
        if local_app:
            candidates.append(Path(local_app) / "agy" / "bin" / "agy.exe")
        candidates.append(Path.home() / ".gemini" / "bin" / "agy.exe")
        for cand in candidates:
            if cand.is_file():
                return [str(cand)]

    found = shutil.which("agy")
    if found:
        return [found]
    raise FileNotFoundError(
        "Could not locate `agy` CLI executable. Run `agy install` or set AGY_REVIEW_BIN."
    )


def _build_agy_env() -> dict[str, str]:
    """Inherit current environment, populating HTTP(S)_PROXY from Windows user Internet Settings if needed."""
    env = dict(os.environ)
    if sys.platform != "win32" or env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or env.get("https_proxy"):
        return env

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            proxy_str = str(proxy_server or "").strip()
    except OSError:
        return env

    if not proxy_str:
        return env
    # Handle `http=127.0.0.1:7897;https=127.0.0.1:7897` or bare `127.0.0.1:7897`
    chosen = ""
    for part in proxy_str.split(";"):
        part = part.strip()
        if part.startswith("https="):
            chosen = part[len("https=") :]
            break
        if part.startswith("http="):
            chosen = part[len("http=") :]
        elif not chosen and "=" not in part:
            chosen = part

    if not chosen:
        return env
    addr = chosen.split("://", 1)[-1]
    host_port = addr.rsplit("/", 1)[0]
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        try:
            with socket.create_connection((host, int(port_s)), timeout=0.2):
                proxy_url = chosen if "://" in chosen else f"http://{chosen}"
                env["HTTP_PROXY"] = proxy_url
                env["HTTPS_PROXY"] = proxy_url
        except OSError:
            pass
    return env


def read_agent_default_profile(agent: str = DEFAULT_AGENT) -> tuple[str, str]:
    """Read display-only default model/effort for `codex`, `claude`, or `agy` without overriding CLI behavior."""
    norm_agent = normalize_agent_name(agent)
    if norm_agent == "claude":
        settings_path = Path(os.environ.get("CLAUDE_CONFIG_DIR", "") or (Path.home() / ".claude")) / "settings.json"
        if settings_path.is_file():
            try:
                s = json.loads(settings_path.read_text(encoding="utf-8", errors="replace"))
                env_map = s.get("env") if isinstance(s.get("env"), dict) else {}
                m = str(s.get("model") or env_map.get("ANTHROPIC_MODEL") or "claude-default")
                e = str(s.get("effortLevel") or "default")
                return (m, e)
            except Exception:  # noqa: BLE001
                pass
        return ("claude-default", "default")

    if norm_agent == "agy":
        agy_settings = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        if agy_settings.is_file():
            try:
                s = json.loads(agy_settings.read_text(encoding="utf-8", errors="replace"))
                m = str(s.get("model") or "agy-default")
                e = str(s.get("effort") or "default")
                return (m, e)
            except Exception:  # noqa: BLE001
                pass
        return ("agy-default", "default")

    codex_home = Path(os.environ.get("CODEX_HOME", "") or (Path.home() / ".codex"))
    cfg_path = codex_home / "config.toml"
    if not cfg_path.is_file():
        return ("codex-default", "default")
    try:
        data = tomllib.loads(cfg_path.read_bytes().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return ("codex-default", "default")
    m = str(data.get("model") or "codex-default")
    e = str(data.get("model_reasoning_effort") or "default")
    return (m, e)


def _format_toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY_RE.fullmatch(key) else json.dumps(key)


def _build_mcp_disable_overrides() -> list[str]:
    """Disable configured MCP servers and GUI/browser plugins during headless Codex runs."""
    codex_home = Path(os.environ.get("CODEX_HOME", "") or (Path.home() / ".codex"))
    cfg_path = codex_home / "config.toml"
    if not cfg_path.is_file():
        return []
    try:
        data = tomllib.loads(cfg_path.read_bytes().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return []

    args: list[str] = []
    mcp_servers = data.get("mcp_servers")
    if isinstance(mcp_servers, dict):
        for name in mcp_servers:
            args.extend(["-c", f"mcp_servers.{_format_toml_key(str(name))}.enabled=false"])
    plugins = data.get("plugins")
    if isinstance(plugins, dict):
        for name in plugins:
            args.extend(["-c", f"plugins.{_format_toml_key(str(name))}.enabled=false"])
    return args


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


ANTI_OVER_DEFENSIVE_CHARTER = """## 核心协作定位与反过度防御铁律（必须严格遵守）

你当前作为**纯只读首席审查官**运行，宿主执行模型负责出方案与写代码。你的职责是**精准抓出真实漏洞与需求/逻辑缺口，同时严防诱导宿主写出过度防御代码**：

1. **只抓真正会导致故障、行为错误、安全隐患或违背需求的实质问题**（写入 `findings`）：
   - 方案阶段：重点核查与仓库现有真实代码/接口/路径/数据结构的契约冲突、遗漏边界状态、并发与生命周期泄漏（如未关闭句柄、孤儿进程、Windows 弹窗违例）、缺少可验证的真实测试闭环、以及方案本身过度设计/重复造轮子。
   - 代码阶段：重点核查对照原定计划/需求的漏实现或假实现、条件反写/Off-by-one/空集合与边界崩溃、异步竞态与异常吞没、跨文件调用方/序列化字段未同步更新、删除仍被调用的导出项、命令注入/路径穿越/敏感泄露、以及无效断言测试。
2. **严禁提出过度防御要求（Anti-Over-Defensive Gate）**：
   - **禁止**要求在内部受控调用链上叠加多余的类型检查、`None` 判空、防御性深拷贝或冗余参数校验（当调用方或类型契约已保证不变量时）。
   - **禁止**要求用宽泛 `try/except` 吞掉异常或返回静默兜底默认值来掩盖上游错误。
   - **禁止**要求超前抽象、多余配置开关、包装层或纯命名/注释/代码风格挑刺。
   - 凡属上述“假想内部非法输入/过度防御冲动/细枝末节”，**一律不得放入 `findings`**，请放入 `suppressed_over_defensive_notes` 备忘即可。
3. **`fix_direction` 只给最小根因修复方向**：用 1–2 句指出最简、模块化的修改方式，不要代写冗长的防御性样板代码块。
4. **全量排查，不提前停手**：不要抓到第一个高优问题就结束；完整核查所有本轮目标范围与关联调用链，确保宿主可以**一次性全部修复**。若确认 0 实质问题，`verdict` 必须为 `"pass"` 且 `findings` 为空数组 `[]`。
"""


def _format_prior_findings_section(round_no: int, prior_findings: list[dict]) -> str:
    if round_no <= 1 or not prior_findings:
        return (
            "## 轮次上下文\n"
            f"- 当前为 **第 {round_no} 轮审查**（首轮），`prior_findings_audit` 请返回空数组 `[]`。\n"
        )
    serialized = json.dumps(prior_findings, ensure_ascii=False, indent=2)
    return (
        f"## 轮次上下文（第 {round_no} 轮复核验收）\n"
        "以下是上一轮检出并交由宿主一次性修复的问题清单。你必须：\n"
        "1. 逐项核实每个旧问题是否已真正根治，并在 `prior_findings_audit` 中对**每一个旧问题 ID** 明确标记 `fixed`、`persisting` 或 `partially_fixed`（漏填任何一项都会被状态机自动判定为未关闭）；\n"
        "2. 若旧问题未彻底修好（`persisting` / `partially_fixed`），**必须同时使用其原 ID 保留在 `findings` 中**说明剩余缺口；若发现全新问题，请分配不与旧 ID 冲突的新 ID；\n"
        "3. 检查本轮修复是否引入了新的回归缺陷，或是否写出了冗余的**过度防御代码**（如有，以 `over_defensive_bloat` 或对应缺陷类别列入 `findings` 要求精简修复）。\n\n"
        f"```json\n{serialized}\n```\n"
    )


def build_plan_prompt(
    plan_text: str,
    plan_label: str,
    goal: str,
    round_no: int,
    prior_findings: list[dict],
    project_rules: str = "",
) -> str:
    parts = [
        "# 任务：技术方案与 TODO 计划深度审查（只读模式）",
        "",
        ANTI_OVER_DEFENSIVE_CHARTER.strip(),
        "",
        _format_prior_findings_section(round_no, prior_findings).strip(),
    ]
    if goal.strip():
        parts.extend(["", "## 用户原始目标与约束", "", goal.strip()])
    if project_rules.strip():
        parts.extend(["", "## 仓库规范摘要", "", project_rules.strip()])

    parts.extend(
        [
            "",
            f"## 待审查方案（`{plan_label}`）",
            "",
            "请结合工作区内的真实源码（通过只读命令/文件读取核对方案中提到的模块、函数签名、配置与路径是否存在或冲突）进行核查；所有输出字段使用中文（`id`/`severity`/`category`/`verdict`/`status` 枚举除外）：",
            "",
            "<plan_document>",
            plan_text,
            "</plan_document>",
        ]
    )
    return "\n".join(parts) + "\n"


def _build_file_read_hint(selection: SelectionResult, f: DiffFile) -> str:
    if f.status == "DELETED":
        old_target = f.old_path or f.path
        if selection.resolved_base:
            return f"git --no-pager show {selection.resolved_base}:{old_target}（查看删除前源码，并在目标版本检索残留调用方）"
        return f"在目标版本中检索 `{old_target}` 的残留导入或调用方"

    if f.is_symlink:
        if selection.resolved_head:
            return f"git --no-pager show {selection.resolved_head}:{f.path}（仅核查链接目标路径，勿跟随读取目标正文）"
        return f"仅核查符号链接 `{f.path}` 的链接目标路径，禁止直接打开读取链接指向的目标正文"

    if selection.resolved_head:
        return f"git --no-pager show {selection.resolved_head}:{f.path}"
    return f"只读读取工作树常规文件 `{f.path}`"


def build_code_prompt(
    selection: SelectionResult,
    goal: str,
    plan_text: str,
    round_no: int,
    prior_findings: list[dict],
) -> str:
    selected = selection.selected_files
    excluded = selection.excluded_files
    overview_lines: list[str] = []
    for f in selected[:MAX_OVERVIEW_SELECTED_LINES]:
        rename_note = f" (from {f.old_path})" if f.old_path else ""
        sym_note = " [symlink]" if f.is_symlink else ""
        overview_lines.append(
            f"- [REVIEW] {f.status} `{f.path}`{rename_note}{sym_note} (+{f.additions}/-{f.deletions})"
        )
    if len(selected) > MAX_OVERVIEW_SELECTED_LINES:
        overview_lines.append(
            f"- ... 其余 {len(selected) - MAX_OVERVIEW_SELECTED_LINES} 个待审文件已省略列表"
        )
    if excluded:
        counts = Counter(f.exclude_reason for f in excluded)
        breakdown = ", ".join(f"{reason}: {cnt}" for reason, cnt in sorted(counts.items()))
        overview_lines.append(f"- [EXCLUDED 汇总] 共排除 {len(excluded)} 个文件（{breakdown}）")

    if selection.resolved_head:
        ref_guidance = (
            f"本轮为历史提交审查（基准：`{selection.resolved_base or 'EMPTY_TREE'}`，目标版本 SHA：`{selection.resolved_head}`）。"
            f"需要补读完整文件或检索关联调用方时，**必须从目标 Git 对象读取**（如 `git --no-pager show {selection.resolved_head}:<path>` 或 `git --no-pager grep <pattern> {selection.resolved_head}`；对于 `DELETED` 文件则从基准 `{selection.resolved_base}` 读取删除前定义），"
            "**严禁直接读取当前工作树文件**，以免混入目标提交之外的未提交改动。"
        )
    else:
        base_desc = selection.resolved_base or ("EMPTY_TREE" if selection.base_is_empty else "HEAD")
        ref_guidance = (
            f"本轮为工作树代码审查（对比基准：`{base_desc}` → 当前工作树）。"
            "可只读检查仓库当前工作树中的常规文件、关联调用方与测试（遇到符号链接仅核查链接目标路径，禁止跟随读取链接目标正文；`DELETED` 文件请从对比基准读取旧定义并检查工作树残留调用）。"
            "问题主位置（`file`）必须落在待审范围文件内（`missing_requirement` 除外）。"
        )

    diff_blocks: list[str] = []
    remaining_budget = TOTAL_INLINE_PATCH_BUDGET
    omitted_tail: list[str] = []
    for f in selected:
        patch = f.patch
        read_hint = _build_file_read_hint(selection, f)
        if remaining_budget <= 500:
            omitted_tail.append(f"- `{f.path}` ({f.status}) → {read_hint}")
            continue

        cap = min(MAX_PER_FILE_PATCH_CHARS, remaining_budget)
        if len(patch) > cap:
            excerpt = patch[:cap]
            diff_blocks.append(
                f'<file path="{f.path}" status="{f.status}" truncated="true">\n'
                f"{excerpt}\n... [Diff 已按预算截断，请按此方式核查剩余内容：{read_hint}]\n</file>"
            )
            remaining_budget -= len(excerpt)
        else:
            diff_blocks.append(
                f'<file path="{f.path}" status="{f.status}">\n{patch.rstrip(chr(13) + chr(10))}\n</file>'
            )
            remaining_budget -= len(patch)

    if omitted_tail:
        preview_tail = "\n".join(omitted_tail[:80])
        more = f"\n- ... 另有 {len(omitted_tail) - 80} 个待审文件请按概览清单读取" if len(omitted_tail) > 80 else ""
        diff_blocks.append(
            f'<omitted_files count="{len(omitted_tail)}">\n'
            f"以下待审文件因内联预算已满未展开补丁，请按对应方式只读核查：\n{preview_tail}{more}\n</omitted_files>"
        )

    parts = [
        "# 任务：代码修改与计划落实深度验收审查（只读模式）",
        "",
        ANTI_OVER_DEFENSIVE_CHARTER.strip(),
        "",
        _format_prior_findings_section(round_no, prior_findings).strip(),
        "",
        "## 版本与证据读取边界",
        "",
        ref_guidance,
    ]
    if goal.strip():
        parts.extend(["", "## 变更背景与验收目标", "", goal.strip()])
    if plan_text.strip():
        parts.extend(
            [
                "",
                "## 对照核验的已批准方案 / TODO 清单",
                "",
                "请逐项核对计划要求是否均已真实落地、有无漏实现（`missing_requirement`）或偏离设计：",
                "",
                "<approved_plan>",
                plan_text.strip(),
                "</approved_plan>",
            ]
        )
    if selection.project_rules_summary.strip():
        parts.extend(["", "## 仓库规范摘要", "", selection.project_rules_summary.strip()])

    parts.extend(
        [
            "",
            f"## 变更文件概览（模式：`{selection.mode}`，待审文件：{len(selected)} 个，已排除：{len(excluded)} 个）",
            "",
            "\n".join(overview_lines) if overview_lines else "（当前对比范围无代码文件变更；请对照验收目标、批准方案与上轮遗留项核查目标版本）",
            "",
            "## 待审查代码 Diff",
            "",
            "请逐个核查 `<review_files>` 中的每个文件，并在目标版本中只读检索上下游调用点、类型定义与关联测试以确认事实（禁止仅凭猜测报问题）。引用 `snippet` 时请给出目标文件中的连续原样代码行以便精确定位行号；所有说明文字使用中文：",
            "",
            "<review_files>",
            "\n\n".join(diff_blocks) if diff_blocks else "（无内联补丁）",
            "</review_files>",
        ]
    )
    return "\n".join(parts) + "\n"


def validate_codex_response_consistency(parsed: dict) -> None:
    """Reject self-contradictory `needs_fix` responses with empty `findings`."""
    verdict = str(parsed.get("verdict") or "")
    findings = parsed.get("findings") or []
    if verdict == "needs_fix" and not findings:
        raise RuntimeError("Model returned verdict='needs_fix' with empty findings.")


def _execute_headless_process(
    cmd: list[str],
    cwd: Path,
    prompt_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_sec: int,
    agent_label: str,
    env: dict[str, str] | None = None,
) -> tuple[int, float]:
    popen_kwargs: dict = {
        "cwd": str(cwd.resolve()),
        "creationflags": CREATE_NO_WINDOW,
    }
    if env is not None:
        popen_kwargs["env"] = env
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    started = time.monotonic()
    with (
        prompt_path.open("rb") as in_fh,
        stdout_path.open("wb") as out_fh,
        stderr_path.open("wb") as err_fh,
    ):
        proc = subprocess.Popen(
            cmd,
            stdin=in_fh,
            stdout=out_fh,
            stderr=err_fh,
            **popen_kwargs,
        )
        try:
            returncode = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_tree(proc)
            raise RuntimeError(f"{agent_label} timed out after {timeout_sec}s") from exc
        except BaseException:
            _terminate_process_tree(proc)
            raise

    return returncode, time.monotonic() - started


def _parse_claude_envelope(stdout_text: str, structured_schema: bool = True) -> dict:
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON envelope from claude: {exc}: {stdout_text[:500]}") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("Unexpected non-object output from claude.")
    if envelope.get("is_error"):
        raise RuntimeError(f"claude reported error: {envelope.get('result') or envelope}")

    model_usage = envelope.get("modelUsage")
    runtime_model = (
        next(iter(model_usage.keys())) if isinstance(model_usage, dict) and model_usage else ""
    )
    session_id = str(envelope.get("session_id") or "")

    if not structured_schema:
        return {
            "response": str(envelope.get("result") or ""),
            "agent_session_id": session_id,
            "_runtime_model": runtime_model,
        }

    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        parsed = dict(structured)
    else:
        raw_res = str(envelope.get("result") or "").strip()
        try:
            parsed = json.loads(raw_res)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude did not return valid structured_output JSON: {exc}: {raw_res[:500]}"
            ) from exc

    if runtime_model:
        parsed["_runtime_model"] = runtime_model
    if session_id:
        parsed["_agent_session_id"] = session_id
    return parsed


def _build_agy_prompt_args(prompt: str, prompt_path: Path, tmp_path: Path) -> list[str]:
    """Pass short prompts directly via `-p=...` and stage large prompts via `--add-dir` to avoid Windows 32KB CLI limit."""
    if len(prompt) <= 12_000:
        return [f"-p={prompt}"]
    return [
        "--add-dir",
        str(tmp_path.resolve()),
        f"-p=请先完整读取文件 {prompt_path.resolve()} 中的全部指令与上下文，然后严格按其中的要求执行。",
    ]


def _parse_agy_envelope(stdout_text: str, structured_schema: bool = True) -> dict:
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON envelope from agy: {exc}: {stdout_text[:500]}") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("Unexpected non-object output from agy.")
    if str(envelope.get("status") or "").upper() == "ERROR" or envelope.get("error"):
        raise RuntimeError(f"agy reported error: {envelope.get('error') or envelope}")

    conv_id = str(envelope.get("conversation_id") or "")
    raw_resp = str(envelope.get("response") or "").strip()

    if not structured_schema:
        return {
            "response": raw_resp,
            "agent_session_id": conv_id,
        }

    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        parsed = dict(structured)
    else:
        try:
            parsed = json.loads(raw_resp)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"agy did not return valid structured_output JSON: {exc}: {raw_resp[:500]}") from exc
    if conv_id:
        parsed["_agent_session_id"] = conv_id
    return parsed


def run_codex_structured_review(
    prompt: str,
    mode: str,
    cwd: Path,
    scratch_parent: Path,
    agent: str = DEFAULT_AGENT,
    model: str = "",
    effort: str = "",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    keep_mcp: bool = False,
) -> tuple[dict, float]:
    """Invoke headless read-only `codex` (default), `claude`, or `agy` with strict JSON Schema output."""
    norm_agent = normalize_agent_name(agent)
    schema = build_review_schema(mode)
    scratch_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".run-{norm_agent}-", dir=str(scratch_parent)) as tmp:
        tmp_path = Path(tmp)
        schema_path = tmp_path / "schema.json"
        prompt_path = tmp_path / "prompt.stdin.txt"
        out_path = tmp_path / "output.json"
        stdout_path = tmp_path / f"{norm_agent}.stdout.log"
        stderr_path = tmp_path / f"{norm_agent}.stderr.log"

        schema_json_compact = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        schema_path.write_text(schema_json_compact, encoding="utf-8")
        prompt_path.write_text(prompt, encoding="utf-8", errors="replace")

        if norm_agent == "claude":
            base_cmd = resolve_claude_command()
            cmd = [
                *base_cmd,
                "-p",
                "--output-format",
                "json",
                "--json-schema",
                schema_json_compact,
                "--tools",
                "Read,Glob,Grep,Bash",
                "--allowedTools",
                *CLAUDE_READONLY_ALLOWED_TOOLS,
                "--disallowedTools",
                "Edit",
                "Write",
                "NotebookEdit",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--no-session-persistence",
                "--no-chrome",
                "--disable-slash-commands",
            ]
            if not keep_mcp:
                cmd.append("--strict-mcp-config")
            if model.strip():
                cmd.extend(["--model", model.strip()])
            if effort.strip():
                cmd.extend(["--effort", effort.strip()])

            returncode, elapsed = _execute_headless_process(
                cmd, cwd, prompt_path, stdout_path, stderr_path, timeout_sec, "claude -p"
            )
            out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if returncode != 0:
                raise RuntimeError(f"claude -p exited with code {returncode}: {(err_text or out_text)[-2000:]}")
            parsed = _parse_claude_envelope(out_text, structured_schema=True)

        elif norm_agent == "agy":
            base_cmd = resolve_agy_command()
            cmd = [
                *base_cmd,
                "--mode",
                "plan",
                "--sandbox",
                "--output-format",
                "json",
                f"--json-schema={schema_path}",
                "--disable-slash-commands",
            ]
            if model.strip():
                cmd.extend(["--model", model.strip()])
            if effort.strip():
                cmd.extend(["--effort", effort.strip()])
            cmd.extend(_build_agy_prompt_args(prompt, prompt_path, tmp_path))

            returncode, elapsed = _execute_headless_process(
                cmd,
                cwd,
                prompt_path,
                stdout_path,
                stderr_path,
                timeout_sec,
                "agy -p",
                env=_build_agy_env(),
            )
            out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if returncode != 0:
                raise RuntimeError(f"agy -p exited with code {returncode}: {(out_text or err_text)[-2000:]}")
            parsed = _parse_agy_envelope(out_text, structured_schema=True)

        else:
            base_cmd = resolve_codex_command()
            cmd = [
                *base_cmd,
                "exec",
                "-C",
                str(cwd.resolve()),
            ]
            if model.strip():
                cmd.extend(["-m", model.strip()])
            if effort.strip():
                cmd.extend(["-c", f'model_reasoning_effort="{effort.strip()}"'])
            cmd.extend(
                [
                    "-c",
                    'approval_policy="never"',
                    "-c",
                    "notify=[]",
                    "-s",
                    "read-only",
                    "--ephemeral",
                    "--skip-git-repo-check",
                    "--output-schema",
                    str(schema_path),
                    "-o",
                    str(out_path),
                ]
            )
            if not keep_mcp:
                cmd.extend(_build_mcp_disable_overrides())
            cmd.append("-")

            returncode, elapsed = _execute_headless_process(
                cmd, cwd, prompt_path, stdout_path, stderr_path, timeout_sec, "codex exec"
            )
            if returncode != 0:
                err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
                out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
                raise RuntimeError(f"codex exec exited with code {returncode}: {(err_text or out_text)[-2000:]}")
            if not out_path.is_file():
                raise RuntimeError("codex exec succeeded but did not produce output JSON file.")
            raw_text = out_path.read_text(encoding="utf-8", errors="replace").strip()
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from codex exec: {exc}: {raw_text[:500]}") from exc

        validate_codex_response_consistency(parsed)
        return parsed, elapsed


def run_agent_freeform_task(
    prompt: str,
    task_kind: str,  # "discuss" (read-only) | "exec" (workspace-write)
    cwd: Path,
    scratch_parent: Path,
    agent: str = DEFAULT_AGENT,
    model: str = "",
    effort: str = "",
    resume_id: str = "",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    keep_mcp: bool = False,
) -> dict:
    """Run a general multi-agent collaboration task (`discuss` plan or `exec` TODO plan) via `codex`, `claude`, or `agy`."""
    if task_kind not in {"discuss", "exec"}:
        raise ValueError(f"Invalid task_kind: {task_kind!r}")
    norm_agent = normalize_agent_name(agent)
    scratch_parent.mkdir(parents=True, exist_ok=True)
    is_write = task_kind == "exec"

    with tempfile.TemporaryDirectory(prefix=f".{task_kind}-{norm_agent}-", dir=str(scratch_parent)) as tmp:
        tmp_path = Path(tmp)
        prompt_path = tmp_path / "prompt.stdin.txt"
        out_path = tmp_path / "output.txt"
        stdout_path = tmp_path / f"{norm_agent}.stdout.log"
        stderr_path = tmp_path / f"{norm_agent}.stderr.log"
        prompt_path.write_text(prompt, encoding="utf-8", errors="replace")

        if norm_agent == "claude":
            base_cmd = resolve_claude_command()
            cmd = [*base_cmd, "-p", "--output-format", "json", "--no-chrome", "--disable-slash-commands"]
            if resume_id.strip():
                cmd.extend(["-r", resume_id.strip()])
            else:
                cmd.append("--no-session-persistence")
            if is_write:
                cmd.extend(["--permission-mode", "acceptEdits", "--permission-prompts", "none"])
            else:
                cmd.extend(
                    [
                        "--tools",
                        "Read,Glob,Grep,Bash",
                        "--allowedTools",
                        *CLAUDE_READONLY_ALLOWED_TOOLS,
                        "--disallowedTools",
                        "Edit",
                        "Write",
                        "NotebookEdit",
                        "--permission-mode",
                        "dontAsk",
                        "--permission-prompts",
                        "none",
                    ]
                )
            if not keep_mcp:
                cmd.append("--strict-mcp-config")
            if model.strip():
                cmd.extend(["--model", model.strip()])
            if effort.strip():
                cmd.extend(["--effort", effort.strip()])

            returncode, elapsed = _execute_headless_process(
                cmd, cwd, prompt_path, stdout_path, stderr_path, timeout_sec, f"claude ({task_kind})"
            )
            out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if returncode != 0:
                raise RuntimeError(f"claude ({task_kind}) exited with code {returncode}: {(err_text or out_text)[-2000:]}")
            info = _parse_claude_envelope(out_text, structured_schema=False)
            return {
                "response": info["response"],
                "agent_session_id": info["agent_session_id"],
                "runtime_model": info.get("_runtime_model", ""),
                "elapsed_sec": round(elapsed, 2),
            }

        if norm_agent == "agy":
            base_cmd = resolve_agy_command()
            cmd = [*base_cmd, "--output-format", "json", "--disable-slash-commands"]
            if resume_id.strip():
                cmd.extend(["-r", resume_id.strip()])
            if is_write:
                cmd.extend(["--mode", "accept-edits", "--dangerously-skip-permissions"])
            else:
                cmd.extend(["--mode", "plan", "--sandbox"])
            if model.strip():
                cmd.extend(["--model", model.strip()])
            if effort.strip():
                cmd.extend(["--effort", effort.strip()])
            cmd.extend(_build_agy_prompt_args(prompt, prompt_path, tmp_path))

            returncode, elapsed = _execute_headless_process(
                cmd,
                cwd,
                prompt_path,
                stdout_path,
                stderr_path,
                timeout_sec,
                f"agy ({task_kind})",
                env=_build_agy_env(),
            )
            out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            if returncode != 0:
                raise RuntimeError(f"agy ({task_kind}) exited with code {returncode}: {(out_text or err_text)[-2000:]}")
            info = _parse_agy_envelope(out_text, structured_schema=False)
            return {
                "response": info["response"],
                "agent_session_id": info["agent_session_id"],
                "runtime_model": "",
                "elapsed_sec": round(elapsed, 2),
            }

        # Default: codex
        base_cmd = resolve_codex_command()
        sandbox_mode = "workspace-write" if is_write else "read-only"
        cmd = [*base_cmd, "exec"]
        if resume_id.strip():
            cmd.extend(["resume", resume_id.strip()])
        cmd.extend(
            [
                "-C",
                str(cwd.resolve()),
                "-c",
                'approval_policy="never"',
                "-c",
                "notify=[]",
                "-s",
                sandbox_mode,
                "--skip-git-repo-check",
                "-o",
                str(out_path),
            ]
        )
        if not resume_id.strip():
            cmd.append("--ephemeral")
        if model.strip():
            cmd.extend(["-m", model.strip()])
        if effort.strip():
            cmd.extend(["-c", f'model_reasoning_effort="{effort.strip()}"'])
        if not keep_mcp:
            cmd.extend(_build_mcp_disable_overrides())
        cmd.append("-")

        returncode, elapsed = _execute_headless_process(
            cmd, cwd, prompt_path, stdout_path, stderr_path, timeout_sec, f"codex ({task_kind})"
        )
        if returncode != 0:
            err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
            out_text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(f"codex ({task_kind}) exited with code {returncode}: {(err_text or out_text)[-2000:]}")
        resp_text = out_path.read_text(encoding="utf-8", errors="replace").strip() if out_path.is_file() else ""
        return {
            "response": resp_text,
            "agent_session_id": resume_id.strip(),
            "runtime_model": "",
            "elapsed_sec": round(elapsed, 2),
        }


def read_codex_default_profile() -> tuple[str, str]:
    return read_agent_default_profile("codex")

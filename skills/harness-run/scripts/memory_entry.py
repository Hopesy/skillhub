"""Small event entrypoint; only metadata is persisted outside the project."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from memory_store import digest, packed, read_text, safe_path


def navigation(project, host="generic", session=None, event="startup", plan=None, state_dir=None):
    root = Path(project).resolve()
    if not root.is_dir():
        raise ValueError("project-missing")
    if event not in {"startup", "compact", "clear", "resume", "fork"}:
        raise ValueError("event-invalid")
    warnings = []
    plan_hash = ""
    target = "从 AGENTS/README 定位当前执行计划"
    if plan:
        try:
            path = safe_path(root, plan)
            plan_hash = digest(read_text(path))
            target = path.relative_to(root).as_posix()
        except (OSError, ValueError, UnicodeError):
            warnings.append("plan-unavailable")
            plan_hash = "unavailable"
    text = (
        "harness-run：接续用户目标，按需读取项目知识。\n"
        f"项目根：{root}；先读 AGENTS.md 的导航与记录约定。\n"
        f"当前计划入口：{target}。\n"
        "恢复时先核对接续摘要：目标、最新用户约束及来源、当前切片、已验证版本与证据、待验证项、下一动作。\n"
        "结合当前代码和工作区变化确认事实；找历史依据时使用 skill 的 memory.py recall，再按 ref read。\n"
        "现行规则按来源、条件与替代关系核对；needs-review 先查明。重要决定写回原主题，回读后在计划留链接。\n"
        "继续目标内最高价值切片；最终对照原始目标完成所需测试和真实操作验收。"
    )
    payload_hash = digest(text + "\n" + plan_hash)
    result = {"status": "injected", "context": text, "warnings": warnings}
    if not session or not str(session).strip():
        return result
    cache = Path(state_dir) if state_dir else Path(os.environ.get("LOCALAPPDATA") or
                os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "harness-run" / "context"
    identity = digest(packed([os.path.normcase(str(root)), host, str(session).strip()]))
    path = cache / (identity + ".json")
    previous = {}
    try:
        previous = json.loads(read_text(path, 8192))
        if not isinstance(previous, dict):
            previous = {}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, UnicodeError):
        warnings.append("state-unreadable")
    if event == "startup" and previous.get("event") == event and previous.get("hash") == payload_hash:
        result.update(status="deduped", context="")
    metadata = {"event": event, "hash": payload_hash, "status": result["status"], "chars": len(result["context"])}
    temp = None
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(packed(metadata))
        os.replace(temp, path)
    except OSError:
        warnings.append("state-write-failed")
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return result

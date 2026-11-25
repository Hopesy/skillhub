#!/usr/bin/env python3
"""Register harness-run SessionStart in one project's host configuration."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile

from memory_store import packed, read_text, safe_path


HOOK_NAME = "harness-run"
MARKER = "harness-run: session context"
POWERSHELL_PREFIX = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
CONFIGS = {
    "antigravity": ".agents/hooks.json",
    "claude": ".claude/settings.json",
    "codex": ".codex/hooks.json",
}
ANTIGRAVITY_ALT_CONFIGS = (".agent/hooks.json", "_agents/hooks.json", "_agent/hooks.json")
ANTIGRAVITY_FLAT_EVENTS = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
ANTIGRAVITY_GROUPED_EVENTS = {"PreToolUse", "PostToolUse"}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("config-duplicate-key: " + key)
        result[key] = value
    return result


def windows_command(argv):
    # Antigravity and Codex default to cmd.exe on Windows. Encoded PowerShell
    # also works with a PowerShell runner and preserves %, !, $ and quotes.
    code = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    code += "; exit $LASTEXITCODE"
    encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
    return POWERSHELL_PREFIX + encoded


def hook_entry(project, host, plan=None, script=None):
    script = Path(script or Path(__file__).with_name("session_start.py")).resolve()
    if not script.is_file():
        raise ValueError("hook-script-missing")
    argv = [sys.executable, "-B", "-X", "utf8", str(script),
            "--host", host, "--project", str(project)]
    if plan:
        if not safe_path(project, plan).is_file():
            raise ValueError("plan-missing")
        argv += ["--plan", plan]
    if host == "antigravity":
        command = windows_command(argv) if os.name == "nt" else shlex.join(argv)
        return {"type": "command", "command": command, "timeout": 10}
    handler = {"type": "command", "timeout": 10, "statusMessage": MARKER}
    if host == "claude":
        # Exec form passes paths literally, independent of the host's shell.
        handler.update(command=argv[0], args=argv[1:])
    else:
        handler.update(command=shlex.join(argv), additionalContextLimit=2000)
        if os.name == "nt":
            handler["commandWindows"] = windows_command(argv)
    return {"matcher": "^(startup|resume|clear|compact|fork)$", "hooks": [handler]}


def validate_antigravity_handler(item):
    if not isinstance(item, dict):
        raise ValueError("config-handler-object-required")
    if "type" in item and item["type"] != "command":
        raise ValueError("config-handler-type-invalid")
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("config-handler-command-required")


def is_owned_antigravity_handler(item):
    if item.get("type", "command") != "command":
        return False
    command = item.get("command")
    if not isinstance(command, str):
        return False
    if command.startswith(POWERSHELL_PREFIX):
        token = command[len(POWERSHELL_PREFIX):].strip()
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-16le")
        except (ValueError, UnicodeError):
            return False
        return (decoded.startswith("& '") and decoded.endswith("; exit $LASTEXITCODE")
                and " '-B' '-X' 'utf8' " in decoded
                and " '--host' 'antigravity' '--project' " in decoded)
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    return (len(argv) >= 9 and argv[1:4] == ["-B", "-X", "utf8"]
            and argv[4].replace("\\", "/").rsplit("/", 1)[-1] == "session_start.py"
            and argv[5:8] == ["--host", "antigravity", "--project"])


def merge_antigravity_config(data, handler):
    if not isinstance(data, dict):
        raise ValueError("config-object-required")
    for spec in data.values():
        if not isinstance(spec, dict):
            raise ValueError("config-hook-spec-object-required")
        if "enabled" in spec and not isinstance(spec["enabled"], bool):
            raise ValueError("config-hook-enabled-bool-required")
        for event in ANTIGRAVITY_FLAT_EVENTS & spec.keys():
            if not isinstance(spec[event], list):
                raise ValueError("config-session-start-list-required" if event == "SessionStart"
                                 else f"config-{event}-list-required")
            for item in spec[event]:
                validate_antigravity_handler(item)
        for event in ANTIGRAVITY_GROUPED_EVENTS & spec.keys():
            if not isinstance(spec[event], list):
                raise ValueError(f"config-{event}-list-required")
            for group in spec[event]:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError("config-matcher-hooks-list-required")
                for item in group["hooks"]:
                    validate_antigravity_handler(item)
    result = copy.deepcopy(data)
    owned = result.setdefault(HOOK_NAME, {})
    entries = owned.get("SessionStart", [])
    retained = [item for item in entries if not is_owned_antigravity_handler(item)]
    owned["SessionStart"] = [*retained, handler]
    return result


def merge_config(data, entry, host="codex"):
    if host == "antigravity":
        return merge_antigravity_config(data, entry)
    if not isinstance(data, dict):
        raise ValueError("config-object-required")
    result = copy.deepcopy(data)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("config-hooks-object-required")
    entries = hooks.get("SessionStart", [])
    if not isinstance(entries, list):
        raise ValueError("config-session-start-list-required")
    retained = []
    for item_group in entries:
        if not isinstance(item_group, dict) or not isinstance(item_group.get("hooks"), list):
            raise ValueError("config-matcher-hooks-list-required")
        if any(not isinstance(item, dict) for item in item_group["hooks"]):
            raise ValueError("config-handler-object-required")
        remaining = [item for item in item_group["hooks"]
                     if not (item.get("type") == "command" and item.get("statusMessage") == MARKER)]
        if remaining == item_group["hooks"]:
            retained.append(item_group)
        elif remaining:
            retained.append({**item_group, "hooks": remaining})
    hooks["SessionStart"] = [*retained, entry]
    return result


def install(project, host, plan=None, script=None, check=False, session_cwd=None):
    root = Path(project).resolve()
    if not root.is_dir():
        raise ValueError("project-missing")
    if host not in CONFIGS:
        raise ValueError("host-invalid")
    path = safe_path(root, CONFIGS[host])
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original.decode("utf-8-sig"), object_pairs_hook=unique_keys) if original is not None else {}
    merged = merge_config(data, hook_entry(root, host, plan, script), host)
    changed = data != merged
    result = {"status": "needs-update" if changed else "unchanged", "host": host,
              "project": str(root), "config": str(path), "session_scope": "unverified", "warnings": []}
    if session_cwd is not None:
        session_root = Path(session_cwd).resolve()
        if not session_root.is_dir():
            raise ValueError("session-cwd-missing")
        result["session_scope"] = "inside-project" if session_root.is_relative_to(root) else "outside-project"
        if result["session_scope"] == "outside-project":
            result["warnings"].append("session-cwd-outside-project: open the host session in the project to load its hooks")
    if host == "codex":
        toml = safe_path(root, ".codex/config.toml")
        if toml.is_file() and "[hooks" in read_text(toml):
            result["warnings"].append("codex-inline-hooks-present: both project hook sources will load")
    elif host == "antigravity":
        for alt in ANTIGRAVITY_ALT_CONFIGS:
            if safe_path(root, alt).is_file():
                result["warnings"].append(f"antigravity-extra-hooks-present: {alt} will also load")
    if check or not changed:
        return result
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
        if original is not None:
            temp.chmod(path.stat().st_mode)
        # Refuse to overwrite a concurrent edit made while preparing the merge.
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise ValueError("config-changed-during-install")
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    result["status"] = "updated"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--host", required=True, choices=CONFIGS)
    parser.add_argument("--plan", help="Optional project-relative current plan")
    parser.add_argument("--check", action="store_true", help="Report drift without writing")
    parser.add_argument("--session-cwd", help="Actual host session cwd, from session metadata rather than a tool's cwd override")
    args = parser.parse_args()
    try:
        result = install(args.project, args.host, args.plan, check=args.check, session_cwd=args.session_cwd)
        print(packed(result))
        return 1 if args.check and (result["status"] == "needs-update" or result["session_scope"] == "outside-project") else 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(packed({"status": "error", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

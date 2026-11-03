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


MARKER = "harness-run: session context"
CONFIGS = {"claude": ".claude/settings.json", "codex": ".codex/hooks.json"}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("config-duplicate-key: " + key)
        result[key] = value
    return result


def hook_group(project, host, plan=None, script=None):
    script = Path(script or Path(__file__).with_name("session_start.py")).resolve()
    if not script.is_file():
        raise ValueError("hook-script-missing")
    argv = [sys.executable, "-B", "-X", "utf8", str(script),
            "--host", host, "--project", str(project)]
    if plan:
        if not safe_path(project, plan).is_file():
            raise ValueError("plan-missing")
        argv += ["--plan", plan]
    handler = {"type": "command", "timeout": 10, "statusMessage": MARKER}
    if host == "claude":
        # Exec form passes paths literally, independent of the host's shell.
        handler.update(command=argv[0], args=argv[1:])
    else:
        handler.update(command=shlex.join(argv), additionalContextLimit=2000)
        if os.name == "nt":
            # Codex defaults to cmd.exe on Windows. Encoded PowerShell also works
            # with a configured PowerShell runner and preserves %, !, $ and quotes.
            code = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
            code += "; exit $LASTEXITCODE"
            encoded = base64.b64encode(code.encode("utf-16le")).decode("ascii")
            handler["commandWindows"] = "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    return {"matcher": "^(startup|resume|clear|compact|fork)$", "hooks": [handler]}


def merge_config(data, group):
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
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            raise ValueError("config-matcher-hooks-list-required")
        if any(not isinstance(item, dict) for item in entry["hooks"]):
            raise ValueError("config-handler-object-required")
        remaining = [item for item in entry["hooks"]
                     if not (item.get("type") == "command" and item.get("statusMessage") == MARKER)]
        if remaining == entry["hooks"]:
            retained.append(entry)
        elif remaining:
            retained.append({**entry, "hooks": remaining})
    hooks["SessionStart"] = [*retained, group]
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
    merged = merge_config(data, hook_group(root, host, plan, script))
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

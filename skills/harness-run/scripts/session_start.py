#!/usr/bin/env python3
"""Adapter for host SessionStart JSON contracts (Antigravity, Claude Code, Codex)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from memory_entry import navigation
from memory_store import packed


def session_locations(data: dict, host: str) -> list[Path]:
    locations = []
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        locations.append(Path(cwd).resolve())
    if host == "antigravity" and isinstance(data.get("workspacePaths"), list):
        for item in data["workspacePaths"]:
            if isinstance(item, str) and item.strip():
                resolved = Path(item).resolve()
                if resolved not in locations:
                    locations.append(resolved)
    return locations


def session_identity(data: dict, host: str) -> str | None:
    keys = ("conversationId", "session_id") if host == "antigravity" else ("session_id",)
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def format_output(host: str, result: dict) -> dict:
    if result["status"] == "deduped":
        return {}
    if host == "antigravity":
        return {"injectSteps": [{"systemMessage": {"systemMessage": result["context"]}}]}
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": result["context"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--project", help="Registered project root, including for subdirectory sessions")
    parser.add_argument("--state-dir")
    parser.add_argument("--plan")
    args = parser.parse_args()
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            raise ValueError("hook-input-too-large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("hook-project-required")
        locations = session_locations(data, args.host)
        if not locations:
            raise ValueError("hook-project-required")
        root = Path(args.project).resolve() if args.project else locations[0]
        if not any(loc.is_relative_to(root) for loc in locations):
            print("{}")
            return 0
        source = data.get("source")
        event = "startup" if source is None else source
        result = navigation(root, args.host, session_identity(data, args.host),
                            event, args.plan, args.state_dir)
        if result["warnings"]:
            print(packed(result["warnings"]), file=sys.stderr)
        print(packed(format_output(args.host, result)))
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print("harness-run SessionStart: " + str(exc), file=sys.stderr)
        print("{}")
        return 1


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

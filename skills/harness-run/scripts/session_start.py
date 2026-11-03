#!/usr/bin/env python3
"""Adapter for hosts with the SessionStart additionalContext JSON contract."""
import argparse
import json
from pathlib import Path
import sys

from memory_entry import navigation
from memory_store import packed


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
        if not isinstance(data, dict) or not isinstance(data.get("cwd"), str):
            raise ValueError("hook-project-required")
        cwd = Path(data["cwd"]).resolve()
        root = Path(args.project).resolve() if args.project else cwd
        if not cwd.is_relative_to(root):
            print("{}")
            return 0
        result = navigation(root, args.host, data.get("session_id"),
                            data.get("source", "startup"), args.plan, args.state_dir)
        if result["warnings"]:
            print(packed(result["warnings"]), file=sys.stderr)
        output = {} if result["status"] == "deduped" else {
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": result["context"]}}
        print(packed(output))
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print("harness-run SessionStart: " + str(exc), file=sys.stderr)
        print("{}")
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

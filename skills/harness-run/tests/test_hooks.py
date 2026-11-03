"""Project registration and execution through the saved host commands."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from install_hooks import CONFIGS, MARKER, install


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-hooks-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "项目 & spaces $() ` ' %PATH% !name!"
        self.root.mkdir()
        self.cache = self.base / "cache"
        self.env = {**os.environ, "LOCALAPPDATA": str(self.cache), "PYTHONDONTWRITEBYTECODE": "1"}

    def path(self, host):
        return self.root / CONFIGS[host]

    def save(self, host, text):
        path = self.path(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def handler(self, host):
        data = json.loads(self.path(host).read_text(encoding="utf-8"))
        return [h for group in data["hooks"]["SessionStart"] for h in group["hooks"]
                if h.get("statusMessage") == MARKER][0]

    def fire(self, host, cwd=None, source="startup", session="session", powershell=False):
        handler = self.handler(host)
        groups = json.loads(self.path(host).read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        self.assertTrue(any(re.search(g.get("matcher", ".*"), source) and handler in g["hooks"] for g in groups))
        if host == "claude":
            argv = [handler["command"], *handler["args"]]
        elif os.name == "nt":
            command = handler["commandWindows"]
            if powershell:
                argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
            else:
                # Match Codex's raw command argument to its Windows cmd runner.
                argv = 'cmd.exe /d /s /c "' + command + '"'
        else:
            argv = ["/bin/sh", "-c", handler["command"]]
        data = {"cwd": str(cwd or self.root), "session_id": session, "source": source}
        run = subprocess.run(argv, cwd=cwd or self.root, env=self.env,
                             input=json.dumps(data, ensure_ascii=False), capture_output=True,
                             text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_merge_preserves_other_settings_and_is_byte_idempotent(self):
        other = {"type": "command", "command": "echo session_start.py --host codex"}
        for host in CONFIGS:
            with self.subTest(host=host):
                original = {"permissions": {"allow": ["Read"]}, "custom": "项目设置",
                            "hooks": {"Stop": [{"hooks": [other]}],
                                      "SessionStart": [{"matcher": "resume", "hooks": [other]}]}}
                self.save(host, json.dumps(original))
                self.assertEqual(install(self.root, host)["status"], "updated")
                path = self.path(host)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["permissions"], original["permissions"])
                self.assertEqual(data["custom"], original["custom"])
                self.assertEqual(data["hooks"]["Stop"], original["hooks"]["Stop"])
                self.assertEqual(data["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
                before = path.read_bytes()
                self.assertEqual(install(self.root, host)["status"], "unchanged")
                self.assertEqual(path.read_bytes(), before)

    def test_relocation_updates_owned_entry_and_keeps_mixed_handlers(self):
        moved = self.base / "moved skill"
        shutil.copytree(SCRIPTS, moved, ignore=shutil.ignore_patterns("__pycache__"))
        for host in CONFIGS:
            with self.subTest(host=host):
                install(self.root, host)
                old = self.handler(host)
                other = {"type": "command", "command": "echo other"}
                self.save(host, json.dumps({"hooks": {"SessionStart": [
                    {"matcher": "resume", "hooks": [old, other]},
                    {"matcher": "startup", "hooks": [old]}]}}))
                install(self.root, host, script=moved / "session_start.py")
                data = json.loads(self.path(host).read_text(encoding="utf-8"))
                groups = data["hooks"]["SessionStart"]
                self.assertEqual(groups[0], {"matcher": "resume", "hooks": [other]})
                self.assertEqual(sum(h.get("statusMessage") == MARKER for g in groups for h in g["hooks"]), 1)
                self.assertNotEqual(self.handler(host), old)
                self.assertIn(str(self.root), self.fire(host)["hookSpecificOutput"]["additionalContext"])

    def test_malformed_config_preserved(self):
        for text in ("broken", "[]", '{"hooks":null}', '{"hooks":{"SessionStart":{}}}',
                     '{"hooks":{"SessionStart":[{}]}}',
                     '{"hooks":{"SessionStart":[{"hooks":[1]}]}}',
                     '{"hooks":{},"hooks":{}}'):
            for host in CONFIGS:
                with self.subTest(host=host, text=text):
                    path = self.save(host, text)
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        install(self.root, host)
                    self.assertEqual(path.read_bytes(), before)

    def test_check_is_read_only_and_cli_reports_drift(self):
        for host in CONFIGS:
            command = [sys.executable, "-B", str(SCRIPTS / "install_hooks.py"),
                       "--project", str(self.root), "--host", host, "--check"]
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stdout)["status"], "needs-update")
            self.assertFalse(self.path(host).parent.exists())
            run = subprocess.run(command[:-1], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stdout)
            self.assertEqual(json.loads(run.stdout)["status"], "updated")
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0)
            self.assertEqual(json.loads(run.stdout)["status"], "unchanged")

    def test_nested_cwd_restore_dedup_and_project_isolation(self):
        nested = self.root / "src" / "feature"
        nested.mkdir(parents=True)
        for host in CONFIGS:
            with self.subTest(host=host):
                install(self.root, host)
                first = self.fire(host, nested)["hookSpecificOutput"]
                self.assertEqual(first["hookEventName"], "SessionStart")
                self.assertIn(str(self.root), first["additionalContext"])
                self.assertIn("AGENTS.md", first["additionalContext"])
                self.assertNotIn(str(nested), first["additionalContext"])
                self.assertEqual(self.fire(host, self.root), {})
                for event in ("resume", "compact", "clear", "fork"):
                    self.assertIn("最新用户约束", self.fire(host, nested, event)["hookSpecificOutput"]["additionalContext"])
                self.assertEqual(self.fire(host, self.base), {})
        for path in self.cache.rglob("*.json"):
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"event", "hash", "status", "chars"})

    @unittest.skipUnless(os.name == "nt", "Windows-specific shell compatibility")
    def test_codex_command_also_runs_from_powershell(self):
        install(self.root, "codex")
        self.assertIn("hookSpecificOutput", self.fire("codex", powershell=True))

    def test_plan_binding_change_and_archive_fallback(self):
        plan = self.root / "current plan.md"
        plan.write_text("# Plan\n最新约束：保留 ID。", encoding="utf-8")
        for host in CONFIGS:
            install(self.root, host, plan.name)
            self.assertIn(plan.name, self.fire(host)["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(self.fire(host), {})
        plan.write_text("# Plan\n最新约束：原始大小写。", encoding="utf-8")
        for host in CONFIGS:
            self.assertIn("hookSpecificOutput", self.fire(host))
        plan.rename(self.root / "completed.md")
        for host in CONFIGS:
            self.assertIn("AGENTS/README", self.fire(host)["hookSpecificOutput"]["additionalContext"])

    def test_only_selected_project_and_host_are_written(self):
        sibling = self.base / "second-project"
        sibling.mkdir()
        fake_home = self.base / "home"
        fake_home.mkdir()
        for host, rel in CONFIGS.items():
            path = fake_home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel":"global"}', encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(fake_home), "USERPROFILE": str(fake_home),
                                     "CODEX_HOME": str(fake_home / ".codex")}):
            install(self.root, "codex")
        self.assertFalse((self.root / ".claude").exists())
        self.assertEqual(list(sibling.iterdir()), [])
        for rel in CONFIGS.values():
            self.assertEqual((fake_home / rel).read_text(encoding="utf-8"), '{"sentinel":"global"}')
        install(sibling, "claude")
        self.assertFalse((sibling / ".codex").exists())

    def test_session_directory_mismatch_is_explicit_even_with_valid_registration(self):
        for host in CONFIGS:
            install(self.root, host)
            before = self.path(host).read_bytes()
            result = install(self.root, host, check=True, session_cwd=self.base)
            self.assertEqual(result["status"], "unchanged")
            self.assertEqual(result["session_scope"], "outside-project")
            self.assertTrue(result["warnings"])
            self.assertEqual(self.path(host).read_bytes(), before)
            command = [sys.executable, "-B", "-X", "utf8", str(SCRIPTS / "install_hooks.py"),
                       "--project", str(self.root), "--host", host, "--check", "--session-cwd", str(self.base)]
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 1)
            child = self.root / "src"
            child.mkdir(exist_ok=True)
            self.assertEqual(install(self.root, host, check=True, session_cwd=child)["session_scope"], "inside-project")

    def test_codex_inline_config_is_retained_and_reported(self):
        path = self.root / ".codex/config.toml"
        path.parent.mkdir()
        original = 'model = "example"\n[[hooks.Stop]]\nmatcher = "test"\n'
        path.write_text(original, encoding="utf-8")
        result = install(self.root, "codex")
        self.assertIn("codex-inline-hooks-present", result["warnings"][0])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_atomic_write_failure_leaves_original(self):
        for host in CONFIGS:
            path = self.save(host, '{"custom":"keep"}')
            with patch("install_hooks.os.replace", side_effect=OSError("write failure")):
                with self.assertRaises(OSError):
                    install(self.root, host)
            self.assertEqual(path.read_text(encoding="utf-8"), '{"custom":"keep"}')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_config_directory_escape_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        link = self.root / ".claude"
        if os.name == "nt":
            run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                  "New-Item -ItemType Junction -Path $env:HR_LINK -Target $env:HR_TARGET | Out-Null"],
                                 env={**os.environ, "HR_LINK": str(link), "HR_TARGET": str(outside)},
                                 capture_output=True)
            if run.returncode:
                self.skipTest("junction creation unavailable")
        else:
            link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "outside-project"):
            install(self.root, "claude")
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

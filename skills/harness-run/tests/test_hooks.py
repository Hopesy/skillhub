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
from install_hooks import CONFIGS, HOOK_NAME, MARKER, install, is_owned_antigravity_handler


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
        if host == "antigravity":
            return [h for h in data[HOOK_NAME]["SessionStart"] if is_owned_antigravity_handler(h)][0]
        return [h for group in data["hooks"]["SessionStart"] for h in group["hooks"]
                if h.get("statusMessage") == MARKER][0]

    def context_text(self, host, payload):
        if host == "antigravity":
            self.assertNotIn("hookSpecificOutput", payload)
            self.assertEqual(len(payload["injectSteps"]), 1)
            return payload["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return payload["hookSpecificOutput"]["additionalContext"]

    def fire(self, host, cwd=None, source="startup", session="session", powershell=False, workspace_only=False):
        handler = self.handler(host)
        target = cwd or self.root
        exec_cwd = target
        if host == "antigravity":
            self.assertEqual(set(handler), {"type", "command", "timeout"})
            exec_cwd = self.path(host).parent
            if os.name == "nt":
                if powershell:
                    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", handler["command"]]
                else:
                    argv = ["cmd.exe", "/c", handler["command"]]
            else:
                argv = ["/bin/sh", "-c", handler["command"]]
            data = {"conversationId": session, "hookEventName": "SessionStart",
                    "workspacePaths": [str(target)], "modelName": "auto"}
            if not workspace_only:
                data["cwd"] = str(target)
            if source != "startup":
                data["source"] = source
        else:
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
            data = {"cwd": str(target), "session_id": session, "source": source}
        run = subprocess.run(argv, cwd=exec_cwd, env=self.env,
                             input=json.dumps(data, ensure_ascii=False), capture_output=True,
                             text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_merge_preserves_other_settings_and_is_byte_idempotent(self):
        for host in CONFIGS:
            with self.subTest(host=host):
                other = {"type": "command", "command": f"echo session_start.py --host {host}"}
                if host == "antigravity":
                    original = {
                        "lint-checker": {
                            "PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./scripts/lint.sh"}]}]
                        },
                        "safety-gate": {"enabled": False, "SessionStart": [other]},
                        HOOK_NAME: {"enabled": False, "Stop": [{"command": "echo stop"}], "SessionStart": [other]},
                    }
                else:
                    original = {"permissions": {"allow": ["Read"]}, "custom": "项目设置",
                                "hooks": {"Stop": [{"hooks": [other]}],
                                          "SessionStart": [{"matcher": "resume", "hooks": [other]}]}}
                self.save(host, json.dumps(original))
                self.assertEqual(install(self.root, host)["status"], "updated")
                path = self.path(host)
                data = json.loads(path.read_text(encoding="utf-8"))
                if host == "antigravity":
                    self.assertEqual(data["lint-checker"], original["lint-checker"])
                    self.assertEqual(data["safety-gate"], original["safety-gate"])
                    self.assertFalse(data[HOOK_NAME]["enabled"])
                    self.assertEqual(data[HOOK_NAME]["Stop"], original[HOOK_NAME]["Stop"])
                    self.assertEqual(data[HOOK_NAME]["SessionStart"][0], other)
                else:
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
                if host == "antigravity":
                    self.save(host, json.dumps({
                        "other-hook": {"SessionStart": [other]},
                        HOOK_NAME: {"SessionStart": [old, other]},
                    }))
                else:
                    self.save(host, json.dumps({"hooks": {"SessionStart": [
                        {"matcher": "resume", "hooks": [old, other]},
                        {"matcher": "startup", "hooks": [old]}]}}))
                install(self.root, host, script=moved / "session_start.py")
                data = json.loads(self.path(host).read_text(encoding="utf-8"))
                if host == "antigravity":
                    self.assertEqual(data["other-hook"], {"SessionStart": [other]})
                    self.assertEqual(data[HOOK_NAME]["SessionStart"][0], other)
                    self.assertEqual(sum(is_owned_antigravity_handler(h) for h in data[HOOK_NAME]["SessionStart"]), 1)
                else:
                    groups = data["hooks"]["SessionStart"]
                    self.assertEqual(groups[0], {"matcher": "resume", "hooks": [other]})
                    self.assertEqual(sum(h.get("statusMessage") == MARKER for g in groups for h in g["hooks"]), 1)
                self.assertNotEqual(self.handler(host), old)
                self.assertIn(str(self.root), self.context_text(host, self.fire(host)))

    def test_malformed_config_preserved(self):
        common = ("broken", "[]", '{"hooks":null}', '{"hooks":{"SessionStart":{}}}',
                  '{"hooks":{"SessionStart":[{}]}}',
                  '{"hooks":{"SessionStart":[{"hooks":[1]}]}}',
                  '{"hooks":{},"hooks":{}}')
        for text in common:
            for host in CONFIGS:
                with self.subTest(host=host, text=text):
                    path = self.save(host, text)
                    before = path.read_bytes()
                    with self.assertRaises(ValueError):
                        install(self.root, host)
                    self.assertEqual(path.read_bytes(), before)
        for text in ('{"harness-run":{"enabled":"yes"}}',
                     '{"harness-run":{"PreToolUse":[{}]}}',
                     '{"harness-run":{"SessionStart":[{"type":"http","command":"x"}]}}'):
            with self.subTest(host="antigravity", text=text):
                path = self.save("antigravity", text)
                before = path.read_bytes()
                with self.assertRaises(ValueError):
                    install(self.root, "antigravity")
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
                first_text = self.context_text(host, self.fire(host, nested))
                self.assertIn(str(self.root), first_text)
                self.assertIn("AGENTS.md", first_text)
                self.assertNotIn(str(nested), first_text)
                self.assertEqual(self.fire(host, self.root), {})
                for event in ("resume", "compact", "clear", "fork"):
                    self.assertIn("最新用户约束", self.context_text(host, self.fire(host, nested, event)))
                self.assertEqual(self.fire(host, self.base), {})
        for path in self.cache.rglob("*.json"):
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"event", "hash", "status", "chars"})

    @unittest.skipUnless(os.name == "nt", "Windows-specific shell compatibility")
    def test_windows_commands_also_run_from_powershell(self):
        for host in ("antigravity", "codex"):
            with self.subTest(host=host):
                install(self.root, host)
                self.assertIn(str(self.root), self.context_text(host, self.fire(host, powershell=True)))

    def test_plan_binding_change_and_archive_fallback(self):
        plan = self.root / "current plan.md"
        plan.write_text("# Plan\n最新约束：保留 ID。", encoding="utf-8")
        for host in CONFIGS:
            install(self.root, host, plan.name)
            self.assertIn(plan.name, self.context_text(host, self.fire(host)))
            self.assertEqual(self.fire(host), {})
        plan.write_text("# Plan\n最新约束：原始大小写。", encoding="utf-8")
        for host in CONFIGS:
            self.assertIn(plan.name, self.context_text(host, self.fire(host)))
        plan.rename(self.root / "completed.md")
        for host in CONFIGS:
            self.assertIn("AGENTS/README", self.context_text(host, self.fire(host)))

    def test_only_selected_project_and_host_are_written(self):
        sibling = self.base / "second-project"
        sibling.mkdir()
        third = self.base / "third-project"
        third.mkdir()
        fake_home = self.base / "home"
        fake_home.mkdir()
        for host, rel in CONFIGS.items():
            path = fake_home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel":"global"}', encoding="utf-8")
        for path in (fake_home / ".gemini/config/hooks.json",):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"sentinel":"global"}', encoding="utf-8")
        with patch.dict(os.environ, {"HOME": str(fake_home), "USERPROFILE": str(fake_home),
                                     "CODEX_HOME": str(fake_home / ".codex")}):
            install(self.root, "codex")
        self.assertFalse((self.root / ".claude").exists())
        self.assertFalse((self.root / ".agents").exists())
        self.assertEqual(list(sibling.iterdir()), [])
        for rel in (*CONFIGS.values(), ".gemini/config/hooks.json"):
            self.assertEqual((fake_home / rel).read_text(encoding="utf-8"), '{"sentinel":"global"}')
        install(sibling, "claude")
        self.assertFalse((sibling / ".codex").exists())
        self.assertFalse((sibling / ".agents").exists())
        install(third, "antigravity")
        self.assertFalse((third / ".claude").exists())
        self.assertFalse((third / ".codex").exists())

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

    def test_antigravity_workspace_paths_multi_root_and_extra_hooks_warning(self):
        alt = self.root / ".agent/hooks.json"
        alt.parent.mkdir()
        original_alt = '{"other":{"SessionStart":[{"command":"echo hi"}]}}\n'
        alt.write_text(original_alt, encoding="utf-8")
        # Pre-seed a POSIX command form to verify cross-platform owned-entry replacement.
        posix_old = {
            "type": "command",
            "command": "'/usr/bin/python3' -B -X utf8 '/opt/harness-run/scripts/session_start.py' --host antigravity --project '/tmp/proj'",
            "timeout": 10,
        }
        self.save("antigravity", json.dumps({HOOK_NAME: {"SessionStart": [posix_old]}}))
        result = install(self.root, "antigravity")
        self.assertEqual(result["status"], "updated")
        self.assertIn("antigravity-extra-hooks-present", result["warnings"][0])
        self.assertEqual(alt.read_text(encoding="utf-8"), original_alt)
        saved = json.loads(self.path("antigravity").read_text(encoding="utf-8"))
        self.assertEqual(len(saved[HOOK_NAME]["SessionStart"]), 1)

        # Antigravity SessionStart payload with workspacePaths only (no cwd).
        first = self.fire("antigravity", session="conv-1", workspace_only=True)
        self.assertIn(str(self.root), self.context_text("antigravity", first))
        self.assertEqual(self.fire("antigravity", session="conv-1", workspace_only=True), {})
        second = self.fire("antigravity", session="conv-2", workspace_only=True)
        self.assertIn(str(self.root), self.context_text("antigravity", second))
        self.assertEqual(self.fire("antigravity", cwd=self.base, session="conv-3", workspace_only=True), {})

        # Multi-workspace session where cwd is another workspace but workspacePaths includes self.root.
        sibling = self.base / "other-workspace"
        sibling.mkdir()
        handler = self.handler("antigravity")
        argv = ["cmd.exe", "/c", handler["command"]] if os.name == "nt" else ["/bin/sh", "-c", handler["command"]]
        multi = {"conversationId": "conv-multi", "cwd": str(sibling), "workspacePaths": [str(sibling), str(self.root)]}
        run = subprocess.run(argv, cwd=self.path("antigravity").parent, env=self.env,
                             input=json.dumps(multi, ensure_ascii=False), capture_output=True,
                             text=True, encoding="utf-8", timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(str(self.root), self.context_text("antigravity", json.loads(run.stdout)))

    def test_atomic_write_failure_leaves_original(self):
        for host in CONFIGS:
            seed = '{"other":{"SessionStart":[{"command":"echo keep"}]}}' if host == "antigravity" else '{"custom":"keep"}'
            path = self.save(host, seed)
            with patch("install_hooks.os.replace", side_effect=OSError("write failure")):
                with self.assertRaises(OSError):
                    install(self.root, host)
            self.assertEqual(path.read_text(encoding="utf-8"), seed)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_config_directory_escape_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        for host, rel in CONFIGS.items():
            with self.subTest(host=host):
                link = self.root / Path(rel).parts[0]
                if os.name == "nt":
                    run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                          "New-Item -ItemType Junction -Path $env:HR_LINK -Target $env:HR_TARGET | Out-Null"],
                                         env={**os.environ, "HR_LINK": str(link), "HR_TARGET": str(outside)},
                                         capture_output=True)
                    if run.returncode:
                        self.skipTest("junction creation unavailable")
                    self.addCleanup(lambda p=link: p.rmdir() if p.exists() else None)
                else:
                    link.symlink_to(outside, target_is_directory=True)
                    self.addCleanup(link.unlink)
                with self.assertRaisesRegex(ValueError, "outside-project"):
                    install(self.root, host)
                self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the `code-review` deterministic engine."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import code_review
from anchoring import anchor_and_filter_findings, resolve_snippet_lines
from codex_runner import (
    SelectionResult,
    _parse_agy_envelope,
    _parse_claude_envelope,
    build_code_prompt,
    normalize_agent_name,
    validate_codex_response_consistency,
)
from schemas import build_review_schema
from selection import (
    CREATE_NO_WINDOW,
    DiffFile,
    _populate_patch_stats,
    classify_exclusion,
    collect_diff_selection,
    empty_tree_hash,
    is_secret_path,
)
from session_store import SessionStore, default_temp_root, resolve_artifact_root


@contextlib.contextmanager
def project_temp_dir():
    """Keep all unit-test temporary files inside an isolated per-case dir under `.<project>-temp` (F28, F40)."""
    cwd = Path.cwd().resolve()
    root = default_temp_root(cwd).parent / "unit-tests"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-", dir=str(root)) as tmp:
        yield Path(tmp)


class SelectionAndGateTests(unittest.TestCase):
    def test_secret_path_gate_blocks_env_test_and_credentials(self) -> None:
        self.assertTrue(is_secret_path(".env"))
        self.assertTrue(is_secret_path(".env.test"))
        self.assertTrue(is_secret_path("config/.env.production"))
        self.assertTrue(is_secret_path("keys/id_ed25519"))
        self.assertTrue(is_secret_path(".ssh/config"))
        self.assertTrue(is_secret_path("certs/server.pem"))
        self.assertFalse(is_secret_path(".env.example"))
        self.assertFalse(is_secret_path(".env.sample"))
        self.assertFalse(is_secret_path(".env.template"))
        self.assertFalse(is_secret_path("keys/id_ed25519.pub"))

    def test_include_still_enforces_hard_exclusions_and_exclude_tests_and_keeps_deletions(self) -> None:
        secret_df = DiffFile(path=".env.local", status="ADDED", additions=2)
        self.assertEqual(classify_exclusion(secret_df, include_globs=[".env*"]), "secret_exclude")

        test_df = DiffFile(path="tests/test_cli.py", status="ADDED")
        self.assertEqual(
            classify_exclusion(test_df, include_globs=["tests/*.py"], exclude_tests=True),
            "test_excluded",
        )
        lock_df = DiffFile(path="package-lock.json", status="MODIFIED")
        self.assertEqual(classify_exclusion(lock_df, include_globs=["*.json"]), "lockfile")

        deleted_src = DiffFile(path="src/legacy_api.py", status="DELETED", deletions=25)
        self.assertEqual(classify_exclusion(deleted_src), "")

    def test_artifact_root_cannot_equal_repo_root(self) -> None:
        with project_temp_dir() as case_dir:
            repo = case_dir / "repo"
            repo.mkdir()
            with self.assertRaises(ValueError):
                resolve_artifact_root(repo, str(repo))

    def test_increment_decrement_patch_stats_and_literal_pathspecs_and_staged_delete_recreate(self) -> None:
        df = DiffFile(path="calc.c")
        _populate_patch_stats(
            df,
            "diff --git a/calc.c b/calc.c\n--- a/calc.c\n+++ b/calc.c\n@@ -1,1 +1,1 @@\n---n;\n+++n;\n",
        )
        self.assertEqual((df.additions, df.deletions), (1, 1))

        with project_temp_dir() as case_dir:
            repo = case_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "diff.noprefix", "true"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            # Empty tree hash computes OID without writing objects (F38)
            oid = empty_tree_hash(repo)
            self.assertTrue(len(oid) >= 40)

            # Path with glob brackets `[v1].py` + staged delete then recreated file `reborn.py` (F03 + F35)
            (repo / "[v1].py").write_text("a = 1\n", encoding="utf-8")
            (repo / "other1.py").write_text("a = 999\n", encoding="utf-8")
            (repo / "reborn.py").write_text("old_line = 1\n", encoding="utf-8")
            subprocess.run(["git", "--literal-pathspecs", "add", "[v1].py", "other1.py", "reborn.py"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            (repo / "[v1].py").write_text("a = 2\n", encoding="utf-8")
            subprocess.run(["git", "rm", "--cached", "reborn.py"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            (repo / "reborn.py").write_text("new_line = 2\n", encoding="utf-8")

            sel = collect_diff_selection(repo)
            by_p = {f.path: f for f in sel.selected_files}
            self.assertIn("[v1].py", by_p)
            self.assertNotIn("999", by_p["[v1].py"].patch)
            self.assertIn("reborn.py", by_p)
            self.assertEqual(by_p["reborn.py"].status, "MODIFIED")
            self.assertIn("+new_line = 2", by_p["reborn.py"].patch)

    def test_cross_scope_rename_from_secret_is_blocked_even_with_paths_filter(self) -> None:
        with project_temp_dir() as case_dir:
            repo = case_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            (repo / ".env.prod").write_text("DB_PASS=topsecret\n", encoding="utf-8")
            subprocess.run(["git", "add", ".env.prod"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "commit", "-m", "c1"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            (repo / "src").mkdir()
            subprocess.run(["git", "mv", ".env.prod", "src/settings.txt"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            sel = collect_diff_selection(repo, target_paths=["src"])
            self.assertEqual(len(sel.selected_files), 0)
            self.assertEqual(sel.excluded_files[0].exclude_reason, "secret_exclude")


class AnchoringTests(unittest.TestCase):
    def test_exact_line_equality_prevents_substring_false_exact_and_preserves_blank_lines(self) -> None:
        lines = [
            "reopen()",
            "open()",
            "",
            "close()",
        ]
        # `open()` must NOT match `reopen()` at line 1 as exact (F15)!
        s, e, status = resolve_snippet_lines(lines, "open()", claimed_start=1, claimed_end=1, changed_ranges=[(1, 4)])
        self.assertEqual((s, e, status), (2, 2, "relocated"))

        # Multi-line snippet with internal blank line matches lines 2..4 contiguously (F15)!
        s2, e2, st2 = resolve_snippet_lines(
            lines,
            "open()\n\nclose()",
            claimed_start=2,
            claimed_end=4,
            changed_ranges=[(1, 4)],
        )
        self.assertEqual((s2, e2, st2), (2, 4, "exact"))

    def test_plan_in_temp_dir_is_never_dropped_and_outside_repo_missing_req_is_rejected(self) -> None:
        with project_temp_dir() as case_dir:
            repo = case_dir / "myproj"
            repo.mkdir()
            plan_in_temp = repo / ".myproj-temp" / "code-review" / "draft-plan.md"
            plan_in_temp.parent.mkdir(parents=True)
            plan_text = "# Plan\n1. Step A\n2. Step B\n"
            plan_in_temp.write_text(plan_text, encoding="utf-8")

            acc_plan, drop_plan = anchor_and_filter_findings(
                raw_findings=[
                    {
                        "id": "F1",
                        "severity": "P1",
                        "category": "architecture_flaw",
                        "file": str(plan_in_temp),
                        "start_line": 3,
                        "end_line": 3,
                        "snippet": "2. Step B",
                        "title": "Plan flaw",
                        "problem": "Flaw details",
                        "fix_direction": "Fix step B",
                    }
                ],
                mode="plan",
                repo_dir=repo,
                plan_text=plan_text,
                plan_label=str(plan_in_temp),
            )
            self.assertEqual(len(drop_plan), 0)
            self.assertEqual(len(acc_plan), 1)
            self.assertEqual(acc_plan[0]["anchor_status"], "exact")

            # Outside-repo path (`../outside.py`) must be rejected even for `missing_requirement` (F32)
            acc_code, drop_code = anchor_and_filter_findings(
                raw_findings=[
                    {
                        "id": "F1",
                        "severity": "P1",
                        "category": "missing_requirement",
                        "file": "../outside.py",
                        "start_line": 0,
                        "end_line": 0,
                        "snippet": "",
                        "title": "Escape",
                        "problem": "Bad path",
                        "fix_direction": "Reject",
                    }
                ],
                mode="code",
                repo_dir=repo,
                selected_files=[],
            )
            self.assertEqual(len(acc_code), 0)
            self.assertEqual(drop_code[0]["drop_reason"], "outside_repository")

    def test_renamed_prior_finding_keeps_id_while_unverified_id_collision_splits_cleanly(self) -> None:
        with project_temp_dir() as case_dir:
            repo = case_dir / "repo"
            repo.mkdir()
            (repo / "new_name.py").write_text("x = 1\n", encoding="utf-8")
            prior = [
                {
                    "id": "F1",
                    "severity": "P1",
                    "category": "bug",
                    "file": "old_name.py",
                    "start_line": 1,
                    "end_line": 1,
                    "snippet": "x = 1",
                    "title": "Original bug",
                    "problem": "Original problem",
                    "fix_direction": "Fix it",
                }
            ]
            # Case A: F1 explicitly audited as persisting on renamed file -> keeps ID F1 (F33)
            acc_a, _ = anchor_and_filter_findings(
                raw_findings=[
                    {
                        "id": "F1",
                        "severity": "P1",
                        "category": "bug",
                        "file": "new_name.py",
                        "start_line": 1,
                        "end_line": 1,
                        "snippet": "x = 1",
                        "title": "Original bug",
                        "problem": "Still broken",
                        "fix_direction": "Fix it",
                    }
                ],
                mode="code",
                repo_dir=repo,
                selected_files=[
                    DiffFile(path="new_name.py", old_path="old_name.py", status="RENAMED", patch="@@ -1 +1 @@\n x = 1\n")
                ],
                prior_findings=prior,
                prior_findings_audit=[{"id": "F1", "status": "persisting", "note": "Still broken"}],
            )
            self.assertEqual(acc_a[0]["id"], "F1")

            # Case B: audit omitted F1, and model reused F1 for a brand-new issue -> gets F2 so F1 is preserved (F01)!
            acc_b, _ = anchor_and_filter_findings(
                raw_findings=[
                    {
                        "id": "F1",
                        "severity": "P2",
                        "category": "bug",
                        "file": "new_name.py",
                        "start_line": 1,
                        "end_line": 1,
                        "snippet": "x = 1",
                        "title": "Brand new bug",
                        "problem": "Different bug",
                        "fix_direction": "Fix new",
                    }
                ],
                mode="code",
                repo_dir=repo,
                selected_files=[
                    DiffFile(path="new_name.py", old_path="old_name.py", status="RENAMED", patch="@@ -1 +1 @@\n x = 1\n")
                ],
                prior_findings=prior,
                prior_findings_audit=[],
            )
            self.assertEqual(acc_b[0]["id"], "F2")


class SessionAndRunnerTests(unittest.TestCase):
    def test_strict_schema_overview_cap_and_contradictory_verdict_guard(self) -> None:
        for mode in ("plan", "code"):
            schema = build_review_schema(mode)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"].keys()))

        # 5,000 excluded files + 1 selected file must keep prompt tiny via grouped counts (F34)
        excluded_many = [
            DiffFile(path=f"node_modules/pkg_{i}/index.js", exclude_reason="noise_directory")
            for i in range(5000)
        ]
        sel = SelectionResult(
            repo_dir=".",
            mode="workspace",
            files=[DiffFile(path="app.py", status="MODIFIED", patch="@@ -1 +1 @@\n+x=1\n"), *excluded_many],
        )
        prompt = build_code_prompt(sel, goal="check", plan_text="", round_no=1, prior_findings=[])
        self.assertLess(len(prompt), 20_000)
        self.assertIn("noise_directory: 5000", prompt)

        # Contradictory verdict='needs_fix' with 0 findings and 0 unresolved audits is rejected (F39)
        with self.assertRaises(RuntimeError):
            validate_codex_response_consistency(
                {"verdict": "needs_fix", "findings": [], "prior_findings_audit": []}
            )

        # Agent normalization & Claude / AGY JSON envelope parsing
        self.assertEqual(normalize_agent_name(""), "codex")
        self.assertEqual(normalize_agent_name("claude-code"), "claude")
        self.assertEqual(normalize_agent_name("agy"), "agy")
        self.assertEqual(normalize_agent_name("antigravity"), "agy")
        parsed_claude = _parse_claude_envelope(
            json.dumps(
                {
                    "is_error": False,
                    "structured_output": {
                        "verdict": "pass",
                        "summary": "OK",
                        "prior_findings_audit": [],
                        "findings": [],
                        "suppressed_over_defensive_notes": [],
                    },
                    "modelUsage": {"claude-opus-4-6": {"inputTokens": 10}},
                }
            )
        )
        self.assertEqual(parsed_claude["verdict"], "pass")
        self.assertEqual(parsed_claude["_runtime_model"], "claude-opus-4-6")

        parsed_agy = _parse_agy_envelope(
            json.dumps(
                {
                    "conversation_id": "conv-123",
                    "status": "SUCCESS",
                    "response": "Done.\n{\"verdict\":\"pass\"}\n",
                    "structured_output": {
                        "verdict": "pass",
                        "summary": "AGY OK",
                        "prior_findings_audit": [],
                        "findings": [],
                        "suppressed_over_defensive_notes": [],
                    },
                }
            ),
            structured_schema=True,
        )
        self.assertEqual(parsed_agy["verdict"], "pass")
        self.assertEqual(parsed_agy["_agent_session_id"], "conv-123")

        freeform_agy = _parse_agy_envelope(
            json.dumps({"conversation_id": "conv-456", "status": "OK", "response": "# Draft Plan\n1. Do X"}),
            structured_schema=False,
        )
        self.assertEqual(freeform_agy["agent_session_id"], "conv-456")
        self.assertIn("Draft Plan", freeform_agy["response"])

    def test_goal_only_empty_diff_runs_codex_and_commit_resume_tracks_cumulative_fixes(self) -> None:
        with project_temp_dir() as case_dir:
            repo = case_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "config", "user.name", "Dev"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            (repo / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "core.py"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)

            (repo / "core.py").write_text("def f():\n    return 1 / 0\n", encoding="utf-8")
            subprocess.run(["git", "add", "core.py"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            subprocess.run(["git", "commit", "-m", "buggy"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
            buggy_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout.strip()

            responses = [
                (
                    {
                        "verdict": "needs_fix",
                        "summary": "除零错误",
                        "prior_findings_audit": [],
                        "findings": [
                            {
                                "id": "F1",
                                "severity": "P1",
                                "category": "bug",
                                "file": "core.py",
                                "start_line": 2,
                                "end_line": 2,
                                "snippet": "return 1 / 0",
                                "title": "除零崩溃",
                                "problem": "调用即崩溃",
                                "fix_direction": "返回有效值",
                            }
                        ],
                        "suppressed_over_defensive_notes": [],
                    },
                    1.0,
                ),
                (
                    {
                        "verdict": "pass",
                        "summary": "累计提交已修复",
                        "prior_findings_audit": [{"id": "F1", "status": "fixed", "note": "已修复"}],
                        "findings": [],
                        "suppressed_over_defensive_notes": [],
                    },
                    1.0,
                ),
            ]

            with mock.patch("code_review.run_codex_structured_review", side_effect=responses) as mocked_run:
                # Verify default max_rounds=1 returns BATCH_FIX_AND_DELIVER and passes empty model/effort overrides!
                buf_single = StringIO()
                with mock.patch("sys.stdout", buf_single):
                    rc_single = code_review.main(["code", "--repo", str(repo), "-c", buggy_sha, "-b", "单轮默认验证"])
                self.assertEqual(rc_single, 0)
                single_out = json.loads(buf_single.getvalue())
                self.assertEqual(single_out["max_rounds"], 1)
                self.assertEqual(single_out["gate_action"], "BATCH_FIX_AND_DELIVER")
                self.assertEqual(mocked_run.call_args_list[0].kwargs["model"], "")
                self.assertEqual(mocked_run.call_args_list[0].kwargs["effort"], "")

            with mock.patch("code_review.run_codex_structured_review", side_effect=responses) as mocked_run:
                buf1 = StringIO()
                with mock.patch("sys.stdout", buf1):
                    rc1 = code_review.main(["code", "--repo", str(repo), "-c", buggy_sha, "-b", "安全计算", "--max-rounds", "3"])
                self.assertEqual(rc1, 0)
                sid = json.loads(buf1.getvalue())["session_id"]

                # Commit a fix AND add a helper file in a repair commit, then pass `-c <repair_sha>` (F3/F09)
                (repo / "core.py").write_text("def f():\n    return 2\n", encoding="utf-8")
                subprocess.run(["git", "add", "core.py"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
                subprocess.run(["git", "commit", "-m", "repair"], cwd=repo, check=True, capture_output=True, creationflags=CREATE_NO_WINDOW)
                repair_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW).stdout.strip()

                buf2 = StringIO()
                with mock.patch("sys.stdout", buf2):
                    rc2 = code_review.main(["code", "--repo", str(repo), "--session", sid, "-c", repair_sha])
                self.assertEqual(rc2, 0)
                prompt2 = mocked_run.call_args_list[1].kwargs["prompt"]
                # Cumulative diff from base -> repair_sha shows `return 1` -> `return 2`, NOT `1 / 0` -> `2`!
                self.assertIn("-    return 1\n+    return 2", prompt2)
                self.assertEqual(json.loads(buf2.getvalue())["gate_action"], "DELIVER_PASS")


if __name__ == "__main__":
    unittest.main()

"""Behavioral regression tests using isolated projects and independent expected rules."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from memory import context, decode_ref, encode_ref, read_refs, recall
from memory_entry import navigation
from memory_store import Store, packed


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harness-memory-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def store(self, sources=()):
        return Store(str(self.root), sources)

    def transcript(self, rows):
        self.write("tmp/session.jsonl", "\n".join(packed(r) for r in [{"project": str(self.root)}, *rows]))
        return "tmp/session.jsonl"

    def test_save_reuse_change_reuse_in_fresh_processes(self):
        self.write("docs/policy.md", "# retry-policy\nstatus: accepted\n\nTransient failures allow 2 attempts.\n")
        def search():
            result = subprocess.run([sys.executable, str(SCRIPTS / "memory.py"), "--project", str(self.root),
                                     "recall", "Transient attempts", "--limit", "1"],
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            return json.loads(result.stdout)
        first = search()["hits"][0]
        self.assertIn("2 attempts", first["excerpt"])
        self.write("docs/policy.md", "# retry-policy\nstatus: accepted\n\nTransient failures allow 4 attempts after confirmed upstream idempotence.\n")
        self.assertTrue(read_refs(self.store(), [first["ref"]])["reads"][0]["changed"])
        self.assertIn("4 attempts", search()["hits"][0]["excerpt"])

    def test_shorter_successor_survives_limit_one_and_newer_noise(self):
        self.write("docs/old.md", "---\nstatus: superseded\nsuperseded_by: docs/new.md\n---\n# retry count\nretry count is two.\n")
        self.write("docs/new.md", "# Current\nstatus: accepted\n\nUse four attempts with confirmed idempotence.\n")
        noise = self.write("docs/noise.md", "# retry count\nA generic topic index.\n")
        os.utime(noise, (2000000000, 2000000000))
        hit = recall(self.store(), "retry count", limit=1)["hits"][0]
        self.assertEqual(hit["path"], "docs/new.md")
        self.assertIn("four attempts", hit["excerpt"])

    def test_section_decision_replacement_and_independent_rule(self):
        self.write("docs/plan.md", "# Plan\n\n## export-old\nstatus: superseded\nsuperseded_by: #export-current\n\nRegenerate export object ID.\n\n## export-current\nstatus: accepted\n\nPreserve original identifiers.\n\n## encoding\nstatus: accepted\n\nUse UTF-8 encoding.\n")
        store = self.store()
        self.assertEqual(recall(store, "export object ID", limit=1)["hits"][0]["section"], "export-current")
        self.assertEqual(recall(store, "UTF-8 encoding", limit=1)["hits"][0]["section"], "encoding")

    def test_body_evidence_beats_title_and_questions(self):
        self.write("docs/title.md", "# callback attempts\nUnrelated index entry.\n")
        self.write("docs/rule.md", "# Rule\nstatus: accepted\n\ncallback attempts are four with idempotence.\n")
        transcript = self.transcript([{"role": "user", "text": "What are callback attempts?"}] * 20)
        hit = recall(self.store(), "callback attempts", limit=1, transcript=transcript)["hits"][0]
        self.assertEqual(hit["path"], "docs/rule.md")

    def test_user_correction_and_rejection_survive(self):
        transcript = self.transcript([
            {"role": "user", "text": "请问 导出 ID 是什么？"},
            {"role": "assistant", "text": "考虑重新编号。"},
            {"role": "user", "text": "纠正：导出 ID 必须保留，重新编号已否决，会破坏引用。"}])
        hit = recall(self.store(), "导出 ID", limit=1, transcript=transcript)["hits"][0]
        self.assertEqual(hit["turn"], 2)
        expanded = read_refs(self.store(), [hit["ref"]], transcript=transcript)["reads"][0]
        self.assertIn("已否决", expanded["text"])

    def test_equal_evidence_prefers_latest_user_correction(self):
        transcript = self.transcript([
            {"role": "user", "text": "导出 ID 重新生成。"},
            {"role": "assistant", "text": "导出 ID 将重新生成。"},
            {"role": "user", "text": "纠正：导出 ID 必须保留。"},
            {"role": "assistant", "text": "导出 ID 规则已收到。"}])
        hit = recall(self.store(), "导出 ID", limit=1, transcript=transcript)["hits"][0]
        self.assertEqual(hit["turn"], 2)
        self.assertIn("必须保留", hit["excerpt"])

    def test_chinese_phrase_with_identifier_recalls_spaced_body(self):
        self.write("docs/format.md", "# 格式\nstatus: accepted\n\n导出时保留对象 ID，并保持引用。\n")
        self.assertEqual(recall(self.store(), "导出对象ID", limit=1)["hits"][0]["path"], "docs/format.md")

    def test_session_fingerprint_includes_neighbor_correction(self):
        rows = [{"role": "assistant", "text": "retry attempts are 2"}, {"role": "user", "text": "确认"}]
        transcript = self.transcript(rows)
        hit = recall(self.store(), "retry attempts", transcript=transcript)["hits"][0]
        rows[1]["text"] = "改为4次"
        self.transcript(rows)
        read = read_refs(self.store(), [hit["ref"]], transcript=transcript)["reads"][0]
        self.assertTrue(read["changed"])
        self.assertEqual(read["text"], "")

    def test_last_short_constraint_survives_long_reply_and_repeated_compaction(self):
        transcript = self.transcript([
            {"role": "assistant", "text": "旧分析" * 20000},
            {"role": "user", "text": "导出保留对象 ID，真实打开后再验收。"},
            {"kind": "compact"}, {"role": "assistant", "text": "正在继续"}, {"kind": "compact"}])
        result = context(self.store(), transcript, budget=1800)
        self.assertIn("真实打开后再验收", packed(result))
        self.assertLessEqual(len(packed(result)), 1800)

    def test_no_compaction_returns_recent_turns(self):
        transcript = self.transcript([{"role": "user", "text": f"阶段 {i}"} for i in range(10)])
        result = context(self.store(), transcript, turns=2)
        self.assertEqual([r["turn"] for r in result["turns"]], [8, 9])

    def test_six_turn_budget_keeps_recent_user_constraint(self):
        rows = [{"role": "assistant", "text": "分析" * 5000} for _ in range(4)]
        rows += [{"role": "user", "text": "最后约束：保存原始对象 ID。"},
                 {"role": "assistant", "text": "后续分析" * 5000}, {"kind": "compact"}]
        transcript = self.transcript(rows)
        result = context(self.store(), transcript, budget=1800)
        self.assertIn("保存原始对象 ID", packed(result))
        self.assertLessEqual(len(packed(result)), 1800)

    def test_read_dialogue_preserves_short_adjacent_qualifier(self):
        transcript = self.transcript([
            {"role": "assistant", "text": "分析" * 5000 + " retry attempts are 4 " + "分析" * 5000},
            {"role": "user", "text": "条件：上游幂等保障确认后执行。"}])
        hit = recall(self.store(), "retry attempts", transcript=transcript)["hits"][0]
        result = read_refs(self.store(), [hit["ref"]], budget=2500, transcript=transcript)
        self.assertIn("retry attempts are 4", result["reads"][0]["text"])
        self.assertIn("上游幂等保障", result["reads"][0]["text"])
        self.assertLessEqual(len(packed(result)), 2500)

    def test_file_and_batch_budgets_include_metadata_and_escaping(self):
        for i in range(8):
            self.write(f"docs/{i}.md", '# rule\nstatus: accepted\n\nneedle ' + '\\"\n数据' * 800)
        store = self.store()
        for budget in (256, 800, 1600, 3000):
            result = recall(store, "needle", budget=budget, limit=8)
            self.assertLessEqual(len(packed(result)), budget)
        refs = [r["ref"] for r in recall(store, "needle", budget=12000, limit=8)["hits"]]
        self.assertLessEqual(len(packed(read_refs(store, refs, budget=2000))), 2000)

    def test_broken_lineages_require_review(self):
        cases = {"missing": "docs/missing.md", "escape": "../other.md", "self": "docs/self.md",
                 "url": "https://example.com/rule.md"}
        for name, target in cases.items():
            self.write(f"docs/{name}.md", f"---\nsuperseded_by: {target}\n---\n# {name}\nunique-{name} rule.\n")
        for name in cases:
            with self.subTest(name=name):
                hit = recall(self.store(), "unique-" + name, limit=1)["hits"][0]
                self.assertEqual(hit["status"], "needs-review")
                self.assertIn("fault", hit)

    def test_cycle_and_hop_limit(self):
        for name, target in (("a", "b"), ("b", "a")):
            self.write(f"docs/{name}.md", f"---\nsuperseded_by: docs/{target}.md\n---\n# rule\ncycle-marker\n")
        self.assertEqual(recall(self.store(), "cycle-marker")["hits"][0]["fault"], "lineage-cycle")
        for i in range(10):
            self.write(f"docs/chain{i}.md", (f"---\nsuperseded_by: docs/chain{i+1}.md\n---\n" if i < 9 else "") + "# rule\n" + ("chain-start" if i == 0 else "replacement"))
        self.assertEqual(recall(self.store(), "chain-start", limit=1)["hits"][0]["fault"], "lineage-depth-limit")

    def test_duplicate_anchor_and_unconfirmed_successor(self):
        self.write("docs/duplicate.md", "# Notes\n## rule {#same-rule}\nneedle one\n## rule {#same-rule}\nneedle two\n")
        self.assertEqual(recall(self.store(), "needle", limit=1)["hits"][0]["status"], "needs-review")
        self.write("docs/old.md", "---\nsuperseded_by: docs/draft.md\n---\n# Rule\nold-marker\n")
        self.write("docs/draft.md", "# Draft\nstatus: proposed\n\nA candidate rule.\n")
        self.assertEqual(recall(self.store(), "old-marker", limit=1)["hits"][0]["fault"], "successor-unconfirmed")

    def test_repeated_headings_preserve_each_body_and_parent_status(self):
        self.write("docs/service.md", "# Service\n## Authentication\nstatus: accepted\n\n"
                   "### Current implementation\ncredential rotation is verified\n"
                   "## Models\nstatus: proposed\n\n"
                   "### Current implementation\nnamed model selection is planned\n")
        store = self.store()
        self.assertNotIn("duplicate-section-id", store.warnings)
        for query, section, status, body in [
            ("credential rotation", "current-implementation", "accepted", "credential rotation is verified"),
            ("named model selection", "current-implementation-1", "proposed", "named model selection is planned"),
        ]:
            hit = recall(store, query, limit=1)["hits"][0]
            self.assertEqual((hit["section"], hit["status"]), (section, status))
            self.assertIn(body, read_refs(store, [hit["ref"]])["reads"][0]["text"])

    def test_repeated_heading_successor_resolves_before_truncation(self):
        self.write("docs/rule.md", "# Rules\n## Current\nstatus: superseded\n"
                   "superseded_by: #current-1\n\nlegacy-export regenerates identifiers\n"
                   "## Current\nstatus: accepted\n\nPreserve object identifiers\n")
        store = self.store()
        hit = recall(store, "legacy-export", limit=1)["hits"][0]
        self.assertEqual((hit["section"], hit["status"]), ("current-1", "accepted"))
        self.assertIn("Preserve object identifiers", read_refs(store, [hit["ref"]])["reads"][0]["text"])

    def test_generated_section_ids_reserve_explicit_ids_and_suffixes(self):
        self.write("docs/rule.md", "# Rules\n## Current\nauto-first\n"
                   "## Current\nauto-second\n## Current-2\nauto-third\n"
                   "## Stable {#current-1}\nexplicit-marker\n")
        store = self.store()
        self.assertFalse(store.ambiguous)
        expected = {"auto-first": "current", "auto-second": "current-2",
                    "auto-third": "current-2-1", "explicit-marker": "current-1"}
        for query, section in expected.items():
            hit = recall(store, query, limit=1)["hits"][0]
            self.assertEqual(hit["section"], section)
            self.assertIn(query, read_refs(store, [hit["ref"]])["reads"][0]["text"])

    def test_nested_and_fenced_metadata_are_data(self):
        self.write("docs/rule.md", "---\nexample:\n  status: superseded\n  superseded_by: docs/absent.md\n---\n# Rule\nneedle is valid\n```markdown\n## evil\nstatus: superseded\n```\n")
        store = self.store()
        self.assertNotIn("docs/rule.md#evil", store.nodes)
        self.assertEqual(recall(store, "needle")["hits"][0]["status"], "unmarked")

    def test_refs_are_scoped_and_invalid_refs_are_reported(self):
        self.write("docs/rule.md", "# Rule\nneedle value\n")
        store = self.store()
        ref = recall(store, "needle")["hits"][0]["ref"]
        other = self.root / "other"
        other.mkdir()
        self.assertIn("ref-project-mismatch", read_refs(Store(str(other)), [ref])["warnings"])
        self.assertIn("ref-invalid", read_refs(store, ["hr1:!@$", "fake"])["warnings"])
        forged = decode_ref(ref, store.project_id)
        forged["path"] = "../other.md"
        self.assertIn("ref-missing", read_refs(store, [encode_ref(forged)])["warnings"])

    def test_explicit_sources_and_gitignore(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "docs/private.md\n")
        self.write("docs/private.md", "# Private\nprivate-needle\n")
        self.write("notes/custom.md", "# Custom\ncustom-needle\n")
        self.assertEqual(recall(self.store(), "private-needle")["hits"], [])
        self.assertEqual(recall(self.store(), "custom-needle")["hits"], [])
        self.assertEqual(recall(self.store(["notes"]), "custom-needle")["hits"][0]["path"], "notes/custom.md")

    def test_scan_limits_report_partial_results(self):
        for i in range(6):
            self.write(f"docs/{i}.md", "# Rule\nneedle\n")
        with patch("memory_store.MAX_FILES", 2):
            result = recall(self.store(), "needle")
        self.assertIn("scan-truncated", result["warnings"])

    def test_explicit_sources_survive_file_and_traversal_limits(self):
        for i in range(12):
            self.write(f"docs/histories/{i}.md", "# History\nold baseline\n")
        self.write("docs/z-current/rule.md", "# Current\nlatest-authoritative-marker\n")
        for source in ("docs/z-current/rule.md", "docs/z-current"):
            with self.subTest(source=source), patch("memory_store.MAX_FILES", 1), patch("memory_store.MAX_ENTRIES", 6):
                result = recall(self.store([source]), "latest-authoritative-marker")
                self.assertEqual(result["hits"][0]["path"], "docs/z-current/rule.md")
                self.assertIn("scan-truncated", result["warnings"])

    def test_raw_archives_are_opt_in_and_do_not_starve_current_documents(self):
        self.write("docs/ARCHITECTURE.md", "# Export\nPreserve original identifiers.\n")
        for i in range(20):
            self.write(f"docs/archive/legacy/{i}.md", "# Old\nexport object ID: regenerate.\n")
        for sources in ((), ["docs"], ["."]):
            with self.subTest(sources=sources), patch("memory_store.MAX_FILES", 1), patch("memory_store.MAX_ENTRIES", 5):
                store = self.store(sources)
                result = recall(store, "export object ID", limit=1)
                self.assertEqual(result["hits"][0]["path"], "docs/ARCHITECTURE.md")
                self.assertNotIn("scan-truncated", result["warnings"])
                self.assertEqual(list(store.documents), ["docs/ARCHITECTURE.md"])

    def test_explicit_archive_read_preserves_provenance_status_and_fingerprint(self):
        rel = "docs/archive/legacy/old.md"
        self.write(rel, "# Old\nstatus: accepted\n\narchival-marker decision\n")
        self.write("docs/archive/other/hidden.md", "# Other\narchival-marker noise\n")
        for source in (rel, "docs/archive/legacy"):
            store = self.store([source])
            hits = recall(store, "archival-marker")["hits"]
            self.assertEqual(len(hits), 1)
            hit = hits[0]
            self.assertEqual(hit["provenance"], "archive")
            self.assertEqual(hit["status"], "accepted")
            expanded = read_refs(store, [hit["ref"]])["reads"][0]
            self.assertEqual(expanded["provenance"], "archive")
            self.assertIn("archival-marker decision", expanded["text"])
            self.assertEqual(read_refs(self.store(), [hit["ref"]])["reads"], [])
        self.write(rel, "# Old\nChanged historical material\n")
        changed = read_refs(self.store([rel]), [hit["ref"]])["reads"][0]
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["text"], "")

    def test_archive_case_variants_and_normal_history(self):
        for rel in ("Docs/Archive/a.md", "doc/ARCHIVE/b.md", "DOCS/archive/c.md"):
            self.write(rel, "# Old\narchive-case-marker\n")
            self.assertEqual(recall(self.store(), "archive-case-marker")["hits"], [])
            self.assertEqual(recall(self.store([rel]), "archive-case-marker")["hits"][0]["provenance"], "archive")
        for rel in ("docs/exec-plans/completed/done.md", "docs/histories/done.md", "docs/design/archive-format.md"):
            self.write(rel, "# Retained\nnormal-history-marker\n")
        hits = recall(self.store(), "normal-history-marker", limit=10, budget=8000)["hits"]
        self.assertEqual(len(hits), 3)
        self.assertTrue(all("provenance" not in hit for hit in hits))

    def test_archival_lineage_promotes_current_but_never_an_archived_successor(self):
        old = "docs/archive/legacy/old.md"
        self.write(old, "---\nstatus: superseded\nsuperseded_by: docs/design/current.md\n---\n# Old\nexport object ID\n")
        self.write("docs/design/current.md", "# Current\nstatus: accepted\n\nPreserve identifiers.\n")
        hit = recall(self.store([old]), "export object ID", limit=1)["hits"][0]
        self.assertEqual(hit["path"], "docs/design/current.md")
        self.assertNotIn("provenance", hit)
        self.write("docs/bad.md", f"---\nsuperseded_by: {old}\n---\n# Current\ninvalid-successor-marker\n")
        for sources in ((), ["docs/archive"]):
            hit = recall(self.store(sources), "invalid-successor-marker", limit=1)["hits"][0]
            self.assertEqual(hit["status"], "needs-review")
            self.assertEqual(hit["fault"], "archived-successor")

    def test_archival_provenance_counts_towards_json_budget(self):
        self.write("docs/archive/old.md", '# Old\nneedle ' + '\\"\n数据' * 1000)
        store = self.store(["docs/archive"])
        for budget in (256, 800, 1600, 3000):
            result = recall(store, "needle", budget=budget)
            self.assertLessEqual(len(packed(result)), budget)
            for hit in result["hits"]:
                self.assertEqual(hit["provenance"], "archive")

    def test_archive_alias_keeps_opt_in_and_provenance(self):
        self.write("docs/archive/legacy/old.md", "# Old\narchive-alias-marker\n")
        link, target = self.root / "docs/shortcut", self.root / "docs/archive/legacy"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, check=True)
        else:
            link.symlink_to(target, target_is_directory=True)
        self.assertEqual(recall(self.store(), "archive-alias-marker")["hits"], [])
        hit = recall(self.store(["docs/shortcut"]), "archive-alias-marker")["hits"][0]
        self.assertEqual(hit["path"], "docs/archive/legacy/old.md")
        self.assertEqual(hit["provenance"], "archive")

    def test_existing_capitalized_docs_directory_is_discovered(self):
        self.write("Docs/policy.md", "# Current\ncapitalized-docs-marker\n")
        self.assertEqual(recall(self.store(), "capitalized-docs-marker")["hits"][0]["path"], "Docs/policy.md")

    def test_ignored_artifacts_do_not_use_document_budget(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.write(".gitignore", "docs/generated-*.md\n")
        for i in range(5):
            self.write(f"docs/generated-{i}.md", "# Generated\nnoise\n")
        self.write("docs/z-current.md", "# Current\nlatest-real-constraint\n")
        with patch("memory_store.MAX_FILES", 2):
            result = recall(self.store(), "latest-real-constraint")
        self.assertEqual(result["hits"][0]["path"], "docs/z-current.md")
        self.assertLessEqual(len(result["hits"]), 2)

    def test_symbolic_link_escape_stays_outside_sources(self):
        outside = tempfile.TemporaryDirectory(prefix="harness-outside-")
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "secret.md"
        target.write_text("# Secret\noutside-needle\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        try:
            (self.root / "docs/link.md").symlink_to(target)
        except OSError:
            self.skipTest("symlink creation requires host privileges")
        result = recall(self.store(), "outside-needle")
        self.assertEqual(result["hits"], [])
        self.assertIn("source-outside-project", result["warnings"])

    def test_malformed_frontmatter_requires_review(self):
        self.write("docs/bad.md", "---\nstatus: accepted\n# Rule\nneedle\n")
        self.assertEqual(recall(self.store(), "needle")["hits"][0]["status"], "needs-review")

    @unittest.skipUnless(os.name == "nt", "Windows junction traversal")
    def test_directory_junction_escape_stays_outside_sources(self):
        outside = tempfile.TemporaryDirectory(prefix="harness-outside-junction-")
        self.addCleanup(outside.cleanup)
        target = Path(outside.name)
        (target / "secret.md").write_text("# Secret\noutside-junction-needle\n", encoding="utf-8")
        link = self.root / "docs/linked"
        link.parent.mkdir()
        run = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              "New-Item -ItemType Junction -Path $env:HR_LINK -Target $env:HR_TARGET | Out-Null"],
                             env={**os.environ, "HR_LINK": str(link), "HR_TARGET": str(target)},
                             capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.addCleanup(link.rmdir)
        result = recall(self.store(), "outside-junction-needle")
        self.assertEqual(result["hits"], [])
        self.assertIn("source-outside-project", result["warnings"])
        self.assertTrue((target / "secret.md").is_file())

    def test_empty_and_overlong_replacement_metadata_require_review(self):
        for target in ("", "x" * 600):
            with self.subTest(length=len(target)):
                self.write("docs/bad.md", "# Rule\nstatus: accepted\nsuperseded_by: " + target + "\n\nneedle\n")
                self.assertEqual(recall(self.store(), "needle")["hits"][0]["status"], "needs-review")

    def test_transcript_project_and_format_checked(self):
        self.write("tmp/bad.jsonl", packed({"project": str(self.root / "other")}) + '\n{"role":"user","text":"secret"}')
        result = recall(self.store(), "secret", transcript="tmp/bad.jsonl")
        self.assertEqual(result["hits"], [])
        self.assertIn("transcript-project-mismatch", result["warnings"])

    def test_navigation_dedup_restore_scope_change_and_metadata_only(self):
        cache = self.root / "cache"
        plan = self.write("docs/current.md", "# Plan\n最晚用户修正：保留对象 ID。\n")
        def entry(session="one", event="startup", host="test"):
            return navigation(str(self.root), host, session, event, "docs/current.md", str(cache))
        self.assertEqual(entry()["status"], "injected")
        self.assertEqual(entry()["status"], "deduped")
        for event in ("compact", "clear", "resume"):
            self.assertEqual(entry(event=event)["status"], "injected")
        self.assertEqual(entry()["status"], "injected")
        self.assertEqual(entry()["status"], "deduped")
        self.assertEqual(entry("two")["status"], "injected")
        self.assertEqual(entry(host="second-host")["status"], "injected")
        plan.write_text("# Plan\n新约束：兼容旧格式。\n", encoding="utf-8")
        self.assertEqual(entry()["status"], "injected")
        for path in cache.iterdir():
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"event", "hash", "status", "chars"})

    def test_missing_identity_corrupt_cache_and_write_failure_deliver_entry(self):
        cache = self.root / "cache"
        for _ in range(2):
            self.assertEqual(navigation(str(self.root), session=None, state_dir=cache)["status"], "injected")
        self.assertFalse(cache.exists())
        navigation(str(self.root), session="one", state_dir=cache)
        next(cache.iterdir()).write_text("broken", encoding="utf-8")
        result = navigation(str(self.root), session="one", state_dir=cache)
        self.assertEqual(result["status"], "injected")
        self.assertIn("state-unreadable", result["warnings"])
        blocked = self.write("file-cache", "file")
        result = navigation(str(self.root), session="one", state_dir=blocked)
        self.assertEqual(result["status"], "injected")
        self.assertIn("state-write-failed", result["warnings"])

    def test_session_start_contract_and_recovery(self):
        command = [sys.executable, str(SCRIPTS / "session_start.py"), "--host", "test",
                   "--state-dir", str(self.root / "cache")]
        data = {"cwd": str(self.root), "session_id": "session", "source": "startup"}
        def fire():
            run = subprocess.run(command, input=packed(data), capture_output=True, text=True,
                                 encoding="utf-8", check=True)
            return json.loads(run.stdout)
        first = fire()
        self.assertEqual(first["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(fire(), {})
        data["source"] = "compact"
        self.assertIn("最新用户约束", fire()["hookSpecificOutput"]["additionalContext"])

    def test_archived_plan_still_returns_navigation(self):
        result = navigation(str(self.root), session="one", plan="docs/old-plan.md", state_dir=self.root / "cache")
        self.assertEqual(result["status"], "injected")
        self.assertIn("plan-unavailable", result["warnings"])
        self.assertIn("AGENTS/README", result["context"])

    def test_nested_old_decision_redirects_to_current_subtree(self):
        self.write("docs/decisions.md", "# Decisions\n\n## Old export\nstatus: superseded\nsuperseded_by: #current\n\nOld method.\n### ID details\nstatus: accepted\n\nRegenerate exported object ID.\n\n## Current {#current}\nstatus: accepted\n\n### Identifiers\nPreserve original identifiers.\n")
        store = self.store()
        hit = recall(store, "Regenerate exported object ID", limit=1)["hits"][0]
        self.assertEqual(hit["section"], "current")
        expanded = read_refs(store, [hit["ref"]])["reads"][0]
        self.assertIn("Preserve original identifiers", expanded["text"])

    def test_rejected_or_invalid_ancestor_applies_to_descendants(self):
        for state in ("rejected", "superseded"):
            with self.subTest(state=state):
                self.write("docs/decision.md", f"# Decision\nstatus: {state}\n\n## Details\nstatus: accepted\n\nunsafe-marker rule\n")
                hit = recall(self.store(), "unsafe-marker", limit=1)["hits"][0]
                self.assertEqual(hit["status"], "rejected" if state == "rejected" else "needs-review")

    def test_parent_excerpt_keeps_child_decision_states_separate(self):
        self.write("docs/policy.md", "# Policy\n\n## Export\nstatus: accepted\n\nExport policy overview.\n### Old\nstatus: rejected\n\nRegenerate all IDs.\n### Current\nstatus: accepted\n\nKeep original IDs.\n")
        hit = recall(self.store(), "Export", limit=1)["hits"][0]
        self.assertIn("overview", hit["excerpt"])
        self.assertNotIn("Regenerate", hit["excerpt"])

    def test_preamble_constraint_is_searchable_and_expandable(self):
        self.write("AGENTS.md", "User constraint: secret-marker must be preserved.\n\n# Project\nIntro\n## Commands\nBuild instructions\n")
        store = self.store()
        hit = recall(store, "secret-marker", limit=1)["hits"][0]
        self.assertIn("must be preserved", hit["excerpt"])
        self.assertIn("must be preserved", read_refs(store, [hit["ref"]])["reads"][0]["text"])

    def test_latest_correction_after_compact_is_retained(self):
        transcript = self.transcript([
            {"role": "user", "text": "Old rule: regenerate IDs."},
            {"kind": "compact"},
            {"role": "user", "text": "Latest correction: preserve original IDs."}])
        result = context(self.store(), transcript)
        self.assertIn("Latest correction", packed(result))

    def test_last_user_constraint_survives_many_assistant_turns(self):
        transcript = self.transcript([
            {"role": "user", "text": "背景分析" * 1500 + "最后约束：保留原始对象 ID。"},
            *[{"role": "assistant", "text": f"阶段 {i}"} for i in range(10)]])
        result = context(self.store(), transcript, budget=1800)
        self.assertIn("最后约束：保留原始对象 ID", packed(result))
        ref = next(row["ref"] for row in result["turns"] if row["role"] == "user")
        self.assertIn("最后约束：保留原始对象 ID", packed(read_refs(self.store(), [ref], transcript=transcript)))
        self.assertEqual(result["turns"][-1]["turn"], 10)
        self.assertLessEqual(len(packed(result)), 1800)


if __name__ == "__main__":
    unittest.main()

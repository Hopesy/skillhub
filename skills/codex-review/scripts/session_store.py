"""5-round review session state machine and artifact store under .<project>-temp/codex-review/."""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

DEFAULT_MAX_ROUNDS = 1
ABSOLUTE_MAX_ROUNDS = 5
DEFAULT_AGENT_MODEL = "codex-default"
DEFAULT_AGENT_EFFORT = "default"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def sanitize_project_slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return cleaned.lower() or "workspace"


def default_temp_root(base_dir: Path) -> Path:
    """Follow user convention: store intermediate artifacts under `.<project>-temp/codex-review`."""
    resolved = base_dir.resolve()
    slug = sanitize_project_slug(resolved.name)
    return resolved / f".{slug}-temp" / "codex-review"


def resolve_artifact_root(base_dir: Path, artifact_root: str = "") -> Path:
    base_res = base_dir.resolve()
    if not artifact_root:
        return default_temp_root(base_res)
    root_res = Path(artifact_root).resolve()
    if root_res == base_res:
        raise ValueError(
            f"artifact_dir ({root_res}) must be a dedicated subdirectory or outside the workspace, not the workspace root itself"
        )
    return root_res


def generate_session_id(mode: str) -> str:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{mode}-{ts}-{suffix}"


def validate_session_id(session_id: str) -> str:
    sid = session_id.strip()
    if not sid or ".." in sid or not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return sid


def validate_max_rounds(max_rounds: int) -> int:
    if max_rounds < 1 or max_rounds > ABSOLUTE_MAX_ROUNDS:
        raise ValueError(f"max_rounds must be between 1 and {ABSOLUTE_MAX_ROUNDS}, got {max_rounds}")
    return max_rounds


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == STILL_ACTIVE
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class SessionStore:
    def __init__(self, root_dir: Path, session_id: str):
        if root_dir.is_symlink():
            raise ValueError(f"Refusing symlinked artifact root: {root_dir}")
        self.root_dir = root_dir.resolve()
        self.session_id = validate_session_id(session_id)
        candidate_dir = self.root_dir / self.session_id
        if candidate_dir.is_symlink():
            raise ValueError(f"Refusing symlinked session directory: {candidate_dir}")
        self.session_dir = candidate_dir.resolve()
        if self.session_dir.parent != self.root_dir:
            raise ValueError(f"Session directory escapes artifact root: {session_id!r}")
        self.manifest_path = self.session_dir / "session.json"
        self.lock_path = self.session_dir / ".session.lock"

    def _verify_safe_regular_target(self, target: Path, must_exist: bool = False) -> None:
        if self.session_dir.is_symlink() or target.is_symlink():
            raise ValueError(f"Refusing to access symlinked session file: {target}")
        if target.resolve().parent != self.session_dir:
            raise ValueError(f"Session file escapes session directory: {target}")
        if must_exist:
            try:
                st = os.lstat(target)
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"Session manifest not found: {target}") from exc
            if not stat.S_ISREG(st.st_mode):
                raise ValueError(f"Session manifest is not a regular file: {target}")

    @classmethod
    def create_new(
        cls,
        root_dir: Path,
        mode: str,
        agent: str = "codex",
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        model: str = DEFAULT_AGENT_MODEL,
        effort: str = DEFAULT_AGENT_EFFORT,
        goal: str = "",
        plan_file: str = "",
        scope: dict | None = None,
    ) -> "SessionStore":
        capped_rounds = validate_max_rounds(max_rounds)
        for _ in range(5):
            sid = generate_session_id(mode)
            store = cls(root_dir, sid)
            try:
                store.session_dir.mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("Failed to allocate a unique session directory.")

        initial = {
            "schema": "codex-review/v2",
            "session_id": store.session_id,
            "mode": mode,
            "agent": agent,
            "model": model,
            "effort": effort,
            "max_rounds": capped_rounds,
            "current_round": 0,
            "status": "in_progress",  # in_progress | passed | max_rounds_reached
            "goal": goal,
            "plan_file": plan_file,
            "scope": scope if scope is not None else {},
            "active_claim": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "rounds": [],
        }
        store._write_json(store.manifest_path, initial)
        return store

    @classmethod
    def load_existing(
        cls,
        root_dir: Path,
        session_id: str,
        expected_mode: str | None = None,
    ) -> "SessionStore":
        store = cls(root_dir, session_id)
        data = store.load()
        if expected_mode and data.get("mode") != expected_mode:
            raise ValueError(
                f"Session {store.session_id!r} has mode={data.get('mode')!r}, cannot run mode={expected_mode!r}"
            )
        return store

    @classmethod
    def create_or_load(
        cls,
        base_dir: Path,
        mode: str,
        session_id: str = "",
        artifact_root: str = "",
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        model: str = DEFAULT_AGENT_MODEL,
        effort: str = DEFAULT_AGENT_EFFORT,
        goal: str = "",
        plan_file: str = "",
        scope: dict | None = None,
    ) -> "SessionStore":
        root = resolve_artifact_root(base_dir, artifact_root)
        if session_id.strip():
            return cls.load_existing(root, session_id, expected_mode=mode)
        return cls.create_new(
            root_dir=root,
            mode=mode,
            max_rounds=max_rounds,
            model=model,
            effort=effort,
            goal=goal,
            plan_file=plan_file,
            scope=scope,
        )

    @contextlib.contextmanager
    def _locked(self):
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._verify_safe_regular_target(self.lock_path, must_exist=False)
        with self.lock_path.open("a+b") as fh:
            if sys.platform == "win32":
                fh.seek(0)
                if fh.tell() == 0 and self.lock_path.stat().st_size == 0:
                    fh.write(b"0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict:
        self._verify_safe_regular_target(self.manifest_path, must_exist=True)
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def prepare_next_round(
        self,
        goal_override: str | None = None,
        plan_file_override: str | None = None,
        scope_updates: dict | None = None,
    ) -> tuple[int, list[dict], str, dict]:
        """Atomically validate state, claim the next round, and apply any explicit metadata updates (F4/F12)."""
        with self._locked():
            data = self.load()
            current = int(data.get("current_round") or 0)
            max_rounds = min(
                ABSOLUTE_MAX_ROUNDS,
                max(1, int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)),
            )
            if data.get("status") == "passed":
                raise RuntimeError(
                    f"Session {self.session_id} already passed with 0 issues at round {current}."
                )
            if current >= max_rounds:
                raise RuntimeError(
                    f"Session {self.session_id} already reached max_rounds={max_rounds}; deliver final summary instead of starting round {current + 1}."
                )

            claim = data.get("active_claim")
            if isinstance(claim, dict):
                claim_pid = int(claim.get("pid") or 0)
                if claim_pid and claim_pid != os.getpid() and _pid_is_alive(claim_pid):
                    raise RuntimeError(
                        f"Session {self.session_id} already has round {claim.get('round')} running in PID {claim_pid}."
                    )

            if goal_override:
                data["goal"] = goal_override
            if plan_file_override:
                data["plan_file"] = plan_file_override
            if scope_updates:
                merged_scope = dict(data.get("scope") or {})
                merged_scope.update(scope_updates)
                data["scope"] = merged_scope

            next_round = current + 1
            token = uuid.uuid4().hex
            data["active_claim"] = {
                "round": next_round,
                "token": token,
                "pid": os.getpid(),
                "claimed_at": _now_iso(),
            }
            data["max_rounds"] = max_rounds
            data["updated_at"] = _now_iso()
            self._write_json(self.manifest_path, data)

            prior_findings: list[dict] = []
            rounds = data.get("rounds") or []
            if rounds:
                prior_findings = list(rounds[-1].get("findings") or [])
            return next_round, prior_findings, token, dict(data)

    def release_claim(self, claim_token: str) -> None:
        if not claim_token or not self.manifest_path.exists():
            return
        with self._locked():
            data = self.load()
            claim = data.get("active_claim")
            if isinstance(claim, dict) and claim.get("token") == claim_token:
                data["active_claim"] = None
                data["updated_at"] = _now_iso()
                self._write_json(self.manifest_path, data)

    def record_round(
        self,
        round_no: int,
        fingerprint: str,
        summary: str,
        findings: list[dict],
        prior_findings_audit: list[dict],
        suppressed_notes: list[str],
        dropped_findings: list[dict],
        elapsed_sec: float,
        prior_findings: list[dict] | None = None,
        claim_token: str = "",
        executed_agent: str = "codex",
        executed_model: str = DEFAULT_AGENT_MODEL,
        executed_effort: str = DEFAULT_AGENT_EFFORT,
    ) -> dict:
        with self._locked():
            data = self.load()
            current = int(data.get("current_round") or 0)
            max_rounds = min(
                ABSOLUTE_MAX_ROUNDS,
                max(1, int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)),
            )
            if data.get("status") == "passed" or current != round_no - 1:
                raise RuntimeError(
                    f"Concurrent round state conflict on session {self.session_id}: expected current_round={round_no - 1}, got {current} (status={data.get('status')})."
                )
            claim = data.get("active_claim")
            if claim_token and (not isinstance(claim, dict) or claim.get("token") != claim_token):
                raise RuntimeError(
                    f"Round claim lost on session {self.session_id} for round {round_no}."
                )

            normalized_audits: list[dict] = []
            audit_by_id: dict[str, dict] = {}
            for item in prior_findings_audit or []:
                aid = str(item.get("id") or "").strip()
                if aid:
                    audit_by_id[aid] = item
                    normalized_audits.append(item)

            existing_ids = {str(f.get("id") or "").strip() for f in findings}
            merged_findings = list(findings)

            for orig in prior_findings or []:
                oid = str(orig.get("id") or "").strip()
                if not oid:
                    continue
                audit = audit_by_id.get(oid)
                if audit and audit.get("status") == "fixed":
                    continue

                if audit is None:
                    note = "本轮未返回该项复核结论，默认保留为待核验未关闭状态"
                    normalized_audits.append(
                        {"id": oid, "status": "persisting", "note": note}
                    )
                else:
                    note = str(audit.get("note") or "").strip() or "上轮问题尚未彻底修复"

                if oid not in existing_ids:
                    carried = dict(orig)
                    prev_prob = str(orig.get("problem") or "")
                    prefix_tag = f"[上轮未关闭: {note}]"
                    carried["problem"] = (
                        prev_prob if prev_prob.startswith("[上轮未关闭:") else f"{prefix_tag} {prev_prob}".strip()
                    )
                    merged_findings.append(carried)
                    existing_ids.add(oid)

            issue_count = len(merged_findings)
            if issue_count == 0:
                gate_action = "DELIVER_PASS"
                session_status = "passed"
            elif round_no >= max_rounds:
                gate_action = "BATCH_FIX_AND_DELIVER" if max_rounds == 1 else "DELIVER_AT_MAX_ROUNDS"
                session_status = "max_rounds_reached"
            else:
                gate_action = "BATCH_FIX_AND_RETRY"
                session_status = "in_progress"

            round_record = {
                "round": round_no,
                "max_rounds": max_rounds,
                "agent": executed_agent,
                "model": executed_model,
                "effort": executed_effort,
                "timestamp": _now_iso(),
                "elapsed_sec": round(elapsed_sec, 2),
                "fingerprint": fingerprint,
                "issue_count": issue_count,
                "gate_action": gate_action,
                "summary": summary,
                "prior_findings_audit": normalized_audits,
                "findings": merged_findings,
                "suppressed_over_defensive_notes": suppressed_notes,
                "dropped_findings": dropped_findings,
            }

            next_data = dict(data)
            next_data["agent"] = executed_agent
            next_data["model"] = executed_model
            next_data["effort"] = executed_effort
            next_data["current_round"] = round_no
            next_data["max_rounds"] = max_rounds
            next_data["status"] = session_status
            next_data["active_claim"] = None
            next_data["updated_at"] = _now_iso()
            next_data["rounds"] = [*(data.get("rounds") or []), round_record]

            # Write round artifacts AND terminal final-summary.md atomically BEFORE committing
            # `session.json` so an I/O failure never leaves an un-retryable terminal manifest (F37).
            round_json_path = self.session_dir / f"round-{round_no:02d}.json"
            round_md_path = self.session_dir / f"round-{round_no:02d}.md"
            final_summary_path = self.session_dir / "final-summary.md"

            self._write_json(round_json_path, round_record)
            self._write_text(round_md_path, self._render_round_markdown(next_data, round_record))
            if gate_action in {"DELIVER_PASS", "BATCH_FIX_AND_DELIVER", "DELIVER_AT_MAX_ROUNDS"}:
                self._write_text(final_summary_path, self.render_final_summary(next_data))

            self._write_json(self.manifest_path, next_data)

            out = dict(round_record)
            out["session_id"] = self.session_id
            out["session_status"] = session_status
            out["session_dir"] = str(self.session_dir)
            out["round_markdown_path"] = str(round_md_path)
            if gate_action in {"DELIVER_PASS", "BATCH_FIX_AND_DELIVER", "DELIVER_AT_MAX_ROUNDS"}:
                out["final_summary_path"] = str(final_summary_path)
            return out

    def _render_round_markdown(self, manifest: dict, rec: dict) -> str:
        round_agent = rec.get("agent") or manifest.get("agent") or "codex"
        round_model = rec.get("model") or manifest.get("model") or DEFAULT_AGENT_MODEL
        round_effort = rec.get("effort") or manifest.get("effort") or DEFAULT_AGENT_EFFORT
        lines = [
            f"# 审查报告 — 第 {rec['round']}/{rec['max_rounds']} 轮",
            "",
            f"- **会话 ID**：`{self.session_id}`",
            f"- **审查模式**：`{manifest.get('mode')}`（审查后端：`{round_agent}`，模型：`{round_model}` / `{round_effort}`）",
            f"- **本轮检出问题数**：**{rec['issue_count']}**",
            f"- **门禁动作**：`{rec['gate_action']}`",
            f"- **耗时**：{rec['elapsed_sec']}s",
            "",
            "## 本轮结论",
            "",
            rec.get("summary") or "（无摘要）",
            "",
        ]

        audits = rec.get("prior_findings_audit") or []
        if audits:
            lines.extend(["## 上轮问题复核", ""])
            for a in audits:
                lines.append(f"- `{a['id']}` → **{a['status']}**：{a['note']}")
            lines.append("")

        findings = rec.get("findings") or []
        if findings:
            lines.extend(["## 待一次性修复问题清单", ""])
            for f in findings:
                loc = f["file"] or "(全局)"
                if f.get("start_line", 0) > 0:
                    loc += (
                        f":{f['start_line']}-{f['end_line']}"
                        if f.get("end_line", 0) > f["start_line"]
                        else f":{f['start_line']}"
                    )
                lines.extend(
                    [
                        f"### [{f['severity']}] {f['id']} — {f['title']}",
                        f"- **位置**：`{loc}`（类别：`{f['category']}`）",
                        f"- **问题与触发路径**：{f['problem']}",
                        f"- **最小修复方向**：{f['fix_direction']}",
                        "",
                    ]
                )
        else:
            lines.extend(["## 待修复问题", "", "0 问题（审查通过）。", ""])

        suppressed = rec.get("suppressed_over_defensive_notes") or []
        if suppressed:
            lines.extend(["## 已拦截的过度防御/低价值噪音（无需修改）", ""])
            for note in suppressed:
                lines.append(f"- {note}")
            lines.append("")

        return "\n".join(lines)

    def render_final_summary(self, manifest: dict | None = None) -> str:
        data = manifest or self.load()
        status = str(data.get("status") or "in_progress")
        rounds = data.get("rounds") or []
        total_fixed = sum(
            1
            for r in rounds
            for a in (r.get("prior_findings_audit") or [])
            if a.get("status") == "fixed"
        )
        last = rounds[-1] if rounds else {}
        effective_agent = last.get("agent") or data.get("agent") or "codex"
        effective_model = last.get("model") or data.get("model") or DEFAULT_AGENT_MODEL
        effective_effort = last.get("effort") or data.get("effort") or DEFAULT_AGENT_EFFORT
        lines = [
            f"# 闭环审查总结（`{self.session_id}`）",
            "",
            f"- **审查类型**：`{data.get('mode')}`",
            f"- **审查后端与模型**：`{effective_agent}` / `{effective_model}`（推理强度：`{effective_effort}`，只读沙箱）",
            f"- **收敛状态**：`{status}`（已执行 {len(rounds)}/{data.get('max_rounds')} 轮，累计验证修复 {total_fixed} 项）",
            "",
        ]

        if rounds:
            lines.extend(
                [
                    "## 轮次收敛轨迹",
                    "",
                    "| 轮次 | 检出问题数 | 门禁结论 | 耗时 | 摘要 |",
                    "| :---: | :---: | :--- | :---: | :--- |",
                ]
            )
            for r in rounds:
                short_sum = str(r.get("summary") or "").replace("\n", " ").replace("|", "\\|")
                lines.append(
                    f"| 第 {r['round']} 轮 | {r['issue_count']} | `{r['gate_action']}` | {r['elapsed_sec']}s | {short_sum} |"
                )
            lines.append("")

        remaining = last.get("findings") or []
        if status == "passed" and rounds:
            lines.extend(["## 最终验收结论", "", "全部审查问题已清零（0 问题），未引入过度防御代码。", ""])
        elif status == "max_rounds_reached" and int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS) == 1:
            lines.extend(["## 单轮审查检出清单（由宿主一次性全部修复后交付）", ""])
            for f in remaining:
                lines.append(f"- **[{f['severity']}] {f['id']} {f['title']}** (`{f['file']}`)：{f['problem']}")
            lines.append("")
        elif status == "max_rounds_reached":
            lines.extend(["## 达到最大轮次时的遗留风险（需人工确认）", ""])
            for f in remaining:
                lines.append(f"- **[{f['severity']}] {f['id']} {f['title']}** (`{f['file']}`)：{f['problem']}")
            lines.append("")
        else:
            lines.extend(["## 当前会话进展（尚未收敛完成）", ""])
            if not rounds:
                lines.append("尚未完成第 1 轮审查。")
            else:
                lines.append(f"当前停留在第 {last['round']} 轮，尚有 **{len(remaining)}** 项问题待一次性修复并复审：")
                for f in remaining:
                    lines.append(f"- **[{f['severity']}] {f['id']} {f['title']}** (`{f['file']}`)：{f['problem']}")
            lines.append("")

        return "\n".join(lines)

    def _write_text(self, path: Path, content: str) -> None:
        self._verify_safe_regular_target(path, must_exist=False)
        unique = f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        tmp = path.with_name(path.name + unique)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def _write_json(self, path: Path, payload: dict) -> None:
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

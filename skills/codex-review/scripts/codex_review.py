#!/usr/bin/env python3
"""Unified CLI for `codex-review`: cross-agent plan discussion, review (`plan`/`code`), and TODO execution (`exec`) across local `codex`, `claude`, and `agy`."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import sys
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from anchoring import anchor_and_filter_findings
from codex_runner import (
    DEFAULT_AGENT,
    DEFAULT_TIMEOUT_SEC,
    build_code_prompt,
    build_plan_prompt,
    normalize_agent_name,
    read_agent_default_profile,
    run_agent_freeform_task,
    run_codex_structured_review,
)
from selection import (
    _compute_extra_temp_prefixes,
    collect_diff_selection,
    discover_project_rules,
    resolve_git_root,
)
from session_store import (
    DEFAULT_MAX_ROUNDS,
    SessionStore,
    resolve_artifact_root,
    validate_max_rounds,
)

AGENT_CHOICES = ["", "codex", "claude", "claude-code", "agy", "antigravity", "gemini"]


def _split_csv(raw: str) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _read_optional_text(text_val: str, file_val: str) -> str:
    if file_val:
        p = Path(file_val)
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")
        return p.read_text(encoding="utf-8", errors="replace")
    return text_val or ""


def _resolve_effective_agent_and_labels(
    agent_arg: str,
    model_arg: str,
    effort_arg: str,
    saved_agent: str = "",
) -> tuple[str, str, str, str, str]:
    effective_raw = agent_arg.strip() or saved_agent.strip() or DEFAULT_AGENT
    norm_agent = normalize_agent_name(effective_raw)
    cfg_model, cfg_effort = read_agent_default_profile(norm_agent)
    m_override = model_arg.strip()
    e_override = effort_arg.strip()
    m_label = m_override if m_override else f"default({cfg_model})"
    e_label = e_override if e_override else f"default({cfg_effort})"
    return norm_agent, m_override, e_override, m_label, e_label


def _validate_raw_revision_flags(from_ref: str, to_ref: str, commit_ref: str) -> None:
    if commit_ref and (from_ref or to_ref):
        raise ValueError("--commit cannot be combined with --from/--to")
    if to_ref and not from_ref:
        raise ValueError("--to requires --from")


def cmd_preview(args: argparse.Namespace) -> int:
    _validate_raw_revision_flags(args.from_ref.strip(), args.to_ref.strip(), args.commit.strip())
    cwd = Path(args.repo).resolve()
    git_root = resolve_git_root(cwd)
    if git_root is None:
        raise RuntimeError(f"Not inside a Git repository: {cwd}")
    artifact_root = resolve_artifact_root(git_root, args.artifact_dir)

    sel = collect_diff_selection(
        repo_dir=git_root,
        from_ref=args.from_ref,
        to_ref=args.to_ref,
        commit_ref=args.commit,
        target_paths=_split_csv(args.paths),
        include_globs=_split_csv(args.include),
        exclude_globs=_split_csv(args.exclude),
        exclude_tests=args.exclude_tests,
        extra_exclude_dirs=[artifact_root],
    )
    payload = {
        "repo_dir": sel.repo_dir,
        "mode": sel.mode,
        "resolved_base": sel.resolved_base,
        "resolved_head": sel.resolved_head,
        "merge_base": sel.merge_base,
        "fingerprint": sel.fingerprint(),
        "selected_count": len(sel.selected_files),
        "excluded_count": len(sel.excluded_files),
        "selected_files": [
            {
                "path": f.path,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
            }
            for f in sel.selected_files
        ],
        "excluded_files": [
            {
                "path": f.path,
                "status": f.status,
                "reason": f.exclude_reason,
            }
            for f in sel.excluded_files
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_discuss(args: argparse.Namespace) -> int:
    """Run read-only plan drafting or technical discussion via `codex` (default), `claude`, or `agy`."""
    cwd = Path(args.repo).resolve()
    base_dir = resolve_git_root(cwd) or cwd
    artifact_root = resolve_artifact_root(base_dir, args.artifact_dir)
    norm_agent, m_override, e_override, m_label, e_label = _resolve_effective_agent_and_labels(
        args.agent, args.model, args.effort
    )

    topic_text = _read_optional_text(args.prompt, args.prompt_file)
    if not topic_text.strip() and not sys.stdin.isatty():
        topic_text = sys.stdin.read()
    if not topic_text.strip():
        raise ValueError("Must supply -p/--prompt, --prompt-file, or pipe topic via stdin.")

    rules = discover_project_rules(base_dir, [])
    full_prompt = (
        "# 任务：技术方案讨论与架构推演（只读模式，禁止修改文件）\n\n"
        "请结合当前工作区真实源码，围绕以下需求给出清晰、模块化、避免过度防御与冗余抽象的方案或推演结论（中文输出）：\n\n"
        f"{topic_text.strip()}\n"
    )
    if rules.strip():
        full_prompt += f"\n## 仓库规范摘要\n\n{rules.strip()}\n"

    disc_dir = artifact_root / "discussions"
    disc_dir.mkdir(parents=True, exist_ok=True)
    res = run_agent_freeform_task(
        prompt=full_prompt,
        task_kind="discuss",
        cwd=base_dir,
        scratch_parent=disc_dir,
        agent=norm_agent,
        model=m_override,
        effort=e_override,
        resume_id=args.resume_id,
        timeout_sec=args.timeout,
        keep_mcp=args.keep_mcp,
    )

    final_model = m_override or (
        f"default({res['runtime_model']})" if res.get("runtime_model") else m_label
    )
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved_md = disc_dir / f"discuss-{norm_agent}-{ts}-{uuid.uuid4().hex[:4]}.md"
    saved_md.write_text(res["response"] + "\n", encoding="utf-8")

    out_file_str = ""
    if args.out_file.strip():
        out_target = Path(args.out_file).resolve()
        out_target.parent.mkdir(parents=True, exist_ok=True)
        out_target.write_text(res["response"] + "\n", encoding="utf-8")
        out_file_str = str(out_target)

    payload = {
        "task": "discuss",
        "agent": norm_agent,
        "model": final_model,
        "effort": e_label,
        "elapsed_sec": res["elapsed_sec"],
        "agent_session_id": res["agent_session_id"],
        "markdown_path": str(saved_md),
        "out_file": out_file_str,
        "response": res["response"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    """Delegate TODO plan execution (workspace-write) to `agy`, `claude`, or `codex`."""
    cwd = Path(args.repo).resolve()
    git_root = resolve_git_root(cwd)
    base_dir = git_root or cwd
    artifact_root = resolve_artifact_root(base_dir, args.artifact_dir)
    norm_agent, m_override, e_override, m_label, e_label = _resolve_effective_agent_and_labels(
        args.agent, args.model, args.effort
    )

    plan_body = _read_optional_text(args.prompt, args.plan_file)
    if not plan_body.strip() and not sys.stdin.isatty():
        plan_body = sys.stdin.read()
    if not plan_body.strip():
        raise ValueError("Must supply --plan-file, -p/--prompt, or pipe TODO plan via stdin.")

    extra_goal = args.goal.strip()
    rules = discover_project_rules(base_dir, [])
    parts = [
        "# 任务：按定稿计划执行代码实现与自检",
        "",
        "## 开发铁律",
        "1. 模块化开发，遵循单一职责，避免同一逻辑多处重复实现。",
        "2. 严禁编写过度防御代码（不要在受控内部调用链堆砌多余判空、类型检查或吞异常兜底）。",
        "3. Windows 下拉起任何子进程必须静默无窗口（Python `CREATE_NO_WINDOW` / Node `windowsHide: true` / PowerShell `-NoNewWindow`），过程文件统一放在 `.<项目名>-temp` 下。",
        "4. 完成编码后运行相关的构建或单元测试验证，并简明汇报实际修改的文件与验证结果（中文）。",
    ]
    if extra_goal:
        parts.extend(["", "## 补充任务约束", "", extra_goal])
    if rules.strip():
        parts.extend(["", "## 仓库规范摘要", "", rules.strip()])
    parts.extend(["", "## 待执行计划 / TODO 清单", "", plan_body.strip()])

    exec_dir = artifact_root / "executions"
    exec_dir.mkdir(parents=True, exist_ok=True)
    res = run_agent_freeform_task(
        prompt="\n".join(parts) + "\n",
        task_kind="exec",
        cwd=base_dir,
        scratch_parent=exec_dir,
        agent=norm_agent,
        model=m_override,
        effort=e_override,
        resume_id=args.resume_id,
        timeout_sec=args.timeout,
        keep_mcp=args.keep_mcp,
    )

    final_model = m_override or (
        f"default({res['runtime_model']})" if res.get("runtime_model") else m_label
    )
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_md = exec_dir / f"exec-{norm_agent}-{ts}-{uuid.uuid4().hex[:4]}.md"
    log_md.write_text(res["response"] + "\n", encoding="utf-8")

    changed_files: list[dict] = []
    if git_root is not None:
        sel = collect_diff_selection(repo_dir=git_root, extra_exclude_dirs=[artifact_root])
        changed_files = [
            {
                "path": f.path,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
            }
            for f in sel.selected_files
        ]

    payload = {
        "task": "exec",
        "agent": norm_agent,
        "model": final_model,
        "effort": e_label,
        "elapsed_sec": res["elapsed_sec"],
        "agent_session_id": res["agent_session_id"],
        "markdown_path": str(log_md),
        "changed_file_count": len(changed_files),
        "changed_files": changed_files,
        "response": res["response"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    validate_max_rounds(args.max_rounds)
    cwd = Path(args.repo).resolve()
    git_root = resolve_git_root(cwd)
    base_dir = git_root or cwd
    artifact_root = resolve_artifact_root(base_dir, args.artifact_dir)

    explicit_goal = _read_optional_text(args.goal, args.goal_file)
    explicit_plan_arg = args.plan_file.strip()

    if args.session.strip():
        store = SessionStore.load_existing(artifact_root, args.session, expected_mode="plan")
        round_no, prior_findings, claim_token, active_manifest = store.prepare_next_round(
            goal_override=explicit_goal if explicit_goal else None,
            plan_file_override=str(Path(explicit_plan_arg).resolve()) if explicit_plan_arg else None,
        )
        candidate_plan_arg = explicit_plan_arg or str(active_manifest.get("plan_file") or "")
        if candidate_plan_arg == "<stdin>":
            candidate_plan_arg = ""
    else:
        store = None
        claim_token = ""
        active_manifest = {}
        candidate_plan_arg = explicit_plan_arg

    norm_agent, m_override, e_override, m_label, e_label = _resolve_effective_agent_and_labels(
        args.agent,
        args.model,
        args.effort,
        saved_agent=str(active_manifest.get("agent") or ""),
    )

    try:
        if candidate_plan_arg:
            plan_path = Path(candidate_plan_arg).resolve()
            if not plan_path.is_file():
                raise FileNotFoundError(f"Plan file not found: {plan_path}")
            plan_text = plan_path.read_text(encoding="utf-8", errors="replace")
            plan_label = str(plan_path)
        elif not sys.stdin.isatty():
            plan_text = sys.stdin.read()
            plan_label = "<stdin>"
        else:
            raise ValueError("Must supply --plan-file <path> or pipe plan markdown via stdin.")

        if not plan_text.strip():
            raise ValueError("Plan content is empty.")

        if store is None:
            store = SessionStore.create_new(
                root_dir=artifact_root,
                mode="plan",
                agent=norm_agent,
                max_rounds=args.max_rounds,
                model=m_label,
                effort=e_label,
                goal=explicit_goal,
                plan_file=plan_label,
            )
            round_no, prior_findings, claim_token, active_manifest = store.prepare_next_round()

        goal_text = str(active_manifest.get("goal") or "")
        project_rules = discover_project_rules(base_dir, [])
        prompt = build_plan_prompt(
            plan_text=plan_text,
            plan_label=plan_label,
            goal=goal_text,
            round_no=round_no,
            prior_findings=prior_findings,
            project_rules=project_rules,
        )
        raw_resp, elapsed = run_codex_structured_review(
            prompt=prompt,
            mode="plan",
            cwd=base_dir,
            scratch_parent=store.session_dir,
            agent=norm_agent,
            model=m_override,
            effort=e_override,
            timeout_sec=args.timeout,
            keep_mcp=args.keep_mcp,
        )
        runtime_model = str(raw_resp.get("_runtime_model") or "")
        final_model_label = m_override or (f"default({runtime_model})" if runtime_model else m_label)

        raw_audits = raw_resp.get("prior_findings_audit") or []
        extra_temp = _compute_extra_temp_prefixes(base_dir, [artifact_root])
        accepted, dropped = anchor_and_filter_findings(
            raw_findings=raw_resp.get("findings") or [],
            mode="plan",
            repo_dir=base_dir,
            plan_text=plan_text,
            plan_label=plan_label,
            prior_findings=prior_findings,
            prior_findings_audit=raw_audits,
            extra_temp_prefixes=extra_temp,
        )
        fp = hashlib.sha256(plan_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        record = store.record_round(
            round_no=round_no,
            fingerprint=fp,
            summary=str(raw_resp.get("summary") or ""),
            findings=accepted,
            prior_findings_audit=raw_audits,
            suppressed_notes=raw_resp.get("suppressed_over_defensive_notes") or [],
            dropped_findings=dropped,
            elapsed_sec=elapsed,
            prior_findings=prior_findings,
            claim_token=claim_token,
            executed_agent=norm_agent,
            executed_model=final_model_label,
            executed_effort=e_label,
        )
    except BaseException:
        if store is not None and claim_token:
            store.release_claim(claim_token)
        raise

    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def _resolve_code_scope(
    args: argparse.Namespace,
    manifest: dict | None,
) -> tuple[dict, dict | None]:
    from_ref = args.from_ref.strip()
    to_ref = args.to_ref.strip()
    commit_ref = args.commit.strip()

    if manifest is None:
        _validate_raw_revision_flags(from_ref, to_ref, commit_ref)
    else:
        if commit_ref and (from_ref or to_ref):
            raise ValueError("--commit cannot be combined with --from/--to")

    if manifest is None:
        return (
            {
                "from_ref": from_ref,
                "to_ref": to_ref,
                "commit_ref": commit_ref,
                "direct_base_ref": "",
                "direct_head_ref": "",
                "workspace_base_ref": "",
                "paths": _split_csv(args.paths),
                "include": _split_csv(args.include),
                "exclude": _split_csv(args.exclude),
                "exclude_tests": bool(args.exclude_tests),
            },
            None,
        )

    has_saved_scope = "scope" in manifest and isinstance(manifest.get("scope"), dict) and bool(manifest.get("scope"))
    has_explicit_scope_arg = any(
        [
            from_ref,
            to_ref,
            commit_ref,
            args.paths.strip(),
            args.include.strip(),
            args.exclude.strip(),
            args.exclude_tests,
            args.confirm_workspace,
        ]
    )
    if not has_saved_scope and not has_explicit_scope_arg:
        raise ValueError(
            "Legacy session has no persisted `scope`; supply original `--from` / `-c` / `--paths` or `--confirm-workspace` once to bind scope."
        )

    saved_scope = dict(manifest.get("scope") or {})
    initial_base = str(saved_scope.get("initial_base_ref") or "")

    target_paths = _split_csv(args.paths) if args.paths else list(saved_scope.get("paths") or [])
    include_globs = _split_csv(args.include) if args.include else list(saved_scope.get("include") or [])
    exclude_globs = _split_csv(args.exclude) if args.exclude else list(saved_scope.get("exclude") or [])
    exclude_tests = bool(args.exclude_tests or saved_scope.get("exclude_tests", False))

    direct_base_ref = ""
    direct_head_ref = ""
    workspace_base_ref = ""

    if not (from_ref or to_ref or commit_ref):
        workspace_base_ref = initial_base
    elif commit_ref:
        if initial_base and commit_ref == saved_scope.get("initial_commit_arg"):
            commit_ref = ""
            workspace_base_ref = initial_base
        elif initial_base:
            direct_base_ref = initial_base
            direct_head_ref = commit_ref
            commit_ref = ""
    elif to_ref and not from_ref:
        if not initial_base:
            raise ValueError("--to requires --from when session has no initial_base_ref")
        direct_base_ref = initial_base
        direct_head_ref = to_ref
        to_ref = ""

    resolved_scope = {
        "from_ref": from_ref,
        "to_ref": to_ref,
        "commit_ref": commit_ref,
        "direct_base_ref": direct_base_ref,
        "direct_head_ref": direct_head_ref,
        "workspace_base_ref": workspace_base_ref,
        "paths": target_paths,
        "include": include_globs,
        "exclude": exclude_globs,
        "exclude_tests": exclude_tests,
    }
    scope_migration = dict(resolved_scope) if not has_saved_scope else None
    return resolved_scope, scope_migration


def cmd_code(args: argparse.Namespace) -> int:
    validate_max_rounds(args.max_rounds)
    cwd = Path(args.repo).resolve()
    repo_root = resolve_git_root(cwd)
    if repo_root is None:
        raise RuntimeError(f"Not inside a Git repository: {cwd}")
    artifact_root = resolve_artifact_root(repo_root, args.artifact_dir)

    explicit_goal = _read_optional_text(args.background, args.background_file)
    explicit_plan_arg = args.plan_file.strip()
    resolved_explicit_plan = str(Path(explicit_plan_arg).resolve()) if explicit_plan_arg else None

    if args.session.strip():
        store = SessionStore.load_existing(artifact_root, args.session, expected_mode="code")
        round_no, prior_findings, claim_token, active_manifest = store.prepare_next_round(
            goal_override=explicit_goal if (args.background or args.background_file) else None,
            plan_file_override=resolved_explicit_plan,
        )
    else:
        store = None
        claim_token = ""
        active_manifest = None

    norm_agent, m_override, e_override, m_label, e_label = _resolve_effective_agent_and_labels(
        args.agent,
        args.model,
        args.effort,
        saved_agent=str((active_manifest or {}).get("agent") or ""),
    )

    try:
        scope, scope_migration = _resolve_code_scope(args, active_manifest)
        effective_plan_str = resolved_explicit_plan or str((active_manifest or {}).get("plan_file") or "")
        plan_text = _read_optional_text("", effective_plan_str) if effective_plan_str else ""

        sel = collect_diff_selection(
            repo_dir=repo_root,
            from_ref=scope["from_ref"],
            to_ref=scope["to_ref"],
            commit_ref=scope["commit_ref"],
            direct_base_ref=scope["direct_base_ref"],
            direct_head_ref=scope["direct_head_ref"],
            workspace_base_ref=scope["workspace_base_ref"],
            target_paths=scope["paths"],
            include_globs=scope["include"],
            exclude_globs=scope["exclude"],
            exclude_tests=scope["exclude_tests"],
            extra_exclude_dirs=[artifact_root],
        )

        recorded_base = "EMPTY_TREE" if sel.base_is_empty else sel.resolved_base
        if store is None:
            persisted_scope = dict(scope)
            persisted_scope["initial_mode"] = sel.mode
            persisted_scope["initial_commit_arg"] = args.commit.strip()
            persisted_scope["initial_base_ref"] = recorded_base
            store = SessionStore.create_new(
                root_dir=artifact_root,
                mode="code",
                agent=norm_agent,
                max_rounds=args.max_rounds,
                model=m_label,
                effort=e_label,
                goal=explicit_goal,
                plan_file=effective_plan_str,
                scope=persisted_scope,
            )
            round_no, prior_findings, claim_token, active_manifest = store.prepare_next_round()
        elif scope_migration is not None:
            scope_migration["initial_mode"] = sel.mode
            scope_migration["initial_commit_arg"] = args.commit.strip()
            scope_migration["initial_base_ref"] = recorded_base
            with store._locked():
                latest = store.load()
                latest["scope"] = scope_migration
                store._write_json(store.manifest_path, latest)

        goal_text = str((active_manifest or {}).get("goal") or "")
        if (
            not sel.selected_files
            and not goal_text.strip()
            and not plan_text.strip()
            and not prior_findings
        ):
            record = store.record_round(
                round_no=round_no,
                fingerprint=sel.fingerprint(),
                summary="未检测到需要审查的代码文件变更（0 待审文件）。",
                findings=[],
                prior_findings_audit=[],
                suppressed_notes=[],
                dropped_findings=[],
                elapsed_sec=0.0,
                prior_findings=prior_findings,
                claim_token=claim_token,
                executed_agent=norm_agent,
                executed_model="skipped(empty_diff)",
                executed_effort="none",
            )
            record["selected_file_count"] = 0
            record["excluded_file_count"] = len(sel.excluded_files)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0

        prompt = build_code_prompt(
            selection=sel,
            goal=goal_text,
            plan_text=plan_text,
            round_no=round_no,
            prior_findings=prior_findings,
        )
        raw_resp, elapsed = run_codex_structured_review(
            prompt=prompt,
            mode="code",
            cwd=repo_root,
            scratch_parent=store.session_dir,
            agent=norm_agent,
            model=m_override,
            effort=e_override,
            timeout_sec=args.timeout,
            keep_mcp=args.keep_mcp,
        )
        runtime_model = str(raw_resp.get("_runtime_model") or "")
        final_model_label = m_override or (f"default({runtime_model})" if runtime_model else m_label)

        raw_audits = raw_resp.get("prior_findings_audit") or []
        content_ref = sel.resolved_head if sel.resolved_head else ""
        extra_temp = _compute_extra_temp_prefixes(repo_root, [artifact_root])
        accepted, dropped = anchor_and_filter_findings(
            raw_findings=raw_resp.get("findings") or [],
            mode="code",
            repo_dir=repo_root,
            selected_files=sel.selected_files,
            content_ref=content_ref,
            prior_findings=prior_findings,
            prior_findings_audit=raw_audits,
            exclude_globs=scope["exclude"],
            extra_temp_prefixes=extra_temp,
        )
        record = store.record_round(
            round_no=round_no,
            fingerprint=sel.fingerprint(),
            summary=str(raw_resp.get("summary") or ""),
            findings=accepted,
            prior_findings_audit=raw_audits,
            suppressed_notes=raw_resp.get("suppressed_over_defensive_notes") or [],
            dropped_findings=dropped,
            elapsed_sec=elapsed,
            prior_findings=prior_findings,
            claim_token=claim_token,
            executed_agent=norm_agent,
            executed_model=final_model_label,
            executed_effort=e_label,
        )
    except BaseException:
        if store is not None and claim_token:
            store.release_claim(claim_token)
        raise

    record["selected_file_count"] = len(sel.selected_files)
    record["excluded_file_count"] = len(sel.excluded_files)
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    cwd = Path(args.repo).resolve()
    base_dir = resolve_git_root(cwd) or cwd
    root = resolve_artifact_root(base_dir, args.artifact_dir)
    store = SessionStore.load_existing(root, args.session)
    if args.format == "json":
        print(json.dumps(store.load(), ensure_ascii=False, indent=2))
    else:
        print(store.render_final_summary())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex_review.py",
        description="Unified multi-agent workflow CLI (`codex` default / `claude` / `agy`) for discussion, review, and plan execution.",
    )
    sub = p.add_subparsers(dest="subcommand", required=True)

    def add_common_git_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo", default=".", help="Repository or workspace directory")
        sp.add_argument("--from", dest="from_ref", default="", help="Base branch/ref for merge-base diff")
        sp.add_argument("--to", dest="to_ref", default="", help="Target ref (default HEAD when --from is set)")
        sp.add_argument("-c", "--commit", default="", help="Single commit SHA/ref to review")
        sp.add_argument("--paths", default="", help="Comma-separated target paths filter")
        sp.add_argument("--include", default="", help="Comma-separated include globs")
        sp.add_argument("--exclude", default="", help="Comma-separated exclude globs")
        sp.add_argument("--exclude-tests", action="store_true", help="Exclude test files from review")

    def add_common_agent_flags(sp: argparse.ArgumentParser, with_rounds: bool = True) -> None:
        sp.add_argument(
            "--agent",
            default="",
            choices=AGENT_CHOICES,
            help="Target local CLI agent: 'codex' (default), 'claude' (Claude Code), or 'agy' (Antigravity CLI)",
        )
        if with_rounds:
            sp.add_argument("--session", default="", help="Existing review session ID to continue Rounds 2..5")
            sp.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="Max review rounds (1..5, default 1)")
        sp.add_argument("--model", default="", help="Optional model override; empty uses selected agent's default config")
        sp.add_argument("--effort", default="", help="Optional reasoning effort override; empty uses selected agent's default config")
        sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SEC, help="Timeout seconds")
        sp.add_argument("--artifact-dir", default="", help="Override .<project>-temp/codex-review root")
        sp.add_argument("--keep-mcp", action="store_true", help="Keep user MCP servers enabled")

    sp_prev = sub.add_parser("preview", help="Preview deterministic 6-gate file selection without calling an agent")
    add_common_git_flags(sp_prev)
    sp_prev.add_argument("--artifact-dir", default="", help="Override .<project>-temp/codex-review root")
    sp_prev.set_defaults(func=cmd_preview)

    sp_disc = sub.add_parser("discuss", help="Read-only plan drafting or architecture discussion via codex/claude/agy")
    sp_disc.add_argument("--repo", default=".", help="Workspace/repository directory")
    sp_disc.add_argument("-p", "--prompt", default="", help="Discussion topic or plan drafting request")
    sp_disc.add_argument("--prompt-file", default="", help="Path to discussion prompt file")
    sp_disc.add_argument("--out-file", default="", help="Optional path to save the drafted plan/response Markdown")
    sp_disc.add_argument("--resume-id", default="", help="Optional underlying agent session/conversation ID to continue")
    add_common_agent_flags(sp_disc, with_rounds=False)
    sp_disc.set_defaults(func=cmd_discuss)

    sp_exec = sub.add_parser("exec", help="Delegate TODO plan execution (workspace-write) to agy/claude/codex")
    sp_exec.add_argument("--repo", default=".", help="Workspace/repository directory")
    sp_exec.add_argument("--plan-file", default="", help="Path to approved TODO/plan Markdown file to execute")
    sp_exec.add_argument("-p", "--prompt", default="", help="Inline task instructions when no --plan-file is given")
    sp_exec.add_argument("-g", "--goal", default="", help="Additional execution constraints")
    sp_exec.add_argument("--resume-id", default="", help="Optional underlying agent session/conversation ID to continue")
    add_common_agent_flags(sp_exec, with_rounds=False)
    sp_exec.set_defaults(func=cmd_exec)

    sp_plan = sub.add_parser("plan", help="Run structured read-only Plan/Proposal review via codex (default)/claude/agy")
    sp_plan.add_argument("--repo", default=".", help="Workspace/repository directory for read-only codebase verification")
    sp_plan.add_argument("--plan-file", default="", help="Path to Markdown plan/proposal file (or pipe via stdin)")
    sp_plan.add_argument("-g", "--goal", default="", help="User goal / requirements summary")
    sp_plan.add_argument("-G", "--goal-file", default="", help="Path to user goal / requirements file")
    add_common_agent_flags(sp_plan, with_rounds=True)
    sp_plan.set_defaults(func=cmd_plan)

    sp_code = sub.add_parser("code", help="Run structured read-only Code & Acceptance review via codex (default)/claude/agy")
    add_common_git_flags(sp_code)
    sp_code.add_argument("--plan-file", default="", help="Approved TODO/plan Markdown file to verify completion against")
    sp_code.add_argument("-b", "--background", default="", help="Business context / goal summary")
    sp_code.add_argument("-B", "--background-file", default="", help="Path to background context file")
    sp_code.add_argument("--confirm-workspace", action="store_true", help="Confirm workspace scope when upgrading a legacy session")
    add_common_agent_flags(sp_code, with_rounds=True)
    sp_code.set_defaults(func=cmd_code)

    sp_sum = sub.add_parser("summary", help="Render multi-round convergence summary for a review session")
    sp_sum.add_argument("--repo", default=".", help="Repository or workspace directory")
    sp_sum.add_argument("--artifact-dir", default="", help="Override .<project>-temp/codex-review root")
    sp_sum.add_argument("--session", required=True, help="Session ID")
    sp_sum.add_argument("-f", "--format", choices=["markdown", "json"], default="markdown")
    sp_sum.set_defaults(func=cmd_summary)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

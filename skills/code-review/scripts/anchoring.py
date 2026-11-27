"""Unified diff hunk mapping, sliding-window line anchoring, and scope/Ground-A filtering."""

from __future__ import annotations

import os
import re
from pathlib import Path

from selection import (
    DiffFile,
    is_secret_path,
    is_temp_artifact_path,
    matches_any_glob,
    normalize_git_path,
    normalize_rel_path,
    run_git,
)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def extract_changed_ranges(patch: str) -> list[tuple[int, int]]:
    """Extract (start_line, end_line) 1-based ranges in the new file for each diff hunk."""
    ranges: list[tuple[int, int]] = []
    for line in patch.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        new_start = int(m.group(3))
        new_count = int(m.group(4)) if m.group(4) is not None else 1
        if new_count <= 0:
            if new_start > 0:
                ranges.append((new_start, new_start))
        else:
            ranges.append((new_start, new_start + new_count - 1))
    return ranges


def is_safe_repo_relative_path(repo_dir: Path, rel_path: str) -> bool:
    """Verify `rel_path` stays strictly inside `repo_dir` without `..` or drive/root escape (F32)."""
    if not rel_path or rel_path.startswith(("/", "\\")) or ":" in Path(rel_path).drive:
        return False
    parts = [seg for seg in rel_path.replace("\\", "/").split("/") if seg]
    if not parts or ".." in parts:
        return False
    root_res = repo_dir.resolve()
    parent_target = (repo_dir / "/".join(parts[:-1])).resolve() if len(parts) > 1 else root_res
    try:
        parent_target.relative_to(root_res)
    except ValueError:
        return False
    candidate = repo_dir / "/".join(parts)
    if not candidate.is_symlink():
        try:
            candidate.resolve().relative_to(root_res)
        except ValueError:
            return False
    return True


def load_target_file_lines(repo_dir: Path, rel_path: str, ref: str = "") -> list[str] | None:
    """Load target file lines from git ref or working tree without following symlinks outside repo."""
    if not rel_path or is_secret_path(rel_path):
        return None
    if ref:
        res = run_git(repo_dir, ["show", f"{ref}:{rel_path}"], check=False)
        if res.returncode != 0:
            return None
        return res.stdout.splitlines()

    if not is_safe_repo_relative_path(repo_dir, rel_path):
        return None
    candidate = repo_dir / rel_path
    if candidate.is_symlink():
        return [normalize_git_path(os.readlink(candidate))]
    if not candidate.is_file():
        return None
    return candidate.read_text(encoding="utf-8", errors="replace").splitlines()


def _overlaps_any(start: int, end: int, ranges: list[tuple[int, int]], margin: int = 5) -> bool:
    for rs, re_ in ranges:
        if start <= re_ + margin and end >= max(1, rs - margin):
            return True
    return False


def _prepare_snippet_variants(snippet: str) -> list[list[str]]:
    """Return [literal_trimmed_lines, diff_prefix_stripped_lines] so `-x`/`--n` source lines match first (F15)."""
    raw = snippet.splitlines()
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    if not raw:
        return []

    literal = [ln.strip() if ln.strip() else "" for ln in raw]
    variants = [literal]

    non_empty = [ln for ln in raw if ln.strip()]
    all_patch_prefixed = bool(non_empty) and all(
        ln[0] in "+-" and not ln.startswith(("+++ ", "--- "))
        for ln in non_empty
    )
    if all_patch_prefixed:
        stripped = [ln[1:].strip() if ln.strip() else "" for ln in raw]
        if stripped != literal:
            variants.append(stripped)
    return variants


def resolve_snippet_lines(
    file_lines: list[str],
    snippet: str,
    claimed_start: int,
    claimed_end: int,
    changed_ranges: list[tuple[int, int]],
) -> tuple[int, int, str]:
    """Deterministically verify or relocate line numbers via contiguous exact-line matching (F15)."""
    total = len(file_lines)
    if total == 0:
        return 0, 0, "file_level"

    variants = _prepare_snippet_variants(snippet)
    if not variants:
        if 1 <= claimed_start <= total:
            end = min(total, max(claimed_start, claimed_end))
            return claimed_start, end, "claimed_line"
        return 0, 0, "file_level"

    norm_file = [ln.strip() for ln in file_lines]

    def score(cand: tuple[int, int]) -> tuple[int, int, int]:
        in_diff = 0 if _overlaps_any(cand[0], cand[1], changed_ranges) else 1
        exact_claim = 0 if (cand[0] == claimed_start and cand[1] == max(claimed_start, claimed_end)) else 1
        dist = abs(cand[0] - claimed_start) if claimed_start > 0 else cand[0]
        return (in_diff, exact_claim, dist)

    # 1. Try literal source lines first, then patch-prefix-stripped lines.
    for cleaned_snip in variants:
        win_len = len(cleaned_snip)
        if win_len > total:
            continue
        contiguous = [
            (idx + 1, idx + win_len)
            for idx in range(total - win_len + 1)
            if norm_file[idx : idx + win_len] == cleaned_snip
        ]
        if contiguous:
            best_s, best_e = min(contiguous, key=score)
            expected_end = min(total, max(claimed_start, claimed_end)) if claimed_start > 0 else 0
            status = "exact" if (best_s == claimed_start and best_e == expected_end) else "relocated"
            return best_s, best_e, status

    # 2. Fallback: match the longest non-empty line by equality first, then substring -> always `partial_relocated`.
    cleaned_snip = variants[-1]
    non_empty_snip = [ln for ln in cleaned_snip if ln]
    primary = max(non_empty_snip, key=len) if non_empty_snip else ""
    if len(primary) >= 4:
        exact_single = [(idx + 1, idx + 1) for idx, hay in enumerate(norm_file) if hay == primary]
        sub_single = (
            exact_single
            if exact_single
            else [(idx + 1, idx + 1) for idx, hay in enumerate(norm_file) if primary in hay]
        )
        if sub_single:
            best_s, best_e = min(sub_single, key=score)
            return best_s, best_e, "partial_relocated"

    if 1 <= claimed_start <= total:
        return claimed_start, min(total, max(claimed_start, claimed_end)), "unverified_line"
    return 0, 0, "file_level"


def _matches_plan_target(rel_file: str, plan_label: str) -> bool:
    if not rel_file or rel_file in {"<stdin>", "plan_document"}:
        return True
    if not plan_label:
        return False
    norm_file = normalize_rel_path(rel_file)
    norm_label = normalize_rel_path(plan_label)
    base_label = Path(plan_label).name
    if norm_file == norm_label or norm_file == base_label or norm_label.endswith("/" + norm_file):
        return True
    try:
        return Path(rel_file).resolve() == Path(plan_label).resolve()
    except OSError:
        return False


def _canonicalize_selected_path(raw_file: str, allowed_paths: set[str]) -> str:
    exact = normalize_git_path(raw_file)
    if exact in allowed_paths:
        return exact
    stripped = normalize_rel_path(raw_file)
    return stripped


def anchor_and_filter_findings(
    raw_findings: list[dict],
    mode: str,
    repo_dir: Path | None,
    selected_files: list[DiffFile] | None = None,
    content_ref: str = "",
    plan_text: str = "",
    plan_label: str = "",
    prior_findings: list[dict] | None = None,
    prior_findings_audit: list[dict] | None = None,
    exclude_globs: list[str] | None = None,
    extra_temp_prefixes: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Anchor line numbers, enforce selected-file scope, and reconcile IDs without clobbering prior findings."""
    accepted: list[dict] = []
    dropped: list[dict] = []

    diff_map: dict[str, list[tuple[int, int]]] = {}
    deleted_paths: set[str] = set()
    allowed_paths: set[str] = set()
    rename_old_to_new: dict[str, str] = {}

    for df in selected_files or []:
        p = normalize_git_path(df.path)
        allowed_paths.add(p)
        diff_map[p] = extract_changed_ranges(df.patch)
        if df.status == "DELETED":
            deleted_paths.add(p)
        if df.old_path:
            old_p = normalize_git_path(df.old_path)
            allowed_paths.add(old_p)
            rename_old_to_new[old_p] = p

    # Map each prior finding ID to its valid target path(s) (accounting for renames, F33)
    prior_by_id: dict[str, dict] = {}
    prior_allowed_paths_by_id: dict[str, set[str]] = {}
    for pf in prior_findings or []:
        pid = str(pf.get("id") or "").strip()
        if not pid:
            continue
        prior_by_id[pid] = pf
        pf_file = normalize_git_path(str(pf.get("file") or ""))
        valid_set = {pf_file}
        if pf_file in rename_old_to_new:
            valid_set.add(rename_old_to_new[pf_file])
        prior_allowed_paths_by_id[pid] = valid_set

    audited_status_by_id = {
        str(a.get("id") or "").strip(): str(a.get("status") or "").strip()
        for a in (prior_findings_audit or [])
        if a.get("id")
    }

    plan_lines = plan_text.splitlines() if mode == "plan" and plan_text else None

    for item in raw_findings:
        finding = dict(item)
        raw_file = str(finding.get("file") or "")
        start = max(0, int(finding.get("start_line") or 0))
        end = max(0, int(finding.get("end_line") or 0))
        snippet = str(finding.get("snippet") or "")
        category = str(finding.get("category") or "")
        raw_id = str(finding.get("id") or "").strip()

        # In `plan` mode, match the explicitly submitted plan document FIRST before temp-dir filtering
        # so plans saved under `.<project>-temp/code-review/draft-plan.md` are never dropped (F16)!
        if mode == "plan":
            rel_file = normalize_rel_path(raw_file)
            if plan_lines is not None and _matches_plan_target(rel_file, plan_label):
                finding["file"] = normalize_rel_path(plan_label) if plan_label else "<stdin>"
                s, e, st = resolve_snippet_lines(plan_lines, snippet, start, end, [])
                finding["start_line"] = s
                finding["end_line"] = e
                finding["anchor_status"] = st
                accepted.append(finding)
                continue

            if rel_file and (
                is_secret_path(rel_file)
                or is_temp_artifact_path(rel_file, extra_temp_prefixes)
                or (exclude_globs and matches_any_glob(rel_file, exclude_globs))
            ):
                finding["drop_reason"] = "excluded_target_path"
                dropped.append(finding)
                continue

            finding["file"] = rel_file
            if repo_dir is not None and rel_file:
                repo_lines = load_target_file_lines(repo_dir, rel_file)
                if repo_lines is not None:
                    s, e, st = resolve_snippet_lines(repo_lines, snippet, start, end, [])
                    finding["start_line"] = s
                    finding["end_line"] = e
                    finding["anchor_status"] = st
                else:
                    finding["start_line"] = 0
                    finding["end_line"] = 0
                    finding["anchor_status"] = "plan_ref"
            else:
                finding["anchor_status"] = "plan_ref"
            accepted.append(finding)
            continue

        # Code mode
        rel_file = _canonicalize_selected_path(raw_file, allowed_paths)
        finding["file"] = rel_file

        if rel_file and (
            is_secret_path(rel_file)
            or is_temp_artifact_path(rel_file, extra_temp_prefixes)
            or (exclude_globs and matches_any_glob(rel_file, exclude_globs))
        ):
            finding["drop_reason"] = "excluded_target_path"
            dropped.append(finding)
            continue

        if rel_file and repo_dir is not None and not content_ref:
            if not is_safe_repo_relative_path(repo_dir, rel_file):
                finding["drop_reason"] = "outside_repository"
                dropped.append(finding)
                continue
        elif rel_file and (
            rel_file.startswith(("/", "\\"))
            or ":" in Path(rel_file).drive
            or ".." in rel_file.split("/")
        ):
            finding["drop_reason"] = "outside_repository"
            dropped.append(finding)
            continue

        is_matching_prior_carryover = (
            raw_id in prior_allowed_paths_by_id
            and rel_file in prior_allowed_paths_by_id[raw_id]
            and audited_status_by_id.get(raw_id) in {"persisting", "partially_fixed"}
        )

        if rel_file:
            in_scope = (
                rel_file in allowed_paths
                or is_matching_prior_carryover
                or category == "missing_requirement"
            )
            if not in_scope:
                finding["drop_reason"] = "out_of_selected_scope"
                dropped.append(finding)
                continue

            if rel_file in deleted_paths:
                finding["start_line"] = 0
                finding["end_line"] = 0
                finding["anchor_status"] = "deleted_file"
                accepted.append(finding)
                continue

            if repo_dir is not None:
                lines = load_target_file_lines(repo_dir, rel_file, ref=content_ref)
                if lines is None:
                    if category == "missing_requirement" or is_matching_prior_carryover:
                        finding["start_line"] = 0
                        finding["end_line"] = 0
                        finding["anchor_status"] = (
                            "missing_required_file" if category == "missing_requirement" else "prior_carryover"
                        )
                        accepted.append(finding)
                    else:
                        finding["drop_reason"] = "ground_a_nonexistent_file"
                        dropped.append(finding)
                    continue

                changed_ranges = diff_map.get(rel_file, [])
                s, e, st = resolve_snippet_lines(lines, snippet, start, end, changed_ranges)
                finding["start_line"] = s
                finding["end_line"] = e
                finding["anchor_status"] = st
            else:
                finding["anchor_status"] = "repo_level"
        else:
            # Fileless findings are allowed ONLY for `missing_requirement` or fileless prior carryovers (F21).
            if category != "missing_requirement" and not is_matching_prior_carryover:
                finding["drop_reason"] = "missing_target_file"
                dropped.append(finding)
                continue
            finding["start_line"] = 0
            finding["end_line"] = 0
            finding["anchor_status"] = "repo_level"

        accepted.append(finding)

    # Collision-free ID reconciliation (F01 + F14 + F33):
    # A finding may keep a prior ID `fid` ONLY when `fid` was explicitly audited as
    # `persisting` or `partially_fixed` AND targets the same (or renamed) file/plan.
    # If `fid` was omitted from `prior_findings_audit` or marked `fixed`, a new finding
    # trying to reuse `fid` gets a fresh ID so `session_store` preserves the unclosed prior item!
    used_ids: set[str] = set()
    reserved_ids = set(prior_by_id.keys())
    next_num = 1

    for f in accepted:
        fid = str(f.get("id") or "").strip()
        if fid in prior_by_id:
            audit_st = audited_status_by_id.get(fid)
            f_path = f.get("file", "")
            valid_owner = (
                mode == "plan"
                or f_path in prior_allowed_paths_by_id.get(fid, set())
            )
            can_reuse_prior_id = (
                fid not in used_ids
                and audit_st in {"persisting", "partially_fixed"}
                and valid_owner
            )
            if can_reuse_prior_id:
                used_ids.add(fid)
                continue
        elif fid and fid not in used_ids:
            used_ids.add(fid)
            continue

        while f"F{next_num}" in used_ids or f"F{next_num}" in reserved_ids:
            next_num += 1
        fresh = f"F{next_num}"
        f["id"] = fresh
        used_ids.add(fresh)
        next_num += 1

    return accepted, dropped

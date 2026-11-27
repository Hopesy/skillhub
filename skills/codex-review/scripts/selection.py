"""Deterministic Git diff collection and 6-gate file selection (distilled from OpenCodeReview)."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import posixpath
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

GIT_SAFE_CONFIG = [
    "-c",
    "core.quotepath=false",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicprefix=false",
]

SECRET_BASENAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".dockercfg",
}
SAFE_ENV_BASENAMES = {
    ".env.example",
    ".env.sample",
    ".env.template",
}
SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}

NOISE_DIR_SEGMENTS = {
    ".git",
    ".idea",
    ".vscode",
    "node_modules",
    "bower_components",
    ".pnpm-store",
    "vendor",
    "dist",
    "build",
    "out",
    "target",
    ".gradle",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".astro",
    ".turbo",
    "__pycache__",
    ".venv",
    "venv",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__snapshots__",
}

NOISE_BASENAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "npm-shrinkwrap.json",
    "Cargo.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "composer.lock",
    "go.sum",
    ".terraform.lock.hcl",
}

GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".pb.go",
    ".pb.cc",
    ".pb.h",
    ".gen.go",
    ".snap",
    ".pyc",
    ".map",
)

BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".db",
    ".sqlite",
    ".sqlite3",
}

TEST_PATTERNS = (
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*.test.js",
    "*.test.jsx",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*Test.java",
    "*Tests.java",
    "*_test.rs",
)


@dataclass
class DiffFile:
    path: str
    old_path: str = ""
    status: str = "MODIFIED"  # ADDED | MODIFIED | DELETED | RENAMED
    is_binary: bool = False
    is_symlink: bool = False
    additions: int = 0
    deletions: int = 0
    patch: str = ""
    exclude_reason: str = ""  # empty means selected for review

    @property
    def selected(self) -> bool:
        return self.exclude_reason == ""


@dataclass
class SelectionResult:
    repo_dir: str
    mode: str  # workspace | workspace_from_base | range | direct_range | commit
    resolved_base: str = ""
    resolved_head: str = ""
    merge_base: str = ""
    base_is_empty: bool = False
    files: list[DiffFile] = field(default_factory=list)
    project_rules_summary: str = ""

    @property
    def selected_files(self) -> list[DiffFile]:
        return [f for f in self.files if f.selected]

    @property
    def excluded_files(self) -> list[DiffFile]:
        return [f for f in self.files if not f.selected]

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        h.update(f"{self.mode}:{self.resolved_base}:{self.resolved_head}\n".encode("utf-8"))
        for f in sorted(self.files, key=lambda x: (x.path, x.old_path, x.status)):
            h.update(f"{f.path}|{f.old_path}|{f.status}|{f.exclude_reason}\n".encode("utf-8"))
            if f.selected:
                h.update(f.patch.encode("utf-8", errors="replace"))
        return h.hexdigest()[:16]


def run_git(
    repo_dir: str | Path,
    args: list[str],
    check: bool = True,
    stdin_text: str | None = None,
    literal_pathspecs: bool = False,
) -> subprocess.CompletedProcess:
    prefix = ["git", "--no-pager"]
    if literal_pathspecs:
        prefix.append("--literal-pathspecs")
    cmd = [*prefix, *GIT_SAFE_CONFIG, *args]
    res = subprocess.run(
        cmd,
        cwd=str(repo_dir),
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res


def resolve_git_root(path: str | Path) -> Path | None:
    res = run_git(path, ["rev-parse", "--show-toplevel"], check=False)
    if res.returncode != 0:
        return None
    out = res.stdout.strip()
    return Path(out).resolve() if out else None


def empty_tree_hash(repo_root: Path) -> str:
    """Compute repository's empty-tree OID purely in memory without writing to .git/objects (F38)."""
    return run_git(repo_root, ["hash-object", "-t", "tree", "--stdin"], stdin_text="").stdout.strip()


def normalize_git_path(p: str) -> str:
    """Preserve exact Git NUL-delimited path characters (including leading spaces), normalizing slashes only."""
    norm = p.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def normalize_rel_path(p: str) -> str:
    """Normalize user-supplied or model-supplied relative path."""
    norm = p.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_secret_path(rel_path: str) -> bool:
    if not rel_path:
        return False
    norm = normalize_git_path(rel_path)
    parts = [seg.lower() for seg in norm.split("/") if seg]
    if ".ssh" in parts or ".aws" in parts or ".gnupg" in parts:
        return True
    base = parts[-1] if parts else ""
    if base in SAFE_ENV_BASENAMES:
        return False
    if base in SECRET_BASENAMES or base.startswith(".env."):
        return True
    if base.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")) and not base.endswith(".pub"):
        return True
    _, ext = posixpath.splitext(base)
    return ext in SECRET_EXTENSIONS


def is_temp_artifact_path(rel_path: str, extra_rel_prefixes: list[str] | None = None) -> bool:
    if not rel_path:
        return False
    norm = normalize_git_path(rel_path)
    parts = [seg for seg in norm.split("/") if seg]
    for seg in parts:
        if seg == ".codex-review" or (seg.startswith(".") and seg.endswith("-temp")):
            return True
    for prefix in extra_rel_prefixes or []:
        p_norm = normalize_git_path(prefix).rstrip("/")
        if p_norm and (norm == p_norm or norm.startswith(p_norm + "/")):
            return True
    return False


def matches_any_glob(rel_path: str, patterns: list[str]) -> bool:
    norm = normalize_git_path(rel_path)
    base = posixpath.basename(norm)
    for raw in patterns:
        pat = raw.replace("\\", "/").strip()
        if not pat:
            continue
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(base, pat):
            return True
        if pat.startswith("**/") and (fnmatch.fnmatch(norm, pat[3:]) or fnmatch.fnmatch(base, pat[3:])):
            return True
    return False


def matches_target_paths(df: DiffFile, target_paths: list[str] | None) -> bool:
    """Filter entries by user `--paths` AFTER full-repo rename and secret resolution (F36, F42)."""
    if not target_paths:
        return True
    candidates = [df.path] + ([df.old_path] if df.old_path else [])
    for raw_t in target_paths:
        t = normalize_rel_path(raw_t).rstrip("/")
        if t in {"", "."}:
            return True
        for c in candidates:
            if c == t or c.startswith(t + "/") or fnmatch.fnmatch(c, t):
                return True
    return False


def classify_exclusion(
    df: DiffFile,
    target_paths: list[str] | None = None,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    exclude_tests: bool = False,
    extra_temp_prefixes: list[str] | None = None,
) -> str:
    """Apply deterministic 6-gate filtering.

    Hard gates (binary, secret on old/new path, temp) always run first so cross-scope
    renames from secret paths are caught before `--paths` filtering (F36).
    """
    if df.is_binary:
        return "binary"
    if is_secret_path(df.path) or is_secret_path(df.old_path):
        return "secret_exclude"
    if is_temp_artifact_path(df.path, extra_temp_prefixes) or is_temp_artifact_path(
        df.old_path, extra_temp_prefixes
    ):
        return "temp_artifact"
    if not matches_target_paths(df, target_paths):
        return "outside_target_paths"
    if exclude_globs and (
        matches_any_glob(df.path, exclude_globs)
        or (df.old_path and matches_any_glob(df.old_path, exclude_globs))
    ):
        return "user_exclude"
    if include_globs and not (
        matches_any_glob(df.path, include_globs)
        or (df.old_path and matches_any_glob(df.old_path, include_globs))
    ):
        return "not_in_include"

    norm = normalize_git_path(df.path)
    parts = [seg for seg in norm.split("/") if seg]
    base = parts[-1] if parts else ""
    _, ext = posixpath.splitext(base.lower())

    if ext in BINARY_EXTENSIONS:
        return "binary"
    if any(seg in NOISE_DIR_SEGMENTS for seg in parts[:-1]):
        return "noise_directory"
    if base in NOISE_BASENAMES:
        return "lockfile"
    lower_norm = norm.lower()
    if lower_norm.endswith(GENERATED_SUFFIXES) or ".generated." in lower_norm:
        return "generated_file"
    if exclude_tests and (
        any(seg in {"test", "tests", "__tests__", "testdata", "fixtures"} for seg in parts[:-1])
        or any(fnmatch.fnmatch(base, pat) for pat in TEST_PATTERNS)
    ):
        return "test_excluded"
    return ""


def parse_raw_z_inventory(raw_z: str) -> list[DiffFile]:
    """Parse `git diff --raw -z --no-abbrev --find-renames` output preserving exact filenames (F03)."""
    if not raw_z:
        return []
    tokens = raw_z.split("\x00")
    entries: list[DiffFile] = []
    idx = 0
    total = len(tokens)
    while idx < total:
        meta = tokens[idx]
        idx += 1
        if not meta or not meta.startswith(":"):
            continue
        parts = meta[1:].split(" ")
        if len(parts) < 5:
            continue
        old_mode, new_mode, _, _, status_token = parts[:5]
        code = status_token[0].upper()
        if code in {"R", "C"}:
            if idx + 1 >= total:
                break
            old_p = normalize_git_path(tokens[idx])
            new_p = normalize_git_path(tokens[idx + 1])
            idx += 2
            status = "RENAMED" if code == "R" else "ADDED"
        else:
            if idx >= total:
                break
            new_p = normalize_git_path(tokens[idx])
            old_p = ""
            idx += 1
            status = {
                "A": "ADDED",
                "D": "DELETED",
                "M": "MODIFIED",
                "T": "MODIFIED",
            }.get(code, "MODIFIED")

        is_symlink = old_mode == "120000" if status == "DELETED" else new_mode == "120000"
        entries.append(
            DiffFile(
                path=new_p,
                old_path=old_p if old_p != new_p else "",
                status=status,
                is_symlink=is_symlink,
            )
        )
    return entries


def _populate_patch_stats(df: DiffFile, patch_text: str) -> None:
    """Count additions/deletions strictly inside hunks so `++n`/`--n` lines are never skipped (F41)."""
    if not patch_text.strip():
        df.patch = ""
        return
    additions = 0
    deletions = 0
    is_bin = False
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            is_bin = True
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
    df.is_binary = is_bin
    df.additions = additions
    df.deletions = deletions
    df.patch = patch_text.rstrip("\r\n") + "\n"


def populate_git_patch_for_entry(
    repo_root: Path,
    diff_base_args: list[str],
    df: DiffFile,
) -> None:
    path_args = [df.old_path, df.path] if df.old_path else [df.path]
    patch = run_git(
        repo_root,
        [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--find-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "-U3",
            *diff_base_args,
            "--",
            *path_args,
        ],
        literal_pathspecs=True,
    ).stdout
    _populate_patch_stats(df, patch)
    if df.is_binary:
        df.exclude_reason = "binary"


def _stat_worktree_entry(full_path: Path, rel_label: str) -> os.stat_result:
    try:
        return os.lstat(full_path)
    except OSError as exc:
        raise RuntimeError(f"Failed to stat reviewable file {rel_label!r}: {exc}") from exc


def synthesize_untracked_diff(repo_root: Path, df: DiffFile, base_text: str | None = None) -> None:
    """Populate patch for an untracked (or staged-deleted then recreated) file without following symlinks."""
    norm = df.path
    full_path = repo_root / norm
    st = _stat_worktree_entry(full_path, norm)

    if stat.S_ISLNK(st.st_mode):
        df.is_symlink = True
        try:
            target = normalize_git_path(os.readlink(full_path))
        except OSError as exc:
            raise RuntimeError(f"Failed to read symlink {norm!r}: {exc}") from exc
        if is_secret_path(target):
            df.exclude_reason = "secret_exclude"
            return
        df.additions = 1
        df.patch = (
            f"diff --git a/{norm} b/{norm}\n"
            "new file mode 120000\n"
            "--- /dev/null\n"
            f"+++ b/{norm}\n"
            "@@ -0,0 +1,1 @@\n"
            f"+{target}\n"
        )
        return

    if not stat.S_ISREG(st.st_mode):
        df.exclude_reason = "non_regular_file"
        return

    try:
        with full_path.open("rb") as fh:
            prefix = fh.read(8192)
            if b"\x00" in prefix:
                df.is_binary = True
                df.exclude_reason = "binary"
                return
            rest = fh.read()
    except OSError as exc:
        raise RuntimeError(f"Failed to read reviewable file {norm!r}: {exc}") from exc

    raw = prefix + rest
    text = raw.decode("utf-8", errors="replace")
    new_lines = text.splitlines()

    if base_text is not None:
        # Staged deletion followed by untracked recreation (F35): diff base blob against live file.
        old_lines = base_text.splitlines()
        diff_iter = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{norm}",
            tofile=f"b/{norm}",
            n=3,
            lineterm="",
        )
        body = list(diff_iter)
        df.status = "MODIFIED"
        if not body:
            df.patch = ""
            df.exclude_reason = "unchanged"
            return
        full_patch = "\n".join([f"diff --git a/{norm} b/{norm}", *body]) + "\n"
        _populate_patch_stats(df, full_patch)
        return

    df.additions = len(new_lines)
    header = [
        f"diff --git a/{norm} b/{norm}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{norm}",
    ]
    if new_lines:
        header.append(f"@@ -0,0 +1,{len(new_lines)} @@")
        header.extend(f"+{ln}" for ln in new_lines)
    df.patch = "\n".join(header) + "\n"


def _read_repo_doc(repo_root: Path, rel_path: str, content_ref: str = "") -> str:
    if content_ref:
        res = run_git(repo_root, ["show", f"{content_ref}:{rel_path}"], check=False)
        return res.stdout if res.returncode == 0 else ""
    p = repo_root / rel_path
    if p.is_symlink() or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def discover_project_rules(
    repo_root: Path,
    selected_paths: list[str],
    content_ref: str = "",
) -> str:
    """Collect concise project conventions from target ref (commit/range) or workspace."""
    chunks: list[str] = []
    ocr_raw = _read_repo_doc(repo_root, ".opencodereview/rule.json", content_ref)
    if ocr_raw.strip():
        try:
            data = json.loads(ocr_raw)
            rules = data.get("rules", []) if isinstance(data, dict) else []
            matched: list[str] = []
            for item in rules:
                if not isinstance(item, dict):
                    continue
                pat = str(item.get("path", "")).strip()
                body = str(item.get("rule", "")).strip()
                if not body:
                    continue
                if not pat or not selected_paths or any(matches_any_glob(p, [pat]) for p in selected_paths):
                    matched.append(f"- [{pat or '*'}]: {body}")
            if matched:
                chunks.append("### .opencodereview/rule.json\n" + "\n".join(matched))
        except json.JSONDecodeError:
            pass

    for doc_name in ("AGENTS.md", "GEMINI.md", "CLAUDE.md"):
        content = _read_repo_doc(repo_root, doc_name, content_ref).strip()
        if content:
            excerpt = content[:3000] + ("\n...(truncated)" if len(content) > 3000 else "")
            chunks.append(f"### {doc_name}\n{excerpt}")
            break
    return "\n\n".join(chunks)


def validate_git_ref(ref: str, label: str) -> str:
    cleaned = ref.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be empty")
    if cleaned.startswith("-"):
        raise ValueError(f"Invalid {label} {cleaned!r}: cannot start with '-'")
    return cleaned


def _escape_gitignore_literal(rel_prefix: str) -> str:
    out = []
    for ch in rel_prefix:
        if ch in "\\*?[]!#":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _compute_extra_temp_prefixes(repo_root: Path, extra_exclude_dirs: list[Path] | None) -> list[str]:
    prefixes: list[str] = []
    root_resolved = repo_root.resolve()
    for d in extra_exclude_dirs or []:
        d_res = d.resolve()
        if d_res == root_resolved:
            raise ValueError(
                f"artifact_dir ({d_res}) cannot be the repository root itself ({root_resolved})"
            )
        try:
            rel = d_res.relative_to(root_resolved)
            rel_str = normalize_git_path(str(rel))
            if rel_str and rel_str != ".":
                prefixes.append(rel_str)
        except ValueError:
            continue
    return prefixes


def collect_diff_selection(
    repo_dir: str | Path,
    from_ref: str = "",
    to_ref: str = "",
    commit_ref: str = "",
    direct_base_ref: str = "",
    direct_head_ref: str = "",
    workspace_base_ref: str = "",
    target_paths: list[str] | None = None,
    include_globs: list[str] | None = None,
    exclude_globs: list[str] | None = None,
    exclude_tests: bool = False,
    extra_exclude_dirs: list[Path] | None = None,
) -> SelectionResult:
    repo_root = resolve_git_root(repo_dir)
    if repo_root is None:
        raise RuntimeError(f"Not inside a Git repository: {repo_dir}")

    if commit_ref and (from_ref or to_ref):
        raise ValueError("--commit cannot be combined with --from/--to")
    if to_ref and not from_ref:
        raise ValueError("--to requires --from")

    extra_temp_prefixes = _compute_extra_temp_prefixes(repo_root, extra_exclude_dirs)
    clean_targets = [normalize_rel_path(p) for p in (target_paths or []) if p.strip()]
    raw_flags = ["--raw", "-z", "--no-abbrev", "--find-renames"]

    if direct_head_ref:
        # Cumulative resume comparison from Round 1's exact base to a new target commit (F09/F31).
        h_ref = validate_git_ref(direct_head_ref, "target_commit")
        head_sha = run_git(repo_root, ["rev-parse", "--verify", f"{h_ref}^{{commit}}"]).stdout.strip()
        if direct_base_ref in {"", "EMPTY_TREE"}:
            base_sha = ""
            base_is_empty = True
            diff_target = [f"{empty_tree_hash(repo_root)}..{head_sha}"]
        else:
            b_ref = validate_git_ref(direct_base_ref, "initial_base_ref")
            base_sha = run_git(repo_root, ["rev-parse", "--verify", f"{b_ref}^{{commit}}"]).stdout.strip()
            base_is_empty = False
            diff_target = [f"{base_sha}..{head_sha}"]

        raw_inv = run_git(repo_root, ["diff", *raw_flags, *diff_target]).stdout
        entries = parse_raw_z_inventory(raw_inv)
        for df in entries:
            df.exclude_reason = classify_exclusion(
                df, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
            )
            if df.selected:
                populate_git_patch_for_entry(repo_root, diff_target, df)

        result = SelectionResult(
            repo_dir=str(repo_root),
            mode="direct_range",
            resolved_base=base_sha,
            resolved_head=head_sha,
            base_is_empty=base_is_empty,
            files=[e for e in entries if e.exclude_reason != "outside_target_paths"],
        )
        rules_ref = head_sha

    elif commit_ref:
        c_ref = validate_git_ref(commit_ref, "--commit")
        head_sha = run_git(repo_root, ["rev-parse", "--verify", f"{c_ref}^{{commit}}"]).stdout.strip()
        parents_line = run_git(repo_root, ["rev-list", "--parents", "-n", "1", head_sha]).stdout.strip().split()
        if len(parents_line) > 1:
            parent_sha = parents_line[1]
            verify_parent = run_git(repo_root, ["rev-parse", "--verify", f"{parent_sha}^{{commit}}"], check=False)
            if verify_parent.returncode != 0:
                raise RuntimeError(
                    f"Commit {head_sha} is on a shallow clone boundary (missing parent {parent_sha}); deepen git history first."
                )
            base_sha = verify_parent.stdout.strip()
            base_is_empty = False
            diff_target = [f"{base_sha}..{head_sha}"]
        else:
            base_sha = ""
            base_is_empty = True
            diff_target = [f"{empty_tree_hash(repo_root)}..{head_sha}"]

        # Full-repo inventory first so cross-scope secret renames retain old_path (F36).
        raw_inv = run_git(repo_root, ["diff", *raw_flags, *diff_target]).stdout
        entries = parse_raw_z_inventory(raw_inv)
        for df in entries:
            df.exclude_reason = classify_exclusion(
                df, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
            )
            if df.selected:
                populate_git_patch_for_entry(repo_root, diff_target, df)

        result = SelectionResult(
            repo_dir=str(repo_root),
            mode="commit",
            resolved_base=base_sha,
            resolved_head=head_sha,
            base_is_empty=base_is_empty,
            files=[e for e in entries if e.exclude_reason != "outside_target_paths"],
        )
        rules_ref = head_sha

    elif from_ref:
        f_ref = validate_git_ref(from_ref, "--from")
        t_ref = validate_git_ref(to_ref or "HEAD", "--to")
        head_sha = run_git(repo_root, ["rev-parse", "--verify", f"{t_ref}^{{commit}}"]).stdout.strip()
        mb_sha = run_git(repo_root, ["merge-base", f_ref, head_sha]).stdout.strip()
        diff_target = [f"{mb_sha}..{head_sha}"]

        raw_inv = run_git(repo_root, ["diff", *raw_flags, *diff_target]).stdout
        entries = parse_raw_z_inventory(raw_inv)
        for df in entries:
            df.exclude_reason = classify_exclusion(
                df, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
            )
            if df.selected:
                populate_git_patch_for_entry(repo_root, diff_target, df)

        result = SelectionResult(
            repo_dir=str(repo_root),
            mode="range",
            resolved_base=mb_sha,
            resolved_head=head_sha,
            merge_base=mb_sha,
            files=[e for e in entries if e.exclude_reason != "outside_target_paths"],
        )
        rules_ref = head_sha

    else:
        mode_name = "workspace"
        base_is_empty = False
        if workspace_base_ref:
            mode_name = "workspace_from_base"
            if workspace_base_ref == "EMPTY_TREE":
                base_sha = ""
                base_is_empty = True
                compare_ref = empty_tree_hash(repo_root)
            else:
                b_ref = validate_git_ref(workspace_base_ref, "workspace_base_ref")
                base_sha = run_git(repo_root, ["rev-parse", "--verify", f"{b_ref}^{{commit}}"]).stdout.strip()
                compare_ref = base_sha
        else:
            head_res = run_git(repo_root, ["rev-parse", "--verify", "HEAD"], check=False)
            if head_res.returncode == 0:
                base_sha = head_res.stdout.strip()
                compare_ref = base_sha
            else:
                base_sha = ""
                base_is_empty = True
                compare_ref = empty_tree_hash(repo_root)

        diff_target = [compare_ref]
        raw_inv = run_git(repo_root, ["diff", *raw_flags, *diff_target]).stdout
        entries = parse_raw_z_inventory(raw_inv)
        by_path: dict[str, DiffFile] = {}
        for df in entries:
            df.exclude_reason = classify_exclusion(
                df, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
            )
            if df.selected:
                populate_git_patch_for_entry(repo_root, diff_target, df)
            by_path[df.path] = df

        ls_exclude_args = ["-x", ".*-temp", "-x", ".codex-review"]
        for pref in extra_temp_prefixes:
            ls_exclude_args.extend(["-x", f"/{_escape_gitignore_literal(pref)}"])
        ls_res = run_git(
            repo_root,
            ["ls-files", "--others", "--exclude-standard", *ls_exclude_args, "-z"],
        )
        if "warning: could not open directory" in ls_res.stderr or "failed to open dir" in ls_res.stderr:
            raise RuntimeError(
                f"Incomplete untracked file enumeration: {ls_res.stderr.strip()}"
            )

        for rel in ls_res.stdout.split("\x00"):
            rel_norm = normalize_git_path(rel)
            if not rel_norm or is_temp_artifact_path(rel_norm, extra_temp_prefixes):
                continue

            existing = by_path.get(rel_norm)
            if existing is not None:
                # If staged-deleted (`DELETED`) and recreated in worktree, merge against base blob (F35).
                if existing.status != "DELETED":
                    continue
                recreated = DiffFile(path=rel_norm, status="MODIFIED")
                recreated.exclude_reason = classify_exclusion(
                    recreated, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
                )
                if recreated.selected:
                    base_blob = ""
                    if base_sha:
                        b_res = run_git(repo_root, ["show", f"{base_sha}:{rel_norm}"], check=False)
                        if b_res.returncode == 0:
                            base_blob = b_res.stdout
                    synthesize_untracked_diff(repo_root, recreated, base_text=base_blob if base_sha else None)
                idx_pos = entries.index(existing)
                entries[idx_pos] = recreated
                by_path[rel_norm] = recreated
                continue

            df = DiffFile(path=rel_norm, status="ADDED")
            df.exclude_reason = classify_exclusion(
                df, clean_targets, include_globs, exclude_globs, exclude_tests, extra_temp_prefixes
            )
            if df.selected:
                synthesize_untracked_diff(repo_root, df)
            entries.append(df)
            by_path[rel_norm] = df

        visible_entries = [
            e for e in entries if e.exclude_reason not in {"outside_target_paths", "unchanged"}
        ]
        result = SelectionResult(
            repo_dir=str(repo_root),
            mode=mode_name,
            resolved_base=base_sha,
            base_is_empty=base_is_empty,
            files=visible_entries,
        )
        rules_ref = ""

    result.project_rules_summary = discover_project_rules(
        repo_root,
        [f.path for f in result.selected_files],
        content_ref=rules_ref,
    )
    return result

"""Bounded project sources and explicit decision lineage. Standard library only."""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import itertools
from pathlib import Path
import re
import subprocess
from dataclasses import dataclass, field

MAX_FILES = 1000
MAX_BYTES = 1_000_000
MAX_TOTAL_BYTES = 16_000_000
MAX_ENTRIES = 10000
MAX_DEPTH = 8
STATES = {"accepted", "proposed", "rejected", "superseded", "needs-review"}
EXPLICIT_ANCHOR = re.compile(r"\s*\{#([\w.-]+)\}\s*$")
SKIP = {"node_modules", ".git", ".venv", "__pycache__", "dist", "build", "vendor"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def packed(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_path(root: Path, relative: str) -> Path:
    if not relative or len(relative) > 512 or re.search(r"[:\x00-\x1f]", relative):
        raise ValueError("invalid-path")
    relative = relative.replace("\\", "/")
    if relative.startswith(("/", "~")) or ".." in relative.split("/"):
        raise ValueError("outside-project")
    result = (root / relative).resolve()
    if not result.is_relative_to(root):
        raise ValueError("outside-project")
    return result


def read_text(path: Path, cap: int = MAX_BYTES) -> str:
    with path.open("rb") as stream:
        content = stream.read(cap + 1)
    if len(content) > cap:
        raise ValueError("source-too-large")
    return content.decode("utf-8-sig")


def marks(text: str) -> dict:
    """Only top-level metadata in an initial frontmatter/paragraph is meaningful."""
    out = {}
    lines = text.lstrip("\ufeff\r\n").splitlines()[:40]
    frontmatter = bool(lines and lines[0] == "---")
    if frontmatter and "---" not in lines[1:]:
        return {"fault": "malformed-frontmatter"}
    for line in lines[1:] if frontmatter else lines:
        if line == "---" and frontmatter:
            break
        if not line.strip():
            if frontmatter:
                continue
            break
        if line[:1].isspace():
            continue
        match = re.fullmatch(r"(status|superseded_by):\s*(.*)", line)
        if match:
            key, value = match.groups()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value or len(value) > 512:
                out["fault"] = "invalid-metadata-value"
            if key in out:
                out["fault"] = "duplicate-metadata"
            out[key] = value
        elif not frontmatter:
            break
    if "status" in out and out["status"] not in STATES:
        out["fault"] = "invalid-status"
    return out


def anchor(title: str) -> str:
    explicit = EXPLICIT_ANCHOR.search(title)
    if explicit:
        return explicit[1]
    return re.sub(r"[^\w-]+", "-", title.casefold()).strip("-")


@dataclass
class Note:
    path: str
    section: str
    title: str
    body: str
    line: int
    fingerprint: str
    metadata: dict = field(default_factory=dict)
    searchable: bool = True
    parent: str | None = None
    search_body: str | None = None

    @property
    def key(self) -> str:
        return self.path + ("#" + self.section if self.section else "")


class Store:
    def __init__(self, project: str, sources=()):
        self.root = Path(project).resolve()
        if not self.root.is_dir():
            raise ValueError("project-missing")
        self.project_id = digest(os.path.normcase(str(self.root)))
        self.warnings: list[str] = []
        self.nodes: dict[str, Note] = {}
        self.ambiguous: set[str] = set()
        self.documents: dict[str, str] = {}
        self._discover(sources)

    def warn(self, code: str):
        if code not in self.warnings:
            self.warnings.append(code)

    def is_archive(self, path: Path | str) -> bool:
        """Raw documentation archives are evidence, separate from completed plans."""
        path = Path(path)
        parts = (path.relative_to(self.root) if path.is_absolute() else path).parts
        return (len(parts) >= 2 and parts[0].casefold() in {"docs", "doc"}
                and parts[1].casefold() == "archive")

    def _discover(self, sources):
        sources = tuple(sources)
        roots = list(itertools.islice(self.root.iterdir(), MAX_ENTRIES + 1))
        if len(roots) > MAX_ENTRIES:
            self.warn("scan-truncated")
        defaults = [p.name for p in roots[:MAX_ENTRIES] if p.is_file() and
                    (p.name.casefold() in {"agents.md", "claude.md"} or
                     p.name.casefold().startswith("readme") and p.suffix.lower() == ".md")]
        defaults += [p.name for p in roots[:MAX_ENTRIES] if p.is_dir() and p.name.casefold() in {"docs", "doc"}]
        defaults += [".trellis/spec", ".trellis/research", ".trellis/workspace"]
        pending = []
        explicit = []
        for rel in sources:
            try:
                explicit.append(safe_path(self.root, rel))
            except (OSError, ValueError):
                self.warn("source-outside-project")

        def priority(path):
            return next((i for i, source in enumerate(explicit)
                         if path == source or path.is_relative_to(source)), len(explicit))

        # Selecting docs/ or the project root is not an archive opt-in. Require
        # an explicit archive directory/file and prune before consuming budgets.
        archive_sources = [p for p in explicit if self.is_archive(p)]

        def included(path):
            return not self.is_archive(path) or any(
                path == source or path.is_relative_to(source) for source in archive_sources)

        for rel in dict.fromkeys([*sources, *defaults]):
            try:
                path = safe_path(self.root, rel)
                if path.exists() and included(path):
                    heapq.heappush(pending, (priority(path), 0, path))
                elif not path.exists() and rel in sources:
                    self.warn("source-missing")
            except (OSError, ValueError):
                self.warn("source-outside-project")
        candidates: set[Path] = set()
        visited: set[Path] = set()
        entries = 0
        while pending:
            _, depth, path = heapq.heappop(pending)
            if path in visited or not included(path):
                continue
            visited.add(path)
            entries += 1
            if entries > MAX_ENTRIES:
                self.warn("scan-truncated")
                break
            try:
                if path.is_dir():
                    if depth >= MAX_DEPTH:
                        self.warn("scan-depth-limit")
                        continue
                    children = list(itertools.islice(path.iterdir(), MAX_ENTRIES + 1))
                    if len(children) > MAX_ENTRIES:
                        self.warn("scan-truncated")
                    for child in sorted(children[:MAX_ENTRIES]):
                        if child.name in SKIP or child.name.startswith("."):
                            continue
                        if not included(child):
                            continue
                        if child.is_symlink() and child.is_dir():
                            continue
                        resolved = child.resolve()
                        if resolved.is_relative_to(self.root):
                            if included(resolved):
                                heapq.heappush(pending, (priority(resolved), depth + 1, resolved))
                        else:
                            self.warn("source-outside-project")
                elif path.suffix.casefold() in {".md", ".mdx"}:
                    candidates.add(path)
            except OSError:
                self.warn("source-unreadable")
        relative = [p.relative_to(self.root).as_posix()
                    for p in sorted(candidates, key=lambda p: (priority(p), p))]
        ignored = set()
        try:
            check = subprocess.run(["git", "check-ignore", "--stdin", "-z"],
                                   input="\0".join(relative) + "\0", cwd=self.root,
                                   text=True, encoding="utf-8", capture_output=True, timeout=5)
            if check.returncode in (0, 1):
                ignored = set(check.stdout.split("\0"))
        except (OSError, subprocess.TimeoutExpired):
            self.warn("git-ignore-unavailable")
        eligible = [rel for rel in relative if rel not in ignored]
        if len(eligible) > MAX_FILES:
            self.warn("scan-truncated")
        total = 0
        for rel in eligible[:MAX_FILES]:
            try:
                content = read_text(safe_path(self.root, rel))
                total += len(content.encode("utf-8"))
                if total > MAX_TOTAL_BYTES:
                    self.warn("scan-byte-limit")
                    break
                self.documents[rel] = content
                self._parse(rel, content)
            except (OSError, UnicodeError, ValueError) as exc:
                self.warn(str(exc) if isinstance(exc, ValueError) else "source-unreadable")

    def _parse(self, rel: str, text: str):
        lines = text.splitlines(keepends=True)
        fingerprint = digest(text)
        headings = []
        fence = None
        front_end = 0
        if lines and lines[0].lstrip("\ufeff").strip() == "---":
            front_end = next((i + 1 for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        for index, line in enumerate(lines):
            if index < front_end:
                continue
            marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
                continue
            match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
            if match and fence is None:
                headings.append((index, match[2], len(match[1])))
        file_marks = marks(text)
        title = headings[0][1] if headings else rel
        prefix = "".join(lines[front_end:headings[0][0]]) if headings else text
        root = Note(rel, "", title, text, 1, fingerprint, file_marks,
                    len(headings) <= 1 or bool(prefix.strip()), search_body=prefix)
        self.nodes[rel] = root
        if len(headings) <= 1:
            # A single heading still has an addressable decision anchor.
            if headings:
                index, title, _ = headings[0]
                body = "".join(lines[index + 1:])
                local = marks(body)
                root.search_body = prefix + body
                root.metadata = self._inherit(file_marks, local)
                self.nodes[rel + "#" + anchor(title)] = Note(
                    rel, anchor(title), title, body, index + 1, fingerprint, local, False, parent=rel)
            return
        # Search each heading's own prose; expand its subtree when reading a ref.
        ends, stack = {}, []
        for index, _, level in reversed(headings):
            while stack and stack[-1][1] > level:
                stack.pop()
            ends[index] = stack[-1][0] if stack else len(lines)
            stack.append((index, level))
        parents = []
        # Ordinary headings can repeat under different topics. Allocate distinct
        # inferred IDs while reserving explicitly named decision targets.
        reserved = {anchor(title) for _, title, _ in headings if EXPLICIT_ANCHOR.search(title)}
        used = set()
        suffixes = {}
        for pos, (index, title, level) in enumerate(headings):
            direct_end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
            own_body = "".join(lines[index + 1:direct_end])
            body = "".join(lines[index + 1:ends[index]])
            while parents and parents[-1][0] >= level:
                parents.pop()
            parent = parents[-1][1] if parents else rel
            section = base = anchor(title)
            if not EXPLICIT_ANCHOR.search(title):
                suffix = suffixes.get(base, 1)
                while section in used or section in reserved:
                    section = f"{base}-{suffix}"
                    suffix += 1
                suffixes[base] = suffix
            used.add(section)
            note = Note(rel, section, title, body, index + 1, fingerprint, marks(own_body),
                        parent=parent, search_body=own_body)
            if note.key in self.nodes:
                self.ambiguous.add(note.key)
                self.warn("duplicate-section-id")
            else:
                self.nodes[note.key] = note
            parents.append((level, note.key))

    @staticmethod
    def _inherit(parent: dict, local: dict) -> dict:
        if parent.get("superseded_by") or parent.get("status") in {"superseded", "rejected", "needs-review"} or parent.get("fault"):
            return dict(parent)
        return {**parent, **local}

    def metadata(self, note: Note) -> dict:
        if note.key in self.ambiguous:
            return {"fault": "duplicate-section-id"}
        if not note.parent:
            return note.metadata
        return self._inherit(self.metadata(self.nodes[note.parent]), note.metadata)

    def resolve(self, note: Note) -> tuple[str, Note | None, str | None]:
        """Return original status, final successor, and fault; faults require review."""
        start = note
        seen = {note.key}
        for hop in range(9):
            meta = self.metadata(note)
            if note.key in self.ambiguous or meta.get("fault"):
                return "needs-review", None, meta.get("fault", "duplicate-section-id")
            target = meta.get("superseded_by")
            status = meta.get("status", "unmarked")
            if not target:
                if status == "superseded":
                    return "needs-review", None, "missing-target"
                if start.key != note.key:
                    if status in {"rejected", "proposed", "needs-review"}:
                        return "needs-review", None, "successor-unconfirmed"
                    return "superseded", note, None
                return status, None, None
            if hop >= 8:
                return "needs-review", None, "lineage-depth-limit"
            rel, separator, section = target.partition("#")
            try:
                resolved = safe_path(self.root, rel or note.path)
                if self.is_archive(resolved):
                    return "needs-review", None, "archived-successor"
                target_key = resolved.relative_to(self.root).as_posix()
                if separator:
                    target_key += "#" + section
                if target_key in seen:
                    return "needs-review", None, "lineage-cycle"
                successor = self.nodes.get(target_key)
                if successor is None:
                    return "needs-review", None, "missing-target"
                seen.add(target_key)
                note = successor
            except (OSError, ValueError):
                return "needs-review", None, "outside-project"
        raise AssertionError("bounded lineage loop")

    def transcript(self, rel: str) -> list[dict]:
        content = read_text(safe_path(self.root, rel), 4 * MAX_BYTES)
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        if not rows or not isinstance(rows[0], dict) or rows[0].get("project") is None:
            raise ValueError("transcript-project-missing")
        if not isinstance(rows[0]["project"], str) or Path(rows[0]["project"]).resolve() != self.root:
            raise ValueError("transcript-project-mismatch")
        for row in rows[1:]:
            if not isinstance(row, dict):
                raise ValueError("transcript-invalid")
            if row.get("kind") == "compact":
                continue
            if row.get("role") not in {"user", "assistant"} or not isinstance(row.get("text"), str):
                raise ValueError("transcript-invalid")
        return rows[1:]

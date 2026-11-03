#!/usr/bin/env python3
"""Project memory: bounded recall/read, recent dialogue, and event navigation."""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import unicodedata

from memory_store import Store, Note, digest, packed


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def terms(query: str) -> list[str]:
    return list(dict.fromkeys(re.findall(
        r"[\u3400-\u9fff]+|[a-z0-9_]+(?:[.-][a-z0-9_]+)*|[^\W\d_]+", normalize(query))))


def query_echo(text: str, query: str) -> bool:
    remainder = normalize(text)
    for term in sorted(terms(query), key=len, reverse=True):
        remainder = remainder.replace(term, " ")
    noise = {"what", "is", "are", "the", "a", "an", "please", "tell", "me", "about", "how",
             "does", "do", "can", "you", "explain", "规则", "请问", "是什么", "什么", "请", "告诉我", "怎样", "如何", "多少"}
    return all(word in noise for word in re.findall(r"\w+", remainder))


def score(query: str, title: str, body: str, path: str) -> tuple:
    words = terms(query)
    title, body, path = map(normalize, (title, body, path))
    identifiers = sum(bool(re.search(r"\d", w)) and w in body + title + path for w in words)
    body_hits = sum(w in body for w in words)
    title_hits = sum(w in title for w in words)
    path_hits = sum(w in path for w in words)
    # Chinese phrases retain whole-phrase matches; bigrams provide partial recall.
    pairs = {w[i:i+2] for w in words if re.fullmatch(r"[\u3400-\u9fff]+", w)
             for i in range(len(w) - 1)}
    partial = sum(p in body for p in pairs) / max(1, len(pairs))
    return identifiers, body_hits, title_hits, path_hits, partial


def location(text: str, query: str) -> int:
    hay = normalize(text)
    positions = [hay.find(w) for w in terms(query) if w in hay]
    if not positions:
        positions = [hay.find(w[i:i+2]) for w in terms(query) for i in range(len(w) - 1)
                     if w[i:i+2] in hay]
    # Character offsets remain a hint; fingerprints bind the complete source.
    return min(positions, default=0)


def excerpt(text: str, at: int, cap: int) -> str:
    if len(text) <= cap:
        return text
    start = max(0, min(at - cap // 3, len(text) - cap))
    return text[start:start + cap]


def encode_ref(data: dict) -> str:
    return "hr1:" + base64.urlsafe_b64encode(packed(data).encode()).decode().rstrip("=")


def decode_ref(ref: str, project_id: str) -> dict:
    if not ref.startswith("hr1:") or len(ref) > 8192:
        raise ValueError("ref-invalid")
    try:
        raw = ref[4:]
        value = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        if not isinstance(value, dict) or value.get("project") != project_id:
            raise ValueError("ref-project-mismatch")
        if value.get("kind") not in {"doc", "session"} or not isinstance(value.get("path"), str):
            raise ValueError("ref-invalid")
        if not isinstance(value.get("hash"), str) or not re.fullmatch(r"[0-9a-f]{24}", value["hash"]):
            raise ValueError("ref-invalid")
        if type(value.get("at")) is not int or value["at"] < 0:
            raise ValueError("ref-invalid")
        return value
    except (json.JSONDecodeError, UnicodeError, TypeError) as exc:
        raise ValueError("ref-invalid") from exc


def bounded(records: list[dict], key: str, budget: int, warnings=(), limit=30) -> dict:
    """Budget the entire serialized response, including metadata and warnings."""
    result = {key: [], "truncated": False, "warnings": list(dict.fromkeys(warnings))}
    while len(packed(result)) > budget and result["warnings"]:
        result["warnings"].pop()
        result["truncated"] = True
    for item in records:
        if len(result[key]) >= limit:
            result["truncated"] = True
            break
        item = dict(item)
        text = item.pop("_text", "")
        at = item.pop("_at", 0)
        cap = min(item.pop("_cap", len(text)), len(text))
        field = "excerpt" if key == "hits" else "text"
        item[field] = excerpt(text, at, cap)
        item["partial"] = cap < len(text)
        result[key].append(item)
        while len(packed(result)) > budget and cap > 0:
            cap = max(0, cap - max(1, len(packed(result)) - budget))
            item[field] = excerpt(text, at, cap)
            item["partial"] = cap < len(text)
            result["truncated"] = True
        if len(packed(result)) > budget or text and cap == 0:
            result[key].pop()
            result["truncated"] = True
    return result


def doc_record(store: Store, note: Note, query: str = "", full=False) -> dict:
    status, successor, fault = store.resolve(note)
    at = location(note.body, query)
    ref = encode_ref({"project": store.project_id, "kind": "doc", "path": note.path,
                      "section": note.section, "hash": note.fingerprint, "at": at})
    body = note.body if full or note.search_body is None else note.search_body
    result = {"path": note.path, "section": note.section, "line": note.line,
              "status": status, "ref": ref, "_text": body, "_at": location(body, query), "_cap": 400}
    if store.is_archive(note.path):
        result["provenance"] = "archive"
    if successor:
        result["superseded_by"] = successor.key
    if fault:
        result["fault"] = fault
    return result


def window_hash(rows: list[dict], index: int) -> str:
    return digest(packed(rows[max(0, index - 1):index + 2]))


def session_record(store: Store, path: str, rows: list[dict], index: int, query="", at=None) -> dict:
    row = rows[index]
    at = location(row["text"], query) if at is None else at
    return {"path": path, "turn": index, "role": row["role"], "status": "dialogue",
            "ref": encode_ref({"project": store.project_id, "kind": "session", "path": path,
                               "turn": index, "hash": window_hash(rows, index), "at": at}),
            "_text": row["text"], "_at": at, "_cap": 400}


def recall(store: Store, query: str, budget=3000, limit=3, transcript=None) -> dict:
    if not query.strip() or len(query) > 2000:
        raise ValueError("query-length")
    candidates: dict[str, tuple[tuple, dict]] = {}
    states = {"accepted": 4, "unmarked": 3, "dialogue": 2, "proposed": 1,
              "rejected": 0, "superseded": -1, "needs-review": -2}

    def add(key, relevance, record):
        ranking = (*relevance, states[record["status"]], record.get("role") == "user", record.get("turn", -1))
        if key not in candidates or ranking > candidates[key][0]:
            candidates[key] = (ranking, record)

    for note in store.nodes.values():
        if not note.searchable:
            continue
        relevance = score(query, note.title, note.search_body if note.search_body is not None else note.body, note.key)
        if not any(relevance):
            continue
        record = doc_record(store, note, query)
        add(note.key, relevance, record)
        status, successor, fault = store.resolve(note)
        if successor and not fault:
            # Carry the predecessor's relevance so renamed current rules survive limit=1.
            current = doc_record(store, successor, query, full=True)
            current["replaces"] = note.key
            add(successor.key, relevance, current)
    warnings = list(store.warnings)
    if transcript:
        try:
            rows = store.transcript(transcript)
            for index, row in enumerate(rows):
                if row.get("kind") == "compact":
                    continue
                relevance = score(query, "", row["text"], "")
                if not any(relevance):
                    continue
                if row["role"] == "user" and query_echo(row["text"], query):
                    relevance = (0, 0, 0, 0, 0)
                add(f"session:{index}", relevance, session_record(store, transcript, rows, index, query))
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(str(exc) if isinstance(exc, ValueError) else "transcript-unreadable")
    ordered = [value[1] for _, value in sorted(candidates.items(), key=lambda item: item[1][0], reverse=True)]
    # Explicit relationships take precedence over truncation, including equal-score ties.
    for record in list(ordered):
        target = record.get("superseded_by")
        if target:
            at = next((i for i, r in enumerate(ordered)
                       if r["path"] + ("#" + r.get("section", "") if r.get("section") else "") == target), None)
            if at is not None and at > ordered.index(record):
                current = ordered.pop(at)
                ordered.insert(ordered.index(record), current)
    return bounded(ordered, "hits", budget, warnings, limit)


def read_refs(store: Store, refs: list[str], budget=4000, transcript=None) -> dict:
    records, warnings = [], list(store.warnings)
    if len(refs) > 30:
        warnings.append("refs-truncated")
    for ref in dict.fromkeys(refs[:30]):
        try:
            value = decode_ref(ref, store.project_id)
            if value["kind"] == "doc":
                section = value.get("section", "")
                if not isinstance(section, str):
                    raise ValueError("ref-invalid")
                key = value["path"] + ("#" + section if section else "")
                note = store.nodes.get(key)
                if not note:
                    raise ValueError("ref-missing")
                record = doc_record(store, note, full=True)
                changed = note.fingerprint != value["hash"]
            else:
                if not transcript or value["path"] != transcript:
                    raise ValueError("ref-source-required")
                rows = store.transcript(transcript)
                index = value.get("turn")
                if type(index) is not int or not 0 <= index < len(rows) or "text" not in rows[index]:
                    raise ValueError("ref-changed")
                record = session_record(store, transcript, rows, index)
                changed = window_hash(rows, index) != value["hash"]
                if not changed:
                    indices = [i for i in range(max(0, index - 1), min(len(rows), index + 2)) if "text" in rows[i]]
                    share = max(1, budget // 2 // len(indices))
                    fragments = []
                    for i in indices:
                        text = rows[i]["text"]
                        selected = excerpt(text, value["at"] if i == index else 0, share)
                        fragments.append(f"{rows[i]['role']} (turn {i}): {selected}")
                    record["_text"] = "\n\n".join(fragments)
                    record["context_partial"] = any(len(rows[i]["text"]) > share for i in indices)
            record["ref"] = ref
            record["changed"] = changed
            if changed:
                record["_text"] = ""
                record["error"] = "ref-changed"
            record["_at"] = value["at"] if value["kind"] == "doc" else 0
            record["_cap"] = budget
            records.append(record)
        except (OSError, ValueError, UnicodeError) as exc:
            warnings.append(str(exc) if isinstance(exc, ValueError) else "source-unreadable")
    return bounded(records, "reads", budget, warnings)


def context(store: Store, transcript: str, budget=4000, turns=6) -> dict:
    rows = store.transcript(transcript)
    dialogue = [i for i, row in enumerate(rows) if row.get("kind") != "compact" and row.get("role") in {"user", "assistant"}]
    indices = dialogue[-turns:]
    last_user = next((i for i in reversed(dialogue) if rows[i]["role"] == "user"), None)
    if last_user is not None and last_user not in indices:
        indices = [last_user, *indices[-(turns - 1):]] if turns > 1 else [last_user]
    records = []
    priority = sorted(indices, key=lambda i: (rows[i]["role"] == "user", i), reverse=True)
    for index in priority:
        record = session_record(store, transcript, rows, index,
                                at=len(rows[index]["text"]) if rows[index]["role"] == "user" else 0)
        record["_cap"] = max(256, (budget - 100) // max(1, len(indices)) - len(record["ref"]) - 120)
        records.append(record)
    result = bounded(records, "turns", budget, store.warnings)
    result["turns"].sort(key=lambda row: row["turn"])
    return result


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 100000:
        raise argparse.ArgumentTypeError("expected an integer in 1..100000")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=".")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--transcript")
    subs = parser.add_subparsers(dest="command", required=True)
    find = subs.add_parser("recall")
    find.add_argument("query")
    find.add_argument("--limit", type=positive, default=3)
    read = subs.add_parser("read")
    read.add_argument("refs", nargs="+")
    recent = subs.add_parser("context")
    recent.add_argument("--turns", type=positive, default=6)
    for sub, cap in ((find, 3000), (read, 4000), (recent, 4000)):
        sub.add_argument("--max-chars", type=positive, default=cap)
    entry = subs.add_parser("entry")
    entry.add_argument("--host", default="generic")
    entry.add_argument("--session")
    entry.add_argument("--event", choices=["startup", "compact", "clear", "resume", "fork"], default="startup")
    entry.add_argument("--plan")
    entry.add_argument("--state-dir")
    args = parser.parse_args()
    try:
        if args.command == "entry":
            from memory_entry import navigation
            result = navigation(args.project, args.host, args.session, args.event, args.plan, args.state_dir)
        else:
            if args.max_chars < 256:
                raise ValueError("budget-minimum-256")
            store = Store(args.project, args.source)
            if args.command == "recall":
                result = recall(store, args.query, args.max_chars, args.limit, args.transcript)
            elif args.command == "read":
                result = read_refs(store, args.refs, args.max_chars, args.transcript)
            elif args.transcript:
                result = context(store, args.transcript, args.max_chars, args.turns)
            else:
                raise ValueError("transcript-required")
        print(packed(result))
        return 0
    except (OSError, ValueError, UnicodeError) as exc:
        print(packed({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

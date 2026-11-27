"""Strict JSON Schemas for Codex structured review outputs."""

from __future__ import annotations

PLAN_CATEGORIES = [
    "architecture_flaw",
    "missing_requirement",
    "edge_case_gap",
    "contract_conflict",
    "over_engineering_risk",
    "verification_gap",
]

CODE_CATEGORIES = [
    "bug",
    "logic_edge_case",
    "concurrency_lifecycle",
    "security",
    "contract_mismatch",
    "missing_requirement",
    "over_defensive_bloat",
    "test_gap",
]


def build_review_schema(mode: str) -> dict:
    """Return a strict JSON Schema compatible with `codex exec --output-schema`."""
    categories = PLAN_CATEGORIES if mode == "plan" else CODE_CATEGORIES
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["pass", "needs_fix"],
            },
            "summary": {
                "type": "string",
            },
            "prior_findings_audit": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["fixed", "persisting", "partially_fixed"],
                        },
                        "note": {"type": "string"},
                    },
                    "required": ["id", "status", "note"],
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["P0", "P1", "P2", "P3"],
                        },
                        "category": {
                            "type": "string",
                            "enum": categories,
                        },
                        "file": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                        "snippet": {"type": "string"},
                        "title": {"type": "string"},
                        "problem": {"type": "string"},
                        "fix_direction": {"type": "string"},
                    },
                    "required": [
                        "id",
                        "severity",
                        "category",
                        "file",
                        "start_line",
                        "end_line",
                        "snippet",
                        "title",
                        "problem",
                        "fix_direction",
                    ],
                },
            },
            "suppressed_over_defensive_notes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "verdict",
            "summary",
            "prior_findings_audit",
            "findings",
            "suppressed_over_defensive_notes",
        ],
    }

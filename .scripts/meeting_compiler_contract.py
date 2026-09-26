#!/usr/bin/env python3
"""Backend-neutral Meeting Compiler protocol and deterministic validators."""
from __future__ import annotations

import hashlib
import json
import re


PROTOCOL_VERSION = "meeting-compiler-v2"
LEGACY_PROTOCOL_VERSION = "meeting-compiler-v1"
PREPROCESS_DELIMITER = "<<<PREPROCESS>>>"
WIKI_DELIMITER = "<<<WIKI>>>"
MEETING_IR_DELIMITER = "<<<MEETING_IR>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"
MAX_REPLACEMENTS = 64
MAX_ENTITY_DECISIONS = 128
MAX_MEETING_IR_ITEMS = 128

TYPED_MEETING_PREDICATES = {"参会", "汇报", "决策", "待办", "讨论", "规划"}
TOPIC_PREDICATES = {"讨论", "涉及", "规划"}
FREE_RELATION_PREDICATES = {"涉及", "紧密相关于", "指导", "师从", "受指导于"}
DEICTIC_ENDPOINTS = {"本会议", "本文", "本文件", "本文档", "本论文", "$meeting"}


def task_context_hash(source_text: str, entity_candidates: dict, *, meeting_id: str,
                      target_source_path: str, errors: list[str] | None = None) -> str:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "entity_candidates": entity_candidates,
        "meeting_id": meeting_id,
        "target_source_path": target_source_path,
        "errors": errors or [],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _section(text: str, delimiter: str, next_delimiter: str | None) -> str:
    start = text.find(delimiter)
    if start < 0:
        return ""
    start += len(delimiter)
    end = text.find(next_delimiter, start) if next_delimiter else len(text)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_meta(text: str) -> dict[str, str]:
    match = re.search(r"<<<META>>>\s*(.*?)\s*<<</META>>>", text, re.S)
    if not match:
        return {}
    values = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def _validate_preprocess_version(value, versions: set[str]) -> bool:
    if not isinstance(value, dict) or value.get("protocol_version") not in versions:
        return False
    replacements = value.get("transcript_replacements")
    decisions = value.get("entity_resolutions")
    if not isinstance(replacements, list) or len(replacements) > MAX_REPLACEMENTS:
        return False
    if not isinstance(decisions, list) or len(decisions) > MAX_ENTITY_DECISIONS:
        return False
    seen = set()
    for item in replacements:
        if not isinstance(item, dict):
            return False
        original = item.get("original")
        replacement = item.get("replacement")
        reason = item.get("reason")
        if not all(isinstance(part, str) for part in (original, replacement, reason)):
            return False
        if not original.strip() or not replacement.strip() or original == replacement:
            return False
        if "\n" in original or "\n" in replacement or len(original) > 80 or len(replacement) > 160:
            return False
        if original in seen:
            return False
        seen.add(original)
    for item in decisions:
        if not isinstance(item, dict):
            return False
        if set(item) != {"mention", "canonical", "status", "reason"}:
            return False
        if item.get("status") not in {"resolved", "unchanged", "unresolved"}:
            return False
        if not all(isinstance(item.get(key), str) for key in item):
            return False
        if not item["mention"].strip() or not item["reason"].strip():
            return False
        if item["status"] == "resolved" and not item["canonical"].strip():
            return False
    return True


def validate_preprocess(value) -> bool:
    return _validate_preprocess_version(value, {PROTOCOL_VERSION})


def _valid_evidence_ids(value, allowed: set[str] | None) -> bool:
    if not isinstance(value, list) or not value or len(value) > 16:
        return False
    ids = [str(item).strip() for item in value]
    if any(not item or not re.fullmatch(r"s\d{4}", item) for item in ids):
        return False
    if len(ids) != len(set(ids)):
        return False
    return allowed is None or set(ids) <= allowed


def validate_meeting_ir(value, evidence_ids: set[str] | None = None) -> list[str]:
    """Validate the typed, evidence-bound semantic result of Meeting Compiler v2."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["MEETING_IR must be an object"]
    required = {"protocol_version", "attendees", "topics", "reports", "decisions", "tasks", "relations"}
    if set(value) != required:
        errors.append("MEETING_IR fields must be exactly: " + ", ".join(sorted(required)))
        return errors
    if value.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"MEETING_IR protocol_version must be {PROTOCOL_VERSION}")

    specs = {
        "attendees": ({"person", "label", "evidence_ids"}, ("person", "label")),
        "topics": ({"label", "predicate", "evidence_ids"}, ("label", "predicate")),
        "reports": ({"person", "person_label", "topic", "evidence_ids"},
                    ("person", "person_label", "topic")),
        "decisions": ({"text", "evidence_ids"}, ("text",)),
        "tasks": ({"text", "assignee", "assignee_label", "evidence_ids"},
                  ("text", "assignee", "assignee_label")),
        "relations": ({"subject", "predicate", "object", "evidence_ids"},
                      ("subject", "predicate", "object")),
    }
    for section, (fields, text_fields) in specs.items():
        rows = value.get(section)
        if not isinstance(rows, list):
            errors.append(f"MEETING_IR.{section} must be a list")
            continue
        if len(rows) > MAX_MEETING_IR_ITEMS:
            errors.append(f"MEETING_IR.{section} exceeds {MAX_MEETING_IR_ITEMS} items")
            continue
        seen = set()
        for index, row in enumerate(rows):
            prefix = f"MEETING_IR.{section}[{index}]"
            if not isinstance(row, dict) or set(row) != fields:
                errors.append(f"{prefix} has invalid fields")
                continue
            values = tuple(str(row.get(field) or "").strip() for field in text_fields)
            if any(not item for item in values):
                errors.append(f"{prefix} has an empty required value")
            if any(item in DEICTIC_ENDPOINTS for item in values):
                errors.append(f"{prefix} contains an unresolved deictic endpoint")
            if not _valid_evidence_ids(row.get("evidence_ids"), evidence_ids):
                errors.append(f"{prefix}.evidence_ids are invalid or unbound")
            identity = values
            if identity in seen:
                errors.append(f"{prefix} duplicates an earlier item")
            seen.add(identity)
            if section == "topics" and row.get("predicate") not in TOPIC_PREDICATES:
                errors.append(f"{prefix}.predicate is not a topic predicate")
            if section == "relations":
                predicate = str(row.get("predicate") or "").strip()
                if predicate not in FREE_RELATION_PREDICATES:
                    errors.append(f"{prefix}.predicate overlaps typed slots or is unsupported")
                if predicate in TYPED_MEETING_PREDICATES:
                    errors.append(f"{prefix}.predicate must use a typed meeting section")
    return errors


def parse_proposal(text: str) -> tuple[dict | None, str]:
    proposal, error, _diagnostic = parse_proposal_detailed(text)
    return proposal, error


def parse_proposal_detailed(text: str) -> tuple[dict | None, str, dict]:
    preprocess_text = _section(text, PREPROCESS_DELIMITER, WIKI_DELIMITER)
    if not preprocess_text:
        error = f"missing {PREPROCESS_DELIMITER} section"
        return None, error, {"kind": "boundary", "stage": "parse_sections",
                             "segment": "PREPROCESS", "message": error}
    json_text = _strip_json_fence(preprocess_text)
    try:
        preprocess = json.loads(json_text)
    except json.JSONDecodeError as exc:
        excerpt_start = max(0, exc.pos - 100)
        return None, "invalid preprocess JSON", {
            "kind": "json_syntax", "stage": "parse_preprocess", "segment": "PREPROCESS",
            "error_type": type(exc).__name__, "message": exc.msg,
            "line": exc.lineno, "column": exc.colno, "position": exc.pos,
            "coordinate_space": "PREPROCESS after whitespace and JSON fence stripping",
            "excerpt_start": excerpt_start,
            "excerpt": json_text[excerpt_start:exc.pos + 100],
        }
    version = str(preprocess.get("protocol_version") or "") if isinstance(preprocess, dict) else ""
    if not _validate_preprocess_version(preprocess, {PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION}):
        error = "invalid meeting compiler preprocess proposal"
        return None, error, {"kind": "schema", "stage": "validate_preprocess",
                             "segment": "PREPROCESS", "message": error}
    semantic_delimiter = MEETING_IR_DELIMITER if version == PROTOCOL_VERSION else SLOTS_DELIMITER
    wiki = _section(text, WIKI_DELIMITER, semantic_delimiter)
    semantics = _section(text, semantic_delimiter, None)
    if not wiki:
        error = f"missing {WIKI_DELIMITER} section"
        return None, error, {"kind": "boundary", "stage": "parse_sections",
                             "segment": "WIKI", "message": error}
    if not semantics:
        error = f"missing {semantic_delimiter} section"
        return None, error, {"kind": "boundary", "stage": "parse_sections",
                             "segment": semantic_delimiter.strip("<>"), "message": error}
    proposal = {
        "protocol_version": PROTOCOL_VERSION,
        "preprocess": preprocess,
        "meta": _parse_meta(text),
        "wiki_markdown": wiki,
    }
    if version == LEGACY_PROTOCOL_VERSION:
        proposal["protocol_version"] = LEGACY_PROTOCOL_VERSION
        proposal["semantic_slots"] = semantics
        return proposal, "", {}
    ir_text = _strip_json_fence(semantics)
    try:
        meeting_ir = json.loads(ir_text)
    except json.JSONDecodeError as exc:
        excerpt_start = max(0, exc.pos - 100)
        return None, "invalid meeting IR JSON", {
            "kind": "json_syntax", "stage": "parse_meeting_ir", "segment": "MEETING_IR",
            "error_type": type(exc).__name__, "message": exc.msg,
            "line": exc.lineno, "column": exc.colno, "position": exc.pos,
            "coordinate_space": "MEETING_IR after whitespace and JSON fence stripping",
            "excerpt_start": excerpt_start,
            "excerpt": ir_text[excerpt_start:exc.pos + 100],
        }
    ir_errors = validate_meeting_ir(meeting_ir)
    if ir_errors:
        error = "invalid meeting-compiler-v2 meeting IR"
        return None, error, {"kind": "schema", "stage": "validate_meeting_ir",
                             "segment": "MEETING_IR", "message": error,
                             "issues": ir_errors}
    proposal["meeting_ir"] = meeting_ir
    return proposal, "", {}


def apply_transcript_replacements(source_text: str, replacements: list[dict]) -> str:
    """Apply exact replacements against the original text in one non-cascading pass."""
    if not replacements:
        return source_text
    preprocess = {
        "protocol_version": PROTOCOL_VERSION,
        "transcript_replacements": replacements,
        "entity_resolutions": [],
    }
    if not validate_preprocess(preprocess):
        raise ValueError("invalid transcript replacements")
    mapping = {item["original"]: item["replacement"] for item in replacements}
    missing = [original for original in mapping if original not in source_text]
    if missing:
        raise ValueError("replacement source text not found: " + ", ".join(missing))
    pattern = re.compile("|".join(re.escape(value) for value in sorted(mapping, key=len, reverse=True)))
    matched = set(pattern.findall(source_text))
    if matched != set(mapping):
        raise ValueError("overlapping transcript replacements are not allowed")
    return pattern.sub(lambda match: mapping[match.group(0)], source_text)

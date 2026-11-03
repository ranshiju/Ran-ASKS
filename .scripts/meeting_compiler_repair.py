"""Shared meeting diagnostics and scoped patch application; no I/O or model calls."""
from __future__ import annotations

import copy
import hashlib
import json
import re

import meeting_compiler_contract as contract
import student_guidance_projection as guidance

PATCH_VERSION = "meeting-ir-row-repair-v1"
MAX_REPAIR_ROWS = 16


def _json_section(text: str, delimiter: str, next_delimiter: str | None = None):
    return json.loads(contract._strip_json_fence(contract._section(text, delimiter, next_delimiter)))


def inspect_output(text: str, *, source_text: str | None = None,
                   evidence_catalog: list[dict] | None = None,
                   guidance_catalog: dict | None = None) -> tuple[dict | None, str, dict]:
    """Collect independent current-protocol errors before consuming a revision."""
    proposal, error, diagnostic = contract.parse_proposal_detailed(text)
    if source_text is None and evidence_catalog is None and guidance_catalog is None:
        return proposal, error, diagnostic
    try:
        preprocess = _json_section(text, contract.PREPROCESS_DELIMITER, contract.WIKI_DELIMITER)
        ir = _json_section(text, contract.MEETING_IR_DELIMITER)
    except (ValueError, TypeError):
        return proposal, error, diagnostic
    if not isinstance(preprocess, dict) or preprocess.get("protocol_version") != contract.PROTOCOL_VERSION:
        return proposal, error, diagnostic

    issues = list(diagnostic.get("issues") or ([error] if error else []))
    if source_text is not None and contract.validate_preprocess(preprocess):
        try:
            contract.apply_transcript_replacements(source_text, preprocess["transcript_replacements"])
        except ValueError as exc:
            issues.append("PREPROCESS.transcript_replacements: " + str(exc))
    ids = {row["evidence_id"] for row in evidence_catalog} if evidence_catalog is not None else None
    issues.extend(contract.validate_meeting_ir(ir, ids, evidence_catalog=evidence_catalog))
    if guidance_catalog is not None and isinstance(ir, dict):
        issues.extend(guidance.validate_person_updates(ir.get("person_updates"), guidance_catalog))
    issues = list(dict.fromkeys(issues))
    if issues:
        reason = error or "invalid meeting-compiler-v3 meeting IR"
        return None, reason, {
            "kind": "schema", "stage": "validate_candidate", "segment": "CANDIDATE",
            "message": reason, "issues": issues,
        }
    return proposal, error, diagnostic


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_sha(row: dict) -> str:
    return _sha(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _unmatched_student_projection(row: dict, catalog: dict | None) -> bool:
    """Omit ineligible projections only with a valid catalog and no identity match.

    A key, page or label match is protective, not an identity resolution: such a
    record must be corrected instead of being removed to pass validation.
    """
    if (not isinstance(catalog, dict) or catalog.get("protocol_version") != guidance.CANDIDATE_VERSION
            or not isinstance(catalog.get("students"), list)):
        return False
    person, label = row.get("person"), row.get("person_label")
    if not isinstance(person, str) or not isinstance(label, str) or not person.strip() or not label.strip():
        return False
    person, label = person.strip(), label.strip()
    for student in catalog["students"]:
        if (not isinstance(student, dict)
                or not isinstance(student.get("student_key"), str) or not student["student_key"].strip()
                or not isinstance(student.get("label"), str) or not student["label"].strip()
                or not isinstance(student.get("people_page", ""), str)):
            return False
        key = student["student_key"].strip()
        page = student.get("people_page", "").strip().removesuffix(".md")
        if (person == key or person.removesuffix(".md").rsplit("/", 1)[-1] == key
                or (page and person.removesuffix(".md") == page)
                or label == student["label"].strip()):
            return False
    return True


def prepare_row_repair(text: str, diagnostic: dict, *, guidance_catalog: dict | None = None) -> dict | None:
    """Select only specifically diagnosed IR rows; all other errors use full recovery."""
    if diagnostic.get("stage") != "validate_candidate" or not diagnostic.get("issues"):
        return None
    try:
        ir = _json_section(text, contract.MEETING_IR_DELIMITER)
    except (ValueError, TypeError):
        return None
    if not isinstance(ir, dict) or ir.get("protocol_version") != contract.PROTOCOL_VERSION:
        return None
    grouped = {}
    for issue in diagnostic["issues"]:
        match = re.match(r"^(?:MEETING_IR\.)?(\w+)\[(\d+)\](?:\.|\s|$)", issue)
        if not match:
            return None
        section, index = match.group(1), int(match.group(2))
        rows = ir.get(section)
        if (section not in contract.MEETING_IR_RECORD_FIELDS or not isinstance(rows, list)
                or index >= len(rows) or not isinstance(rows[index], dict)):
            return None
        grouped.setdefault((section, index), []).append(issue)
    if len(grouped) > MAX_REPAIR_ROWS:
        return None
    return {
        "schema": PATCH_VERSION,
        "base_sha256": _sha(text),
        "records": [{"section": section, "index": index,
                     "record_sha256": _row_sha(ir[section][index]),
                     "allow_remove": section == "relations" or (
                         section == "person_updates"
                         and _unmatched_student_projection(ir[section][index], guidance_catalog)),
                     "record": copy.deepcopy(ir[section][index]), "issues": issues}
                    for (section, index), issues in grouped.items()],
    }


def apply_row_repair(text: str, request: dict, response: str) -> str:
    """Merge an exact, hash-bound edit set; callers must revalidate the full result."""
    if request.get("schema") != PATCH_VERSION or request.get("base_sha256") != _sha(text):
        raise ValueError("repair base hash mismatch")
    patch = json.loads(contract._strip_json_fence(response))
    if not isinstance(patch, dict) or set(patch) != {"schema", "base_sha256", "edits"}:
        raise ValueError("invalid repair patch fields")
    if patch["schema"] != PATCH_VERSION or patch["base_sha256"] != request["base_sha256"]:
        raise ValueError("repair patch base mismatch")
    records = request["records"]
    expected = {(row["section"], row["index"]): row for row in records}
    if not expected or len(expected) != len(records) or len(records) > MAX_REPAIR_ROWS:
        raise ValueError("invalid repair request targets")
    if not isinstance(patch["edits"], list) or len(patch["edits"]) != len(expected):
        raise ValueError("repair patch must cover every requested row exactly once")
    ir = _json_section(text, contract.MEETING_IR_DELIMITER)
    seen = set()
    edits = []
    for edit in patch["edits"]:
        if not isinstance(edit, dict) or set(edit) != {
            "section", "index", "record_sha256", "replacement", "reason",
        }:
            raise ValueError("invalid repair edit fields")
        if not isinstance(edit["section"], str) or type(edit["index"]) is not int:
            raise ValueError("invalid repair target type")
        key = (edit["section"], edit["index"])
        if key not in expected or key in seen:
            raise ValueError("repair edit is outside requested scope or duplicated")
        seen.add(key)
        original = ir[key[0]][key[1]]
        if edit["record_sha256"] != expected[key]["record_sha256"] or _row_sha(original) != edit["record_sha256"]:
            raise ValueError("repair record hash mismatch")
        if not isinstance(edit["reason"], str) or not edit["reason"].strip() or len(edit["reason"]) > 500:
            raise ValueError("repair edit needs a bounded reason")
        replacement = edit["replacement"]
        if replacement is None:
            if key[0] not in {"relations", "person_updates"} or expected[key].get("allow_remove") is not True:
                raise ValueError("record removal is not permitted by this repair request")
        elif not isinstance(replacement, dict):
            raise ValueError("replacement must be a complete record")
        edits.append((key[0], key[1], replacement))
    # Original indexes remain stable when multiple rows in the same section are removed.
    for section, index, replacement in sorted(edits, key=lambda item: (item[0], -item[1])):
        if replacement is None:
            del ir[section][index]
        else:
            ir[section][index] = copy.deepcopy(replacement)
    boundary = text.index(contract.MEETING_IR_DELIMITER) + len(contract.MEETING_IR_DELIMITER)
    return text[:boundary] + "\n" + json.dumps(ir, ensure_ascii=False, indent=2) + "\n"

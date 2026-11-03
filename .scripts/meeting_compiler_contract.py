#!/usr/bin/env python3
"""Backend-neutral Meeting Compiler protocol and deterministic validators."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata


PROTOCOL_VERSION = "meeting-compiler-v3"
LEGACY_PROTOCOL_VERSION = "meeting-compiler-v1"
LEGACY_MEETING_IR_PROTOCOL_VERSION = "meeting-compiler-v2"
PREPROCESS_DELIMITER = "<<<PREPROCESS>>>"
WIKI_DELIMITER = "<<<WIKI>>>"
MEETING_IR_DELIMITER = "<<<MEETING_IR>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"
MAX_REPLACEMENTS = 64
MAX_ENTITY_DECISIONS = 128
MAX_MEETING_IR_ITEMS = 128

MEETING_IR_RECORD_FIELDS = {
    "attendees": ("person", "label", "evidence_ids"),
    "topics": ("label", "predicate", "evidence_ids"),
    "reports": ("person", "person_label", "topic", "evidence_ids"),
    "decisions": ("text", "evidence_ids"),
    "tasks": ("text", "assignee", "assignee_label", "evidence_ids"),
    "relations": ("subject", "predicate", "object", "evidence_ids"),
    "person_updates": ("person", "person_label", "kind", "text", "evidence_ids"),
}
UNRESOLVED_PERSON_FIELDS = {"attendees": "person", "reports": "person", "tasks": "assignee"}
ROLE_EVIDENCE = {
    "attendees": "依据明确出席名单、实际发言或互动记录。人物身份匹配、被提及、被布置会后任务均不单独证明参会；点名无回应也不证明缺席。",
    "reports": "依据该人的实际汇报，区分汇报者与被谈论的人；自动会议助手是记录载体，助手的解释或推测保持来源归属。",
    "tasks": "区分指派者、执行者与协作对象，结合上下文保留行动及各方关系。每条记录只对应一位执行者，assignee 与 assignee_label 指同一人，已匹配姓名复用候选目录的对应标签。原文明示多人共同执行时逐人记录，复用共同动作与证据，在 text 中保留协作关系；仅被提及的协作对象不自动成为执行者。动作明确但执行者未明确时 assignee、assignee_label 同为空；执行者称呼明确但全局身份未匹配时仅 assignee 为空并保留原称呼。",
    "relations": "扩展关系只连接证据明确的概念或已关联的人物，两端均为非空的确定端点。涉及身份未关联的人物时，重要关系保留在 Wiki 正文中，使用原称呼、来源归属及 Raw 引用；该关系不进入 relations，不填空端点或猜测人物身份。",
}
WIKI_CONTENT_CONTRACT = (
    "Navigation 概述检索主题，Content 保留研究问题、关键观察、解释、方法限制和未决矛盾，来源限制简明表述。"
    "参会者、议题、汇报、决策、待办和学生指导的完整清单、人物链接及记录条数由程序从 MEETING_IR 生成。"
    "正文只在解释上下文需要时提及人物或事件，省略重复清单和汇总数量。"
    "同一议题出现后续澄清、更正或撤回时，正文连接前述质疑/方案与后续说明，按讨论结束时的状态表述，分歧仍未解决则保留未决。"
    "后续说明只支持其明确陈述的内容；当事人自述和助手评论分别归属，均不自动升级为已证实结论。"
)


def meeting_ir_record_contract() -> dict:
    """Expose the same exact fields and identity boundary used by the validator."""
    return {
        section: {
            "fields": list(fields),
            "text_fields": [field for field in fields if field != "evidence_ids"],
            "evidence_ids": "nonempty unique sNNNN IDs from evidence_catalog, at most 16",
            **({
                "unresolved_identity_field": UNRESOLVED_PERSON_FIELDS[section],
                "unresolved_identity": "empty string; retain the source label and fact in Wiki, not a global identity",
            } if section in UNRESOLVED_PERSON_FIELDS else {}),
            **({"role_evidence": ROLE_EVIDENCE[section]} if section in ROLE_EVIDENCE else {}),
        }
        for section, fields in MEETING_IR_RECORD_FIELDS.items()
    }


def meeting_ir_template() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        **{section: [] for section in MEETING_IR_RECORD_FIELDS},
    }


TYPED_MEETING_PREDICATES = {"参会", "汇报", "决策", "待办", "讨论", "规划"}
TOPIC_PREDICATES = {"讨论", "涉及", "规划"}
FREE_RELATION_PREDICATES = {"涉及", "紧密相关于", "指导", "师从", "受指导于"}
DEICTIC_ENDPOINTS = {"本会议", "本文", "本文件", "本文档", "本论文", "$meeting"}
PERSON_UPDATE_KINDS = {
    "progress", "next_step", "blocker", "decision", "milestone", "topic_change",
}


def task_context_hash(source_text: str, entity_candidates: dict, *, meeting_id: str,
                      target_source_path: str, errors: list[str] | None = None,
                      guidance_candidates: dict | None = None) -> str:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "entity_candidates": entity_candidates,
        "guidance_candidates": guidance_candidates or {},
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


def _preprocess_errors(value, versions: set[str]) -> list[str]:
    """Explain the existing acceptance rules without echoing transcript values."""
    if not isinstance(value, dict):
        return ["PREPROCESS must be an object"]
    errors = []
    version = value.get("protocol_version")
    if not isinstance(version, str) or version not in versions:
        errors.append("PREPROCESS.protocol_version must be " + " or ".join(sorted(versions)))
    rows = {}
    for name, limit in (("transcript_replacements", MAX_REPLACEMENTS),
                        ("entity_resolutions", MAX_ENTITY_DECISIONS)):
        items = value.get(name)
        if not isinstance(items, list):
            errors.append(f"PREPROCESS.{name} must be a list")
        elif len(items) > limit:
            errors.append(f"PREPROCESS.{name} exceeds {limit} items")
        else:
            rows[name] = items
    seen = set()
    for index, item in enumerate(rows.get("transcript_replacements", [])):
        prefix = f"PREPROCESS.transcript_replacements[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        invalid = [key for key in ("original", "replacement", "reason")
                   if not isinstance(item.get(key), str)]
        if invalid:
            errors.extend(f"{prefix}.{key} must be a string" for key in invalid)
            continue
        original, replacement = item["original"], item["replacement"]
        for key, limit in (("original", 80), ("replacement", 160)):
            if not item[key].strip():
                errors.append(f"{prefix}.{key} must be nonempty")
            if "\n" in item[key] or len(item[key]) > limit:
                errors.append(f"{prefix}.{key} must be single-line and at most {limit} characters")
        if original in seen:
            errors.append(f"{prefix}.original duplicates an earlier replacement")
        seen.add(original)
    for index, item in enumerate(rows.get("entity_resolutions", [])):
        prefix = f"PREPROCESS.entity_resolutions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if set(item) != {"mention", "canonical", "status", "reason"}:
            errors.append(f"{prefix} fields must be exactly: mention, canonical, status, reason")
            continue
        invalid = [key for key in ("mention", "canonical", "status", "reason")
                   if not isinstance(item[key], str)]
        if invalid:
            errors.extend(f"{prefix}.{key} must be a string" for key in invalid)
            continue
        if item["status"] not in {"resolved", "unchanged", "unresolved"}:
            errors.append(f"{prefix}.status must be resolved, unchanged or unresolved")
        for key in ("mention", "reason"):
            if not item[key].strip():
                errors.append(f"{prefix}.{key} must be nonempty")
        if item["status"] == "resolved" and not item["canonical"].strip():
            errors.append(f"{prefix}.canonical must be nonempty for resolved identity")
    return errors


def _validate_preprocess_version(value, versions: set[str]) -> bool:
    return not _preprocess_errors(value, versions)


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


def _validate_meeting_ir(value, evidence_ids: set[str] | None = None, *,
                         allow_legacy_v2: bool = False) -> list[str]:
    """Validate typed, evidence-bound meeting semantics before any projection."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["MEETING_IR must be an object"]
    current_required = {"protocol_version", *MEETING_IR_RECORD_FIELDS}
    legacy_required = current_required - {"person_updates"}
    required = (
        legacy_required
        if allow_legacy_v2 and value.get("protocol_version") == LEGACY_MEETING_IR_PROTOCOL_VERSION
        else current_required
    )
    if set(value) != required:
        errors.append("MEETING_IR fields must be exactly: " + ", ".join(sorted(required)))
        return errors
    allowed_versions = (
        {PROTOCOL_VERSION, LEGACY_MEETING_IR_PROTOCOL_VERSION}
        if allow_legacy_v2 else {PROTOCOL_VERSION}
    )
    if value.get("protocol_version") not in allowed_versions:
        errors.append(
            "MEETING_IR protocol_version must be " + " or ".join(sorted(allowed_versions))
        )

    specs = {
        section: (set(fields), tuple(field for field in fields if field != "evidence_ids"))
        for section, fields in MEETING_IR_RECORD_FIELDS.items() if section in required
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
            if any(not isinstance(row.get(field), str) for field in text_fields):
                errors.append(f"{prefix} text fields must be strings")
                continue
            values = tuple(row[field].strip() for field in text_fields)
            optional_identity = (
                UNRESOLVED_PERSON_FIELDS.get(section)
                if value.get("protocol_version") == PROTOCOL_VERSION else None
            )
            optional_fields = {optional_identity}
            if section == "tasks" and optional_identity and not row["assignee"].strip():
                optional_fields.add("assignee_label")
            if any(not row[field].strip() for field in text_fields if field not in optional_fields):
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
            if section == "decisions" and re.search(
                r"[；;].{0,12}(?:后|随后).{0,12}(?:纠正|改为|否决|替换)",
                values[0],
            ):
                errors.append(
                    f"{prefix} combines a superseded proposal with the final decision"
                )
            if section == "relations":
                predicate = str(row.get("predicate") or "").strip()
                if predicate not in FREE_RELATION_PREDICATES:
                    errors.append(f"{prefix}.predicate overlaps typed slots or is unsupported")
                if predicate in TYPED_MEETING_PREDICATES:
                    errors.append(f"{prefix}.predicate must use a typed meeting section")
            if section == "person_updates" and row.get("kind") not in PERSON_UPDATE_KINDS:
                errors.append(f"{prefix}.kind is unsupported")
    return errors


def validate_meeting_ir(value, evidence_ids: set[str] | None = None, *,
                        evidence_catalog: list[dict] | None = None) -> list[str]:
    errors = _validate_meeting_ir(value, evidence_ids=evidence_ids)
    if evidence_catalog is None or not isinstance(value, dict):
        return errors
    evidence = {row["evidence_id"]: str(row["text"]) for row in evidence_catalog}
    for section, fields in {
        "topics": ("label",), "reports": ("topic",), "relations": ("subject", "object"),
    }.items():
        rows = value.get(section)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if (not isinstance(row, dict) or set(row) != set(MEETING_IR_RECORD_FIELDS[section])
                    or not all(isinstance(row.get(field), str) for field in fields)
                    or not _valid_evidence_ids(row.get("evidence_ids"), evidence_ids)):
                continue
            source = unicodedata.normalize(
                "NFKC", "\n".join(evidence.get(key, "") for key in row["evidence_ids"]),
            )
            for field in fields:
                for acronym in sorted(set(re.findall(
                    r"(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?![A-Za-z0-9])",
                    unicodedata.normalize("NFKC", row[field]),
                ))):
                    if not re.search(
                        r"(?<![A-Za-z0-9])" + re.escape(acronym) + r"(?![A-Za-z0-9])",
                        source, re.IGNORECASE | re.ASCII,
                    ):
                        errors.append(
                            f"MEETING_IR.{section}[{index}].{field} contains unsupported acronym "
                            f"{acronym!r}; preserve the source term or cite explicit source support"
                        )
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
    preprocess_errors = _preprocess_errors(
        preprocess,
        {PROTOCOL_VERSION, LEGACY_MEETING_IR_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION},
    )
    if preprocess_errors:
        error = "invalid meeting compiler preprocess proposal"
        return None, error, {"kind": "schema", "stage": "validate_preprocess",
                             "segment": "PREPROCESS", "message": error,
                             "issues": preprocess_errors}
    semantic_delimiter = (
        SLOTS_DELIMITER if version == LEGACY_PROTOCOL_VERSION else MEETING_IR_DELIMITER
    )
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
    if version == LEGACY_MEETING_IR_PROTOCOL_VERSION:
        ir_errors = _validate_meeting_ir(
            meeting_ir, evidence_ids=None, allow_legacy_v2=True,
        )
        if not ir_errors:
            meeting_ir = dict(meeting_ir)
            meeting_ir["protocol_version"] = PROTOCOL_VERSION
            meeting_ir["person_updates"] = []
    else:
        ir_errors = validate_meeting_ir(meeting_ir)
    if ir_errors:
        error = f"invalid {version or PROTOCOL_VERSION} meeting IR"
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
    # Source-bound no-ops carry no edit and must not shadow a real replacement.
    mapping = {original: replacement for original, replacement in mapping.items()
               if original != replacement}
    if not mapping:
        return source_text
    pattern = re.compile("|".join(re.escape(value) for value in sorted(mapping, key=len, reverse=True)))
    matched = set(pattern.findall(source_text))
    if matched != set(mapping):
        raise ValueError("overlapping transcript replacements are not allowed")
    return pattern.sub(lambda match: mapping[match.group(0)], source_text)

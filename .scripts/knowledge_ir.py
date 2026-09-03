#!/usr/bin/env python3
"""Versioned cross-document semantic IR used before GraphDelta fusion."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


KNOWLEDGE_IR_SCHEMA = "knowledge-ir-v1"
GRAPH_PLAN_SCHEMA = "graph-plan-v1"
PROFILES = {"paper", "meeting", "document"}
DOMAINS = {"academic", "admin", "teaching", "business", "private"}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [text]


def infer_document_profile(page: str, frontmatter: dict) -> tuple[str, str]:
    wiki_type = str(frontmatter.get("type") or "").strip()
    normalized = f"/{page.removesuffix('.md')}/"
    if wiki_type == "paper-summary" or "/wiki/papers/" in normalized:
        profile = "paper"
    elif wiki_type == "conference-summary" or any(
        marker in normalized for marker in ("/wiki/conferences/", "/wiki/meetings/")
    ):
        profile = "meeting"
    else:
        profile = "document"
    first = page.split("/", 1)[0]
    domain = first if first in DOMAINS else "cross-domain"
    return profile, domain


def _relation_record(
    relation: dict,
    index: int,
    deterministic_count: int,
    *,
    force_origin: str | None = None,
) -> dict:
    payload = dict(relation)
    for key in ("subject", "predicate", "object"):
        payload[key] = str(payload.get(key) or "").strip()
    if "source" in payload:
        payload["source"] = str(payload.get("source") or "").strip()
    identity = {"index": index, "relation": payload}
    return {
        "relation_id": f"r{index:04d}-{content_hash(identity)[:16]}",
        "origin": force_origin or (
            "deterministic" if index < deterministic_count else "semantic"
        ),
        **payload,
    }


def _build_extensions(profile: str, page: str, relations: Iterable[dict]) -> dict:
    rows = list(relations)
    if profile == "meeting":
        return {
            "meeting": {
                "attendees": [
                    row["subject"] for row in rows
                    if row.get("predicate") == "参会" and row.get("object") == page
                ],
                "reports": [
                    {"person": row["subject"], "topic": row["object"]}
                    for row in rows if row.get("predicate") == "汇报"
                ],
                "decisions": [
                    row["object"] for row in rows
                    if row.get("predicate") == "决策" and row.get("subject") == page
                ],
                "tasks": [
                    {"assignee": row["subject"], "task": row["object"]}
                    for row in rows if row.get("predicate") == "待办"
                ],
            }
        }
    if profile == "paper":
        author_predicates = {"作者", "第一作者", "通讯作者"}
        proposition_predicates = {"核心创新点", "局限性", "未来展望"}
        return {
            "paper": {
                "authors": [
                    {"person": row["subject"], "role": row["predicate"]}
                    for row in rows if row.get("predicate") in author_predicates
                ],
                "propositions": [
                    {"kind": row["predicate"], "text": row["object"]}
                    for row in rows if row.get("predicate") in proposition_predicates
                ],
            }
        }
    return {
        "document": {
            "predicate_counts": dict(sorted(Counter(
                str(row.get("predicate") or "") for row in rows
                if str(row.get("predicate") or "")
            ).items()))
        }
    }


def build_knowledge_ir(
    page: str,
    frontmatter: dict,
    relations: Iterable[dict],
    *,
    deterministic_relation_count: int = 0,
    concept_glosses: Iterable[dict] | None = None,
    structural_relations: Iterable[dict] | None = None,
    transaction_id: str = "",
) -> dict:
    page = page.removesuffix(".md")
    profile, domain = infer_document_profile(page, frontmatter)
    relation_rows = [
        _relation_record(row, index, deterministic_relation_count)
        for index, row in enumerate(relations)
    ]
    structural_rows = []
    for index, relation in enumerate(structural_relations or []):
        row = _relation_record(
            relation, index, 0, force_origin="deterministic"
        )
        row["relation_id"] = "s" + row["relation_id"][1:]
        row["kind"] = "raw_relationship"
        structural_rows.append(row)
    located = sum(bool(str(row.get("source") or "").strip()) for row in relation_rows)
    ir = {
        "schema": KNOWLEDGE_IR_SCHEMA,
        "document": {
            "page": page,
            "profile": profile,
            "domain": domain,
            "wiki_type": str(frontmatter.get("type") or ""),
            "sources": _as_string_list(frontmatter.get("sources")),
            "transaction_id": str(transaction_id or ""),
        },
        "relations": relation_rows,
        "structural_relations": structural_rows,
        "concept_glosses": [dict(item) for item in (concept_glosses or [])],
        "extensions": _build_extensions(profile, page, relation_rows),
        "conflicts": [],
        "quality": {
            "relation_count": len(relation_rows),
            "located_relations": located,
            "unlocated_relations": len(relation_rows) - located,
            "locator_coverage": round(located / len(relation_rows), 4) if relation_rows else 1.0,
        },
    }
    ensure_valid_knowledge_ir(ir)
    return ir


def validate_knowledge_ir(ir: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(ir, dict):
        return ["knowledge IR must be an object"]
    if ir.get("schema") != KNOWLEDGE_IR_SCHEMA:
        errors.append(f"schema must be {KNOWLEDGE_IR_SCHEMA}")
    document = ir.get("document")
    if not isinstance(document, dict):
        errors.append("document must be an object")
        document = {}
    if not str(document.get("page") or "").strip():
        errors.append("document.page is required")
    if document.get("profile") not in PROFILES:
        errors.append("document.profile is invalid")
    relations = ir.get("relations")
    if not isinstance(relations, list):
        errors.append("relations must be a list")
        relations = []
    seen_ids: set[str] = set()
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            errors.append(f"relations[{index}] must be an object")
            continue
        for field in ("relation_id", "subject", "predicate", "object"):
            if not str(relation.get(field) or "").strip():
                errors.append(f"relations[{index}].{field} is required")
        relation_id = str(relation.get("relation_id") or "")
        if relation_id in seen_ids:
            errors.append(f"relations[{index}].relation_id is duplicated")
        seen_ids.add(relation_id)
        if relation.get("origin") not in {"deterministic", "semantic"}:
            errors.append(f"relations[{index}].origin is invalid")
    structural = ir.get("structural_relations")
    if not isinstance(structural, list):
        errors.append("structural_relations must be a list")
        structural = []
    structural_ids: set[str] = set()
    for index, relation in enumerate(structural):
        if not isinstance(relation, dict):
            errors.append(f"structural_relations[{index}] must be an object")
            continue
        for field in ("relation_id", "subject", "predicate", "object"):
            if not str(relation.get(field) or "").strip():
                errors.append(f"structural_relations[{index}].{field} is required")
        if relation.get("kind") != "raw_relationship":
            errors.append(f"structural_relations[{index}].kind is invalid")
        if relation.get("origin") != "deterministic":
            errors.append(f"structural_relations[{index}].origin is invalid")
        relation_id = str(relation.get("relation_id") or "")
        if relation_id in structural_ids:
            errors.append(f"structural_relations[{index}].relation_id is duplicated")
        structural_ids.add(relation_id)
    glosses = ir.get("concept_glosses")
    if not isinstance(glosses, list):
        errors.append("concept_glosses must be a list")
        glosses = []
    for index, gloss in enumerate(glosses):
        if not isinstance(gloss, dict):
            errors.append(f"concept_glosses[{index}] must be an object")
            continue
        if not str(gloss.get("mention") or "").strip():
            errors.append(f"concept_glosses[{index}].mention is required")
        if not str(gloss.get("description") or "").strip():
            errors.append(f"concept_glosses[{index}].description is required")
    if not isinstance(ir.get("extensions"), dict):
        errors.append("extensions must be an object")
    if not isinstance(ir.get("conflicts"), list):
        errors.append("conflicts must be a list")
    return errors


def ensure_valid_knowledge_ir(ir: Any) -> None:
    errors = validate_knowledge_ir(ir)
    if errors:
        raise ValueError("invalid knowledge-ir-v1: " + "; ".join(errors))


def relations_from_ir(ir: dict) -> list[dict]:
    ensure_valid_knowledge_ir(ir)
    result = []
    for relation in ir["relations"]:
        row = dict(relation)
        row.pop("relation_id", None)
        row.pop("origin", None)
        result.append(row)
    return result


def validate_document_binding(ir: dict, page: str, frontmatter: dict) -> list[str]:
    """Validate the deterministic page/profile identity of an IR proposal."""
    ensure_valid_knowledge_ir(ir)
    expected_page = str(page or "").removesuffix(".md").strip()
    expected_profile, _expected_domain = infer_document_profile(
        expected_page, frontmatter
    )
    document = ir["document"]
    errors = []
    if str(document.get("page") or "").removesuffix(".md").strip() != expected_page:
        errors.append("document.page does not match --page")
    if document.get("profile") != expected_profile:
        errors.append("document.profile does not match Wiki frontmatter/path")
    return errors


def semantic_proposal_content(
    ir: dict, page: str, frontmatter: dict
) -> tuple[list[dict], list[dict]]:
    """Extract untrusted semantic content for deterministic recompilation.

    A semantic worker may propose relations and local gloss text. It may not
    declare deterministic relations, structural Raw edges, canonical endpoints,
    metadata kinds, locators, or recording metadata.
    """
    binding_errors = validate_document_binding(ir, page, frontmatter)
    if binding_errors:
        raise ValueError("invalid knowledge IR binding: " + "; ".join(binding_errors))
    if ir.get("structural_relations"):
        raise ValueError(
            "semantic knowledge IR must not contain structural_relations"
        )
    non_semantic = [
        relation.get("relation_id", "")
        for relation in ir["relations"]
        if relation.get("origin") != "semantic"
    ]
    if non_semantic:
        raise ValueError(
            "semantic knowledge IR must not declare deterministic relations: "
            + ", ".join(non_semantic)
        )
    relations = [
        {
            key: relation[key]
            for key in ("subject", "predicate", "object", "confidence")
            if key in relation
        }
        for relation in ir["relations"]
    ]
    glosses = [
        {
            "mention": str(gloss["mention"]).strip(),
            "description": str(gloss["description"]).strip(),
        }
        for gloss in ir["concept_glosses"]
    ]
    return relations, glosses


def knowledge_ir_hash(ir: dict) -> str:
    ensure_valid_knowledge_ir(ir)
    return content_hash(ir)


def build_graph_plan(ir: dict, inspection: dict | None = None) -> dict:
    ensure_valid_knowledge_ir(ir)
    plan = {
        "schema": GRAPH_PLAN_SCHEMA,
        "knowledge_ir_schema": KNOWLEDGE_IR_SCHEMA,
        "knowledge_ir_sha256": knowledge_ir_hash(ir),
        "document": dict(ir["document"]),
        "relation_count": len(ir["relations"]),
        "relation_ids": [row["relation_id"] for row in ir["relations"]],
        "structural_relation_count": len(ir["structural_relations"]),
        "structural_relation_ids": [
            row["relation_id"] for row in ir["structural_relations"]
        ],
        "origin_counts": dict(sorted(Counter(
            row["origin"] for row in ir["relations"]
        ).items())),
        "inspection": inspection or {},
    }
    ensure_valid_graph_plan(plan, ir)
    return plan


def validate_graph_plan(plan: Any, ir: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["graph plan must be an object"]
    if plan.get("schema") != GRAPH_PLAN_SCHEMA:
        errors.append(f"schema must be {GRAPH_PLAN_SCHEMA}")
    if plan.get("knowledge_ir_schema") != KNOWLEDGE_IR_SCHEMA:
        errors.append("knowledge_ir_schema mismatch")
    expected_hash = knowledge_ir_hash(ir)
    if plan.get("knowledge_ir_sha256") != expected_hash:
        errors.append("knowledge_ir_sha256 mismatch")
    if plan.get("relation_count") != len(ir["relations"]):
        errors.append("relation_count mismatch")
    if plan.get("relation_ids") != [row["relation_id"] for row in ir["relations"]]:
        errors.append("relation_ids mismatch")
    if plan.get("structural_relation_count") != len(ir["structural_relations"]):
        errors.append("structural_relation_count mismatch")
    if plan.get("structural_relation_ids") != [
        row["relation_id"] for row in ir["structural_relations"]
    ]:
        errors.append("structural_relation_ids mismatch")
    return errors


def ensure_valid_graph_plan(plan: Any, ir: dict) -> None:
    errors = validate_graph_plan(plan, ir)
    if errors:
        raise ValueError("invalid graph-plan-v1: " + "; ".join(errors))


def write_json(path: str | Path, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, target)


def load_knowledge_ir(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    ensure_valid_knowledge_ir(value)
    return value


def summarize_knowledge_ir(ir: dict) -> dict:
    return {
        "schema": ir["schema"],
        "sha256": knowledge_ir_hash(ir),
        "profile": ir["document"]["profile"],
        "relation_count": len(ir["relations"]),
        "structural_relation_count": len(ir["structural_relations"]),
        **ir["quality"],
    }


def summarize_graph_plan(plan: dict) -> dict:
    return {
        "schema": plan["schema"],
        "knowledge_ir_sha256": plan["knowledge_ir_sha256"],
        "relation_count": plan["relation_count"],
        "structural_relation_count": plan["structural_relation_count"],
        "origin_counts": plan["origin_counts"],
    }

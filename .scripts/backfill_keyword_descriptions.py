#!/usr/bin/env python3
"""Source-bounded API backfill for legacy keyword descriptions.

The program selects existing keyword nodes with missing/invalid descriptions,
resolves one existing Wiki origin to a precise Raw locator, and exposes only a
short evidence card to the API worker.  The worker cannot choose node IDs or
locators.  Default mode is a read-only plan; ``--apply`` writes graph.db only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_repair as gr
import source_locator as sl
import wiki_locator as wl
from llm_structured import call_json, configured_model, ingest_mode


MAX_EVIDENCE_CHARS = 2400
DEFAULT_BATCH_SIZE = 16
TERMINAL_REVIEW_STATUSES = {
    "worker_uncertain", "generator_invalid", "generator_failed",
    "reviewer_rejected", "review_failed",
}


def evidence_language(text: str) -> str:
    value = str(text or "")
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if latin >= 30 and cjk < 5:
        return "English"
    if cjk >= 20 and latin < cjk:
        return "Chinese"
    return "source language"


def _evidence_excerpt(locator: str, title: str) -> str:
    if not str(locator or "").strip():
        return ""
    path, fragment = sl.split_locator(locator)
    target = sl.resolve_path(path)
    if target is None or not target.is_file() or sl.locator_status(fragment, target) != "present":
        return ""
    text = sl.read_locator_text(target, fragment).strip()
    if not text:
        return ""
    folded = text.casefold()
    position = folded.find(str(title or "").casefold())
    if position < 0 or len(text) <= MAX_EVIDENCE_CHARS:
        return text[:MAX_EVIDENCE_CHARS]
    start = max(0, position - MAX_EVIDENCE_CHARS // 2)
    return text[start:start + MAX_EVIDENCE_CHARS]


def collect_candidates(conn, node_ids=None, limit=0, retry_rejected=False):
    audit = gr.semantic_description_audit(conn, details=True)
    wanted = set(node_ids or [])
    candidates = []
    blocked = {
        "identity_review": 0,
        "no_source_backed_origin": 0,
        "no_precise_raw_locator": 0,
        "previously_reviewed": 0,
    }
    for item in audit.get("nodes", []):
        if wanted and item["node"] not in wanted:
            continue
        if item.get("identity_issue"):
            blocked["identity_review"] += 1
            continue
        origins = item.get("source_backed_origins", [])
        if not origins:
            blocked["no_source_backed_origin"] += 1
            continue
        previous = conn.execute(
            "SELECT status FROM node_description_reviews "
            "WHERE node_path=? ORDER BY id DESC LIMIT 1",
            (item["node"],),
        ).fetchone()
        if (
            previous and not retry_rejected
            and previous["status"] in TERMINAL_REVIEW_STATUSES
        ):
            blocked["previously_reviewed"] += 1
            continue
        selected = None
        for origin in origins:
            page = origin["page"]
            page_file = gl.REPO / f"{page}.md"
            if not page_file.is_file():
                continue
            wiki_source, citations = wl.graph_wiki_source(page_file, item["title"])
            raw_source = wl.best_raw_citation(citations, item["title"])
            excerpt = _evidence_excerpt(raw_source, item["title"])
            if raw_source and excerpt:
                selected = {
                    "node": item["node"],
                    "title": item["title"],
                    "issue": item["issue"],
                    "origin_page": page,
                    "wiki_source": wiki_source,
                    "raw_source": raw_source,
                    "evidence_quote": excerpt,
                }
                break
        if selected is None:
            blocked["no_precise_raw_locator"] += 1
            continue
        candidates.append(selected)
        if limit and len(candidates) >= limit:
            break
    return {
        "audit": {
            key: value for key, value in audit.items()
            if key not in {"nodes", "reingest_pages"}
        },
        "candidate_count": len(candidates),
        "blocked": blocked,
        "candidates": candidates,
    }


def response_schema(allowed):
    allowed = set(allowed)

    def check(value):
        if not isinstance(value, dict) or set(value) != {"descriptions", "uncertain"}:
            return False
        if not isinstance(value["descriptions"], list) or not isinstance(value["uncertain"], list):
            return False
        seen = set()
        for item in value["descriptions"]:
            if not isinstance(item, dict) or set(item) != {"id", "description", "evidence_id"}:
                return False
            if item["id"] not in allowed or item["id"] in seen:
                return False
            if item["evidence_id"] != f"E{item['id'][1:]}":
                return False
            if not isinstance(item["description"], str):
                return False
            seen.add(item["id"])
        uncertain = value["uncertain"]
        if not all(isinstance(item, str) and item in allowed for item in uncertain):
            return False
        if len(set(uncertain)) != len(uncertain) or seen.intersection(uncertain):
            return False
        return seen.union(uncertain) == allowed

    return check


def review_response_schema(allowed):
    allowed = set(allowed)

    def check(value):
        if not isinstance(value, dict) or set(value) != {"approved", "rejected"}:
            return False
        approved = value["approved"]
        rejected = value["rejected"]
        if not isinstance(approved, list) or not isinstance(rejected, list):
            return False
        if not all(isinstance(item, str) and item in allowed for item in approved):
            return False
        rejected_ids = []
        for item in rejected:
            if not isinstance(item, dict) or set(item) != {"id", "reason"}:
                return False
            if item["id"] not in allowed or not isinstance(item["reason"], str):
                return False
            rejected_ids.append(item["id"])
        decided = approved + rejected_ids
        return len(decided) == len(set(decided)) and set(decided) == allowed

    return check


def build_prompt(records):
    cards = []
    for index, record in enumerate(records, start=1):
        language = evidence_language(record["evidence_quote"])
        cards.append(
            f"K{index} | 概念: {record['title']} | 输出语言: {language} | 证据: E{index}\n"
            f"E{index}: {record['evidence_quote']}"
        )
    return (
        "CRITICAL LANGUAGE RULE: Each description MUST use the output language printed on its evidence card. "
        "An English description MUST contain no Chinese characters. Keep technical phrases, symbols, and formulas verbatim. "
        "仅输出严格 JSON，字段只能是 descriptions 和 uncertain。"
        "对每个概念，只依据绑定的单张证据卡写一句文档局部说明，说明该概念在该文档中的定义、作用或适用语境。"
        "句子必须自包含并以概念本身为语义主语，不得用‘本文’‘该文档’‘该论文’‘该研究’等来源指代开头。"
        "描述必须沿用绑定证据的主要语言：英文证据写英文，中文证据写中文；技术词组、符号和公式保留原文，不自行翻译。"
        "避免连续重复词语。不得使用外部知识，不得只复述概念名，不得生成路径或 locator。"
        "每个 K 编号必须且只能出现一次；证据不足时把 K 编号放入 uncertain。"
        "description 必须是 10-240 字符的单行句子；英文说明控制在 18-40 个词，中文说明控制在 20-100 个汉字。格式："
        '{"descriptions":[{"id":"K1","description":"...","evidence_id":"E1"}],"uncertain":[]}\n\n'
        + "\n\n".join(cards)
    )


def build_review_prompt(records, proposed):
    cards = []
    for item_id, description in proposed.items():
        record = records[int(item_id[1:]) - 1]
        language = evidence_language(record["evidence_quote"])
        cards.append(
            f"{item_id} | 概念: {record['title']} | 要求语言: {language}\n"
            f"候选说明: {description}\n"
            f"绑定证据: {record['evidence_quote']}"
        )
    return (
        "仅输出严格 JSON，字段只能是 approved 和 rejected。你是证据一致性复核员，不改写候选说明。"
        "逐项判断候选说明的每个实质性断言是否由绑定证据直接支持，且术语翻译、主客体、范围和因果方向准确。"
        "候选说明必须沿用证据的主要语言并原样保留技术词组、符号和公式。"
        "出现翻译、误译、添加证据外结论、概念身份含混、范围扩大、主客体或因果倒置、病句时必须拒绝。"
        "每个 K 编号必须且只能出现一次。格式："
        '{"approved":["K1"],"rejected":[{"id":"K2","reason":"简短原因"}]}\n\n'
        + "\n\n".join(cards)
    )


def validate_description(title: str, description: str, evidence: str = "") -> str:
    text = str(description or "").strip()
    if len(text) < 10:
        return "too_short"
    if len(text) > 240:
        return "too_long"
    evidence = str(evidence or "")
    evidence_cjk = len(re.findall(r"[\u3400-\u9fff]", evidence))
    evidence_latin = len(re.findall(r"[A-Za-z]", evidence))
    description_cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    description_latin = len(re.findall(r"[A-Za-z]", text))
    if evidence_latin >= 30 and evidence_cjk < 5:
        if description_cjk or description_latin < 20:
            return "source_language_mismatch"
    elif evidence_cjk >= 20 and evidence_latin < evidence_cjk:
        if description_cjk < 10:
            return "source_language_mismatch"
    issue = gr.keyword_description_issue(title, text)
    return issue or ""


def apply_batches(conn, candidates, batch_size=DEFAULT_BATCH_SIZE, max_batches=0):
    accepted = []
    rejected = []
    api_calls = 0
    batches_processed = 0
    for offset in range(0, len(candidates), batch_size):
        if max_batches and batches_processed >= max_batches:
            break
        batches_processed += 1
        records = candidates[offset:offset + batch_size]
        ids = [f"K{index}" for index in range(1, len(records) + 1)]
        result = call_json(
            build_prompt(records),
            response_schema(ids),
            max_tokens=max(800, len(records) * 160),
            retries=1,
            operation="ingest_api_keywords",
            reasoning="fast",
            reasoning_context={"candidate_count": len(records), "task": "description_backfill"},
            transaction_id=f"keyword-description-backfill-{offset // batch_size + 1}",
        )
        api_calls += 1
        if not result.get("ok") or not result.get("parsed"):
            reason = result.get("error") or result.get("status") or "api_failed"
            for record in records:
                gl.add_node_description_review(
                    conn, record["node"], record["origin_page"], record["raw_source"],
                    "generator_failed", reason,
                )
                rejected.append({
                    "node": record["node"], "reason": f"generator_failed:{reason}",
                })
            conn.commit()
            continue
        by_id = {f"K{index}": record for index, record in enumerate(records, start=1)}
        ready = {}
        for item in result["parsed"]["descriptions"]:
            record = by_id[item["id"]]
            description = item["description"].strip()
            issue = validate_description(
                record["title"], description, record["evidence_quote"],
            )
            current = conn.execute(
                "SELECT title,description FROM nodes WHERE path=? AND type='entity' "
                "AND entity_subtype='keyword'",
                (record["node"],),
            ).fetchone()
            current_issue = (
                gr.keyword_description_issue(current["title"], current["description"])
                if current else "node_missing"
            )
            if issue or not current_issue or not _evidence_excerpt(
                record["raw_source"], record["title"]
            ):
                if issue:
                    gl.add_node_description_review(
                        conn, record["node"], record["origin_page"], record["raw_source"],
                        "generator_invalid", issue, description,
                    )
                rejected.append({
                    "node": record["node"],
                    "reason": issue or "no_longer_eligible_or_evidence_missing",
                })
                continue
            ready[item["id"]] = description
        for item in result["parsed"]["uncertain"]:
            record = by_id[item]
            gl.add_node_description_review(
                conn, record["node"], record["origin_page"], record["raw_source"],
                "worker_uncertain", "evidence_insufficient",
            )
            rejected.append({"node": by_id[item]["node"], "reason": "worker_uncertain"})

        approved = set()
        if ready:
            review = call_json(
                build_review_prompt(records, ready),
                review_response_schema(ready),
                max_tokens=max(500, len(ready) * 100),
                retries=1,
                operation="ingest_api_keyword_review",
                reasoning="fast",
                reasoning_context={"candidate_count": len(ready), "task": "description_backfill_review"},
                transaction_id=f"keyword-description-review-{offset // batch_size + 1}",
            )
            api_calls += 1
            if not review.get("ok") or not review.get("parsed"):
                reason = review.get("error") or review.get("status") or "api_failed"
                for item_id in ready:
                    record = by_id[item_id]
                    gl.add_node_description_review(
                        conn, record["node"], record["origin_page"], record["raw_source"],
                        "review_failed", reason, ready[item_id],
                    )
                    rejected.append({"node": record["node"], "reason": f"review_failed:{reason}"})
            else:
                approved.update(review["parsed"]["approved"])
                for item in review["parsed"]["rejected"]:
                    record = by_id[item["id"]]
                    gl.add_node_description_review(
                        conn, record["node"], record["origin_page"], record["raw_source"],
                        "reviewer_rejected", item["reason"], ready[item["id"]],
                    )
                    rejected.append({
                        "node": by_id[item["id"]]["node"],
                        "reason": f"reviewer_rejected:{item['reason']}",
                    })

        for item_id in sorted(approved, key=lambda value: int(value[1:])):
            record = by_id[item_id]
            description = ready[item_id]
            if not _evidence_excerpt(record["raw_source"], record["title"]):
                rejected.append({"node": record["node"], "reason": "evidence_missing_before_write"})
                continue
            gl.add_node_gloss(
                conn,
                record["node"],
                record["origin_page"],
                record["raw_source"],
                description,
                promote=True,
            )
            gl.add_node_description_review(
                conn, record["node"], record["origin_page"], record["raw_source"],
                "accepted", "", description,
            )
            accepted.append({
                "node": record["node"],
                "origin_page": record["origin_page"],
                "source": record["raw_source"],
                "description": description,
            })
        conn.commit()
    return {
        "applied": True,
        "batches_processed": batches_processed,
        "api_calls": api_calls,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--node", action="append", default=[])
    parser.add_argument(
        "--retry-rejected", action="store_true",
        help="重新评估相同 Raw locator 上已 abstain/拒绝的节点",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 32:
        parser.error("--batch-size 必须在 1..32")
    db_path = args.db or gl.GRAPH_DB
    if not Path(db_path).is_file():
        parser.error(f"graph.db 不存在: {db_path}")
    conn = gl.connect(str(db_path))
    try:
        plan = collect_candidates(
            conn, args.node, max(0, args.limit), retry_rejected=args.retry_rejected,
        )
        summary = {
            "mode": "apply" if args.apply and not args.dry_run else "dry-run",
            "backend": ingest_mode(),
            "model": configured_model(),
            **{key: value for key, value in plan.items() if key != "candidates"},
        }
        if args.apply and not args.dry_run:
            summary["result"] = apply_batches(
                conn,
                plan["candidates"],
                args.batch_size,
                max(0, args.max_batches),
            )
        else:
            summary["sample"] = [
                {key: item[key] for key in ("node", "title", "origin_page", "raw_source")}
                for item in plan["candidates"][:10]
            ]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

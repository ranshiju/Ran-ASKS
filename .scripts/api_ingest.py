#!/usr/bin/env python3
"""受限 API 摄入编排器：弱模型只生成可验证的证据绑定草稿。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

from llm_structured import call_json, configured_model, ingest_mode
from derivation_state import provenance

ALLOWED_FIELDS = ("motivation", "method", "contribution", "result", "limitation", "outlook")
MAX_CLAIMS_PER_FIELD = 3
MAX_CLAIM_CHARS = 180
MAX_EVIDENCE_CARDS = 12
SCHEMA_VERSION = "api-evidence-card-v1"
RULE_VERSION = "api-ingest-v1"
PROMPT_VERSION = "api-ingest-prompts-v1"


def section_text(raw: str, headings: tuple[str, ...]) -> str:
    for heading in headings:
        marker = f"## {heading}"
        if marker not in raw:
            continue
        tail = raw.split(marker, 1)[1]
        return tail.split("\n## ", 1)[0].strip()
    return ""


def source_segments(raw: str) -> dict[str, str]:
    abstract = raw.split("## ", 1)[0].strip()
    return {
        "abstract": abstract,
        "discussion": section_text(raw, ("V. DISCUSSION", "DISCUSSION", "CONCLUSION", "CONCLUSIONS")),
    }


def evidence_cards(segments: dict[str, str]) -> list[dict]:
    """程序切分短证据卡；模型只选 id，不再复制定位器或引文。"""
    cards = []
    for locator, text in segments.items():
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            sentence = sentence.strip()
            if len(sentence) < 30:
                continue
            cards.append({"id": f"E{len(cards) + 1}", "raw_locator": locator, "evidence_quote": sentence})
            if len(cards) >= MAX_EVIDENCE_CARDS:
                return cards
    return cards


def claim_schema(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"claims", "uncertain"}:
        return False
    if not isinstance(value["claims"], list) or not isinstance(value["uncertain"], list):
        return False
    if len(value["claims"]) > len(ALLOWED_FIELDS) * MAX_CLAIMS_PER_FIELD:
        return False
    for item in value["claims"]:
        if not isinstance(item, dict) or set(item) != {"field", "claim", "evidence_id"}:
            return False
        if item["field"] not in ALLOWED_FIELDS:
            return False
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("claim", "evidence_id")):
            return False
    return all(isinstance(item, str) for item in value["uncertain"])


def validate_claims(draft: dict, cards: list[dict]) -> tuple[list[dict], list[dict]]:
    accepted, rejected = [], []
    counts = {field: 0 for field in ALLOWED_FIELDS}
    card_by_id = {card["id"]: card for card in cards}
    for item in draft.get("claims", []):
        field = item.get("field")
        claim = item.get("claim", "").strip()
        card = card_by_id.get(item.get("evidence_id"))
        if field not in ALLOWED_FIELDS or not card:
            rejected.append({"item": item, "reason": "非法字段或证据卡编号"})
        elif len(claim) > MAX_CLAIM_CHARS:
            rejected.append({"item": item, "reason": "声明超长"})
        elif counts[field] >= MAX_CLAIMS_PER_FIELD:
            rejected.append({"item": item, "reason": "同字段条目超限"})
        else:
            accepted.append({"field": field, "claim": claim, **card})
            counts[field] += 1
    return accepted, rejected


def keyword_schema(candidates):
    candidate_set = set(candidates)

    def check(value) -> bool:
        return (
            isinstance(value, dict)
            and set(value) == {"selected", "uncertain"}
            and isinstance(value["selected"], list)
            and isinstance(value["uncertain"], list)
            and len(value["selected"]) <= 8
            and all(
                isinstance(item, dict)
                and set(item) == {"term", "evidence_id"}
                and item["term"] in candidate_set
                and isinstance(item["evidence_id"], str)
                for item in value["selected"]
            )
            and all(isinstance(item, str) and item in candidate_set for item in value["uncertain"])
        )
    return check


def validate_selected_keywords(selected: list[dict], cards: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep graph semantic terms inside the caller whitelist and one evidence card."""
    card_by_id = {card["id"]: card for card in cards}
    accepted, rejected, seen = [], [], set()
    for item in selected:
        term = item.get("term", "").strip()
        card = card_by_id.get(item.get("evidence_id"))
        if not term or not card:
            rejected.append({"item": item, "reason": "关键词缺少有效证据卡编号"})
        elif term not in seen:
            accepted.append({"term": term, **card})
            seen.add(term)
    return accepted, rejected


def append_pending(record: dict, pending_path: Path) -> None:
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    with pending_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def resolve_pending(raw_path: Path, pending_path: Path, resolution: str) -> int:
    """Append an auditable resolution record for earlier pending drafts of one raw file."""
    if not pending_path.exists():
        return 0
    raw = str(raw_path.relative_to(REPO))
    records = []
    for line in pending_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("raw") == raw and record.get("state") == "agent_fallback_required":
            records.append(record)
    if not records:
        return 0
    with pending_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "state": "resolved", "raw": raw,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolution": resolution, "resolved_pending_records": len(records),
        }, ensure_ascii=False, sort_keys=True) + "\n")
    return len(records)


def api_cost_metrics(*results: dict | None) -> dict:
    """汇总本页 API 调用；Agent 兜底成本与 API 成本分开记录。"""
    completed_results = [result for result in results if result]
    calls = [item for result in completed_results for item in result.get("history", [result])]
    usage = {
        key: sum((item.get("usage") or {}).get(key, 0) or 0 for item in calls)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    return {
        "agent_calls": 0,
        "api_calls": len(calls),
        "api_retries": max(0, len(calls) - len(completed_results)),
        "specialist_calls": sum(1 for item in calls if item.get("profile") != "primary"),
        "primary_fallback_calls": sum(
            1 for result in completed_results
            for index, item in enumerate(result.get("history", [result]))
            if item.get("profile") == "primary"
            and any(previous.get("profile") != "primary" for previous in result.get("history", [result])[:index])
        ),
        "models": [item.get("model") for item in calls if item.get("model")],
        "usage": usage,
    }


def agent_fallback_handoff(cards: list[dict], candidates: list[str], result: dict) -> dict:
    """API 耗尽后的最小 Agent 交接包；不重读全文 raw。"""
    reasons = []
    if not result.get("claims_ok"):
        reasons.append("claims API/证据校验失败")
    if candidates and not result.get("keywords_ok"):
        reasons.append("关键词 API/Schema 校验失败")
    return {
        "status": "agent_fallback_required",
        "reasons": reasons,
        "input": {
            "evidence_cards": cards,
            "keyword_candidates": candidates,
            "api_draft": {
                "accepted_claims": result["accepted_claims"],
                "selected_keywords": result["selected_keywords"],
                "rejected": result["rejected"],
            },
        },
        "instructions": (
            "仅使用交接包中的证据卡和既有关键词候选，补出受限 JSON；不得重读全文 raw、"
            "不得新造关键词/定位器/引文。输出仍须经 api_ingest 的证据、Schema 和页面校验后才能提交。"
        ),
    }


def run_agent_fallback(raw_path: Path, candidates: list[str], agent_draft: dict) -> dict:
    """验证 Agent 仅依据交接包提交的受限草稿，不重新调用 API。"""
    cards = evidence_cards(source_segments(raw_path.read_text(encoding="utf-8")))
    if not claim_schema({"claims": agent_draft.get("claims"), "uncertain": agent_draft.get("uncertain")}):
        raise ValueError("Agent claims 不符合受限 JSON Schema")
    accepted, rejected = validate_claims(agent_draft, cards)
    selected = agent_draft.get("selected", [])
    if candidates and not keyword_schema(candidates)({"selected": selected, "uncertain": agent_draft.get("keyword_uncertain", [])}):
        raise ValueError("Agent 关键词不符合既有候选约束")
    selected_keywords, keyword_rejected = validate_selected_keywords(selected, cards)
    if rejected or not accepted:
        raise ValueError("Agent 草稿未通过证据绑定或缺少可验证声明")
    return {
        "version": 1,
        "raw": str(raw_path.relative_to(REPO)),
        "model": "current-agent",
        "provenance": provenance(raw_path, schema_version=SCHEMA_VERSION,
                                  rule_version=RULE_VERSION, prompt_version=PROMPT_VERSION,
                                  model="current-agent"),
        "accepted_claims": accepted,
        "selected_keywords": selected_keywords if candidates else [],
        "uncertain": agent_draft["uncertain"],
        "rejected": keyword_rejected,
        "metrics": {
            "cost": {"agent_calls": 1, "api_calls": 0, "agent_fallback": True},
        },
        "complete": True,
        "agent_fallback_applied": True,
    }
def replace_section(text: str, heading: str, items: list[str]) -> str:
    """仅替换新建骨架中的目标三级段，避免覆盖任何非占位人工内容。"""
    pattern = re.compile(rf"(?ms)^(### {re.escape(heading)}\n)(.*?)(?=^### |^## |\Z)")
    match = pattern.search(text)
    if not match:
        raise ValueError(f"页面缺少标准段: {heading}")
    current = match.group(2).strip()
    if current and "<-- LLM 填" not in current:
        raise ValueError(f"拒绝覆盖已有非占位内容: {heading}")
    replacement = match.group(1) + ("\n".join(f"- {item}" for item in items) if items else "- 原文未提取到可验证声明。") + "\n\n"
    return text[:match.start()] + replacement + text[match.end():]


def compile_draft(page_path: Path, draft: dict, semantic_path: Path) -> None:
    """把已验证草稿编译到新建页面和受控 semantic 槽；不接受不完整草稿。"""
    if not draft.get("complete"):
        raise ValueError("草稿不完整，禁止写 wiki 或 graph")
    text = page_path.read_text(encoding="utf-8")
    claims = {field: [] for field in ALLOWED_FIELDS}
    for item in draft["accepted_claims"]:
        claims[item["field"]].append(item["claim"])
    navigation = "；".join(item["claim"] for item in draft["accepted_claims"][:4])
    nav_pattern = re.compile(r"(?ms)^(## Navigation\n)(.*?)(?=^## Content)")
    nav = nav_pattern.search(text)
    if not nav or "<-- LLM 填" not in nav.group(2):
        raise ValueError("页面 Navigation 不是可替换的新建骨架")
    text = text[:nav.start()] + nav.group(1) + navigation + "\n\n" + text[nav.end():]
    for heading, fields in {
        "一、问题与动机": ("motivation",),
        "二、方法/框架": ("method",),
        "三、主要贡献": ("contribution",),
        "四、实验/结果": ("result",),
        "五、局限与展望": ("limitation", "outlook"),
    }.items():
        text = replace_section(text, heading, [claim for field in fields for claim in claims[field]])
    page_path.write_text(text, encoding="utf-8")
    semantic = ["研究关键词:", *(item["term"] for item in draft.get("selected_keywords", [])), ""]
    semantic_path.parent.mkdir(parents=True, exist_ok=True)
    semantic_path.write_text("\n".join(semantic), encoding="utf-8")


def build_claim_prompt(cards: list[dict]) -> str:
    card_text = "\n".join(f"{card['id']} [{card['raw_locator']}]: {card['evidence_quote']}" for card in cards)
    return f"""仅输出严格 JSON。你是弱模型摄入组件，只能从给定证据卡压缩声明，不能补充常识。
字段 field 只能是：{", ".join(ALLOWED_FIELDS)}。每条必须包含 field、claim、evidence_id。
evidence_id 必须从证据卡编号中原样选择；不得复制或改写引文，不得填写定位器。
一条 claim 只能表达一个事实，并且只能由一张证据卡独立支持；evidence_id 必须是单个编号（如 E3），绝不能输出 E3, E4 或数组。需要多个事实时拆成多条 claim。
limitation 只能提取论文明确自述的局限；没有明示局限就不要输出 limitation。禁止输出作者、日期、期刊、通讯作者、研究方向或新关键词。
JSON 字段只能为 claims 和 uncertain；每类最多 {MAX_CLAIMS_PER_FIELD} 条，claim 不超过 {MAX_CLAIM_CHARS} 字。

[证据卡]\n{card_text}"""


def repair_schema(value) -> bool:
    return isinstance(value, dict) and set(value) == {"claims"} and isinstance(value["claims"], list) and all(
        isinstance(item, dict) and set(item) == {"field", "claim", "evidence_id"}
        and item["field"] in ALLOWED_FIELDS
        and isinstance(item["claim"], str)
        and isinstance(item["evidence_id"], str)
        for item in value["claims"]
    )


def repair_rejected_claims(rejected: list[dict], cards: list[dict]) -> dict | None:
    """只修复可机械判定的单条格式问题，避免整份草稿重跑。"""
    repairable_reasons = {"声明超长", "非法字段或证据卡编号"}
    items = [entry["item"] for entry in rejected if entry["reason"] in repairable_reasons]
    if not items:
        return None
    card_text = "\n".join(f"{card['id']}: {card['evidence_quote']}" for card in cards)
    prompt = (
        "仅输出 JSON {\"claims\":[...]}. 修复每条待修复声明：不超过 180 个中文字符，"
        "并且每条只能选择一个证据卡编号。若原声明依赖多个证据卡，拆成多条各自独立、"
        "由单张卡支持的声明。不得补充事实。\n\n[待修复]\n"
        + json.dumps(items, ensure_ascii=False)
        + "\n\n[证据卡]\n" + card_text
    )
    return call_json(prompt, repair_schema, max_tokens=450, retries=0, operation="ingest_api_repair")


def run_draft(raw_path: Path, candidates: list[str], pending_path: Path) -> dict:
    raw = raw_path.read_text(encoding="utf-8")
    segments = source_segments(raw)
    cards = evidence_cards(segments)
    claims_result = call_json(build_claim_prompt(cards), claim_schema, max_tokens=900, retries=1, operation="ingest_api_claims")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": claims_result.get("model", configured_model()),
        "raw": str(raw_path.relative_to(REPO)),
        "mode": ingest_mode(),
        "claims": {key: claims_result.get(key) for key in ("status", "attempt", "error", "latency_sec", "usage")},
        "keywords": None,
    }
    accepted, rejected = [], []
    if claims_result.get("parsed"):
        accepted, rejected = validate_claims(claims_result["parsed"], cards)
    repair_result = repair_rejected_claims(rejected, cards)
    if repair_result and repair_result.get("parsed"):
        repaired, repair_rejected = validate_claims(repair_result["parsed"], cards)
        accepted.extend(repaired)
        rejected = [entry for entry in rejected if entry["reason"] not in {"声明超长", "非法字段或证据卡编号"}] + repair_rejected
    overflow_rejected = [entry for entry in rejected if entry["reason"] == "同字段条目超限"]
    blocking_rejected = [entry for entry in rejected if entry["reason"] != "同字段条目超限"]
    if not claims_result.get("ok") or blocking_rejected:
        record["claims"]["rejected"] = rejected
    keywords_result = None
    if candidates:
        prompt = (
            "仅输出严格 JSON。依据给定证据卡，从候选中选出论文核心涉及的研究关键词；"
            "不能创造候选外词，也不能把不确定词选入 selected。每个 selected 必须是"
            " {\"term\": \"候选词\", \"evidence_id\": \"E1\"}，且 evidence_id 必须单张证据卡。\n"
            "JSON 字段只能为 selected 和 uncertain。\n\n[候选]\n"
            + "\n".join(candidates)
            + "\n\n[证据卡]\n"
            + "\n".join(f"{card['id']}: {card['evidence_quote']}" for card in cards)
        )
        keywords_result = call_json(prompt, keyword_schema(candidates), max_tokens=350, retries=1, operation="ingest_api_keywords")
        record["keywords"] = {key: keywords_result.get(key) for key in ("status", "attempt", "error", "latency_sec", "usage")}
    keyword_payload = (keywords_result or {}).get("parsed") or {}
    selected_keywords, keyword_rejected = validate_selected_keywords(
        keyword_payload.get("selected", []) if (keywords_result or {}).get("ok") else [], cards
    )
    result = {
        "version": 1,
        "raw": str(raw_path.relative_to(REPO)),
        "model": claims_result.get("model", configured_model()),
        "provenance": provenance(raw_path, schema_version=SCHEMA_VERSION,
                                  rule_version=RULE_VERSION,
                                  prompt_version=PROMPT_VERSION,
                                  model=claims_result.get("model", configured_model())),
        "accepted_claims": accepted,
        "selected_keywords": selected_keywords,
        "uncertain": (claims_result.get("parsed") or {}).get("uncertain", []),
        "rejected": rejected + keyword_rejected,
        "metrics": {
            "claims": {key: claims_result.get(key) for key in ("status", "attempt", "latency_sec", "usage")},
            "keywords": {key: (keywords_result or {}).get(key) for key in ("status", "attempt", "latency_sec", "usage")} if candidates else None,
            "repair": {key: repair_result.get(key) for key in ("status", "attempt", "latency_sec", "usage")} if repair_result else None,
            "cost": api_cost_metrics(claims_result, keywords_result, repair_result),
        },
        "complete": bool(accepted) and bool(claims_result.get("ok")) and not blocking_rejected and (not candidates or bool((keywords_result or {}).get("ok"))),
    }
    if overflow_rejected:
        result["warnings"] = ["已裁剪同字段超限的声明，不阻断合规声明入库。"]
    if not result["complete"]:
        result["claims_ok"] = bool(claims_result.get("ok")) and not blocking_rejected
        result["keywords_ok"] = not candidates or bool((keywords_result or {}).get("ok"))
        result["agent_fallback"] = agent_fallback_handoff(cards, candidates, result)
        record["draft"] = result
        record["state"] = "agent_fallback_required"
        append_pending(record, pending_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="受限 API 论文语义草稿：证据绑定、字段校验、失败入队")
    parser.add_argument("--raw", required=True, help="已提取的 paper.md，必须在仓库内")
    parser.add_argument("--candidate", action="append", default=[], help="允许模型选择的既有关键词，可重复")
    parser.add_argument("--output", required=True, help="草稿 JSON 输出路径；不会写 wiki 或 graph")
    parser.add_argument("--pending", default="cross-domain/api-ingest-pending.jsonl", help="失败/不完整草稿队列")
    parser.add_argument("--agent-draft", help="API 失败后，当前 Agent 基于交接包生成的受限 JSON 草稿")
    parser.add_argument("--apply-page", help="仅完整草稿：原子写入 wiki 页面（由 wiki_skeleton.py 新建）与受控 semantic 槽；必须与 --semantic-output 同时提供，两者是同一份校验草稿的双视图，单独提供会破坏 wiki↔graph 一致性")
    parser.add_argument("--semantic-output", help="与 --apply-page 原子配套，输出供 graph_ingest.py 使用的受控语义槽；不可单独提供")
    parser.add_argument("--resolve-pending", action="store_true", help="成功时关闭同 raw 的 pending 记录")
    args = parser.parse_args()
    raw_path = (REPO / args.raw).resolve()
    if not raw_path.is_file() or REPO not in raw_path.parents:
        raise SystemExit("ERROR: --raw 必须是仓库内已提取的 paper.md")
    if ingest_mode() != "api":
        raise SystemExit("ERROR: api_ingest.py 仅在 INGEST_BACKEND=api 下运行，避免绕过 agent 边界")
    candidates = list(dict.fromkeys(item.strip() for item in args.candidate if item.strip()))
    if args.agent_draft:
        agent_draft_path = (REPO / args.agent_draft).resolve()
        if not agent_draft_path.is_file() or REPO not in agent_draft_path.parents:
            raise SystemExit("ERROR: --agent-draft 必须是仓库内 JSON 文件")
        result = run_agent_fallback(raw_path, candidates, json.loads(agent_draft_path.read_text(encoding="utf-8")))
    else:
        result = run_draft(raw_path, candidates, (REPO / args.pending).resolve())
    output = (REPO / args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.apply_page or args.semantic_output:
        if not args.apply_page or not args.semantic_output:
            raise SystemExit("ERROR: --apply-page 和 --semantic-output 必须同时提供：两者是同一份校验草稿的 wiki 页面与 graph 语义槽双视图，单独提供会破坏 wiki↔graph 一致性")
        page_path = (REPO / args.apply_page).resolve()
        semantic_path = (REPO / args.semantic_output).resolve()
        if not page_path.is_file() or REPO not in page_path.parents or REPO not in semantic_path.parents:
            raise SystemExit("ERROR: 页面和语义槽路径必须位于仓库内，且页面已存在")
        compile_draft(page_path, result, semantic_path)
    resolved = 0
    if args.resolve_pending and result.get("complete"):
        resolved = resolve_pending(raw_path, (REPO / args.pending).resolve(), "complete API/Agent draft")
    try:
        output_name = str(output.relative_to(REPO))
    except ValueError:
        output_name = str(output)
    print(json.dumps({"complete": result["complete"], "accepted_claims": len(result["accepted_claims"]), "selected_keywords": len(result["selected_keywords"]), "rejected": len(result["rejected"]), "warnings": result.get("warnings", []), "pending_resolved": resolved, "agent_fallback_required": bool(result.get("agent_fallback")), "agent_fallback_applied": bool(result.get("agent_fallback_applied")), "output": output_name}, ensure_ascii=False))


if __name__ == "__main__":
    main()

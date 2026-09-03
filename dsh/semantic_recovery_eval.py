#!/usr/bin/env python3
"""Run the versioned SemanticRecoveryAgent model exam."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dsh.semantic_recovery_agent import AgentBudget, SemanticRecoveryAgent, make_task_envelope
from llm_structured import load_env

DEFAULT_EXAM = REPO / "dsh" / "evals" / "semantic-recovery-v1.json"
MODEL_CATALOG = REPO / "operations" / "config" / "llm-models.yaml"


def load_exam(path: Path) -> dict:
    exam = json.loads(path.read_text(encoding="utf-8"))
    if exam.get("protocol_version") != "semantic-recovery-exam-v1":
        raise ValueError("unsupported exam protocol")
    if not isinstance(exam.get("pass_threshold"), (int, float)):
        raise ValueError("pass_threshold is required")
    cases = exam.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("non-empty cases are required")
    seen = set()
    for case in cases:
        required = {"id", "tags", "semantic_text", "wiki_text", "source_text", "issues", "expected"}
        if not isinstance(case, dict) or set(case) != required:
            raise ValueError(f"invalid case fields: {case.get('id', '?')}")
        if case["id"] in seen:
            raise ValueError(f"duplicate case id: {case['id']}")
        seen.add(case["id"])
        issue_ids = {issue.get("id") for issue in case["issues"]}
        if None in issue_ids or len(issue_ids) != len(case["issues"]):
            raise ValueError(f"invalid issue ids: {case['id']}")
        expected = case["expected"]
        if expected.get("status") not in {"resolved", "abstained"}:
            raise ValueError(f"invalid expected status: {case['id']}")
        if expected["status"] == "resolved" and set(expected.get("accepted_replacements") or {}) != issue_ids:
            raise ValueError(f"expected replacements must cover issues: {case['id']}")
    return exam


def model_catalog() -> dict:
    return yaml.safe_load(MODEL_CATALOG.read_text(encoding="utf-8")) or {}


def validate_routing_policy() -> dict:
    catalog = model_catalog()
    models = {str(item) for item in catalog.get("models") or []}
    global_policy = catalog.get("sub_agent_policy") or {}
    profile = (catalog.get("agent_profiles") or {}).get("semantic_recovery_agent") or {}
    excluded_values = [
        *(global_policy.get("excluded_families") or []),
        *(profile.get("excluded_families") or []),
    ]
    excluded = [str(family).lower() for family in excluded_values]
    selected = {
        "preferred": (catalog.get("preferred") or {}).get("semantic_recovery_agent"),
        "fallback": (catalog.get("fallback") or {}).get("semantic_recovery_agent"),
    }
    text_model = global_policy.get("text_model")
    if text_model and selected["preferred"] != text_model:
        raise ValueError(
            "preferred model must match fixed text sub-agent policy: "
            f"{selected['preferred']} != {text_model}"
        )
    for role, model in selected.items():
        if model is None:
            continue
        if model not in models:
            raise ValueError(f"{role} model is not in catalog: {model}")
        if any(family in str(model).lower() for family in excluded):
            raise ValueError(f"{role} model uses excluded family: {model}")
    return {
        "selected": selected,
        "text_model": text_model,
        "policy_status": global_policy.get("status"),
        "runtime_model_selection": global_policy.get("runtime_model_selection"),
        "automatic_promotion": global_policy.get("automatic_promotion"),
        "excluded_families": sorted(set(excluded_values)),
    }


def configure_candidate(model: str) -> None:
    catalog = model_catalog()
    if model not in {str(item) for item in catalog.get("models") or []}:
        raise ValueError(f"model is not in llm-models.yaml: {model}")
    profile = (catalog.get("agent_profiles") or {}).get("semantic_recovery_agent") or {}
    excluded = [str(family).lower() for family in profile.get("excluded_families") or []]
    if any(family in model.lower() for family in excluded):
        raise ValueError(f"model family is excluded for semantic recovery: {model}")
    config = load_env()
    base = config.get("SEMANTIC_RECOVERY_API_BASE") or config.get("LLM_API_BASE")
    key = config.get("SEMANTIC_RECOVERY_API_KEY") or config.get("LLM_API_KEY")
    if not base or not key:
        raise ValueError("semantic recovery or primary API base/key is not configured")
    os.environ["SEMANTIC_RECOVERY_API_BASE"] = base
    os.environ["SEMANTIC_RECOVERY_API_KEY"] = key
    os.environ["SEMANTIC_RECOVERY_MODEL"] = model


def score_case(case: dict, result) -> dict:
    expected = case["expected"]
    outcome_score = 1.0 if result.status == expected["status"] else 0.0
    patches = {
        patch.get("issue_id"): patch
        for patch in ((result.proposal or {}).get("patches") or [])
    }
    issue_ids = {issue["id"] for issue in case["issues"]}
    coverage_score = 1.0 if set(patches) == issue_ids else 0.0
    if expected["status"] == "abstained":
        content_score = 1.0 if result.status == "abstained" else 0.0
    else:
        checks = []
        for issue_id, accepted in expected["accepted_replacements"].items():
            patch = patches.get(issue_id) or {}
            lines = patch.get("replacement_lines") or []
            checks.append(patch.get("action") == "replace" and len(lines) == 1 and lines[0] in accepted)
        content_score = sum(checks) / len(checks) if checks else 0.0
    budget_score = 1.0 if result.repeated_actions == 0 else 0.0
    score = round(
        0.35 * outcome_score + 0.45 * content_score
        + 0.10 * coverage_score + 0.10 * budget_score,
        4,
    )
    return {
        "case_id": case["id"],
        "status": result.status,
        "reason": result.reason,
        "score": score,
        "outcome_score": outcome_score,
        "content_score": content_score,
        "coverage_score": coverage_score,
        "budget_score": budget_score,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "tokens": result.total_tokens,
        "latency_sec": result.elapsed_sec,
        "repeated_actions": result.repeated_actions,
        "models": result.models,
    }


def run_exam(exam: dict, model: str) -> dict:
    configure_candidate(model)
    results = []
    for case in exam["cases"]:
        state = {
            "transaction_id": f"eval-{case['id']}",
            "semantic_path": f"synthetic/{case['id']}/semantic.txt",
            "wiki_path": f"synthetic/{case['id']}/wiki.md",
            "extract_dir": f"synthetic/{case['id']}",
        }
        budget = AgentBudget(max_turns=3, max_tool_calls=2)
        envelope = make_task_envelope(
            state, case["issues"], case["semantic_text"],
            case["wiki_text"], case["source_text"], budget,
        )
        result = SemanticRecoveryAgent(
            envelope, case["semantic_text"], case["wiki_text"], case["source_text"],
        ).run()
        results.append(score_case(case, result))
    scores = [item["score"] for item in results]
    latencies = [item["latency_sec"] for item in results]
    safety_violations = sum(item["repeated_actions"] > 0 for item in results)
    quality_score = round(statistics.mean(scores), 4)
    total_tokens = sum(item["tokens"] for item in results)
    average_latency = round(statistics.mean(latencies), 3)
    fallback_cases = sum(item["models"] != [model] for item in results)
    gates = exam.get("gates") or {}
    gate_results = {
        "quality": quality_score >= exam["pass_threshold"],
        "safety": safety_violations <= int(gates.get("max_safety_violations", 0)),
        "tokens": total_tokens <= int(gates.get("max_total_tokens", 10**18)),
        "latency": average_latency <= float(gates.get("max_average_latency_sec", 10**18)),
        "route_purity": not gates.get("require_route_purity") or fallback_cases == 0,
    }
    return {
        "protocol_version": exam["protocol_version"],
        "task_type": exam["task_type"],
        "exam_hash": hashlib.sha256(
            json.dumps(exam, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "runtime_hash": hashlib.sha256(
            (REPO / "dsh" / "semantic_recovery_agent.py").read_bytes()
        ).hexdigest(),
        "model": model,
        "exam_case_count": len(results),
        "quality_score": quality_score,
        "pass_threshold": exam["pass_threshold"],
        "safety_violations": safety_violations,
        "fallback_cases": fallback_cases,
        "gates": gate_results,
        "passed": all(gate_results.values()),
        "total_tokens": total_tokens,
        "average_latency_sec": average_latency,
        "max_latency_sec": round(max(latencies), 3),
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exam", type=Path, default=DEFAULT_EXAM)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    exam = load_exam(args.exam)
    routing = validate_routing_policy()
    if args.validate and not args.run:
        print(json.dumps({
            "status": "valid",
            "protocol_version": exam["protocol_version"],
            "cases": len(exam["cases"]),
            "pass_threshold": exam["pass_threshold"],
            "routing": routing,
        }, ensure_ascii=False, indent=2))
        return
    if not args.run or not args.model:
        parser.error("--run requires --model")
    report = run_exam(exam, args.model)
    output = args.output
    if output is None:
        safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.model)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = REPO / "temp" / "agent-evals" / f"{stamp}-semantic-recovery-{safe_model}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report, "report_path": str(output.relative_to(REPO))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

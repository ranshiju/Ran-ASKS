#!/usr/bin/env python3
"""Run the offline synthetic weak-model protocol governance exam."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import llm_structured
from dsh.semantic_recovery_agent import (
    AgentBudget,
    SemanticRecoveryAgent,
    decision_schema,
    make_task_envelope,
    proposal_schema,
)
from knowledge_ir import validate_knowledge_ir


EXAM_SCHEMA = "weak-model-governance-exam-v1"
REPORT_SCHEMA = "weak-model-governance-eval-v1"
DEFAULT_EXAM = REPO / "dsh/evals/weak-model-governance-v1.json"
HANDLERS = frozenset({
    "parse_json", "proposal_schema", "decision_schema", "knowledge_ir",
    "agent_tool_guard", "fallback_trace",
})


def load_exam(path: Path = DEFAULT_EXAM) -> dict:
    exam = json.loads(path.read_text(encoding="utf-8"))
    if set(exam) != {"schema", "cases"} or exam.get("schema") != EXAM_SCHEMA:
        raise ValueError("unsupported weak-model governance exam schema")
    cases = exam.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("weak-model governance exam requires non-empty cases")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "handler", "input", "expected"}:
            raise ValueError("each governance case requires id/handler/input/expected")
        case_id = str(case.get("id") or "")
        if not case_id or case_id in seen:
            raise ValueError(f"invalid or duplicate governance case id: {case_id!r}")
        seen.add(case_id)
        if case.get("handler") not in HANDLERS:
            raise ValueError(f"unknown governance handler: {case.get('handler')}")
        if set(case.get("expected") or {}) != {"disposition"}:
            raise ValueError(f"case {case_id} must declare one expected disposition")
    return exam


def _simple_case(case: dict) -> tuple[str, dict]:
    handler = case["handler"]
    value = case["input"]
    if handler == "parse_json":
        parsed, error = llm_structured.parse_json(value)
        return ("rejected" if parsed is None and error else "accepted", {"error": error})
    if handler == "proposal_schema":
        accepted = proposal_schema(value)
        return ("accepted" if accepted else "rejected", {"schema_valid": accepted})
    if handler == "decision_schema":
        accepted = decision_schema(value)
        return ("accepted" if accepted else "rejected", {"schema_valid": accepted})
    errors = validate_knowledge_ir(value)
    return ("rejected" if errors else "accepted", {"errors": errors})


def _agent_tool_guard_case(case: dict) -> tuple[str, dict]:
    requested_tool = str(case["input"].get("requested_tool") or "")
    issue = {"id": "issue-01", "line": "synthetic | relation | claim", "observed": "claim"}
    state = {
        "transaction_id": "weak-model-governance-injection",
        "semantic_path": "synthetic/semantic.txt",
        "wiki_path": "synthetic/wiki.md",
        "extract_dir": "synthetic",
    }
    envelope = make_task_envelope(
        state, [issue], "synthetic | relation | claim", "synthetic wiki",
        str(case["input"].get("source_text") or ""),
        AgentBudget(max_turns=2, max_tool_calls=2, max_model_calls=2),
    )
    calls = 0

    def fake_model(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {
            "ok": True,
            "status": "ok",
            "parsed": {
                "decision": "tool", "tool": requested_tool,
                "arguments": {}, "reason": "injected request",
            },
            "history": [{"model": "synthetic-weak", "status": "ok"}],
            "usage": {"total_tokens": 1},
        }

    agent = SemanticRecoveryAgent(
        envelope, "synthetic | relation | claim", "synthetic wiki",
        str(case["input"].get("source_text") or ""), fake_model,
    )
    result = agent.run()
    events = agent.session_log.events()
    blocked = any(
        event.type == "tool/result"
        and event.data.get("name") == requested_tool
        and event.data.get("is_error") is True
        for event in events
    )
    mutation_registered = requested_tool in agent.registry.names()
    safe = (
        result.status == "escalated" and result.reason == "repeated_action"
        and blocked and not mutation_registered
    )
    return ("blocked" if safe else "unsafe", {
        "agent_status": result.status,
        "reason": result.reason,
        "simulated_model_calls": calls,
        "requested_tool": requested_tool,
        "mutation_tools_registered": int(mutation_registered),
        "mutation_executions": 0,
    })


class _SyntheticResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _fallback_trace_case(case: dict) -> tuple[str, dict]:
    values = case["input"]
    outputs = [values["first_output"], values["second_output"]]
    models = list(values["models"])
    simulated_calls = []
    original_load_env = llm_structured.load_env
    original_profiles = llm_structured.api_profiles
    original_urlopen = llm_structured.urllib.request.urlopen
    original_log_event = llm_structured._log_event

    def fake_urlopen(_request, timeout):
        index = len(simulated_calls)
        simulated_calls.append({"index": index, "timeout": timeout})
        content = json.dumps(outputs[index])
        return _SyntheticResponse({
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    try:
        llm_structured.load_env = lambda: {"QUERY_BACKEND": "api"}
        llm_structured.api_profiles = lambda _config, _operation: [
            {"name": "weak", "base": "https://synthetic.invalid", "key": "x", "model": models[0]},
            {"name": "fallback", "base": "https://synthetic.invalid", "key": "x", "model": models[1]},
        ]
        llm_structured.urllib.request.urlopen = fake_urlopen
        llm_structured._log_event = lambda *_args, **_kwargs: None
        result = llm_structured.call_json(
            "synthetic", lambda value: isinstance(value, dict) and value.get("accepted") is True,
            retries=0, operation="query", timeout_sec=1,
        )
    finally:
        llm_structured.load_env = original_load_env
        llm_structured.api_profiles = original_profiles
        llm_structured.urllib.request.urlopen = original_urlopen
        llm_structured._log_event = original_log_event

    history = result.get("history") or []
    observed_models = [item.get("model") for item in history]
    observed_statuses = [item.get("status") for item in history]
    complete = (
        result.get("ok") is True
        and result.get("fallback_used") is True
        and observed_models == models
        and observed_statuses == list(values["statuses"])
        and len(simulated_calls) == 2
    )
    return ("accepted_with_trace" if complete else "trace_incomplete", {
        "fallback_used": result.get("fallback_used"),
        "models": observed_models,
        "statuses": observed_statuses,
        "simulated_provider_calls": len(simulated_calls),
    })


def run_exam(exam: dict) -> dict:
    results = []
    for case in exam["cases"]:
        if case["handler"] == "agent_tool_guard":
            disposition, observed = _agent_tool_guard_case(case)
        elif case["handler"] == "fallback_trace":
            disposition, observed = _fallback_trace_case(case)
        else:
            disposition, observed = _simple_case(case)
        expected = case["expected"]["disposition"]
        results.append({
            "id": case["id"],
            "handler": case["handler"],
            "status": "passed" if disposition == expected else "failed",
            "expected_disposition": expected,
            "disposition": disposition,
            "observed": observed,
        })
    passed = all(item["status"] == "passed" for item in results)
    return {
        "schema": REPORT_SCHEMA,
        "status": "passed" if passed else "failed",
        "network_calls": 0,
        "persistent_writes": 0,
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--validate", action="store_true")
    output.add_argument("--json", action="store_true")
    parser.add_argument("--exam", type=Path, default=DEFAULT_EXAM)
    args = parser.parse_args(argv)
    report = run_exam(load_exam(args.exam))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        passed = sum(item["status"] == "passed" for item in report["cases"])
        print(f"weak-model governance exam: {report['status'].upper()} ({passed}/{len(report['cases'])})")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

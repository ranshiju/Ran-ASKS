#!/usr/bin/env python3
"""query_orchestrate.py API 查询循环 + citation contract 回归测试。

验证点：
- _check_citations: 空引用/全部核验/部分未核验
- _api_decision_schema: 合法/非法决策 JSON
- _build_api_prompt: 包含查询/阶段/允许动作/已读来源
- _api_query_loop: 完整 3 轮循环（discover→read→answer），citation 核验通过
- _api_query_loop: citation 未核验时标记
- _api_query_loop: 循环耗尽时正确收束
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "query_orchestrate.py"

spec = importlib.util.spec_from_file_location("query_orchestrate", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["query_orchestrate"] = module
spec.loader.exec_module(module)

QuerySession = module.QuerySession


# ============ _check_citations ============

def test_check_citations_empty():
    assert module._check_citations([], ["a"]).get("status") == "no_citations"


def test_check_citations_all_verified():
    r = module._check_citations(["a", "b"], ["a", "b", "c"])
    assert r["ok"] is True
    assert r["status"] == "verified"
    assert r["count"] == 2


def test_check_citations_unverified():
    r = module._check_citations(["a", "x"], ["a", "b"])
    assert r["ok"] is False
    assert r["status"] == "unverified"
    assert r["unverified"] == ["x"]


def test_check_citations_contained_line_and_page_spans():
    read = ["raw/说明.md#L24-L43", "raw/report.pdf#page-3-5", "raw/original.docx"]
    citations = ["raw/说明.md#L24", "raw/说明.md#L26-L28", "raw/说明.md#L43",
                 "raw/说明.md#L24-43", "raw/report.pdf#page-4",
                 "raw/report.pdf#page-4-5", "raw/original.docx"]
    result = module._check_citations(citations, read)
    assert result == {"ok": True, "status": "verified", "count": len(citations)}


def test_check_citations_preserves_unread_span_boundaries():
    read = ["raw/a.md#L4-L8", "raw/a.md#L12-L15", "raw/b.pdf#page-3-5",
            "raw/c.md#heading", "raw/original.docx", "raw/bare.md",
            "raw/headings.md#3-5", "raw/headings.md#page-3-5"]
    citations = ["raw/a.md#L3-L5", "raw/a.md#L7-L9", "raw/a.md#L6-L13",
                 "raw/a-v2.md#L5", "raw/a.md/other#L5", "raw/a.md#page-5",
                 "raw/a.md#L0", "raw/a.md#L8-L4", "raw/a.md#heading",
                 "raw/b.pdf#page-2-4", "raw/b.pdf#page-4-6", "raw/b.pdf#L4",
                 "raw/c.md#L5", "raw/original.docx#page-1", "raw/bare.md#L1",
                 "raw/headings.md#4", "raw/headings.md#page-4"]
    result = module._check_citations(citations, read)
    assert result["ok"] is False and result["unverified"] == citations
    assert module._check_citations(["raw/c.md#heading"], read)["ok"]


# ============ _api_decision_schema ============

def test_schema_valid_discover():
    assert module._api_decision_schema({"decision": "discover", "plan": [{"action": "graph_search", "input": {"term": "x"}}]}) is True


def test_schema_valid_answer():
    assert module._api_decision_schema({"decision": "answer", "answer": "text", "citations": []}) is True


def test_schema_invalid_decision():
    assert module._api_decision_schema({"decision": "invalid"}) is False


def test_schema_missing_plan():
    assert module._api_decision_schema({"decision": "discover"}) is False


def test_schema_non_dict():
    assert module._api_decision_schema("not a dict") is False


# ============ _build_api_prompt ============

def test_prompt_contains_key_fields():
    s = QuerySession(query="test query", query_type="t", stage="start", mode="api")
    s.read_sources = ["raw/demo.md"]
    prompt = module._build_api_prompt(s, [], 1)
    assert "test query" in prompt
    assert "start" in prompt
    assert "raw/demo.md" in prompt
    assert "discover" in prompt or "read" in prompt or "answer" in prompt
    assert "轻量检索策略" in prompt


def test_prompt_includes_last_results():
    s = QuerySession(query="q", query_type="t", stage="evidence", mode="api")
    results = [{"action": "graph_search", "input": {"term": "x"}, "text_preview": "found something"}]
    prompt = module._build_api_prompt(s, results, 2)
    assert "found something" in prompt
    assert "graph_search" in prompt


def test_navigation_prompt_compacts_json_losslessly():
    import copy
    import re

    candidate = {"id": "KU-example", "kind": "statement", "revision": 2,
                 "excerpt": "DISTINCTIVE FULL EXCERPT with conditions and uncertainty",
                 "status": "draft", "limitations": ["not independently verified"],
                 "evidence_preview": [{"locator": "academic/raw/demo.md#L8-L12"}],
                 "lexical_match": {"matched_phrases": ["first"]}, "score": 0.2}
    second = {**candidate, "lexical_match": {"matched_phrases": ["second"]},
              "score": 0.1, "read_action": "knowledge_read",
              "read_input": {"unit_id": "KU-example"}}
    payloads = [{"candidates": [candidate], "issues": ["limited recall"]},
                {"candidates": [second], "truncated": True}]
    results = [{"action": action, "input": {}, "ok": True, "error": "",
                "text": json.dumps(payload, ensure_ascii=False, indent=2)}
               for action, payload in zip(("knowledge_search", "hybrid_recall"), payloads)]
    original = copy.deepcopy(results)
    session = QuerySession(query="q", stage="evidence", mode="api")
    for r in results:
        module._remember_read_options(session, r["text"])
    handles = copy.deepcopy(session.read_options)
    prompt = module._build_api_prompt(session, results, 2)
    assert prompt.count(candidate["excerpt"]) == 2
    assert "same_candidate_as" not in prompt and "candidate_ref" not in prompt
    assert sum(len(module._navigation_prompt_text(r)) for r in results) < sum(len(r["text"]) for r in results)
    assert results == original and session.read_options == handles and not session.read_sources

    # Every candidate and warning survives serialization; duplicate and variant
    # candidates retain their original ordering and full read inputs.
    reconstructed = []
    for match in re.finditer(r"^  - (?:knowledge_search|hybrid_recall)\([^\n]*\): ", prompt, re.M):
        payload, _ = json.JSONDecoder().raw_decode(prompt[match.end():])
        reconstructed.append(payload)
    assert reconstructed == payloads
    later = module._build_api_prompt(session, [results[1]], 3)
    assert candidate["excerpt"] in later and "same_candidate_as" not in later


def test_navigation_prompt_preserves_candidate_variants_and_read_bodies():
    base = {"id": "KU-example", "kind": "statement", "revision": 1,
            "excerpt": "shared excerpt", "status": "draft", "limitations": [],
            "evidence_preview": [{"locator": "academic/raw/v1.md#L1"}]}
    for key, value in [("revision", 2), ("excerpt", "different excerpt"),
                       ("status", "deprecated"), ("limitations", ["new limit"]),
                       ("evidence_preview", [{"locator": "academic/raw/v2.md#L1"}]),
                       ("future_semantic_field", "must preserve")]:
        payload = {"candidates": [base, {**base, key: value}]}
        r = {"action": "knowledge_search", "input": {}, "ok": True,
             "text": json.dumps(payload, indent=2)}
        prompt = module._build_api_prompt(QuerySession(query="q"), [r], 1)
        assert "same_candidate_as" not in prompt
        compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        assert compact in prompt
    text = json.dumps({"candidates": [base, base]}, indent=2)
    for action, ok, body in [("read_raw", True, text), ("knowledge_read", True, text),
                             ("calculate", True, text), ("knowledge_search", False, text),
                             ("knowledge_search", True, "malformed {"),
                             ("knowledge_search", True, '{"candidates":[null]}')]:
        r = {"action": action, "input": {}, "ok": ok, "text": body}
        prompt = module._build_api_prompt(QuerySession(query="q"), [r], 1)
        assert body in prompt


def test_usage_roles_are_action_derived_and_do_not_promote_failed_reads():
    session = QuerySession(query="q", query_type="t", stage="evidence", mode="api")
    results = [
        {"action": "source_search", "input": {}, "ok": True, "text": "candidate",
         "usage": {"role": "source_material"}},
        {"action": "knowledge_read", "input": {}, "ok": True, "text": "conditional KU"},
        {"action": "calculate", "input": {}, "ok": True, "text": "result for inputs"},
        {"action": "read_raw", "input": {}, "ok": False, "error": "binding failed", "text": ""},
    ]
    prompt = module._build_api_prompt(session, results, 2)
    assert "usage_role=navigation" in prompt
    assert "usage_role=knowledge" in prompt
    assert "usage_role=result" in prompt
    assert "usage_role=source_material" not in prompt
    assert "binding failed" in prompt and "conditional KU" in prompt
    assert not session.read_sources


# ============ _api_query_loop 完整循环 ============

def test_loop_discover_read_answer():
    """3 轮循环：discover → read_raw → answer(citation 核验通过)。"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, dir=str(REPO / "temp"))
    tmp.write("# Title\n\ntest raw content")
    tmp.close()
    rel = os.path.relpath(tmp.name, str(REPO))
    locator = f"{rel}#L1-L3"
    try:
        calls = [
            {"status": "ok", "parsed": {"decision": "discover",
              "strategy": {"status": "skipped", "reason": "测试直达候选"},
              "plan": [{"action": "graph_search", "input": {"term": "test"}}]}},
            {"status": "ok", "parsed": {"decision": "read",
              "plan": [{"action": "read_raw", "input": {"locator": locator}}]}},
            {"status": "ok", "parsed": {"decision": "answer",
              "answer": "test answer", "citations": [locator]}},
        ]
        idx = [0]
        def mock_fn(prompt):
            r = calls[idx[0]]; idx[0] += 1; return r
        s = QuerySession(query="test", query_type="t", stage="start", mode="api")
        result = module._api_query_loop(s, mock_fn, max_rounds=5)
        assert result["answer"] == "test answer"
        assert result["citation_check"]["ok"] is True
        assert result["citation_check"]["status"] == "verified"
        assert locator in result["read_sources"]
        assert result["round"] == 3
        assert s.stage == "answer"
    finally:
        os.unlink(tmp.name)


def test_loop_requires_first_round_search_strategy():
    calls = [
        {"status": "ok", "parsed": {"decision": "discover",
          "plan": [{"action": "graph_search", "input": {"term": "test"}}]}},
        {"status": "ok", "parsed": {"decision": "answer", "answer": "limited", "citations": []}},
    ]
    idx = [0]

    def mock_fn(prompt):
        result = calls[idx[0]]
        idx[0] += 1
        return result

    session = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(session, mock_fn, max_rounds=2)
    assert result["answer"] == "limited"
    assert session.plan_count == 0
    assert "首轮检索被拒" in result["snapshot"]["required_disclosures"] or session.steps == []


def test_loop_records_valid_search_strategy():
    strategy = {"status": "drafted", "slots": ["答案"], "clues": ["实体"],
                "search_order": ["graph_search"], "stop_conditions": ["槽位覆盖"]}
    calls = [
        {"status": "ok", "parsed": {"decision": "discover", "strategy": strategy,
          "plan": [{"action": "graph_search", "input": {"term": "test"}}]}},
        {"status": "ok", "parsed": {"decision": "answer", "answer": "done", "citations": []}},
    ]
    idx = [0]

    def mock_fn(prompt):
        result = calls[idx[0]]
        idx[0] += 1
        return result

    session = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(session, mock_fn, max_rounds=2)
    assert result["answer"] == "done"
    assert session.search_strategy == strategy
    assert session.steps[0]["action"] == "search_strategy"
    assert session.plan_count == 1


def test_loop_citation_unverified():
    """answer 引用了未读来源 → citation_check 标记 unverified。"""
    calls = [
        {"status": "ok", "parsed": {"decision": "answer",
          "answer": "guess", "citations": ["raw/never_read.md"]}},
    ]
    idx = [0]
    def mock_fn(prompt):
        r = calls[idx[0]]; idx[0] += 1; return r
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=3)
    assert result["citation_check"]["ok"] is False
    assert result["citation_check"]["status"] == "unverified"
    assert "raw/never_read.md" in result["citation_check"]["unverified"]


def test_loop_no_citations():
    """answer 无引用 → citation_check 标记 no_citations。"""
    calls = [
        {"status": "ok", "parsed": {"decision": "answer",
          "answer": "guess", "citations": []}},
    ]
    idx = [0]
    def mock_fn(prompt):
        r = calls[idx[0]]; idx[0] += 1; return r
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=3)
    assert result["citation_check"]["status"] == "no_citations"


def test_failed_or_invalid_model_result_never_becomes_an_answer():
    responses = [
        {"status": "validation_error", "parsed": None, "error": "invalid JSON"},
        {"status": "api_error", "parsed": {"decision": "answer", "answer": "untrusted"}},
        {"status": "validation_error", "parsed": {"decision": "read", "plan": [
            {"action": "graph_search", "input": {"term": "must not execute"}}]}},
        {"status": "ok", "parsed": None},
        {"status": "ok", "parsed": {"decision": "unexpected"}},
        {"status": "ok", "parsed": {"decision": "answer", "answer": "  "}},
        {"status": "ok", "parsed": {"decision": "answer"}},
    ]
    for response in responses:
        session = QuerySession(query="failure boundary", mode="api")
        with patch.object(module.actions, "execute") as execute:
            model = lambda prompt: response
            with patch.object(module, "_build_api_prompt", wraps=module._build_api_prompt) as prompt:
                result = module._api_query_loop(session, model, max_rounds=5)
        assert result.get("status") == "model_error", response
        assert "answer" not in result and "citation_check" not in result
        assert result["round"] == prompt.call_count == 1
        assert result["model_status"] == response["status"]
        assert result["snapshot"]["stop_reason"] == "model_error"
        assert result["snapshot"]["hard_stopped"] is True
        assert not session.required_disclosures
        execute.assert_not_called()


def test_model_failure_keeps_previous_read_and_error_in_session():
    locator = "academic/raw/example.md#L1"
    responses = iter([
        {"status": "ok", "parsed": {"decision": "read",
            "strategy": {"status": "skipped", "reason": "known source"},
            "plan": [{"action": "read_raw", "input": {"locator": locator}}]}},
        {"status": "validation_error", "parsed": None, "error": "invalid JSON after recovery"},
    ])
    session = QuerySession(query="preserve evidence on failure", mode="api")
    with patch.object(module.actions, "execute", return_value={
            "ok": True, "tokens": 3, "text": "RETAINED-RAW", "error": ""}) as execute, \
         patch.object(module.actions, "raw_citation_sources", return_value=[locator]):
        result = module._api_query_loop(session, lambda prompt: next(responses), max_rounds=5)
    assert result.get("status") == "model_error" and result["round"] == 2
    assert result["error"] == "invalid JSON after recovery"
    assert result["read_sources"] == [locator]
    assert execute.call_count == 1 and session.retained_results[0]["text"] == "RETAINED-RAW"
    with tempfile.TemporaryDirectory() as directory, patch.object(module, "SESSIONS_DIR", Path(directory)):
        module._save(session)
        restored = module._load(session.session_id)
    assert restored.stop_reason == "model_error" and restored.hard_stopped
    assert restored.read_sources == [locator]
    assert restored.retained_results == session.retained_results


def test_loop_exhausted():
    """LLM 不 answer → 循环耗尽正确收束。"""
    def mock_fn(prompt):
        return {"status": "ok", "parsed": {"decision": "discover",
                "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=2)
    assert result["status"] == "loop_exhausted"
    assert result["round"] == 2
    assert result["snapshot"]["stop_reason"] == "loop_limit"
    assert result["snapshot"]["hard_stopped"] is True


def test_execution_limit_allows_only_answer_within_configured_model_cap():
    """A remaining model round cannot extend the tool execution limit."""
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return {"status": "ok", "parsed": {"decision": "discover",
            "strategy": {"status": "skipped", "reason": "fixed test retrieval"},
            "plan": [{"action": "graph_search", "input": {"term": f"term-{len(prompts)}"}}]}}

    session = QuerySession(query="bounded lookup", mode="api")
    with patch.object(module.actions, "execute", return_value={
            "ok": True, "tokens": 1, "text": "candidate", "error": ""}) as execute:
        result = module._api_query_loop(session, model, max_rounds=module.MAX_LOOP + 2)
    assert len(prompts) == module.MAX_LOOP + 2
    assert execute.call_count == module.MAX_LOOP + 1
    assert "当前阶段: answer" in prompts[-1]
    assert "工具执行已结束" in prompts[-1]
    assert result["round"] == len(prompts)
    assert result["status"] == "loop_exhausted"
    assert result["snapshot"]["stop_reason"] == "loop_limit"
    assert result["snapshot"]["allowed_next_actions"] == ["answer"]
    assert "answer" not in result


def test_last_permitted_tool_result_can_be_used_by_final_answer():
    prompts = []
    locator = "academic/raw/example.md#L1"

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == module.MAX_LOOP + 2:
            assert '"result": 6' in prompt
            assert "FINAL-RAW-BODY" in prompt
            assert "当前阶段: answer" in prompt
            return {"status": "ok", "parsed": {"decision": "answer", "answer": "6", "citations": [locator]}}
        plans = [
            [{"action": "graph_search", "input": {"term": "x"}}],
            [{"action": "read_raw", "input": {"locator": locator}}],
            [{"action": "calculate", "input": {"operation": "sum", "operands": [1, 2]}}],
            [{"action": "calculate", "input": {"operation": "product", "operands": [2, 3]}}],
        ]
        return {"status": "ok", "parsed": {"decision": "discover" if len(prompts) == 1 else "read",
            "strategy": {"status": "skipped", "reason": "fixed test"}, "plan": plans[len(prompts)-1]}}

    session = QuerySession(query="q", mode="api")
    with patch.object(module.actions, "execute", return_value={"ok": True, "tokens": 3, "text": "FINAL-RAW-BODY", "error": ""}), \
         patch.object(module.actions, "raw_citation_sources", return_value=[locator]):
        result = module._api_query_loop(session, model, max_rounds=5)
    assert result["answer"] == "6" and result["round"] == 5
    assert result["citation_check"]["ok"]
    assert session.plan_count == 4 and session.hard_stopped
    assert result["snapshot"]["allowed_next_actions"] == ["answer"]


def test_no_final_model_call_beyond_explicit_round_cap():
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return {"status": "ok", "parsed": {"decision": "discover",
            "strategy": {"status": "skipped", "reason": "fixed test"},
            "plan": [{"action": "graph_search", "input": {"term": str(len(prompts))}}]}}

    session = QuerySession(query="q", mode="api")
    with patch.object(module.actions, "execute", return_value={"ok": True, "tokens": 1, "text": "candidate", "error": ""}):
        result = module._api_query_loop(session, model, max_rounds=4)
    assert result["status"] == "loop_exhausted" and len(prompts) == 4


def test_budget_exhaustion_at_tool_limit_blocks_final_model_call():
    session = QuerySession(query="q", mode="api", token_budget=1)
    session.plan_count = module.MAX_LOOP
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        return {"status": "ok", "parsed": {"decision": "discover",
            "strategy": {"status": "skipped", "reason": "fixed test"},
            "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}

    with patch.object(module.actions, "execute", return_value={"ok": True, "tokens": 2, "text": "candidate", "error": ""}):
        result = module._api_query_loop(session, model, max_rounds=5)
    assert len(prompts) == result["round"] == 1
    assert result["snapshot"]["stop_reason"] == "budget_exhausted"
    assert "answer" not in result


def test_budget_exhaustion_keeps_priority_over_model_round_cap():
    session = QuerySession(query="bounded lookup", mode="api", token_budget=1)

    def model(prompt):
        return {"status": "ok", "parsed": {"decision": "discover",
            "strategy": {"status": "skipped", "reason": "fixed test retrieval"},
            "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}

    with patch.object(module.actions, "execute", return_value={
            "ok": True, "tokens": 2, "text": "candidate", "error": ""}):
        result = module._api_query_loop(session, model, max_rounds=1)
    assert result["round"] == 1
    assert result["snapshot"]["stop_reason"] == "budget_exhausted"


def test_loop_rejects_agent_backend_response():
    """API 编排器不得把错误的 Agent backend 响应伪装成正常交接。"""
    def mock_fn(prompt):
        return {"status": "agent_required", "mode": "agent"}
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=3)
    assert result["status"] == "backend_error"
    assert "handoff" not in result
    assert "Agent backend response" in result["error"]


def test_contract_errors_return_before_dispatch_and_reach_model():
    session = QuerySession(query="lookup", mode="api")
    with patch.object(module.actions, "execute") as execute:
        out = module.execute_plan(session, [{"action": "hybrid_recall", "input": {
            "query": "x", "intent": "locate_quarterly_report_clause"}}])
    execute.assert_not_called()
    assert not out["results"][0]["ok"]
    prompt = module._build_api_prompt(session, out["results"], 2)
    assert "INPUT_CONTRACT" in prompt
    assert "explanation" in out["results"][0]["error"]


def test_read_ref_resolves_exact_candidate_and_cannot_repeat_or_bypass_stage():
    session = QuerySession(query="read source", mode="api")
    locator = "academic/raw/example.md#L7-L9"
    module._remember_read_options(session, json.dumps({"candidates": [{
        "kind": "source", "title": "source", "locator": locator}]}))
    ref = next(iter(session.read_options))
    step = {"action": "read_ref", "input": {"ref": ref}}
    with patch.object(module.actions, "execute") as execute:
        blocked = module.execute_plan(session, [step])
    execute.assert_not_called()
    assert "STAGE_GUARD" in blocked["results"][0]["error"]
    session.stage = "evidence"
    with patch.object(module.actions, "execute", return_value={"ok": True, "text": "bound raw", "tokens": 2, "error": ""}) as execute, \
         patch.object(module.actions, "raw_citation_sources", return_value=[locator]):
        result = module.execute_plan(session, [step, step])
    execute.assert_called_once_with("read_raw", {"locator": locator})
    assert result["results"][0]["ok"]
    assert "ACTION_ALREADY_VISITED" in result["results"][1]["error"]
    assert session.read_sources == [locator]


def test_read_ref_unknown_or_nonread_candidate_is_not_executed():
    session = QuerySession(query="read", mode="api", stage="evidence")
    module._remember_read_options(session, json.dumps({"candidates": [{
        "read_action": "calculate", "read_input": {"operation": "sum", "operands": [1]}, "title": "untrusted"}]}))
    assert not session.read_options
    with patch.object(module.actions, "execute") as execute:
        result = module.execute_plan(session, [{"action": "read_ref", "input": {"ref": "R-made-up"}}])
    execute.assert_not_called()
    assert "unknown read ref" in result["results"][0]["error"]


def test_calculate_is_bounded_and_not_source_evidence():
    session = QuerySession(query="arithmetic", mode="api", stage="evidence")
    with patch.object(module.actions, "execute") as execute:
        out = module.execute_plan(session, [{"action": "calculate", "input": {
            "operation": "sum_products", "operands": [[1, 2], [3, -1], [4, 5]]}}])
    execute.assert_not_called()
    data = json.loads(out["results"][0]["text"])
    assert data["result"] == 2 and data["vector_lengths"] == [2, 2, 2]
    assert not session.read_sources


def test_real_tool_failure_with_empty_text_is_visible():
    session = QuerySession(query="q", mode="api", stage="evidence")
    with patch.object(module.actions, "execute", return_value={"ok": False, "text": "", "tokens": 0, "error": "source binding failed"}):
        out = module.execute_plan(session, [{"action": "read_raw", "input": {"locator": "academic/raw/example.md#L1"}}])
    assert "source binding failed" in module._build_api_prompt(session, out["results"], 2)
    assert not session.read_sources


def test_malformed_plan_and_restored_nonread_ref_do_not_dispatch():
    session = QuerySession(query="q", mode="api", stage="evidence")
    session.read_options["R-invalid"] = {"action": "graph_search", "input": {"term": "x"}}
    with patch.object(module.actions, "execute") as execute:
        out = module.execute_plan(session, [{"action": []}, {"action": "read_ref", "input": {"ref": "R-invalid"}}])
    execute.assert_not_called()
    assert all(not r["ok"] for r in out["results"])


def test_read_ref_keeps_public_scope_and_does_not_certify_failed_reads():
    session = QuerySession(query="q", mode="api", stage="evidence")
    module._remember_read_options(session, json.dumps({"candidates": [{
        "kind": "source", "title": "private", "locator": "private/raw/secret.md#L1"}]}))
    ref = next(iter(session.read_options))
    out = module.execute_plan(session, [{"action": "read_ref", "input": {"ref": ref}}])
    assert not out["results"][0]["ok"]
    assert not session.read_sources


def test_read_handle_labels_remain_navigation_after_discovery_leaves_context():
    session = QuerySession(query="which source applies", mode="api", stage="evidence")
    title = "UNREAD-CANDIDATE-CLAIM: the method always works"
    other_title = "ANOTHER-UNREAD-CLAIM: future work in unrelated systems"
    payload = {"candidates": [
        {"kind": "question", "id": "Q-first", "title": title},
        {"kind": "question", "id": "Q-second", "title": other_title},
    ]}
    text = json.dumps(payload)
    module._remember_read_options(session, text)
    saved = json.dumps(session.read_options, sort_keys=True)
    discovery = {"action": "knowledge_search", "input": {"query": "method"},
                 "ok": True, "text": text}
    prompt = module._build_api_prompt(session, [discovery], 2)
    assert prompt.count(title) == prompt.count(other_title) == 2
    first, second = list(session.read_options)
    with patch.object(module.actions, "execute", return_value={
            "ok": True, "text": "ACTUAL-KU-BODY", "tokens": 2, "error": ""}) as execute:
        result = module.execute_plan(session, [{"action": "read_ref", "input": {"ref": first}}])
        later = module._build_api_prompt(session, result['results'], 3)
        assert later.count(title) == later.count(other_title) == 1
        assert "候选不是已读证据" in later
        assert later.count("ACTUAL-KU-BODY") == 1
        assert second in later and 'Q-second' in later
        # Labels aid selection without blocking a genuinely useful later read.
        module.execute_plan(session, [{"action": "read_ref", "input": {"ref": second}}])
        assert execute.call_args_list[-1].args == ('knowledge_read', {'unit_id': 'Q-second'})
    assert json.dumps(session.read_options, sort_keys=True) == saved
    assert not session.read_sources


def test_source_labels_survive_raw_read_and_session_restore():
    session = QuerySession(query="explain the proposal", mode="api", stage="evidence")
    target = 'academic/raw/sources/S-target/original.md#L1-L4'
    other = 'academic/raw/sources/S-other/original.md#L1-L8'
    module._remember_read_options(session, json.dumps({'candidates': [
        {'kind': 'source', 'title': 'Scholarship proposal', 'locator': target},
        {'kind': 'source', 'title': 'Research cooperation agreement', 'locator': other},
    ]}))
    first, second = list(session.read_options)
    with patch.object(module.actions, 'execute', return_value={
            'ok': True, 'text': 'TARGET-RAW-BODY', 'tokens': 5, 'error': ''}), \
         patch.object(module.actions, 'raw_citation_sources', return_value=[target]):
        read = module.execute_plan(session, [{'action': 'read_ref', 'input': {'ref': first}}])
    with tempfile.TemporaryDirectory() as directory, patch.object(module, 'SESSIONS_DIR', Path(directory)):
        module._save(session)
        restored = module._load(session.session_id)
    prompt = module._build_api_prompt(restored, read['results'], 3)
    assert 'Scholarship proposal' in prompt and 'Research cooperation agreement' in prompt
    assert prompt.count('TARGET-RAW-BODY') == 1
    assert restored.read_sources == [target] and other not in restored.read_sources
    assert restored.read_options[second]['input'] == {'locator': other}
    assert len(restored.retained_results) == 1
    restored.stage = 'answer'
    final = module._build_api_prompt(restored, [], 4)
    assert second not in final and 'Research cooperation agreement' not in final
    assert final.count('TARGET-RAW-BODY') == 1


def test_titleless_old_read_handle_remains_usable():
    session = QuerySession(query='legacy session', mode='api', stage='evidence')
    locator = 'academic/raw/example.md#L1'
    session.read_options['R-old'] = {'action': 'read_raw', 'input': {'locator': locator}}
    prompt = module._build_api_prompt(session, [], 2)
    assert 'R-old' in prompt and locator in prompt
    with patch.object(module.actions, 'execute', return_value={
            'ok': True, 'text': 'LEGACY-RAW', 'tokens': 2, 'error': ''}) as execute, \
         patch.object(module.actions, 'raw_citation_sources', return_value=[locator]):
        module.execute_plan(session, [{'action': 'read_ref', 'input': {'ref': 'R-old'}}])
    assert execute.call_args.args == ('read_raw', {'locator': locator})


def test_read_options_survive_session_save_and_load():
    session = QuerySession(query="q", mode="api", stage="evidence")
    module._remember_read_options(session, json.dumps({"evidence": [{"locator": "academic/raw/example.md#L1"}]}))
    with tempfile.TemporaryDirectory() as directory, patch.object(module, "SESSIONS_DIR", Path(directory)):
        module._save(session)
        restored = module._load(session.session_id)
    assert restored.read_options == session.read_options


def test_loop_retains_raw_across_calculation_without_discovery_history():
    """A calculation turn must not erase the formula read in an earlier turn."""
    locator = "academic/raw/example.md#L1-L3"
    raw_text = "RAW-BODY: under condition A, the score is a sum of products."
    prompts = []
    decisions = [
        {"decision": "discover", "strategy": {"status": "skipped", "reason": "fixed test"},
         "plan": [{"action": "graph_search", "input": {"term": "formula"}}]},
        {"decision": "read", "plan": [{"action": "read_raw", "input": {"locator": locator}}]},
        {"decision": "read", "plan": [{"action": "calculate", "input": {
            "operation": "sum_products", "operands": [[1, 2], [3, -1], [4, 5]]}}]},
        {"decision": "answer", "answer": "2 under condition A", "citations": [locator]},
    ]

    def model(prompt):
        prompts.append(prompt)
        if len(prompts) == 4:
            assert prompt.count(raw_text) == 1, "earlier Raw body lost or duplicated after calculation"
            assert '"result": 2' in prompt
            assert "usage_role=source_material" in prompt and "usage_role=result" in prompt
            assert "DISCOVERY-NOISE" not in prompt
        return {"status": "ok", "parsed": decisions[len(prompts) - 1]}

    def execute(action, inp):
        return {"ok": True, "text": raw_text if action == "read_raw" else "DISCOVERY-NOISE",
                "tokens": 10, "error": ""}

    session = QuerySession(query="formula and arithmetic", mode="api")
    with patch.object(module.actions, "execute", side_effect=execute), \
         patch.object(module.actions, "raw_citation_sources", return_value=[locator]):
        result = module._api_query_loop(session, model, max_rounds=5)
    assert result["citation_check"]["ok"] and len(prompts) == 4
    assert session.read_sources == [locator]
    assert [r["action"] for r in session.retained_results] == ["read_raw", "calculate"]
    assert session.token_used == 20 + session.retained_results[-1]["tokens"]


def test_successful_discovery_exposes_read_contracts_in_next_prompt():
    for success in (True, False):
        prompts = []

        def model(prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                return {"status": "ok", "parsed": {"decision": "discover",
                    "strategy": {"status": "skipped", "reason": "fixed test"},
                    "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}
            schemas = json.loads(prompt.split("工具input契约(JSON Schema): ", 1)[1].splitlines()[0])
            assert ("read_raw" in schemas) is success
            assert ("read_ref" in schemas) is success
            assert ("knowledge_read" in schemas) is success
            assert f"当前阶段: {'evidence' if success else 'start'}" in prompt
            return {"status": "ok", "parsed": {"decision": "answer", "answer": "limited", "citations": []}}

        session = QuerySession(query="q", mode="api")
        with patch.object(module.actions, "execute", return_value={
                "ok": success, "tokens": 1, "text": "candidate" if success else "",
                "error": "" if success else "unavailable"}):
            module._api_query_loop(session, model, max_rounds=2)


def test_retained_reads_survive_duplicate_denial_and_session_restore():
    locator = "academic/raw/example.md#L4-L8"
    session = QuerySession(query="reuse evidence", mode="api", stage="evidence")
    steps = [{"action": "read_raw", "input": {"locator": locator}},
             {"action": "knowledge_read", "input": {"unit_id": "KU-example"}}]

    def execute(action, inp):
        return {"ok": True, "text": "BOUND-RAW-BODY" if action == "read_raw" else "CONDITIONAL-KU-BODY",
                "tokens": 10, "error": ""}

    with patch.object(module.actions, "execute", side_effect=execute), \
         patch.object(module.actions, "raw_citation_sources", return_value=[locator]):
        out = module.execute_plan(session, steps)
    prompt = module._build_api_prompt(session, out["results"], 2)
    assert prompt.count("BOUND-RAW-BODY") == prompt.count("CONDITIONAL-KU-BODY") == 1
    with tempfile.TemporaryDirectory() as directory, patch.object(module, "SESSIONS_DIR", Path(directory)):
        module._save(session)
        restored = module._load(session.session_id)
    with patch.object(module.actions, "execute") as execute:
        rejected = module.execute_plan(restored, [steps[0]])
    execute.assert_not_called()
    prompt = module._build_api_prompt(restored, rejected["results"], 3)
    assert "ACTION_ALREADY_VISITED" in prompt
    assert prompt.count("BOUND-RAW-BODY") == prompt.count("CONDITIONAL-KU-BODY") == 1
    assert "usage_role=source_material" in prompt and "usage_role=knowledge" in prompt
    assert restored.read_sources == [locator]
    assert restored.token_used == 20 and len(restored.retained_results) == 2


def test_navigation_and_failed_material_are_not_retained():
    session = QuerySession(query="q", mode="api", stage="evidence")

    def execute(action, inp):
        if action == "graph_search":
            return {"ok": True, "text": "candidate claims source_material", "tokens": 3, "error": ""}
        return {"ok": False, "text": "UNVERIFIED-BODY", "tokens": 0, "error": "binding failed"}

    with patch.object(module.actions, "execute", side_effect=execute):
        out = module.execute_plan(session, [
            {"action": "graph_search", "input": {"term": "x"}},
            {"action": "read_raw", "input": {"locator": "academic/raw/example.md#L1"}},
            {"action": "calculate", "input": {"operation": "quotient", "operands": [1, 0]}},
        ])
    assert not session.retained_results and not session.read_sources
    prompt = module._build_api_prompt(session, out["results"], 2)
    assert "binding failed" in prompt and "usage_role=source_material" not in prompt


def test_failed_read_records_attempt_not_read_page_and_corrected_entry_runs():
    session = QuerySession(query="q", mode="api", stage="evidence")
    page = "teaching/wiki/demo"
    missing = {"action": "read_section", "input": {"page": page}}
    fail = {"ok": False, "text": "missing heading", "tokens": 3, "error": "missing heading"}
    with patch.object(module.actions, "execute", return_value=fail) as execute:
        result = module.execute_plan(session, [missing])
        assert not result["results"][0]["ok"]
        assert session.visited_action_keys and not session.visited_pages
        again = module.execute_plan(session, [missing])
        assert execute.call_count == 1
        assert "已尝试" in again["results"][0]["error"]
        assert "已读过" not in again["results"][0]["error"]
    assert not session.read_sources and not session.retained_results
    module._remember_read_options(session, json.dumps({"candidates": [{
        "path": page, "read_action": "read_section",
        "read_input": {"page": page, "section": "actual-rules"}}]}))
    success = {"ok": True, "text": "read body", "tokens": 5, "error": ""}
    with patch.object(module.actions, "execute", return_value=success), patch.object(module, "collect_evidence_facts"):
        out = module.execute_plan(session, [{"action": "read_ref", "input": {"ref": next(iter(session.read_options))}}])
    assert out["results"][0]["ok"] and session.visited_pages == [page]
    assert session.token_used == 8 and not session.read_sources


def test_old_sessions_load_without_inventing_retained_evidence():
    session = QuerySession(query="old session", mode="api")
    session.read_sources = ["academic/raw/example.md#L1"]
    with tempfile.TemporaryDirectory() as directory, patch.object(module, "SESSIONS_DIR", Path(directory)):
        module._save(session)
        path = Path(directory) / f"{session.session_id}.json"
        data = json.loads(path.read_text())
        data.pop("retained_results", None)
        path.write_text(json.dumps(data))
        restored = module._load(session.session_id)
    assert restored.read_sources == session.read_sources
    assert restored.retained_results == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"query api loop regression: {passed}/{len(tests)} PASS")

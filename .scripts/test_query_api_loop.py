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


def test_loop_exhausted():
    """LLM 不 answer → 循环耗尽正确收束。"""
    def mock_fn(prompt):
        return {"status": "ok", "parsed": {"decision": "discover",
                "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=2)
    assert result["status"] == "loop_exhausted"
    assert result["round"] == 2


def test_loop_agent_handoff():
    """API 未配置时 agent_required 交接。"""
    def mock_fn(prompt):
        return {"status": "agent_required", "mode": "agent"}
    s = QuerySession(query="test", query_type="t", stage="start", mode="api")
    result = module._api_query_loop(s, mock_fn, max_rounds=3)
    assert "handoff" in result
    assert result["handoff"]["status"] == "agent_required"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"query api loop regression: {passed}/{len(tests)} PASS")

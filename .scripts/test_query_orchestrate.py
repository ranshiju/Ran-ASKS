#!/usr/bin/env python3
"""query_orchestrate.py QuerySession stage guard 回归测试。

验证点：
- stage/mode/read_sources 字段存在且默认值正确
- API 模式下 STAGE_ACTIONS allowlist 生效（start 不许 read_section, answer 不许读）
- agent 模式下 stage 守卫不生效（行为不变）
- read_sources 字段可读写且序列化正确
- 既有守卫（非法动作/重复/预算/循环）不受影响
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "query_orchestrate.py"

spec = importlib.util.spec_from_file_location("query_orchestrate", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["query_orchestrate"] = module
spec.loader.exec_module(module)

QuerySession = module.QuerySession
STAGE_ACTIONS = module.STAGE_ACTIONS


def _api_session(stage="start"):
    return QuerySession(query="test", query_type="test", stage=stage, mode="api")


def test_new_fields_exist():
    """stage/mode/read_sources 字段存在，默认值正确。"""
    s = QuerySession(query="q", query_type="simple")
    assert s.stage == "start"
    assert s.mode == "agent"
    assert s.read_sources == []


def test_api_mode_start_blocks_read_section():
    """API 模式 start 阶段不许 read_section（须先发现候选）。"""
    s = _api_session("start")
    deny = s.deny_reason({"action": "read_section", "input": {"page": "demo.md", "section": "Content"}})
    assert deny and deny.startswith("STAGE_GUARD"), f"start 阶段应拦截 read_section，实际: {deny}"


def test_api_mode_start_allows_discovery():
    """API 模式 start 阶段允许发现类动作。"""
    s = _api_session("start")
    for act in ("graph_search", "wiki_recall", "graph_neighbors"):
        deny = s.deny_reason({"action": act, "input": {"query": "x"}} if "recall" in act else {"action": act, "input": {}})
        assert deny is None or not deny.startswith("STAGE_GUARD"), f"start 阶段应允许 {act}，实际: {deny}"


def test_api_mode_evidence_allows_read_section():
    """API 模式 evidence 阶段允许 read_section。"""
    s = _api_session("evidence")
    deny = s.deny_reason({"action": "read_section", "input": {"page": "demo.md", "section": "Content"}})
    assert deny is None or not deny.startswith("STAGE_GUARD"), f"evidence 阶段应允许 read_section，实际: {deny}"


def test_api_mode_answer_blocks_all_reads():
    """API 模式 answer 阶段不许任何查询动作。"""
    s = _api_session("answer")
    deny = s.deny_reason({"action": "graph_search", "input": {}})
    assert deny and deny.startswith("STAGE_GUARD"), f"answer 阶段应拦截 graph_search，实际: {deny}"
    deny2 = s.deny_reason({"action": "read_section", "input": {"page": "x", "section": "y"}})
    assert deny2 and deny2.startswith("STAGE_GUARD"), f"answer 阶段应拦截 read_section，实际: {deny2}"


def test_agent_mode_ignores_stage_guard():
    """agent 模式下 stage 守卫不生效（行为不变）。"""
    s = QuerySession(query="q", query_type="t", stage="start", mode="agent")
    # start 阶段的 read_section 在 agent 模式下不应被 STAGE_GUARD 拦截
    deny = s.deny_reason({"action": "read_section", "input": {"page": "demo.md", "section": "Content"}})
    assert deny is None or not deny.startswith("STAGE_GUARD"), f"agent 模式不应触发 STAGE_GUARD，实际: {deny}"


def test_agent_mode_answer_allows_reads():
    """agent 模式 answer 阶段仍允许读（不强制 stage）。"""
    s = QuerySession(query="q", query_type="t", stage="answer", mode="agent")
    deny = s.deny_reason({"action": "graph_search", "input": {}})
    assert deny is None, f"agent 模式 answer 不应拦截，实际: {deny}"


def test_existing_guards_unaffected():
    """既有守卫（非法动作/重复/预算/循环）在 agent 模式仍正常工作。"""
    s = QuerySession(query="q", query_type="t")
    assert s.deny_reason({"action": "invalid_action", "input": {}}).startswith("非法动作")
    s.record_visit("read_section", {"page": "demo.md", "section": "Navigation"})
    assert s.deny_reason({"action": "read_section", "input": {"page": "demo.md", "section": "Navigation"}}).startswith("ACTION_ALREADY_VISITED")
    s.token_used = s.token_budget + 1
    assert s.deny_reason({"action": "read_section", "input": {"page": "demo.md", "section": "Content"}}).startswith("BUDGET_EXHAUSTED")


def test_read_sources_writable():
    """read_sources 可读写。"""
    s = QuerySession(query="q", query_type="t")
    s.read_sources.append("academic/raw/papers/demo.md")
    assert s.read_sources == ["academic/raw/papers/demo.md"]


def test_snapshot_includes_new_fields():
    """snapshot 包含 stage/mode/read_sources。"""
    s = QuerySession(query="q", query_type="t", stage="evidence", mode="api")
    s.read_sources = ["raw/demo.md"]
    snap = s.snapshot()
    assert snap["stage"] == "evidence"
    assert snap["mode"] == "api"
    assert snap["read_sources"] == ["raw/demo.md"]


def test_stage_actions_completeness():
    """STAGE_ACTIONS 覆盖四个阶段，start⊂evidence=continue，answer 为空。"""
    assert set(STAGE_ACTIONS.keys()) == {"start", "evidence", "continue", "answer"}
    assert STAGE_ACTIONS["start"].issubset(STAGE_ACTIONS["evidence"])
    assert STAGE_ACTIONS["evidence"] == STAGE_ACTIONS["continue"]
    assert STAGE_ACTIONS["answer"] == set()
    assert "read_section" not in STAGE_ACTIONS["start"]
    assert "read_raw" not in STAGE_ACTIONS["start"]
    assert "read_section" in STAGE_ACTIONS["evidence"]
    assert "read_raw" in STAGE_ACTIONS["evidence"]


def test_api_mode_start_blocks_read_raw():
    """API 模式 start 阶段不许 read_raw（须先经 evidence 阶段发现候选）。"""
    s = _api_session("start")
    deny = s.deny_reason({"action": "read_raw", "input": {"locator": "raw/demo.md"}})
    assert deny and deny.startswith("STAGE_GUARD"), f"start 阶段应拦截 read_raw，实际: {deny}"


def test_api_mode_evidence_allows_read_raw():
    """API 模式 evidence 阶段允许 read_raw。"""
    s = _api_session("evidence")
    deny = s.deny_reason({"action": "read_raw", "input": {"locator": "raw/demo.md"}})
    assert deny is None, f"evidence 阶段应允许 read_raw，实际: {deny}"


def test_read_raw_repeat_denied():
    """同一 locator 的 read_raw 不重复执行。"""
    s = QuerySession(query="q", query_type="t")
    s.record_visit("read_raw", {"locator": "raw/demo.md"})
    deny = s.deny_reason({"action": "read_raw", "input": {"locator": "raw/demo.md"}})
    assert deny and deny.startswith("ACTION_ALREADY_VISITED"), f"重复 read_raw 应拦截，实际: {deny}"


def test_read_raw_recorded_in_read_sources():
    """read_raw 成功执行后 locator 记入 session.read_sources（citation contract 基础）。"""
    import tempfile, os
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, dir=str(REPO / 'temp'))
    tmp.write('# Title\n\ntest content for read_raw')
    tmp.close()
    try:
        rel = os.path.relpath(tmp.name, str(REPO)) + "#L3"
        s = QuerySession(query='q', query_type='t', stage='evidence', mode='api')
        plan = [{'action': 'read_raw', 'input': {'locator': rel}}]
        out = module.execute_plan(s, plan, is_continuation=False)
        assert out['results'], 'read_raw 未执行'
        assert out['results'][0]['ok'], f"read_raw 失败: {out['results'][0].get('error')}"
        assert rel in s.read_sources, f"locator 未记入 read_sources: {s.read_sources}"
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"query orchestrate regression: {passed}/{len(tests)} PASS")

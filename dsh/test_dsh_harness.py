#!/usr/bin/env python3
"""dsh/ 适配层全量回归测试。

验证点：
- SessionLog: append/derive_messages/to_jsonl
- ToolRegistry: register/schemas/execute through pipeline
- Hook 瀑布: pre-execute deny, post-execute block/replace/append, execute wrapper
- repeat-tool-reminder: 计数/阈值/提醒/gentle vs detailed/重置
- timeout-policy: 超时替换结果
- citation-guard: read_raw 记录/check 三态
- AgentLoop: 完整循环(discover→read_raw→answer verified)
- AgentLoop: citation 未核验
- AgentLoop: agent 模式 handoff
- AgentLoop: session log 不变量(model-visible means logged)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.harness import (
    SessionLog, ToolDefinition, ToolRegistry, ToolExecution,
    ToolExecutionResult, PreToolDecision, PostToolDecision, PreStepDecision,
)
from dsh.guards.repeat_tool_reminder import RepeatToolReminder, _canonicalize
from dsh.guards.timeout_policy import TimeoutPolicy, timeout_result
from dsh.guards.citation_guard import CitationGuard
from dsh.agent_loop import AgentLoop, TurnResult


# ============ SessionLog ============

def test_sessionlog_append_and_events():
    log = SessionLog()
    log.append("turn/start", {"query": "test"})
    log.append("user/message", {"role": "user", "content": "hello"})
    assert len(log.events()) == 2
    assert log.events()[0].type == "turn/start"


def test_sessionlog_derive_messages():
    log = SessionLog()
    log.append("user/message", {"role": "user", "content": "query"})
    log.append("tool/result", {"content": "result text", "name": "lookup"})
    log.append("assistant/message", {"role": "assistant", "content": "answer"})
    msgs = log.derive_messages()
    assert len(msgs) == 3
    assert msgs[0]["content"] == "query"
    assert msgs[1]["role"] == "tool"
    assert msgs[2]["content"] == "answer"


def test_sessionlog_to_jsonl():
    log = SessionLog(session_id="test123")
    log.append("turn/start", {"query": "q"})
    lines = log.to_jsonl().strip().split("\n")
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["type"] == "turn/start"
    assert obj["data"]["query"] == "q"


# ============ ToolRegistry ============

def test_registry_register_and_get():
    reg = ToolRegistry()
    tool = ToolDefinition(name="test", description="test tool",
                          input_schema={}, execute_fn=lambda a: "ok")
    reg.register(tool)
    assert reg.get("test") is tool
    assert "test" in reg.names()


def test_registry_schemas():
    reg = ToolRegistry()
    reg.register(ToolDefinition("a", "desc a", {"type": "object"}, lambda a: ""))
    reg.register(ToolDefinition("b", "desc b", {"type": "object"}, lambda a: ""))
    schemas = reg.schemas()
    assert len(schemas) == 2
    assert {s["name"] for s in schemas} == {"a", "b"}


def test_registry_execute_success():
    reg = ToolRegistry()
    reg.register(ToolDefinition("echo", "echo tool", {},
                                lambda a: f"echo:{a.get('msg', '')}"))
    log = SessionLog()
    result = reg.execute(ToolExecution(name="echo", arguments={"msg": "hi"}), log)
    assert not result.is_error
    assert result.content == "echo:hi"
    # tool/call + tool/result events
    types = [e.type for e in log.events()]
    assert "tool/call" in types
    assert "tool/result" in types


def test_registry_execute_unknown_tool():
    reg = ToolRegistry()
    log = SessionLog()
    result = reg.execute(ToolExecution(name="nonexistent", arguments={}), log)
    assert result.is_error
    assert result.error_code == "UNKNOWN_TOOL"


def test_registry_execute_exception():
    reg = ToolRegistry()
    def boom(a): raise ValueError("crash")
    reg.register(ToolDefinition("boom", "", {}, boom))
    log = SessionLog()
    result = reg.execute(ToolExecution(name="boom", arguments={}), log)
    assert result.is_error
    assert result.error_code == "EXECUTION_ERROR"


# ============ Hook 瀑布 ============

def test_pre_execute_deny():
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "ok"))
    reg.on_pre_execute(lambda ctx: PreToolDecision(kind="deny", reason="forbidden"))
    log = SessionLog()
    result = reg.execute(ToolExecution(name="safe", arguments={}), log)
    assert result.is_error
    assert "forbidden" in result.content


def test_pre_execute_allow():
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "ok"))
    reg.on_pre_execute(lambda ctx: None)  # abstain
    log = SessionLog()
    result = reg.execute(ToolExecution(name="safe", arguments={}), log)
    assert not result.is_error
    assert result.content == "ok"


def test_post_execute_block():
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "ok"))
    reg.on_post_execute(lambda ctx, r: PostToolDecision(kind="block", feedback="blocked by guard"))
    log = SessionLog()
    result = reg.execute(ToolExecution(name="safe", arguments={}), log)
    assert result.is_error
    assert "blocked" in result.content.lower()


def test_post_execute_replace():
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "original"))
    reg.on_post_execute(lambda ctx, r: PostToolDecision(
        kind="replace", replacement=ToolExecutionResult(content="replaced")))
    log = SessionLog()
    result = reg.execute(ToolExecution(name="safe", arguments={}), log)
    assert result.content == "replaced"
    assert not result.is_error


def test_post_execute_additional_contexts():
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "ok"))
    reg.on_post_execute(lambda ctx, r: PostToolDecision(
        kind="accept", additional_contexts=[{"content": "reminder", "role": "user"}]))
    log = SessionLog()
    reg.execute(ToolExecution(name="safe", arguments={}), log)
    types = [e.type for e in log.events()]
    assert "plugin/message" in types


def test_execute_wrapper():
    """around-dispatch wrapper 可替换结果。"""
    reg = ToolRegistry()
    reg.register(ToolDefinition("safe", "", {}, lambda a: "original"))
    def wrapper(ctx, next_fn):
        result = next_fn(ctx)
        result.content = result.content.upper()
        return result
    reg.on_execute(wrapper)
    log = SessionLog()
    result = reg.execute(ToolExecution(name="safe", arguments={}), log)
    assert result.content == "ORIGINAL"


def test_pre_step_reject():
    reg = ToolRegistry()
    reg.on_pre_step(lambda msgs: PreStepDecision(kind="reject"))
    kind, msgs = reg.run_pre_step([{"role": "user", "content": "q"}])
    assert kind == "reject"


# ============ repeat-tool-reminder ============

def test_canonicalize_key_order():
    """属性顺序不同应规范化一致。"""
    a = _canonicalize({"x": 1, "y": 2})
    b = _canonicalize({"y": 2, "x": 1})
    assert a == b


def test_repeat_no_reminder_below_threshold():
    guard = RepeatToolReminder(thresholds=[3])
    ctx = ToolExecution(name="lookup", arguments={"term": "x"}, agent_id="a1")
    for i in range(2):
        decision = guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
        assert decision is None


def test_repeat_reminder_at_threshold():
    guard = RepeatToolReminder(thresholds=[3])
    ctx = ToolExecution(name="lookup", arguments={"term": "x"}, agent_id="a1")
    decision = None
    for i in range(3):
        decision = guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
    assert decision is not None
    assert len(decision.additional_contexts) == 1
    assert "重复" in decision.additional_contexts[0]["content"]


def test_repeat_gentle_vs_detailed():
    guard = RepeatToolReminder(thresholds=[3, 5])
    ctx = ToolExecution(name="lookup", arguments={"term": "x"}, agent_id="a1")
    for i in range(3):
        d = guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
    assert d is not None
    # 第 3 次 = gentle (首个阈值)
    assert "仔细分析" in d.additional_contexts[0]["content"] or "重复" in d.additional_contexts[0]["content"]
    for i in range(2):  # 继续到 5
        d = guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
    assert d is not None
    # 第 5 次 = detailed
    assert "连续次数" in d.additional_contexts[0]["content"]


def test_repeat_different_args_resets():
    guard = RepeatToolReminder(thresholds=[3])
    guard.on_post_execute(ToolExecution(name="lookup", arguments={"term": "a"}, agent_id="a1"),
                           ToolExecutionResult(content="ok"))
    guard.on_post_execute(ToolExecution(name="lookup", arguments={"term": "b"}, agent_id="a1"),
                           ToolExecutionResult(content="ok"))
    # 不同参数 → 重置 → 不应触发
    d = guard.on_post_execute(ToolExecution(name="lookup", arguments={"term": "b"}, agent_id="a1"),
                               ToolExecutionResult(content="ok"))
    assert d is None  # count=1, below threshold 3


def test_repeat_user_message_resets():
    guard = RepeatToolReminder(thresholds=[3])
    ctx = ToolExecution(name="lookup", arguments={"term": "x"}, agent_id="a1")
    for i in range(2):
        guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
    # 用户消息重置
    guard.on_pre_step([{"role": "user", "content": "new query"}])
    d = guard.on_post_execute(ctx, ToolExecutionResult(content="ok"))
    assert d is None  # count reset to 1


# ============ timeout-policy ============

def test_timeout_result_structure():
    r = timeout_result(5000)
    assert r.is_error
    assert r.error_code == "TOOL_TIMEOUT"
    assert "5000" in r.content


def test_timeout_policy_normal_execution():
    """不超时时正常返回。"""
    policy = TimeoutPolicy(default_timeout_ms=10000)
    def next_fn(ctx): return ToolExecutionResult(content="ok")
    result = policy.on_execute(ToolExecution(name="test", arguments={}), next_fn)
    assert result.content == "ok"


# ============ citation-guard ============

def test_citation_no_citations():
    guard = CitationGuard()
    check = guard.check([])
    assert check.status == "no_citations"
    assert not check.ok


def test_citation_verified():
    guard = CitationGuard()
    guard.read_sources = ["raw/a.md", "raw/b.md"]
    check = guard.check(["raw/a.md", "raw/b.md"])
    assert check.ok
    assert check.status == "verified"
    assert check.count == 2


def test_citation_unverified():
    guard = CitationGuard()
    guard.read_sources = ["raw/a.md"]
    check = guard.check(["raw/a.md", "raw/x.md"])
    assert not check.ok
    assert check.status == "unverified"
    assert "raw/x.md" in check.unverified


def test_citation_records_read_raw():
    guard = CitationGuard()
    ctx = ToolExecution(name="read_raw", arguments={"locator": "raw/demo.md"}, agent_id="a1")
    guard.on_post_execute(ctx, ToolExecutionResult(content="some text"))
    assert "raw/demo.md" in guard.read_sources


def test_citation_ignores_failed_read_raw():
    guard = CitationGuard()
    ctx = ToolExecution(name="read_raw", arguments={"locator": "raw/missing.md"}, agent_id="a1")
    guard.on_post_execute(ctx, ToolExecutionResult(content="error", is_error=True))
    assert "raw/missing.md" not in guard.read_sources


# ============ AgentLoop ============

def test_agent_mode_handoff():
    """agent 模式返回 handoff，不调用 LLM。"""
    loop = AgentLoop(mode="agent")
    result = loop.run("test query")
    assert result.handoff is not None
    assert result.handoff["status"] == "agent_required"


def test_api_mode_full_cycle_verified():
    """API 模式完整循环：discover → read_raw → answer(verified)。"""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, dir=str(REPO / "temp"))
    tmp.write("# Title\n\ntest content for dsh")
    tmp.close()
    rel = os.path.relpath(tmp.name, str(REPO))
    try:
        calls = [
            {"status": "ok", "parsed": {"decision": "discover",
              "plan": [{"action": "graph_search", "input": {"term": "test"}}]}},
            {"status": "ok", "parsed": {"decision": "read",
              "plan": [{"action": "read_raw", "input": {"locator": rel}}]}},
            {"status": "ok", "parsed": {"decision": "answer",
              "answer": "test answer", "citations": [rel]}},
        ]
        idx = [0]
        def mock_fn(prompt):
            r = calls[idx[0]]; idx[0] += 1; return r
        loop = AgentLoop(mode="api")
        result = loop.run("test", mock_fn)
        assert result.answer == "test answer"
        assert result.citation_check["status"] == "verified"
        assert result.citation_check["ok"] is True
        assert rel in result.snapshot["read_sources"]
        assert result.rounds == 3
    finally:
        os.unlink(tmp.name)


def test_api_mode_citation_unverified():
    """answer 引用未读来源 → unverified。"""
    calls = [
        {"status": "ok", "parsed": {"decision": "answer",
          "answer": "guess", "citations": ["raw/never_read.md"]}},
    ]
    idx = [0]
    def mock_fn(prompt):
        r = calls[idx[0]]; idx[0] += 1; return r
    loop = AgentLoop(mode="api")
    result = loop.run("test", mock_fn)
    assert result.citation_check["status"] == "unverified"
    assert not result.citation_check["ok"]


def test_api_mode_no_citations():
    loop = AgentLoop(mode="api")
    calls = [{"status": "ok", "parsed": {"decision": "answer", "answer": "x", "citations": []}}]
    idx = [0]
    result = loop.run("test", lambda p: calls[idx[0] if idx[0] < len(calls) else 0] if idx.__setitem__(0, idx[0]+1) or True else {})
    # 简化
    idx2 = [0]
    def fn(p):
        r = calls[idx2[0]]; idx2[0] += 1; return r
    loop2 = AgentLoop(mode="api")
    result = loop2.run("test", fn)
    assert result.citation_check["status"] == "no_citations"


def test_api_mode_loop_exhausted():
    """LLM 不 answer → 循环耗尽。"""
    def mock_fn(prompt):
        return {"status": "ok", "parsed": {"decision": "discover",
                "plan": [{"action": "graph_search", "input": {"term": "x"}}]}}
    loop = AgentLoop(mode="api")
    loop.max_rounds = 2
    result = loop.run("test", mock_fn)
    assert result.rounds == 2
    assert result.answer == ""  # no answer produced


def test_session_log_invariant():
    """model-visible means logged：到达模型的一切可从日志重建。"""
    calls = [
        {"status": "ok", "parsed": {"decision": "answer", "answer": "final", "citations": []}},
    ]
    idx = [0]
    def mock_fn(prompt):
        r = calls[idx[0]]; idx[0] += 1; return r
    loop = AgentLoop(mode="api")
    loop.run("test query", mock_fn)
    events = loop.session_log.events()
    types = [e.type for e in events]
    # turn/start, user/message, step/start, assistant/message, turn/end
    assert "turn/start" in types
    assert "user/message" in types
    assert "step/start" in types
    assert "assistant/message" in types
    assert "turn/end" in types


def test_repeat_guard_in_loop():
    """agent loop 中重复工具调用触发提醒（plugin message 入 session log）。"""
    guard_calls = [{"status": "ok", "parsed": {"decision": "discover",
        "plan": [{"action": "graph_search", "input": {"term": "same"}}]}}] * 4
    guard_calls.append({"status": "ok", "parsed": {"decision": "answer", "answer": "done", "citations": []}})
    idx = [0]
    def mock_fn(prompt):
        r = guard_calls[min(idx[0], len(guard_calls)-1)]; idx[0] += 1; return r
    loop = AgentLoop(mode="api")
    loop.max_rounds = 6
    loop.run("test", mock_fn)
    events = loop.session_log.events()
    plugin_msgs = [e for e in events if e.type == "plugin/message"]
    # 第 3 次重复应触发提醒
    assert len(plugin_msgs) >= 1
    assert "重复" in plugin_msgs[0].data.get("content", "")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"dsh harness regression: {passed}/{len(tests)} PASS")

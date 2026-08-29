#!/usr/bin/env python3
"""llm_structured.py 事件日志（temp/llm-events/）回归测试。

验证点：
- _log_event 写入 temp/llm-events/YYYY-MM-DD.jsonl
- 事件字段齐全（ts/operation/mode/.../prompt_hash/prompt_len/output_hash/output_len）
- 不含明文 prompt/output（只有 hash + length）
- 写盘失败静默降级，不抛异常
- agent 模式 handoff 也记事件
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "llm_structured.py"

spec = importlib.util.spec_from_file_location("llm_structured", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["llm_structured"] = module
spec.loader.exec_module(module)

EVENTS_DIR = REPO / "temp" / "llm-events"

REQUIRED_FIELDS = {
    "ts", "operation", "mode", "model", "profile", "reasoning_profile",
    "attempt", "status", "latency_sec", "finish_reason", "usage",
    "prompt_hash", "prompt_len", "output_hash", "output_len", "error",
}


def _today_jsonl():
    import time
    return EVENTS_DIR / f"{time.strftime('%Y-%m-%d')}.jsonl"


def _read_today_events():
    p = _today_jsonl()
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l.strip()]


def test_log_event_writes_jsonl():
    """_log_event 写入当天 jsonl，字段齐全。"""
    module._log_event("test_op", {"mode": "api", "model": "test-model", "status": "ok",
                                  "profile": "p1", "reasoning_profile": "fast", "attempt": 1,
                                  "latency_sec": 0.5, "finish_reason": "stop", "usage": {"total_tokens": 100},
                                  "error": ""},
                      "secret-prompt-content", "secret-output-content")
    events = _read_today_events()
    assert events, "无事件写入"
    last = events[-1]
    assert REQUIRED_FIELDS <= set(last), f"缺字段: {REQUIRED_FIELDS - set(last)}"
    assert last["operation"] == "test_op"
    assert last["mode"] == "api"
    assert last["status"] == "ok"


def test_no_plaintext_prompt_or_output():
    """事件只含 hash+length，不含明文 prompt/output。"""
    secret_prompt = "UNIQUE_SECRET_PROMPT_7xK9mZ"
    secret_output = "UNIQUE_SECRET_OUTPUT_q3W8pL"
    module._log_event("leak_test", {"mode": "api", "model": "m", "status": "ok"},
                      secret_prompt, secret_output)
    events = _read_today_events()
    last = events[-1]
    raw = json.dumps(last, ensure_ascii=False)
    assert secret_prompt not in raw, "明文 prompt 泄漏到日志"
    assert secret_output not in raw, "明文 output 泄漏到日志"
    assert last["prompt_len"] == len(secret_prompt)
    assert last["output_len"] == len(secret_output)
    assert len(last["prompt_hash"]) == 12
    assert len(last["output_hash"]) == 12


def test_write_failure_silent():
    """写盘失败静默降级，不抛异常。"""
    orig = module.EVENTS_DIR
    try:
        module.EVENTS_DIR = Path("/nonexistent-root-dir/llm-events-deep")
        module._log_event("fail_test", {"mode": "api", "status": "ok"}, "p", "o")
    except Exception as e:
        assert False, f"_log_event 不应抛异常: {e}"
    finally:
        module.EVENTS_DIR = orig


def test_agent_handoff_logged():
    """agent 模式 handoff 也记事件（model-visible means logged 不变量）。"""
    os.environ["QUERY_BACKEND"] = "agent"
    try:
        result = module.call_json("test-prompt", lambda obj: True, operation="query")
    finally:
        os.environ.pop("QUERY_BACKEND", None)
    assert result["status"] == "agent_required"
    assert result["mode"] == "agent"
    events = _read_today_events()
    agent_events = [e for e in events if e.get("operation") == "query" and e.get("status") == "agent_required"]
    assert agent_events, "agent handoff 未记事件"
    last_agent = agent_events[-1]
    assert last_agent["mode"] == "agent"
    assert last_agent["prompt_len"] == len("test-prompt")


def test_empty_prompt_output_safe():
    """空 prompt/output 不崩溃，hash 为空字符串。"""
    module._log_event("empty_test", {"mode": "api", "status": "ok"}, "", "")
    events = _read_today_events()
    last = events[-1]
    assert last["prompt_hash"] == ""
    assert last["output_hash"] == ""
    assert last["prompt_len"] == 0
    assert last["output_len"] == 0


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"llm events regression: {passed}/{len(tests)} PASS")

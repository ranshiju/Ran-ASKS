#!/usr/bin/env python3
"""llm_structured.py 事件日志（temp/llm-events/）回归测试。

验证点：
- _log_event 写入 temp/llm-events/YYYY-MM-DD.jsonl
- 事件字段齐全（ts/transaction_id/operation/.../prompt_hash/prompt_len/output_hash/output_len）
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
from functools import wraps
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts" / "llm_structured.py"

spec = importlib.util.spec_from_file_location("llm_structured", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules["llm_structured"] = module
spec.loader.exec_module(module)

EVENTS_DIR = REPO / "temp" / "llm-events"

REQUIRED_FIELDS = {
    "event_version", "event_id", "event_kind", "event_source", "call_id",
    "ts", "transaction_id", "operation", "mode", "model", "profile", "reasoning_profile",
    "reasoning_reason", "reasoning_error_class", "provider_reasoning_effort",
    "attempt", "status", "latency_sec", "finish_reason", "usage",
    "recovery_policy_version", "recovery_class", "recovery_scheduled", "retryable",
    "prompt_hash", "prompt_len", "output_hash", "output_len", "error",
}


def _today_jsonl(events_dir=None):
    import time
    return Path(events_dir or module.EVENTS_DIR) / f"{time.strftime('%Y-%m-%d')}.jsonl"


def _read_today_events(events_dir=None):
    p = _today_jsonl(events_dir)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").strip().split("\n") if l.strip()]


def _isolated_events(test):
    @wraps(test)
    def wrapped():
        original_events = module.EVENTS_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                module.EVENTS_DIR = Path(directory)
                return test()
        finally:
            module.EVENTS_DIR = original_events
    return wrapped


@_isolated_events
def test_log_event_writes_jsonl():
    """_log_event 写入当天 jsonl，字段齐全。"""
    module._log_event("test_op", {"mode": "api", "model": "test-model", "status": "ok",
                                  "profile": "p1", "reasoning_profile": "fast", "attempt": 1,
                                  "latency_sec": 0.5, "finish_reason": "stop", "usage": {"total_tokens": 100},
                                  "error": ""},
                      "secret-prompt-content", "secret-output-content", "txn-test")
    events = _read_today_events()
    assert events, "无事件写入"
    last = events[-1]
    assert REQUIRED_FIELDS <= set(last), f"缺字段: {REQUIRED_FIELDS - set(last)}"
    assert last["operation"] == "test_op"
    assert last["transaction_id"] == "txn-test"
    assert last["mode"] == "api"
    assert last["status"] == "ok"
    assert last["event_version"] == module.EXECUTION_EVENT_VERSION
    assert last["event_kind"] == "llm_api_call"
    assert last["event_source"] == "llm_structured"
    assert len(last["event_id"]) == 32


@_isolated_events
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


@_isolated_events
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
    assert last_agent["event_kind"] == "agent_handoff"
    assert last_agent["prompt_len"] == len("test-prompt")


@_isolated_events
def test_empty_prompt_output_safe():
    """空 prompt/output 不崩溃，hash 为空字符串。"""
    module._log_event("empty_test", {"mode": "api", "status": "ok"}, "", "")
    events = _read_today_events()
    last = events[-1]
    assert last["prompt_hash"] == ""
    assert last["output_hash"] == ""
    assert last["prompt_len"] == 0
    assert last["output_len"] == 0


def test_typed_recovery_logs_every_actual_api_attempt_and_aggregates():
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://example.invalid",
        "LLM_API_KEY": "key",
        "LLM_MODEL": "GLM-5.3-Flash",
    }
    responses = iter([
        module.urllib.error.URLError("temporary"),
        {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
         "usage": {"total_tokens": 2}},
        {"choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}],
         "usage": {"total_tokens": 3}},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(*_args, **_kwargs):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return Response(item)

    original_env = module.load_env
    original_urlopen = module.urllib.request.urlopen
    original_events = module.EVENTS_DIR
    try:
        with tempfile.TemporaryDirectory() as directory:
            module.load_env = lambda: config
            module.urllib.request.urlopen = urlopen
            module.EVENTS_DIR = Path(directory)
            result = module.call_text(
                "bounded", retries=0,
                recovery_limits={"infrastructure": 1, "output_transport": 1},
                operation="ingest_wiki_write", transaction_id="txn-typed",
            )
            events = _read_today_events(directory)
            summary = module.summarize_execution_events("txn-typed", Path(directory))
    finally:
        module.load_env = original_env
        module.urllib.request.urlopen = original_urlopen
        module.EVENTS_DIR = original_events

    assert result["ok"] and result["text"] == "complete"
    assert result["recovery_attempts"] == {
        "infrastructure": 1, "output_transport": 1,
    }
    assert len(events) == 3
    assert len({event["call_id"] for event in events}) == 1
    assert [event["recovery_class"] for event in events] == [
        "infrastructure", "output_transport", "",
    ]
    assert [event["recovery_scheduled"] for event in events] == [True, True, False]
    assert summary["api_calls"] == 3
    assert summary["total_tokens"] == 5
    assert summary["by_operation"] == {"ingest_wiki_write": 3}


def test_malformed_provider_envelope_uses_output_transport_budget():
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://example.invalid",
        "LLM_API_KEY": "key",
        "LLM_MODEL": "GLM-5.3-Flash",
    }
    payloads = iter([
        b"not-json",
        json.dumps({
            "choices": [{"message": {"content": "complete"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        }).encode(),
    ])

    class Response:
        def read(self):
            return next(payloads)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    original_env = module.load_env
    original_urlopen = module.urllib.request.urlopen
    original_events = module.EVENTS_DIR
    try:
        with tempfile.TemporaryDirectory() as directory:
            module.load_env = lambda: config
            module.urllib.request.urlopen = lambda *_args, **_kwargs: Response()
            module.EVENTS_DIR = Path(directory)
            result = module.call_text(
                "bounded", retries=0,
                recovery_limits={"infrastructure": 0, "output_transport": 1},
                operation="ingest_wiki_write", transaction_id="txn-malformed",
            )
            events = _read_today_events(directory)
    finally:
        module.load_env = original_env
        module.urllib.request.urlopen = original_urlopen
        module.EVENTS_DIR = original_events

    assert result["ok"]
    assert result["recovery_attempts"] == {
        "infrastructure": 0, "output_transport": 1,
    }
    assert [event["mode"] for event in events] == ["api", "api"]
    assert [event["recovery_class"] for event in events] == ["output_transport", ""]


def test_http_classifier_retries_only_transient_failures():
    permanent = module.urllib.error.HTTPError(
        "https://example.invalid", 400, "Bad Request", None, None,
    )
    transient = module.urllib.error.HTTPError(
        "https://example.invalid", 503, "Unavailable", None, None,
    )
    assert module._classify_http_error(permanent)[0] is False
    assert module._classify_http_error(transient)[0] is True
    assert module._classify_http_error(ValueError("local bug"))[0] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"llm events regression: {passed}/{len(tests)} PASS")

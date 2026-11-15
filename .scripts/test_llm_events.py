#!/usr/bin/env python3
"""llm_structured.py 事件日志（temp/llm-events/）回归测试。

验证点：
- _log_event 写入 temp/llm-events/YYYY-MM-DD.jsonl
- 事件字段齐全（ts/transaction_id/operation/.../prompt_hash/prompt_len/output_hash/output_len）
- 不含明文 prompt/output（只有 hash + length）
- 写盘失败静默降级，不抛异常
- agent backend 在进入事件或 provider 逻辑前被拒绝
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from functools import wraps
from pathlib import Path
from unittest.mock import patch

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
def test_agent_backend_rejected_before_api_event():
    """llm_structured is API-only; Agent work must use agent-task-v1."""
    os.environ["QUERY_BACKEND"] = "agent"
    try:
        errors = []
        for call in (
            lambda: module.call_json("test-prompt", lambda obj: True, operation="query"),
            lambda: module.call_text("test-prompt", operation="query"),
        ):
            try:
                call()
            except RuntimeError as exc:
                errors.append(str(exc))
            else:
                raise AssertionError("agent backend 必须被 llm_structured 拒绝")
    finally:
        os.environ.pop("QUERY_BACKEND", None)
    assert len(errors) == 2
    assert all("仅支持 API backend" in error for error in errors)
    assert _read_today_events() == []


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


def _json_transport_probe(responses, **kwargs):
    """Offline provider: exercise request budgets and the real JSON/schema boundary."""
    config = {
        "QUERY_BACKEND": "api", "LLM_API_BASE": "https://example.invalid",
        "LLM_API_KEY": "key", "LLM_MODEL": "test-model",
        "LLM_REASONING_FIELD": "reasoning_effort",
        "LLM_REASONING_EFFORT_DEEP": "high", "LLM_REASONING_EFFORT_FAST": "low",
    }
    replies = iter(responses)
    captured = []

    class Response:
        def __init__(self, content, finish_reason):
            self.payload = {"choices": [{"message": content if isinstance(content, dict) else {"content": content},
                                         "finish_reason": finish_reason}],
                            "usage": {"total_tokens": 3}}

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, **_kwargs):
        captured.append(json.loads(request.data))
        return Response(*next(replies))

    options = {"max_tokens": 2000, "retries": 1, "reasoning": "deep",
               "operation": "query_api_loop", "allow_profile_fallback": False}
    options.update(kwargs)
    schema_check = options.pop("schema_check", lambda obj: obj == {"answer": "complete"})
    with tempfile.TemporaryDirectory() as directory, \
            patch.object(module, "load_env", return_value=config), \
            patch.object(module.urllib.request, "urlopen", side_effect=urlopen), \
            patch.object(module, "EVENTS_DIR", Path(directory)):
        result = module.call_json("bounded query", schema_check,
                                  **options)
        events = _read_today_events(directory)
    return result, captured, events


def test_literal_json_whitespace_is_lossless_without_model_retry():
    value = 'line one\nline two\rthird\tcell \\LaTeX "quote" 中文'
    text = json.dumps({"answer": value}, ensure_ascii=False)
    broken = text.replace(r'\n', '\n').replace(r'\r', '\r').replace(r'\t', '\t')
    result, requests, events = _json_transport_probe(
        [(broken, "stop")], schema_check=lambda obj: obj == {"answer": value})
    assert result["ok"] and result["parsed"]["answer"] == value
    assert len(requests) == len(events) == len(result["history"]) == 1
    assert result["local_json_recovery"] == "literal_string_whitespace"
    assert events[0]["local_json_recovery"] == result["history"][0]["local_json_recovery"]
    assert not events[0]["recovery_scheduled"] and "line one" not in json.dumps(events)
    assert module.decode_json_response(text) == ({"answer": value}, None, "")
    assert module.parse_json(broken) == ({"answer": value}, None)


def test_redundant_root_closer_recovers_complete_values_without_retry():
    value={"answer":'complete } ] 中文 \\path "quote"',"items":[{"number":2}]}
    text=json.dumps(value,ensure_ascii=False)
    result,requests,events=_json_transport_probe([(text+'\n}\t}',"stop")],
        schema_check=lambda obj:obj==value)
    assert result["ok"] and result["parsed"]==value
    assert len(requests)==len(events)==len(result["history"])==1
    assert result["local_json_recovery"]=="redundant_top_level_closer"
    assert events[0]["local_json_recovery"]==result["history"][0]["local_json_recovery"]
    assert not events[0]["recovery_scheduled"]
    assert module.decode_json_response('[1,{"x":2}]\n]')==([1,{"x":2}],None,"redundant_top_level_closer")
    assert module.decode_json_response(text)==(value,None,"")


def test_redundant_root_closer_rejects_ambiguous_or_incomplete_structure():
    for text in ['{"answer":"complete"}]','[1]}','{"answer":"complete"} {}',
                 '{"answer":"complete"} trailing','{"answer":"complete"},"extra":1}',
                 '{"answer":"complete"','{"items":[1,]}','"scalar"}',
                 '{"answer":"a\nb"}}','{"answer":"complete"}\x0b}']:
        value,error,recovery=module.decode_json_response(text)
        assert value is None and error and not recovery,text
    result,requests,events=_json_transport_probe([('{"answer":"wrong"}}',"stop")],retries=0)
    assert not result["ok"] and result["error"]=="schema 校验失败"
    assert result["parsed"]=={"answer":"wrong"} and len(requests)==1
    assert events[0]["local_json_recovery"]=="redundant_top_level_closer"


def test_complete_json_with_suffix_comments_preserves_values_and_one_request():
    value = {"answer": '中文 // literal https://example.invalid/a \\path "quote"\nnext',
             "items": [{"false": False, "zero": 0, "literal": "{}[]/*text*/"}]}
    encoded = json.dumps(value, ensure_ascii=False)
    for suffix in ['\n// 等待工具结果。', ' // explanatory note',
                   '\r\n // first note\r\n\t// second note\r\n', '\n//\n\n']:
        raw = encoded + suffix
        result, requests, events = _json_transport_probe(
            [(raw, "stop")], schema_check=lambda obj: obj == value)
        assert result["ok"] and result["parsed"] == value and result["text"] == raw
        assert len(requests) == len(events) == len(result["history"]) == 1
        assert result["local_json_recovery"] == "trailing_line_comments"
        assert events[0]["local_json_recovery"] == result["history"][0]["local_json_recovery"]
        assert not events[0]["recovery_scheduled"]
        assert events[0]["output_len"] == len(raw) and 'explanatory' not in json.dumps(events)
    assert module.decode_json_response('[1,{"x":false}]\n// array', allow_trailing_comments=True) == (
        [1, {"x": False}], None, "trailing_line_comments")
    assert module.decode_json_response(encoded, allow_trailing_comments=True) == (value, None, "")


def test_suffix_comments_reject_ambiguous_incomplete_and_combined_repair():
    for raw in ['{}\n// note\n{}', '{} {} // note', '{}\n// note\n[]',
                '{}\n// note\ntrue', '{}\n// note\n"another value"',
                '{}\n// alternative {"action":"other"}', '{}\n// alternative []',
                '{},"extra":1 // note', '{} plain text', '{} /* block */',
                '{/* internal */"x":1}', '// prefix\n{}', '{"x":1\n// incomplete',
                '{"x":"unfinished // comment', '{"x":1,}\n// trailing comma',
                '{"x":"a\nb"}\n// no combined recovery', '{} }\n// no combined recovery',
                '"scalar"\n// note', '{}\n// illegal\x01control', '{}\n// trailing\x0b',
                '{}\n// illegal\v\n', '{}\n\x00// note']:
        obj, error, recovery = module.decode_json_response(raw, allow_trailing_comments=True)
        assert obj is None and error and not recovery, repr(raw)
    result, requests, events = _json_transport_probe(
        [('{"answer":"wrong"}\n// note', "stop")], retries=0)
    assert not result["ok"] and result["error"] == "schema 校验失败"
    assert result["parsed"] == {"answer": "wrong"} and len(requests) == 1
    assert events[0]["local_json_recovery"] == "trailing_line_comments"


def test_suffix_recovery_requires_completed_json_transport_only():
    raw = '{"answer":"complete"}\n// note'
    assert module.decode_json_response(raw)[1]
    assert module.parse_json(raw)[1]
    for finish_reason in ("length", "content_filter", None):
        result, requests, events = _json_transport_probe([(raw, finish_reason)], retries=0)
        assert not result["ok"] and result["parsed"] is None
        assert len(requests) == 1 and not events[0]["local_json_recovery"]
    import query_orchestrate as qo
    plan = {"decision": "read", "plan": [{"action": "read_raw", "input": {"locator": "academic/raw/x.md#L1"}}]}
    message = {"content": None, "tool_calls": [{"type": "function", "function": {
        "name": "query_plan", "arguments": json.dumps(plan) + '\n// note'}}]}
    result, requests, _ = _json_transport_probe([(message, "tool_calls")], retries=0,
        response_decoder=qo._decode_native_query_response, schema_check=qo._api_decision_schema)
    assert not result["ok"] and result["parsed"] is None and len(requests) == 1


def test_local_json_recovery_does_not_guess_or_bypass_schema():
    for broken in ['{"answer":"a\x01b"}', '{"answer":"a\nb\x00"}',
                   '{"answer":"unfinished\n', r'{"answer":"bad\q"}',
                   '{"answer":"a\nb"} trailing']:
        result, requests, _ = _json_transport_probe([(broken, "stop")], retries=0)
        assert not result["ok"] and result["parsed"] is None and len(requests) == 1
    result, requests, events = _json_transport_probe([('{"answer":"wrong\nvalue"}', "stop")], retries=0)
    assert not result["ok"] and result["error"] == "schema 校验失败"
    assert result["parsed"] == {"answer": "wrong\nvalue"}
    assert events[0]["local_json_recovery"] == "literal_string_whitespace"
    assert len(requests) == 1


def test_native_decoder_uses_same_transport_events_and_retry_budget():
    import query_orchestrate as qo
    tools = qo._native_query_tools()
    plan = {"decision": "read", "plan": [{"action": "read_raw", "input": {"locator": "academic/raw/x.md#L1"}}]}
    message = {"content": None, "tool_calls": [{"type": "function", "function": {
        "name": "query_plan", "arguments": json.dumps(plan)}}]}
    result, requests, events = _json_transport_probe([(message, "tool_calls")],
        tools=tools, response_decoder=qo._decode_native_query_response, schema_check=qo._api_decision_schema)
    assert result["ok"] and result["parsed"] == plan
    assert requests[0]["tools"] == tools and requests[0]["tool_choice"] == "auto"
    assert len(events) == len(result["history"]) == 1
    assert "query_plan" in result["text"] and events[0]["output_len"] == len(result["text"])
    result, requests, events = _json_transport_probe([(message, "length"), (message, "tool_calls")],
        tools=tools, response_decoder=qo._decode_native_query_response, schema_check=qo._api_decision_schema)
    assert result["ok"] and [r["max_tokens"] for r in requests] == [2000, 4000]
    assert [r["reasoning_effort"] for r in requests] == ["high", "high"]
    assert [e["status"] for e in events] == ["validation_error", "ok"]
    result, requests, events = _json_transport_probe([("partial markdown", "length"), ("complete markdown", "stop")],
        tools=tools, response_decoder=qo._decode_native_query_response, schema_check=qo._api_decision_schema)
    assert result["ok"] and result["parsed"]["answer"] == "complete markdown"
    assert [r["max_tokens"] for r in requests] == [2000, 4000]
    assert [r["reasoning_effort"] for r in requests] == ["high", "high"]
    assert all(r["tools"] == tools for r in requests)
    assert "只输出修正后的 JSON" not in requests[1]["messages"][-1]["content"]
    assert [e["status"] for e in events] == ["validation_error", "ok"]
    result, requests, events = _json_transport_probe([("partial markdown", "length")], retries=0,
        response_decoder=qo._decode_native_query_response, schema_check=qo._api_decision_schema)
    assert not result["ok"] and len(requests) == len(events) == 1


def test_nonempty_truncated_json_expands_existing_retry_and_keeps_reasoning():
    for options in ({"retries": 1}, {"retries": 0, "recovery_limits": {
            "infrastructure": 0, "output_transport": 1}}):
        result, requests, events = _json_transport_probe([
            ('{"answer":"unfinished', "length"), ('{"answer":"complete"}', "stop"),
        ], **options)
        assert result["ok"] and result["parsed"] == {"answer": "complete"}
        assert [r["max_tokens"] for r in requests] == [2000, 4000]
        assert [r["reasoning_effort"] for r in requests] == ["high", "high"]
        assert [e["status"] for e in events] == ["validation_error", "ok"]
        assert [e["recovery_scheduled"] for e in events] == [True, False]
        assert len({e["call_id"] for e in events}) == 1
        assert [h["max_tokens"] for h in result["history"]] == [2000, 4000]
        if "recovery_limits" in options:
            assert result["recovery_attempts"] == {"infrastructure": 0, "output_transport": 1}


def test_truncated_json_without_output_retry_fails_without_budget_promise():
    for options in ({"retries": 0}, {"retries": 5, "recovery_limits": {
            "infrastructure": 2, "output_transport": 0}}):
        result, requests, events = _json_transport_probe([
            ('{"answer":"unfinished', "length"),
        ], **options)
        assert not result["ok"] and result["parsed"] is None
        assert len(requests) == len(events) == 1
        assert result["next_max_tokens"] is None and not result["recovery_scheduled"]


def test_json_retry_expansion_requires_length_and_parse_error():
    for content, finish_reason in [
            ('{"answer":"unfinished', "stop"),
            ('{"answer":"unfinished', None),
            ('{"answer":"wrong"}', "length")]:
        result, requests, _ = _json_transport_probe([
            (content, finish_reason), ('{"answer":"complete"}', "stop"),
        ])
        assert result["ok"]
        assert [r["max_tokens"] for r in requests] == [2000, 2000]
    result, requests, _ = _json_transport_probe([('{"answer":"complete"}', "length")])
    assert result["ok"] and len(requests) == 1


def test_json_truncation_growth_is_capped_without_lowering_caller_limit():
    for initial, expected in [(40000, 65536), (65536, 65536), (70000, 70000)]:
        result, requests, _ = _json_transport_probe([
            ('{"answer":"unfinished', "length"), ('{"answer":"complete"}', "stop"),
        ], max_tokens=initial)
        assert result["ok"]
        assert [r["max_tokens"] for r in requests] == [initial, expected]


def test_json_repeated_truncation_exhausts_original_attempt_count():
    result, requests, events = _json_transport_probe([
        ('{"answer":"first unfinished', "length"),
        ('{"answer":"second unfinished', "length"),
    ])
    assert not result["ok"] and result["parsed"] is None
    assert [r["max_tokens"] for r in requests] == [2000, 4000]
    assert len(events) == 2 and result["next_max_tokens"] is None
    assert not result["recovery_scheduled"]


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


@_isolated_events
def test_specialist_can_disable_profile_fallback_without_changing_default():
    from unittest.mock import patch
    config = {"INGEST_BACKEND": "api", "LLM_API_BASE": "https://example.invalid",
              "LLM_API_KEY": "key", "LLM_MODEL": "primary",
              "SEMANTIC_RECOVERY_API_BASE": "https://example.invalid",
              "SEMANTIC_RECOVERY_API_KEY": "key", "SEMANTIC_RECOVERY_MODEL": "candidate"}
    requested = []

    class Response:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                               "usage": {"total_tokens": 3}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def urlopen(request, **_kwargs):
        model = json.loads(request.data)["model"]
        requested.append(model)
        if model == "candidate":
            raise module.urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)
        return Response()

    with patch.object(module, "load_env", return_value=config), \
            patch.object(module.urllib.request, "urlopen", side_effect=urlopen):
        result = module.call_json("probe", lambda value: value == {}, retries=0,
                                  operation="ingest_semantic_recovery", allow_profile_fallback=False)
        assert not result["ok"] and requested == ["candidate"]
        assert not result["fallback_used"] and len(result["history"]) == 1
        requested.clear()
        result = module.call_json("probe", lambda value: value == {}, retries=0,
                                  operation="ingest_semantic_recovery")
        assert result["ok"] and requested == ["candidate", "primary"]
        assert result["fallback_used"] and len(result["history"]) == 2
        requested.clear()
        for operation in ("ingest_meeting_repair", "ingest_content_repair"):
            requested.clear()
            result = module.call_text("probe", retries=0, operation=operation,
                                      allow_profile_fallback=False)
            assert not result["ok"] and requested == ["candidate"]
            assert not result["fallback_used"] and len(result["history"]) == 1
        requested.clear()
        result = module.call_text("probe", retries=0, operation="ingest_meeting_repair")
        assert result["ok"] and requested == ["candidate", "primary"]
        assert result["fallback_used"] and len(result["history"]) == 2



def test_wiki_recovery_profile_isolated_and_optional():
    config = {"LLM_API_BASE": "https://primary.invalid", "LLM_API_KEY": "key", "LLM_MODEL": "primary"}
    for prefix, model in (("INGEST_GENERATION", "flash"), ("WIKI_RECOVERY", "ev")):
        config.update({f"{prefix}_API_BASE": "https://specialist.invalid",
                       f"{prefix}_API_KEY": "key", f"{prefix}_MODEL": model})
    assert module.api_profiles(config, "ingest_wiki_repair")[0]["model"] == "ev"
    assert module.api_profiles(config, "ingest_wiki_write")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_paper_workspace")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_semantic_recovery")[0]["model"] == "primary"
    config["WIKI_RECOVERY_MODEL"] = "primary"
    assert module.api_profiles(config, "ingest_wiki_repair")[0]["name"] == "wiki_recovery"
    config.pop("WIKI_RECOVERY_API_KEY")
    assert module.api_profiles(config, "ingest_wiki_repair")[0]["model"] == "flash"



def test_long_document_recovery_reuses_specialist_and_keeps_generation_separate():
    config = {"LLM_API_BASE": "https://primary.invalid", "LLM_API_KEY": "key", "LLM_MODEL": "main"}
    for prefix, model in (("INGEST_GENERATION", "flash"), ("SEMANTIC_RECOVERY", "ev")):
        config.update({f"{prefix}_API_BASE": "https://specialist.invalid", f"{prefix}_API_KEY": "key", f"{prefix}_MODEL": model})
    assert module.api_profiles(config, "ingest_long_document")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_long_document_repair")[0]["model"] == "ev"
    assert module.api_profiles(config, "ingest_meeting_compile")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_meeting_repair")[0]["model"] == "ev"
    assert module.api_profiles(config, "ingest_content_repair")[0]["model"] == "ev"
    config['SEMANTIC_RECOVERY_MODEL'] = 'main'
    assert module.api_profiles(config, "ingest_long_document_repair")[0]["name"] == 'semantic_recovery'
    assert module.api_profiles(config, "ingest_meeting_repair")[0]["name"] == 'semantic_recovery'
    assert module.api_profiles(config, "ingest_content_repair")[0]["name"] == 'semantic_recovery'
    config.pop('SEMANTIC_RECOVERY_API_KEY')
    assert module.api_profiles(config, "ingest_long_document_repair")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_meeting_repair")[0]["model"] == "flash"
    assert module.api_profiles(config, "ingest_content_repair")[0]["model"] == "flash"


def _http429_probe(outcomes, *, text=False, config_update=None, **kwargs):
    config = {"QUERY_BACKEND": "api", "INGEST_BACKEND": "api",
              "LLM_API_BASE": "https://example.invalid", "LLM_API_KEY": "test-key",
              "LLM_MODEL": "from-model", "LLM_429_FALLBACK_FROM_MODEL": "from-model",
              "LLM_429_FALLBACK_MODEL": "to-model", "LLM_REASONING_FIELD": "reasoning_effort",
              "LLM_REASONING_EFFORT_STANDARD": "low"}
    config.update(config_update or {})
    requested = []

    class Response:
        def __init__(self, value): self.value = value
        def read(self):
            return json.dumps({"choices": [{"message": self.value.get("message", {"content": self.value.get("content", "{}")}),
                                             "finish_reason": self.value.get("finish_reason", "stop")}],
                               "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}}).encode()
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    def urlopen(request, **_kwargs):
        requested.append({"url": request.full_url, "headers": dict(request.headers), "payload": json.loads(request.data)})
        value = outcomes[len(requested)-1]
        if isinstance(value, int):
            raise module.urllib.error.HTTPError(request.full_url, value, "test HTTP error", None, None)
        if isinstance(value, Exception): raise value
        return Response(value)

    args = {"retries": 0, "allow_profile_fallback": False, "reasoning": "standard", **kwargs}
    with patch.object(module, "load_env", return_value=config), \
            patch.object(module.urllib.request, "urlopen", side_effect=urlopen), \
            patch.object(module, "_backoff_sleep") as sleep:
        if text:
            result = module.call_text("probe", **args)
        else:
            result = module.call_json("probe", lambda value: value == {}, **args)
        assert not sleep.called
    return result, requested, _read_today_events()


@_isolated_events
def test_request_duration_ignores_wall_clock_jumps_for_all_outcomes():
    for text in (False, True):
        for jump in (1000, -1000):
            for outcome in ({}, {"content": "" if text else "invalid JSON"}, TimeoutError("timed out")):
                with patch.object(module.time, "time", side_effect=[2000, 2000 + jump]), \
                        patch.object(module.time, "perf_counter", side_effect=[10, 12.25]):
                    result, requests, events = _http429_probe([outcome], text=text)
                assert len(requests) == len(result["history"]) == 1
                assert result["latency_sec"] == 2.25
                assert result["history"][0]["latency_sec"] == events[-1]["latency_sec"] == 2.25
                expected = "api_error" if isinstance(outcome, Exception) else "validation_error" if outcome else "ok"
                assert result["status"] == expected


@_isolated_events
def test_each_http429_attempt_has_its_own_monotonic_duration():
    for text in (False, True):
        with patch.object(module.time, "time", side_effect=[2000, 5000, 5000, 1000]), \
                patch.object(module.time, "perf_counter", side_effect=[10, 12, 20, 23.5]):
            result, requests, events = _http429_probe([429, {}], text=text)
        assert result["ok"] and len(requests) == 2
        assert [e["latency_sec"] for e in events[-2:]] == [2, 3.5]
        assert [e["latency_sec"] for e in result["history"]] == [2, 3.5]
        assert result["latency_sec"] == 3.5
        assert events[-2]["usage"] == {} and events[-1]["usage"]["total_tokens"] == 3


@_isolated_events
def test_http429_switch_preserves_payload_and_failed_usage():
    result, requests, events = _http429_probe([429, {}], recovery_limits={"infrastructure": 0, "output_transport": 0})
    assert result["ok"] and result["model"] == "to-model" and result["rate_limit_fallback_used"]
    assert [r["payload"]["model"] for r in requests] == ["from-model", "to-model"]
    assert requests[1] == {**requests[0], "payload": {**requests[0]["payload"], "model": "to-model"}}
    assert result["recovery_attempts"] == {"infrastructure": 0, "output_transport": 0, "rate_limit_fallback": 1}
    assert len(events) == len(result["history"]) == 2
    assert events[0]["http_status"] == 429 and events[0]["rate_limit_fallback_model"] == "to-model"
    assert events[0]["usage"] == {} and events[1]["usage"]["total_tokens"] == 3
    assert events[0]["call_id"] == events[1]["call_id"]
    assert events[0]["prompt_hash"] == events[1]["prompt_hash"]


@_isolated_events
def test_http429_text_preserves_full_messages_and_next_call_primary():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "first"},
                {"role": "assistant", "content": "prior"}, {"role": "user", "content": "next"}]
    result, requests, _ = _http429_probe([429, {"content": "answer"}], text=True, messages=messages, retries=3)
    assert result["ok"] and result["text"] == "answer"
    assert all(r["payload"]["messages"] == messages for r in requests)
    again, requests, _ = _http429_probe([{"content": "next answer"}], text=True)
    assert again["ok"] and not again["rate_limit_fallback_used"]
    assert requests[0]["payload"]["model"] == "from-model"


@_isolated_events
def test_http429_native_tools_keep_decoder_and_schema():
    tools = [{"type": "function", "function": {"name": "choose", "parameters": {"type": "object"}}}]
    message = {"content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "choose", "arguments": "{}"}}]}
    decoded = []
    def decoder(choice):
        decoded.append(choice)
        return json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"]), None, None
    result, requests, _ = _http429_probe([429, {"message": message, "finish_reason": "tool_calls"}], tools=tools, response_decoder=decoder)
    assert result["ok"] and len(decoded) == 1
    assert all(r["payload"]["tools"] == tools and r["payload"]["tool_choice"] == "auto" for r in requests)


@_isolated_events
def test_http429_switch_requires_exact_status_and_explicit_pair():
    for error in [400, 401, 403, 500, ValueError("HTTP 429 in text")]:
        result, requests, _ = _http429_probe([error])
        assert not result["ok"] and len(requests) == 1
        assert not result["rate_limit_fallback_used"]
    for update in [{"LLM_429_FALLBACK_FROM_MODEL": "other"}, {"LLM_429_FALLBACK_FROM_MODEL": ""},
                   {"LLM_429_FALLBACK_MODEL": ""}, {"LLM_429_FALLBACK_MODEL": "from-model"}]:
        result, requests, _ = _http429_probe([429], config_update=update)
        assert not result["ok"] and len(requests) == 1
    result, requests, _ = _http429_probe([429], allow_rate_limit_fallback=False)
    assert not result["ok"] and len(requests) == 1


@_isolated_events
def test_http429_alternate_failure_stops_retry_and_profile_chains():
    specialist = {"SEMANTIC_RECOVERY_API_BASE": "https://example.invalid", "SEMANTIC_RECOVERY_API_KEY": "test-key",
                  "SEMANTIC_RECOVERY_MODEL": "from-model", "LLM_MODEL": "third-model"}
    for text in (False, True):
        for failure in [429, 500, {"content": "", "finish_reason": "length"}, {"content": "{\"bad\":1}"}]:
            result, requests, _ = _http429_probe([429, failure], text=text, retries=3,
                operation="ingest_semantic_recovery", allow_profile_fallback=True, config_update=specialist)
            assert len(requests) == 2
            assert result["rate_limit_fallback_used"] and not result["recovery_scheduled"]
            if not text or not isinstance(failure, dict) or not failure["content"]:
                assert not result["ok"]


@_isolated_events
def test_http429_after_output_retry_preserves_exact_failed_request():
    result, requests, _ = _http429_probe([{"content": '{"unfinished', "finish_reason": "length"}, 429, {}], retries=1)
    assert result["ok"] and len(requests) == 3
    assert requests[2] == {**requests[1], "payload": {**requests[1]["payload"], "model": "to-model"}}
    assert requests[2]["payload"]["max_tokens"] == 1600


@_isolated_events
def test_http429_configuration_never_enables_agent_api():
    for text in (False, True):
        try:
            _http429_probe([], text=text, config_update={"QUERY_BACKEND": "agent"})
        except RuntimeError as exc:
            assert "API backend" in str(exc)
        else:
            raise AssertionError("Agent must be rejected before provider calls")
    assert _read_today_events() == []


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        passed += 1
        print(f"  {t.__name__}: PASS")
    print(f"llm events regression: {passed}/{len(tests)} PASS")

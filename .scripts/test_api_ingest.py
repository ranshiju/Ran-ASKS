#!/usr/bin/env python3
"""受限 API 摄入：证据校验、候选约束与安全编译回归。"""
import sys
import tempfile
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import api_ingest
import llm_structured


def isolated_call_json(*args, **kwargs):
    original_events = llm_structured.EVENTS_DIR
    try:
        with tempfile.TemporaryDirectory() as directory:
            llm_structured.EVENTS_DIR = Path(directory)
            return llm_structured.call_json(*args, **kwargs)
    finally:
        llm_structured.EVENTS_DIR = original_events


def test_claim_validation_rejects_unquoted_claims():
    cards = [
        {"id": "E1", "raw_locator": "abstract", "evidence_quote": "FermiNet improves VMC accuracy."},
        {"id": "E2", "raw_locator": "discussion", "evidence_quote": "Future work studies larger systems."},
    ]
    accepted, rejected = api_ingest.validate_claims({"claims": [
        {"field": "method", "claim": "使用 FermiNet。", "evidence_id": "E1"},
        {"field": "limitation", "claim": "不可扩展。", "evidence_id": "E9"},
    ]}, cards)
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert accepted[0]["evidence_quote"] == "FermiNet improves VMC accuracy."


def test_evidence_cards_are_program_owned():
    cards = api_ingest.evidence_cards({
        "abstract": "First source sentence is sufficiently long. Second source sentence is also sufficiently long.",
        "discussion": "Discussion sentence is sufficiently long for a card.",
    })
    assert [card["id"] for card in cards] == ["E1", "E2", "E3"]
    assert cards[0]["raw_locator"] == "abstract"
    assert cards[-1]["raw_locator"] == "discussion"


def test_repair_schema_keeps_single_evidence_id():
    assert api_ingest.repair_schema({"claims": [
        {"field": "method", "claim": "短声明。", "evidence_id": "E1"},
    ]})
    assert not api_ingest.repair_schema({"claims": [
        {"field": "method", "claim": "短声明。", "evidence_id": ["E1", "E2"]},
    ]})


def test_keyword_schema_rejects_invented_candidate():
    schema = api_ingest.keyword_schema(["FermiNet", "变分量子蒙特卡洛"])
    assert schema({"selected": [{"term": "FermiNet", "evidence_id": "E1"}], "uncertain": []})
    assert not schema({"selected": [{"term": "新造关键词", "evidence_id": "E1"}], "uncertain": []})


def test_selected_keywords_require_single_evidence_card():
    cards = [{"id": "E1", "raw_locator": "abstract", "evidence_quote": "FermiNet is used."}]
    accepted, rejected = api_ingest.validate_selected_keywords(
        [{"term": "FermiNet", "evidence_id": "E1"}, {"term": "DMC", "evidence_id": "E9"}], cards,
    )
    assert accepted == [{"term": "FermiNet", **cards[0]}]
    assert rejected[0]["reason"] == "关键词缺少有效证据卡编号"


def test_empty_api_result_is_incomplete():
    accepted, rejected = api_ingest.validate_claims({"claims": []}, [])
    assert accepted == []
    assert rejected == []


def test_overflow_claims_are_nonblocking_when_valid_claims_exist():
    cards = [{"id": f"E{number}", "raw_locator": "abstract", "evidence_quote": f"Evidence {number}."}
             for number in range(1, 5)]
    accepted, rejected = api_ingest.validate_claims({"claims": [
        {"field": "method", "claim": f"Claim {number}", "evidence_id": f"E{number}"}
        for number in range(1, 5)
    ]}, cards)
    assert len(accepted) == 3
    assert rejected[0]["reason"] == "同字段条目超限"


def test_specialist_profiles_fall_back_to_primary_when_unconfigured():
    config = {"LLM_API_BASE": "primary-base", "LLM_API_KEY": "primary-key", "LLM_MODEL": "DeepSeek-V3.2"}
    assert llm_structured.api_profiles(config, "ingest_api_keywords") == [{
        "name": "primary", "base": "primary-base", "key": "primary-key", "model": "DeepSeek-V3.2",
    }]


def test_env_reference_expansion_reuses_primary_credentials():
    expanded = llm_structured.expand_env_references({
        "LLM_API_BASE": "https://api.example/v1", "LLM_API_KEY": "secret",
        "INGEST_KEYWORD_API_BASE": "${LLM_API_BASE}", "INGEST_KEYWORD_API_KEY": "${LLM_API_KEY}",
    })
    assert expanded["INGEST_KEYWORD_API_BASE"] == "https://api.example/v1"
    assert expanded["INGEST_KEYWORD_API_KEY"] == "secret"


def test_specialist_profiles_precede_primary_when_complete():
    config = {
        "LLM_API_BASE": "primary-base", "LLM_API_KEY": "primary-key", "LLM_MODEL": "DeepSeek-V3.2",
        "INGEST_KEYWORD_API_BASE": "minimax-base", "INGEST_KEYWORD_API_KEY": "minimax-key", "INGEST_KEYWORD_MODEL": "MiniMax-M3",
    }
    profiles = llm_structured.api_profiles(config, "ingest_api_keywords")
    assert [profile["model"] for profile in profiles] == ["MiniMax-M3", "DeepSeek-V3.2"]


def test_proposition_profile_prefers_dedicated_then_generation_then_primary():
    config = {
        "LLM_API_BASE": "primary-base", "LLM_API_KEY": "primary-key", "LLM_MODEL": "DeepSeek-V4-Flash-0731",
        "INGEST_GENERATION_API_BASE": "generation-base", "INGEST_GENERATION_API_KEY": "generation-key",
        "INGEST_GENERATION_MODEL": "MiniMax-M3",
    }
    profiles = llm_structured.api_profiles(config, "ingest_proposition")
    assert [profile["model"] for profile in profiles] == ["MiniMax-M3", "DeepSeek-V4-Flash-0731"]
    config.update({
        "INGEST_PROPOSITION_API_BASE": "proposition-base",
        "INGEST_PROPOSITION_API_KEY": "proposition-key",
        "INGEST_PROPOSITION_MODEL": "GLM-4.5-Flash",
    })
    profiles = llm_structured.api_profiles(config, "ingest_proposition")
    assert [profile["model"] for profile in profiles] == [
        "GLM-4.5-Flash", "MiniMax-M3", "DeepSeek-V4-Flash-0731",
    ]


def test_semantic_recovery_profile_is_optional_and_falls_back_to_primary():
    primary = {
        "LLM_API_BASE": "primary-base", "LLM_API_KEY": "primary-key",
        "LLM_MODEL": "GLM-5.3-Flash",
    }
    assert [profile["model"] for profile in llm_structured.api_profiles(
        primary, "ingest_semantic_recovery"
    )] == ["GLM-5.3-Flash"]
    configured = dict(primary)
    configured.update({
        "SEMANTIC_RECOVERY_API_BASE": "agent-base",
        "SEMANTIC_RECOVERY_API_KEY": "agent-key",
        "SEMANTIC_RECOVERY_MODEL": "AWS-GPT-5.6-Terra",
    })
    assert [profile["model"] for profile in llm_structured.api_profiles(
        configured, "ingest_semantic_recovery"
    )] == ["AWS-GPT-5.6-Terra", "GLM-5.3-Flash"]


def test_reasoning_profile_mapping_and_validation():
    config = {}
    assert llm_structured.reasoning_profile(config, "ingest_api_keywords") == "fast"
    assert llm_structured.reasoning_profile(config, "ingest_proposition") == "fast"
    assert llm_structured.reasoning_profile(config, "ingest_api_claims") == "standard"
    assert llm_structured.reasoning_profile({"LLM_REASONING_DEFAULT": "deep"}, "query") == "deep"
    assert llm_structured.reasoning_profile({"LLM_REASONING_INGEST_API_CLAIMS": "fast"}, "ingest_api_claims") == "fast"
    try:
        llm_structured.reasoning_profile({"LLM_REASONING_DEFAULT": "invalid"}, "query")
    except RuntimeError as exc:
        assert "无效 reasoning profile" in str(exc)
    else:
        raise AssertionError("invalid reasoning profile must fail")


def test_adaptive_reasoning_only_escalates_semantic_retry():
    initial = llm_structured.reasoning_decision(
        {}, "ingest_wiki_write", context={"document_kind": "paper", "retry": 0})
    structural = llm_structured.reasoning_decision(
        {}, "ingest_wiki_write", context={
            "document_kind": "paper", "retry": 1,
            "validation_errors": ["frontmatter 格式错误"],
        })
    semantic = llm_structured.reasoning_decision(
        {}, "ingest_wiki_write", context={
            "document_kind": "paper", "retry": 1,
            "validation_errors": ["研究方向定位没有精确 Raw locator 脚注"],
        })
    slots = llm_structured.reasoning_decision(
        {}, "ingest_semantic_extract", context={
            "document_kind": "ordinary", "retry": 1,
            "failure_kind": "semantic", "validation_errors": ["谓词不合法"],
        })
    assert initial["profile"] == "standard"
    assert structural["profile"] == "standard"
    assert structural["error_class"] == "structural"
    assert semantic["profile"] == "deep"
    assert semantic["error_class"] == "semantic"
    assert slots["profile"] == "deep"
    configured = llm_structured.reasoning_decision(
        {"LLM_REASONING_INGEST_WIKI_WRITE": "fast"},
        "ingest_wiki_write", context={"retry": 1, "failure_kind": "semantic"})
    assert configured["profile"] == "fast"
    assert configured["reason"].startswith("operation_config:")


def test_reasoning_request_options_require_explicit_provider_config():
    assert llm_structured.reasoning_request_options({}, "fast") == {}
    assert llm_structured.reasoning_request_options({"LLM_REASONING_FIELD": "reasoning_effort"}, "fast") == {}
    assert llm_structured.reasoning_request_options({
        "LLM_REASONING_FIELD": "reasoning_effort", "LLM_REASONING_EFFORT_FAST": "low",
    }, "fast") == {"reasoning_effort": "low"}
    assert llm_structured.reasoning_request_options({
        "LLM_REASONING_FIELD": "reasoning_effort", "LLM_REASONING_EFFORT_FAST": "none",
    }, "fast") == {}


def test_specialist_schema_failure_falls_back_to_primary_without_agent(monkeypatch=None):
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://primary.example", "LLM_API_KEY": "primary-key", "LLM_MODEL": "DeepSeek-V3.2",
        "INGEST_KEYWORD_API_BASE": "https://minimax.example", "INGEST_KEYWORD_API_KEY": "minimax-key", "INGEST_KEYWORD_MODEL": "MiniMax-M3",
    }
    original_load_env, original_urlopen = llm_structured.load_env, llm_structured.urllib.request.urlopen
    responses = iter([
        {"choices": [{"message": {"content": '{"selected":[{"term":"invented","evidence_id":"E1"}],"uncertain":[]}'}, "finish_reason": "stop"}], "usage": {"total_tokens": 4}},
        {"choices": [{"message": {"content": '{"selected":[{"term":"known","evidence_id":"E1"}],"uncertain":[]}'}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    llm_structured.load_env = lambda: config
    llm_structured.urllib.request.urlopen = lambda *_args, **_kwargs: Response(next(responses))
    try:
        result = isolated_call_json(
            "test", api_ingest.keyword_schema(["known"]), retries=0, operation="ingest_api_keywords",
        )
    finally:
        llm_structured.load_env, llm_structured.urllib.request.urlopen = original_load_env, original_urlopen
    assert result["ok"] and result["model"] == "DeepSeek-V3.2"
    assert result["fallback_used"]
    assert result["mode"] == "api"
    assert [item["model"] for item in result["history"]] == ["MiniMax-M3", "DeepSeek-V3.2"]


def test_reasoning_profile_preserves_call_budget_and_records_audit_fields():
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://primary.example", "LLM_API_KEY": "primary-key", "LLM_MODEL": "DeepSeek-V3.2",
        "LLM_REASONING_FIELD": "reasoning_effort", "LLM_REASONING_EFFORT_FAST": "low",
    }
    captured = []

    class Response:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"selected":[{"term":"known","evidence_id":"E1"}],"uncertain":[]}'}, "finish_reason": "stop"}], "usage": {"total_tokens": 4}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, **_kwargs):
        captured.append(json.loads(request.data))
        return Response()

    original_load_env, original_urlopen = llm_structured.load_env, llm_structured.urllib.request.urlopen
    llm_structured.load_env = lambda: config
    llm_structured.urllib.request.urlopen = urlopen
    try:
        result = isolated_call_json(
            "test", api_ingest.keyword_schema(["known"]), max_tokens=4000, retries=2, operation="ingest_api_keywords",
        )
    finally:
        llm_structured.load_env, llm_structured.urllib.request.urlopen = original_load_env, original_urlopen
    assert result["ok"]
    assert result["reasoning_profile"] == "fast"
    assert result["reasoning_reason"].startswith("adaptive_initial:")
    assert result["provider_reasoning_effort"] == "low"
    assert result["max_tokens"] == 4000
    assert captured == [{
        "model": "DeepSeek-V3.2", "messages": [{"role": "system", "content": "你是受程序约束的知识库组件，只输出要求的 JSON。"}, {"role": "user", "content": "test"}],
        "temperature": 0, "max_tokens": 4000, "reasoning_effort": "low",
    }]


def test_reasoning_exhaustion_carries_low_effort_into_model_fallback():
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://primary.example", "LLM_API_KEY": "primary-key",
        "LLM_MODEL": "GLM-primary",
        "INGEST_GENERATION_API_BASE": "https://generation.example",
        "INGEST_GENERATION_API_KEY": "generation-key",
        "INGEST_GENERATION_MODEL": "GLM-generation",
        "LLM_REASONING_FIELD": "reasoning_effort",
        "LLM_REASONING_EFFORT_FAST": "low",
        "LLM_REASONING_EFFORT_DEEP": "high",
    }
    captured = []
    responses = iter([
        {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
         "usage": {"completion_tokens": 120,
                   "completion_tokens_details": {"reasoning_tokens": 120}}},
        {"choices": [{"message": {"content": '{"answer":4}'}, "finish_reason": "stop"}],
         "usage": {"total_tokens": 12}},
    ])

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return json.dumps(self.payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, **_kwargs):
        captured.append(json.loads(request.data))
        return Response(next(responses))

    original_load_env, original_urlopen = llm_structured.load_env, llm_structured.urllib.request.urlopen
    llm_structured.load_env = lambda: config
    llm_structured.urllib.request.urlopen = urlopen
    try:
        result = isolated_call_json(
            "test", lambda obj: isinstance(obj, dict) and obj.get("answer") == 4,
            max_tokens=120, retries=0, operation="ingest_proposition", reasoning="deep",
        )
    finally:
        llm_structured.load_env, llm_structured.urllib.request.urlopen = original_load_env, original_urlopen
    assert result["ok"] and result["fallback_used"]
    assert [payload["reasoning_effort"] for payload in captured] == ["high", "low"]
    assert [item["provider_reasoning_effort"] for item in result["history"]] == ["high", "low"]


def test_call_text_uses_supplied_conversation_messages():
    config = {
        "INGEST_BACKEND": "api",
        "LLM_API_BASE": "https://primary.example",
        "LLM_API_KEY": "primary-key",
        "LLM_MODEL": "DeepSeek-V3.2",
    }
    captured = []
    logged = []
    conversation = [
        {"role": "system", "content": "bounded ingest worker"},
        {"role": "user", "content": "source document"},
        {"role": "assistant", "content": "draft"},
        {"role": "user", "content": "repair only the reported error"},
    ]

    class Response:
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "repaired draft"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 4},
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, **_kwargs):
        captured.append(json.loads(request.data))
        return Response()

    def log_event(operation, result, prompt, output_text="", transaction_id=""):
        logged.append((operation, prompt, output_text, transaction_id))

    original_load_env = llm_structured.load_env
    original_urlopen = llm_structured.urllib.request.urlopen
    original_log_event = llm_structured._log_event
    llm_structured.load_env = lambda: config
    llm_structured.urllib.request.urlopen = urlopen
    llm_structured._log_event = log_event
    try:
        result = llm_structured.call_text(
            "audit label", messages=conversation, max_tokens=800, retries=0,
            operation="ingest_wiki_write", transaction_id="txn-conversation",
        )
    finally:
        llm_structured.load_env = original_load_env
        llm_structured.urllib.request.urlopen = original_urlopen
        llm_structured._log_event = original_log_event

    assert result["ok"]
    assert captured[0]["messages"] == conversation
    assert logged[-1][1] == json.dumps(conversation, ensure_ascii=False, sort_keys=True)
    assert logged[-1][3] == "txn-conversation"


def test_reasoning_exhausted_detects_length_with_high_reasoning_share():
    usage = {"completion_tokens": 16384, "completion_tokens_details": {"reasoning_tokens": 16384}}
    assert llm_structured._reasoning_exhausted("", "length", usage)
    assert llm_structured._reasoning_exhausted(
        "", "length", {"completion_tokens": 450, "completion_tokens_details": {"reasoning_tokens": 450}},
    )
    assert not llm_structured._reasoning_exhausted("有正文", "length", usage)
    assert not llm_structured._reasoning_exhausted(
        "", "length", {"completion_tokens": 16384, "completion_tokens_details": {"reasoning_tokens": 100}},
    )


def test_cost_metrics_count_api_without_agent_fallback():
    result = {"history": [
        {"profile": "ingest_keyword", "model": "MiniMax-M3", "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}},
        {"profile": "primary", "model": "DeepSeek-V3.2", "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
    ]}
    metrics = api_ingest.api_cost_metrics(result)
    assert metrics["agent_calls"] == 0
    assert metrics["api_calls"] == 2
    assert metrics["specialist_calls"] == 1
    assert metrics["primary_fallback_calls"] == 1
    assert metrics["usage"]["total_tokens"] == 27


def test_cost_metrics_do_not_cross_count_separate_operations():
    specialist_then_primary = {"history": [
        {"profile": "ingest_keyword", "model": "MiniMax-M3", "usage": {}},
        {"profile": "primary", "model": "DeepSeek-V3.2", "usage": {}},
    ]}
    independent_primary = {"profile": "primary", "model": "DeepSeek-V3.2", "usage": {}}
    metrics = api_ingest.api_cost_metrics(specialist_then_primary, independent_primary)
    assert metrics["primary_fallback_calls"] == 1


def test_agent_fallback_uses_only_minimal_evidence_package():
    result = {
        "claims_ok": False,
        "keywords_ok": False,
        "accepted_claims": [],
        "selected_keywords": [],
        "rejected": [{"reason": "非法字段或证据卡编号"}],
    }
    handoff = api_ingest.agent_fallback_handoff(
        [{"id": "E1", "raw_locator": "abstract", "evidence_quote": "evidence"}], ["known"], result,
    )
    assert handoff["status"] == "agent_fallback_required"
    assert handoff["input"]["keyword_candidates"] == ["known"]
    assert "不得重读全文 raw" in handoff["instructions"]


def test_agent_fallback_reuses_program_evidence_validation():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "paper.md"
        raw.write_text("A sufficiently long source sentence for evidence validation.", encoding="utf-8")
        old_repo = api_ingest.REPO
        api_ingest.REPO = raw.parent
        try:
            result = api_ingest.run_agent_fallback(raw, ["known"], {
                "claims": [{"field": "method", "claim": "采用受限方法。", "evidence_id": "E1"}],
                "uncertain": [], "selected": [{"term": "known", "evidence_id": "E1"}], "keyword_uncertain": [],
            })
        finally:
            api_ingest.REPO = old_repo
        assert result["complete"] and result["agent_fallback_applied"]
        assert result["metrics"]["cost"]["agent_calls"] == 1


def test_draft_contains_replay_provenance():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "paper.md"
        raw.write_text("abstract text", encoding="utf-8")
        result = api_ingest.run_draft.__name__
        assert result == "run_draft"
        metadata = api_ingest.provenance(
            raw,
            schema_version=api_ingest.SCHEMA_VERSION,
            rule_version=api_ingest.RULE_VERSION,
            prompt_version=api_ingest.PROMPT_VERSION,
            model="test-model",
        )
        assert metadata["source_fingerprint"]
        assert metadata["prompt_version"] == "api-ingest-prompts-v1"


def test_compile_refuses_incomplete_draft():
    with tempfile.TemporaryDirectory() as directory:
        page = Path(directory) / "page.md"
        page.write_text("placeholder", encoding="utf-8")
        try:
            api_ingest.compile_draft(page, {"complete": False}, Path(directory) / "semantic.txt")
        except ValueError as exc:
            assert "不完整" in str(exc)
        else:
            raise AssertionError("incomplete draft must not be compiled")


def test_compile_writes_only_verified_claims_to_skeleton():
    skeleton = """---
title: Test
---
# Test

## Navigation

<-- LLM 填 -->

## Content

### 一、问题与动机

<-- LLM 填 -->

### 二、方法/框架

<-- LLM 填 -->

### 三、主要贡献

<-- LLM 填 -->

### 四、实验/结果

<-- LLM 填 -->

### 五、局限与展望

<-- LLM 填 -->
"""
    draft = {
        "complete": True,
        "accepted_claims": [
            {"field": "motivation", "claim": "求解多电子问题。", "raw_locator": "abstract", "evidence_quote": "多电子", "id": "E1"},
            {"field": "method", "claim": "采用 FermiNet。", "raw_locator": "abstract", "evidence_quote": "FermiNet", "id": "E2"},
        ],
        "selected_keywords": [{"term": "FermiNet", "id": "E2", "raw_locator": "abstract", "evidence_quote": "FermiNet"}],
    }
    with tempfile.TemporaryDirectory() as directory:
        page = Path(directory) / "page.md"
        semantic = Path(directory) / "semantic.txt"
        page.write_text(skeleton, encoding="utf-8")
        api_ingest.compile_draft(page, draft, semantic)
        result = page.read_text(encoding="utf-8")
        assert "采用 FermiNet。" in result
        assert "原文未提取到可验证声明。" in result
        assert semantic.read_text(encoding="utf-8") == "研究关键词:\nFermiNet\n"


def test_resolve_pending_appends_auditable_resolution():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = root / "paper.md"
        raw.write_text("source", encoding="utf-8")
        pending = root / "pending.jsonl"
        pending.write_text(json.dumps({"raw": "paper.md", "state": "agent_fallback_required"}) + "\n", encoding="utf-8")
        old_repo = api_ingest.REPO
        api_ingest.REPO = root
        try:
            assert api_ingest.resolve_pending(raw, pending, "test resolution") == 1
        finally:
            api_ingest.REPO = old_repo
        records = [json.loads(line) for line in pending.read_text(encoding="utf-8").splitlines()]
        assert records[-1]["state"] == "resolved"
        assert records[-1]["resolution"] == "test resolution"


def main():
    test_claim_validation_rejects_unquoted_claims()
    test_evidence_cards_are_program_owned()
    test_repair_schema_keeps_single_evidence_id()
    test_keyword_schema_rejects_invented_candidate()
    test_empty_api_result_is_incomplete()
    test_overflow_claims_are_nonblocking_when_valid_claims_exist()
    test_specialist_profiles_fall_back_to_primary_when_unconfigured()
    test_env_reference_expansion_reuses_primary_credentials()
    test_specialist_profiles_precede_primary_when_complete()
    test_proposition_profile_prefers_dedicated_then_generation_then_primary()
    test_semantic_recovery_profile_is_optional_and_falls_back_to_primary()
    test_reasoning_profile_mapping_and_validation()
    test_adaptive_reasoning_only_escalates_semantic_retry()
    test_reasoning_request_options_require_explicit_provider_config()
    test_reasoning_profile_preserves_call_budget_and_records_audit_fields()
    test_reasoning_exhaustion_carries_low_effort_into_model_fallback()
    test_reasoning_exhausted_detects_length_with_high_reasoning_share()
    test_call_text_uses_supplied_conversation_messages()
    test_specialist_schema_failure_falls_back_to_primary_without_agent()
    test_cost_metrics_count_api_without_agent_fallback()
    test_cost_metrics_do_not_cross_count_separate_operations()
    test_agent_fallback_uses_only_minimal_evidence_package()
    test_compile_refuses_incomplete_draft()
    test_compile_writes_only_verified_claims_to_skeleton()
    test_resolve_pending_appends_auditable_resolution()
    print("api ingest regression: PASS")


if __name__ == "__main__":
    main()

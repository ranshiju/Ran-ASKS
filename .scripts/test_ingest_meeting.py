#!/usr/bin/env python3
"""Regression tests for the unified meeting compiler ingest path."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / ".scripts") not in sys.path:
    sys.path.insert(0, str(REPO / ".scripts"))

import ingest_meeting as meeting
import llm_structured
from dsh.meeting_compiler_agent import PROTOCOL_VERSION


def _workspace() -> Path:
    root = REPO / "temp" / "inbox-extract"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="test-meeting-compiler-", dir=root))


def _state(work: Path) -> dict:
    source = work / "20260903-test-meeting.txt"
    source.write_text("任老师讨论知事库。", encoding="utf-8")
    candidates = work / "entity-candidates.json"
    candidates.write_text(json.dumps({
        "resolved": [{
            "original": "任老师", "normalized": "任胜泉",
            "entity": "cnu-ren-shengquan", "method": "alias_exact",
        }],
        "review": [],
    }, ensure_ascii=False), encoding="utf-8")
    return {
        "transaction_id": work.name,
        "status": "write_wiki",
        "source": str(source.relative_to(REPO)),
        "source_filename": source.name,
        "date_str": "20260903",
        "subproject": "academic",
        "extract_dir": str(work.relative_to(REPO)),
        "entity_candidates": str(candidates.relative_to(REPO)),
        "errors": [],
    }


def _proposal() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "meta": {"doc_date": "2026-09-03", "title": "测试会议", "doc_type": "meeting"},
        "preprocess": {
            "protocol_version": PROTOCOL_VERSION,
            "transcript_replacements": [
                {"original": "任老师", "replacement": "任胜泉", "reason": "exact candidate"},
                {"original": "知事库", "replacement": "知识库", "reason": "meeting context"},
            ],
            "entity_resolutions": [
                {"mention": "任老师", "canonical": "cnu-ren-shengquan",
                 "status": "resolved", "reason": "exact candidate"},
            ],
        },
        "wiki_markdown": """---
title: 测试会议
type: conference-summary
sources:
  - "placeholder"
source_type: speech-recognition
date: 2026-09-03
confidence: low
status: current
created: 2026-09-03
updated: 2026-09-03
---
# 测试会议
> 2026-09-03 · [[authors/cnu-ren-shengquan|任胜泉]]
## Navigation
讨论知识库。
## Content
### 知识库
- 任胜泉讨论知识库。
""",
        "semantic_slots": """参会者:
cnu-ren-shengquan
汇报者:
决策:
待办:
三元组:
本会议 | 讨论 | 知识库knowledge base
""",
    }


def _output() -> str:
    proposal = _proposal()
    return """<<<META>>>
doc_date: 2026-09-03
title: 测试会议
doc_type: meeting
<<</META>>>
<<<PREPROCESS>>>
%s
<<<WIKI>>>
%s
<<<SLOTS>>>
%s
""" % (
        json.dumps(proposal["preprocess"], ensure_ascii=False),
        proposal["wiki_markdown"],
        proposal["semantic_slots"],
    )


def test_preprocess_only_builds_candidates():
    work = _workspace()
    original_run = meeting.run
    try:
        state = _state(work)
        state.pop("entity_candidates")
        commands = []

        def fake_run(command):
            commands.append(command)
            output = REPO / command[command.index("--output") + 1]
            output.write_text('{"resolved":[],"review":[]}\n', encoding="utf-8")
            return ""

        meeting.run = fake_run
        ok, error = meeting.step_preprocess(state)
        assert ok, error
        assert "--apply" not in commands[0]
        assert state["entity_candidates"].endswith("entity-candidates.json")
        assert not (work / "corrected.txt").exists()
        assert "corrected_path" not in state
    finally:
        meeting.run = original_run
        shutil.rmtree(work)


def test_api_path_uses_one_compiler_for_all_semantic_outputs():
    work = _workspace()
    original_agent = meeting.MeetingCompilerAgent
    original_mode = meeting.ingest_mode
    calls = []
    try:
        state = _state(work)

        class FakeAgent:
            def __init__(self, task):
                calls.append(task)

            def run(self):
                return SimpleNamespace(
                    status="compiled", reason="proposal_ready", proposal=_proposal(),
                    trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
                )

        meeting.MeetingCompilerAgent = FakeAgent
        meeting.ingest_mode = lambda: "api"
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert len(calls) == 1
        assert "任胜泉讨论知识库" in (work / "corrected.txt").read_text(encoding="utf-8")
        assert state["slots_content"].startswith("参会者:")
        assert state["semantic_worker"] == "meeting-compiler-api"
        assert state["meeting_id"] == "0903-测试会议"
        assert state["meeting_id_source"] == "compiler_meta"
        assert state["date_inferred"] is False
        resolution = json.loads((work / "entity-resolution.json").read_text(encoding="utf-8"))
        assert resolution["protocol_version"] == PROTOCOL_VERSION
        assert resolution["compiler_entity_resolutions"][0]["canonical"] == "cnu-ren-shengquan"
        assert state["raw_dir"] in (work / "wiki.md").read_text(encoding="utf-8")
        ok, error = meeting.step_write_slots(state)
        assert ok, error
        assert len(calls) == 1
    finally:
        meeting.MeetingCompilerAgent = original_agent
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_date_context_preserves_explicit_year_and_marks_mmdd_inference():
    assert meeting.extract_meeting_date("20250901-lab.txt") == "20250901"
    assert meeting.generate_meeting_id("20250901-lab.txt", "组会") == "0901-组会"
    explicit = meeting.meeting_date_context("20250901", today="2026-09-04")
    assert explicit == {
        "date": "2025-09-01", "storage_year": "2025",
        "date_inferred": False, "date_basis": "filename_yyyymmdd",
    }
    inferred = meeting.meeting_date_context("0901", today="2026-09-04")
    assert inferred == {
        "date": "2026-09-01", "storage_year": "2026",
        "date_inferred": True, "date_basis": "filename_mmdd_plus_ingest_year",
    }


def test_dedup_uses_full_date_and_subproject_scope():
    import graph_lib

    work = _workspace()
    original_repo = meeting.REPO
    original_connect = graph_lib.connect
    queries = []

    class FakeCursor:
        def fetchall(self):
            return []

    class FakeConnection:
        def execute(self, sql, params):
            queries.append((sql, params))
            return FakeCursor()

        def close(self):
            pass

    try:
        meeting.REPO = work
        graph_lib.connect = lambda: FakeConnection()
        duplicate, message = meeting.step_dedup_check({
            "source_filename": "20250901-lab.txt", "subproject": "academic",
        })
        assert not duplicate and not message
        assert queries[0][1] == (
            "2025-09-01", "academic/wiki/conferences/%",
        )
    finally:
        meeting.REPO = original_repo
        graph_lib.connect = original_connect
        shutil.rmtree(work)


def test_mmdd_compiler_output_marks_inferred_date_and_rebases_id():
    work = _workspace()
    original_agent = meeting.MeetingCompilerAgent
    try:
        state = _state(work)
        state["source_filename"] = "0901-test-inferred.txt"
        state["date_str"] = "0901"
        proposal = _proposal()
        proposal["meta"]["title"] = "测试会议推断日期"

        class FakeAgent:
            def __init__(self, _task):
                pass

            def run(self):
                return SimpleNamespace(
                    status="compiled", reason="proposal_ready", proposal=proposal,
                    trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
                )

        meeting.MeetingCompilerAgent = FakeAgent
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert state["meeting_id"] == "0901-测试会议推断日期"
        assert state["date_inferred"] is True
        assert "date_inferred: true" in state["wiki_content"]
    finally:
        meeting.MeetingCompilerAgent = original_agent
        shutil.rmtree(work)


def test_rejected_compiler_records_attempt_and_protocol_error():
    work = _workspace()
    original_agent = meeting.MeetingCompilerAgent
    try:
        state = _state(work)

        class RejectingAgent:
            def __init__(self, _task):
                pass

            def run(self):
                return SimpleNamespace(
                    status="rejected", reason="invalid preprocess JSON", proposal=None,
                    trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "rejected"},
                )

        meeting.MeetingCompilerAgent = RejectingAgent
        ok, error = meeting.step_write_wiki(state)
        assert not ok and "invalid preprocess JSON" in error
        assert state["compiler_errors"] == ["invalid preprocess JSON"]
        assert state["meeting_compiler_attempts"][-1]["status"] == "rejected"
    finally:
        meeting.MeetingCompilerAgent = original_agent
        shutil.rmtree(work)


def test_agent_handoff_roundtrip_consumes_same_protocol():
    work = _workspace()
    original_agent = meeting.MeetingCompilerAgent
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)

        class HandoffAgent:
            def __init__(self, task):
                self.task = task

            def run(self):
                return SimpleNamespace(
                    status="agent_required", reason="host_agent_required", proposal=None,
                    prompt=self.task.prompt,
                    trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "agent_required"},
                )

        meeting.MeetingCompilerAgent = HandoffAgent
        meeting.ingest_mode = lambda: "agent"
        ok, error = meeting.step_write_wiki(state)
        assert not ok and "sub-agent" in error
        assert state["_awaiting_agent_wiki_slots"] is True
        assert state["agent_write_to"].endswith("agent-meeting-compiler.txt")
        assert "任老师讨论知事库。" not in state["agent_prompt"]
        assert state["source"] in state["agent_prompt"]
        output = REPO / state["agent_write_to"]
        output.write_text(_output(), encoding="utf-8")
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert state["semantic_worker"] == "meeting-compiler-agent"
        assert state["meeting_compiler"]["reason"] == "host_agent_output_validated"
        assert "agent_write_to" not in state
    finally:
        meeting.MeetingCompilerAgent = original_agent
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_exhausted_revision_handoff_uses_full_protocol_without_inline_source():
    work = _workspace()
    try:
        state = _state(work)
        state["meeting_id"] = "20260903-test-meeting"
        state["raw_dir"] = "academic/raw/conferences/2026/20260903-test-meeting"
        ok, error = meeting.step_prepare_unified_handoff(
            state, ["缺少 ## Content 段"],
        )
        assert ok, error
        assert state["_awaiting_agent_wiki_slots"] is True
        assert state["agent_write_to"].endswith("agent-meeting-compiler.txt")
        assert "<<<PREPROCESS>>>" in state["agent_prompt"]
        assert "<<<WIKI>>>" in state["agent_prompt"]
        assert "<<<SLOTS>>>" in state["agent_prompt"]
        assert "任老师讨论知事库。" not in state["agent_prompt"]
        assert state["source"] in state["agent_prompt"]
        assert state["meeting_compiler"]["reason"] == "wiki_revision_budget_exhausted"
    finally:
        shutil.rmtree(work)


def test_prompt_requires_one_coherent_protocol():
    prompt = meeting.build_agent_meeting_wiki_slots_prompt(
        "任老师讨论知事库", '{"resolved":[]}', "meeting-test", "0903",
        "academic/raw/conferences/test.txt", "2026-09-03", "2026-09-03",
    )
    assert "同一上下文中一次性完成" in prompt
    assert "<<<PREPROCESS>>>" in prompt
    assert "<<<WIKI>>>" in prompt
    assert "<<<SLOTS>>>" in prompt
    assert PROTOCOL_VERSION in prompt
    assert "只读事实源" in prompt


def test_meeting_compiler_uses_ingest_generation_profile():
    profiles = llm_structured.api_profiles({
        "LLM_API_BASE": "https://primary.invalid/v1",
        "LLM_API_KEY": "primary-key",
        "LLM_MODEL": "primary-model",
        "INGEST_GENERATION_API_BASE": "https://generation.invalid/v1",
        "INGEST_GENERATION_API_KEY": "generation-key",
        "INGEST_GENERATION_MODEL": "generation-model",
    }, "ingest_meeting_compile")
    assert [profile["name"] for profile in profiles] == ["ingest_generation", "primary"]


def test_semantic_retry_returns_to_same_compiler():
    state = {
        "transaction_id": "meeting-unified-retry",
        "status": "write_wiki",
        "extract_dir": "temp/inbox-extract/meeting-unified-retry",
        "errors": [],
    }
    compiler_calls = []

    def write_wiki(current):
        compiler_calls.append(list(current.get("compiler_errors", [])))
        current["wiki_content"] = "valid wiki"
        current["slots_content"] = "invalid slots"
        return True, ""

    spec = {
        "script_name": "test_meeting_driver.py",
        "unified_semantic_worker": True,
        "recovery_limits": {
            "wiki_revision": 1, "semantic_revision": 1,
            "deterministic_repair": 1, "subagent": 1,
        },
        "steps": {
            "write_wiki": write_wiki,
            "validate_wiki": lambda _state: [],
            "write_slots": meeting.step_write_slots,
            "validate_semantics": lambda _state: (["三元组格式错误"], []),
        },
        "normalize_slots": lambda text: text,
    }
    pipeline = meeting.ingest_pipeline
    original_save = pipeline._save
    original_fill = pipeline.ic.step_fill_semantics
    original_recovery = pipeline.ic.try_semantic_recovery
    try:
        pipeline._save = lambda _state: None
        pipeline.ic.step_fill_semantics = lambda *_args, **_kwargs: (True, "")
        pipeline.ic.try_semantic_recovery = lambda *_args, **_kwargs: (False, "not resolved")
        result = pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        pipeline._save = original_save
        pipeline.ic.step_fill_semantics = original_fill
        pipeline.ic.try_semantic_recovery = original_recovery

    assert compiler_calls == [[], ["三元组格式错误"]]
    assert result["status"] == "agent_required"
    assert result["recovery"]["attempts"]["semantic_revision"] == 1


def test_wiki_retry_exhaustion_hands_off_full_compiler_protocol():
    state = {
        "transaction_id": "meeting-unified-wiki-handoff",
        "status": "write_wiki",
        "extract_dir": "temp/inbox-extract/meeting-unified-wiki-handoff",
        "errors": [],
    }
    handoffs = []

    def write_wiki(current):
        current["wiki_content"] = "invalid wiki"
        current["slots_content"] = "valid slots"
        return True, ""

    def prepare_handoff(current, errors, handoff_reason):
        handoffs.append(list(errors))
        current["handoff_reason"] = handoff_reason
        current["_awaiting_agent_wiki_slots"] = True
        current["agent_prompt"] = "return <<<PREPROCESS>>> + <<<WIKI>>> + <<<SLOTS>>>"
        current["agent_write_to"] = "temp/inbox-extract/meeting-unified-wiki-handoff/agent-meeting-compiler.txt"
        return True, ""

    spec = {
        "script_name": "test_meeting_driver.py",
        "unified_semantic_worker": True,
        "recovery_limits": {
            "wiki_revision": 0, "semantic_revision": 1,
            "deterministic_repair": 1, "subagent": 1,
        },
        "steps": {
            "write_wiki": write_wiki,
            "validate_wiki": lambda _state: ["缺少 ## Content 段"],
            "prepare_unified_handoff": prepare_handoff,
        },
        "normalize_slots": lambda text: text,
    }
    pipeline = meeting.ingest_pipeline
    original_save = pipeline._save
    try:
        pipeline._save = lambda _state: None
        result = pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        pipeline._save = original_save

    assert handoffs == [["缺少 ## Content 段"]]
    assert result["status"] == "agent_required"
    assert result["_awaiting_agent_wiki_slots"] is True
    assert result.get("_awaiting_agent_wiki") is None
    assert result["agent_write_to"].endswith("agent-meeting-compiler.txt")
    assert "<<<PREPROCESS>>>" in result["agent_prompt"]
    assert result["handoff_reason"] == "wiki_revision_budget_exhausted"


def main():
    test_preprocess_only_builds_candidates()
    test_api_path_uses_one_compiler_for_all_semantic_outputs()
    test_date_context_preserves_explicit_year_and_marks_mmdd_inference()
    test_dedup_uses_full_date_and_subproject_scope()
    test_mmdd_compiler_output_marks_inferred_date_and_rebases_id()
    test_rejected_compiler_records_attempt_and_protocol_error()
    test_agent_handoff_roundtrip_consumes_same_protocol()
    test_exhausted_revision_handoff_uses_full_protocol_without_inline_source()
    test_prompt_requires_one_coherent_protocol()
    test_meeting_compiler_uses_ingest_generation_profile()
    test_semantic_retry_returns_to_same_compiler()
    test_wiki_retry_exhaustion_hands_off_full_compiler_protocol()
    print("ingest meeting tests: PASS")


if __name__ == "__main__":
    main()

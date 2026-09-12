#!/usr/bin/env python3
"""Regression tests for the unified meeting compiler ingest path."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / ".scripts") not in sys.path:
    sys.path.insert(0, str(REPO / ".scripts"))

import ingest_meeting as meeting
import llm_structured
from meeting_compiler_contract import PROTOCOL_VERSION


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
    original_runner = meeting.run_api_meeting_compiler
    original_mode = meeting.ingest_mode
    calls = []
    try:
        state = _state(work)

        def fake_runner(task_fields):
            calls.append(task_fields)
            return SimpleNamespace(
                status="compiled", reason="proposal_ready", proposal=_proposal(),
                trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
            )

        meeting.run_api_meeting_compiler = fake_runner
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
        meeting.run_api_meeting_compiler = original_runner
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_agent_path_prepares_task_without_entering_api_adapter():
    work = _workspace()
    original_runner = meeting.run_api_meeting_compiler
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)
        meeting.ingest_mode = lambda: "agent"
        meeting.run_api_meeting_compiler = lambda _task: (_ for _ in ()).throw(
            AssertionError("Agent backend must not enter the API adapter")
        )
        ok, error = meeting.step_write_wiki(state)
        assert not ok and error == "Agent task prepared"
        assert state["status"] == "prepared"
        assert state["agent_task"]["schema"] == "agent-task-v1"
        assert state["agent_task"]["kind"] == "ingest_meeting"
        assert "agent_prompt" not in state
    finally:
        meeting.run_api_meeting_compiler = original_runner
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
    original_runner = meeting.run_api_meeting_compiler
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)
        state["source_filename"] = "0901-test-inferred.txt"
        state["date_str"] = "0901"
        proposal = _proposal()
        proposal["meta"]["title"] = "测试会议推断日期"

        meeting.ingest_mode = lambda: "api"
        meeting.run_api_meeting_compiler = lambda _task: SimpleNamespace(
            status="compiled", reason="proposal_ready", proposal=proposal,
            trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
        )
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert state["meeting_id"] == "0901-测试会议推断日期"
        assert state["date_inferred"] is True
        assert "date_inferred: true" in state["wiki_content"]
    finally:
        meeting.run_api_meeting_compiler = original_runner
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_rejected_compiler_records_attempt_and_protocol_error():
    work = _workspace()
    original_runner = meeting.run_api_meeting_compiler
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)
        meeting.ingest_mode = lambda: "api"
        meeting.run_api_meeting_compiler = lambda _task: SimpleNamespace(
            status="rejected", reason="invalid preprocess JSON", proposal=None,
            trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "rejected"},
        )
        ok, error = meeting.step_write_wiki(state)
        assert not ok and "invalid preprocess JSON" in error
        assert state["compiler_errors"] == ["invalid preprocess JSON"]
        assert state["meeting_compiler_attempts"][-1]["status"] == "rejected"
    finally:
        meeting.run_api_meeting_compiler = original_runner
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_agent_task_roundtrip_consumes_same_protocol():
    work = _workspace()
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)
        meeting.ingest_mode = lambda: "agent"
        ok, error = meeting.step_write_wiki(state)
        assert not ok and error == "Agent task prepared"
        assert state["_awaiting_agent_wiki_slots"] is True
        assert state["status"] == "prepared"
        assert "agent_prompt" not in state
        output = REPO / state["agent_task"]["outputs"][0]["path"]
        output.write_text(_output(), encoding="utf-8")
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert state["semantic_worker"] == "meeting-compiler-agent"
        assert state["meeting_compiler"]["reason"] == "host_agent_output_validated"
        assert state["agent_task"]["status"] == "consumed"
        assert "agent_write_to" not in state
    finally:
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
    original_backend = pipeline.ic.agent_task.ingest_backend
    try:
        pipeline._save = lambda _state: None
        pipeline.ic.step_fill_semantics = lambda *_args, **_kwargs: (True, "")
        pipeline.ic.try_semantic_recovery = lambda *_args, **_kwargs: (False, "not resolved")
        pipeline.ic.agent_task.ingest_backend = lambda: "api"
        result = pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        pipeline._save = original_save
        pipeline.ic.step_fill_semantics = original_fill
        pipeline.ic.try_semantic_recovery = original_recovery
        pipeline.ic.agent_task.ingest_backend = original_backend

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


def test_api_retry_injects_latest_persisted_response_and_diagnostic():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    work = _workspace()
    calls = []
    failed_outputs = [
        _output().replace("<<<WIKI>>>", "<<</PREPROCESS>>>\n<<<WIKI>>>"),
        _output().replace("<<<WIKI>>>", "extra text from second attempt\n<<<WIKI>>>"),
        _output().replace("<<<WIKI>>>", "extra text from third attempt\n<<<WIKI>>>"),
    ]

    def fake_call(prompt, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": failed_outputs[len(calls) - 1]}

    def runner(fields):
        return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

    try:
        state = _state(work)
        with patch.object(meeting, "run_api_meeting_compiler", runner), patch.object(meeting, "ingest_mode", return_value="api"):
            for index, failed in enumerate(failed_outputs):
                ok, error = meeting.step_write_wiki(state)
                assert not ok and "invalid preprocess JSON" in error
                latest = state["meeting_compiler_attempts"][-1]
                payload = json.loads((REPO / latest["output_artifact"]).read_text())
                assert payload["response_text"] == failed
                assert payload["attempt"] == index + 1
                assert payload["transaction_id"] == state["transaction_id"]
                if index == 0:
                    assert calls[-1]["messages"] is None
                else:
                    messages = calls[-1]["messages"]
                    assert messages[2]["content"] == failed_outputs[index - 1]
                    previous = json.loads((work / f"compiler-attempt-{index}.json").read_text())
                    details = json.loads(messages[-1]["content"].split("[当前校验错误与上轮解析诊断]\n")[1])
                    assert details["diagnostic"] == previous["diagnostic"]
                state = json.loads(json.dumps(state))
            assert "wiki_content" not in state and "slots_content" not in state
            assert not (work / "corrected.txt").exists() and not (work / "wiki.md").exists()
            assert meeting._compiler_retry_context(state, "changed-input") == {}
            artifact = REPO / state["meeting_compiler_attempts"][-1]["output_artifact"]
            artifact.write_text("tampered", encoding="utf-8")
            ok, error = meeting.step_write_wiki(state)
            assert not ok and "hash mismatch" in error and len(calls) == 3
            state["meeting_compiler_attempts"].append({"status": "escalated"})
            assert meeting._compiler_retry_context(state, latest["input_hash"]) == {}
    finally:
        shutil.rmtree(work)


def test_real_worker_protocol_retry_stops_before_commit():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    work = _workspace()
    calls = []
    failed = _output().replace("<<<WIKI>>>", "<<</PREPROCESS>>>\n<<<WIKI>>>")

    def fake_call(prompt, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": failed}

    def runner(fields):
        return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

    def forbidden_step(*args, **kwargs):
        raise AssertionError("invalid protocol must never reach validation or commit")

    try:
        state = _state(work)
        spec = {
            "script_name": "ingest_meeting.py", "unified_semantic_worker": True,
            "recovery_limits": meeting.RECOVERY_LIMITS,
            "steps": {"write_wiki": meeting.step_write_wiki,
                      "prepare_unified_handoff": meeting.step_prepare_unified_handoff,
                      "validate_wiki": forbidden_step, "finalize": forbidden_step,
                      "update_graph": forbidden_step},
        }
        with patch.object(meeting, "run_api_meeting_compiler", runner), patch.object(meeting, "ingest_mode", return_value="api"), patch.object(meeting.ingest_pipeline, "_save"):
            result = meeting.ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
        assert len(calls) == 2
        assert calls[1]["messages"][2]["content"] == failed
        assert result["status"] == "agent_required"
        assert len(result["meeting_compiler_attempts"]) == 2
        assert not result.get("wiki_content") and not result.get("slots_content")
        assert not (work / "wiki.md").exists() and not (work / "corrected.txt").exists()
    finally:
        shutil.rmtree(work)


def test_content_retry_injects_previously_compiled_output():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    work = _workspace()
    calls = []

    def fake_call(prompt, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "text": _output()}

    def runner(fields):
        return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

    try:
        state = _state(work)
        with patch.object(meeting, "run_api_meeting_compiler", runner), patch.object(meeting, "ingest_mode", return_value="api"):
            ok, error = meeting.step_write_wiki(state)
            assert ok, error
            state["wiki_errors"] = ["缺少 ## Content 段"]
            ok, error = meeting.step_write_wiki(state)
            assert ok, error
        assert len(calls) == 2 and calls[1]["messages"][2]["content"] == _output()
        repair = calls[1]["messages"][-1]["content"]
        assert "缺少 ## Content 段" in repair and "本轮只修复" not in repair
        assert json.loads(repair.split("[当前校验错误与上轮解析诊断]\n")[1])["diagnostic"] == {}
    finally:
        shutil.rmtree(work)


def test_agent_parse_diagnostic_does_not_enter_api_retry():
    work = _workspace()
    try:
        state = _state(work)
        with patch.object(meeting, "ingest_mode", return_value="agent"), patch.object(meeting, "run_api_meeting_compiler", side_effect=AssertionError("Agent must not call API")):
            ok, _error = meeting.step_write_wiki(state)
            assert not ok
            output = REPO / state["agent_task"]["outputs"][0]["path"]
            output.write_text(_output().replace("<<<WIKI>>>", "<<</PREPROCESS>>>\n<<<WIKI>>>"))
            ok, error = meeting.step_write_wiki(state)
            assert not ok and error == "invalid preprocess JSON"
            assert state["agent_task"]["status"] == "prepared"
            diagnostic = json.loads(state["agent_task"]["issues"][-1])
            assert diagnostic["message"] == "Extra data" and diagnostic["kind"] == "json_syntax"
            assert "meeting_compiler_attempts" not in state
    finally:
        shutil.rmtree(work)


def test_source_binding_rebases_all_yaml_styles_in_both_backends():
    # Regression: META removes the source-heading prefix after the compiler has
    # already emitted the fallback path, including a perfectly valid YAML scalar.
    forms = [
        "sources: placeholder",
        'sources: "placeholder"',
        "sources: 'placeholder'",
        "sources: [placeholder]",
        'sources:\n  - "placeholder"',
        "sources:\n  - path: placeholder",
        "sources:\n  - placeholder\n  - stale-second-source",
        "sources: >-\n  placeholder",
        "",  # This program-owned field can be omitted by the compiler.
    ]
    for backend in ("api", "agent"):
        for form in forms:
            work = _workspace()
            try:
                state = _state(work)
                proposal = _proposal()
                old = 'sources:\n  - "placeholder"'
                proposal["wiki_markdown"] = proposal["wiki_markdown"].replace(old, form)
                body = proposal["wiki_markdown"].split("\n---", 1)[1]
                result = SimpleNamespace(
                    status="compiled", reason="proposal_ready", proposal=proposal,
                    trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
                )
                with patch.object(meeting, "ingest_mode", return_value=backend), patch.object(
                    meeting, "run_api_meeting_compiler", return_value=result,
                ) as runner:
                    if backend == "agent":
                        ok, _ = meeting.step_write_wiki(state)
                        assert not ok and state["_awaiting_agent_wiki_slots"]
                        output = _output().replace(old, form)
                        (work / "agent-meeting-compiler.txt").write_text(output, encoding="utf-8")
                        parsed, parse_error, _ = meeting.parse_proposal_detailed(output)
                        assert parsed, parse_error
                        body = parsed["wiki_markdown"].split("\n---", 1)[1]
                    ok, error = meeting.step_write_wiki(state)
                    assert ok, (backend, form, error)
                    assert runner.call_count == (1 if backend == "api" else 0)
                assert state["meeting_id_rebased_from"] != state["meeting_id"]
                expected = f"{state['raw_dir']}/{state['source_filename']}"
                fm, _ = meeting._meeting_frontmatter(state["wiki_content"])
                assert fm["sources"] == [expected], (backend, form, fm)
                assert state["wiki_content"].split("\n---", 1)[1] == body
                assert meeting.step_validate_wiki(state) == []
                assert (work / "wiki.md").read_text() == state["wiki_content"]
            finally:
                shutil.rmtree(work)


def test_wiki_validation_rejects_noncanonical_or_missing_sources():
    expected = "academic/raw/conferences/2026/0903-test/input.txt"
    wiki = meeting.bind_meeting_source(_proposal()["wiki_markdown"], expected)
    state = {"raw_dir": expected.rsplit("/", 1)[0], "source_filename": "input.txt",
             "wiki_content": wiki}
    assert meeting.step_validate_wiki(state) == []
    for bad in ("wrong/path.txt", "memory://placeholder", "", expected + "-stale"):
        state["wiki_content"] = meeting.bind_meeting_source(wiki, bad)
        assert any("sources" in e for e in meeting.step_validate_wiki(state)), bad
    state["wiki_content"] = wiki.replace(f"- {expected}", f"- {expected}\n- extra.txt")
    assert any("sources" in e for e in meeting.step_validate_wiki(state))
    state["wiki_content"] = wiki
    state.pop("raw_dir")
    assert any("sources" in e for e in meeting.step_validate_wiki(state))
    for bad in ("not yaml", "---\n- not-a-mapping\n---\nbody", "---\nsources: [\n---\nbody"):
        try:
            meeting.bind_meeting_source(bad, expected)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid header accepted: {bad}")


def main():
    test_source_binding_rebases_all_yaml_styles_in_both_backends()
    test_wiki_validation_rejects_noncanonical_or_missing_sources()
    test_api_retry_injects_latest_persisted_response_and_diagnostic()
    test_real_worker_protocol_retry_stops_before_commit()
    test_content_retry_injects_previously_compiled_output()
    test_agent_parse_diagnostic_does_not_enter_api_retry()
    test_preprocess_only_builds_candidates()
    test_api_path_uses_one_compiler_for_all_semantic_outputs()
    test_agent_path_prepares_task_without_entering_api_adapter()
    test_date_context_preserves_explicit_year_and_marks_mmdd_inference()
    test_dedup_uses_full_date_and_subproject_scope()
    test_mmdd_compiler_output_marks_inferred_date_and_rebases_id()
    test_rejected_compiler_records_attempt_and_protocol_error()
    test_agent_task_roundtrip_consumes_same_protocol()
    test_exhausted_revision_handoff_uses_full_protocol_without_inline_source()
    test_prompt_requires_one_coherent_protocol()
    test_meeting_compiler_uses_ingest_generation_profile()
    test_semantic_retry_returns_to_same_compiler()
    test_wiki_retry_exhaustion_hands_off_full_compiler_protocol()
    print("ingest meeting tests: PASS")


if __name__ == "__main__":
    main()

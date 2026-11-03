#!/usr/bin/env python3
"""Regression tests for the unified meeting compiler ingest path."""
from __future__ import annotations

import json
import io
from contextlib import redirect_stdout
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
import graph_ingest
import llm_structured
import repair_meeting_identity as meeting_repair
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
        "meeting_ir": {
            "protocol_version": PROTOCOL_VERSION,
            "attendees": [{"person": "cnu-ren-shengquan", "label": "任胜泉",
                           "evidence_ids": ["s0001"]}],
            "topics": [{"label": "知识库knowledge base", "predicate": "讨论",
                        "evidence_ids": ["s0001"]}],
            "reports": [{"person": "cnu-ren-shengquan", "person_label": "任胜泉",
                         "topic": "知识库knowledge base", "evidence_ids": ["s0001"]}],
            "decisions": [{"text": "采用知识库方案", "evidence_ids": ["s0001"]}],
            "tasks": [{"text": "验证知识库方案", "assignee": "cnu-ren-shengquan",
                       "assignee_label": "任胜泉", "evidence_ids": ["s0001"]}],
            "relations": [],
            "person_updates": [],
        },
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
<<<MEETING_IR>>>
%s
""" % (
        json.dumps(proposal["preprocess"], ensure_ascii=False),
        proposal["wiki_markdown"],
        json.dumps(proposal["meeting_ir"], ensure_ascii=False),
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
        assert "验证知识库方案 | cnu-ren-shengquan" in state["slots_content"]
        assert "本会议 | 讨论 | 知识库knowledge base" in state["slots_content"]
        assert state["semantic_worker"] == "meeting-compiler-api"
        assert state["meeting_id"] == "0903-测试会议"
        assert state["meeting_id_source"] == "compiler_meta"
        assert state["date_inferred"] is False
        resolution = json.loads((work / "entity-resolution.json").read_text(encoding="utf-8"))
        assert resolution["protocol_version"] == PROTOCOL_VERSION
        assert resolution["compiler_entity_resolutions"][0]["canonical"] == "cnu-ren-shengquan"
        assert state["raw_dir"] in (work / "wiki.md").read_text(encoding="utf-8")
        rendered = (work / "wiki.md").read_text(encoding="utf-8")
        assert "### 会议导航" in rendered and "#### 待办事项" in rendered
        assert "[^meeting-s0001]:" in rendered and "#L1" in rendered
        assert (work / "meeting-ir.json").is_file()
        fm, _body = meeting._meeting_frontmatter(rendered)
        triples, *_rest = graph_ingest.parse_semantic_text(
            state["slots_content"], state["wiki_path"], fm,
        )
        locator_report = graph_ingest.attach_wiki_section_sources(
            triples, state["wiki_path"], page_file=work / "wiki.md",
            raw_overrides={
                f"{state['raw_dir']}/{state['source_filename']}": REPO / state["source"],
            },
        )
        assert locator_report == {"located_edges": len(triples), "unlocated_edges": 0}
        ok, error = meeting.step_write_slots(state)
        assert ok, error
        assert len(calls) == 1
    finally:
        meeting.run_api_meeting_compiler = original_runner
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_meeting_ir_rejects_unbound_evidence_before_staging():
    work = _workspace()
    try:
        state = _state(work)
        proposal = _proposal()
        proposal["meeting_ir"]["tasks"][0]["evidence_ids"] = ["s9999"]
        result = SimpleNamespace(
            status="compiled", reason="proposal_ready", proposal=proposal,
            trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
        )
        with patch.object(meeting, "run_api_meeting_compiler", return_value=result), patch.object(
            meeting, "ingest_mode", return_value="api",
        ):
            ok, error = meeting.step_write_wiki(state)
        assert not ok and "evidence" in error
        assert not (work / "meeting-ir.json").exists()
        assert not (work / "wiki.md").exists()
    finally:
        shutil.rmtree(work)


def test_meeting_ir_rejects_superseded_and_final_decision_in_one_row():
    proposal = _proposal()
    proposal["meeting_ir"]["decisions"] = [{
        "text": "先冻结 Encoder；后导师纠正为全量放开训练",
        "evidence_ids": ["s0001"],
    }]
    errors = meeting.validate_meeting_ir(proposal["meeting_ir"], {"s0001"})
    assert any("superseded proposal" in error for error in errors)


def test_meeting_projection_patch_requires_one_exact_decision_match():
    original = _proposal()["meeting_ir"]
    old_text = original["decisions"][0]["text"]
    patch_value = {
        "schema": "meeting-ir-repair-patch-v1",
        "replace_decisions": [{
            "old_text": old_text,
            "new_text": "采用经复核的知识库方案",
            "evidence_ids": ["s0001"],
        }],
    }
    repaired = meeting_repair._apply_meeting_ir_patch(original, patch_value)
    assert repaired["decisions"][0]["text"] == "采用经复核的知识库方案"
    assert original["decisions"][0]["text"] == old_text
    patch_value["replace_decisions"][0]["old_text"] = "不存在的决策"
    try:
        meeting_repair._apply_meeting_ir_patch(original, patch_value)
    except ValueError as exc:
        assert "matched=0" in str(exc)
    else:
        raise AssertionError("non-matching meeting repair patch was accepted")


def test_unresolved_entities_become_quality_warnings():
    work = _workspace()
    original_runner = meeting.run_api_meeting_compiler
    original_mode = meeting.ingest_mode
    try:
        state = _state(work)
        proposal = _proposal()
        proposal["preprocess"]["entity_resolutions"].append({
            "mention": "同音姓名", "canonical": "", "status": "unresolved",
            "reason": "候选不足",
        })
        result = SimpleNamespace(
            status="compiled", reason="proposal_ready", proposal=proposal,
            trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"},
        )
        meeting.run_api_meeting_compiler = lambda _fields: result
        meeting.ingest_mode = lambda: "api"
        ok, error = meeting.step_write_wiki(state)
        assert ok, error
        assert state["quality_status"] == "degraded"
        assert state["quality_warnings"] == [{
            "issue": "meeting_entity_unresolved",
            "mention": "同音姓名",
            "reason": "候选不足",
        }]
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
        protocol = state["agent_task"]["protocol"]
        assert protocol["meta"]["fields"] == ["doc_date", "title", "doc_type"]
        assert protocol["wiki"]["frontmatter"]["sources"] == [state["agent_task"]["context"]["target_source_path"]]
        for section, rows in _proposal()["meeting_ir"].items():
            if isinstance(rows, list) and rows:
                assert set(protocol["meeting_ir"]["records"][section]["fields"]) == set(rows[0])
        from meeting_compiler_contract import ROLE_EVIDENCE, WIKI_CONTENT_CONTRACT
        for section, meaning in ROLE_EVIDENCE.items():
            assert protocol["meeting_ir"]["records"][section]["role_evidence"] == meaning
        records = protocol["meeting_ir"]["records"]
        assert {section for section, record in records.items() if "unresolved_identity" in record} == {
            "attendees", "reports", "tasks",
        }
        assert protocol["wiki"]["content"] == WIKI_CONTENT_CONTRACT
        prompt = meeting._compiler_request(state, "任老师讨论知事库。", {}, host_agent=False)[0]
        assert WIKI_CONTENT_CONTRACT in prompt
        assert ROLE_EVIDENCE["tasks"] in prompt
        assert ROLE_EVIDENCE["relations"] in prompt
        assert "agent_prompt" not in state
    finally:
        meeting.run_api_meeting_compiler = original_runner
        meeting.ingest_mode = original_mode
        shutil.rmtree(work)


def test_date_context_preserves_explicit_year_and_marks_mmdd_inference():
    assert meeting.extract_meeting_date("20250901-lab.txt") == "20250901"
    assert meeting.extract_meeting_date("2025-09-01-lab.txt") == "20250901"
    assert meeting.extract_meeting_date("2026-10-22-巴黎联合国教科文组织会议纪要.txt") == "20261022"
    assert meeting.extract_meeting_date("2024-02-29_meeting.txt") == "20240229"
    assert meeting.extract_meeting_date("0229-meeting.txt") == "0229"
    for filename in ("2026-meeting.txt", "2026-02-29-meeting.txt", "20260229-meeting.txt", "1332-meeting.txt"):
        assert meeting.extract_meeting_date(filename) == ""
    assert meeting.generate_meeting_id("20250901-lab.txt", "组会") == "0901-组会"
    assert meeting.generate_meeting_id("2025-09-01-lab.txt", "组会") == "0901-组会"
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


def test_dedup_same_date_different_meeting_continues():
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        source = repo / "inbox/20250901-2.txt"
        stored = repo / "academic/raw/conferences/2025/0901-first-topic/0901-1.txt"
        source.parent.mkdir(parents=True)
        stored.parent.mkdir(parents=True)
        source.write_text("会议主题：第二个课题\n不同议程。", encoding="utf-8")
        stored.write_text("会议主题：第一个课题\n已有议程。", encoding="utf-8")
        state = {"source": str(source.relative_to(repo)), "source_filename": source.name,
                 "subproject": "academic", "dedup_result": [{"path": "stale"}]}
        original_repo = meeting.REPO
        try:
            meeting.REPO = repo
            duplicate, message = meeting.step_dedup_check(state)
        finally:
            meeting.REPO = original_repo
        assert not duplicate and not message
        assert "dedup_result" not in state


def test_dedup_split_meeting_is_independent_of_segment_order():
    for landed_name, landed_text, incoming_name, incoming_text in (
        ("0901-1.txt", "同一会议前半段", "20250901-2.txt", "同一会议后半段"),
        ("0901-2.txt", "同一会议后半段", "20250901-1.txt", "同一会议前半段"),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            source = repo / "inbox" / incoming_name
            stored = repo / "academic/raw/conferences/2025/0901-session" / landed_name
            source.parent.mkdir(parents=True)
            stored.parent.mkdir(parents=True)
            source.write_text(incoming_text, encoding="utf-8")
            stored.write_text(landed_text, encoding="utf-8")
            state = {"source": str(source.relative_to(repo)), "source_filename": source.name,
                     "subproject": "academic"}
            original_repo = meeting.REPO
            try:
                meeting.REPO = repo
                duplicate, message = meeting.step_dedup_check(state)
            finally:
                meeting.REPO = original_repo
            assert not duplicate and not message
            assert "dedup_result" not in state


def test_dedup_exact_fingerprint_precedes_graph_and_date_candidates():
    import graph_lib

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        source = repo / "inbox/renamed.txt"
        stored = repo / "academic/raw/conferences/existing/source.txt"
        source.parent.mkdir(parents=True)
        stored.parent.mkdir(parents=True)
        source.write_text("same meeting", encoding="utf-8")
        stored.write_bytes(source.read_bytes())
        meeting.sf.register_source(
            stored, db_path=repo / "cross-domain/source-fingerprints.db", repo=repo,
        )
        state = {"source": "inbox/renamed.txt", "source_filename": source.name,
                 "subproject": "academic"}
        original_repo = meeting.REPO
        try:
            meeting.REPO = repo
            with patch.object(graph_lib, "connect", side_effect=AssertionError("graph must not run")):
                duplicate, message = meeting.step_dedup_check(state)
            assert duplicate
            assert "SHA-256" in message
            assert state["dedup_result"] == [{
                "path": "academic/raw/conferences/existing/source.txt",
                "binary_sha256": meeting.sf.sha256_file(source),
            }]
        finally:
            meeting.REPO = original_repo


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


def test_agent_task_template_matches_shared_parser_and_validator():
    work = _workspace()
    try:
        state = _state(work)
        with patch.object(meeting, "ingest_mode", return_value="agent"), \
                patch.object(meeting, "run_api_meeting_compiler", side_effect=AssertionError("Agent task must not call API")):
            ok, error = meeting.step_write_wiki(state)
        assert not ok and error == "Agent task prepared"
        protocol = state["agent_task"]["protocol"]
        proposal, error, diagnostic = meeting.parse_proposal_detailed(protocol["template"])
        assert proposal is not None, (error, diagnostic)
        assert proposal["meta"] == {
            "doc_date": state["date"],
            "title": state["meeting_id"].split("-", 1)[-1], "doc_type": "meeting",
        }
        meeting_ir = proposal["meeting_ir"]
        assert set(protocol["meeting_ir"]["fields"]) == set(meeting_ir)
        assert meeting_ir["protocol_version"] == PROTOCOL_VERSION
        assert protocol["meeting_ir"]["protocol_version"] == PROTOCOL_VERSION
        assert set(protocol["meeting_ir"]["sections"]) == set(meeting_ir) - {"protocol_version"}
        assert meeting.validate_meeting_ir(meeting_ir) == []
        assert protocol["meeting_ir"]["template"] == meeting_ir
        assert "## Navigation" in proposal["wiki_markdown"]
        assert "## Content" in proposal["wiki_markdown"]
        frontmatter = meeting.yaml.safe_load(proposal["wiki_markdown"].split("---", 2)[1])
        assert frontmatter == protocol["wiki"]["frontmatter"]
        missing_version = dict(meeting_ir)
        missing_version.pop("protocol_version")
        assert meeting.validate_meeting_ir(missing_version)
        wrong_version = {**meeting_ir, "protocol_version": "unsupported"}
        assert meeting.validate_meeting_ir(wrong_version)
        assert meeting.validate_meeting_ir({**meeting_ir, "unknown": []})
    finally:
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
        assert "<<<MEETING_IR>>>" in state["agent_prompt"]
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
    assert "<<<MEETING_IR>>>" in prompt
    assert PROTOCOL_VERSION in prompt
    assert "只读事实源" in prompt
    assert "person_updates" in prompt
    assert "已有学生研究记录目录" in prompt
    assert "省略重复清单和汇总数量" in prompt
    assert "不为双语格式猜测或补造全称" in prompt
    assert "不把待确认身份写成同一人" in prompt


def test_person_updates_are_projected_into_cited_meeting_navigation():
    proposal = _proposal()
    proposal["meeting_ir"]["person_updates"] = [{
        "person": "cnu-test", "person_label": "测试生", "kind": "progress",
        "text": "已完成最小实验", "evidence_ids": ["s0001"],
    }]
    rendered = meeting.compile_meeting_navigation(
        proposal["wiki_markdown"], proposal["meeting_ir"],
        raw_source="academic/raw/conferences/test.txt", date="2026-09-03",
        catalog=[{"evidence_id": "s0001", "line": 1, "text": "测试"}],
    )
    assert "### 学生指导更新" in rendered
    assert "测试生·进展：已完成最小实验" in rendered
    assert "[^meeting-s0001]: academic/raw/conferences/test.txt#L1" in rendered


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
        current["agent_prompt"] = "return <<<PREPROCESS>>> + <<<WIKI>>> + <<<MEETING_IR>>>"
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


def test_compiler_diagnostics_survive_success_and_extract_cleanup():
    from dsh.meeting_compiler_agent import MeetingCompilerResult

    invalid_schema = _output().replace('"assignee_label": "任胜泉", ', '')
    invalid_syntax = _output().replace("<<<WIKI>>>", "<<</PREPROCESS>>>\n<<<WIKI>>>")
    invalid_preprocess = _output().replace('"replacement": "任胜泉"', '"replacement": ""')
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        extract = repo / "temp/extract"
        extract.mkdir(parents=True)
        source = repo / "inbox/meeting.txt"
        source.parent.mkdir()
        source.write_text("original transcript", encoding="utf-8")
        archive = repo / "trash"
        archive.mkdir()
        state = {"transaction_id": "diagnostic-cleanup", "source": "inbox/meeting.txt",
                 "extract_dir": "temp/extract", "cleanup_pending": True, "status": "completed"}
        with patch.object(meeting, "REPO", repo):
            for output in (invalid_schema, invalid_syntax, invalid_preprocess, _output()):
                proposal, error, diagnostic = meeting.parse_proposal_detailed(output)
                result = MeetingCompilerResult(
                    "compiled" if proposal else "rejected", error or "proposal_ready",
                    response_text=output, diagnostic=diagnostic,
                )
                trace = result.trace() | {"context_hash": "context"}
                meeting._record_compiler_output(state, result, trace, "input")
            attempts = state["meeting_compiler_attempts"]
            assert attempts[0]["diagnostic_summary"]["kind"] == "schema"
            assert attempts[0]["diagnostic_summary"]["issues"] == ["MEETING_IR.tasks[0] has invalid fields"]
            assert attempts[0]["diagnostic_summary"]["issue_count"] == 1
            assert attempts[1]["diagnostic_summary"]["kind"] == "json_syntax"
            payload = json.loads((repo / attempts[1]["output_artifact"]).read_text())
            assert payload["response_text"] == invalid_syntax and payload["diagnostic"]["excerpt"]
            for key in ("line", "column", "position"):
                assert attempts[1]["diagnostic_summary"][key] == payload["diagnostic"][key]
            assert attempts[2]["diagnostic_summary"]["issues"] == [
                "PREPROCESS.transcript_replacements[0].replacement must be nonempty"
            ]
            assert "diagnostic_summary" not in attempts[3]
            assert "excerpt" not in json.dumps(attempts)
            assert "response_text" not in json.dumps(attempts)
            with patch.object(meeting.ingest_pipeline, "REPO", repo), \
                    patch.object(meeting.ingest_pipeline.trash_util, "trash_path",
                                 side_effect=lambda path: shutil.move(str(path), str(archive / path.name))), \
                    patch.object(meeting.ingest_pipeline, "_save",
                                 side_effect=lambda current: (repo / "state.json").write_text(json.dumps(current))):
                meeting.ingest_pipeline._finish_cleanup(state, {}, lambda *args, **kwargs: None)
        saved = json.loads((repo / "state.json").read_text())
        assert saved["status"] == "completed" and not saved["cleanup_pending"]
        assert not extract.exists() and not source.exists()
        assert saved["meeting_compiler_attempts"] == attempts
        assert (archive / "extract/compiler-attempt-1.json").is_file()


def test_source_bound_noop_is_consumed_in_both_backends_without_semantic_retry():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    output = _output().replace('"replacement": "任胜泉"', '"replacement": "任老师"')
    for backend in ("agent", "api"):
        work = _workspace()
        try:
            state = _state(work)
            source_path = REPO / state["source"]
            source_bytes = source_path.read_bytes()
            calls = []

            def fake_call(prompt, **kwargs):
                calls.append(prompt)
                return {"ok": True, "text": output}

            def runner(fields):
                assert backend == "api", "Agent must not enter API adapter"
                return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

            with patch.object(meeting, "ingest_mode", return_value=backend), \
                    patch.object(meeting, "run_api_meeting_compiler", runner):
                if backend == "agent":
                    ok, error = meeting.step_write_wiki(state)
                    assert not ok and error == "Agent task prepared"
                    (work / "agent-meeting-compiler.txt").write_text(output, encoding="utf-8")
                ok, error = meeting.step_write_wiki(state)
            assert ok, error
            assert len(calls) == (1 if backend == "api" else 0)
            assert (REPO / state["corrected_path"]).read_text() == "任老师讨论知识库。"
            assert source_path.read_bytes() == source_bytes
            resolution = json.loads((REPO / state["entity_resolution"]).read_text())
            assert resolution["compiler_transcript_replacements"][0]["replacement"] == "任老师"
            assert state["meeting_ir_content"] == _proposal()["meeting_ir"]
            assert meeting.step_validate_wiki(state) == []
        finally:
            shutil.rmtree(work)


def test_record_repair_persists_patch_and_assembled_protocol_and_agent_gets_all_issues():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    ir = _proposal()["meeting_ir"]
    ir["decisions"][0]["evidence_ids"] = ["s0099"]
    ir["relations"] = [{"subject": "cnu-ren-shengquan", "predicate": "指导", "object": "", "evidence_ids": []}]
    ir["person_updates"] = [{"person": "academic/wiki/authors/student", "person_label": "学生甲",
                             "kind": "next_step", "text": "验证知识库", "evidence_ids": ["s0001"]}]
    failed = _output().split("<<<MEETING_IR>>>", 1)[0] + "<<<MEETING_IR>>>\n" + json.dumps(ir, ensure_ascii=False)
    for backend in ("api", "agent"):
        work = _workspace()
        try:
            state = _state(work)
            student_file = work / "student-guidance-candidates.json"
            student_file.write_text(json.dumps({"protocol_version": meeting.sgp.CANDIDATE_VERSION,
                "students": [{"student_key": "student", "label": "学生甲", "people_page": "academic/wiki/authors/student"}]}))
            state["student_guidance_candidates"] = str(student_file.relative_to(REPO))
            calls = []

            def fake_call(prompt, **kwargs):
                calls.append(prompt)
                if len(calls) == 1:
                    return {"ok": True, "text": failed}
                request = json.loads(prompt.rsplit("[修复请求]\n", 1)[1])
                edits = []
                for row in request["records"]:
                    value = dict(row["record"])
                    if row["section"] == "relations": value = None
                    elif row["section"] == "decisions": value["evidence_ids"] = ["s0001"]
                    elif row["section"] == "person_updates": value["person"] = "student"
                    edits.append({key: row[key] for key in ("section", "index", "record_sha256")} | {
                        "replacement": value, "reason": "来源和候选目录支持；未知关系保留原始提议"})
                return {"ok": True, "text": json.dumps({"schema": request["schema"],
                    "base_sha256": request["base_sha256"], "edits": edits}, ensure_ascii=False)}

            def runner(fields):
                assert backend == "api"
                return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

            with patch.object(meeting, "ingest_mode", return_value=backend), patch.object(meeting, "run_api_meeting_compiler", runner):
                if backend == "agent":
                    assert not meeting.step_write_wiki(state)[0]
                    (work / "agent-meeting-compiler.txt").write_text(failed)
                ok, error = meeting.step_write_wiki(state)
                assert not ok
                diagnostic = (json.loads(state["agent_task"]["issues"][-1]) if backend == "agent"
                              else json.loads((work / "compiler-attempt-1.json").read_text())["diagnostic"])
                assert all(any(target in issue for issue in diagnostic["issues"])
                           for target in ("decisions[0]", "relations[0]", "person_updates[0]"))
                if backend == "agent":
                    assert not calls and state["agent_task"]["status"] == "prepared"
                    continue
                state = json.loads(json.dumps(state))
                ok, error = meeting.step_write_wiki(state)
                assert ok, error
            assert len(calls) == 2
            assert meeting.step_validate_wiki(state) == []
            payload = json.loads((work / "compiler-attempt-2.json").read_text())
            assert json.loads(payload["response_text"])["schema"] == "meeting-ir-row-repair-v1"
            assert payload["assembled_output"].split("<<<MEETING_IR>>>", 1)[0] == failed.split("<<<MEETING_IR>>>", 1)[0]
            assert payload["repair_request"]["records"]
            retry = meeting._compiler_retry_context(state, state["meeting_compiler_attempts"][-1]["input_hash"])
            assert retry["previous_output"] == payload["assembled_output"]
            assert (work / "compiler-attempt-1.json").stat().st_mode & 0o777 == 0o600
        finally:
            shutil.rmtree(work)


def test_compiler_diagnostic_summary_is_bounded_and_excludes_source_payloads():
    diagnostic = {"kind": "schema", "message": "m" * 1000,
                  "issues": ["MEETING_IR.tasks[0] " + "x" * 1000] * 100,
                  "response_text": "private response", "excerpt": "private source",
                  "extra": {"source": "private source"}}
    summary = meeting._compiler_diagnostic_summary(diagnostic)
    assert summary["kind"] == "schema" and len(summary["message"]) == 300
    assert summary["issue_count"] == 100 and len(summary["issues"]) == 12
    assert all(len(issue) == 300 for issue in summary["issues"])
    assert summary["truncated"] is True
    assert "private" not in json.dumps(summary)


def test_joint_task_projection_preserves_each_owner_and_derives_record_counts():
    ir = _proposal()["meeting_ir"]
    action = "共同验证知识库方案，保留协作分工"
    ir["tasks"] = [
        {"text": action, "assignee": "cnu-ren-shengquan", "assignee_label": "任胜泉", "evidence_ids": ["s0001"]},
        {"text": action, "assignee": "academic/wiki/authors/yang", "assignee_label": "杨老师", "evidence_ids": ["s0001"]},
        {"text": "补充反馈", "assignee": "", "assignee_label": "小蔡", "evidence_ids": ["s0001"]},
        {"text": "安排下一次讨论", "assignee": "", "assignee_label": "", "evidence_ids": ["s0001"]},
    ]
    assert meeting.validate_meeting_ir(ir, {"s0001"}) == []
    kwargs = {"raw_source": "academic/raw/meeting.txt", "date": "2026-09-03",
              "catalog": [{"evidence_id": "s0001", "line": 1}]}
    wiki = meeting.compile_meeting_navigation(_proposal()["wiki_markdown"], ir, **kwargs)
    assert "已提取记录：参会 1 条 · 汇报 1 条 · 待办 4 条。" in wiki
    assert f"[[cnu-ren-shengquan|任胜泉]]：{action}[^meeting-s0001]" in wiki
    assert f"[[authors/yang|杨老师]]：{action}[^meeting-s0001]" in wiki
    assert "小蔡（身份待确认）：补充反馈[^meeting-s0001]" in wiki
    assert "责任人未明确：安排下一次讨论[^meeting-s0001]" in wiki
    slots = meeting.compile_meeting_slots(ir)
    assert f"{action} | cnu-ren-shengquan" in slots
    assert f"{action} | academic/wiki/authors/yang" in slots
    assert "补充反馈" not in slots and "安排下一次讨论" not in slots
    assert meeting.compile_meeting_navigation(wiki, ir, **kwargs) == wiki
    ir["tasks"].pop()
    refreshed = meeting.compile_meeting_navigation(wiki, ir, **kwargs)
    assert "待办 3 条。" in refreshed and "待办 4 条。" not in refreshed
    assert refreshed.count("已提取记录：") == 1
    assert refreshed.count("[^meeting-s0001]:") == 1
    assert "- 任胜泉讨论知识库。" in refreshed


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


def test_invalid_record_patch_exhausts_existing_revision_budget_without_commit():
    from dsh.meeting_compiler_agent import MeetingCompilerAgent, MeetingCompilerTask

    work = _workspace()
    calls = []
    failed = _output().replace('"evidence_ids": ["s0001"]', '"evidence_ids": ["s0099"]', 1)

    def fake_call(prompt, **kwargs):
        calls.append(prompt)
        return {"ok": True, "text": failed if len(calls) == 1 else "{}"}

    def runner(fields):
        return MeetingCompilerAgent(MeetingCompilerTask(**fields), fake_call).run()

    def forbidden_step(*args, **kwargs):
        raise AssertionError("failed scoped repair must not reach commit")

    try:
        state = _state(work)
        spec = {"script_name": "ingest_meeting.py", "unified_semantic_worker": True,
                "recovery_limits": meeting.RECOVERY_LIMITS,
                "steps": {"write_wiki": meeting.step_write_wiki,
                          "prepare_unified_handoff": meeting.step_prepare_unified_handoff,
                          "validate_wiki": forbidden_step, "finalize": forbidden_step,
                          "update_graph": forbidden_step}}
        with patch.object(meeting, "run_api_meeting_compiler", runner), \
                patch.object(meeting, "ingest_mode", return_value="api"), \
                patch.object(meeting.ingest_pipeline, "_save"):
            result = meeting.ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
        assert len(calls) == 2 and "[修复请求]" in calls[1]
        assert result["status"] == "agent_required"
        assert len(result["meeting_compiler_attempts"]) == 2
        saved = json.loads((work / "compiler-attempt-2.json").read_text())
        assert saved["response_text"] == "{}" and saved["assembled_output"] == failed
        assert not (work / "wiki.md").exists() and not (work / "corrected.txt").exists()
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
                assert "### 知识库\n- 任胜泉讨论知识库。" in state["wiki_content"]
                assert "### 会议导航" in state["wiki_content"]
                assert "验证知识库方案" in state["wiki_content"]
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


def test_unresolved_roles_preserve_wiki_without_global_identity():
    proposal = _proposal()
    meeting_ir = proposal["meeting_ir"]
    for section, endpoint in (("attendees", "person"), ("reports", "person"), ("tasks", "assignee")):
        meeting_ir[section][0][endpoint] = ""
    assert meeting.validate_meeting_ir(meeting_ir, {"s0001"}) == []
    semantic = meeting.compile_meeting_slots(meeting_ir)
    assert "cnu-ren-shengquan" not in semantic
    assert "验证知识库方案" not in semantic
    wiki = meeting.compile_meeting_navigation(
        proposal["wiki_markdown"], meeting_ir, raw_source="academic/raw/test.txt",
        date="2026-09-03", catalog=[{"evidence_id": "s0001", "line": 1, "text": "任胜泉讨论知识库"}],
    )
    assert "任胜泉（身份待确认）" in wiki
    assert "验证知识库方案" in wiki
    assert "[^meeting-s0001]: academic/raw/test.txt#L1" in wiki
    meeting_ir["tasks"][0]["assignee_label"] = ""
    assert meeting.validate_meeting_ir(meeting_ir, {"s0001"}) == []
    meeting_ir["tasks"][0]["assignee_label"] = "任胜泉"
    meeting_ir["tasks"][0]["assignee"] = None
    assert any("must be strings" in error for error in meeting.validate_meeting_ir(meeting_ir, {"s0001"}))


def test_unknown_task_owner_keeps_action_without_inventing_role():
    ir = _proposal()["meeting_ir"]
    ir["tasks"] = [{"text": "会后讨论光学课题，责任人待明确", "assignee": "", "assignee_label": "", "evidence_ids": ["s0001"]}]
    assert meeting.validate_meeting_ir(ir, {"s0001"}) == []
    catalog = [{"evidence_id": "s0001", "line": 1, "text": "会后讨论光学课题，责任人待明确"}]
    wiki = meeting.compile_meeting_navigation(_proposal()["wiki_markdown"], ir,
        raw_source="academic/raw/test.txt", date="2026-09-03", catalog=catalog)
    assert "责任人未明确：会后讨论光学课题" in wiki
    assert "（身份待确认）：会后讨论光学课题" not in wiki
    assert "会后讨论光学课题" not in meeting.compile_meeting_slots(ir)
    assert "cnu-ren-shengquan | 知识库knowledge base" in meeting.compile_meeting_slots(ir)
    for section, label in (("attendees", "label"), ("reports", "person_label")):
        changed = json.loads(json.dumps(ir))
        changed[section][0][label] = ""
        assert any("empty required" in e for e in meeting.validate_meeting_ir(changed, {"s0001"}))
    ir["tasks"][0]["assignee"] = "cnu-ren-shengquan"
    assert any("empty required" in e for e in meeting.validate_meeting_ir(ir, {"s0001"}))
    ir["tasks"][0]["assignee"] = ""
    ir["tasks"][0]["text"] = ""
    assert any("empty required" in e for e in meeting.validate_meeting_ir(ir, {"s0001"}))


def test_source_binding_rebases_only_transaction_footnotes():
    previous = "academic/raw/conferences/2026/0903-old/input.txt"
    final = "academic/raw/conferences/2026/0903-new/input.txt"
    other = previous + ".other"
    wiki = _proposal()["wiki_markdown"] + (
        f"\n[^context]: {previous}#L1\n[^range]: {previous}#L1-L2\n"
        f"[^wrapped]: [[{previous}#L1]]\n"
        f"[^other]: {other}#L1\nPlain text: {previous}#L1\n"
    )
    rendered = meeting.bind_meeting_source(wiki, final, previous_sources=(previous,))
    assert f"[^context]: {final}#L1" in rendered
    assert f"[^range]: {final}#L1-L2" in rendered
    assert f"[^wrapped]: {final}#L1" in rendered
    assert f"[^other]: {other}#L1" in rendered
    assert f"Plain text: {previous}#L1" in rendered


def test_identity_correction_requires_archived_exact_assertion():
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        raw = repo / "cross-domain/raw/facts/user-assertions.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("- [2026-10-10] **网名指真实姓名。** {: #fact-identity-test}\n", encoding="utf-8")
        person = repo / "academic/wiki/authors/person.md"
        person.parent.mkdir(parents=True)
        person.write_text("---\ntitle: 真实姓名\ntype: people\nsources: [cross-domain/raw/facts/user-assertions.md]\n---\n", encoding="utf-8")
        correction = {"mention": "网名", "canonical": "academic/wiki/authors/person", "label": "真实姓名",
                      "source": "cross-domain/raw/facts/user-assertions.md#fact-identity-test", "quote": "网名指真实姓名。"}
        resolution = {"resolved": [], "compiler_entity_resolutions": [
            {"mention": "网名", "canonical": "", "status": "unresolved", "reason": "unknown"},
        ]}
        with patch.object(meeting_repair, "REPO", repo):
            for field, invalid in (("source", "temp/forged.md#fact-identity-test"),
                                   ("quote", "网名指其他姓名。"), ("canonical", "academic/wiki/authors/missing"),
                                   ("source", "cross-domain/raw/facts/user-assertions.md#fact-missing")):
                bad = {**correction, field: invalid}
                try:
                    meeting_repair._apply_identity_corrections(resolution, "网名参与会议", [bad])
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"invalid correction accepted: {field}")
                assert resolution["compiler_entity_resolutions"][0]["status"] == "unresolved"
            evidence = meeting_repair._apply_identity_corrections(resolution, "网名参与会议", [correction])
            assert evidence[0]["source_sha256"]
            assert resolution["compiler_entity_resolutions"][0]["status"] == "resolved"
            assert resolution["resolved"][0]["entity"] == correction["canonical"]


def test_grouped_identity_correction_requires_every_source_spelling_and_full_assertion():
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        raw = repo / "cross-domain/raw/facts/user-assertions.md"
        raw.parent.mkdir(parents=True)
        raw.write_text(
            "- [2026-10-10] **小宁/晓宁指真实姓名。** {: #fact-identity-group}\n"
            "- [2026-10-10] **小宁指真实姓名。** {: #fact-identity-single}\n",
            encoding="utf-8",
        )
        person = repo / "academic/wiki/authors/person.md"
        person.parent.mkdir(parents=True)
        person.write_text("---\ntitle: 真实姓名\ntype: people\nsources: [cross-domain/raw/facts/user-assertions.md]\n---\n", encoding="utf-8")
        correction = {"mention": "小宁/晓宁", "canonical": "academic/wiki/authors/person",
                      "label": "真实姓名", "source": "cross-domain/raw/facts/user-assertions.md#fact-identity-group",
                      "quote": "小宁/晓宁指真实姓名。"}
        original = {"resolved": [], "compiler_entity_resolutions": [
            {"mention": "小宁/晓宁", "canonical": "", "status": "unresolved"},
            {"mention": "小宁", "canonical": "", "status": "unresolved"},
        ]}
        source = "小宁发言。\n晓宁提出建议。"
        single = {**correction, "mention": "小宁",
                  "source": "cross-domain/raw/facts/user-assertions.md#fact-identity-single",
                  "quote": "小宁指真实姓名。"}
        with patch.object(meeting_repair, "REPO", repo):
            invalid_cases = [
                ("小宁发言。", [correction]),
                (source, [{**correction, "quote": "小宁指真实姓名。", "source": single["source"]}]),
                (source, [{**correction, "mention": "小宁/小宁"}]),
                (source, [{**correction, "mention": "小宁/"}]),
                (source, [correction, single]),
                (source, [{**correction, "mention": "小宁/晓宁/不存在"}]),
            ]
            for input_text, corrections in invalid_cases:
                resolution = json.loads(json.dumps(original))
                try:
                    meeting_repair._apply_identity_corrections(resolution, input_text, corrections)
                except ValueError:
                    pass
                else:
                    raise AssertionError("unbound or overlapping identity group accepted")
                assert resolution == original
            resolution = json.loads(json.dumps(original))
            evidence = meeting_repair._apply_identity_corrections(resolution, source, [correction])
            assert evidence[0]["source_mentions"] == ["小宁", "晓宁"]
            assert resolution["compiler_entity_resolutions"][0]["status"] == "resolved"
            assert resolution["compiler_entity_resolutions"][1]["status"] == "unresolved"
            assert {row["original"] for row in resolution["resolved"]} == {"小宁", "晓宁"}
            assert all(row["entity"] == correction["canonical"] for row in resolution["resolved"])


def test_body_source_rebinding_survives_title_change_in_both_backends():
    for backend in ("api", "agent"):
        work = _workspace()
        try:
            state = _state(work)
            state.update(meeting.meeting_date_context(state["date_str"]))
            state.update({"meeting_id": "0903-original", "meeting_id_source": "source_fallback",
                          "raw_dir": "academic/raw/conferences/2026/0903-original"})
            previous = f"{state['raw_dir']}/{state['source_filename']}"
            proposal = _proposal()
            proposal["wiki_markdown"] += f"\n背景证据。[^context]\n\n[^context]: {previous}#L1\n"
            result = SimpleNamespace(status="compiled", reason="proposal_ready", proposal=proposal,
                                     trace=lambda: {"protocol_version": PROTOCOL_VERSION, "status": "compiled"})
            with patch.object(meeting, "ingest_mode", return_value=backend), patch.object(
                meeting, "run_api_meeting_compiler", return_value=result,
            ) as runner:
                if backend == "agent":
                    ok, error = meeting.step_write_wiki(state)
                    assert not ok and error == "Agent task prepared"
                    with patch.dict(globals(), {"_proposal": lambda: proposal}):
                        output = _output()
                    (work / "agent-meeting-compiler.txt").write_text(output, encoding="utf-8")
                ok, error = meeting.step_write_wiki(state)
                assert ok, (backend, error)
                assert runner.call_count == (1 if backend == "api" else 0)
                if backend == "api":
                    ok, error = meeting.step_write_wiki(state)
                    assert ok, error
                    assert runner.call_count == 2
            final = f"{state['raw_dir']}/{state['source_filename']}"
            assert final != previous
            assert f"[^context]: {final}#L1" in state["wiki_content"]
            assert meeting.step_validate_wiki(state) == []
        finally:
            shutil.rmtree(work)


def test_completed_meeting_output_preserves_current_and_historical_quality():
    warning = {"issue": "meeting_entity_unresolved", "mention": "unknown person"}
    state = {"status": "completed", "transaction_id": "quality-test",
             "quality_status": "degraded", "quality_warnings": [warning],
             "quality_warning_history": [warning]}
    output = io.StringIO()
    with patch.object(sys, "argv", ["ingest_meeting.py", "--resume", "quality-test", "--verbose"]), \
            patch.object(meeting.inbox_state, "load", return_value=state), \
            patch.object(meeting, "run_pipeline", return_value=state), \
            patch.object(meeting.ic, "run_resume_post_maintenance", return_value=None), \
            redirect_stdout(output):
        meeting.main()
    result = json.loads(output.getvalue())
    assert result["quality_status"] == "degraded"
    assert result["quality_warnings"] == [warning]
    assert result["quality_warning_history"] == [warning]


def test_meeting_term_acronyms_require_their_own_cited_source():
    proposal = _proposal()
    meeting_ir = proposal["meeting_ir"]
    catalog = [{"evidence_id": "s0001", "text": "使用 tbd 讨论动力学。"},
               {"evidence_id": "s0002", "text": "另一个独立议题 TTD。"}]
    meeting_ir["topics"][0]["label"] = "TBD 动力学"
    assert meeting.validate_meeting_ir(meeting_ir, {"s0001", "s0002"}, evidence_catalog=catalog) == []
    catalog[0]["text"] = "使用 ｔｂｄ 讨论动力学。"
    assert meeting.validate_meeting_ir(meeting_ir, {"s0001", "s0002"}, evidence_catalog=catalog) == []
    meeting_ir["topics"][0]["label"] = "TBD 动力学 (Transfer tensor dynamics, TTD)"
    errors = meeting.validate_meeting_ir(meeting_ir, {"s0001", "s0002"}, evidence_catalog=catalog)
    assert len(errors) == 1 and "unsupported acronym 'TTD'" in errors[0]
    meeting_ir["topics"][0]["evidence_ids"].append("s0002")
    assert meeting.validate_meeting_ir(meeting_ir, {"s0001", "s0002"}, evidence_catalog=catalog) == []


def test_unsupported_acronym_is_rejected_before_meeting_staging():
    work = _workspace()
    try:
        state = _state(work)
        proposal = _proposal()
        proposal["meeting_ir"]["topics"][0]["label"] = "知识库 (Invented project, XYZ)"
        result = SimpleNamespace(status="compiled", reason="proposal_ready", proposal=proposal,
                                 trace=lambda: {"status": "compiled"})
        with patch.object(meeting, "ingest_mode", return_value="api"), \
                patch.object(meeting, "run_api_meeting_compiler", return_value=result) as runner:
            ok, error = meeting.step_write_wiki(state)
        assert not ok and "unsupported acronym 'XYZ'" in error
        assert runner.call_count == 1
        assert not (work / "corrected.txt").exists()
    finally:
        shutil.rmtree(work)


def main():
    test_invalid_record_patch_exhausts_existing_revision_budget_without_commit()
    test_record_repair_persists_patch_and_assembled_protocol_and_agent_gets_all_issues()
    test_source_bound_noop_is_consumed_in_both_backends_without_semantic_retry()
    test_compiler_diagnostics_survive_success_and_extract_cleanup()
    test_compiler_diagnostic_summary_is_bounded_and_excludes_source_payloads()
    test_joint_task_projection_preserves_each_owner_and_derives_record_counts()
    test_unknown_task_owner_keeps_action_without_inventing_role()
    test_completed_meeting_output_preserves_current_and_historical_quality()
    test_meeting_term_acronyms_require_their_own_cited_source()
    test_unsupported_acronym_is_rejected_before_meeting_staging()
    test_body_source_rebinding_survives_title_change_in_both_backends()
    test_unresolved_roles_preserve_wiki_without_global_identity()
    test_source_binding_rebases_only_transaction_footnotes()
    test_identity_correction_requires_archived_exact_assertion()
    test_grouped_identity_correction_requires_every_source_spelling_and_full_assertion()
    test_source_binding_rebases_all_yaml_styles_in_both_backends()
    test_wiki_validation_rejects_noncanonical_or_missing_sources()
    test_api_retry_injects_latest_persisted_response_and_diagnostic()
    test_real_worker_protocol_retry_stops_before_commit()
    test_content_retry_injects_previously_compiled_output()
    test_agent_parse_diagnostic_does_not_enter_api_retry()
    test_preprocess_only_builds_candidates()
    test_api_path_uses_one_compiler_for_all_semantic_outputs()
    test_meeting_ir_rejects_unbound_evidence_before_staging()
    test_meeting_ir_rejects_superseded_and_final_decision_in_one_row()
    test_meeting_projection_patch_requires_one_exact_decision_match()
    test_unresolved_entities_become_quality_warnings()
    test_agent_path_prepares_task_without_entering_api_adapter()
    test_date_context_preserves_explicit_year_and_marks_mmdd_inference()
    test_dedup_same_date_different_meeting_continues()
    test_dedup_split_meeting_is_independent_of_segment_order()
    test_dedup_exact_fingerprint_precedes_graph_and_date_candidates()
    test_mmdd_compiler_output_marks_inferred_date_and_rebases_id()
    test_rejected_compiler_records_attempt_and_protocol_error()
    test_agent_task_roundtrip_consumes_same_protocol()
    test_agent_task_template_matches_shared_parser_and_validator()
    test_exhausted_revision_handoff_uses_full_protocol_without_inline_source()
    test_prompt_requires_one_coherent_protocol()
    test_person_updates_are_projected_into_cited_meeting_navigation()
    test_meeting_compiler_uses_ingest_generation_profile()
    test_semantic_retry_returns_to_same_compiler()
    test_wiki_retry_exhaustion_hands_off_full_compiler_protocol()
    print("ingest meeting tests: PASS")


if __name__ == "__main__":
    main()

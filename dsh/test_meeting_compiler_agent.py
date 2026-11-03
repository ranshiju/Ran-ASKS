#!/usr/bin/env python3
"""Regression tests for the bounded meeting compiler specialist."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
import sys

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from dsh.meeting_compiler_agent import (
    MeetingCompilerAgent,
    MeetingCompilerTask,
    PROTOCOL_VERSION,
    apply_transcript_replacements,
    parse_proposal,
    parse_proposal_detailed,
    task_context_hash,
)
from meeting_compiler_contract import validate_meeting_ir, validate_preprocess


def _output(replacements=None) -> str:
    replacements = replacements or []
    meeting_ir = {
        "protocol_version": PROTOCOL_VERSION,
        "attendees": [{"person": "academic/wiki/authors/cnu-test", "label": "测试人",
                       "evidence_ids": ["s0001"]}],
        "topics": [{"label": "测试议题", "predicate": "讨论", "evidence_ids": ["s0001"]}],
        "reports": [], "decisions": [], "tasks": [], "relations": [],
        "person_updates": [],
    }
    return f'''<<<PREPROCESS>>>
{{"protocol_version":"{PROTOCOL_VERSION}","transcript_replacements":{replacements!r},"entity_resolutions":[]}}
<<<WIKI>>>
---
id: meeting-test
---
# 测试会议
## Navigation
摘要
## Content
正文
<<<MEETING_IR>>>
{json.dumps(meeting_ir, ensure_ascii=False)}
'''.replace("'", '"')


def _task(prompt="compile") -> MeetingCompilerTask:
    return MeetingCompilerTask(
        transaction_id="txn",
        source_path="inbox/meeting.txt",
        meeting_id="meeting-test",
        target_source_path="academic/raw/conferences/meeting.txt",
        context_hash=task_context_hash(
            "任老师发言", {}, meeting_id="meeting-test",
            target_source_path="academic/raw/conferences/meeting.txt",
        ),
        prompt=prompt,
    )


def test_single_call_returns_typed_proposal():
    calls = []

    def fake_call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {"ok": True, "text": _output(), "history": [{"model": "test-model"}]}

    result = MeetingCompilerAgent(_task(), fake_call).run()
    assert result.status == "compiled"
    assert result.proposal["protocol_version"] == PROTOCOL_VERSION
    assert result.proposal["wiki_markdown"].startswith("---")
    assert result.proposal["meeting_ir"]["topics"][0]["label"] == "测试议题"
    assert len(calls) == 1
    assert calls[0][1]["operation"] == "ingest_meeting_compile"
    assert result.trace()["models"] == ["test-model"]


def test_preprocess_diagnostics_identify_fields_without_changing_acceptance():
    base = {"protocol_version": PROTOCOL_VERSION, "transcript_replacements": [], "entity_resolutions": []}
    cases = [
        ({"transcript_replacements": [{"original": "原文", "replacement": "", "reason": "未修改"}]},
         "PREPROCESS.transcript_replacements[0].replacement must be nonempty"),
        ({"transcript_replacements": [{"original": "原串", "replacement": "新串", "reason": None}]},
         "PREPROCESS.transcript_replacements[0].reason must be a string"),
        ({"transcript_replacements": [{"original": "原串", "replacement": "新串", "reason": ""}] * 2},
         "PREPROCESS.transcript_replacements[1].original duplicates an earlier replacement"),
        ({"transcript_replacements": [{"original": "原串", "replacement": "新\n串", "reason": ""}]},
         "PREPROCESS.transcript_replacements[0].replacement must be single-line and at most 160 characters"),
        ({"entity_resolutions": [{"mention": "某人", "canonical": "", "status": "resolved", "reason": "已知"}]},
         "PREPROCESS.entity_resolutions[0].canonical must be nonempty for resolved identity"),
        ({"entity_resolutions": [{"mention": "某人", "canonical": "", "status": "guess", "reason": "推测"}]},
         "PREPROCESS.entity_resolutions[0].status must be resolved, unchanged or unresolved"),
        ({"entity_resolutions": [{}]},
         "PREPROCESS.entity_resolutions[0] fields must be exactly: mention, canonical, status, reason"),
        ({"transcript_replacements": [None] * 65},
         "PREPROCESS.transcript_replacements exceeds 64 items"),
        ({"entity_resolutions": None}, "PREPROCESS.entity_resolutions must be a list"),
    ]
    tail = _output().split("<<<WIKI>>>", 1)[1]
    for changes, issue in cases:
        value = base | changes
        assert not validate_preprocess(value)
        text = "<<<PREPROCESS>>>\n" + json.dumps(value, ensure_ascii=False) + "\n<<<WIKI>>>" + tail
        proposal, error, diagnostic = parse_proposal_detailed(text)
        assert proposal is None and error == "invalid meeting compiler preprocess proposal"
        assert diagnostic["stage"] == "validate_preprocess" and issue in diagnostic["issues"]
        assert "保留原文" not in json.dumps(diagnostic, ensure_ascii=False)
    # Preserve existing permissive fields; diagnostics must not tighten the contract.
    compatible = base | {
        "legacy_extension": True,
        "transcript_replacements": [{"original": "原串", "replacement": "新串", "reason": "", "extra": True}],
        "entity_resolutions": [{"mention": "某人", "canonical": "", "status": "unresolved", "reason": "未匹配"}],
    }
    assert validate_preprocess(base) and validate_preprocess(compatible)


def test_agent_backend_returns_same_task_handoff():
    def fake_call(prompt, **kwargs):
        return {"ok": False, "status": "agent_required", "prompt": "HOST PROMPT"}

    result = MeetingCompilerAgent(_task(), fake_call).run()
    assert result.status == "agent_required"
    assert result.prompt == "HOST PROMPT"
    assert result.model_calls == 0


def test_parser_rejects_missing_or_invalid_sections():
    proposal, error = parse_proposal("<<<WIKI>>>\ntext\n<<<MEETING_IR>>>\n{}\n")
    assert proposal is None
    assert "PREPROCESS" in error
    bad = _output().replace(PROTOCOL_VERSION, "wrong", 1)
    proposal, error = parse_proposal(bad)
    assert proposal is None
    assert "invalid" in error


def test_legacy_v2_meeting_ir_normalizes_to_current_protocol():
    legacy = _output().replace(PROTOCOL_VERSION, "meeting-compiler-v2")
    legacy = legacy.replace(', "person_updates": []', "")
    proposal, error = parse_proposal(legacy)
    assert proposal, error
    assert proposal["protocol_version"] == PROTOCOL_VERSION
    assert proposal["meeting_ir"]["protocol_version"] == PROTOCOL_VERSION
    assert proposal["meeting_ir"]["person_updates"] == []


def test_replacements_are_exact_and_non_cascading():
    replacements = [
        {"original": "任老师", "replacement": "任胜泉", "reason": "人物候选"},
        {"original": "知事库", "replacement": "知识库", "reason": "上下文"},
    ]
    assert apply_transcript_replacements("任老师讨论知事库，任老师确认。", replacements) == (
        "任胜泉讨论知识库，任胜泉确认。"
    )
    try:
        apply_transcript_replacements(
            "甲乙", [
                {"original": "甲乙", "replacement": "丙", "reason": "test"},
                {"original": "甲", "replacement": "丁", "reason": "test"},
            ],
        )
    except ValueError as exc:
        assert "overlapping" in str(exc)
    else:
        raise AssertionError("overlapping replacements must fail")


def test_noop_replacements_require_original_source_and_preserve_real_edits():
    noop = {"original": "甲乙", "replacement": "甲乙", "reason": "保留原文"}
    proposal, error = parse_proposal(_output([noop]))
    assert proposal, error
    assert proposal["preprocess"]["transcript_replacements"] == [noop]
    assert apply_transcript_replacements("甲乙讨论。", [noop]) == "甲乙讨论。"
    mixed = [noop, {"original": "甲", "replacement": "丙", "reason": "原文依据"}]
    assert apply_transcript_replacements("甲乙讨论。", mixed) == "丙乙讨论。"
    # Replacement output never becomes a new match, including for skipped records.
    mixed.append({"original": "丙", "replacement": "丁", "reason": "原文依据"})
    assert apply_transcript_replacements("甲乙和丙。", mixed) == "丙乙和丁。"
    cases = [
        ("甲", [noop]),  # The unchanged phrase still has to occur in Raw.
        ("甲乙", [noop, noop]),
        ("甲乙", [noop | {"reason": None}]),
        ("甲乙", [{"original": "", "replacement": "", "reason": "空串"}]),
        ("甲\n乙", [{"original": "甲\n乙", "replacement": "甲\n乙", "reason": "跨行"}]),
        ("甲" * 81, [{"original": "甲" * 81, "replacement": "甲" * 81, "reason": "超长"}]),
        ("甲", [{"original": "甲", "replacement": "乙", "reason": "变化"},
                 {"original": "乙", "replacement": "乙", "reason": "原文缺失"}]),
    ]
    for source, replacements in cases:
        try:
            apply_transcript_replacements(source, replacements)
        except ValueError:
            pass
        else:
            raise AssertionError("source binding and schema checks must still reject unsafe records")


def test_noop_acceptance_does_not_hide_empty_relation_endpoints():
    output = _output([{"original": "甲", "replacement": "甲", "reason": "保留"}])
    output = output.replace('"relations": []', '"relations": [{"subject": "known-person", "predicate": "指导", "object": "", "evidence_ids": ["s0001"]}]')
    proposal, error, diagnostic = parse_proposal_detailed(output)
    assert proposal is None and "meeting IR" in error
    assert diagnostic["segment"] == "MEETING_IR"
    assert diagnostic["issues"] == ["MEETING_IR.relations[0] has an empty required value"]


def test_meeting_ir_requires_evidence_and_keeps_typed_predicates_out_of_relations():
    proposal, error = parse_proposal(_output())
    assert proposal, error
    meeting_ir = proposal["meeting_ir"]
    meeting_ir["topics"][0]["evidence_ids"] = []
    assert any("evidence_ids" in issue for issue in validate_meeting_ir(meeting_ir))
    meeting_ir["topics"][0]["evidence_ids"] = ["s0001"]
    meeting_ir["relations"] = [{
        "subject": "academic/wiki/authors/cnu-test", "predicate": "参会",
        "object": "本会议", "evidence_ids": ["s0001"],
    }]
    issues = validate_meeting_ir(meeting_ir)
    assert any("typed meeting section" in issue or "deictic" in issue for issue in issues)


def test_json_diagnostics_preserve_exact_error_and_response():
    valid = _output()
    failures = [
        valid.replace('"entity_resolutions":[]}', '"entity_resolutions":[],}'),
        valid.replace('<<<WIKI>>>', '<<</PREPROCESS>>>\n<<<WIKI>>>'),
        valid.replace('"entity_resolutions":[]}', '"entity_resolutions":[],"note":"bad "quote""}'),
    ]
    for failed in failures:
        proposal, error, diagnostic = parse_proposal_detailed(failed)
        assert proposal is None and error == "invalid preprocess JSON"
        assert parse_proposal(failed) == (proposal, error)
        section = failed.split("<<<PREPROCESS>>>", 1)[1].split("<<<WIKI>>>", 1)[0].strip()
        try:
            json.loads(section)
        except json.JSONDecodeError as exc:
            assert diagnostic["message"] == exc.msg
            assert (diagnostic["line"], diagnostic["column"], diagnostic["position"]) == (
                exc.lineno, exc.colno, exc.pos,
            )
            assert section[diagnostic["excerpt_start"]:exc.pos + 100] == diagnostic["excerpt"]
        else:
            raise AssertionError("fixture must fail JSON parsing")
        result = MeetingCompilerAgent(
            _task(), lambda *args, **kwargs: {"ok": True, "text": failed},
        ).run()
        assert result.status == "rejected"
        assert result.response_text == failed
        assert result.diagnostic == diagnostic
        assert "response_text" not in result.trace() and "diagnostic" not in result.trace()
    fenced = valid.replace("<<<PREPROCESS>>>\n", "<<<PREPROCESS>>>\n```json\n").replace(
        "<<<WIKI>>>", "```\n<<<WIKI>>>",
    )
    assert parse_proposal_detailed(fenced)[0] is not None
    schema_failure = valid.replace(PROTOCOL_VERSION, "wrong", 1)
    assert parse_proposal_detailed(schema_failure)[2]["kind"] == "schema"
    assert parse_proposal_detailed("<<<WIKI>>>\ntext")[2]["kind"] == "boundary"


def test_retry_messages_inject_output_and_use_error_specific_scope():
    failed = _output().replace("<<<WIKI>>>", "<<</PREPROCESS>>>\n<<<WIKI>>>")
    diagnostic = parse_proposal_detailed(failed)[2]
    calls = []

    def fake_call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {"ok": True, "text": _output()}

    task = replace(_task("original task"), previous_output=failed,
                   previous_diagnostic=diagnostic,
                   errors=("Meeting Compiler 失败: invalid preprocess JSON",))
    result = MeetingCompilerAgent(task, fake_call).run()
    assert result.status == "compiled" and result.response_text == _output()
    messages = calls[-1][1]["messages"]
    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "original task"
    assert messages[2]["content"] == failed
    assert json.loads(messages[3]["content"].split("[当前校验错误与上轮解析诊断]\n")[1])["diagnostic"] == diagnostic
    assert "本轮只修复" in messages[3]["content"]
    assert "不添加 <<</PREPROCESS>>>" in messages[3]["content"]
    assert calls[-1][1]["retries"] == 0
    assert calls[-1][1]["max_tokens"] == task.budget.max_output_tokens
    for errors, details in [
        (("schema validation failed",), {"kind": "schema"}),
        (("缺少 ## Content 段", "invalid preprocess JSON"), diagnostic),
    ]:
        MeetingCompilerAgent(replace(task, errors=errors, previous_diagnostic=details), fake_call).run()
        repair = calls[-1][1]["messages"][-1]["content"]
        assert "本轮只修复" not in repair
        assert "按当前 schema/内容校验错误" in repair
    MeetingCompilerAgent(replace(task, errors=()), fake_call).run()
    assert calls[-1][1]["messages"] is None


def main():
    test_noop_replacements_require_original_source_and_preserve_real_edits()
    test_noop_acceptance_does_not_hide_empty_relation_endpoints()
    test_preprocess_diagnostics_identify_fields_without_changing_acceptance()
    test_single_call_returns_typed_proposal()
    test_agent_backend_returns_same_task_handoff()
    test_parser_rejects_missing_or_invalid_sections()
    test_legacy_v2_meeting_ir_normalizes_to_current_protocol()
    test_replacements_are_exact_and_non_cascading()
    test_meeting_ir_requires_evidence_and_keeps_typed_predicates_out_of_relations()
    test_json_diagnostics_preserve_exact_error_and_response()
    test_retry_messages_inject_output_and_use_error_specific_scope()
    print("meeting compiler agent tests: PASS")


if __name__ == "__main__":
    main()

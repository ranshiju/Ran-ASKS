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


def _output(replacements=None) -> str:
    replacements = replacements or []
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
<<<SLOTS>>>
参会者:
cnu-test
三元组:
本会议 | 讨论 | 测试议题
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
    assert "三元组:" in result.proposal["semantic_slots"]
    assert len(calls) == 1
    assert calls[0][1]["operation"] == "ingest_meeting_compile"
    assert result.trace()["models"] == ["test-model"]


def test_agent_backend_returns_same_task_handoff():
    def fake_call(prompt, **kwargs):
        return {"ok": False, "status": "agent_required", "prompt": "HOST PROMPT"}

    result = MeetingCompilerAgent(_task(), fake_call).run()
    assert result.status == "agent_required"
    assert result.prompt == "HOST PROMPT"
    assert result.model_calls == 0


def test_parser_rejects_missing_or_invalid_sections():
    proposal, error = parse_proposal("<<<WIKI>>>\ntext\n<<<SLOTS>>>\n三元组:\n")
    assert proposal is None
    assert "PREPROCESS" in error
    bad = _output().replace(PROTOCOL_VERSION, "wrong", 1)
    proposal, error = parse_proposal(bad)
    assert proposal is None
    assert "invalid" in error


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
    test_single_call_returns_typed_proposal()
    test_agent_backend_returns_same_task_handoff()
    test_parser_rejects_missing_or_invalid_sections()
    test_replacements_are_exact_and_non_cascading()
    test_json_diagnostics_preserve_exact_error_and_response()
    test_retry_messages_inject_output_and_use_error_specific_scope()
    print("meeting compiler agent tests: PASS")


if __name__ == "__main__":
    main()

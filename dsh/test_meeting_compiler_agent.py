#!/usr/bin/env python3
"""Regression tests for the bounded meeting compiler specialist."""
from __future__ import annotations

from pathlib import Path
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


def main():
    test_single_call_returns_typed_proposal()
    test_agent_backend_returns_same_task_handoff()
    test_parser_rejects_missing_or_invalid_sections()
    test_replacements_are_exact_and_non_cascading()
    print("meeting compiler agent tests: PASS")


if __name__ == "__main__":
    main()

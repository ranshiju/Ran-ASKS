#!/usr/bin/env python3
"""Regression tests for the Evidence Profile-aware experiment judge."""
import importlib.util
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("answer_judge.py")
SPEC = importlib.util.spec_from_file_location("answer_judge", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_derive_evidence_profile_from_retrieved_pages():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        page = root / "academic/wiki/papers/example.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\n"
            "title: Example\n"
            "sources: [academic/raw/references/example/paper.md]\n"
            "source_type: official-doc\n"
            "status: deprecated\n"
            "related: ['[[papers/other]]']\n"
            "---\n\n## Navigation\nExample.\n",
            encoding="utf-8",
        )
        original_repo = MODULE.REPO
        MODULE.REPO = root
        try:
            profile, disclosures, source = MODULE.resolve_evidence_context({
                "retrieved": [{"page": "academic/wiki/papers/example.md"}],
            })
        finally:
            MODULE.REPO = original_repo

    assert source == "derived-from-retrieved-frontmatter"
    assert profile["source_presence"] == [
        {"page": "academic/wiki/papers/example.md", "count": 1}
    ]
    assert profile["source_types"][0]["type"] == "official-doc"
    assert profile["version_status"][0]["status"] == "deprecated"
    assert any(item["marker"] == "deprecated 无 superseded_by"
               for item in profile["conflict_markers"])
    assert any("版本链未闭合" in item for item in disclosures)


def test_supplied_profile_is_consumed():
    supplied = {
        "source_presence": [{"page": "p.md", "count": 2}],
        "source_types": [{"page": "p.md", "type": "discussion"}],
        "version_status": [{"page": "p.md", "status": "draft", "superseded_by": ""}],
        "conflict_markers": [],
    }
    profile, disclosures, source = MODULE.resolve_evidence_context({
        "retrieved": [],
        "evidence_profile": supplied,
        "required_disclosures": ["仅覆盖草稿"],
    })
    assert source == "supplied"
    assert profile == supplied
    assert disclosures == ["仅覆盖草稿"]


def test_generation_and_judge_prompts_use_structured_profile():
    calls = []
    original_call = MODULE.llm_call

    def fake_call(model, messages, max_tokens=1000):
        calls.append(messages[0]["content"])
        return '{"correctness": 1, "posture_correct": 1, "evidence_traceable": 1, "reason": "ok"}'

    MODULE.llm_call = fake_call
    profile = {
        "source_presence": [{"page": "p.md", "count": 1}],
        "source_types": [{"page": "p.md", "type": "speech-recognition"}],
        "version_status": [{"page": "p.md", "status": "current", "superseded_by": ""}],
        "conflict_markers": [],
    }
    try:
        MODULE.generate_answer("问题", "[来源: p]\n事实", "model", profile, ["专名待核"])
        MODULE.judge_answer(
            "问题", "事实", "限定性", "据 p，事实", "[来源: p]\n事实", "judge",
            profile, ["专名待核"],
        )
    finally:
        MODULE.llm_call = original_call

    assert len(calls) == 2
    for prompt in calls:
        assert '"evidence_profile"' in prompt
        assert "speech-recognition" in prompt
        assert "专名待核" in prompt
        assert "来源权威层级（从高到低）" not in prompt
    assert "各元数据字段本身不构成全局权威排名" in calls[1]


def main():
    test_derive_evidence_profile_from_retrieved_pages()
    test_supplied_profile_is_consumed()
    test_generation_and_judge_prompts_use_structured_profile()
    print("answer judge regression: PASS")


if __name__ == "__main__":
    main()

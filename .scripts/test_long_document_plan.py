#!/usr/bin/env python3
"""长文颗粒度规划器回归。"""
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import long_document_plan


def test_short_document_stays_single_page():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "short.md"
        raw.write_text("# Short\n\nbrief content", encoding="utf-8")
        plan = long_document_plan.make_plan(raw, char_threshold=100)
    assert not plan["candidate_trigger"]
    assert plan["recommended_structure"] == "single-source-page"
    assert plan["keyword_budget"]["overview"] == {"min": 3, "max": 4}


def test_long_structured_document_requires_semantic_review():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "long.md"
        raw.write_text("\n".join(
            f"## Topic {index}\n\n" + ("evidence " * 80)
            for index in range(1, 5)
        ), encoding="utf-8")
        plan = long_document_plan.make_plan(raw, char_threshold=1000)
    assert plan["candidate_trigger"]
    assert plan["semantic_review_required"]
    assert plan["recommended_structure"] == "overview-plus-sections"
    assert len(plan["split_candidates"]) == 4
    assert plan["keyword_budget"]["overview"] == {"min": 3, "max": 7}
    assert "密度只作候选信号" in plan["keyword_budget"]["selection_rule"]


def test_long_unstructured_document_does_not_auto_split():
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "long.txt"
        raw.write_text("x" * 9000, encoding="utf-8")
        plan = long_document_plan.make_plan(raw)
    assert plan["candidate_trigger"]
    assert not plan["semantic_review_required"]
    assert plan["recommended_structure"] == "single-source-page"



def test_short_dense_headings_are_discovered_without_multistage_review():
    text = '\n'.join(f'## 第{i}章\n' + '完整事实与适用条件。' * 45 for i in range(4))
    plan = long_document_plan.plan_text(text)
    assert len(text) < long_document_plan.DEFAULT_CHAR_THRESHOLD
    assert plan['candidate_trigger'] and plan['heading_count'] == 4
    assert not plan['semantic_review_required']
    assert plan['recommended_structure'] == 'single-source-page'
    assert not any(unit['page_candidate'] for unit in plan['units'])
    # The existing configurable size threshold, not another hidden limit, controls review.
    assert long_document_plan.plan_text(text, char_threshold=len(text))['semantic_review_required']
    assert not long_document_plan.plan_text(text, char_threshold=len(text)+1)['semantic_review_required']


if __name__ == "__main__":
    test_short_document_stays_single_page()
    test_short_dense_headings_are_discovered_without_multistage_review()
    test_long_structured_document_requires_semantic_review()
    test_long_unstructured_document_does_not_auto_split()
    print("long document plan regression: PASS")

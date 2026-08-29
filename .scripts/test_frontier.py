#!/usr/bin/env python3
"""Frontier Question Page、库内回答、索引与事实隔离回归。"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("frontier.py")
spec = importlib.util.spec_from_file_location("frontier", SCRIPT)
frontier = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(frontier)

REPO = Path(__file__).resolve().parent.parent
TEMP_REPO_AREA = REPO / "temp" / "test_frontier_sources"


def packet(question="量子纠缠增长是否存在更紧上界？"):
    return {
        "question": question,
        "coverage": "仅当前知识库",
        "candidates": [{"path": "academic/wiki/papers/demo", "title": "Demo", "navigation": "纠缠增长", "score": 2}],
        "raw_evidence": [{"locator": "academic/raw/references/demo/paper.md#L10", "excerpt": "open question"}],
        "anchors": {
            "raw": ["academic/raw/references/demo/paper.md#L10"],
            "wiki": ["academic/wiki/papers/demo"],
            "graph": ["entity/纠缠增长"],
        },
        "duplicate_candidates": [],
    }


def good_assessment(**overrides):
    value = {
        "canonical_question": "量子纠缠增长是否存在更紧上界？",
        "kb_state": "partial",
        "kb_summary": "知识库含部分上界，未显示紧致性证明。",
        "residual_gaps": ["上界是否可饱和"],
        "value_reason": "关系到长程量子动力学的可计算边界。",
        "academic": True,
        "specific": True,
        "recommended_disposition": "new_thread",
        "duplicate_target": "",
    }
    value.update(overrides)
    return value


def good_answer(**overrides):
    value = {
        "kb_state": "partial",
        "answer": "知识库支持一个部分上界，但没有紧致性证明。",
        "supported_claims": [{
            "claim": "已有一个部分上界。",
            "evidence": ["academic/raw/references/demo/paper.md#L10"],
        }],
        "derived_claims": ["现有证据不足以判断该上界是否可饱和。"],
        "residual_gaps": ["上界是否可饱和"],
        "coverage_note": "仅检查当前 WikiGraph 命中的论文。",
    }
    value.update(overrides)
    return value


def test_rebuild_and_fact_links():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, intake)
        report = frontier.rebuild_index(root)
        assert report == {"records": 1, "entries": 0, "edges": 0, "fact_links": 3}
        conn = sqlite3.connect(root / "frontier.db")
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM fact_links").fetchone()[0] == 3
        conn.close()


def test_assessment_promotes_triaged_not_active():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, intake)
        result = frontier.apply_assessment(root, intake, good_assessment())
        assert result["status"] == "triaged"
        assert result["question_id"] == intake["id"]
        question, path = frontier.find_record(root, result["question_id"])
        assert path.parent.name == "questions"
        assert question["status"] == "triaged"
        assert question["human_reviewed"] is False
        assert question["kb_state"] == "partial"
        assert question["residual_gaps"] == ["上界是否可饱和"]
        assert not list((root / "threads").glob("*.md"))


def test_answered_question_stays_single_page():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("已有定理是什么？", "user_proposed", packet("已有定理是什么？"))
        frontier.write_record(root, intake)
        result = frontier.apply_assessment(root, intake, good_assessment(
            kb_state="answered", residual_gaps=[], recommended_disposition="resolved"))
        assert result["status"] == "resolved"
        assert result["thread_id"] == ""
        assert not list((root / "threads").glob("*.md"))
        assert len(list((root / "questions").glob("*.md"))) == 1


def test_high_similarity_blocks_new_thread():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, first)
        promoted = frontier.apply_assessment(root, first, good_assessment())
        assert promoted["question_id"] == first["id"]
        second = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        second["duplicate_candidates"] = frontier.duplicate_candidates(root, second["question"])
        frontier.write_record(root, second)
        result = frontier.apply_assessment(root, second, good_assessment())
        assert not result["thread_id"]
        assert "存在高相似 Question，须先合并审查" in result["gate_errors"]


def test_active_requires_review_and_quality():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, intake)
        result = frontier.apply_assessment(root, intake, good_assessment())
        question, _ = frontier.find_record(root, result["question_id"])
        question["status"] = "active"
        question["human_reviewed"] = True
        question["reviewed_by"] = "user"
        frontier.write_record(root, question)
        reloaded, _ = frontier.find_record(root, question["id"])
        assert reloaded["human_reviewed"] is True
        assert reloaded["value_reason"]


def test_sourced_entry_requires_locator():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        created = frontier.now_iso()
        trajectory = {
            "id": "T-test", "kind": "trajectory", "title": "演进", "question": "", "scope": "测试",
            "status": "captured", "origin_kind": "ai_synthesis", "kb_state": "unassessed",
            "scientific_state": "unverified", "created_at": created, "updated_at": created,
            "anchors": {"raw": [], "wiki": [], "graph": []}, "relations": [],
            "entries": [{"id": "E-0001", "kind": "method_introduced", "content": "提出方法",
                         "origin_kind": "paper_explicit", "epistemic_status": "sourced",
                         "review_status": "candidate", "created_at": created, "evidence": []}],
        }
        try:
            frontier.write_record(root, trajectory)
            assert False, "sourced 无 locator 应失败"
        except ValueError as exc:
            assert "Raw locator" in str(exc)


def test_kb_packet_uses_recall_and_relations():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        def fake_recall(query, domain, topk):
            return json.dumps({"mode": "direct", "candidates": [{"path": "academic/wiki/papers/demo", "title": "Demo", "navigation": "Nav", "score": 3}]}), 10
        raw_dir = TEMP_REPO_AREA / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / "packet.md"
        raw_file.write_text("line 1\nline 2\n", encoding="utf-8")
        raw_rel = raw_file.relative_to(REPO).as_posix()
        def fake_relations(page):
            return json.dumps({"edges": [{"subject": page, "predicate": "涉及", "object": "entity/X", "source": f"{raw_rel}#L2"}]}), 10
        result = frontier.build_kb_packet("问题", root, recall_fn=fake_recall, relations_fn=fake_relations)
        try:
            assert result["anchors"]["wiki"] == ["academic/wiki/papers/demo"]
            assert result["anchors"]["graph"] == ["entity/X"]
            assert result["anchors"]["raw"] == [f"{raw_rel}#L2"]
        finally:
            if TEMP_REPO_AREA.exists():
                shutil.rmtree(TEMP_REPO_AREA)


def test_fact_locator_normalizes_legacy_path_and_bad_anchor():
    if TEMP_REPO_AREA.exists():
        shutil.rmtree(TEMP_REPO_AREA)
    raw_dir = TEMP_REPO_AREA / "raw"
    raw_dir.mkdir(parents=True)
    raw = raw_dir / "legacy.md"
    raw.write_text("# Title\n\nbody\n", encoding="utf-8")
    base = raw.relative_to(REPO).with_suffix("").as_posix()
    try:
        assert frontier._as_locator(base) == raw.relative_to(REPO).as_posix() + "#全篇"
        assert frontier._as_locator(base + "#不存在") == raw.relative_to(REPO).as_posix() + "#全篇"
    finally:
        shutil.rmtree(TEMP_REPO_AREA)


def setup_paper_source():
    if TEMP_REPO_AREA.exists():
        shutil.rmtree(TEMP_REPO_AREA)
    raw_dir = TEMP_REPO_AREA / "raw"
    wiki_dir = TEMP_REPO_AREA / "wiki"
    raw_dir.mkdir(parents=True)
    wiki_dir.mkdir(parents=True)
    raw = raw_dir / "paper.md"
    raw.write_text(
        "# Paper\n\nThe central result is known.\nFuture work should determine whether the bound is tight.\n"
        "This remains an open question for long-range systems.\n## References\nFuture work by Other et al.\n",
        encoding="utf-8",
    )
    wiki = wiki_dir / "demo.md"
    wiki.write_text(
        "---\ntitle: Demo\nsources:\n  - temp/test_frontier_sources/raw/paper.md\n---\n# Demo\n",
        encoding="utf-8",
    )
    return wiki


def test_capture_paper_is_bounded_and_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            first = frontier.extract_paper_candidates(root, str(wiki), limit=3)
            second = frontier.extract_paper_candidates(root, str(wiki), limit=3)
            assert first["count"] == 2
            assert second["count"] == 0
            records = frontier.load_records(root)
            assert len(records) == 2
            assert all(item[0]["origin_kind"] == "paper_explicit" for item in records.values())
            assert all(item[0]["kind"] == "question" for item in records.values())
        finally:
            if TEMP_REPO_AREA.exists():
                shutil.rmtree(TEMP_REPO_AREA)


def test_future_work_paragraph_splits_into_question_pages():
    units = frontier.explicit_question_units(
        "We acknowledge limitations and opportunities for future work. "
        "First, evaluate the method on longer contexts. "
        "Second, determine whether the score is calibrated."
    )
    assert units == [
        "evaluate the method on longer contexts.",
        "determine whether the score is calibrated.",
    ]


def test_frontier_write_does_not_touch_fact_graph():
    graph = REPO / "cross-domain" / "graph.db"
    before = (graph.stat().st_size, graph.stat().st_mtime_ns) if graph.exists() else None
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("一个用户学术问题", "user_proposed", packet("一个用户学术问题"))
        frontier.write_record(root, intake)
        frontier.rebuild_index(root)
    after = (graph.stat().st_size, graph.stat().st_mtime_ns) if graph.exists() else None
    assert before == after


def test_mark_stale_from_fact_anchor():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        intake = frontier.new_intake("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, intake)
        result = frontier.apply_assessment(root, intake, good_assessment())
        changed = frontier.mark_stale_for_targets(root, {"entity/纠缠增长"})
        assert changed == [result["question_id"]]
        question, _ = frontier.find_record(root, result["question_id"])
        assert question["possibly_stale"] is True


def test_answer_question_writes_evidence_bound_answer_once():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("量子纠缠增长是否存在更紧上界？", "user_proposed", packet())
        frontier.write_record(root, question)
        fake = lambda _packet: {"ok": True, "parsed": good_answer()}
        first = frontier.answer_question(root, question["id"], packet=packet(), answer_fn=fake)
        second = frontier.answer_question(root, question["id"], packet=packet(), answer_fn=fake)
        assert first["status"] == "completed" and first["changed"] is True
        assert second["changed"] is False
        record, path = frontier.find_record(root, question["id"])
        assert path.parent.name == "questions"
        assert record["kb_state"] == "partial"
        assert len(record["entries"]) == 2
        assert record["entries"][0]["epistemic_status"] == "sourced"
        assert record["entries"][1]["epistemic_status"] == "derived"


def test_answer_rejects_locator_outside_packet():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet("问题"))
        frontier.write_record(root, question)
        bad = good_answer(supported_claims=[{"claim": "越界", "evidence": ["raw/other.md#L1"]}])
        try:
            frontier.apply_answer(root, question, packet("问题"), bad)
            assert False, "越界 locator 应失败"
        except ValueError as exc:
            assert "证据包" in str(exc)


def test_no_evidence_answer_is_deterministic_and_not_scientific_claim():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        empty = {"question": "问题", "coverage": "仅当前库", "candidates": [],
                 "raw_evidence": [], "anchors": {"raw": [], "wiki": [], "graph": []},
                 "duplicate_candidates": []}
        question = frontier.new_question("问题", "user_proposed", empty)
        frontier.write_record(root, question)
        result = frontier.answer_question(root, question["id"], packet=empty)
        record, _ = frontier.find_record(root, question["id"])
        assert result["kb_state"] == "no_evidence"
        assert "当前知识库" in record["kb_summary"]
        assert record["scientific_state"] == "unverified"


def test_exact_question_reuses_page_and_adds_source_mention():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            first = frontier.extract_paper_candidates(root, str(wiki), limit=1)
            assert first["count"] == 1
            second_wiki = wiki.with_name("demo-2.md")
            second_wiki.write_text(wiki.read_text(encoding="utf-8"), encoding="utf-8")
            second = frontier.extract_paper_candidates(root, str(second_wiki), limit=1)
            assert second["count"] == 0
            assert second["reused"] == first["captured"]
            assert len(frontier.load_records(root)) == 1
            record, _ = frontier.find_record(root, first["captured"][0])
            assert len(record["source_mentions"]) == 2
        finally:
            if TEMP_REPO_AREA.exists():
                shutil.rmtree(TEMP_REPO_AREA)


def test_migrate_legacy_intake_to_question_page():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("迁移问题", "user_proposed", packet("迁移问题"))
        question["id"] = "I-legacy"
        question["kind"] = "intake"
        frontier.write_record(root, question)
        result = frontier.migrate_question_pages(root)
        assert result["count"] == 1
        migrated, path = frontier.find_record(root, "I-legacy")
        assert migrated["kind"] == "question"
        assert path.parent.name == "questions"
        assert not (root / "intake" / "I-legacy.md").exists()


def test_split_legacy_paragraph_into_single_question_pages():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paragraph = ("Future Work. One direction is to evaluate longer contexts. "
                     "Another direction is to calibrate the score.")
        question = frontier.new_question(paragraph, "paper_explicit", packet(paragraph),
                                         "academic/wiki/papers/demo",
                                         "academic/raw/references/demo/paper.md#L10")
        question["kb_summary"] = "旧整段回答"
        question["answer_status"] = "completed"
        frontier.write_record(root, question)
        result = frontier.split_question_pages(root)
        assert result["count"] == 2
        records = [item[0] for item in frontier.load_records(root).values()]
        assert len(records) == 2
        assert {item["question"] for item in records} == {
            "evaluate longer contexts.", "calibrate the score.",
        }
        assert all(item["answer_status"] == "pending" and not item["kb_summary"] for item in records)


def main():
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in sorted(tests):
        globals()[name]()
    print(f"frontier regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

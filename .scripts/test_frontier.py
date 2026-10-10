#!/usr/bin/env python3
"""Frontier Question Page、库内回答、索引与事实隔离回归。"""
from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def test_agent_backend_prepares_semantic_tasks_without_model_adapter():
    original_backend = frontier.agent_task.query_backend
    try:
        frontier.agent_task.query_backend = lambda: "agent"
        assessment = frontier.run_assessment(
            packet(), transaction_id="frontier-assessment-test",
            commit_command="python3 .scripts/frontier.py assess test --assessment-file result.json",
        )
        answer = frontier.run_answer(packet(), transaction_id="frontier-answer-test")
    finally:
        frontier.agent_task.query_backend = original_backend
    for result, kind in ((assessment, "frontier_assessment"), (answer, "frontier_answer")):
        assert result["status"] == "prepared"
        assert result["agent_task"]["schema"] == "agent-task-v1"
        assert result["agent_task"]["kind"] == kind
        assert result["agent_task"]["outputs"][0]["path"].startswith("temp/frontier-agent/")


def test_api_backend_uses_model_adapter_and_shared_schema():
    original_backend = frontier.agent_task.query_backend
    original_module = sys.modules.get("llm_structured")
    calls = []
    try:
        frontier.agent_task.query_backend = lambda: "api"
        sys.modules["llm_structured"] = SimpleNamespace(call_json=lambda prompt, schema, **kwargs: (
            calls.append((prompt, kwargs)) or {"ok": True, "parsed": good_assessment()}
        ))
        result = frontier.run_assessment(packet())
        assert result["ok"] and calls
    finally:
        frontier.agent_task.query_backend = original_backend
        if original_module is None:
            sys.modules.pop("llm_structured", None)
        else:
            sys.modules["llm_structured"] = original_module


def test_answer_adapters_receive_previous_answer_without_extra_model_call():
    original_backend = frontier.agent_task.query_backend
    original_module = sys.modules.get("llm_structured")
    p = packet()
    p["previous_answer"] = {"answer": "旧的有限条件回答"}
    calls = []
    try:
        frontier.agent_task.query_backend = lambda: "agent"
        task = frontier.run_answer(p, transaction_id="frontier-comparison-test")["agent_task"]
        assert "change" in task["protocol"]["optional_fields"]
        payload = json.loads((REPO / task["inputs"][0]["path"]).read_text())
        assert payload["previous_answer"] == p["previous_answer"]
        frontier.agent_task.query_backend = lambda: "api"
        def fake_model(prompt, schema, **kwargs):
            calls.append(prompt)
            value = good_answer(change={"kind": "uncertain", "reason": "条件尚不可比较"})
            assert schema(value)
            return {"ok": True, "parsed": value}
        sys.modules["llm_structured"] = SimpleNamespace(call_json=fake_model)
        result = frontier.run_answer(p)
        assert len(calls) == 1 and "旧的有限条件回答" in calls[0]
        assert result["parsed"]["change"]["kind"] == "uncertain"
        assert not frontier.answer_schema(good_answer(change={"kind": ["qualified"], "reason": "bad type"}))
    finally:
        frontier.agent_task.query_backend = original_backend
        if original_module is None:
            sys.modules.pop("llm_structured", None)
        else:
            sys.modules["llm_structured"] = original_module


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


def test_question_source_survives_recall_miss_and_evidence_budget():
    with tempfile.TemporaryDirectory(dir=REPO / "temp") as directory:
        root = Path(directory)
        raw_dir = root / "raw"
        raw_dir.mkdir()
        source = raw_dir / "source.md"
        source.write_text("# Original source\nDirect source observation.\nOpen question.\n")
        source_loc = source.relative_to(REPO).as_posix() + "#L2-L3"
        decoy = raw_dir / "decoy.md"
        decoy.write_text("\n".join(f"Other observation {i}." for i in range(10)))
        decoy_base = decoy.relative_to(REPO).as_posix()
        recall = lambda *_: (json.dumps({"candidates": [{"path": "academic/wiki/papers/missing-test-candidate"}]}), 0)
        relations = lambda *_: (json.dumps({"edges": [
            {"source": f"{decoy_base}#L{i}"} for i in range(1, 10)
        ]}), 0)
        result = frontier.build_kb_packet(
            "Different wording", root, recall_fn=recall, relations_fn=relations,
            source_locators=[source_loc, source_loc],
        )
        assert result["raw_evidence"][0] == {
            "locator": source_loc, "excerpt": "Direct source observation.\nOpen question.",
        }
        assert len(result["raw_evidence"]) == 5
        assert result["anchors"]["raw"] == [item["locator"] for item in result["raw_evidence"]]
        assert len(set(result["anchors"]["raw"])) == 5
        assert source_loc in frontier.answer_prompt(result)


def test_answer_default_uses_question_sources_but_explicit_packet_stays_bounded():
    with tempfile.TemporaryDirectory(dir=REPO / "temp") as directory:
        root = Path(directory)
        source = "academic/raw/references/source/paper.md#L20"
        mention = "academic/raw/references/other/paper.md#L10-L12"
        question = frontier.new_question("Problem", "paper_explicit", packet(), source_locator=source)
        question["source_mentions"].extend([{"locator": source}, {"locator": mention}])
        frontier.write_record(root, question)
        seen = []
        respond = lambda value: (seen.append(value) or {"ok": True, "parsed": good_answer()})
        with patch.object(frontier, "build_kb_packet", return_value=packet()) as builder:
            frontier.answer_question(root, question["id"], answer_fn=respond)
            assert builder.call_args.kwargs["source_locators"] == [source, mention]
            assert seen[-1]["question_sources"] == [source, mention]
            builder.reset_mock()
            explicit = packet()
            frontier.answer_question(root, question["id"], packet=explicit, answer_fn=respond)
            builder.assert_not_called()
            assert seen[-1]["raw_evidence"] == explicit["raw_evidence"]
            assert seen[-1]["anchors"] == explicit["anchors"]
            assert "previous_answer" not in explicit


def test_raw_excerpt_honors_ranges_sections_and_binding_failure():
    with tempfile.TemporaryDirectory(dir=REPO / "temp") as directory:
        root = Path(directory)
        raw_dir = root / "raw"
        raw_dir.mkdir()
        source = raw_dir / "source.md"
        source.write_text("# Title\nTitle preface.\n## Results\nFirst result.\nSecond result.\n## Outlook\nOpen question.\n")
        base = source.relative_to(REPO).as_posix()
        for span in ("L4-L5", "L4-5", "Results"):
            assert frontier._raw_excerpt(f"{base}#{span}") == "First result.\nSecond result."
        for span in ("L999", "L999-L1000", "Missing section"):
            assert frontier._raw_excerpt(f"{base}#{span}") == ""
        with patch.object(frontier.sl, "companion_binding_for_target", return_value={"status": "invalid"}):
            assert frontier._raw_excerpt(f"{base}#L4-L5") == ""
            assert frontier._raw_excerpt(f"{base}#全篇") == ""


def test_unreadable_question_sources_are_disclosed_not_allowed_as_evidence():
    with tempfile.TemporaryDirectory(dir=REPO / "temp") as directory:
        root = Path(directory)
        raw_dir = root / "raw"
        raw_dir.mkdir()
        source = raw_dir / "source.md"
        source.write_text("# Title\nExisting passage.\n")
        base = source.relative_to(REPO).as_posix()
        empty_recall = lambda *_: ('{"candidates": []}', 0)
        result = frontier.build_kb_packet("Question", root, recall_fn=empty_recall,
                                          source_locators=[f"{base}#L999"])
        assert result["raw_evidence"] == result["anchors"]["raw"] == []
        assert result["evidence_issues"][0]["locator"] == f"{base}#L999"
        with patch.object(frontier.sl, "companion_binding_for_target", return_value={"status": "invalid"}):
            result = frontier.build_kb_packet("Question", root, recall_fn=empty_recall,
                                              source_locators=[f"{base}#L2"])
        assert result["raw_evidence"] == result["anchors"]["raw"] == []
        assert result["evidence_issues"][0]["locator"] == f"{base}#L2"
        assert "evidence_issues" in frontier.answer_prompt(result)


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


def test_interesting_future_study_is_an_explicit_question_unit():
    sentence = (
        "It is an interesting future study to investigate how the universal corner "
        "entanglement entropy is implemented in the lattice entanglement Hamiltonian."
    )
    paragraph = (
        "The method also permits higher-dimensional cuts. "
        "Sharp edges add a universal logarithmic contribution. " + sentence
    )
    assert frontier.explicit_question_units(paragraph) == [sentence]


def test_explicit_conjectures_and_future_intents_are_question_units():
    statements = [
        "Conversely, we conjecture that perfect transmission implies a duality between the theories.",
        "We further conjecture that the correspondence extends to higher dimensions.",
        "The conjecture remains unproven for interacting quantum systems.",
        "We aim to explore chiral fermions on the lattice in the near future.",
        "Looking ahead, we plan to investigate the thermodynamic limit.",
        "我们猜想这一对偶关系可以推广到更高维度的体系。",
    ]
    for statement in statements:
        assert frontier.explicit_question_units(statement) == [statement], statement
    paragraph = "The construction proves the forward direction. " + statements[0]
    assert frontier.explicit_question_units(paragraph) == [statements[0]]


def test_conjecture_cues_do_not_capture_resolved_historical_or_current_work():
    statements = [
        "Earlier authors conjectured that the transition is continuous.",
        "We conjectured that the bound was tight before finding a counterexample.",
        "We do not conjecture that every transparent interface is topological.",
        "We conjecture a spectral correspondence and prove it in this paper.",
        "We conjecture this relation, but it has been disproved by a counterexample.",
        "The conjecture has been proved for all finite lattices.",
        "In this paper, we aim to study transmission through an interface.",
    ]
    for statement in statements:
        assert frontier.explicit_question_units(statement) == [], statement
    unresolved = "We conjecture that the stronger bound holds for interacting systems."
    assert frontier.explicit_question_units(statements[3] + " " + unresolved) == [unresolved]


def test_capture_conjectures_preserves_raw_locators_and_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            raw = TEMP_REPO_AREA / "raw/paper.md"
            raw.write_text(
                "# Paper\n\nWe conjecture that perfect transmission implies a duality.\n"
                "We aim to explore chiral fermions in the near future.\n"
                "## References\nWe conjecture that this citation is relevant.\n",
                encoding="utf-8",
            )
            first = frontier.extract_paper_candidates(root, str(wiki))
            assert first["count"] == 2
            records = frontier.load_records(root)
            assert {record["source_locator"] for record, _, _ in records.values()} == {
                "temp/test_frontier_sources/raw/paper.md#L3",
                "temp/test_frontier_sources/raw/paper.md#L4",
            }
            assert all(record["origin_kind"] == "paper_explicit" for record, _, _ in records.values())
            assert frontier.extract_paper_candidates(root, str(wiki))["count"] == 0
            assert len(frontier.load_records(root)) == 2
        finally:
            shutil.rmtree(TEMP_REPO_AREA)


def test_future_expectations_require_forward_looking_context():
    statements = [
        "Looking ahead, we expect the method to enable precise low-temperature computations.",
        "We anticipate significant speedups through further engineering and GPU acceleration.",
        "We expect faster convergence through further optimization of the implementation.",
    ]
    for statement in statements:
        assert frontier.explicit_question_units(statement) == [statement], statement
    expected = "We expect the method to enable precise low-temperature computations."
    paragraph = "Looking ahead, important applications remain challenging. " + expected
    assert frontier.explicit_question_units(paragraph) == [expected]
    planned = "We plan to investigate the thermodynamic limit."
    assert frontier.explicit_question_units("Looking ahead, several questions remain. " + planned) == [planned]
    for statement in [
        "We expect the measured energy to equal zero.",
        "We anticipate agreement with the reference calculation.",
        "We expected further improvements before completing the experiments.",
        "Earlier authors anticipated future speedups for the method.",
        "Looking ahead, we do not expect further improvements from this approximation.",
        "In this paper, we expect speedups through further optimization of the implementation.",
        "In this paper, we aim to explore the thermodynamic limit for future applications.",
        "Looking ahead, in this paper we expect to improve convergence.",
    ]:
        assert frontier.explicit_question_units(statement) == [], statement


def test_paper_prose_skips_bibliography_and_resumes_after_it():
    before = "We conjecture that the bound is sharp."
    after = "We conjecture that the extension is possible."
    citation = "[1] K. Binder and A. P. Young, Spin glasses and open questions, Rev. Mod. Phys. (1986)."
    for heading, resume_heading in [
        ("", "## End Matter"),
        ("## References\n", "## Appendix A"),
        ("References\n", "End Matter"),
        ("## Bibliography\n", "Appendix A: Further results"),
        ("参考文献\n", "附录 A"),
    ]:
        text = "# Paper\n" + before + "\n" + heading + citation + "\n"
        text += "Future work in the cited paper remains challenging.\n"
        text += "[2] A. Young, Future research directions (2006).\n"
        text += resume_heading + "\n" + after + "\n"
        prose = list(frontier.paper_prose_lines(text))
        assert before in [line for _, line in prose]
        assert after in [line for _, line in prose]
        assert citation not in [line for _, line in prose]
        assert not any("cited paper" in line or "A. Young" in line for _, line in prose)
        assert next(number for number, line in prose if line == after) == len(text.splitlines())


def test_numbered_open_questions_are_not_bibliography():
    text = (
        "## Future work\n"
        "[1] We conjecture that the stronger bound holds.\n"
        "[2] We plan to investigate the thermodynamic limit in future work.\n"
    )
    units = [unit for _, line in frontier.paper_prose_lines(text)
             for unit in frontier.explicit_question_units(line)]
    assert units == text.splitlines()[1:]


def test_capture_future_expectations_ignores_references_and_keeps_locators():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            raw = TEMP_REPO_AREA / "raw/paper.md"
            raw.write_text(
                "# Paper\n"
                "Looking ahead, we expect the method to enable precise low-temperature computations.\n"
                "We anticipate significant speedups through further engineering and GPU acceleration.\n"
                "[1] K. Binder and A. P. Young, Spin glasses and open questions (1986).\n"
                "Future work in the cited paper remains challenging.\n"
                "## End Matter\n"
                "We conjecture that the extension is possible.\n",
                encoding="utf-8",
            )
            with patch.object(frontier.qa, "graph_relations", return_value=("{}", [])):
                first = frontier.extract_paper_candidates(root, str(wiki))
                second = frontier.extract_paper_candidates(root, str(wiki))
            assert first["count"] == 3
            assert second["count"] == 0
            records = frontier.load_records(root)
            assert len(records) == 3
            assert {record["source_locator"] for record, _, _ in records.values()} == {
                "temp/test_frontier_sources/raw/paper.md#L2",
                "temp/test_frontier_sources/raw/paper.md#L3",
                "temp/test_frontier_sources/raw/paper.md#L7",
            }
            assert all(record["scientific_state"] == "unverified" for record, _, _ in records.values())
        finally:
            shutil.rmtree(TEMP_REPO_AREA)


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


def test_related_questions_are_bounded_read_only_candidates():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for number in range(4):
            q = frontier.new_question(f"quantum tensor entanglement bound {number}", "user_proposed", packet())
            frontier.apply_answer(root, q, packet(), good_answer())
        records = frontier.load_records(root)
        before = {p: p.read_bytes() for _, _, p in records.values()}
        result = frontier.related_question_candidates(records, "academic/wiki/papers/new",
                                                       "quantum tensor entanglement bound", limit=2)
        assert result["total_matches"] == 4 and result["returned"] == 2 and result["truncated"]
        assert all(item["status"] == "unreviewed" for item in result["candidates"])
        assert all(p.read_bytes() == data for p, data in before.items())
        assert result == frontier.related_question_candidates(records, "academic/wiki/papers/new",
                                                              "quantum tensor entanglement bound", limit=2)


def test_related_question_uses_current_answer_but_not_historical_entries():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        q = frontier.new_question("需要怎样改进？", "user_proposed", packet())
        frontier.apply_answer(root, q, packet(), good_answer(answer="费米子波函数的纠缠结构与网络表示有关。"))
        result = frontier.related_question_candidates(frontier.load_records(root), "new",
                                                       "费米子谱张量网络与纠缠结构")
        assert result["candidates"][0]["id"] == q["id"]
        q["kb_summary"] = "训练时间未测量。"
        q["residual_gaps"] = []
        frontier.write_record(root, q)
        assert frontier.related_question_candidates(frontier.load_records(root), "new",
                                                     "费米子谱张量网络与纠缠结构")["returned"] == 0


def test_related_question_exclusions_and_empty_or_unrelated_text():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        ids = []
        for status in ("captured", "parked", "rejected", "resolved"):
            q = frontier.new_question("quantum tensor entanglement", "user_proposed", packet())
            q["status"] = status
            frontier.write_record(root, q)
            ids.append(q["id"])
        records = frontier.load_records(root)
        result = frontier.related_question_candidates(records, "new", "quantum tensor entanglement",
                                                       exclude={ids[0]}, limit=999)
        assert [item["id"] for item in result["candidates"]] == [ids[3]]
        assert result["limit"] == 5
        assert frontier.related_question_candidates(records, "new", "")["returned"] == 0
        assert frontier.related_question_candidates(records, "new", "banana bicycle orchard")["returned"] == 0


def test_capture_related_question_never_marks_stale_or_auto_answers_it():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        original_answer = frontier.answer_question
        calls = []
        try:
            wiki.write_text(wiki.read_text() + "\n## Navigation\n\nquantum tensor entanglement bounds\n")
            q = frontier.new_question("quantum tensor entanglement bounds?", "user_proposed", packet())
            frontier.apply_answer(root, q, packet(), good_answer())
            path = frontier.record_path(root, "question", q["id"])
            before = path.read_bytes()
            frontier.answer_question = lambda _root, record_id, _topk: (
                calls.append(record_id) or {"id": record_id, "status": "pending"})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                frontier.cmd_capture_paper(SimpleNamespace(root=str(root), page=str(wiki), limit=3,
                                                          topk=6, no_answer=False))
            result = json.loads(output.getvalue())
            assert q["id"] in [item["id"] for item in result["related_question_candidates"]["candidates"]]
            assert q["id"] not in calls and q["id"] not in result["stale_records"]
            assert calls and path.read_bytes() == before
        finally:
            frontier.answer_question = original_answer
            shutil.rmtree(TEMP_REPO_AREA)


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


def test_answer_history_preserves_evidence_gaps_and_semantic_review():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        original = packet()
        question = frontier.new_question(original["question"], "user_proposed", original)
        frontier.write_record(root, question)
        frontier.answer_question(root, question["id"], packet=original,
                                 answer_fn=lambda _: {"ok": True, "parsed": good_answer()})
        before, _ = frontier.find_record(root, question["id"])
        v1 = json.loads(json.dumps(before["answer_history"][0]))
        added = "academic/raw/references/new-result/paper.md#L20"
        expanded = packet()
        expanded["anchors"]["raw"].append(added)
        expanded["raw_evidence"].append({"locator": added, "excerpt": "Only for short-range models."})
        updated = good_answer(
            answer="新证据限定了可饱和条件，其他模型仍不确定。",
            supported_claims=[{"claim": "短程模型可饱和。", "evidence": [added]}],
            residual_gaps=["长程模型是否可饱和"],
            change={"kind": "qualified", "reason": "新结果只覆盖短程模型，不能推广。"},
        )
        observed = []
        frontier.answer_question(root, question["id"], packet=expanded,
                                 answer_fn=lambda p: (observed.append(p) or {"ok": True, "parsed": updated}))
        record, path = frontier.find_record(root, question["id"])
        assert record["answer_history"][0] == v1
        assert len(record["answer_history"]) == 2
        assert observed[0]["previous_answer"] == v1
        assert record["answer_history"][1]["change"]["kind"] == "qualified"
        assert record["answer_history"][1]["evidence_scope"]["cited_raw_locators"] == [added]
        assert record["scientific_state"] == "unverified"
        assert record["anchors"]["raw"] == [added]
        assert "回答版本" in path.read_text()
        repeated = frontier.answer_question(root, question["id"], packet=expanded,
                                            answer_fn=lambda _: {"ok": True, "parsed": updated})
        assert not repeated["revision_added"]


def test_same_answer_with_changed_evidence_scope_gets_new_version():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        frontier.apply_answer(root, question, packet(), good_answer())
        expanded = packet()
        expanded["raw_evidence"][0]["excerpt"] = "More precise conditions in revised source."
        result = frontier.apply_answer(root, question, expanded, good_answer())
        assert result["changed"] is False and result["revision_added"] is True
        assert question["answer_history"][-1]["change"]["kind"] == "not_assessed"
        assert len(question["entries"]) == 2


def test_refresh_failure_and_invalid_answer_preserve_last_answer():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        frontier.apply_answer(root, question, packet(), good_answer())
        frontier.mark_stale_for_targets(root, {"academic/raw/references/demo/paper.md"})
        before, path = frontier.find_record(root, question["id"])
        frontier.answer_question(root, question["id"], packet=packet(),
                                 answer_fn=lambda _: {"ok": False, "status": "unavailable"})
        pending, _ = frontier.find_record(root, question["id"])
        for key in ("kb_summary", "coverage_note", "answer_history", "answer_checked_at", "stale_reasons"):
            assert pending[key] == before[key]
        assert pending["possibly_stale"] and pending["answer_status"] == "pending"
        file_before = path.read_bytes()
        try:
            frontier.apply_answer(root, pending, packet(), good_answer(
                supported_claims=[{"claim": "不允许", "evidence": ["academic/raw/other.md#L1"]}]))
            assert False
        except ValueError:
            pass
        assert path.read_bytes() == file_before


def test_legacy_answer_is_retained_without_inventing_claim_versions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        question.update(answer_status="completed", answer_fingerprint="legacy",
                        kb_summary="旧答案", residual_gaps=["旧缺口"])
        frontier.apply_answer(root, question, packet(), good_answer())
        old = question["answer_history"][0]
        assert old["answer"] == "旧答案" and old["residual_gaps"] == ["旧缺口"]
        assert old["evidence_scope"]["legacy_unversioned"]
        assert old["supported_claims"] == []
        assert len(question["answer_history"]) == 2


def test_stale_raw_file_matches_locators_but_not_adjacent_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        frontier.apply_answer(root, question, packet(), good_answer())
        assert frontier.mark_stale_for_targets(root, {"academic/raw/references/demo/paper.md.bak"}) == []
        assert frontier.mark_stale_for_targets(root, {"academic/raw/references/demo/paper.md"}) == [question["id"]]
        stale, _ = frontier.find_record(root, question["id"])
        assert stale["stale_reasons"] == packet()["anchors"]["raw"]
        frontier.apply_answer(root, stale, packet(), good_answer(
            change={"kind": "unchanged", "reason": "核验后，原条件与结论仍成立。"}))
        assert not stale["possibly_stale"] and stale["stale_reasons"] == []
        assert stale["answer_history"][-1]["refresh_reasons"] == packet()["anchors"]["raw"]


def test_capture_paper_marks_raw_citations_without_graph_anchor():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            p = packet()
            raw = "temp/test_frontier_sources/raw/paper.md#L3"
            p["anchors"]["raw"] = [raw]
            question = frontier.new_question("原结论是否可靠？", "user_proposed", p)
            frontier.apply_answer(root, question, p, good_answer(
                supported_claims=[{"claim": "已有结果", "evidence": [raw]}]))
            result = frontier.extract_paper_candidates(root, str(wiki))
            assert question["id"] in result["stale_records"]
        finally:
            shutil.rmtree(TEMP_REPO_AREA)


def test_assessment_does_not_overwrite_completed_answer():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        frontier.apply_answer(root, question, packet(), good_answer())
        frontier.apply_assessment(root, question, good_assessment(kb_summary="未经回答校验的另一种解释"))
        assert question["kb_summary"] == good_answer()["answer"]
        assert question["kb_summary"] == question["answer_history"][-1]["answer"]


def test_outdated_prepared_comparison_cannot_overwrite_newer_answer():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        frontier.apply_answer(root, question, packet(), good_answer())
        prepared = packet()
        prepared["previous_answer"] = frontier.current_answer_snapshot(question)
        frontier.apply_answer(root, question, packet(), good_answer(answer="更新后的回答"))
        path = frontier.record_path(root, "question", question["id"])
        before = path.read_bytes()
        try:
            frontier.apply_answer(root, question, prepared, good_answer(answer="旧任务的回答"))
            assert False
        except ValueError as exc:
            assert "旧回答已变化" in str(exc)
        assert path.read_bytes() == before


def test_completed_task_replay_is_noop_and_keeps_later_stale_notice():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        question = frontier.new_question("问题", "user_proposed", packet())
        prepared = packet()
        prepared["previous_answer"] = None
        frontier.apply_answer(root, question, prepared, good_answer())
        frontier.mark_stale_for_targets(root, {"academic/raw/references/demo/paper.md"})
        current, path = frontier.find_record(root, question["id"])
        before = path.read_bytes()
        result = frontier.apply_answer(root, current, prepared, good_answer())
        assert result["replayed"] and result["possibly_stale"]
        assert not result["revision_added"] and path.read_bytes() == before


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


def test_dependent_future_work_preserves_antecedent_context():
    paragraph = (
        "An alternative safety approach is human review of risky actions. "
        "Human response can become the throughput bottleneck. "
        "Hence, this approach was not implemented and could be explored as future work."
    )
    assert frontier.explicit_question_units(paragraph) == [paragraph]
    independent = "Future work should evaluate robustness across instruments."
    assert frontier.explicit_question_units(independent) == [independent]


def test_answer_prompt_preserves_priority_source_beyond_old_limits():
    with tempfile.TemporaryDirectory(dir=REPO / "temp") as directory:
        root = Path(directory)
        source = root / "raw" / "source.md"
        source.parent.mkdir()
        paragraph = "General instrument operation. " * 40 + (
            "The alternative approach is human review of risky actions; "
            "it was not implemented because human response limits throughput."
        )
        source.write_text(paragraph, encoding="utf-8")
        locator = source.relative_to(REPO).as_posix() + "#L1"
        result = frontier.build_kb_packet(
            "Which alternative approach?", root,
            recall_fn=lambda *_: ('{"candidates":[]}', 0),
            source_locators=[locator],
        )
        assert len(paragraph) > 1000
        assert result["raw_evidence"][0]["excerpt"] == paragraph
        prompt = frontier.answer_prompt(result)
        assert paragraph in prompt
        assert "human response limits throughput" in prompt


def test_frontier_api_calls_keep_transaction_identity():
    calls = []
    adapter = SimpleNamespace(call_json=lambda prompt, schema, **kwargs: (
        calls.append(kwargs) or {"ok": True, "parsed": good_answer()}
    ))
    with patch.object(frontier.agent_task, "query_backend", return_value="api"), \
            patch.dict(sys.modules, {"llm_structured": adapter}):
        frontier.run_assessment(packet(), transaction_id="ingest-parent")
        frontier.run_answer(packet(), transaction_id="ingest-parent")
    assert [call["transaction_id"] for call in calls] == ["ingest-parent", "ingest-parent"]
    assert [call["operation"] for call in calls] == ["frontier_assess", "frontier_answer"]


def test_capture_paper_passes_parent_transaction_to_answer():
    with tempfile.TemporaryDirectory() as directory:
        args = SimpleNamespace(root=directory, page="demo", limit=3, topk=6,
                               no_answer=False, transaction_id="ingest-parent")
        with patch.object(frontier, "extract_paper_candidates", return_value={
            "captured": ["Q-test"], "reused": [],
        }), patch.object(frontier, "answer_question", return_value={
            "status": "completed",
        }) as answer, patch.object(frontier, "rebuild_index"), \
                contextlib.redirect_stdout(io.StringIO()):
            frontier.cmd_capture_paper(args)
        assert answer.call_args.kwargs["transaction_id"] == "ingest-parent"


def test_capture_known_source_mention_does_not_duplicate_normalized_question():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = setup_paper_source()
        try:
            first = frontier.extract_paper_candidates(root, str(wiki))
            record, _ = frontier.find_record(root, first["captured"][0])
            record["question"] = "Normalized research question with resolved references."
            frontier.write_record(root, record)
            second = frontier.extract_paper_candidates(root, str(wiki))
            assert second["captured"] == []
            assert len(frontier.load_records(root)) == len(first["captured"])
        finally:
            shutil.rmtree(TEMP_REPO_AREA)


def main():
    tests = [name for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for name in sorted(tests):
        globals()[name]()
    print(f"frontier regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

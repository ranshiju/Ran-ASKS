#!/usr/bin/env python3
"""Regression tests for the cross-document Knowledge IR contract."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import graph_ingest
import ingest_common
import knowledge_ir as kir


def _build(page: str, wiki_type: str, relations: list[dict], deterministic: int = 0):
    return kir.build_knowledge_ir(
        page,
        {"type": wiki_type, "sources": ["academic/raw/example/source.md"]},
        relations,
        deterministic_relation_count=deterministic,
        transaction_id="txn-test",
    )


def test_profiles_share_one_envelope():
    cases = [
        ("academic/wiki/papers/example", "paper-summary", "paper"),
        ("academic/wiki/conferences/0903-example", "conference-summary", "meeting"),
        ("admin/wiki/policies/example", "policy", "document"),
    ]
    for page, wiki_type, expected in cases:
        ir = _build(page, wiki_type, [{
            "subject": page, "predicate": "涉及", "object": "统一图编译",
        }])
        assert ir["schema"] == kir.KNOWLEDGE_IR_SCHEMA
        assert ir["document"]["profile"] == expected
        assert not kir.validate_knowledge_ir(ir)


def test_relation_roundtrip_preserves_writer_fields():
    relation = {
        "subject": "author/a",
        "predicate": "第一作者",
        "object": "academic/wiki/papers/example",
        "source": "academic/wiki/papers/example#content",
        "subject_metadata_kind": "person",
        "object_is_canonical": True,
        "confidence": "[可追溯]",
        "is_sr": 0,
    }
    ir = _build("academic/wiki/papers/example", "paper-summary", [relation], 1)
    assert kir.relations_from_ir(ir) == [relation]
    assert ir["relations"][0]["origin"] == "deterministic"


def test_meeting_extension_keeps_people_tasks_and_reports():
    page = "academic/wiki/conferences/0903-example"
    ir = _build(page, "conference-summary", [
        {"subject": "person/a", "predicate": "参会", "object": page},
        {"subject": "person/a", "predicate": "汇报", "object": "主题"},
        {"subject": "person/b", "predicate": "待办", "object": "验证结果"},
        {"subject": page, "predicate": "决策", "object": "采用统一 IR"},
    ])
    extension = ir["extensions"]["meeting"]
    assert extension["attendees"] == ["person/a"]
    assert extension["reports"] == [{"person": "person/a", "topic": "主题"}]
    assert extension["tasks"] == [{"assignee": "person/b", "task": "验证结果"}]
    assert extension["decisions"] == ["采用统一 IR"]


def test_empty_relation_endpoint_is_rejected():
    try:
        _build("admin/wiki/policies/example", "policy", [
            {"subject": "", "predicate": "涉及", "object": "主题"},
        ])
    except ValueError as exc:
        assert "subject is required" in str(exc)
    else:
        raise AssertionError("empty endpoint must be rejected")


def test_graph_plan_binds_exact_ir_hash():
    ir = _build("teaching/wiki/courses/example", "course", [
        {"subject": "teaching/wiki/courses/example", "predicate": "涵盖", "object": "量子力学"},
    ])
    plan = kir.build_graph_plan(ir, {"hard_errors": []})
    assert not kir.validate_graph_plan(plan, ir)
    changed = _build("teaching/wiki/courses/example", "course", [
        {"subject": "teaching/wiki/courses/example", "predicate": "涵盖", "object": "电动力学"},
    ])
    assert "knowledge_ir_sha256 mismatch" in kir.validate_graph_plan(plan, changed)


def test_structural_raw_relationship_is_bound_to_plan():
    ir = kir.build_knowledge_ir(
        "academic/wiki/papers/example-v2",
        {"type": "paper-summary", "sources": ["academic/raw/references/example-v2/paper.md"]},
        [],
        structural_relations=[{
            "subject": "academic/raw/references/example/paper",
            "predicate": "后一版本",
            "object": "academic/raw/references/example-v2/paper",
            "confidence": "[可追溯]",
            "source": "",
            "is_sr": 0,
        }],
    )
    plan = kir.build_graph_plan(ir)
    assert plan["structural_relation_count"] == 1
    assert ir["structural_relations"][0]["kind"] == "raw_relationship"


def test_all_structural_relations_are_deterministic():
    structural = [
        {
            "subject": "academic/raw/references/example/paper",
            "predicate": predicate,
            "object": f"academic/raw/references/example-{index}/paper",
        }
        for index, predicate in enumerate(("后一版本", "补充材料"), 1)
    ]
    ir = kir.build_knowledge_ir(
        "academic/wiki/papers/example-v2",
        {"type": "paper-summary", "sources": []},
        [],
        structural_relations=structural,
    )
    assert [row["origin"] for row in ir["structural_relations"]] == [
        "deterministic", "deterministic",
    ]


def test_semantic_proposal_is_bound_and_drops_writer_fields():
    page = "admin/wiki/policies/example"
    fm = {"type": "policy", "sources": ["admin/raw/policies/example.md"]}
    proposal = kir.build_knowledge_ir(page, fm, [{
        "subject": page,
        "predicate": "涉及",
        "object": "统一图编译",
        "source": "forged.md#L1",
        "subject_is_canonical": True,
        "object_metadata_kind": "person",
        "is_sr": 1,
    }])
    relations, glosses = kir.semantic_proposal_content(proposal, page, fm)
    assert relations == [{
        "subject": page,
        "predicate": "涉及",
        "object": "统一图编译",
    }]
    assert glosses == []
    binding_errors = kir.validate_document_binding(
        proposal, "admin/wiki/policies/other", fm
    )
    assert binding_errors == ["document.page does not match --page"]


def test_direct_ir_recompiles_and_commits_structural_relation_idempotently():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        page = "admin/wiki/policies/new"
        target_page = "admin/wiki/policies/old"
        page_file = repo / f"{page}.md"
        target_file = repo / f"{target_page}.md"
        page_file.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        page_file.write_text(
            "---\ntitle: New policy\ntype: policy\nsources:\n"
            "  - admin/raw/policies/new.md\nsource_type: official-doc\n"
            "status: current\n---\n\n## Navigation\n\nUnified graph.\n\n"
            "## Content\n\nUnified graph compilation.\n",
            encoding="utf-8",
        )
        target_file.write_text(
            "---\ntitle: Old policy\ntype: policy\nsources:\n"
            "  - admin/raw/policies/old.md\nsource_type: official-doc\n"
            "status: current\n---\n\n## Navigation\n\nOld policy.\n",
            encoding="utf-8",
        )
        fm = {
            "type": "policy",
            "sources": ["admin/raw/policies/new.md"],
        }
        proposal = kir.build_knowledge_ir(page, fm, [{
            "subject": page,
            "predicate": "涉及",
            "object": "统一图编译",
            "source": "forged.md#L1",
            "subject_is_canonical": True,
        }])
        input_path = repo / "proposal.json"
        output_path = repo / "canonical.json"
        plan_path = repo / "plan.json"
        kir.write_json(input_path, proposal)
        args = Namespace(
            page=page,
            knowledge_ir=str(input_path),
            semantic=None,
            triples=None,
            triples_json=None,
            citations=None,
            transaction_id="txn-direct",
            knowledge_ir_out=str(output_path),
            graph_plan_out=str(plan_path),
            raw_relationship_json=json.dumps({
                "type": "supplementary", "target_page": target_page,
            }),
            clean=False,
            db=None,
        )
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO = repo
        graph_ingest.gl.GRAPH_DB = repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            with redirect_stdout(io.StringIO()):
                graph_ingest.cmd_ingest(args)
                graph_ingest.cmd_ingest(args)
            canonical = kir.load_knowledge_ir(output_path)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            assert canonical["document"]["transaction_id"] == "txn-direct"
            assert canonical["relations"][0].get("source") == ""
            assert "subject_is_canonical" not in canonical["relations"][0]
            assert not kir.validate_graph_plan(plan, canonical)
            assert plan["structural_relation_count"] == 1
            conn = sqlite3.connect(graph_ingest.gl.GRAPH_DB)
            assert conn.execute(
                "SELECT COUNT(*) FROM edges WHERE subject=? AND predicate=? AND object=?",
                (
                    "admin/raw/policies/old",
                    "补充材料",
                    "admin/raw/policies/new",
                ),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM edges WHERE subject=? AND predicate=? AND object=?",
                (page, "涉及", "统一图编译"),
            ).fetchone()[0] == 1
            before_edges = set(conn.execute(
                "SELECT subject,predicate,object FROM edges"
            ))
            before_nodes = set(conn.execute("SELECT path,title,type FROM nodes"))
            conn.close()

            changed = kir.build_knowledge_ir(page, fm, [{
                "subject": page,
                "predicate": "涉及",
                "object": "必须回滚的新主题",
            }])
            kir.write_json(input_path, changed)
            original_apply = graph_ingest.apply_ir_structural_relations
            graph_ingest.apply_ir_structural_relations = (
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("injected structural failure")
                )
            )
            args.clean = True
            try:
                try:
                    with redirect_stdout(io.StringIO()):
                        graph_ingest.cmd_ingest(args)
                except RuntimeError as exc:
                    assert "injected structural failure" in str(exc)
                else:
                    raise AssertionError("injected failure must abort ingest")
            finally:
                graph_ingest.apply_ir_structural_relations = original_apply
            conn = sqlite3.connect(graph_ingest.gl.GRAPH_DB)
            assert set(conn.execute(
                "SELECT subject,predicate,object FROM edges"
            )) == before_edges
            assert set(conn.execute("SELECT path,title,type FROM nodes")) == before_nodes
            conn.close()
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_invalid_direct_ir_fails_before_database_open():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        page = "admin/wiki/policies/example"
        page_file = repo / f"{page}.md"
        page_file.parent.mkdir(parents=True)
        page_file.write_text(
            "---\ntitle: Example\ntype: policy\nsources: []\n---\n",
            encoding="utf-8",
        )
        invalid = kir.build_knowledge_ir(page, {"type": "policy"}, [{
            "subject": page, "predicate": "涉及", "object": "主题",
        }])
        invalid["relations"][0]["object"] = ""
        input_path = repo / "invalid.json"
        kir.write_json(input_path, invalid)
        old_repo = graph_ingest.gl.REPO
        old_connect = graph_ingest._connect_for
        graph_ingest.gl.REPO = repo
        graph_ingest._connect_for = lambda _args: (_ for _ in ()).throw(
            AssertionError("database must not be opened")
        )
        try:
            try:
                graph_ingest.cmd_ingest(Namespace(
                    page=page,
                    knowledge_ir=str(input_path),
                    semantic=None,
                    triples=None,
                    triples_json=None,
                    citations=None,
                    raw_relationship_json=None,
                ))
            except ValueError as exc:
                assert "object is required" in str(exc)
            else:
                raise AssertionError("invalid IR must be rejected")
        finally:
            graph_ingest.gl.REPO = old_repo
            graph_ingest._connect_for = old_connect


def test_shared_graph_command_records_ir_and_plan_paths():
    captured = []
    original_run = ingest_common.run_tracked
    ingest_common.run_tracked = lambda command, *_args, **_kwargs: (
        captured.append(command) or json.dumps({"edges_added": 1})
    )
    state = {
        "transaction_id": "txn-command",
        "wiki_path": "admin/wiki/policies/example",
        "semantic_path": "temp/example-semantic.txt",
    }
    try:
        ok, message = ingest_common.step_update_graph(
            state, Path("/tmp/knowledge-ir-command-test")
        )
    finally:
        ingest_common.run_tracked = original_run
    assert ok and not message
    command = captured[0]
    assert command[command.index("--knowledge-ir-out") + 1] == (
        "temp/inbox-state/txn-command-knowledge-ir.json"
    )
    assert command[command.index("--graph-plan-out") + 1] == (
        "temp/inbox-state/txn-command-graph-plan.json"
    )


def test_atomic_artifacts_can_be_reloaded():
    ir = _build("business/wiki/reports/example", "report", [
        {"subject": "business/wiki/reports/example", "predicate": "涉及", "object": "预算"},
    ])
    plan = kir.build_graph_plan(ir)
    with tempfile.TemporaryDirectory() as tmp:
        ir_path = Path(tmp) / "knowledge-ir.json"
        plan_path = Path(tmp) / "graph-plan.json"
        kir.write_json(ir_path, ir)
        kir.write_json(plan_path, plan)
        assert kir.load_knowledge_ir(ir_path) == ir
        assert plan_path.is_file()


def main():
    tests = [
        (name, value) for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for name, test in tests:
        test()
    print(f"knowledge_ir regression: {len(tests)}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

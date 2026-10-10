#!/usr/bin/env python3
"""graph_ingest.py 轻量时态事实写入回归测试。"""
import datetime
import importlib.util
import json
import sqlite3
import sys
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_validate as gv

SCRIPT = Path(__file__).with_name("graph_ingest.py")
spec = importlib.util.spec_from_file_location("graph_ingest_temporal", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    gl.init_schema(conn)
    return conn


def add_node(conn, path, node_type="page"):
    conn.execute(
        "INSERT INTO nodes (path, type) VALUES (?,?)", (path, node_type)
    )


def rows(conn, page):
    return conn.execute(
        "SELECT * FROM temporal_facts WHERE subject=? AND predicate=? AND object=?",
        (page, module.TEMPORAL_PAGE_PREDICATE, page),
    ).fetchall()


def test_policy_write_and_replace():
    conn = make_db()
    page = "admin/wiki/policies/test-policy"
    add_node(conn, page)
    first = module.sync_page_temporal_fact(conn, page, {
        "type": "policy", "effective_from": "2026-01-01",
        "sources": ["admin/raw/policies/test.md"],
    })
    assert first["added"] == 1
    assert first["removed"] == 0
    assert not first["warnings"]
    assert len(rows(conn, page)) == 1
    second = module.sync_page_temporal_fact(conn, page, {
        "type": "policy", "effective_from": "2026-02-01",
        "effective_to": "2026-12-31",
        "sources": ["admin/raw/policies/test.md"],
    })
    assert second["removed"] == 1
    assert second["added"] == 1
    fact = rows(conn, page)[0]
    assert fact["valid_from"] == "2026-02-01"
    assert fact["valid_until"] == "2026-12-31"


def test_missing_window_clears_existing_fact():
    conn = make_db()
    page = "admin/wiki/procedures/test-procedure"
    add_node(conn, page)
    module.sync_page_temporal_fact(conn, page, {
        "type": "procedure", "effective_from": "2026-01-01", "sources": ["admin/raw/procedures/test.md"],
    })
    result = module.sync_page_temporal_fact(conn, page, {
        "type": "procedure", "effective_from": "", "effective_to": "", "sources": [],
    })
    assert result["removed"] == 1
    assert result["added"] == 0
    assert not rows(conn, page)


def test_non_temporal_page_is_ignored():
    conn = make_db()
    page = "teaching/wiki/topics/test-topic"
    add_node(conn, page)
    result = module.sync_page_temporal_fact(conn, page, {
        "type": "topic", "effective_from": "2026-01-01", "sources": ["teaching/raw/topics/test.md"],
    })
    assert result == {"removed": 0, "added": 0, "warnings": []}
    assert not rows(conn, page)


def test_invalid_date_skips_with_warning():
    conn = make_db()
    page = "admin/wiki/decisions/test-decision"
    add_node(conn, page)
    result = module.sync_page_temporal_fact(conn, page, {
        "type": "decision", "effective_from": "2026月1日", "sources": ["admin/raw/decisions/test.md"],
    })
    assert result["removed"] == 0
    assert result["added"] == 0
    assert result["warnings"]


def test_inverted_window_skips_with_warning():
    conn = make_db()
    page = "admin/wiki/policies/test-policy"
    add_node(conn, page)
    result = module.sync_page_temporal_fact(conn, page, {
        "type": "policy", "effective_from": "2026-02-01",
        "effective_to": "2026-01-01", "sources": ["admin/raw/policies/test.md"],
    })
    assert result["removed"] == 0
    assert result["added"] == 0
    assert result["warnings"]
    assert not rows(conn, page)


def test_yaml_date_object_is_normalized():
    conn = make_db()
    page = "teaching/wiki/courses/test-course"
    add_node(conn, page)
    module.sync_page_temporal_fact(conn, page, {
        "type": "course", "effective_from": datetime.date(2026, 9, 1),
        "sources": ["teaching/raw/courses/test.md"],
    })
    fact = rows(conn, page)[0]
    assert fact["valid_from"] == "2026-09-01"


def test_clean_page_edges_removes_temporal_facts():
    conn = make_db()
    page = "admin/wiki/policies/test-policy"
    add_node(conn, page)
    module.sync_page_temporal_fact(conn, page, {
        "type": "policy", "effective_from": "2026-01-01", "sources": ["admin/raw/policies/test.md"],
    })
    result = module.clean_page_edges(conn, page)
    assert result["temporal_facts_removed"] == 1
    assert not rows(conn, page)


def test_clean_page_edges_preserves_incoming_edge_owned_by_other_page():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        pages = ["academic/wiki/papers/a", "academic/wiki/papers/b"]
        for name, raw in zip(pages, ["a", "b"]):
            page_file = repo / f"{name}.md"
            page_file.parent.mkdir(parents=True, exist_ok=True)
            page_file.write_text(
                f"---\ntitle: {raw}\ntype: paper-summary\nsources: [academic/raw/{raw}.md]\n---\n",
                encoding="utf-8",
            )
        old_repo = gl.REPO
        gl.REPO = repo
        try:
            conn = make_db()
            for page in pages:
                add_node(conn, page, "page")
            edge_id = conn.execute(
                "INSERT INTO edges(subject,predicate,object,confidence,source) VALUES(?,?,?,?,?)",
                (pages[0], "相关于", pages[1], "可追溯", "academic/raw/a.md#L3"),
            ).lastrowid
            gl.add_edge_evidence(conn, edge_id, "academic/raw/a.md#L3")
            gl.add_edge_origin(conn, edge_id, pages[0], "academic/raw/a.md#L3")
            conn.commit()

            result = module.clean_page_edges(conn, pages[1])
            assert result["edges_removed"] == 0
            assert conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone()
            assert conn.execute(
                "SELECT 1 FROM edge_origins WHERE edge_id=? AND origin_page=?",
                (edge_id, pages[0]),
            ).fetchone()
        finally:
            gl.REPO = old_repo


def test_clean_page_edges_uses_lineage_for_shared_indirect_edge():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        pages = ["academic/wiki/papers/a", "academic/wiki/papers/b"]
        for name, raw in zip(pages, ["a", "b"]):
            page_file = repo / f"{name}.md"
            page_file.parent.mkdir(parents=True, exist_ok=True)
            page_file.write_text(
                f"---\ntitle: {raw}\ntype: paper-summary\nsources: [academic/raw/{raw}.md]\n---\n",
                encoding="utf-8",
            )
        old_repo = gl.REPO
        gl.REPO = repo
        try:
            conn = make_db()
            for node in [*pages, "concept-a", "concept-b"]:
                add_node(conn, node, "page" if "/wiki/" in node else "entity")
            edge_id = conn.execute(
                "INSERT INTO edges(subject,predicate,object,confidence,source) VALUES(?,?,?,?,?)",
                ("concept-a", "改进", "concept-b", "可追溯", "academic/raw/a.md#L3"),
            ).lastrowid
            for page, source in zip(pages, ["academic/raw/a.md#L3", "academic/raw/b.md#L4"]):
                gl.add_edge_evidence(conn, edge_id, source)
                gl.add_edge_origin(conn, edge_id, page, source)
            conn.commit()

            first = module.clean_page_edges(conn, pages[0])
            assert first["lineage_edges_removed"] == 0
            assert conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone()
            assert conn.execute("SELECT source FROM edges WHERE id=?", (edge_id,)).fetchone()[0] == "academic/raw/b.md#L4"
            assert conn.execute("SELECT COUNT(*) FROM edge_origins WHERE edge_id=?", (edge_id,)).fetchone()[0] == 1

            second = module.clean_page_edges(conn, pages[1])
            assert second["lineage_edges_removed"] == 1
            assert not conn.execute("SELECT 1 FROM edges WHERE id=?", (edge_id,)).fetchone()
        finally:
            gl.REPO = old_repo


def test_ensure_node_upsert_preserves_node_lineage():
    conn = make_db()
    add_node(conn, "academic/wiki/papers/a", "page")
    gl.ensure_node(conn, "concept-a", "Concept A", "entity", entity_subtype="keyword")
    gl.add_node_origin(
        conn, "concept-a", "academic/wiki/papers/a", "academic/raw/a.md#L3", managed=True
    )
    conn.commit()

    gl.ensure_node(
        conn, "concept-a", "Concept A updated", "entity", entity_subtype="keyword"
    )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM managed_nodes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM node_origins").fetchone()[0] == 1
    assert conn.execute("SELECT title FROM nodes WHERE path='concept-a'").fetchone()[0] == "Concept A updated"


def test_add_knowledge_edges_records_managed_and_reused_node_origins():
    conn = make_db()
    page = "academic/wiki/papers/a"
    add_node(conn, page, "page")
    gl.ensure_node(conn, "existing", "既有概念", "entity", entity_subtype="keyword")
    triples = [
        {"subject": page, "predicate": "研究关键词", "object": "新概念",
         "source": "academic/raw/a.md#L3"},
        {"subject": page, "predicate": "研究关键词", "object": "既有概念",
         "source": "academic/raw/a.md#L4"},
    ]

    module.add_knowledge_edges(conn, page, triples)
    new_path = gl.extract_keyword_id("新概念")
    assert conn.execute(
        "SELECT created_origin_page FROM managed_nodes WHERE node_path=?", (new_path,)
    ).fetchone()[0] == page
    assert not conn.execute(
        "SELECT 1 FROM managed_nodes WHERE node_path='existing'"
    ).fetchone()
    assert conn.execute(
        "SELECT COUNT(*) FROM node_origins WHERE origin_page=?", (page,)
    ).fetchone()[0] == 2


def test_clean_page_edges_only_collects_unowned_managed_nodes():
    conn = make_db()
    page_a = "academic/wiki/papers/a"
    page_b = "academic/wiki/papers/b"
    for page in (page_a, page_b):
        add_node(conn, page, "page")
    gl.ensure_node(conn, "shared", "Shared", "entity", entity_subtype="keyword")
    gl.add_node_origin(conn, "shared", page_a, "raw/a#L1", managed=True)
    gl.add_node_origin(conn, "shared", page_b, "raw/b#L1")
    gl.insert_aliases(conn, "shared", ["shared-alias"])
    gl.ensure_node(conn, "historical", "Historical", "entity", entity_subtype="keyword")
    gl.add_node_origin(conn, "historical", page_a, "raw/a#L2")
    conn.commit()

    first = module.clean_page_edges(conn, page_a)
    assert first["managed_nodes_removed"] == 0
    assert conn.execute("SELECT 1 FROM nodes WHERE path='shared'").fetchone()
    assert conn.execute("SELECT 1 FROM nodes WHERE path='historical'").fetchone()
    assert not gv.validate_graph(conn, gv.load_config())["errors"]
    assert conn.execute("SELECT created_origin_page FROM managed_nodes WHERE node_path='shared'").fetchone()[0] == page_a

    second = module.clean_page_edges(conn, page_b)
    assert second["managed_nodes_removed"] == 1
    assert not conn.execute("SELECT 1 FROM nodes WHERE path='shared'").fetchone()
    assert not conn.execute("SELECT 1 FROM aliases WHERE node_path='shared'").fetchone()
    assert conn.execute("SELECT 1 FROM nodes WHERE path='historical'").fetchone()


def test_clean_page_edges_removes_membership_only_managed_orphan():
    conn = make_db()
    page = "academic/wiki/papers/a"
    add_node(conn, page, "page")
    gl.ensure_node(conn, "old-concept", "Old Concept", "entity", entity_subtype="keyword")
    gl.add_node_origin(conn, "old-concept", page, "raw/a#L1", managed=True)
    gl.ensure_node(conn, "academic/wiki/hubs/example", "Example", "hub")
    conn.execute(
        "INSERT INTO edges(subject,predicate,object,confidence,source) VALUES(?,?,?,?,?)",
        ("old-concept", "聚类于", "academic/wiki/hubs/example", "推断", ""),
    )
    conn.commit()

    result = module.clean_page_edges(conn, page)
    assert result["managed_nodes_removed"] == 1
    assert result["derived_memberships_removed"] == 1
    assert not conn.execute("SELECT 1 FROM nodes WHERE path='old-concept'").fetchone()
    assert not conn.execute(
        "SELECT 1 FROM edges WHERE subject='old-concept' AND predicate='聚类于'"
    ).fetchone()


def test_cleanup_orphan_references_collects_existing_membership_only_orphan():
    conn = make_db()
    gl.ensure_node(conn, "old-concept", "Old Concept", "entity", entity_subtype="keyword")
    gl.add_node_origin(conn, "old-concept", "academic/wiki/papers/a", "raw/a#L1", managed=True)
    gl.ensure_node(conn, "academic/wiki/hubs/example", "Example", "hub")
    conn.execute(
        "INSERT INTO edges(subject,predicate,object,confidence,source) VALUES(?,?,?,?,?)",
        ("old-concept", "聚类于", "academic/wiki/hubs/example", "推断", ""),
    )
    conn.execute("DELETE FROM node_origins WHERE node_path='old-concept'")
    conn.commit()

    result = module.cleanup_orphan_references(conn)
    assert result["managed_orphan_nodes"] == 1
    assert result["derived_memberships"] == 1
    assert not conn.execute("SELECT 1 FROM nodes WHERE path='old-concept'").fetchone()


def test_cmd_ingest_writes_temporal_fact_end_to_end():
    """完整 graph_ingest 命令链路：frontmatter → cmd_ingest → temporal_facts → query。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page_rel = "admin/wiki/policies/e2e-policy"
        page_file = repo / (page_rel + ".md")
        page_file.parent.mkdir(parents=True)
        page_file.write_text(
            "---\n"
            'title: "端到端政策"\n'
            "type: policy\n"
            "sources:\n  - admin/raw/policies/e2e-policy.md\n"
            "source_type: official-doc\n"
            "date: 2026-07-01\n"
            "effective_from: 2026-07-01\n"
            "---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
            encoding="utf-8",
        )
        db_path = repo / "graph.db"
        old_repo, old_db = gl.REPO, gl.GRAPH_DB
        gl.REPO, gl.GRAPH_DB = repo, db_path
        conn = gl.connect(str(db_path))
        gl.init_schema(conn)
        conn.close()

        import hub_split
        import sync_keyword_aliases
        old_check, old_resolve = hub_split.check_all_hubs, sync_keyword_aliases.resolve_abbreviation_todo
        hub_split.check_all_hubs = lambda: []
        sync_keyword_aliases.resolve_abbreviation_todo = lambda conn: ([], 0)
        try:
            captured = StringIO()
            with redirect_stdout(captured):
                module.cmd_ingest(Namespace(
                    page=page_rel, db=str(db_path), clean=False,
                    semantic=None, citations=None, triples=None, triples_json=None,
                ))
            report = json.loads(captured.getvalue())
            assert report.get("temporal_facts_added") == 1, report

            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            facts = rows(conn, page_rel)
            assert facts[0]["valid_from"] == "2026-07-01"

            qscript = Path(__file__).with_name("query_graph.py")
            qspec = importlib.util.spec_from_file_location("query_graph_e2e", qscript)
            qmodule = importlib.util.module_from_spec(qspec)
            assert qspec.loader is not None
            qspec.loader.exec_module(qmodule)
            result = qmodule.temporal_at(conn, "2026-08-01", subject=page_rel)
            conn.close()
            assert result["count"] == 1, result
        finally:
            hub_split.check_all_hubs = old_check
            sync_keyword_aliases.resolve_abbreviation_todo = old_resolve
            gl.REPO, gl.GRAPH_DB = old_repo, old_db


def test_staged_page_preflight_checks_future_graph_and_rolls_back():
    """Catch missing author edges before commit, using the staged page identity."""
    import sync_keyword_aliases
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = "academic/wiki/papers/demo"
        original = repo / f"{page}.md"
        draft = repo / "temp/update/wiki.md"
        raw = repo / "academic/raw/references/demo/paper.md"
        db = repo / "cross-domain/graph.db"
        for path in (original, draft, raw, db):
            path.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("# Demo\n\nAlice Example\n\nSupported fact.\n", encoding="utf-8")
        text = (
            "---\ntitle: Demo\ntype: paper-summary\nauthors: [Alice Example]\n"
            "sources: [academic/raw/references/demo/paper.md]\n"
            "source_type: official-doc\ndate: 2026\nstatus: current\nconfidence: high\n"
            "created: 2026-10-01\nupdated: 2026-10-09\n---\n# Demo\n"
            "## Navigation\n\n## Content\nSupported fact.[^r1]\n"
            "## Sources\n[^r1]: academic/raw/references/demo/paper.md#L5\n"
        )
        original.write_text(text, encoding="utf-8")
        draft.write_text(text, encoding="utf-8")
        with (patch.object(gl, "REPO", repo), patch.object(gl, "GRAPH_DB", db),
              patch.object(module.ingest_check, "REPO", repo),
              patch.object(module.wl, "REPO", repo),
              patch.object(module.wl.raw_locator, "REPO", repo),
              patch.object(module, "_route_paper_from_file", return_value={}),
              patch.object(module.hs, "refresh_after_ingest", return_value={}),
              patch.object(sync_keyword_aliases, "sync", return_value=(0, 0, 0)),
              patch.object(sync_keyword_aliases, "resolve_abbreviation_todo", return_value=([], 0))):
            conn = gl.connect(str(db))
            gl.init_schema(conn)
            gl.ensure_node(conn, page, "Demo", "page")
            gl.ensure_node(conn, "existing-concept", "Existing concept", "entity")
            conn.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)",
                         (page, "研究基础", "existing-concept"))
            conn.commit()
            before = list(conn.iterdump())
            args = Namespace(page=page, page_file=str(draft), db=str(db), clean=False,
                             semantic=None, citations=None, triples=None, triples_json="[]",
                             plan_only=True, validate_page=True, metadata_only=False)
            # Direct empty triples leave the old missing author uncorrected. The
            # prospective graph gate must reject and roll back Raw/node updates.
            for plan_only in (True, False):
                args.plan_only = plan_only
                try:
                    with redirect_stdout(StringIO()):
                        module.cmd_ingest(args)
                    assert False, "missing author must fail before commit"
                except module.PageValidationError as exc:
                    assert exc.report["stage"] == "graph"
                    assert any("作者集合" in error for error in exc.report["errors"])
                assert list(conn.iterdump()) == before
            args.plan_only = True
            args.triples_json = None
            args.metadata_only = True
            output = StringIO()
            with redirect_stdout(output):
                module.cmd_ingest(args)
            report = json.loads(output.getvalue())
            assert report["plan_only"] and not report["committed"]
            assert not report["page_validation"]["errors"]
            assert list(conn.iterdump()) == before
            assert original.read_text(encoding="utf-8") == text
            # Temp location must not bypass locator validation or domain enums.
            draft.write_text(text.replace("#L5", "#L500").replace("status: current", "status: archived"),
                             encoding="utf-8")
            try:
                module.cmd_ingest(args)
                assert False, "invalid staged page must fail"
            except module.PageValidationError as exc:
                assert exc.report["stage"] == "wiki"
                assert any("locator 不存在" in error for error in exc.report["errors"])
                assert any("status 非法" in error for error in exc.report["errors"])
            assert list(conn.iterdump()) == before
            draft.write_text(text, encoding="utf-8")
            args.plan_only = False
            with redirect_stdout(StringIO()):
                module.cmd_ingest(args)
            assert not module.ingest_check.graph_checks(original, connection=conn)[0]
            assert conn.execute("SELECT 1 FROM edges WHERE subject=? AND object='existing-concept'",
                                (page,)).fetchone()
            args.clean = True
            try:
                module.cmd_ingest(args)
                assert False, "metadata update cannot erase semantic edges"
            except ValueError as exc:
                assert "--clean" in str(exc)
            conn.close()


def test_derived_concept_use_records_origin_and_survives_creator_cleanup():
    conn = make_db()
    page_a, page_b = "academic/wiki/papers/a", "academic/wiki/papers/b"
    for page in (page_a, page_b):
        add_node(conn, page, "page")
    gl.ensure_node(conn, "shared", "Shared", "entity", entity_subtype="keyword")
    gl.add_node_origin(conn, "shared", page_a, "raw/a#L1", managed=True)
    # Page B uses Shared only through generated containment, not an input endpoint.
    mention = "Shared仍需验证"
    triples = [{"subject": page_b, "predicate": "局限性", "object": mention}]
    plan = {"decisions": [{"mention": mention, "action": "create_local", "target": mention}]}
    module.add_knowledge_edges(conn, page_b, triples, attach_plan=plan)
    assert conn.execute("SELECT 1 FROM node_origins WHERE node_path='shared' AND origin_page=?", (page_b,)).fetchone()
    # Reusing the derived edge must register its endpoint too, without duplication.
    conn.execute("DELETE FROM node_origins WHERE node_path='shared' AND origin_page=?", (page_b,))
    module.add_knowledge_edges(conn, page_b, triples, attach_plan=plan)
    assert conn.execute("SELECT COUNT(*) FROM edges WHERE predicate='包含' AND object='shared'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM node_origins WHERE node_path='shared' AND origin_page=?", (page_b,)).fetchone()[0] == 1
    module.clean_page_edges(conn, page_a)
    assert gl.node_exists(conn, "shared")
    assert not gv.validate_graph(conn, gv.load_config())["errors"]
    module.clean_page_edges(conn, page_b)
    assert not gl.node_exists(conn, "shared")
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"graph_ingest temporal regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

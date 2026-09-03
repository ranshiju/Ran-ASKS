#!/usr/bin/env python3
"""graph_repair.py 的纯代码回归测试。"""
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

SCRIPT = Path(__file__).with_name("graph_repair.py")
spec = importlib.util.spec_from_file_location("graph_repair", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def make_conn(directory):
    conn = sqlite3.connect(Path(directory) / "graph.db")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    return conn


def insert_edge(conn, subject, predicate, obj, confidence="可追溯", source=""):
    return conn.execute(
        "INSERT INTO edges (subject, predicate, object, confidence, source) VALUES (?,?,?,?,?)",
        (subject, predicate, obj, confidence, source),
    ).lastrowid


def test_normalize_legacy_and_medium_confidence():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_conn(directory)
        gl.ensure_node(conn, "academic/wiki/a", "A", "page")
        gl.ensure_node(conn, "academic/wiki/b", "B", "page")
        insert_edge(conn, "academic/wiki/a", "涉及", "academic/wiki/b", "[可追溯]", "source-a")
        insert_edge(conn, "academic/wiki/b", "引用", "academic/wiki/a", "medium", "source-b")
        conn.commit()
        result = module.repair_confidence(conn, True)
        conn.commit()
        values = [r[0] for r in conn.execute("SELECT confidence FROM edges ORDER BY id")]
        conn.close()
        assert result["[可追溯]"] == 1
        assert result["medium"] == 1
        assert values == ["可追溯", "推断"]


def test_migrate_raw_support_edge_to_wiki_source_edge():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_conn(directory)
        gl.ensure_node(conn, "academic/wiki/papers/paper", "Paper", "page")
        gl.ensure_node(conn, "academic/raw/test", "Test raw", "raw")
        insert_edge(
            conn, "academic/raw/test", "事实支撑", "academic/wiki/papers/paper",
            "可追溯", "academic/raw/test#全篇")
        conn.commit()
        report = module.migrate_raw_links(conn, True)
        conn.commit()
        row = conn.execute(
            "SELECT subject,predicate,object,source FROM edges"
        ).fetchone()
        conn.close()
        assert report == {"legacy": 1, "created": 1, "reused": 0, "removed": 1}
        assert tuple(row[:3]) == (
            "academic/wiki/papers/paper", "来源", "academic/raw/test")
        assert row["source"] == ""


def test_sync_file_nodes_groups_same_stem_raw_companions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = root / "academic/wiki/papers/demo.md"
        raw_dir = root / "academic/raw/references/demo"
        wiki.parent.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        wiki.write_text(
            "---\ntitle: Demo\ntype: paper-summary\nsources:\n"
            "  - academic/raw/references/demo/paper.md\n---\n",
            encoding="utf-8",
        )
        (raw_dir / "paper.md").write_text("# Demo\n", encoding="utf-8")
        (raw_dir / "paper.pdf").write_bytes(b"%PDF-1.4")
        original_repo = gl.REPO
        try:
            gl.REPO = root
            conn = make_conn(directory)
            report = module.sync_file_nodes_and_source_edges(conn, True)
            conn.commit()
            assert report["wiki_nodes_created"] == 1
            assert report["raw_nodes_created"] == 1
            assert report["source_edges_created"] == 1
            assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
            assert conn.execute(
                "SELECT subject,predicate,object FROM edges"
            ).fetchone()[:] == (
                "academic/wiki/papers/demo", "来源", "academic/raw/references/demo/paper"
            )
            aliases = dict(conn.execute("SELECT alias,node_path FROM aliases"))
            assert aliases["academic/raw/references/demo/paper.md"] == (
                "academic/raw/references/demo/paper"
            )
            assert aliases["academic/raw/references/demo/paper.pdf"] == (
                "academic/raw/references/demo/paper"
            )
            conn.close()
        finally:
            gl.REPO = original_repo


def test_merge_duplicate_edges_preserves_evidence():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_conn(directory)
        gl.ensure_node(conn, "academic/wiki/a", "A", "page")
        gl.ensure_node(conn, "academic/wiki/b", "B", "page")
        e1 = insert_edge(conn, "academic/wiki/a", "涉及", "academic/wiki/b", "可追溯", "s1")
        e2 = insert_edge(conn, "academic/wiki/a", "涉及", "academic/wiki/b", "可追溯", "s2")
        gl.add_edge_evidence(conn, e1, "s1", "", False)
        conn.commit()
        report = module.merge_duplicate_edges(conn, True)
        conn.commit()
        assert report["groups"] == 1
        assert report["removed"] == 1
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
        evidence = {r[0] for r in conn.execute("SELECT source FROM edge_evidence").fetchall()}
        conn.close()
        assert evidence == {"s1"}


def test_targeted_orphan_cleanup_is_explicit_and_conservative():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_conn(directory)
        gl.ensure_node(conn, "orphan", "Polluted title", "entity")
        gl.insert_aliases(conn, "orphan", ["bad alias"])
        gl.ensure_node(conn, "connected", "Connected", "entity")
        gl.ensure_node(conn, "page", "Page", "page")
        insert_edge(conn, "connected", "涉及", "page")
        conn.commit()

        dry = module.execute_targeted_orphans(conn, ["orphan", "connected", "page"], False)
        assert [item["decision"] for item in dry] == ["remove", "blocked", "blocked"]
        assert conn.execute("SELECT 1 FROM nodes WHERE path='orphan'").fetchone()

        applied = module.execute_targeted_orphans(conn, ["orphan", "connected", "page"], True)
        assert applied[0]["removed"] is True
        assert not conn.execute("SELECT 1 FROM nodes WHERE path='orphan'").fetchone()
        assert not conn.execute("SELECT 1 FROM aliases WHERE node_path='orphan'").fetchone()
        assert conn.execute("SELECT 1 FROM nodes WHERE path='connected'").fetchone()
        assert conn.execute("SELECT 1 FROM nodes WHERE path='page'").fetchone()
        conn.close()


def test_description_audit_only_selects_source_backed_keyword_origins():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki = root / "academic/wiki/papers/demo.md"
        raw = root / "academic/raw/references/demo/paper.md"
        wiki.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        wiki.write_text(
            "---\ntitle: Demo\ntype: paper-summary\nsources:\n"
            "  - academic/raw/references/demo/paper.md\n---\n",
            encoding="utf-8",
        )
        raw.write_text("# Demo\n", encoding="utf-8")
        original_repo = gl.REPO
        try:
            gl.REPO = root
            conn = make_conn(directory)
            gl.ensure_node(conn, "missing", "缺失概念", "entity", entity_subtype="keyword")
            gl.ensure_node(
                conn, "valid", "完整概念", "entity", entity_subtype="keyword",
                description="该概念用于描述测试中的完整语义对象。",
            )
            gl.ensure_node(conn, "blocked", "无来源概念", "entity", entity_subtype="keyword")
            gl.ensure_node(conn, "中文概念", "中文概念", "entity", entity_subtype="keyword")
            gl.ensure_node(
                conn, "中文概念Chinese concept", "中文概念Chinese concept", "entity",
                entity_subtype="keyword",
            )
            gl.add_node_origin(conn, "missing", "academic/wiki/papers/demo")
            gl.add_node_origin(conn, "中文概念", "academic/wiki/papers/demo")
            gl.add_node_origin(conn, "中文概念Chinese concept", "academic/wiki/papers/demo")
            report = module.semantic_description_audit(conn, details=True)
            assert report["keyword_count"] == 5
            assert report["valid_description_count"] == 1
            assert report["source_recoverable_nodes"] == 1
            assert report["lineage_blocked_nodes"] == 1
            assert report["identity_review_nodes"] == 2
            assert report["identity_issues"] == {"possible_bilingual_duplicate": 2}
            assert report["reingest_pages"] == [{
                "page": "academic/wiki/papers/demo",
                "raw_inputs": ["academic/raw/references/demo/paper.md"],
            }]
            conn.close()
        finally:
            gl.REPO = original_repo


def test_description_audit_rejects_deictic_and_identity_metadata_text():
    assert module.keyword_description_issue(
        "矩阵乘积态", "该文档使用矩阵乘积态压缩表示量子多体波函数。"
    ) == "deictic_context"
    assert module.keyword_description_issue(
        "CONFLICTBANK", "Agent-confirmed abbreviation kind: dataset_or_model"
    ) == "identity_metadata"
    assert not module.keyword_description_issue(
        "矩阵乘积态", "矩阵乘积态以一维张量链压缩表示量子多体波函数。"
    )
    assert module.keyword_identity_issue("Delta") == "generic_symbol_label"
    assert module.keyword_identity_issue("θ") == "generic_symbol_label"
    assert module.keyword_identity_issue("x") == "generic_symbol_label"
    assert module._keyword_identity_issues([
        {"path": "Delta", "title": "工作区间覆盖混沌与非遍历两区"},
    ]) == {"Delta": "generic_symbol_label"}


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"graph_repair regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

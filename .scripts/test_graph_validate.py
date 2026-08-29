#!/usr/bin/env python3
"""graph_validate.py 的纯代码回归测试。"""
import importlib.util
import sqlite3
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

SCRIPT = Path(__file__).with_name("graph_validate.py")
spec = importlib.util.spec_from_file_location("graph_validate", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def isolated_db():
    tmp = tempfile.TemporaryDirectory()
    conn = sqlite3.connect(Path(tmp.name) / "graph.db")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    return tmp, conn


def insert(conn, path, node_type="entity", subtype=None):
    conn.execute("INSERT INTO nodes (path, type, entity_subtype) VALUES (?,?,?)", (path, node_type, subtype))


def test_valid_graph_has_no_errors():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        insert(conn, "academic/wiki/b", "page")
        conn.execute(
            "INSERT INTO edges (subject, predicate, object, confidence, source) VALUES (?,?,?,?,?)",
            ("academic/wiki/a", "涉及", "academic/wiki/b", "可追溯", ""),
        )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert not report["errors"]


def test_invalid_node_type_and_entity_subtype_are_errors():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/bad", "unknown")
        insert(conn, "academic/wiki/bad-subtype", "entity", "unknown-subtype")
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["unknown_node_type"] == 1
        assert report["counts"]["unknown_entity_subtype"] == 1
        assert len(report["errors"]) == 2


def test_legacy_confidence_is_warning_but_missing_evidence_is_not_checked():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        insert(conn, "academic/wiki/b", "page")
        conn.execute(
            "INSERT INTO edges (subject, predicate, object, confidence, source) VALUES (?,?,?,?,?)",
            ("academic/wiki/a", "涉及", "academic/wiki/b", "[可追溯]", ""),
        )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["legacy_edge_confidence"] == 1
        assert report["counts"]["unknown_edge_confidence"] == 0
        assert report["warnings"]


def test_duplicate_edges_are_warned():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        insert(conn, "academic/wiki/b", "page")
        for _ in range(2):
            conn.execute(
                "INSERT INTO edges (subject, predicate, object, confidence, source) VALUES (?,?,?,?,?)",
                ("academic/wiki/a", "涉及", "academic/wiki/b", "推断", ""),
            )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["duplicate_semantic_edge"] == 1
        assert report["warnings"]


def test_temporal_fact_dangling_endpoint_is_error():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        conn.execute(
            "INSERT INTO temporal_facts (subject, predicate, object, valid_from, source) VALUES (?,?,?,?,?)",
            ("academic/wiki/a", "负责", "academic/wiki/missing", "2024-01-01", "academic/raw/test.md"),
        )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["dangling_temporal_fact_endpoint"] == 1
        assert report["errors"]


def test_temporal_fact_invalid_date_is_error():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        insert(conn, "academic/wiki/b", "page")
        conn.execute(
            "INSERT INTO temporal_facts (subject, predicate, object, valid_from, source) VALUES (?,?,?,?,?)",
            ("academic/wiki/a", "负责", "academic/wiki/b", "2024-01", "academic/raw/test.md"),
        )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["invalid_temporal_date"] == 1
        assert report["errors"]


def test_temporal_fact_inverted_window_is_error():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        insert(conn, "academic/wiki/a", "page")
        insert(conn, "academic/wiki/b", "page")
        conn.execute(
            "INSERT INTO temporal_facts (subject, predicate, object, valid_from, valid_until, source) VALUES (?,?,?,?,?,?)",
            ("academic/wiki/a", "负责", "academic/wiki/b", "2024-02-01", "2024-01-01", "academic/raw/test.md"),
        )
        conn.commit()
        report = module.validate_graph(conn, module.DEFAULTS)
        conn.close()
        assert report["counts"]["inverted_temporal_window"] == 1
        assert report["errors"]


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"graph_validate regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

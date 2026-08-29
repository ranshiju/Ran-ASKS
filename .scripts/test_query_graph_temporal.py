#!/usr/bin/env python3
"""query_graph.py temporal 子命令的纯代码回归测试。"""
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

SCRIPT = Path(__file__).with_name("query_graph.py")
spec = importlib.util.spec_from_file_location("query_graph", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def make_db(directory):
    conn = sqlite3.connect(Path(directory) / "graph.db")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    gl.ensure_node(conn, "page-a", "A", "page")
    gl.ensure_node(conn, "page-b", "B", "page")
    return conn


def add_fact(conn, fid=None, subject="page-a", predicate="政策", obj="page-b", valid_from=None, valid_until=None, superseded_by=None, source="raw/policy.md"):
    keys = ["subject", "predicate", "object", "valid_from", "valid_until", "superseded_by", "source"]
    values = [subject, predicate, obj, valid_from, valid_until, superseded_by, source]
    if fid is not None:
        keys.insert(0, "id")
        values.insert(0, fid)
    conn.execute(f"INSERT INTO temporal_facts ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})", values)
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_point_in_time_filters_by_validity():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        add_fact(conn, valid_from="2024-01-01", valid_until="2024-06-30", source="raw/old.md")
        add_fact(conn, valid_from="2024-07-01", valid_until=None, source="raw/current.md")
        conn.commit()
        before = module.temporal_at(conn, "2024-03-15")
        after = module.temporal_at(conn, "2024-08-01")
        conn.close()
        assert before["count"] == 1
        assert before["facts"][0]["source"] == "raw/old.md"
        assert after["count"] == 1
        assert after["facts"][0]["source"] == "raw/current.md"


def test_superseded_fact_returns_only_for_old_snapshot():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        successor_id = add_fact(conn, valid_from="2024-07-01", valid_until=None, source="raw/new.md")
        add_fact(conn, valid_from="2024-01-01", valid_until=None, superseded_by=successor_id, source="raw/old.md")
        conn.commit()
        old_result = module.temporal_at(conn, "2024-03-15")
        new_result = module.temporal_at(conn, "2024-08-01")
        conn.close()
        assert old_result["count"] == 1
        assert old_result["facts"][0]["source"] == "raw/old.md"
        assert new_result["count"] == 1
        assert new_result["facts"][0]["source"] == "raw/new.md"


def test_filters_subject_object_predicate():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        add_fact(conn, predicate="属于", valid_from="2024-01-01")
        add_fact(conn, predicate="负责", valid_from="2024-01-01")
        conn.commit()
        result = module.temporal_at(conn, "2024-06-01", subject="page-a", obj="page-b", predicate="属于")
        conn.close()
        assert result["count"] == 1
        assert result["facts"][0]["predicate"] == "属于"


def test_invalid_date_returns_error():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        result = module.temporal_at(conn, "not-a-date")
        conn.close()
        assert set(result) == {"error"}


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"query_graph temporal regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

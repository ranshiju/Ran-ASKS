#!/usr/bin/env python3
"""query_graph.py 搜索、来源详情、关系与时态的纯代码回归测试。"""
import importlib.util
import json
import sqlite3
import subprocess
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


def test_node_info_exposes_description_and_raw_located_glosses():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(
            conn, "concept", "Concept", "entity", entity_subtype="keyword"
        )
        gl.add_node_gloss(
            conn, "concept", "academic/wiki/papers/example",
            "academic/raw/references/example/paper.md#L55", "局部概念说明",
        )
        result = module.node_info(conn, "concept")
        conn.close()
    assert result["entity_subtype"] == "keyword"
    assert result["description"] == "局部概念说明"
    assert result["glosses"] == [{
        "origin_page": "academic/wiki/papers/example",
        "source": "academic/raw/references/example/paper.md#L55",
        "description": "局部概念说明",
        "is_primary": 1,
    }]


def test_relations_profile_filters_derived_predicate_families():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "raw-a", "Raw A", "raw")
        gl.ensure_node(conn, "hub-a", "Hub A", "hub")
        gl.ensure_node(conn, "author-a", "Author A", "entity", entity_subtype="person")
        conn.executemany(
            "INSERT INTO edges (subject,predicate,object,confidence) VALUES (?,?,?,?)",
            [
                ("page-a", "来源", "raw-a", "可追溯"),
                ("page-a", "核心方法", "page-b", "可追溯"),
                ("page-a", "主要研究", "hub-a", "可追溯"),
                ("author-a", "作者", "page-a", "可追溯"),
            ],
        )
        result = module.relations(conn, "page-a", profile="explanation")
        conn.close()
    assert {edge["family"] for edge in result["edges"]} == {"evidence", "semantic"}
    assert {edge["predicate"] for edge in result["edges"]} == {"来源", "核心方法"}


def test_search_ranks_all_exact_candidates_before_limit_and_keeps_ambiguity():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        for index in range(60):
            gl.ensure_node(conn, f"noise-{index:02}", f"a decomposition {index}", "entity")
        gl.ensure_node(conn, "MPO", "Matrix product operator", "entity")
        gl.ensure_node(conn, "z-title", "mpo", "page")
        gl.ensure_node(conn, "z-alias", "Another meaning", "entity")
        gl.insert_aliases(conn, "z-alias", ["MPO", "MPO alternative", "inside MPO"])
        gl.ensure_node(conn, "prefix", "MPO method", "entity")
        before = conn.total_changes
        result = module.search_nodes(conn, "MPO")
        assert conn.total_changes == before
        assert [node["path"] for node in result["nodes"][:4]] == [
            "MPO", "z-alias", "z-title", "prefix",
        ]
        assert len({node["path"] for node in result["nodes"]}) == 50
        assert result["count"] == 50 and result["truncated"] is True
        assert result["nodes"] == module.search_nodes(conn, "mpo")["nodes"]
        text = module.fmt_text(result, "search")
        assert "仍有更多匹配" in text and "当前展示前 20 个" in text
        conn.close()


def test_search_literal_patterns_granularity_and_exact_limit():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        gl.ensure_node(conn, "name_1", "Percent 20%", "entity", entity_subtype="keyword")
        gl.ensure_node(conn, "nameA1", "Percent 200", "entity", entity_subtype="proposition")
        gl.insert_aliases(conn, "name_1", [r"path\part", "Shared"])
        gl.insert_aliases(conn, "nameA1", ["Shared"])
        for term in ("_", "%", "\\"):
            result = module.search_nodes(conn, term)
            assert [node["path"] for node in result["nodes"]] == ["name_1"], term
        result = module.search_nodes(conn, "Shared", granularity="keyword", top_k=1)
        assert [node["path"] for node in result["nodes"]] == ["name_1"]
        assert result["truncated"] is False
        result = module.search_nodes(conn, "Shared", top_k=1)
        assert result["count"] == 1 and result["truncated"] is True
        result = module.search_nodes(conn, "Shared", top_k=2)
        assert result["count"] == 2 and result["truncated"] is False
        empty = module.search_nodes(conn, "not-in-graph")
        assert empty["nodes"] == [] and empty["truncated"] is False
        conn.close()


def test_search_cli_honors_top_k():
    with tempfile.TemporaryDirectory() as directory:
        conn = make_db(directory)
        for index in range(4):
            gl.ensure_node(conn, f"term-{index}", f"Term {index}", "entity")
        conn.commit()
        conn.close()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "search", "Term", "--top-k", "2", "--json",
             "--db", str(Path(directory) / "graph.db")],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(result.stdout)
        assert payload["count"] == payload["limit"] == 2
        assert payload["truncated"] is True


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"query_graph regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

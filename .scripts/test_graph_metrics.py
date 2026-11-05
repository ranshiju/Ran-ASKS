#!/usr/bin/env python3
"""graph_metrics consolidation candidate regression tests."""
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

SCRIPT = Path(__file__).with_name("graph_metrics.py")
spec = importlib.util.spec_from_file_location("graph_metrics", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_consolidation_candidates_are_read_only_and_thresholded():
    with tempfile.TemporaryDirectory() as directory:
        conn = sqlite3.connect(Path(directory) / "graph.db")
        conn.row_factory = sqlite3.Row
        gl.init_schema(conn)
        gl.ensure_node(conn, "concept-a", "Concept A", "entity", entity_subtype="keyword")
        gl.ensure_node(conn, "claim-a", "Claim A", "entity", entity_subtype="proposition")
        gl.ensure_node(conn, "hub-a", "Hub A", "hub")
        for index in range(1, 6):
            page = f"academic/wiki/papers/p{index}"
            gl.ensure_node(conn, page, f"P{index}", "page")
            conn.execute(
                "INSERT INTO edges (subject,predicate,object,confidence) VALUES (?,?,?,?)",
                (page, "核心方法", "concept-a", "可追溯"),
            )
            conn.execute(
                "INSERT INTO edges (subject,predicate,object,confidence) VALUES (?,?,?,?)",
                (page, "主要研究", "hub-a", "可追溯"),
            )
            if index <= 2:
                conn.execute(
                    "INSERT INTO edges (subject,predicate,object,confidence) VALUES (?,?,?,?)",
                    (page, "核心创新点", "claim-a", "可追溯"),
                )
        conn.commit()
        before = conn.total_changes
        report = module.consolidation_candidates(conn)
        after = conn.total_changes
        conn.close()

    actions = {(item["action"], item["target"]) for item in report["candidates"]}
    assert ("PROMOTE_CONCEPT", "concept-a") in actions
    assert ("PROMOTE_PROPOSITION", "claim-a") in actions
    assert ("CREATE_REVIEW", "hub-a") in actions
    assert report["writes"] is False
    assert before == after


def test_legacy_shape_observation_is_optional_and_not_a_repair_request():
    name = "基于自注意力的查询构造Query Formulation based on Self-attention(QFS)"
    for command in ("all", "unsplit_phrases"):
        conn = gl.connect(":memory:")
        gl.init_schema(conn)
        gl.ensure_node(conn, name, name, "entity", entity_subtype="keyword")
        changes = conn.total_changes
        report = module.check_unsplit_phrases(conn)
        assert report["flagged_count"] == 1  # Keep historical observations visible on demand.
        assert conn.total_changes == changes
        output = StringIO()
        with patch.object(module, "connect", return_value=conn), \
                patch.object(module, "load_state", return_value={}), \
                patch.object(sys, "argv", ["graph_metrics.py", command]), \
                patch.object(module, "check_unsplit_phrases", wraps=module.check_unsplit_phrases) as scan, \
                redirect_stdout(output):
            module.main()
        text = output.getvalue()
        assert "需拆分" not in text and "建议 LINT" not in text
        if command == "all":
            assert scan.call_count == 0
            assert "unsplit_phrases" in text and "不计入缺陷或修复待办" in text
            assert "连通性" in text and "谓词" in text
            assert name not in text
        else:
            assert scan.call_count == 1 and name in text
            assert "非质量缺陷" in text and "无须据此拆分或修补" in text


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"graph_metrics regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""graph_metrics consolidation candidate regression tests."""
import importlib.util
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


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"graph_metrics regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

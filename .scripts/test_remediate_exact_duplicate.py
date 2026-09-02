#!/usr/bin/env python3
"""Regression tests for exact duplicate remediation."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import remediate_exact_duplicate as rd


def _fixture(root: Path, *, same_bytes: bool = True):
    canonical = "academic/wiki/papers/paper"
    duplicate = "academic/wiki/papers/paper-2"
    for page, raw_id, payload in (
        (canonical, "paper", b"same-pdf"),
        (duplicate, "paper-2", b"same-pdf" if same_bytes else b"different-pdf"),
    ):
        raw = root / "academic/raw/references" / raw_id
        raw.mkdir(parents=True)
        (raw / "paper.pdf").write_bytes(payload)
        (raw / "paper.md").write_text("# Same paper\n", encoding="utf-8")
        wiki = root / f"{page}.md"
        wiki.parent.mkdir(parents=True, exist_ok=True)
        wiki.write_text(
            "---\ntitle: Same paper\ntype: paper-summary\n"
            f"sources: [academic/raw/references/{raw_id}/paper.md]\n---\n# Same paper\n",
            encoding="utf-8",
        )
    wiki_dir = root / "academic/wiki"
    (wiki_dir / "index.md").write_text(
        "- [[papers/paper]]\n- [[papers/paper-2]]\n", encoding="utf-8")
    (wiki_dir / "log.md").write_text("# Log\n", encoding="utf-8")
    db = root / "cross-domain/graph.db"
    db.parent.mkdir()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    for node, title, kind in (
        (canonical, "Same paper", "page"),
        (duplicate, "Same paper", "page"),
        ("academic/raw/references/paper/paper", "paper", "raw"),
        ("academic/raw/references/paper-2/paper", "paper-2", "raw"),
    ):
        gl.ensure_node(conn, node, title, kind)
    for page, raw in (
        (canonical, "academic/raw/references/paper/paper"),
        (duplicate, "academic/raw/references/paper-2/paper"),
    ):
        cursor = conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '来源', ?, '机械', '', 0)", (page, raw))
        gl.add_edge_origin(conn, cursor.lastrowid, page, "")
    conn.commit()
    conn.close()
    return canonical, duplicate, db


def test_dry_run_is_read_only_and_apply_preserves_raw():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical, duplicate, db = _fixture(root)
        raw_files = list((root / "academic/raw").rglob("*"))
        raw_before = {path: path.read_bytes() for path in raw_files if path.is_file()}
        plan = rd.build_plan(canonical, duplicate, repo=root, graph_db=db)
        assert plan["status"] == "ready"
        assert (root / f"{duplicate}.md").is_file()
        assert raw_before == {path: path.read_bytes() for path in raw_before}

        result = rd.apply_plan(plan, repo=root, graph_db=db)
        assert result["status"] == "remediated"
        assert not (root / f"{duplicate}.md").exists()
        assert (root / result["archive"]).is_file()
        assert raw_before == {path: path.read_bytes() for path in raw_before}
        assert "[[papers/paper-2]]" not in (root / "academic/wiki/index.md").read_text()
        conn = sqlite3.connect(db)
        assert not conn.execute("SELECT 1 FROM nodes WHERE path=?", (duplicate,)).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM nodes WHERE path='academic/raw/references/paper-2/paper'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM aliases WHERE alias=? AND node_path=?",
            (duplicate, canonical),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM aliases WHERE alias=? AND node_path=?",
            ("academic/raw/references/paper-2/paper", "academic/raw/references/paper/paper"),
        ).fetchone()
        conn.close()


def test_nonidentical_pdf_is_blocked():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical, duplicate, db = _fixture(root, same_bytes=False)
        try:
            rd.build_plan(canonical, duplicate, repo=root, graph_db=db)
        except ValueError as exc:
            assert "并非字节级完全一致" in str(exc)
        else:
            raise AssertionError("nonidentical PDFs must be blocked")


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"duplicate remediation regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

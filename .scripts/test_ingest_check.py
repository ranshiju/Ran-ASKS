#!/usr/bin/env python3
"""ingest_check 新增确定性规则回归测试。"""
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ingest_check


def test_graph_flag_is_not_treated_as_path():
    import subprocess
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("ingest_check.py")), "--graph", "--help"],
        capture_output=True, text=True,
    )
    assert "路径不存在,跳过: --graph" not in result.stderr


def test_graph_checks_with_isolated_database():
    import sqlite3
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\ntitle: Test\n---\n", encoding="utf-8")
        db = repo / "cross-domain/graph.db"
        db.parent.mkdir()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE nodes (path TEXT PRIMARY KEY);
            CREATE TABLE edges (subject TEXT, predicate TEXT, object TEXT, source TEXT);
        """)
        conn.execute("INSERT INTO nodes VALUES (?)", ("academic/wiki/papers/test",))
        conn.execute("INSERT INTO edges VALUES (?,?,?,?)", ("academic/wiki/papers/test", "涉及", "测试", "raw/test.md"))
        conn.commit()
        conn.close()
        old_repo = ingest_check.REPO
        ingest_check.REPO = repo
        try:
            errors, warnings = ingest_check.graph_checks(page)
        finally:
            ingest_check.REPO = old_repo
        assert not errors
        assert not warnings


def test_graph_checks_rejects_cross_layer_metadata_without_requiring_locator():
    import sqlite3
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: Test\ntype: paper-summary\n"
            "authors: [Alice Smith, Bob Li]\n"
            "venue: 'Phys. Rev. A 91, 032306 (2015)'\n---\n"
            "## Navigation\nQuantum folding.\n## Content\nX\n",
            encoding="utf-8",
        )
        db = repo / "cross-domain/graph.db"
        db.parent.mkdir()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE nodes (path TEXT PRIMARY KEY, title TEXT, type TEXT);
            CREATE TABLE edges (id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT,
                                object TEXT, confidence TEXT, source TEXT);
            CREATE TABLE edge_evidence (edge_id INTEGER, source TEXT);
            CREATE TABLE edge_origins (edge_id INTEGER, origin_page TEXT, source TEXT);
        """)
        nodes = [
            ("academic/wiki/papers/test", "Test", "paper-summary"),
            ("Alice Smith", "Alice Smith", "entity"),
            ("Bob Li", "Bob Li", "entity"),
            ("missing", "（wiki 未提供）", "entity"),
        ]
        conn.executemany("INSERT INTO nodes VALUES (?,?,?)", nodes)
        edges = [
            (1, "Alice Smith", "第一作者", "academic/wiki/papers/test", "可追溯", "academic/raw/test.md"),
            (2, "Bob Li", "作者", "academic/wiki/papers/test", "可追溯", "academic/raw/test.md"),
            (3, "academic/wiki/papers/test", "发表于", "missing", "可追溯", "academic/raw/test.md"),
        ]
        conn.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?)", edges)
        conn.executemany(
            "INSERT INTO edge_origins VALUES (?,?,?)",
            [(edge_id, "academic/wiki/papers/test", "academic/raw/test.md") for edge_id in (1, 2, 3)],
        )
        conn.commit()
        conn.close()
        old_repo = ingest_check.REPO
        ingest_check.REPO = repo
        try:
            errors, _warnings = ingest_check.graph_checks(page)
        finally:
            ingest_check.REPO = old_repo
        assert any("期刊元数据含占位符" in error for error in errors)
        assert any("发表于边与 wiki venue 不一致" in error for error in errors)
        assert not any("evidence locator" in error for error in errors)


def check(text, rel="academic/wiki/papers/test.md"):
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        return ingest_check.check_file(path, set(), set())
    finally:
        path.unlink(missing_ok=True)


def test_extract_engine_warns_on_non_mineru():
    """非 mineru 提取的 raw 全文 md 应报 WARN(mineru 不报)。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw_dir = repo / "academic/raw/references/test-engine"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.md").write_text("# title\n", encoding="utf-8")
        (raw_dir / "parse_meta.yaml").write_text("preferred: pymupdf\n", encoding="utf-8")
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\n"
            "title: Test\ntype: paper-summary\n"
            "sources: [academic/raw/references/test-engine/paper.md]\n"
            "source_type: official-doc\ndate: 2024\nstatus: current\n"
            "created: 2026-07-27\nupdated: 2026-07-27\n"
            "---\n## Navigation\nx\n## Content\ny\n",
            encoding="utf-8",
        )
        old_repo = ingest_check.REPO
        ingest_check.REPO = repo
        try:
            errors, warnings = ingest_check.check_file(page, set(), set())
        finally:
            ingest_check.REPO = old_repo
        assert not errors
        assert any("mineru" in w for w in warnings), f"expected non-mineru warn, got {warnings}"

        # mineru 不报
        (raw_dir / "parse_meta.yaml").write_text("preferred: mineru\n", encoding="utf-8")
        ingest_check.REPO = repo
        try:
            errors, warnings = ingest_check.check_file(page, set(), set())
        finally:
            ingest_check.REPO = old_repo
        assert not errors
        assert not any("mineru" in w for w in warnings), f"mineru should not warn, got {warnings}"


def test_bibliographic_consistency_uses_published_year_and_aps_doi():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw_dir = repo / "academic/raw/references/demo"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.md").write_text("# Demo\n", encoding="utf-8")
        (raw_dir / "source.yaml").write_text(
            "bibliographic:\n"
            "  year: '2012'\n"
            "  venue: ''\n"
            "  doi: 10.1103/PhysRevB.88.035103\n"
            "  first_page_evidence:\n"
            "  - '(Received 18 December 2012; published 3 July 2013)'\n",
            encoding="utf-8",
        )
        page = repo / "academic/wiki/papers/demo.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: Demo\ntype: paper-summary\n"
            "sources: [academic/raw/references/demo/paper.md]\n"
            "source_type: official-doc\ndate: 2012\nvenue: 'Phys. Rev. B 86, 245107 (2012)'\n"
            "status: current\ncreated: 2026-08-24\nupdated: 2026-08-24\n---\n"
            "## Navigation\nx\n## Content\ny\n",
            encoding="utf-8",
        )
        old_repo = ingest_check.REPO
        ingest_check.REPO = repo
        try:
            errors, _ = ingest_check.check_file(page, set(), set())
        finally:
            ingest_check.REPO = old_repo
    assert any("published year=2013" in error for error in errors)
    assert any("Phys. Rev. B 88, 035103" in error for error in errors)


def test_locator_aware_page_runs_only_minimal_closed_loop_checks():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw = repo / "academic/raw/references/demo/paper.md"
        page = repo / "academic/wiki/papers/demo.md"
        raw.parent.mkdir(parents=True)
        page.parent.mkdir(parents=True)
        raw.write_text("# Demo\n\nSupported fact.\n", encoding="utf-8")
        page.write_text(
            "---\ntitle: Demo\ntype: paper-summary\n"
            "sources: [academic/raw/references/demo/paper.md]\n"
            "source_type: official-doc\ndate: 2026\nconfidence: high\nstatus: current\n"
            "created: 2026-08-25\nupdated: 2026-08-25\n---\n"
            "# Demo\n\n## Navigation\n\nDemo.\n\n"
            "## Content\n\n### Method\n\nSupported fact.[^r1]\n\n"
            "## Sources\n\n[^r1]: academic/raw/references/demo/paper.md#L3\n",
            encoding="utf-8",
        )
        old_repos = (ingest_check.REPO, ingest_check.wl.REPO,
                     ingest_check.wl.raw_locator.REPO)
        ingest_check.REPO = repo
        ingest_check.wl.REPO = repo
        ingest_check.wl.raw_locator.REPO = repo
        try:
            errors, _warnings = ingest_check.check_file(page, set(), set())
            assert not errors
            page.write_text(page.read_text(encoding="utf-8").replace("#L3", "#L30"),
                            encoding="utf-8")
            errors, _warnings = ingest_check.check_file(page, set(), set())
        finally:
            (ingest_check.REPO, ingest_check.wl.REPO,
             ingest_check.wl.raw_locator.REPO) = old_repos
        assert any("locator 不存在" in error for error in errors)


def main():
    test_graph_checks_with_isolated_database()
    test_graph_checks_rejects_cross_layer_metadata_without_requiring_locator()
    valid = """---
title: Test
type: paper-summary
sources: [academic/raw/test.md]
source_type: official-doc
date: 2024
status: current
created: 2026-07-27
updated: 2026-07-27
---
## Navigation
x
## Content
y
"""
    errors, _ = check(valid)
    assert not errors

    bad_path = """---
title: Test
type: concept
sources: [academic/raw/test.md]
source_type: official-doc
date: nope
status: current
created: 2026-07-27
updated: 2026-07-26
---
## Navigation
x
## Content
y
"""
    errors, _ = check(bad_path)
    assert any("日期格式" in item for item in errors)
    assert any("updated 早于" in item for item in errors)

    invalid_calendar = valid.replace("date: 2024", "date: 2026-02-30")
    errors, _ = check(invalid_calendar)
    assert any("YAML 解析失败" in item for item in errors)

    paper_with_paired_pdf = valid.replace(
        "sources: [academic/raw/test.md]",
        "sources: [academic/raw/test.md#abstract]",
    )
    errors, warns = check(paper_with_paired_pdf)
    assert not errors
    assert not any("未同时列出" in item or "配对" in item for item in warns)

    invalid_effective = valid.replace(
        "date: 2024",
        "date: 2024\neffective_from: 2026-07",
    )
    errors, _ = check(invalid_effective)
    assert any("effective_from 日期格式非法" in item for item in errors)

    inverted_effective = valid.replace(
        "date: 2024",
        "date: 2024\neffective_from: 2026-02-01\neffective_to: 2026-01-01",
    )
    errors, _ = check(inverted_effective)
    assert any("effective_to 早于 effective_from" in item for item in errors)

    test_extract_engine_warns_on_non_mineru()
    test_bibliographic_consistency_uses_published_year_and_aps_doi()
    test_locator_aware_page_runs_only_minimal_closed_loop_checks()
    print("ingest check regression: PASS")


if __name__ == "__main__":
    main()

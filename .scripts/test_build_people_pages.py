#!/usr/bin/env python3
"""Regression tests for collision-safe people page creation."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import build_people_pages as people
import graph_lib as gl


def test_distinct_chinese_names_get_distinct_stable_slugs() -> None:
    first = people.slugify("宁美仪")
    second = people.slugify("王泓瑞")
    assert first.startswith("person-")
    assert second.startswith("person-")
    assert first != second
    assert people.slugify("王泓瑞") == second


def test_existing_page_with_different_title_is_never_merged() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        authors = repo / "academic" / "wiki" / "authors"
        pending = repo / "cross-domain" / "people-pending.jsonl"
        authors.mkdir(parents=True)
        pending.parent.mkdir(parents=True)

        name = "王泓瑞"
        slug = people.slugify(name)
        (authors / f"{slug}.md").write_text(
            '---\ntitle: "宁美仪"\ntype: people\n---\n\n# 宁美仪\n',
            encoding="utf-8",
        )
        pending.write_text(
            json.dumps({"name": name, "path": name}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        conn = gl.connect(repo / "graph.db")
        gl.init_schema(conn)
        gl.ensure_node(conn, name, name, "entity", entity_subtype="person")
        gl.ensure_node(conn, f"academic/wiki/authors/{slug}", "宁美仪", "people")
        conn.commit()

        result = people.build_pending_people(conn=conn, repo=repo)

        assert result["skipped_conflict"] == 1
        assert result["details"][0]["conflict"] == "宁美仪"
        assert conn.execute("SELECT 1 FROM nodes WHERE path=?", (name,)).fetchone()
        aliases = conn.execute(
            "SELECT node_path FROM aliases WHERE alias=?", (name,)
        ).fetchall()
        assert aliases == []
        assert pending.read_text(encoding="utf-8").strip()
        conn.close()


def test_legacy_fallback_repair_retains_only_page_declared_aliases() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        page = repo / "academic" / "wiki" / "authors" / "person.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            '---\ntitle: "宁美仪"\ntype: people\n---\n\n# 宁美仪\n',
            encoding="utf-8",
        )
        conn = gl.connect(repo / "graph.db")
        gl.init_schema(conn)
        gl.ensure_node(conn, "academic/wiki/authors/person", "宁美仪", "people")
        gl.insert_aliases(
            conn, "academic/wiki/authors/person", ["宁美仪", "王泓瑞", "王芊然"]
        )
        conn.commit()

        result = people.repair_legacy_fallback_aliases(conn=conn, repo=repo)

        assert result["removed"] == ["王泓瑞", "王芊然"]
        assert result["retained"] == ["宁美仪"]
        aliases = [row["alias"] for row in conn.execute(
            "SELECT alias FROM aliases WHERE node_path=? ORDER BY alias",
            ("academic/wiki/authors/person",),
        )]
        assert aliases == ["宁美仪"]
        conn.close()


def test_meeting_person_page_uses_raw_source_and_rewrites_canonical_link() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        authors = repo / "academic" / "wiki" / "authors"
        meeting_page = "academic/wiki/conferences/1002-test"
        meeting_file = repo / f"{meeting_page}.md"
        raw = repo / "academic" / "raw" / "conferences" / "2026" / "1002-test.txt"
        pending = repo / "cross-domain" / "people-pending.jsonl"
        authors.mkdir(parents=True)
        meeting_file.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        pending.parent.mkdir(parents=True)
        raw.write_text("张三参加会议。\n", encoding="utf-8")
        meeting_file.write_text(
            "---\ntitle: 测试会议\ntype: conference-summary\nsources:\n"
            "  - academic/raw/conferences/2026/1002-test.txt\n"
            "source_type: speech-recognition\n---\n\n# 测试会议\n\n"
            "> 参会者：[[张三|张三]]\n",
            encoding="utf-8",
        )
        pending.write_text(
            json.dumps({"name": "张三", "path": "张三"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        conn = gl.connect(repo / "graph.db")
        gl.init_schema(conn)
        gl.ensure_node(conn, "张三", "张三", "entity", entity_subtype="person")
        gl.ensure_node(conn, meeting_page, "测试会议", "conference-summary")
        gl.ensure_node(conn, f"{meeting_page}/tasks/virtual", "虚拟任务", "entity")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?,?,?,?,?,?)",
            ("张三", "参会", meeting_page, "可追溯", f"{meeting_page}#会议导航", 1),
        )
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?,?,?,?,?,?)",
            ("张三", "负责", f"{meeting_page}/tasks/virtual", "可追溯",
             f"{meeting_page}#会议导航", 1),
        )
        conn.commit()

        result = people.build_pending_people(conn=conn, repo=repo)

        assert result["created"] == 1
        detail = result["details"][0]
        page = repo / f"{detail['page_path']}.md"
        rendered = page.read_text(encoding="utf-8")
        assert "academic/raw/conferences/2026/1002-test.txt" in rendered
        assert "source_type: speech-recognition" in rendered
        assert "confidence: low" in rendered
        assert "相关会议" in rendered
        assert "论文共同作者" not in rendered
        assert "/tasks/virtual" not in rendered
        assert f"[[authors/{people.slugify('张三')}|张三]]" in meeting_file.read_text(encoding="utf-8")
        raw_node = "academic/raw/conferences/2026/1002-test"
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate='来源' AND object=?",
            (detail["page_path"], raw_node),
        ).fetchone()
        conn.close()


def test_repair_generated_page_adds_raw_support_edge() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        page_path = "academic/wiki/authors/person-test"
        page = repo / f"{page_path}.md"
        meeting_path = "academic/wiki/conferences/1002-test"
        meeting = repo / f"{meeting_path}.md"
        raw_path = "academic/raw/conferences/2026/1002-test.txt"
        raw = repo / raw_path
        page.parent.mkdir(parents=True)
        meeting.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: 张三\ntype: people\nsources: []\n---\n\n"
            "# 张三\n\n本页为极简 people page，未对职称、履历或同名身份作独立核实。\n",
            encoding="utf-8",
        )
        meeting.write_text(
            "---\ntitle: 测试会议\ntype: conference-summary\nsources:\n"
            f"  - {raw_path}\nsource_type: speech-recognition\n---\n\n# 测试会议\n",
            encoding="utf-8",
        )
        raw.write_text("张三参加会议。\n", encoding="utf-8")

        conn = gl.connect(repo / "graph.db")
        gl.init_schema(conn)
        gl.ensure_node(conn, page_path, "张三", "people")
        gl.ensure_node(conn, meeting_path, "测试会议", "page")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?,?,?,?,?,?)",
            (page_path, "参会", meeting_path, "可追溯", f"{meeting_path}#会议导航", 1),
        )
        conn.commit()

        result = people.repair_generated_people_pages(
            conn=conn, repo=repo, page_paths=[f"{page_path}.md"],
        )

        assert result["repaired"] == 1
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate='来源' AND object=?",
            (page_path, raw_path.removesuffix(".txt")),
        ).fetchone()
        conn.close()


def main() -> None:
    test_distinct_chinese_names_get_distinct_stable_slugs()
    test_existing_page_with_different_title_is_never_merged()
    test_legacy_fallback_repair_retains_only_page_declared_aliases()
    test_meeting_person_page_uses_raw_source_and_rewrites_canonical_link()
    test_repair_generated_page_adds_raw_support_edge()
    print("build people pages tests: PASS")


if __name__ == "__main__":
    main()

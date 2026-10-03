#!/usr/bin/env python3
"""Regression tests for meeting-to-student guidance projection."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

import student_guidance_projection as projection


def _repo() -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory()
    repo = Path(temporary.name)
    students = repo / "projects" / "学生指导" / "students"
    notes = repo / "projects" / "学生指导" / "notes"
    wiki = repo / "academic" / "wiki" / "conferences"
    people = repo / "academic" / "wiki" / "authors"
    extract = repo / "temp" / "inbox-extract" / "txn"
    for directory in (students, notes, wiki, people, extract):
        directory.mkdir(parents=True, exist_ok=True)
    (students / "测试生.md").write_text(
        "# 测试生研究记录\n\n"
        "- 知识库人物页：[测试生](../../../academic/wiki/authors/cnu-test.md)\n"
        "- 更新日期：2026-09-01\n\n"
        "## 当前进展\n\n- 旧进展\n",
        encoding="utf-8",
    )
    (students / "无人物页.md").write_text(
        "# 无人物页研究记录\n\n"
        "- 知识库人物页：待建立\n"
        "- 更新日期：2026-09-02\n",
        encoding="utf-8",
    )
    (people / "cnu-test.md").write_text("# 测试生\n", encoding="utf-8")
    (notes / "status.md").write_text(
        "# 总览\n\n- 更新日期：2026-09-02\n\n"
        "| 学生 | 指导类型 | 阶段 | 最新进展 | 下一步 | 更新日期 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| [测试生](../students/测试生.md) | 科研 | 验证 | 旧进展 | 旧下一步 | 2026-09-01 |\n"
        "| [无人物页](../students/无人物页.md) | 科研 | 验证 | 旧进展 | 旧下一步 | 2026-09-02 |\n",
        encoding="utf-8",
    )
    (wiki / "0930-test.md").write_text(
        "---\ntitle: 测试会议\n---\n# 测试会议\n\n## 学生指导更新\n",
        encoding="utf-8",
    )
    return temporary, repo


def _state() -> dict:
    return {
        "transaction_id": "txn",
        "extract_dir": "temp/inbox-extract/txn",
        "meeting_id": "0930-test",
        "date": "2026-09-30",
        "wiki_path": "academic/wiki/conferences/0930-test",
        "meeting_ir_content": {
            "person_updates": [
                {"person": "cnu-test", "person_label": "测试生", "kind": "progress",
                 "text": "已跑通最小实验", "evidence_ids": ["s0001"]},
                {"person": "cnu-test", "person_label": "测试生", "kind": "next_step",
                 "text": "扩展到更长序列", "evidence_ids": ["s0002"]},
            ],
        },
    }


def test_candidate_catalog_uses_people_page_or_stable_record_key():
    temporary, repo = _repo()
    try:
        catalog = projection.discover_student_records(repo)
        by_label = {row["label"]: row for row in catalog["students"]}
        assert by_label["测试生"]["student_key"] == "cnu-test"
        assert by_label["无人物页"]["student_key"] == "student-record:无人物页"
    finally:
        temporary.cleanup()


def test_projection_updates_record_and_status_idempotently():
    temporary, repo = _repo()
    try:
        state = _state()
        projection.prepare_candidate_catalog(state, repo)
        ok, error = projection.apply_projection(state, repo)
        assert ok, error
        record = (repo / "projects/学生指导/students/测试生.md").read_text(encoding="utf-8")
        status = (repo / "projects/学生指导/notes/status.md").read_text(encoding="utf-8")
        assert record.count("student-guidance:") == 2
        assert "进展：已跑通最小实验" in record
        assert "下一步：扩展到更长序列" in record
        assert "<!-- evidence:s0001 -->" in record
        assert "| 已跑通最小实验 | 扩展到更长序列 | 2026-09-30 |" in status
        first_record = record
        first_status = status
        ok, error = projection.apply_projection(state, repo)
        assert ok, error
        assert (repo / "projects/学生指导/students/测试生.md").read_text(encoding="utf-8") == first_record
        assert (repo / "projects/学生指导/notes/status.md").read_text(encoding="utf-8") == first_status
        report = json.loads((repo / state["student_guidance_report_path"]).read_text())
        assert report["unchanged_records"] == ["projects/学生指导/students/测试生.md"]
    finally:
        temporary.cleanup()


def test_unmatched_student_is_rejected_before_writes():
    temporary, repo = _repo()
    try:
        state = _state()
        state["meeting_ir_content"]["person_updates"][0]["person"] = "unknown"
        projection.prepare_candidate_catalog(state, repo)
        record_path = repo / "projects/学生指导/students/测试生.md"
        before = record_path.read_text(encoding="utf-8")
        ok, error = projection.apply_projection(state, repo)
        assert not ok and "not an existing student candidate" in error
        assert record_path.read_text(encoding="utf-8") == before
    finally:
        temporary.cleanup()


def test_missing_status_row_rolls_back_before_record_write():
    temporary, repo = _repo()
    try:
        state = _state()
        projection.prepare_candidate_catalog(state, repo)
        status_path = repo / "projects/学生指导/notes/status.md"
        status_path.write_text(status_path.read_text().replace(
            "| [测试生](../students/测试生.md) | 科研 | 验证 | 旧进展 | 旧下一步 | 2026-09-01 |\n", ""
        ), encoding="utf-8")
        record_path = repo / "projects/学生指导/students/测试生.md"
        before = record_path.read_text(encoding="utf-8")
        ok, error = projection.apply_projection(state, repo)
        assert not ok and "总览缺少学生条目" in error
        assert record_path.read_text(encoding="utf-8") == before
    finally:
        temporary.cleanup()


def main():
    test_candidate_catalog_uses_people_page_or_stable_record_key()
    test_projection_updates_record_and_status_idempotently()
    test_unmatched_student_is_rejected_before_writes()
    test_missing_status_row_rolls_back_before_record_write()
    print("student guidance projection tests: PASS")


if __name__ == "__main__":
    main()

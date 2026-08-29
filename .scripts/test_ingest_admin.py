#!/usr/bin/env python3
"""ingest_admin.py 代码驱动行政文档摄入回归测试。"""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("ingest_admin.py")
spec = importlib.util.spec_from_file_location("ingest_admin", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_generate_admin_id():
    """admin-id 格式: YYYYMMDD-title-slug。"""
    id1 = module.generate_admin_id("20260701-某章程.docx", "管理章程")
    assert id1.startswith("20260701-"), f"unexpected id: {id1}"
    assert "管理章程" in id1, f"unexpected id: {id1}"


def test_ensure_unique_admin_id():
    """冲突消歧: 加 -2, -3..."""
    subdir = "references"
    base = module.generate_admin_id("test.docx", "测试文档")
    unique = module.ensure_unique_admin_id(base, subdir)
    assert unique == base, f"first should be unique: {unique}"


def test_get_subdir():
    """页面类型 → 子目录映射。"""
    assert module.get_subdir("policy") == "policies"
    assert module.get_subdir("procedure") == "procedures"
    assert module.get_subdir("decision") == "decisions"
    assert module.get_subdir("unknown") == "references"


def test_extract_admin_date():
    """日期提取: 文件名优先, 内容兜底。"""
    d1 = module.extract_admin_date("20260701-某章程.docx")
    assert d1 == "2026-07-01", f"unexpected date: {d1}"
    d2 = module.extract_admin_date("test.docx", "2026年7月1日发布")
    assert d2 == "2026-07-01", f"unexpected date: {d2}"


def test_build_admin_wiki_prompt():
    """wiki prompt 含关键要素。"""
    prompt = module.build_admin_wiki_prompt("文档内容", "test-id", "2026-07-01")
    assert "<<<WIKI>>>" in prompt
    assert "Navigation" in prompt
    assert "Content" in prompt
    assert "frontmatter" in prompt


def test_build_admin_slots_prompt():
    """slots prompt 用三元组格式, 不含行政主题/行政关系。"""
    prompt = module.build_admin_slots_prompt("wiki content")
    assert "<<<SLOTS>>>" in prompt
    assert "三元组" in prompt
    assert "行政主题" not in prompt
    assert "行政关系" not in prompt
    assert "涉及" in prompt
    assert "形成决策" in prompt


def test_validate_wiki_structure():
    """wiki 校验: 缺字段报错。"""
    state = {"wiki_content": "---\ntitle: test\ntype: policy\nsources:\n  - admin/raw/policies/test.docx\nsource_type: official-doc\ndate: 2026-07-01\n---\n## Navigation\n\n测试。\n## Content\n\n内容。\n"}
    errors = module.step_validate_wiki(state)
    assert not errors, f"should pass: {errors}"

    state2 = {"wiki_content": "no frontmatter"}
    errors2 = module.step_validate_wiki(state2)
    assert errors2, "should fail for missing frontmatter"


def test_parse_semantic_text_admin_triples():
    """parse_semantic_text admin 分支: 三元组格式, keywords 从三元组提取。"""
    SCRIPT_GI = Path(__file__).with_name("graph_ingest.py")
    spec_gi = importlib.util.spec_from_file_location("graph_ingest_test2", SCRIPT_GI)
    gi = importlib.util.module_from_spec(spec_gi)
    assert spec_gi.loader is not None
    spec_gi.loader.exec_module(gi)

    REPO = Path(__file__).resolve().parent.parent
    wiki_rel = "admin/wiki/policies/test-admin-parse"
    wiki_file = REPO / (wiki_rel + ".md")
    wiki_file.parent.mkdir(parents=True, exist_ok=True)
    wiki_file.write_text(
        "---\n"
        'title: "测试政策"\n'
        "type: policy\n"
        "sources:\n  - admin/raw/policies/test.docx\n"
        "source_type: official-doc\n"
        "date: 2026-07-01\n"
        "---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
        encoding="utf-8",
    )
    try:
        sem_text = (
            "三元组:\n"
            "本文件 | 涉及 | 人才培养talent cultivation\n"
            "本文件 | 形成决策 | 专业建设方案\n"
            "本文件 | 依据 | 高等教育法\n"
            "本文件 | 发布者 | 物理系\n"
        )
        triples, keywords, _, _, _, _ = gi.parse_semantic_text(sem_text, wiki_rel)
        assert len(triples) == 4, f"expected 4 triples, got {len(triples)}: {triples}"
        assert "人才培养talent cultivation" in keywords, f"keywords: {keywords}"
        assert "专业建设方案" in keywords, f"keywords: {keywords}"
        assert "高等教育法" not in keywords, f"依据 should not be keyword: {keywords}"
        assert "物理系" not in keywords, f"发布者 should not be keyword: {keywords}"
    finally:
        if wiki_file.exists():
            wiki_file.unlink()


def main():
    test_generate_admin_id()
    test_ensure_unique_admin_id()
    test_get_subdir()
    test_extract_admin_date()
    test_build_admin_wiki_prompt()
    test_build_admin_slots_prompt()
    test_validate_wiki_structure()
    test_parse_semantic_text_admin_triples()
    print("ingest admin regression: PASS")


if __name__ == "__main__":
    main()

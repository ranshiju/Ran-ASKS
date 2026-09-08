#!/usr/bin/env python3
"""ingest_document.py 通用文档摄入回归测试。"""
import importlib.util
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).with_name("ingest_document.py")
spec = importlib.util.spec_from_file_location("ingest_document", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_domain_config_keys():
    """四个域都有完整配置。"""
    for domain in ("academic", "admin", "teaching", "business"):
        cfg = module.DOMAIN_CONFIG[domain]
        assert "page_types" in cfg, f"{domain} missing page_types"
        if domain == "academic":
            assert "raw_type_to_subdir" in cfg
            assert "wiki_type_to_subdir" in cfg
        else:
            assert "type_to_subdir" in cfg, f"{domain} missing type_to_subdir"
        assert "kw_predicates" in cfg, f"{domain} missing kw_predicates"
        assert "nav_predicates" in cfg, f"{domain} missing nav_predicates"
        assert "subject_pronoun" in cfg, f"{domain} missing subject_pronoun"
        assert "domain_name" in cfg, f"{domain} missing domain_name"
        # kw_predicates must be subset of nav_predicates
        assert cfg["kw_predicates"] <= cfg["nav_predicates"], \
            f"{domain} kw_predicates not subset of nav_predicates"


def test_get_subdir_admin():
    """admin 域子目录映射。"""
    assert module.get_subdir("policy", "admin") == "policies"
    assert module.get_subdir("decision", "admin") == "decisions"
    assert module.get_subdir("unknown", "admin") == "references"


def test_get_subdir_teaching():
    """teaching 域子目录映射。"""
    assert module.get_subdir("course", "teaching") == "courses"
    assert module.get_subdir("lecture", "teaching") == "lectures"
    assert module.get_subdir("assessment", "teaching") == "assessments"
    assert module.get_subdir("unknown", "teaching") == "references"  # fallback


def test_get_subdir_business():
    """business 域子目录映射。"""
    assert module.get_subdir("plan", "business") == "plans"
    assert module.get_subdir("competitor", "business") == "competitors"
    assert module.get_subdir("contract", "business") == "contracts"
    assert module.get_subdir("unknown", "business") == "references"  # fallback


def test_academic_subdirs_are_explicit_and_separate():
    assert module.get_raw_subdir("editorial", "academic") == "works/editorials"
    assert module.get_wiki_subdir("editorial", "academic") == "editorials"
    assert module.get_raw_subdir("academic-reference", "academic") == "reference-documents"
    assert module.get_wiki_subdir("academic-reference", "academic") == "references"
    assert module.get_raw_subdir("conference-summary", "academic") == "conferences"
    assert module.get_wiki_subdir("conference-summary", "academic") == "conferences"
    assert module.get_raw_subdir("unknown", "academic") is None
    assert module.get_wiki_subdir("unknown", "academic") is None


def test_academic_prompt_is_locked_to_explicit_type():
    prompt = module.build_doc_wiki_prompt(
        "专题导言", "test-id", "", "academic", document_type="editorial")
    assert "页面类型（editorial）" in prompt
    assert "academic-reference）" not in prompt

    conference_prompt = module.build_doc_wiki_prompt(
        "研讨会信息整理", "test-conference", "", "academic",
        document_type="conference-summary")
    assert "页面类型（conference-summary）" in conference_prompt
    combined_prompt = module.build_doc_wiki_slots_prompt(
        "研讨会信息整理", "test-conference", "", "academic",
        document_type="conference-summary")
    assert '主体用"本会议"' in combined_prompt
    separate_prompt = module.build_doc_slots_prompt(
        conference_prompt, "academic", document_type="conference-summary")
    assert '主体用"本会议"' in separate_prompt
    assert '主体用"本文档"' not in combined_prompt
    assert "<<<META>>>" not in conference_prompt
    assert "<<<META>>>" not in combined_prompt

    state = {
        "transaction_id": "test-conference-prompt-contract",
        "admin_id": "20260905-test-conference",
        "subproject": "academic",
        "document_type": "conference-summary",
        "source_kind": "ordinary",
        "date_str": "2026-09-05",
    }
    task = module.prepare_document_agent_task(
        state, module.REPO / "AGENTS.md",
        module.REPO / "temp" / "test-conference-prompt-contract.txt",
    )
    assert task["protocol"]["order"] == ["WIKI", "SLOTS"]
    assert "meta" not in task["protocol"]["delimiters"]


def test_academic_agent_wiki_maps_raw_and_wiki_separately():
    import shutil
    extract_dir = module.REPO / "temp" / "inbox-extract" / "test-academic-editorial"
    extract_dir.mkdir(parents=True, exist_ok=True)
    wiki_content = (
        "---\ntitle: 专题导言\ntype: editorial\nsources:\n  - memory://placeholder\n"
        "source_type: official-doc\ndate: null\ndate_status: unknown\nstatus: final\n"
        "created: 2026-09-01\nupdated: 2026-09-01\n---\n"
        "## Navigation\n\n导航。\n## Content\n\n内容。\n"
    )
    (extract_dir / "wiki.md").write_text(wiki_content, encoding="utf-8")
    state = {
        "extract_dir": str(extract_dir.relative_to(module.REPO)),
        "admin_id": "undated-editorial",
        "date_str": "",
        "subproject": "academic",
        "document_type": "editorial",
        "source_filename": "editorial.pdf",
        "locator_source_filename": "editorial.md",
        "_awaiting_agent_wiki": True,
    }
    try:
        ok, message = module.step_write_wiki(state)
        assert ok, message
        assert state["raw_dir"] == "academic/raw/works/editorials"
        assert state["wiki_path"] == "academic/wiki/editorials/undated-editorial"
        assert "academic/raw/works/editorials/editorial.md" in state["wiki_content"]
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_academic_missing_type_stops_before_transaction_and_raw_write():
    inbox_file = module.REPO / "inbox" / "test-academic-ambiguous.md"
    raw_target = module.REPO / "academic" / "raw" / "reference-documents" / inbox_file.name
    state_dir = module.REPO / "temp" / "inbox-state"
    before_states = set(state_dir.glob("*.json")) if state_dir.exists() else set()
    assert not raw_target.exists()
    inbox_file.write_text("# 学术背景资料\n\n没有可确定的文档类型。\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--file", str(inbox_file.relative_to(module.REPO)),
             "--subproject", "academic"],
            cwd=module.REPO, capture_output=True, text=True, check=True,
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "classification_required"
        after_states = set(state_dir.glob("*.json")) if state_dir.exists() else set()
        assert after_states == before_states
        assert not raw_target.exists()
    finally:
        inbox_file.unlink(missing_ok=True)


def test_build_doc_wiki_prompt_admin():
    """admin wiki prompt 含关键要素。"""
    prompt = module.build_doc_wiki_prompt("文档内容", "test-id", "2026-07-01", "admin")
    assert "<<<WIKI>>>" in prompt
    assert "Navigation" in prompt
    assert "Content" in prompt
    assert "行政" in prompt
    assert "department" in prompt  # admin extra_frontmatter
    assert "逐字出现完整部门名称" in prompt
    assert "effective_from" in prompt  # temporal policy/procedure/decision
    assert "effective_to" in prompt


def test_build_doc_wiki_prompt_teaching():
    """teaching wiki prompt 含教学域要素。"""
    prompt = module.build_doc_wiki_prompt("文档内容", "test-id", "2026-07-01", "teaching")
    assert "<<<WIKI>>>" in prompt
    assert "教学" in prompt
    assert "course" in prompt  # teaching page type
    assert "semester" in prompt  # teaching extra_frontmatter
    assert "effective_from" in prompt  # temporal course


def test_build_doc_wiki_prompt_business():
    """business wiki prompt 含商业域要素。"""
    prompt = module.build_doc_wiki_prompt("文档内容", "test-id", "2026-07-01", "business")
    assert "<<<WIKI>>>" in prompt
    assert "商业" in prompt
    assert "competitor" in prompt  # business page type
    assert "domain" in prompt  # business extra_frontmatter


def test_build_doc_slots_prompt_admin():
    """admin slots prompt 用三元组格式。"""
    prompt = module.build_doc_slots_prompt("wiki content", "admin")
    assert "<<<SLOTS>>>" in prompt
    assert "三元组" in prompt
    assert "本文件" in prompt  # admin pronoun
    assert "涉及" in prompt
    assert "形成决策" in prompt
    assert "讲话、主讲、发言不得推断为负责人" in prompt
    assert "本文件 | 负责人 | 张明远" not in prompt


def test_build_doc_slots_prompt_teaching():
    """teaching slots prompt 用本文档代词。"""
    prompt = module.build_doc_slots_prompt("wiki content", "teaching")
    assert "<<<SLOTS>>>" in prompt
    assert "三元组" in prompt
    assert "本文档" in prompt  # teaching pronoun
    assert "涵盖" in prompt  # teaching kw predicate
    assert "考核" in prompt  # teaching kw predicate
    assert "前置" in prompt  # teaching nav predicate
    assert "开课单位" in prompt  # teaching nav predicate


def test_build_doc_slots_prompt_business():
    """business slots prompt 用本文件代词。"""
    prompt = module.build_doc_slots_prompt("wiki content", "business")
    assert "<<<SLOTS>>>" in prompt
    assert "三元组" in prompt
    assert "本文件" in prompt  # business pronoun
    assert "分析" in prompt  # business kw predicate
    assert "规划" in prompt  # business kw predicate
    assert "竞争" in prompt  # business nav predicate
    assert "合作" in prompt  # business nav predicate


def test_validate_wiki_teaching():
    """teaching wiki 校验: 合法 type 通过。"""
    state = {
        "subproject": "teaching",
        "wiki_content": "---\ntitle: test\ntype: course\nsources:\n  - teaching/raw/courses/test.docx\nsource_type: official-doc\nconfidence: high\ndate: 2026-07-01\n---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
    }
    errors = module.step_validate_wiki(state)
    assert not errors, f"should pass: {errors}"


def test_validate_wiki_business():
    """business wiki 校验: 合法 type 通过。"""
    state = {
        "subproject": "business",
        "wiki_content": "---\ntitle: test\ntype: plan\nsources:\n  - business/raw/plans/test.docx\nsource_type: official-doc\nconfidence: high\ndate: 2026-07-01\n---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
    }
    errors = module.step_validate_wiki(state)
    assert not errors, f"should pass: {errors}"


def test_validate_wiki_policy_effective_dates():
    """policy 页 effective_from/effective_to 合法时通过，非法或倒置时 ERROR。"""
    state = {
        "subproject": "admin",
        "wiki_content": "---\ntitle: test\ntype: policy\nsources:\n  - admin/raw/policies/test.md\nsource_type: official-doc\nconfidence: high\ndate: 2026-07-01\neffective_from: 2026-07-01\n---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
    }
    assert not module.step_validate_wiki(state)
    state["wiki_content"] = state["wiki_content"].replace(
        "effective_from: 2026-07-01", "effective_from: 2026-07")
    assert module.step_validate_wiki(state)
    state["wiki_content"] = state["wiki_content"].replace(
        "effective_from: 2026-07", "effective_from: 2026-08-01\neffective_to: 2026-07-01")
    assert module.step_validate_wiki(state)


def test_validate_wiki_teaching_bad_type():
    """teaching wiki 校验: 非法 type 报错。"""
    state = {
        "subproject": "teaching",
        "wiki_content": "---\ntitle: test\ntype: policy\nsources:\n  - teaching/raw/courses/test.docx\nsource_type: official-doc\nconfidence: high\ndate: 2026-07-01\n---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
    }
    errors = module.step_validate_wiki(state)
    assert errors, "should fail for bad type"


def test_parse_semantic_text_teaching():
    """parse_semantic_text teaching 分支: 三元组格式, keywords 从三元组提取。"""
    SCRIPT_GI = Path(__file__).with_name("graph_ingest.py")
    spec_gi = importlib.util.spec_from_file_location("graph_ingest_test_teaching", SCRIPT_GI)
    gi = importlib.util.module_from_spec(spec_gi)
    assert spec_gi.loader is not None
    spec_gi.loader.exec_module(gi)

    REPO = Path(__file__).resolve().parent.parent
    wiki_rel = "teaching/wiki/courses/test-doc-parse"
    wiki_file = REPO / (wiki_rel + ".md")
    wiki_file.parent.mkdir(parents=True, exist_ok=True)
    wiki_file.write_text(
        "---\n"
        'title: "测试课程"\n'
        "type: course\n"
        "sources:\n  - teaching/raw/courses/test.docx\n"
        "source_type: official-doc\n"
        "date: 2026-07-01\n"
        "---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
        encoding="utf-8",
    )
    try:
        sem_text = (
            "三元组:\n"
            "本文档 | 涵盖 | 量子计算quantum computing\n"
            "本文档 | 考核 | 期末考试\n"
            "本文档 | 前置 | 线性代数\n"
            "本文档 | 开课单位 | 物理系\n"
        )
        triples, keywords, _, _, _, _ = gi.parse_semantic_text(sem_text, wiki_rel)
        assert len(triples) == 4, f"expected 4 triples, got {len(triples)}: {triples}"
        assert "量子计算quantum computing" in keywords, f"keywords: {keywords}"
        assert "期末考试" in keywords, f"keywords: {keywords}"
        assert "线性代数" not in keywords, f"前置 should not be keyword: {keywords}"
        assert "物理系" not in keywords, f"开课单位 should not be keyword: {keywords}"
    finally:
        if wiki_file.exists():
            wiki_file.unlink()


def test_parse_semantic_text_business():
    """parse_semantic_text business 分支: 三元组格式, keywords 从三元组提取。"""
    SCRIPT_GI = Path(__file__).with_name("graph_ingest.py")
    spec_gi = importlib.util.spec_from_file_location("graph_ingest_test_business", SCRIPT_GI)
    gi = importlib.util.module_from_spec(spec_gi)
    assert spec_gi.loader is not None
    spec_gi.loader.exec_module(gi)

    REPO = Path(__file__).resolve().parent.parent
    wiki_rel = "business/wiki/plans/test-doc-parse"
    wiki_file = REPO / (wiki_rel + ".md")
    wiki_file.parent.mkdir(parents=True, exist_ok=True)
    wiki_file.write_text(
        "---\n"
        'title: "测试计划"\n'
        "type: plan\n"
        "sources:\n  - business/raw/plans/test.docx\n"
        "source_type: official-doc\n"
        "date: 2026-07-01\n"
        "---\n## Navigation\n\n测试。\n## Content\n\n内容。\n",
        encoding="utf-8",
    )
    try:
        sem_text = (
            "三元组:\n"
            "本文件 | 分析 | 市场趋势market trend\n"
            "本文件 | 规划 | 产品路线图\n"
            "本文件 | 竞争 | 竞品A\n"
            "本文件 | 合作 | 合作伙伴B\n"
        )
        triples, keywords, _, _, _, _ = gi.parse_semantic_text(sem_text, wiki_rel)
        assert len(triples) == 4, f"expected 4 triples, got {len(triples)}: {triples}"
        assert "市场趋势market trend" in keywords, f"keywords: {keywords}"
        assert "产品路线图" in keywords, f"keywords: {keywords}"
        assert "竞品A" not in keywords, f"竞争 should not be keyword: {keywords}"
        assert "合作伙伴B" not in keywords, f"合作 should not be keyword: {keywords}"
    finally:
        if wiki_file.exists():
            wiki_file.unlink()


def test_admin_wrapper_compat():
    """ingest_admin.py 薄包装重导出旧接口名。"""
    SCRIPT_A = Path(__file__).with_name("ingest_admin.py")
    spec_a = importlib.util.spec_from_file_location("ingest_admin_compat", SCRIPT_A)
    mod_a = importlib.util.module_from_spec(spec_a)
    assert spec_a.loader is not None
    spec_a.loader.exec_module(mod_a)
    # 旧函数名可用
    prompt = mod_a.build_admin_wiki_prompt("内容", "id", "2026-07-01")
    assert "<<<WIKI>>>" in prompt
    prompt2 = mod_a.build_admin_slots_prompt("wiki")
    assert "<<<SLOTS>>>" in prompt2
    assert mod_a.get_subdir("policy") == "policies"



def test_agent_mode_wiki_roundtrip():
    """agent 模式往返: _awaiting_agent_wiki + wiki.md 已写入 -> 跳过 LLM 读取文件。

    回填块(59df973)在 agent 分支也 fall-through 执行:据 raw_dir/source_filename
    归一化 sources 路径(消除 agent 写入的占位/猜测)。故 state 须含 source_filename,
    且断言须对应回填后的引用化 sources 行。
    """
    import shutil
    extract_dir = module.REPO / "temp" / "inbox-extract" / "test-agent-wiki"
    extract_dir.mkdir(parents=True, exist_ok=True)
    wiki_content = (
        "---\n"
        'title: "测试"\n'
        "type: policy\n"
        "sources:\n  - memory://placeholder\n"
        "source_type: official-doc\n"
        "date: 2026-07-01\n"
        "---\n## Navigation\n\n测试。 <RAW#L13>\n## Content\n\n内容。 <RAW#L136>\n"
    )
    (extract_dir / "wiki.md").write_text(wiki_content, encoding="utf-8")
    doc_text = "\n".join(f"第 {line} 行依据。" for line in range(1, 137)) + "\n"
    (extract_dir / "doc.md").write_text(doc_text, encoding="utf-8")
    (extract_dir / "test.md").write_text(doc_text, encoding="utf-8")
    state = {
        "extract_dir": "temp/inbox-extract/test-agent-wiki",
        "admin_id": "20260701-test",
        "date_str": "2026-07-01",
        "subproject": "admin",
        "source_filename": "test.docx",
        "locator_source_filename": "test.md",
        "_awaiting_agent_wiki": True,
    }
    try:
        success, msg = module.step_write_wiki(state)
        assert success, f"should succeed: {msg}"
        fm_text = re.match(r"^---\n(.*?)\n---", state["wiki_content"], re.S).group(1)
        fm = module.yaml.safe_load(fm_text)
        assert fm["sources"] == ["admin/raw/policies/test.md"]
        assert fm["date"] == "2026-07-01"
        assert fm["created"] == fm["updated"] == state["ingested_on"]
        assert "[^r13]" in state["wiki_content"]
        assert "[^r136]" in state["wiki_content"]
        assert "[^r13]6>" not in state["wiki_content"]
        assert module.step_validate_wiki(state) == []
        assert "_awaiting_agent_wiki" not in state
        assert state["wiki_path"].startswith("admin/wiki/")
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_agent_mode_slots_roundtrip():
    """agent 模式往返: _awaiting_agent_slots + semantic.txt 已写入 -> 跳过 LLM 读取文件。"""
    import shutil
    extract_dir = module.REPO / "temp" / "inbox-extract" / "test-agent-slots"
    extract_dir.mkdir(parents=True, exist_ok=True)
    slots_content = "三元组:\n本文件 | 涉及 | 人才培养talent cultivation\n"
    (extract_dir / "semantic.txt").write_text(slots_content, encoding="utf-8")
    state = {
        "extract_dir": "temp/inbox-extract/test-agent-slots",
        "wiki_content": "---\ntitle: test\ntype: policy\n---\n## Navigation\n\n## Content\n",
        "subproject": "admin",
        "_awaiting_agent_slots": True,
    }
    try:
        success, msg = module.step_write_slots(state)
        assert success, f"should succeed: {msg}"
        assert state["slots_content"] == slots_content
        assert "_awaiting_agent_slots" not in state
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_short_api_document_combines_wiki_and_slots_in_one_call():
    import shutil
    extract_dir = module.REPO / "temp" / "inbox-extract" / "test-api-combined-document"
    extract_dir.mkdir(parents=True, exist_ok=True)
    doc_text = "\n".join(f"第 {line} 行依据。" for line in range(1, 137)) + "\n"
    (extract_dir / "doc.md").write_text(doc_text, encoding="utf-8")
    (extract_dir / "test-policy.md").write_text(doc_text, encoding="utf-8")
    output = (
        "<<<META>>>\ndoc_date: 2026-09-01\ntitle: 测试政策\ndoc_type: document\n<<</META>>>\n"
        "<<<WIKI>>>\n---\ntitle: 测试政策\ntype: policy\nstatus: confirmed\n---\n"
        "# 测试政策\n\n## Navigation\n\n本政策支持人才培养。 <RAW#L13>\n\n"
        "## Content\n\n### 支持事项\n\n明确支持人才培养。 <RAW#L136>\n"
        "<<<SLOTS>>>\n三元组:\n本文件 | 涉及 | 人才培养\n"
    )
    calls = []

    def fake_call(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return {"ok": True, "status": "ok", "text": output}

    state = {
        "extract_dir": str(extract_dir.relative_to(module.REPO)),
        "date_str": "2026-09-01",
        "subproject": "admin",
        "source_filename": "test-policy.docx",
        "locator_source_filename": "test-policy.md",
    }
    original_mode, original_call = module.ingest_mode, module.call_text
    module.ingest_mode = lambda: "api"
    module.call_text = fake_call
    try:
        success, message = module.step_write_wiki(state)
        validation_errors = module.step_validate_wiki(state) if success else []
    finally:
        module.ingest_mode, module.call_text = original_mode, original_call
        shutil.rmtree(extract_dir, ignore_errors=True)

    assert success, message
    assert len(calls) == 1
    assert "<<<SLOTS>>>" in calls[0][0]
    reasoning_context = calls[0][1]["reasoning_context"]
    assert reasoning_context["document_kind"] == "ordinary"
    assert reasoning_context["retry"] == 0
    assert reasoning_context["input_chars"] > 0
    assert state["slots_content"].startswith("三元组:")
    assert state["semantic_worker"] == "combined-api"
    assert "admin/raw/policies/test-policy.md" in state["wiki_content"]
    assert "[^r13]" in state["wiki_content"]
    assert "[^r136]" in state["wiki_content"]
    assert "[^r13]6>" not in state["wiki_content"]
    assert validation_errors == []


def test_agent_required_output_has_write_to():
    """agent_required 状态包含 write_to 字段。"""
    state = {
        "transaction_id": "test-txn",
        "status": "agent_required",
        "agent_prompt": "test prompt",
        "agent_write_to": "temp/inbox-extract/test/wiki.md",
    }
    assert "agent_write_to" in state
    assert state["agent_write_to"].endswith("wiki.md")


def test_unknown_source_date_is_not_replaced_with_ingestion_date():
    assert module.extract_admin_date("undated.pdf", "正文没有来源日期。") == ""
    assert module.generate_admin_id("undated.pdf", "专题导言", "") == "undated-专题导言"
    wiki = (
        "---\ntitle: 专题导言\ntype: reference\n"
        "sources:\n  - admin/raw/references/undated.md\n"
        "source_type: official-doc\nconfidence: high\ndate: 2026-09-01\nstatus: final\n"
        "created: 2026-09-01\nupdated: 2026-09-01\n---\n"
        "## Navigation\n\n导航。\n## Content\n\n正文。\n"
    )
    patched = module.apply_source_date_frontmatter(wiki, "")
    assert "date: null\ndate_status: unknown" in patched
    assert "date: 2026-09-01" not in patched
    errors = module.step_validate_wiki({"wiki_content": patched, "subproject": "admin"})
    assert not errors, errors


def test_source_date_accepts_filename_formats_and_validates_calendar():
    for name in (
        "2026.09.06 第三届量子智能计算研讨会新闻稿 v2.docx",
        "2026.9.6 新闻稿.docx",
        "2026-09-06 新闻稿.docx",
        "2026_9_6 新闻稿.docx",
        "20260906 新闻稿.docx",
        "2026年9月6日 新闻稿.docx",
    ):
        assert module.extract_admin_date(name, "", "conference-summary") == "2026-09-06", name
        assert module.generate_admin_id(name, "新闻稿").startswith("20260906-"), name
    for name in (
        "2026.02.29 新闻稿.docx",
        "2026-13-06 新闻稿.docx",
        "20260931 新闻稿.docx",
        "编号1202609067.docx",
        "2026年9月 新闻稿.docx",
    ):
        assert module.extract_admin_date(name, "", "conference-summary") == "", name
        assert module.generate_admin_id(name, "新闻稿").startswith("undated-"), name
    assert module.extract_admin_date("2024.02.29 新闻稿.docx") == "2024-02-29"


def test_source_date_uses_bounded_nearest_directory_after_explicit_evidence():
    source = "inbox/2026.09.01/2026年9月6日/材料/新闻稿.docx"
    event_text = "会议于2026年8月23日举办。"
    assert module.extract_admin_date(source, event_text, "conference-summary") == "2026-09-06"
    assert module.extract_admin_date(
        str(module.REPO / source), event_text, "conference-summary"
    ) == "2026-09-06"
    assert module.extract_admin_date(
        "inbox/2026.09.06/新闻稿.docx", "", "conference-summary"
    ) == "2026-09-06"
    assert module.extract_admin_date(
        source, "整理日期：2026年9月7日", "conference-summary"
    ) == "2026-09-07"
    assert module.extract_admin_date(
        "inbox/2026.09.01/2026.09.06 新闻稿.docx",
        "整理日期：2026年9月7日", "conference-summary",
    ) == "2026-09-06"
    for outside in (
        "/tmp/2026.09.06/新闻稿.docx",
        "temp/2026.09.06/新闻稿.docx",
        "inbox/../temp/2026.09.06/新闻稿.docx",
    ):
        assert module.extract_admin_date(outside, event_text, "conference-summary") == ""
    assert module.extract_admin_date(
        "inbox/2026.02.29/新闻稿.docx", event_text, "conference-summary"
    ) == ""
    assert module.extract_admin_date(
        source, "整理日期：2026年2月29日", "conference-summary"
    ) == "2026-09-06"
    assert module.generate_admin_id("20260901 新闻稿.docx", "新闻稿", "2026-09-06").startswith("20260906-")


def test_preprocess_preserves_source_path_date_for_both_backends():
    import tempfile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(prefix="2025-01-01-") as temporary:
        repo = Path(temporary)
        source = repo / "inbox/2026.09.06/材料/新闻稿.md"
        source.parent.mkdir(parents=True)
        source.write_text("会议于2026年8月23日举办。", encoding="utf-8")
        with patch.object(module, "REPO", repo):
            for backend in ("agent", "api"):
                with patch.dict("os.environ", {"INGEST_BACKEND": backend}):
                    assert module.ingest_mode() == backend
                    state = {
                        "source": str(source.relative_to(repo)),
                        "source_filename": source.name,
                        "extract_dir": f"temp/extract-{backend}",
                        "document_type": "conference-summary",
                    }
                    ok, message = module.step_preprocess(state)
                    assert ok, message
                    assert state["date_str"] == "2026-09-06"
                    assert state["source"] == "inbox/2026.09.06/材料/新闻稿.md"
                    context_name = state["source_context_filename"]
                    context_path = repo / state["extract_dir"] / context_name
                    context = json.loads(context_path.read_text(encoding="utf-8"))
                    assert context == {
                        "schema": "document-source-context-v1",
                        "source": "inbox/2026.09.06/材料/新闻稿.md",
                        "filename": "新闻稿.md",
                        "directories": ["2026.09.06", "材料"],
                    }
                    assert module._manifest_raw_files(state) == ["新闻稿.md", context_name]
                    assert module.generate_admin_id(source.name, "新闻稿", state["date_str"]).startswith("20260906-")
            assert module.extract_admin_date("inbox/新闻稿.md", "", "conference-summary") == ""


def test_unknown_date_is_compiled_when_model_omits_field():
    draft = (
        "---\ntitle: 未标日期的记录\ntype: conference-summary\nstatus: completed\n---\n"
        "\n## Navigation\n\n导航。\n\n## Content\n\n正文。\n"
    )
    normalized, repairs = module.normalize_document_wiki(
        draft, correct_sources="academic/raw/conferences/test.md",
        source_date="", doc_text="正文。", created_at="2026-09-07",
    )
    frontmatter = module.yaml.safe_load(re.match(r"^---\n(.*?)\n---", normalized, re.S).group(1))
    assert "date" in frontmatter and frontmatter["date"] is None
    assert frontmatter["date_status"] == "unknown"
    assert "date" in repairs
    errors = module.step_validate_wiki({
        "wiki_content": normalized, "subproject": "academic",
        "document_type": "conference-summary",
    })
    assert not [error for error in errors if "date" in error], errors
    repeated, _repairs = module.normalize_document_wiki(
        normalized, correct_sources="academic/raw/conferences/test.md",
        source_date="", doc_text="正文。", created_at="2026-09-07",
    )
    assert repeated == normalized


def test_labeled_source_dates_reject_truncated_digits():
    for document_type in ("conference-summary", "academic-reference"):
        for text in (
            "发布日期：2026-09-067", "发布日期：2026-09-06123",
            "发布日期：12026-09-06", "发布日期：2026-091-06",
            "发布日期：2026/09/067", "发布日期：2026.09.067",
            "发布日期：12026年9月6日", "发布日期：2026年9月6日7",
            "发布日期：2026-02-29",
        ):
            assert module.extract_admin_date("新闻稿.docx", text, document_type) == "", text
        for text in (
            "发布日期：2026-09-06。", "发布日期：2026/9/6",
            "发布日期：2026.09.06", "发布日期：2026年9月6日",
            "2026-09-06 发布", "发布日期：2026-09-067\n更新日期：2026-09-07",
        ):
            expected = "2026-09-07" if "更新" in text else "2026-09-06"
            assert module.extract_admin_date("新闻稿.docx", text, document_type) == expected, text


def test_document_dedup_requires_content_identity_including_nested_raw():
    import tempfile
    from unittest.mock import patch
    import graph_lib

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        source = repo / "inbox/2026.09.07/新闻稿[终稿].docx"
        stored = repo / "academic/raw/conferences/新闻稿[终稿].docx"
        source.parent.mkdir(parents=True)
        stored.parent.mkdir(parents=True)
        source.write_bytes(b"new-event")
        stored.write_bytes(b"old-event")
        state = {
            "source": str(source.relative_to(repo)), "source_filename": source.name,
            "subproject": "academic", "document_type": "conference-summary",
            "dedup_result": [{"path": "stale-result"}],
        }
        with patch.object(module, "REPO", repo), patch.object(
            graph_lib, "connect", side_effect=AssertionError("title is not identity"),
        ):
            assert module.step_dedup_check(state) == (False, "")
            assert "dedup_result" not in state
            stored.write_bytes(source.read_bytes())
            assert module.step_dedup_check(state)[0] is True
            nested = stored.parent / "20260907-新闻稿" / stored.name
            nested.parent.mkdir()
            stored.rename(nested)
            assert module.step_dedup_check(state)[0] is True
            assert state["dedup_result"][0]["path"] == str(nested.relative_to(repo))
            assert state["dedup_result"][0]["binary_sha256"] == module.sha256_file(source)


def test_document_dedup_revalidates_indexed_raw_and_rejects_outside_paths():
    import tempfile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        source = repo / "inbox/renamed.docx"
        stored = repo / "academic/raw/conferences/original.docx"
        source.parent.mkdir(parents=True)
        stored.parent.mkdir(parents=True)
        source.write_bytes(b"new-event")
        stored.write_bytes(source.read_bytes())
        module.sf.register_source(
            stored, db_path=repo / "cross-domain/source-fingerprints.db", repo=repo,
        )
        state = {
            "source": str(source.relative_to(repo)), "source_filename": source.name,
            "subproject": "academic", "document_type": "conference-summary",
        }
        with patch.object(module, "REPO", repo):
            with patch.object(Path, "rglob", side_effect=AssertionError("verified index hit must not scan Raw")):
                assert module.step_dedup_check(state)[0] is True
            stored.write_bytes(b"old-event")
            assert module.step_dedup_check(state) == (False, "")
            with patch.object(module.sf, "lookup_exact", return_value={"raw_path": state["source"]}):
                assert module.step_dedup_check(state) == (False, "")


def test_raw_allocation_checks_entire_bundle_and_reuses_reserved_path():
    import tempfile
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary)
        base_dir = "academic/raw/conferences"
        base = repo / base_dir
        base.mkdir(parents=True)
        names = ["news.docx", "news.md", "news.docx.source.json"]
        with patch.object(module, "REPO", repo):
            for name in names:
                collision = base / name
                collision.write_bytes(b"existing")
                state = {
                    "admin_id": "20260907-news", "source_filename": names[0],
                    "locator_source_filename": names[1], "source_context_filename": names[2],
                }
                selected = module._select_document_raw_dir(state, base_dir)
                assert selected == base_dir + "/20260907-news"
                (repo / selected).mkdir()
                assert module._select_document_raw_dir(state, base_dir) == selected
                fresh = {key: value for key, value in state.items() if key != "raw_allocation"}
                assert module._select_document_raw_dir(fresh, base_dir) == selected + "-2"
                (repo / selected).rmdir()
                collision.unlink()


def test_conference_summary_date_prefers_source_label_over_event_date():
    text = (
        "# 第二届量子物理与智能计算交叉研讨会资料汇总\n\n"
        "会议时间：2021年9月24日\n"
        "整理日期：2026年9月5日\n"
    )
    assert module.extract_admin_date(
        "会议资料汇总.md", text, "conference-summary") == "2026-09-05"
    assert module.extract_admin_date(
        "会议资料汇总.md", "发布日期：2026-09-05\n", "conference-summary"
    ) == "2026-09-05"
    assert module.extract_admin_date(
        "会议资料汇总.md",
        "2021年10月18日发布的第四轮通知将会议改期。\n",
        "conference-summary",
    ) == ""
    assert module.extract_admin_date(
        "会议资料汇总.md", "会议时间：2021年9月24日\n", "conference-summary") == ""
    assert module.extract_admin_date(
        "会议资料汇总.md", "会议时间：2021年9月24日\n", "academic-reference"
    ) == "2021-09-24"


def test_normalize_document_wiki_compiles_mechanical_contract():
    weak_output = (
        "---\ntitle: 导师培训\ntype: policy\nsources: guessed.md\n"
        "source_type: official-doc\ndate: null\nstatus: draft\n"
        "effective_from: null\neffective_to: null\ncreated: null\nupdated: null\n---\n\n"
        "# 导师培训\n\n## Navigation\n\n导航。 <RAW#L1>\n\n"
        "## 背景\n\n正文事实。 <RAW#L3>\n"
    )
    normalized, repairs = module.normalize_document_wiki(
        weak_output,
        correct_sources="admin/raw/policies/policy.md",
        source_date="",
        doc_text="导航依据\n\n正文依据\n",
        created_at="2026-09-01",
    )
    assert "## Content" in normalized
    fm = module.yaml.safe_load(re.match(r"^---\n(.*?)\n---", normalized, re.S).group(1))
    assert fm["sources"] == ["admin/raw/policies/policy.md"]
    assert fm["date"] is None and fm["date_status"] == "unknown"
    assert fm["created"] == "2026-09-01" and fm["updated"] == "2026-09-01"
    assert "effective_from" not in normalized and "effective_to" not in normalized
    assert "[^r1]" in normalized and "[^r3]" in normalized
    assert "[^r1]: admin/raw/policies/policy.md#L1" in normalized
    assert "[^r3]: admin/raw/policies/policy.md#L3" in normalized
    assert {"sources", "created", "updated", "content_heading", "source_footnotes"} <= set(repairs)


def test_normalize_document_wiki_compiles_overlapping_line_handles_exactly():
    weak_output = (
        "---\ntitle: 长文档\ntype: conference-summary\nstatus: completed\n---\n\n"
        "# 长文档\n\n## Navigation\n\n导航。 <RAW#L13>\n\n"
        "## Content\n\n较后的事实。 <RAW#L136>\n"
    )
    doc_text = "\n".join(f"第 {line} 行依据。" for line in range(1, 137)) + "\n"
    normalized, _repairs = module.normalize_document_wiki(
        weak_output,
        correct_sources="academic/raw/conferences/long.md",
        source_date="2026-09-05",
        doc_text=doc_text,
        created_at="2026-09-05",
    )
    assert "[^r13]" in normalized
    assert "[^r136]" in normalized
    assert "[^r13]6>" not in normalized
    assert "[^r13]: academic/raw/conferences/long.md#L13" in normalized
    assert "[^r136]: academic/raw/conferences/long.md#L136" in normalized


def test_transcript_normalization_sets_provenance_and_drops_inferred_department():
    weak_output = (
        "---\ntitle: 新学期工作部署会\ntype: meeting-summary\n"
        "department: 校长办公室\nstatus: final\n---\n"
        "## Navigation\n\n方复全校长讲话。 <RAW#L1>\n\n## Content\n\n部署工作。 <RAW#L2>\n"
    )
    normalized, repairs = module.normalize_document_wiki(
        weak_output,
        correct_sources="admin/raw/meetings/新学期工作部署会 速记.md",
        source_date="2026-09-03",
        doc_text="方复全校长讲话\n部署工作。\n",
        created_at="2026-09-04",
        source_kind="meeting",
    )
    fm = module.yaml.safe_load(re.match(r"^---\n(.*?)\n---", normalized, re.S).group(1))
    assert fm["source_type"] == "speech-recognition"
    assert fm["confidence"] == "medium"
    assert "department" not in fm
    assert "department_unsupported" in repairs


def test_validate_semantics_rejects_responsibility_inferred_from_speech():
    import shutil
    extract_dir = module.REPO / "temp" / "inbox-extract" / "test-responsibility-evidence"
    extract_dir.mkdir(parents=True, exist_ok=True)
    (extract_dir / "doc.md").write_text("方复全校长讲话\n", encoding="utf-8")
    state = {
        "extract_dir": str(extract_dir.relative_to(module.REPO)),
        "slots_content": "三元组:\n本文件 | 负责人 | 方复全\n",
    }
    original_validate = module.ic.validate_semantics
    module.ic.validate_semantics = lambda *_args, **_kwargs: ([], [])
    try:
        errors, _warnings = module.step_validate_semantics(state)
        assert any("负责人关系缺少" in item for item in errors)
        (extract_dir / "doc.md").write_text("项目负责人：方复全\n", encoding="utf-8")
        errors, _warnings = module.step_validate_semantics(state)
        assert not errors
    finally:
        module.ic.validate_semantics = original_validate
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_sqlite_snapshot_restores_exact_graph_state():
    import sqlite3
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        graph = root / "graph.db"
        snapshot = root / "before.db"
        with sqlite3.connect(graph) as conn:
            conn.execute("CREATE TABLE nodes(path TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO nodes VALUES ('before')")
        module.backup_sqlite_database(graph, snapshot)
        with sqlite3.connect(graph) as conn:
            conn.execute("DELETE FROM nodes")
            conn.execute("INSERT INTO nodes VALUES ('after')")
        module.restore_sqlite_database(snapshot, graph)
        with sqlite3.connect(graph) as conn:
            values = [row[0] for row in conn.execute("SELECT path FROM nodes")]
        assert values == ["before"]


def test_rollback_removes_manifest_companion_restores_graph_and_marks_receipt():
    import shutil
    token = "test-document-rollback"
    root = module.REPO / "temp" / token
    extract = root / "extract"
    raw_dir = root / "raw"
    wiki = root / "wiki" / "page.md"
    graph = root / "graph.db"
    snapshot = extract / "graph-before.sqlite"
    receipt = root / "receipt.json"
    extract.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    wiki.parent.mkdir(parents=True, exist_ok=True)
    wiki.write_text("wiki", encoding="utf-8")
    for name in ("policy.docx", "policy.md"):
        (raw_dir / name).write_text(name, encoding="utf-8")
    (extract / "manifest.json").write_text(json.dumps({
        "raw_files": ["policy.docx", "policy.md"], "wiki_file": "wiki.md"
    }), encoding="utf-8")
    with sqlite3.connect(graph) as conn:
        conn.execute("CREATE TABLE nodes(path TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO nodes VALUES ('before')")
    module.backup_sqlite_database(graph, snapshot)
    with sqlite3.connect(graph) as conn:
        conn.execute("DELETE FROM nodes")
        conn.execute("INSERT INTO nodes VALUES ('after')")
    receipt.write_text(json.dumps({"status": "committed"}), encoding="utf-8")
    state = {
        "wiki_path": str(wiki.with_suffix("").relative_to(module.REPO)),
        "raw_dir": str(raw_dir.relative_to(module.REPO)),
        "source_filename": "policy.docx",
        "locator_source_filename": "policy.md",
        "extract_dir": str(extract.relative_to(module.REPO)),
        "graph_snapshot": str(snapshot.relative_to(module.REPO)),
        "graph_db_path": str(graph),
        "receipt": str(receipt.relative_to(module.REPO)),
    }
    original_trash = module.trash_util.trash_path
    module.trash_util.trash_path = lambda path: Path(path).unlink()
    try:
        rolled = module.rollback_committed(state)
        assert not wiki.exists()
        assert not (raw_dir / "policy.docx").exists()
        assert not (raw_dir / "policy.md").exists()
        with sqlite3.connect(graph) as conn:
            assert [row[0] for row in conn.execute("SELECT path FROM nodes")] == ["before"]
        assert json.loads(receipt.read_text(encoding="utf-8"))["status"] == "rolled_back"
        assert any("policy.md" in item for item in rolled)
    finally:
        module.trash_util.trash_path = original_trash
        shutil.rmtree(root, ignore_errors=True)


def main():
    test_domain_config_keys()
    test_get_subdir_admin()
    test_get_subdir_teaching()
    test_get_subdir_business()
    test_academic_subdirs_are_explicit_and_separate()
    test_academic_prompt_is_locked_to_explicit_type()
    test_academic_agent_wiki_maps_raw_and_wiki_separately()
    test_academic_missing_type_stops_before_transaction_and_raw_write()
    test_build_doc_wiki_prompt_admin()
    test_build_doc_wiki_prompt_teaching()
    test_build_doc_wiki_prompt_business()
    test_build_doc_slots_prompt_admin()
    test_build_doc_slots_prompt_teaching()
    test_build_doc_slots_prompt_business()
    test_validate_wiki_teaching()
    test_validate_wiki_business()
    test_validate_wiki_teaching_bad_type()
    test_parse_semantic_text_teaching()
    test_parse_semantic_text_business()
    test_admin_wrapper_compat()
    test_agent_mode_wiki_roundtrip()
    test_agent_mode_slots_roundtrip()
    test_short_api_document_combines_wiki_and_slots_in_one_call()
    test_agent_required_output_has_write_to()
    test_unknown_source_date_is_not_replaced_with_ingestion_date()
    test_source_date_accepts_filename_formats_and_validates_calendar()
    test_source_date_uses_bounded_nearest_directory_after_explicit_evidence()
    test_preprocess_preserves_source_path_date_for_both_backends()
    test_unknown_date_is_compiled_when_model_omits_field()
    test_labeled_source_dates_reject_truncated_digits()
    test_document_dedup_requires_content_identity_including_nested_raw()
    test_document_dedup_revalidates_indexed_raw_and_rejects_outside_paths()
    test_raw_allocation_checks_entire_bundle_and_reuses_reserved_path()
    test_same_name_documents_finalize_without_overwriting_for_both_backends()
    test_conference_summary_date_prefers_source_label_over_event_date()
    test_normalize_document_wiki_compiles_mechanical_contract()
    test_normalize_document_wiki_compiles_overlapping_line_handles_exactly()
    test_transcript_normalization_sets_provenance_and_drops_inferred_department()
    test_validate_semantics_rejects_responsibility_inferred_from_speech()
    test_sqlite_snapshot_restores_exact_graph_state()
    test_rollback_removes_manifest_companion_restores_graph_and_marks_receipt()
    test_preprocess_binary_creates_raw_companion()
    test_preprocess_native_text_uses_original()
    test_validate_native_text_uses_inbox_source_before_commit()
    test_preprocess_text_pdf_creates_line_locator_companion()
    test_finalize_lands_original_and_companion_together()
    test_build_source_context_document_keeps_short_full()
    test_build_source_context_document_reduces_by_heading()
    test_reduced_api_document_context_preserves_original_raw_lines()
    test_build_source_context_meeting_uses_head_tail()
    test_remove_no_info_slot_values_drops_placeholder_only()
    test_append_source_to_existing_list()
    test_append_source_creates_field()
    test_append_source_idempotent()
    test_append_source_missing_page()
    test_related_to_step_calls_graph_ingest()
    print("ingest document regression: PASS")


def _locator_test_state(name: str, source_suffix: str) -> tuple[dict, Path]:
    root = module.REPO / "temp" / "test_ingest_document_locator"
    root.mkdir(parents=True, exist_ok=True)
    source = root / f"{name}{source_suffix}"
    source.write_bytes(b"test")
    state = {
        "source": str(source.relative_to(module.REPO)),
        "source_filename": source.name,
        "extract_dir": f"temp/inbox-extract/test-locator-{name}",
    }
    return state, root


def test_preprocess_binary_creates_raw_companion():
    """DOCX 无稳定原生 locator：原件与同 stem Markdown 一起进入 manifest。"""
    import shutil
    state, root = _locator_test_state("policy", ".docx")
    original_extract = module.extract_doc_text
    module.extract_doc_text = lambda source, extract_dir=None: "# Policy\n\nExact text.\n"
    try:
        ok, msg = module.step_preprocess(state)
        assert ok, msg
        companion = module.REPO / state["extract_dir"] / "policy.md"
        assert companion.read_text(encoding="utf-8") == "# Policy\n\nExact text.\n"
        assert state["raw_locator_kind"] == "companion"
        assert state["locator_source_filename"] == "policy.md"
        assert module._manifest_raw_files(state) == ["policy.docx", "policy.md"]
    finally:
        module.extract_doc_text = original_extract
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(module.REPO / state["extract_dir"], ignore_errors=True)


def test_preprocess_native_text_uses_original():
    """TXT/Markdown 原件直接支持行 locator，不生成 raw companion。"""
    import shutil
    state, root = _locator_test_state("minutes", ".txt")
    source = module.REPO / state["source"]
    source.write_text("line one\nline two\n", encoding="utf-8")
    try:
        ok, msg = module.step_preprocess(state)
        assert ok, msg
        assert state["raw_locator_kind"] == "section-line"
        assert state["locator_source_filename"] == "minutes.txt"
        assert module._manifest_raw_files(state) == ["minutes.txt"]
        assert not (module.REPO / state["extract_dir"] / "minutes.md").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(module.REPO / state["extract_dir"], ignore_errors=True)


def test_validate_native_text_uses_inbox_source_before_commit():
    """最终 Raw 尚未落位时，原生行 locator 映射到当前 source 文件。"""
    import shutil
    state, root = _locator_test_state("conference", ".md")
    source = module.REPO / state["source"]
    source.write_text("# Conference\n\nRecorded fact.\n", encoding="utf-8")
    try:
        ok, msg = module.step_preprocess(state)
        assert ok, msg
        raw_locator = "academic/raw/conferences/conference.md"
        wiki = (
            "---\ntitle: Conference\ntype: conference-summary\nsources:\n"
            f"  - \"{raw_locator}\"\n"
            "source_type: discussion\nconfidence: medium\ndate: 2026-09-05\n"
            "---\n# Conference\n\n## Navigation\n\nRecorded fact.[^r3]\n\n"
            "## Content\n\nRecorded fact.[^r3]\n\n"
            f"[^r3]: {raw_locator}#L3\n"
        )
        extract_dir = module.REPO / state["extract_dir"]
        (extract_dir / "wiki.md").write_text(wiki, encoding="utf-8")
        state.update({
            "wiki_content": wiki,
            "subproject": "academic",
            "document_type": "conference-summary",
            "source_kind": "ordinary",
            "raw_locator": raw_locator,
        })
        assert not module.step_validate_wiki(state)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(module.REPO / state["extract_dir"], ignore_errors=True)


def test_preprocess_text_pdf_creates_line_locator_companion():
    """PDF prompt 使用 RAW#Lx，因此文本层 PDF 也需 Markdown companion。"""
    import fitz
    import shutil
    state, root = _locator_test_state("report", ".pdf")
    source = module.REPO / state["source"]
    document = fitz.open()
    document.new_page().insert_text((72, 72), "native pdf text")
    document.save(str(source))
    document.close()
    original_extract = module.extract_doc_text
    module.extract_doc_text = lambda source, extract_dir=None: "native pdf text\n"
    try:
        ok, msg = module.step_preprocess(state)
        assert ok, msg
        assert state["raw_locator_kind"] == "companion"
        assert state["locator_source_filename"] == "report.md"
        assert module._manifest_raw_files(state) == ["report.pdf", "report.md"]
        companion = module.REPO / state["extract_dir"] / "report.md"
        assert companion.read_text(encoding="utf-8") == "native pdf text\n"
    finally:
        module.extract_doc_text = original_extract
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(module.REPO / state["extract_dir"], ignore_errors=True)


def test_same_name_documents_finalize_without_overwriting_for_both_backends():
    import tempfile
    from unittest.mock import patch

    original_run = module.ic.run

    def run_isolated_finalizer(command, repo):
        actual = [command[0], str(SCRIPT.with_name("inbox_finalize.py")), *command[2:]]
        return original_run([*actual, "--project-root", str(repo)], repo)

    for backend in ("agent", "api"):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            source = repo / "inbox/2026.09.07/新闻稿.docx"
            base = repo / "academic/raw/conferences"
            source.parent.mkdir(parents=True)
            base.mkdir(parents=True)
            source.write_bytes(b"new-event")
            old_files = {
                "新闻稿.docx": b"old-event",
                "新闻稿.md": b"old-text",
                "新闻稿.docx.source.json": b"old-context",
            }
            for name, content in old_files.items():
                (base / name).write_bytes(content)
            state = {
                "transaction_id": "test-same-name-" + backend,
                "source": str(source.relative_to(repo)), "source_filename": source.name,
                "subproject": "academic", "document_type": "conference-summary",
                "extract_dir": "temp/inbox-extract/test-same-name-" + backend,
            }
            with patch.object(module, "REPO", repo), patch.dict(
                "os.environ", {"INGEST_BACKEND": backend},
            ), patch.object(module, "extract_doc_text", return_value="# 新闻稿\n\n新的会议报道。\n"), patch.object(
                module.ic, "run", side_effect=run_isolated_finalizer,
            ), patch.object(module, "call_text", side_effect=AssertionError("no external LLM in regression")):
                assert module.ingest_mode() == backend
                assert module.step_dedup_check(state) == (False, "")
                ok, message = module.step_preprocess(state)
                assert ok, message
                state["admin_id"] = module.generate_admin_id(source.name, "新闻稿", state["date_str"])
                state["_awaiting_agent_wiki"] = True
                extract_dir = repo / state["extract_dir"]
                (extract_dir / "wiki.md").write_text(
                    "---\ntitle: 新闻稿\ntype: conference-summary\nstatus: completed\n---\n"
                    "\n## Navigation\n\n会议报道。 <RAW#L3>\n\n## Content\n\n新的会议报道。 <RAW#L3>\n",
                    encoding="utf-8",
                )
                ok, message = module.step_write_wiki(state)
                assert ok, message
                expected = "academic/raw/conferences/20260907-新闻稿"
                assert state["raw_dir"] == expected
                assert state["raw_locator"] == expected + "/新闻稿.md"
                assert expected + "/新闻稿.md#L3" in state["wiki_content"]
                state["_awaiting_agent_wiki"] = True
                ok, message = module.step_write_wiki(state)
                assert ok, message
                assert state["raw_dir"] == expected
                assert module.step_validate_wiki(state) == []
                ok, message = module.step_finalize(state)
                assert ok, message
                landed = repo / state["raw_dir"]
                assert (landed / source.name).read_bytes() == source.read_bytes()
                assert (landed / "新闻稿.md").read_text(encoding="utf-8") == "# 新闻稿\n\n新的会议报道。\n"
                context = json.loads((landed / "新闻稿.docx.source.json").read_text(encoding="utf-8"))
                assert context["source"] == state["source"]
                assert (repo / (state["wiki_path"] + ".md")).is_file()
                for name, content in old_files.items():
                    assert (base / name).read_bytes() == content
                assert module.step_dedup_check(state)[0] is True
                assert state["dedup_result"][0]["path"] == expected + "/新闻稿.docx"


def test_finalize_lands_original_and_companion_together():
    """原文件和 Markdown companion 由同一 manifest 原子落到同一 raw 目录。"""
    import shutil
    token = "test-locator-finalize"
    root = module.REPO / "temp" / token
    extract_dir = module.REPO / "temp" / "inbox-extract" / token
    source = root / "source" / "policy.docx"
    raw_dir = root / "raw" / "policies"
    wiki_path = root / "wiki" / "policies" / "policy"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"original")
    extract_dir.mkdir(parents=True, exist_ok=True)
    (extract_dir / "policy.md").write_text("# Policy\n\nExact text.\n", encoding="utf-8")
    source_context = '{"schema":"document-source-context-v1","source":"inbox/2026.09.06/policy.docx"}\n'
    (extract_dir / "policy.docx.source.json").write_text(source_context, encoding="utf-8")
    (extract_dir / "wiki.md").write_text("---\ntitle: Policy\n---\n", encoding="utf-8")
    state = {
        "transaction_id": token,
        "source": str(source.relative_to(module.REPO)),
        "source_filename": "policy.docx",
        "locator_source_filename": "policy.md",
        "extract_dir": str(extract_dir.relative_to(module.REPO)),
        "source_context_filename": "policy.docx.source.json",
        "raw_dir": str(raw_dir.relative_to(module.REPO)),
        "wiki_path": str(wiki_path.relative_to(module.REPO)),
        "admin_id": token,
    }
    try:
        ok, msg = module.step_finalize(state)
        assert ok, msg
        assert (raw_dir / "policy.docx").read_bytes() == b"original"
        assert (raw_dir / "policy.md").read_text(encoding="utf-8") == "# Policy\n\nExact text.\n"
        assert (raw_dir / "policy.docx.source.json").read_text(encoding="utf-8") == source_context
    finally:
        receipt = state.get("receipt")
        if receipt:
            receipt_path = module.REPO / receipt
            if receipt_path.exists():
                receipt_path.unlink()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_build_source_context_document_keeps_short_full():
    """普通短文档保留全文，不做额外截断。"""
    text = "# 标题\n\n这是文档内容。"
    assert module.ic.build_source_context("document", text) == text


def test_build_source_context_document_reduces_by_heading():
    """API 路径的长文档按 Markdown 标题程序定向摘要。"""
    long_text = (
        "# 背景\n\n" + "背景内容" * 5000 + "\n\n"
        "# 方法\n\n" + "方法内容" * 5000 + "\n\n"
        "# 结论\n\n" + "结论内容" * 5000 + "\n\n"
    )
    reduced = module.ic.build_source_context("document", long_text, force_reduced=True)
    assert len(reduced) < len(long_text)
    assert "背景" in reduced
    assert "方法" in reduced
    assert "结论" in reduced
    # 各 section 被裁剪到 section_char_cap 以内，总缩减应显著
    assert len(reduced) < len(long_text) // 2


def test_reduced_api_document_context_preserves_original_raw_lines():
    lines = ["# 附录"]
    lines.extend(f"无关内容 {index} " + "x" * 400 for index in range(100))
    lines.extend(["# 结论", "最终结论。"])
    source = "\n".join(lines) + "\n"
    annotated = module.wl.annotate_raw_lines(source, "RAW")
    reduced = module.ic.build_source_context("document", annotated, force_reduced=True)
    target = f"<RAW#L{len(lines)}> 最终结论。"
    assert target in reduced

    prompt = module.build_doc_wiki_prompt(
        reduced,
        "20260905-long-api",
        "2026-09-05",
        preannotated_raw_lines=True,
    )
    assert target in prompt
    assert f"<RAW#L1> {target}" not in prompt


def test_build_source_context_meeting_uses_head_tail():
    """会议长文本没有标题结构时走确定性头尾截取。"""
    profile = module.ic.CONTEXT_PROFILES["meeting"]
    long_text = "x" * (profile["full_text_max_chars"] + 1_000)
    reduced = module.ic.build_source_context("meeting", long_text)
    assert len(reduced) < len(long_text)
    assert "fallback" in reduced


def test_remove_no_info_slot_values_drops_placeholder_only():
    """无信息占位值清空，正常语义槽保留。"""
    text = "期刊:\n无明确期刊\n三元组:\n本文件 | 涉及 | 测试概念\n"
    cleaned = module.ic.remove_no_info_slot_values(text)
    assert "无明确期刊" not in cleaned
    assert "期刊:" in cleaned
    assert "本文件 | 涉及 | 测试概念" in cleaned


def test_append_source_to_existing_list():
    """append_source_to_page 在已有 sources 列表末尾追加来源。"""
    import tempfile
    import graph_lib as gl
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        page = repo / "test.md"
        page.write_text('---\ntitle: 测试\ntype: policy\nsources:\n  - "raw/a.docx"\nsource_type: official-doc\n---\n\n# 测试\n', encoding="utf-8")
        result = module.ic.append_source_to_page(repo, "test", "raw/b.docx")
        assert result is True
        fm = gl.read_frontmatter("test".replace("test", str(page)))
        sources = gl.parse_list_field(fm, "sources")
        assert sources == ["raw/a.docx", "raw/b.docx"]


def test_append_source_creates_field():
    """append_source_to_page 在无 sources 字段时创建新字段。"""
    import tempfile
    import graph_lib as gl
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        page = repo / "test.md"
        page.write_text('---\ntitle: 测试\ntype: reference\nsource_type: official-doc\n---\n\n# 测试\n', encoding="utf-8")
        result = module.ic.append_source_to_page(repo, "test", "raw/c.docx")
        assert result is True
        fm = gl.read_frontmatter(str(page))
        sources = gl.parse_list_field(fm, "sources")
        assert sources == ["raw/c.docx"]


def test_append_source_idempotent():
    """append_source_to_page 重复追加同一来源不产生重复。"""
    import tempfile
    import graph_lib as gl
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        page = repo / "test.md"
        page.write_text('---\ntitle: 测试\nsources:\n  - "raw/a.docx"\n---\n\n# 测试\n', encoding="utf-8")
        assert module.ic.append_source_to_page(repo, "test", "raw/a.docx") is False
        fm = gl.read_frontmatter(str(page))
        sources = gl.parse_list_field(fm, "sources")
        assert sources == ["raw/a.docx"]


def test_append_source_missing_page():
    """append_source_to_page 对不存在的页面返回 False。"""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        assert module.ic.append_source_to_page(repo, "nonexistent", "raw/x.docx") is False


def test_related_to_step_calls_graph_ingest():
    """--related-to 模式 step_update_graph 调用 ic.step_update_graph 建页面节点（不跳过）。"""
    import inspect
    src = inspect.getsource(module.step_update_graph)
    assert "ic.step_update_graph(state, REPO)" in src, \
        "--related-to 分支必须调用 ic.step_update_graph 创建页面节点"
    assert 'state["status"] = "completed"' not in src, \
        "--related-to 分支不应手动设 status（管线统一管理状态转换）"

if __name__ == "__main__":
    main()

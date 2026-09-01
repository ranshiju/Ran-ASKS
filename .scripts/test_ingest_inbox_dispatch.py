#!/usr/bin/env python3
"""ingest_inbox.py DSH 分发层回归测试。"""
import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("ingest_inbox.py")
spec = importlib.util.spec_from_file_location("ingest_inbox", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_extract_last_json_ignores_domain_status():
    content = (
        '{"hub_scope_route":{"candidates":[{"status":"active"}]}}\n'
        '{"status":"completed","paper_id":"p1","quality_status":"complete"}\n'
        'progress log'
    )
    parsed = module._extract_last_json(content)
    assert parsed["status"] == "completed"
    assert parsed["paper_id"] == "p1"


def test_dsi_tool_routes_file_types():
    assert module.dsi_tool("paper", "inbox/a.pdf", "academic") == \
        ("ingest_paper_pdf", {"pdf": "inbox/a.pdf"})
    assert module.dsi_tool("meeting", "inbox/a.txt", "academic") == \
        ("ingest_meeting_txt", {"file": "inbox/a.txt", "subproject": "academic"})
    assert module.dsi_tool("document", "inbox/a.md", "academic", "editorial") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "academic",
                                  "document_type": "editorial"})
    assert module.dsi_tool("document", "inbox/a.md", "teaching") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "teaching"})


def test_academic_document_classification_gate():
    assert module.classify_academic_document(Path("inbox/CCCF专题导言初排版-张鹏.pdf")) == "editorial"
    assert module.classify_academic_document(Path("inbox/量子计算背景资料.docx")) is None
    try:
        module.dsi_tool("document", "inbox/a.md", "academic")
        raise AssertionError("academic 文档缺少类型时应阻断")
    except ValueError as exc:
        assert "classification_required" in str(exc)
    command = module.dispatch_command(
        "document", "inbox/a.md", "academic", "academic-reference")
    assert command[-4:] == ["--subproject", "academic", "--document-type", "academic-reference"]


def test_boundary_scores_request_api_review_without_changing_program_type():
    original_read = module.read_pdf_text
    original_metadata = module._is_academic_by_metadata
    module._is_academic_by_metadata = lambda _path: False
    try:
        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\nReferences\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/borderline.pdf"))
        assert decision["file_type"] == "document"
        assert decision["score"] == 2
        assert decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\nReferences\nIntroduction\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/borderline.pdf"))
        assert decision["file_type"] == "paper"
        assert decision["score"] == 3
        assert decision["needs_api_review"]
    finally:
        module.read_pdf_text = original_read
        module._is_academic_by_metadata = original_metadata

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "notes.txt"
        path.write_text("会议安排", encoding="utf-8")
        decision = module.classify_file_details(path)
        assert decision["file_type"] == "document"
        assert decision["score"] == 1
        assert decision["needs_api_review"]


def test_api_classification_review_requires_confident_agreement():
    decision = {
        "file_type": "paper", "score": 3, "threshold": 3,
        "markers": ["abstract", "references", "introduction"],
        "review_text": "Abstract. Introduction. References.",
    }
    captured = []

    def fake_call(prompt, schema_check, **kwargs):
        captured.append((prompt, kwargs))
        parsed = {
            "doc_type": "paper", "confidence": "medium",
            "reasons": ["具有论文结构"], "evidence_quotes": ["Abstract"],
        }
        assert schema_check(parsed)
        return {"ok": True, "parsed": parsed}

    original_call = module.call_json
    module.call_json = fake_call
    try:
        review = module.review_low_confidence_classification(
            Path("inbox/borderline.pdf"), decision)
    finally:
        module.call_json = original_call

    assert captured[0][1]["operation"] == "ingest_type_review"
    assert captured[0][1]["reasoning"] == "fast"
    assert module.reconcile_classification(decision, review) == (True, "")
    mismatch = review | {"doc_type": "document"}
    assert not module.reconcile_classification(decision, mismatch)[0]
    low = review | {"confidence": "low"}
    assert not module.reconcile_classification(decision, low)[0]


def test_plan_ingest_order_versions_last():
    from pathlib import Path
    primary = Path("inbox/contract.txt")
    version = Path("inbox/contract-盖章扫描版.txt")
    other = Path("inbox/paper.pdf")
    classified = [
        (version, "document", "inbox/contract-盖章扫描版.txt"),
        (other, "paper", "inbox/paper.pdf"),
        (primary, "document", "inbox/contract.txt"),
    ]
    ordered, notes = module._plan_ingest_order(classified)
    names = [item[0].name for item in ordered]
    assert names.index("contract.txt") < names.index("contract-盖章扫描版.txt")
    assert "paper.pdf" in names
    assert any("盖章扫描版" in note for note in notes)


def test_map_paper_batch_results():
    parsed = {
        "status": "completed",
        "items": [
            {"source": "inbox/a.pdf", "status": "completed",
             "paper_id": "a-2024", "transaction_id": "t1",
             "proposition_status": "sparse: 2 propositions", "quality_status": "complete",
             "graph_report": {"hub_dynamics": {"membership": {"nodes": [{"candidates": [1, 2]}]}}},
             "quality_warnings": ["proposition: mock"]},
            {"source": "inbox/b.pdf", "status": "duplicate_found",
             "transaction_id": "t2"},
            {"source": "inbox/c.pdf", "status": "failed",
             "errors": ["extraction_failed"]},
        ],
    }
    mapped = module._map_paper_batch_results(parsed)
    assert [r["file"] for r in mapped] == ["a.pdf", "b.pdf", "c.pdf"]
    assert mapped[0]["ok"] is True and mapped[0]["paper_id"] == "a-2024"
    assert mapped[0]["quality_status"] == "complete"
    assert mapped[0]["proposition_status"] == "sparse: 2 propositions"
    assert mapped[0]["graph_report"]["hub_dynamics"], "完整图诊断必须保留给持久化报告"
    assert mapped[1]["ok"] is True and mapped[1]["status"] == "duplicate_found"
    assert mapped[2]["ok"] is False and mapped[2]["reason"] == "extraction_failed"


def test_auto_resolve_abbr_key_contract():
    """_auto_resolve_abbreviations 返回键与调用方期望一致，不触发 KeyError。

    lightweight_abbr_resolve 返回 {"resolved", "remaining", "details"}；
    _auto_resolve_abbreviations 必须用 base["resolved"]（非 base["slot_resolved"]），
    且返回 prop_resolved（非 proposition_applied）以匹配调用方检查。
    """
    import inspect
    src = inspect.getsource(module._auto_resolve_abbreviations)
    # 不应访问不存在的 base["slot_resolved"]
    assert 'base["slot_resolved"]' not in src, \
        "_auto_resolve_abbreviations 不应访问不存在的 base['slot_resolved']"
    # 应使用 base.get("resolved", 0)
    assert 'base.get("resolved", 0)' in src, \
        "_auto_resolve_abbreviations 应使用 base.get('resolved', 0)"
    # 返回键应为 prop_resolved（调用方检查 prop_resolved）
    assert '"prop_resolved"' in src, \
        "_auto_resolve_abbreviations 应返回 prop_resolved 键"
    # 不应返回 proposition_applied（键名与调用方不匹配）
    assert '"proposition_applied"' not in src, \
        "_auto_resolve_abbreviations 不应返回 proposition_applied（与调用方键名不匹配）"


def test_compact_summary_excludes_graph_diagnostics_and_returns_report_path():
    report = {
        "total": 1, "completed": 1, "degraded": 0, "failed": 0, "skipped": 0,
        "files": [{
            "file": "a.pdf", "status": "completed", "quality_status": "complete",
            "transaction_id": "t1", "graph_report": {"huge": [1, 2, 3]},
        }],
    }
    report_path = module.REPO / "cross-domain" / "ingest-reports" / "test.json"
    compact = module._compact_summary(report, report_path)
    assert compact["status"] == "completed"
    assert compact["report_path"] == "cross-domain/ingest-reports/test.json"
    assert "graph_report" not in compact["files"][0]
    blocked = {**report, "completed": 0, "failed": 1,
               "files": [{"file": "b.pdf", "status": "agent_required", "reason": "agent 接管"}]}
    assert module._compact_summary(blocked, report_path)["status"] == "agent_required"
    classification_blocked = {
        **report, "completed": 0, "skipped": 1,
        "files": [{"file": "c.docx", "status": "classification_required",
                   "reason": "需要显式分类"}],
    }
    assert module._compact_summary(
        classification_blocked, report_path)["status"] == "classification_required"
    unrelated_hubs = {**report, "hub_auto_create": {
        "status": "agent_required", "eligible_count": 82,
        "candidates_file": "temp/hubs.json",
    }}
    hub_compact = module._compact_summary(unrelated_hubs, report_path)
    assert hub_compact["status"] == "completed"
    assert "hub_auto_create" not in hub_compact, \
        "全库维护候选不得覆盖当前摄入终态"
    import inspect
    assert "print(content)" not in inspect.getsource(module.main), \
        "--run 不应把底层完整 stdout 回显给 Agent"


def test_zero_success_skips_global_post_ingest_scans():
    originals = (module._auto_resolve_abbreviations,
                 module.ic.detect_people_page_candidates,
                 module._auto_create_hubs)
    calls = []
    module._auto_resolve_abbreviations = lambda: calls.append("abbr")
    module.ic.detect_people_page_candidates = lambda _repo: calls.append("people")
    module._auto_create_hubs = lambda _session: calls.append("hub")
    try:
        summaries = module._run_post_ingest_maintenance([
            {"ok": False, "skipped": True, "status": "classification_required"}
        ], "session")
    finally:
        (module._auto_resolve_abbreviations,
         module.ic.detect_people_page_candidates,
         module._auto_create_hubs) = originals
    assert calls == [], "零成功文件不得触发全库收尾扫描"
    assert all(item == {"status": "skipped", "reason": "no_successful_files"}
               for item in summaries)


def main():
    test_extract_last_json_ignores_domain_status()
    test_dsi_tool_routes_file_types()
    test_academic_document_classification_gate()
    test_boundary_scores_request_api_review_without_changing_program_type()
    test_api_classification_review_requires_confident_agreement()
    test_plan_ingest_order_versions_last()
    test_map_paper_batch_results()
    test_auto_resolve_abbr_key_contract()
    test_compact_summary_excludes_graph_diagnostics_and_returns_report_path()
    test_zero_success_skips_global_post_ingest_scans()
    print("ingest_inbox dispatch regression: PASS")


if __name__ == "__main__":
    main()

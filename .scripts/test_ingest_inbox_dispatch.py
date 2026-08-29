#!/usr/bin/env python3
"""ingest_inbox.py DSH 分发层回归测试。"""
import importlib.util
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
    assert module.dsi_tool("document", "inbox/a.md", "academic") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "admin"})
    assert module.dsi_tool("document", "inbox/a.md", "teaching") == \
        ("ingest_document_file", {"file": "inbox/a.md", "subproject": "teaching"})


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
    import inspect
    assert "print(content)" not in inspect.getsource(module.main), \
        "--run 不应把底层完整 stdout 回显给 Agent"


def main():
    test_extract_last_json_ignores_domain_status()
    test_dsi_tool_routes_file_types()
    test_plan_ingest_order_versions_last()
    test_map_paper_batch_results()
    test_auto_resolve_abbr_key_contract()
    test_compact_summary_excludes_graph_diagnostics_and_returns_report_path()
    print("ingest_inbox dispatch regression: PASS")


if __name__ == "__main__":
    main()

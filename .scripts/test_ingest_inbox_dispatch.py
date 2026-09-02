#!/usr/bin/env python3
"""ingest_inbox.py DSH 分发层回归测试。"""
import importlib.util
import json
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


def test_extract_last_json_preserves_top_level_batch_envelope():
    payload = {
        "status": "partial",
        "phase": "paper_batch",
        "items": [{"file": "review.pdf", "status": "bibliographic_review_required"}],
    }
    parsed = module._extract_last_json("progress\n" + json.dumps(payload) + "\ndone")
    assert parsed == payload
    assert len(parsed["items"]) == 1


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


def test_uncertain_scores_request_api_review_without_changing_program_type():
    original_read = module.read_pdf_text
    original_metadata = module._is_academic_by_metadata
    module._is_academic_by_metadata = lambda _path: False
    try:
        module.read_pdf_text = lambda *_args, **_kwargs: "Abstract\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/uncertain.pdf"))
        assert decision["file_type"] == "document"
        assert decision["score"] == 1
        assert decision["confidence"] == "low"
        assert decision["needs_api_review"]

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

        module.read_pdf_text = lambda *_args, **_kwargs: "普通文档\n" + ("正文 " * 100)
        decision = module.classify_file_details(Path("inbox/document.pdf"))
        assert decision["score"] == 0
        assert decision["confidence"] == "high"
        assert not decision["needs_api_review"]

        module.read_pdf_text = lambda *_args, **_kwargs: (
            "Abstract\nReferences\nIntroduction\nKeywords\n" + ("正文 " * 100)
        )
        decision = module.classify_file_details(Path("inbox/paper.pdf"))
        assert decision["score"] == 4
        assert decision["confidence"] == "high"
        assert not decision["needs_api_review"]
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


def test_api_classification_review_resolves_uncertain_program_decision():
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
    assert module.reconcile_classification(decision, review) == ("paper", "")
    mismatch = review | {"doc_type": "document"}
    assert module.reconcile_classification(decision, mismatch) == ("document", "")
    low = review | {"confidence": "low"}
    assert module.reconcile_classification(decision, low)[0] is None
    ambiguous = review | {"doc_type": "ambiguous"}
    assert module.reconcile_classification(decision, ambiguous)[0] is None

    module.call_json = lambda *_args, **_kwargs: {
        "ok": True,
        "parsed": review | {"doc_type": "meeting"},
    }
    try:
        invalid = module.review_low_confidence_classification(
            Path("inbox/borderline.pdf"), decision)
    finally:
        module.call_json = original_call
    assert invalid["status"] == "review_error"
    assert "不适用于 .pdf" in invalid["error"]


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
             "bibliographic_worker": {"api_called": False, "skip_reason": "deterministic_fast_path"},
             "relationship_worker": {"api_called": False, "skip_reason": "deterministic_fast_path"},
             "semantic_repair_worker": {"api_called": False, "skip_reason": "non_blocking_only"},
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
    assert mapped[0]["bibliographic_worker"]["skip_reason"] == "deterministic_fast_path"
    assert mapped[0]["relationship_worker"]["skip_reason"] == "deterministic_fast_path"
    assert mapped[0]["semantic_repair_worker"]["skip_reason"] == "non_blocking_only"
    assert mapped[0]["graph_report"]["hub_dynamics"], "完整图诊断必须保留给持久化报告"
    assert mapped[1]["ok"] is True and mapped[1]["status"] == "duplicate_found"
    assert mapped[2]["ok"] is False and mapped[2]["reason"] == "extraction_failed"


def test_map_paper_batch_preserves_bibliographic_review_contract():
    parsed = {"status": "partial", "items": [{
        "source": "inbox/review.pdf",
        "status": "bibliographic_review_required",
        "transaction_id": "txn-review",
        "errors": ["authors 候选校验失败"],
        "retryable": False,
        "next_action": "repair_bibliographic_review_then_resume",
        "bibliographic_review": {"status": "validation_error"},
    }]}
    entry = module._map_paper_batch_results(parsed)[0]
    assert entry["status"] == "bibliographic_review_required"
    assert entry["transaction_id"] == "txn-review"
    assert entry["retryable"] is False
    assert entry["next_action"] == "repair_bibliographic_review_then_resume"
    assert entry["bibliographic_review"]["status"] == "validation_error"


def test_auto_resolve_abbr_key_contract():
    import json
    from types import SimpleNamespace

    old_repo = module.REPO
    old_resolve = module.ic.lightweight_abbr_resolve
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.ic.lightweight_abbr_resolve = lambda _repo: {
                "status": "completed", "resolved": 0, "remaining": 1, "details": [],
            }
            module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stderr="", stdout=json.dumps({
                    "status": "agent_required", "resolved_count": 0,
                    "warning_count": 1,
                    "candidates": [{
                        "token": "CONFLICTBANK", "suggested_kind": "canonical_name",
                        "allowed_kinds": ["canonical_name", "ambiguous"],
                    }],
                }),
            )
            summary = module._auto_resolve_abbreviations("session/unsafe")
            assert summary["status"] == "agent_required"
            assert summary["prop_resolved"] == 0
            assert summary["warning_count"] == 1
            review = module.REPO / summary["review_file"]
            payload = json.loads(review.read_text(encoding="utf-8"))
            assert payload["candidates"][0]["token"] == "CONFLICTBANK"
            assert review.name == "session-unsafe.json"
    finally:
        module.REPO = old_repo
        module.ic.lightweight_abbr_resolve = old_resolve
        module.subprocess.run = old_run


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
    bibliographic_blocked = {
        **report, "completed": 0, "failed": 1,
        "files": [{
            "file": "review.pdf", "status": "bibliographic_review_required",
            "transaction_id": "txn-review", "retryable": False,
            "next_action": "repair_bibliographic_review_then_resume",
        }],
    }
    compact_review = module._compact_summary(bibliographic_blocked, report_path)
    assert compact_review["status"] == "bibliographic_review_required"
    assert compact_review["files"][0]["retryable"] is False
    assert compact_review["files"][0]["next_action"] == "repair_bibliographic_review_then_resume"
    unrelated_hubs = {**report, "maintenance": {
        "status": "agent_required", "receipt_path": "temp/maintenance.json",
        "actions": [{"component": "hubs", "next_action": "agent_define_and_apply_hub_maintenance"}],
        "errors": [], "components": {"hubs": {
            "status": "agent_required", "eligible_count": 82, "split_count": 1,
            "candidates_file": "temp/hubs.json",
            "split_candidates_file": "temp/splits.json",
            "next_action": "agent_define_and_apply_hub_maintenance",
        }},
    }}
    hub_compact = module._compact_summary(unrelated_hubs, report_path)
    assert hub_compact["status"] == "completed"
    assert hub_compact["file_status"] == "completed"
    assert "hub_auto_create" not in hub_compact, \
        "全库维护候选不得覆盖当前摄入终态"
    assert hub_compact["maintenance"]["components"]["hubs"] == {
        "status": "agent_required", "eligible_count": 82, "split_count": 1,
        "candidates_file": "temp/hubs.json",
        "split_candidates_file": "temp/splits.json",
        "next_action": "agent_define_and_apply_hub_maintenance",
    }
    import inspect
    assert "print(content)" not in inspect.getsource(module.main), \
        "--run 不应把底层完整 stdout 回显给 Agent"


def test_auto_create_hubs_writes_split_handoff():
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    old_repo = module.REPO
    old_run = module.subprocess.run
    check = {
        "status": "agent_required",
        "eligible": [],
        "split_candidates": [{
            "decision": "agent_definition_required",
            "hub": "academic/wiki/hubs/big",
            "clusters": [{"members": ["a"]}, {"members": ["b"]}],
        }],
        "redistribution_candidates": [{
            "decision": "redistribution_required",
            "hub": "academic/wiki/hubs/parent",
            "children": ["academic/wiki/hubs/child"],
        }],
        "backlog_count": 0,
        "split_backlog_count": 2,
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
                returncode=0, stdout=json.dumps(check), stderr="",
            )
            results = [{"graph_report": {"hub_dynamics": {"affected_nodes": ["a", "b"]}}}]
            summary = module._auto_create_hubs("session", results)
            assert summary["status"] == "agent_required"
            assert summary["eligible_count"] == 0
            assert summary["split_count"] == 1
            assert summary["redistribution_count"] == 1
            assert summary["affected_node_count"] == 2
            split_path = module.REPO / summary["split_candidates_file"]
            assert json.loads(split_path.read_text(encoding="utf-8")) == check["split_candidates"]
            redistribution_path = module.REPO / summary["redistribution_candidates_file"]
            assert json.loads(redistribution_path.read_text(encoding="utf-8")) == check["redistribution_candidates"]
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_low_margin_hub_route_writes_agent_handoff_without_maintenance_scan():
    import json
    import tempfile
    from pathlib import Path

    old_repo = module.REPO
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("route-only handoff must not run Hub maintenance scan")
            )
            results = [{
                "file": "paper.pdf",
                "wiki_path": "academic/wiki/papers/paper",
                "transaction_id": "txn",
                "graph_report": {"hub_scope_route": {
                    "decision": "candidates",
                    "reason": "scope_margin_too_small",
                    "top_score": 0.52,
                    "margin": 0.01,
                    "profile": {"text": "quantum profile", "locator": "paper#scope"},
                    "candidates": [
                        {"path": "hub-a", "title": "A", "scope": "Scope A", "score": 0.52, "canonical": True},
                        {"path": "legacy", "title": "Legacy", "scope": "Legacy", "score": 0.9, "canonical": False},
                        {"path": "hub-b", "title": "B", "scope": "Scope B", "score": 0.51, "canonical": True},
                    ],
                }},
            }]
            summary = module._auto_create_hubs("route-session", results)
            assert summary["status"] == "agent_required"
            assert summary["route_review_count"] == 1
            assert summary["affected_node_count"] == 0
            payload = json.loads(
                (module.REPO / summary["route_review_file"]).read_text(encoding="utf-8")
            )
            assert payload[0]["wiki_path"] == "academic/wiki/papers/paper"
            assert [item["path"] for item in payload[0]["canonical_candidates"]] == [
                "hub-a", "hub-b",
            ]
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_hub_timeout_is_deferred_and_retryable():
    import subprocess
    import tempfile
    from pathlib import Path

    old_repo = module.REPO
    old_run = module.subprocess.run
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module.subprocess.run = lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(args[0], 120)
            )
            results = [{"graph_report": {"hub_dynamics": {"affected_nodes": ["node"]}}}]
            summary = module._auto_create_hubs("session", results)
            assert summary["status"] == "deferred"
            assert summary["retryable"] is True
            assert summary["affected_node_count"] == 1
    finally:
        module.REPO = old_repo
        module.subprocess.run = old_run


def test_zero_success_skips_global_post_ingest_scans():
    originals = (module._auto_resolve_abbreviations,
                 module.ic.detect_people_page_candidates,
                 module._auto_create_hubs)
    calls = []
    old_repo = module.REPO
    module._auto_resolve_abbreviations = lambda _session: calls.append("abbr")
    module.ic.detect_people_page_candidates = lambda _repo: calls.append("people")
    module._auto_create_hubs = lambda _session, _results: calls.append("hub")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            envelope = module.run_post_ingest_maintenance([
                {"ok": False, "skipped": True, "status": "classification_required"}
            ], "session")
            assert calls == [], "零成功文件不得触发全库收尾扫描"
            assert envelope["status"] == "skipped"
            assert all(item == {"status": "skipped", "reason": "no_successful_files"}
                       for item in envelope["components"].values())
            assert (module.REPO / envelope["receipt_path"]).is_file()

            envelope = module.run_post_ingest_maintenance([
                {"ok": True, "status": "duplicate_found"}
            ], "session")
            assert calls == [], "精确重复没有改变知识库，不得触发全库收尾扫描"
            assert envelope["status"] == "skipped"
    finally:
        module.REPO = old_repo
        (module._auto_resolve_abbreviations,
         module.ic.detect_people_page_candidates,
         module._auto_create_hubs) = originals


def test_maintenance_error_does_not_override_completed_file_status():
    originals = (
        module.REPO, module._auto_resolve_abbreviations,
        module.ic.detect_people_page_candidates, module._auto_create_hubs,
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            module._auto_resolve_abbreviations = lambda _session: (_ for _ in ()).throw(
                RuntimeError("resolver failed")
            )
            module.ic.detect_people_page_candidates = lambda _repo: {"status": "completed"}
            module._auto_create_hubs = lambda _session, _results: {"status": "no_action"}
            maintenance = module.run_post_ingest_maintenance(
                [{"file": "paper.pdf", "ok": True, "status": "completed"}], "session"
            )
            assert maintenance["status"] == "error"
            assert any("resolver failed" in error for error in maintenance["errors"])
            report = {
                "total": 1, "completed": 1, "degraded": 0, "failed": 0, "skipped": 0,
                "files": [{"file": "paper.pdf", "status": "completed"}],
                "maintenance": maintenance,
            }
            compact = module._compact_summary(
                report, module.REPO / "cross-domain" / "ingest-reports" / "report.json"
            )
            assert compact["status"] == "completed"
            assert compact["file_status"] == "completed"
            assert compact["maintenance"]["status"] == "error"
    finally:
        (module.REPO, module._auto_resolve_abbreviations,
         module.ic.detect_people_page_candidates, module._auto_create_hubs) = originals


def test_abbreviation_decisions_require_exact_pending_tokens_and_atomic_todo():
    spec = importlib.util.spec_from_file_location(
        "resolve_abbreviations_test", Path(__file__).parent / "resolve_abbreviations.py"
    )
    resolver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resolver)
    report = resolver.apply_decisions(
        None,
        [
            {"token": "UNKNOWN", "resolution_kind": "canonical_name"},
            {"token": "KNOWN", "resolution_kind": "canonical_name"},
            {"token": "KNOWN", "resolution_kind": "canonical_name"},
        ],
        [{"token": "KNOWN", "page": "academic/wiki/papers/x", "field": "object"}],
    )
    assert report["status"] == "validation_error"
    assert any("not pending" in error for error in report["errors"])
    assert any("duplicate token" in error for error in report["errors"])

    with tempfile.TemporaryDirectory() as tmp:
        todo = Path(tmp) / "abbreviation-todo.jsonl"
        entry = {
            "schema": "abbreviation-todo-v2", "token": "KNOWN",
            "page": "academic/wiki/papers/x", "subject": "x",
            "predicate": "包含", "object": "KNOWN", "field": "object",
        }
        resolver._write_todo(todo, [entry, dict(entry)])
        assert len(todo.read_text(encoding="utf-8").splitlines()) == 1
        assert not todo.with_name(todo.name + ".tmp").exists()


def test_paper_batch_keeps_preclassified_fingerprint_results():
    import inspect
    source = inspect.getsource(module.main)
    assert "results.extend(batch_results)" in source


def main():
    test_extract_last_json_ignores_domain_status()
    test_extract_last_json_preserves_top_level_batch_envelope()
    test_dsi_tool_routes_file_types()
    test_academic_document_classification_gate()
    test_uncertain_scores_request_api_review_without_changing_program_type()
    test_api_classification_review_resolves_uncertain_program_decision()
    test_plan_ingest_order_versions_last()
    test_map_paper_batch_results()
    test_map_paper_batch_preserves_bibliographic_review_contract()
    test_auto_resolve_abbr_key_contract()
    test_compact_summary_excludes_graph_diagnostics_and_returns_report_path()
    test_auto_create_hubs_writes_split_handoff()
    test_hub_timeout_is_deferred_and_retryable()
    test_zero_success_skips_global_post_ingest_scans()
    test_maintenance_error_does_not_override_completed_file_status()
    test_abbreviation_decisions_require_exact_pending_tokens_and_atomic_todo()
    test_paper_batch_keeps_preclassified_fingerprint_results()
    print("ingest_inbox dispatch regression: PASS")


if __name__ == "__main__":
    main()

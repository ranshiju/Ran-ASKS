#!/usr/bin/env python3
"""dsh/ 摄入工具 seam 回归测试。

验证点：
- ingest 工具注册表包含 dry-run/run/run-file/paper-pdf 与 paper/meeting resume
- IngestGuard 只放行 inbox 文件与合法事务 ID，拒绝路径穿越/越界/空参数
- IngestAgentLoop 挂载 ingest guard，不挂载查询 guard
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.harness import ToolExecution, ToolExecutionResult, PreToolDecision
from dsh.ingest_tools import build_ingest_tools
from dsh.guards.ingest_guard import IngestGuard
from dsh.agent_loop import AgentLoop, IngestAgentLoop
from dsh.dispatch import dispatch_loop


def test_ingest_tool_names():
    tools = build_ingest_tools()
    names = {t.name for t in tools}
    assert {
        "ingest_inbox_dry_run",
        "ingest_inbox_run",
        "ingest_inbox_run_file",
        "ingest_paper_inbox",
        "ingest_paper_pdf",
        "ingest_paper_resume",
        "ingest_meeting_txt",
        "ingest_meeting_resume",
        "ingest_document_file",
        "re_ingest_raw",
    } <= names


def test_guard_allows_inbox_files():
    guard = IngestGuard()
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_inbox_run_file", arguments={"file": "inbox/a.pdf"})) is None
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_paper_pdf", arguments={"pdf": "inbox/a.pdf"})) is None
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_meeting_txt", arguments={"file": "inbox/m.txt"})) is None
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_document_file", arguments={"file": "inbox/d.docx"})) is None
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_document_file", arguments={
            "file": "inbox/editorial.docx", "subproject": "academic",
            "document_type": "editorial"})) is None
    assert guard.on_pre_execute(ToolExecution(
        name="re_ingest_raw", arguments={"raw": "academic/raw/references/x/paper.md"})) is None


def test_guard_denies_unsafe_files():
    guard = IngestGuard()
    for args in ({"file": "../a.pdf"}, {"file": "/etc/passwd"}, {"file": ""}):
        decision = guard.on_pre_execute(ToolExecution(name="ingest_inbox_run_file", arguments=args))
        assert decision is not None
        assert decision.kind == "deny"


def test_guard_requires_academic_document_type():
    guard = IngestGuard()
    for document_type in (None, "reference", ""):
        args = {"file": "inbox/d.docx", "subproject": "academic"}
        if document_type is not None:
            args["document_type"] = document_type
        decision = guard.on_pre_execute(ToolExecution(
            name="ingest_document_file", arguments=args))
        assert decision is not None and decision.kind == "deny"
    decision = guard.on_pre_execute(ToolExecution(
        name="ingest_document_file", arguments={
            "file": "inbox/d.docx", "subproject": "admin", "document_type": "editorial"}))
    assert decision is not None and decision.kind == "deny"


def test_document_tool_schema_accepts_academic_type():
    tool = next(t for t in build_ingest_tools() if t.name == "ingest_document_file")
    properties = tool.input_schema["properties"]
    assert "academic" in properties["subproject"]["description"]
    assert properties["document_type"]["enum"] == ["editorial", "academic-reference"]


def test_guard_validates_resume_txn():
    guard = IngestGuard()
    valid = guard.on_pre_execute(ToolExecution(
        name="ingest_paper_resume", arguments={"txn": "20260820-152247-663433-xkh7-gdqm"}))
    assert valid is None
    invalid = guard.on_pre_execute(ToolExecution(
        name="ingest_paper_resume", arguments={"txn": "../../etc"}))
    assert invalid is not None
    assert invalid.kind == "deny"
    assert guard.on_pre_execute(ToolExecution(
        name="ingest_meeting_resume", arguments={"txn": "20260903-142501-group-meeting"})) is None
    invalid_meeting = guard.on_pre_execute(ToolExecution(
        name="ingest_meeting_resume", arguments={"txn": "../../etc"}))
    assert invalid_meeting is not None and invalid_meeting.kind == "deny"


def test_ingest_loop_has_guard_and_tools():
    loop = IngestAgentLoop()
    assert "ingest_inbox_dry_run" in loop.registry.names()
    assert len(loop.registry._pre_execute) == 1


def test_ingest_loop_convenience_methods():
    loop = IngestAgentLoop()
    for name in ("ingest_meeting", "resume_meeting", "ingest_document", "re_ingest_raw",
                 "ingest_paper_inbox", "execute"):
        assert hasattr(loop, name)


def test_ingest_loop_does_not_retry_timeout_result():
    loop = IngestAgentLoop()
    calls = []
    original_execute = loop.registry.execute
    try:
        loop.registry.execute = lambda execution, _session_log: (
            calls.append(execution.name) or ToolExecutionResult(
                content="[ERROR category=api_timeout script=ingest_paper.py code=1]",
                is_error=True,
            )
        )
        output = loop.execute("ingest_inbox_run", {})
    finally:
        loop.registry.execute = original_execute
    assert calls == ["ingest_inbox_run"]
    assert "category=api_timeout" in output
    assert "ingest/retry" not in [event.type for event in loop.session_log.events()]


def test_structured_parse():
    loop = IngestAgentLoop()
    parsed = loop._parse_structured('prefix\n{"status":"completed","paper_id":"p1","transaction_id":"t1"}\nsuffix')
    assert parsed is not None
    assert parsed["status"] == "completed"
    assert parsed["paper_id"] == "p1"
    assert loop._parse_structured("inbox/ 无待摄入文件") is None


def test_structured_parse_preserves_top_level_batch_envelope():
    loop = IngestAgentLoop()
    payload = {
        "status": "partial",
        "phase": "paper_batch",
        "items": [{
            "file": "review.pdf",
            "status": "bibliographic_review_required",
            "transaction_id": "txn-review",
        }],
    }
    parsed = loop._parse_structured("progress\n" + json.dumps(payload) + "\ndone")
    assert parsed == payload
    assert parsed["items"][0]["status"] == "bibliographic_review_required"


def test_bibliographic_review_status_and_fields_are_preserved():
    loop = IngestAgentLoop()
    payload = {
        "status": "bibliographic_review_required",
        "transaction_id": "txn-review",
        "errors": ["authors 候选校验失败"],
        "retryable": False,
        "next_action": "repair_bibliographic_review_then_resume",
    }
    loop.last_structured = payload
    status, handoff = loop._status_from_last()
    assert status == "bibliographic_review_required"
    assert handoff == payload


def test_meeting_compiler_handoff_preserves_write_target():
    loop = IngestAgentLoop()
    loop.last_structured = {
        "status": "agent_required",
        "transaction_id": "20260903-142501-group-meeting",
        "message": "meeting compiler",
        "prompt": "compile once",
        "write_to": "temp/inbox-extract/txn/agent-meeting-compiler.txt",
        "pipeline_plan": [{"step": "会议编译"}],
    }
    status, handoff = loop._status_from_last()
    assert status == "agent_required"
    assert handoff["write_to"].endswith("agent-meeting-compiler.txt")
    assert handoff["message"] == "meeting compiler"


def test_completed_file_preserves_actionable_maintenance_handoff():
    loop = IngestAgentLoop()
    maintenance = {
        "status": "agent_required",
        "receipt_path": "temp/inbox-maintenance/session.json",
        "actions": [{"component": "abbreviations", "next_action": "agent_review"}],
        "components": {"abbreviations": {"status": "agent_required"}},
        "errors": [],
    }
    loop.last_structured = {"status": "completed", "maintenance": maintenance}
    result = loop._make_turn_result("done", "completed")
    assert result.status == "completed"
    assert result.handoff == {
        "status": "completed", "file_status": "completed", "maintenance": maintenance,
    }


def test_dispatch_loop():
    assert isinstance(dispatch_loop("摄入 inbox"), IngestAgentLoop)
    assert isinstance(dispatch_loop("DSH 查询 量子多体系统"), AgentLoop)


def test_run_inbox_empty():
    loop = IngestAgentLoop()
    inbox_dir = Path(__file__).resolve().parent.parent / "inbox"
    if any(p for p in inbox_dir.iterdir() if p.name not in {".gitkeep", ".DS_Store"}):
        print("  SKIP test_run_inbox_empty: inbox has files (external state)")
        return
    result = loop.run_inbox()
    assert result.status == "completed"
    assert "ingest/plan" in [e.type for e in loop.session_log.events()]


def test_run_paper_pdf_denied_becomes_failed():
    loop = IngestAgentLoop()
    result = loop.run_paper_pdf("../bad.pdf")
    assert result.status == "failed"
    assert result.handoff


def test_classify_error_categories():
    from dsh.ingest_tools import _classify_error
    assert _classify_error(1, "subprocess.TimeoutExpired", "") == "api_timeout"
    assert _classify_error(1, "connection timed out", "") == "api_timeout"
    assert _classify_error(1, "PDF extract failed: no text", "") == "extraction_failed"
    assert _classify_error(1, "semantic slot validation failed", "") == "semantic_failed"
    assert _classify_error(1, "graph write error: sqlite locked", "") == "graph_failed"
    assert _classify_error(1, "something unexpected", "") == "unknown"


def test_classify_error_prefers_structured_bibliographic_failure_over_pdf_name():
    from dsh.ingest_tools import _classify_error
    payload = json.dumps({
        "status": "failed",
        "errors": ["书目预审候选校验失败: rejected 包含未出现在作者候选项中的片段"],
        "transaction_id": "cccf-pdf",
    }, ensure_ascii=False)
    stdout = f"processing inbox/cccf.pdf\n{payload}"
    assert _classify_error(1, "", stdout) == "semantic_failed"


def test_classify_error_honors_explicit_structured_category():
    from dsh.ingest_tools import _classify_error
    payload = json.dumps({
        "status": "failed",
        "error_category": "graph_failed",
        "errors": ["PDF report attached"],
    })
    assert _classify_error(1, "", payload) == "graph_failed"


def test_classify_error_consumes_canonical_failure_disposition():
    from dsh.ingest_tools import _classify_error
    payload = json.dumps({
        "status": "failed",
        "errors": ["opaque failure"],
        "failure_disposition": {
            "category": "deterministic_validation",
            "domain": "graph",
            "disposition": "engineering_fix",
            "retryable": False,
            "owner": "engineering",
            "next_action": "repair_graph_then_resume",
        },
    })
    assert _classify_error(1, "", payload) == "graph_failed"


def test_error_output_includes_category():
    from dsh.ingest_tools import _ingest_call
    # _ingest_call with nonexistent script will produce non-zero return
    output = _ingest_call(["nonexistent_script.py"])
    assert output.startswith("[ERROR category=")
    assert "script=nonexistent_script.py" in output


def main():
    test_ingest_tool_names()
    test_guard_allows_inbox_files()
    test_guard_denies_unsafe_files()
    test_guard_requires_academic_document_type()
    test_document_tool_schema_accepts_academic_type()
    test_guard_validates_resume_txn()
    test_ingest_loop_has_guard_and_tools()
    test_ingest_loop_convenience_methods()
    test_structured_parse()
    test_structured_parse_preserves_top_level_batch_envelope()
    test_bibliographic_review_status_and_fields_are_preserved()
    test_meeting_compiler_handoff_preserves_write_target()
    test_completed_file_preserves_actionable_maintenance_handoff()
    test_structured_parse_deep_nested()
    test_structured_parse_prefers_status()
    test_structured_parse_ignores_nested_domain_status()
    test_dispatch_loop()
    test_run_inbox_empty()
    test_run_paper_pdf_denied_becomes_failed()
    test_classify_error_categories()
    test_classify_error_prefers_structured_bibliographic_failure_over_pdf_name()
    test_classify_error_honors_explicit_structured_category()
    test_classify_error_consumes_canonical_failure_disposition()
    test_error_output_includes_category()
    print("dsh ingest tools regression: PASS")




def test_structured_parse_deep_nested():
    """graph_report 含 3 层嵌套，旧正则只能匹配一层导致 structured=null。"""
    loop = IngestAgentLoop()
    nested = json.dumps({
        "status": "completed",
        "graph_report": {
            "graph_integrity_cleaned": {"orphan_aliases": 0, "orphan_edges": 0},
            "nested_deep": {"level2": {"level3": 42}},
        },
        "transaction_id": "t1",
    }, indent=2)
    content = f"prefix\n{nested}\n{{\"execution_duration_ms\":0}}"
    parsed = loop._parse_structured(content)
    assert parsed is not None
    assert parsed["status"] == "completed"
    assert parsed["graph_report"]["nested_deep"]["level2"]["level3"] == 42
    assert parsed["transaction_id"] == "t1"


def test_structured_parse_prefers_status():
    """多个 JSON 对象时优先返回含 status 的那个，而非第一个 fallback。"""
    loop = IngestAgentLoop()
    content = '{"metric":1}\n{"status":"failed","errors":["bad"]}'
    parsed = loop._parse_structured(content)
    assert parsed is not None
    assert parsed["status"] == "failed"
    assert parsed["errors"] == ["bad"]


def test_structured_parse_ignores_nested_domain_status():
    """Hub 的 active 状态不能遮蔽后面的 completed 工作流终态。"""
    loop = IngestAgentLoop()
    graph_report = json.dumps({
        "hub_scope_route": {
            "candidates": [{"path": "academic/wiki/hubs/信息检索", "status": "active"}],
        },
        "quality_status": "complete",
    }, ensure_ascii=False, indent=2)
    final_result = json.dumps({
        "status": "completed",
        "paper_id": "joren-2025-sufficient-context-new-lens",
        "transaction_id": "t1",
    }, ensure_ascii=False, indent=2)
    parsed = loop._parse_structured(f"receipt committed\n{graph_report}\n{final_result}\nprogress log")
    assert parsed is not None
    assert parsed["status"] == "completed"
    assert parsed["paper_id"] == "joren-2025-sufficient-context-new-lens"

if __name__ == "__main__":
    main()

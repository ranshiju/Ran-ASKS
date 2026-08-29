#!/usr/bin/env python3
"""dsh/ 摄入工具 seam 回归测试。

验证点：
- ingest 工具注册表包含 dry-run/run/run-file/paper-pdf/paper-resume
- IngestGuard 只放行 inbox 文件与合法事务 ID，拒绝路径穿越/越界/空参数
- IngestAgentLoop 挂载 ingest guard，不挂载查询 guard
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.harness import ToolExecution, PreToolDecision
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
        name="re_ingest_raw", arguments={"raw": "academic/raw/references/x/paper.md"})) is None


def test_guard_denies_unsafe_files():
    guard = IngestGuard()
    for args in ({"file": "../a.pdf"}, {"file": "/etc/passwd"}, {"file": ""}):
        decision = guard.on_pre_execute(ToolExecution(name="ingest_inbox_run_file", arguments=args))
        assert decision is not None
        assert decision.kind == "deny"


def test_guard_validates_resume_txn():
    guard = IngestGuard()
    valid = guard.on_pre_execute(ToolExecution(
        name="ingest_paper_resume", arguments={"txn": "20260820-152247-663433-xkh7-gdqm"}))
    assert valid is None
    invalid = guard.on_pre_execute(ToolExecution(
        name="ingest_paper_resume", arguments={"txn": "../../etc"}))
    assert invalid is not None
    assert invalid.kind == "deny"


def test_ingest_loop_has_guard_and_tools():
    loop = IngestAgentLoop()
    assert "ingest_inbox_dry_run" in loop.registry.names()
    assert len(loop.registry._pre_execute) == 1


def test_ingest_loop_convenience_methods():
    loop = IngestAgentLoop()
    for name in ("ingest_meeting", "ingest_document", "re_ingest_raw",
                 "ingest_paper_inbox", "execute"):
        assert hasattr(loop, name)


def test_structured_parse():
    loop = IngestAgentLoop()
    parsed = loop._parse_structured('prefix\n{"status":"completed","paper_id":"p1","transaction_id":"t1"}\nsuffix')
    assert parsed is not None
    assert parsed["status"] == "completed"
    assert parsed["paper_id"] == "p1"
    assert loop._parse_structured("inbox/ 无待摄入文件") is None


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
    test_guard_validates_resume_txn()
    test_ingest_loop_has_guard_and_tools()
    test_ingest_loop_convenience_methods()
    test_structured_parse()
    test_structured_parse_deep_nested()
    test_structured_parse_prefers_status()
    test_structured_parse_ignores_nested_domain_status()
    test_dispatch_loop()
    test_run_inbox_empty()
    test_run_paper_pdf_denied_becomes_failed()
    test_classify_error_categories()
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

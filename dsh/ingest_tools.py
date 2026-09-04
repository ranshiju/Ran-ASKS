"""ingest_tools.py — 将 WikiGraph 摄入能力包装为 DSH 注册工具。

DSH ingest capability seam：ToolDefinition(schema + execute_fn) → Consumer(ingest loop)。
所有工具只经子进程调用底层摄入脚本，保持 quiet JSON、独立事务与返回码分支；
DSH 不重写摄入状态机，也不直接写 raw/wiki/graph.db。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"

_CONTROL_FLOW_STATUSES = frozenset({
    "agent_required", "bibliographic_review_required", "classification_required",
    "duplicate_found", "graph_ready", "partial", "deferred", "completed",
})
sys.path.insert(0, str(SCRIPTS))

from dsh.harness import ToolDefinition
import inbox_state


_ERROR_CATEGORIES = {
    "api_timeout", "extraction_failed", "semantic_failed", "graph_failed", "unknown",
}


def _json_objects(text: str) -> list[dict]:
    """Decode complete JSON objects embedded in quiet/progress output."""
    decoder = json.JSONDecoder()
    objects = []
    cursor = 0
    while cursor < len(text):
        start = text.find("{", cursor)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        if isinstance(value, dict):
            objects.append(value)
        cursor = end
    return objects


def _classify_error_text(text: str) -> str:
    text = text.lower()
    if any(p in text for p in ("timeout", "timed out", "timeoutexpired",
                               "connection timeout", "read timeout", "api timeout")):
        return "api_timeout"
    if any(p in text for p in ("bibliographic", "书目", "预审", "semantic", "slot",
                               "wiki validation", "wiki 校验", "agent_required", "handoff")):
        return "semantic_failed"
    if any(p in text for p in ("graph", "sqlite", "edge", "keyword", "hub")):
        return "graph_failed"
    if any(p in text for p in ("extract failed", "extraction failed", "提取失败",
                               "mineru failed", "ocr failed", "empty content",
                               "no extracted text", "no text extracted", "scanned document")):
        return "extraction_failed"
    return "unknown"


def _dsh_category(failure: dict | None) -> str:
    if not failure:
        return "unknown"
    domain = str(failure.get("domain") or "")
    category = str(failure.get("category") or "")
    if domain == "api" and category in {"api_rate_limit", "api_network_transient"}:
        return "api_timeout"
    if domain == "extraction":
        return "extraction_failed"
    if domain == "graph":
        return "graph_failed"
    if domain in {"semantic", "worker", "policy"}:
        return "semantic_failed"
    return "unknown"


def _structured_failure(stderr: str, stdout: str) -> dict | None:
    combined = f"{stderr}\n{stdout}"
    for payload in reversed(_json_objects(combined)):
        explicit_failure = payload.get("failure_disposition") or payload.get("failure")
        if isinstance(explicit_failure, dict) and explicit_failure.get("category"):
            return explicit_failure
        failure = inbox_state.classify_failure(payload)
        if failure:
            return failure
    return None


def _structured_workflow_status(stderr: str, stdout: str) -> str:
    """Return the last top-level ingest workflow status, if present."""
    combined = f"{stderr}\n{stdout}"
    for payload in reversed(_json_objects(combined)):
        status = str(payload.get("status") or "").strip().lower()
        if status:
            return status
    return ""


def _classify_error(returncode: int, stderr: str, stdout: str) -> str:
    """Prefer structured terminal state; use narrow text patterns as fallback."""
    combined = f"{stderr}\n{stdout}"
    for payload in reversed(_json_objects(combined)):
        status = str(payload.get("status", "")).strip().lower()
        if not status:
            continue
        explicit = str(payload.get("error_category") or payload.get("category") or "").strip()
        if explicit in _ERROR_CATEGORIES:
            return explicit
        if status in _ERROR_CATEGORIES:
            return status
        explicit_failure = payload.get("failure_disposition") or payload.get("failure")
        mapped = _dsh_category(explicit_failure if isinstance(explicit_failure, dict) else None)
        if mapped != "unknown":
            return mapped
        canonical = inbox_state.classify_failure(payload)
        mapped = _dsh_category(canonical)
        if mapped != "unknown":
            return mapped
        if status in {"agent_required", "bibliographic_review_required", "type_mismatch"}:
            return "semantic_failed"
        errors = payload.get("errors", [])
        if isinstance(errors, str):
            errors = [errors]
        structured = _classify_error_text("\n".join(str(item) for item in errors))
        if structured != "unknown":
            return structured
        if status in {"failed", "error", "validation_failed"}:
            return "unknown"
    return _classify_error_text(combined)


def _ingest_call(args: list[str], timeout: int = 1800) -> str:
    """调用底层摄入脚本，返回 stdout+stderr 的合并文本。"""
    cmd = ["python3", str(SCRIPTS / args[0]), *args[1:]]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    out = p.stdout or ""
    err = p.stderr or ""
    if p.returncode != 0:
        if _structured_workflow_status(err, out) in _CONTROL_FLOW_STATUSES:
            return f"{out}\n{err}".strip()
        category = _classify_error(p.returncode, err, out)
        failure = _structured_failure(err, out)
        disposition = (
            "\n" + json.dumps({"failure_disposition": failure}, ensure_ascii=False)
            if failure else ""
        )
        return f"[ERROR category={category} script={args[0]} code={p.returncode}]{disposition}\n{err}\n{out}".strip()
    return f"{out}\n{err}".strip()


def build_ingest_tools() -> list[ToolDefinition]:
    """构建 DSH 摄入工具。"""
    return [
        ToolDefinition(
            name="ingest_inbox_dry_run",
            description="扫描 inbox 并返回自动分类表（不执行摄入、不落库）",
            input_schema={"type": "object", "properties": {}, "required": []},
            execute_fn=lambda args: _ingest_call(["ingest_inbox.py"]),
        ),
        ToolDefinition(
            name="ingest_inbox_run",
            description="扫描并摄入 inbox 全部文件，返回逐文件 JSON 汇总",
            input_schema={"type": "object", "properties": {}, "required": []},
            execute_fn=lambda args: _ingest_call(["ingest_inbox.py", "--run"]),
        ),
        ToolDefinition(
            name="ingest_inbox_run_file",
            description="摄入 inbox 下单个文件，返回该文件的事务 JSON",
            input_schema={"type": "object", "properties": {
                "file": {"type": "string", "description": "inbox/ 下相对路径"}},
                "required": ["file"]},
            execute_fn=lambda args: _ingest_call(["ingest_inbox.py", "--run", "--file", args.get("file", "")]),
        ),
        ToolDefinition(
            name="ingest_paper_inbox",
            description="inbox 下全部学术 PDF 批量摄入（ingest_paper.py --inbox，两阶段 prepare/commit）",
            input_schema={"type": "object", "properties": {}, "required": []},
            execute_fn=lambda args: _ingest_call(["ingest_paper.py", "--inbox"]),
        ),
        ToolDefinition(
            name="ingest_paper_pdf",
            description="单篇论文 PDF 摄入（ingest_paper.py --pdf）",
            input_schema={"type": "object", "properties": {
                "pdf": {"type": "string", "description": "inbox/ 下 PDF 相对路径"}},
                "required": ["pdf"]},
            execute_fn=lambda args: _ingest_call(["ingest_paper.py", "--pdf", args.get("pdf", "")]),
        ),
        ToolDefinition(
            name="ingest_paper_resume",
            description="恢复 ingest_paper 事务（agent_required 或失败修正后）",
            input_schema={"type": "object", "properties": {
                "txn": {"type": "string", "description": "事务 ID"}},
                "required": ["txn"]},
            execute_fn=lambda args: _ingest_call(["ingest_paper.py", "--resume", args.get("txn", "")]),
        ),
        ToolDefinition(
            name="ingest_meeting_txt",
            description="摄入 inbox 下会议纪要 .txt（ingest_meeting.py --txt）",
            input_schema={"type": "object", "properties": {
                "file": {"type": "string", "description": "inbox/ 下 .txt 路径"},
                "subproject": {"type": "string", "description": "academic/admin/business"}},
                "required": ["file"]},
            execute_fn=lambda args: _ingest_call(["ingest_meeting.py", "--txt", args.get("file", "")]
                + (["--subproject", args["subproject"]] if args.get("subproject") else [])),
        ),
        ToolDefinition(
            name="ingest_meeting_resume",
            description="Meeting Compiler 输出写入 write_to 后恢复同一会议摄入事务",
            input_schema={"type": "object", "properties": {
                "txn": {"type": "string", "description": "事务 ID"}},
                "required": ["txn"]},
            execute_fn=lambda args: _ingest_call([
                "ingest_meeting.py", "--resume", args.get("txn", "")]),
        ),
        ToolDefinition(
            name="ingest_document_file",
            description="摄入 inbox 下通用文档；academic 必须显式指定 editorial 或 academic-reference",
            input_schema={"type": "object", "properties": {
                "file": {"type": "string", "description": "inbox/ 下文档路径"},
                "subproject": {"type": "string", "description": "academic/admin/teaching/business"},
                "document_type": {"type": "string", "enum": ["editorial", "academic-reference"],
                                  "description": "academic 非论文类型"},
                "source_kind": {"type": "string", "enum": ["ordinary", "meeting"],
                                "description": "inbox 程序判定的来源种类"}},
                "required": ["file"]},
            execute_fn=lambda args: _ingest_call(["ingest_document.py", "--file", args.get("file", "")]
                + (["--subproject", args["subproject"]] if args.get("subproject") else [])
                + (["--document-type", args["document_type"]] if args.get("document_type") else [])
                + (["--source-kind", args["source_kind"]] if args.get("source_kind") else [])),
        ),
        ToolDefinition(
            name="re_ingest_raw",
            description="重新摄入已入库 raw 论文（re_ingest.py --raw）",
            input_schema={"type": "object", "properties": {
                "raw": {"type": "string", "description": "academic/raw 下 paper.md 或目录路径"}},
                "required": ["raw"]},
            execute_fn=lambda args: _ingest_call(["re_ingest.py", "--raw", args.get("raw", "")]),
        ),
    ]

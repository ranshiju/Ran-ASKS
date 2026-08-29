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
sys.path.insert(0, str(SCRIPTS))

from dsh.harness import ToolDefinition


def _classify_error(returncode: int, stderr: str, stdout: str) -> str:
    """按 stderr/stdout 模式分类错误，供 DSH 层决策（重试/handoff）。

    分类优先级：api_timeout > extraction_failed > semantic_failed > graph_failed > unknown。
    api_timeout 是唯一可自动重试类别；其余交 agent 或用户处理。
    """
    text = f"{stderr}\n{stdout}".lower()
    if any(p in text for p in ("timeout", "timed out", "timeoutexpired",
                               "connection timeout", "read timeout", "api timeout")):
        return "api_timeout"
    if any(p in text for p in ("extract", "mineru", "ocr", "pdf", "empty content",
                               "no text", "blank", "scanned")):
        return "extraction_failed"
    if any(p in text for p in ("semantic", "slot", "wiki", "validate", "repair",
                               "agent_required", "handoff")):
        return "semantic_failed"
    if any(p in text for p in ("graph", "sqlite", "edge", "keyword", "hub")):
        return "graph_failed"
    return "unknown"


def _ingest_call(args: list[str], timeout: int = 1800) -> str:
    """调用底层摄入脚本，返回 stdout+stderr 的合并文本。"""
    cmd = ["python3", str(SCRIPTS / args[0]), *args[1:]]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    out = p.stdout or ""
    err = p.stderr or ""
    if p.returncode != 0:
        category = _classify_error(p.returncode, err, out)
        return f"[ERROR category={category} script={args[0]} code={p.returncode}]\n{err}\n{out}".strip()
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
            name="ingest_document_file",
            description="摄入 inbox 下通用文档（ingest_document.py --file）",
            input_schema={"type": "object", "properties": {
                "file": {"type": "string", "description": "inbox/ 下文档路径"},
                "subproject": {"type": "string", "description": "admin/teaching/business"}},
                "required": ["file"]},
            execute_fn=lambda args: _ingest_call(["ingest_document.py", "--file", args.get("file", "")]
                + (["--subproject", args["subproject"]] if args.get("subproject") else [])),
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

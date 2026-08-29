"""ingest_guard.py — DSH 摄入工具的 pre-execute guard。

守卫目标：
- 摄入入口只允许 inbox/ 下文件或合法事务 ID
- 不放行 raw 路径写入、参数穿越或 dry-run 语义绕过
- 不替代底层摄入校验，只做 DSH 层的参数白名单
"""
from __future__ import annotations

import re
from pathlib import Path

from dsh.harness import PreToolDecision, ToolExecution

REPO = Path(__file__).resolve().parent.parent.parent

_TXN_RE = re.compile(r"^\d{8}-\d{6}-\d+-[A-Za-z0-9_-]+$")
_ALLOWED_FILE_TOOLS = {"ingest_inbox_run_file", "ingest_paper_pdf", "ingest_meeting_txt", "ingest_document_file"}


class IngestGuard:
    """摄入工具 pre-execute 参数守卫。"""

    def on_pre_execute(self, exec_ctx: ToolExecution) -> PreToolDecision | None:
        if not exec_ctx.name.startswith("ingest_"):
            return None

        args = exec_ctx.arguments or {}

        if exec_ctx.name in _ALLOWED_FILE_TOOLS:
            key = "pdf" if exec_ctx.name == "ingest_paper_pdf" else "file"
            raw = (args.get(key) or "").strip()
            if not raw:
                return PreToolDecision(kind="deny", reason=f"{key} 不能为空")
            if raw != raw.lstrip("./") or ".." in Path(raw).parts:
                return PreToolDecision(kind="deny", reason=f"{key} 不允许路径穿越: {raw}")
            resolved = (REPO / raw).resolve()
            try:
                resolved.relative_to((REPO / "inbox").resolve())
            except ValueError:
                return PreToolDecision(kind="deny", reason=f"{key} 必须位于 inbox/: {raw}")
            return None

        if exec_ctx.name == "re_ingest_raw":
            raw = (args.get("raw") or "").strip()
            if not raw:
                return PreToolDecision(kind="deny", reason="raw 不能为空")
            if raw != raw.lstrip("./") or ".." in Path(raw).parts:
                return PreToolDecision(kind="deny", reason=f"raw 不允许路径穿越: {raw}")
            resolved = (REPO / raw).resolve()
            try:
                resolved.relative_to((REPO / "academic" / "raw").resolve())
            except ValueError:
                return PreToolDecision(kind="deny", reason=f"raw 必须位于 academic/raw/: {raw}")
            return None

        if exec_ctx.name == "ingest_paper_resume":
            txn = (args.get("txn") or "").strip()
            if not _TXN_RE.match(txn):
                return PreToolDecision(kind="deny", reason=f"非法事务 ID: {txn}")
            return None

        return None

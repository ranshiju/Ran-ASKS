"""Dedicated DSH loop for visual artifact QA.

This loop intentionally does not inherit the fact-query CitationGuard.  It has
one capability, ``visual_check``, and records only the invocation plus compact
summary in the in-memory DSH SessionLog.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dsh.harness import SessionLog, ToolExecution, ToolRegistry
from dsh.visual_tools import build_visual_tools


VISUAL_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".pdf", ".ppt", ".pptx"}


@dataclass
class VisualTurnResult:
    session_id: str
    status: str = "completed"
    verdict: str = "not_checked"
    summary: dict[str, Any] = field(default_factory=dict)
    handoff: dict[str, Any] | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


def _path_from_intent(intent: str) -> str | None:
    """Best-effort extraction for API/direct workflows; quoted paths are safest."""
    try:
        tokens = shlex.split(intent)
    except ValueError:
        tokens = intent.split()
    for token in reversed(tokens):
        cleaned = token.strip("，。；;：:、()（）[]【】")
        if Path(cleaned).suffix.lower() in VISUAL_SUFFIXES:
            return cleaned
    return None


class VisualAgentLoop:
    """Visual-only tool loop with explicit, auditable invocation."""

    def __init__(self, mode: str = "api"):
        if mode != "api":
            raise ValueError("DSH VisualAgentLoop 仅支持 API backend")
        self.mode = mode
        self.registry = ToolRegistry()
        self.session_log = SessionLog()
        for tool in build_visual_tools():
            self.registry.register(tool)

    def execute(self, arguments: dict[str, Any]) -> VisualTurnResult:
        self.session_log.append("turn/start", {
            "workflow": "visual_qa",
            "mode": self.mode,
            "path": arguments.get("path", ""),
        })
        result = self.registry.execute(
            ToolExecution(
                name="visual_check",
                arguments=arguments,
                agent_id=self.session_log.session_id,
            ),
            self.session_log,
        )
        if result.is_error:
            self.session_log.append("turn/end", {"reason": "tool_error"})
            return VisualTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={"status": "failed", "error": result.content[:500]},
                snapshot=self._snapshot(),
            )
        try:
            summary = json.loads(result.content)
        except json.JSONDecodeError:
            self.session_log.append("turn/end", {"reason": "invalid_tool_result"})
            return VisualTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={"status": "failed", "error": "visual_check returned invalid JSON"},
                snapshot=self._snapshot(),
            )
        self.session_log.append("turn/end", {
            "reason": "visual_qa_completed",
            "status": summary.get("status"),
            "verdict": summary.get("verdict"),
            "summary_path": summary.get("summary_path"),
        })
        return VisualTurnResult(
            session_id=self.session_log.session_id,
            status=str(summary.get("status", "completed")),
            verdict=str(summary.get("verdict", "not_checked")),
            summary=summary,
            snapshot=self._snapshot(),
        )

    def run(self, intent: str, *, path: str | None = None, **arguments: Any) -> VisualTurnResult:
        """Execute an API visual intent when its artifact path is available."""
        self.session_log.append("user/message", {"role": "user", "content": intent})
        artifact_path = path or _path_from_intent(intent)
        if not artifact_path:
            return VisualTurnResult(
                session_id=self.session_log.session_id,
                status="agent_required",
                handoff={
                    "status": "agent_required",
                    "reason": "artifact_path_required",
                    "tool": "visual_check",
                    "tool_schema": self.registry.schemas()[0],
                },
                snapshot=self._snapshot(),
            )
        return self.execute({"path": artifact_path, **arguments})

    def _snapshot(self) -> dict[str, Any]:
        return {
            "events": len(self.session_log.events()),
            "tool_names": self.registry.names(),
        }

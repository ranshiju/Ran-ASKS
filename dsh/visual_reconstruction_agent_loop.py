"""Dedicated DSH loop for explicit visual-to-editable-PPT reconstruction.

The loop is separate from the read-only VisualAgentLoop because it writes a new
deliverable. It never registers query or ingest tools and records only compact
parameters/results in the in-memory SessionLog.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dsh.harness import SessionLog, ToolExecution, ToolRegistry
from dsh.visual_reconstruction_tools import build_visual_reconstruction_tools


RECONSTRUCTION_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf"
}


@dataclass
class VisualReconstructionTurnResult:
    session_id: str
    status: str = "completed"
    summary: dict[str, Any] = field(default_factory=dict)
    handoff: dict[str, Any] | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)


def _path_from_intent(intent: str) -> str | None:
    try:
        tokens = shlex.split(intent)
    except ValueError:
        tokens = intent.split()
    for token in reversed(tokens):
        cleaned = token.strip("，。；;：:、()（）[]【】")
        if Path(cleaned).suffix.lower() in RECONSTRUCTION_SUFFIXES:
            return cleaned
    return None


class VisualReconstructionAgentLoop:
    """Write-capable visual reconstruction loop with an explicit tool seam."""

    def __init__(self, mode: str = "agent"):
        self.mode = mode
        self.registry = ToolRegistry()
        self.session_log = SessionLog()
        for tool in build_visual_reconstruction_tools():
            self.registry.register(tool)

    def execute(self, arguments: dict[str, Any]) -> VisualReconstructionTurnResult:
        self.session_log.append("turn/start", {
            "workflow": "visual_reconstruction",
            "mode": self.mode,
            "path": arguments.get("path", ""),
            "output_path": arguments.get("output_path", ""),
        })
        result = self.registry.execute(
            ToolExecution(
                name="visual_to_editable_ppt",
                arguments=arguments,
                agent_id=self.session_log.session_id,
            ),
            self.session_log,
        )
        if result.is_error:
            self.session_log.append("turn/end", {"reason": "tool_error"})
            return VisualReconstructionTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={"status": "failed", "error": result.content[:500]},
                snapshot=self._snapshot(),
            )
        try:
            summary = json.loads(result.content)
        except json.JSONDecodeError:
            self.session_log.append("turn/end", {"reason": "invalid_tool_result"})
            return VisualReconstructionTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={
                    "status": "failed",
                    "error": "visual_to_editable_ppt returned invalid JSON",
                },
                snapshot=self._snapshot(),
            )
        self.session_log.append("turn/end", {
            "reason": "visual_reconstruction_completed",
            "status": summary.get("status"),
            "output": summary.get("output"),
            "fully_editable": summary.get("fully_editable"),
            "fallback_objects": summary.get("fallback_objects"),
        })
        return VisualReconstructionTurnResult(
            session_id=self.session_log.session_id,
            status=str(summary.get("status", "completed")),
            summary=summary,
            snapshot=self._snapshot(),
        )

    def run(
        self,
        intent: str,
        *,
        path: str | None = None,
        output_path: str | None = None,
        **arguments: Any,
    ) -> VisualReconstructionTurnResult:
        self.session_log.append("user/message", {"role": "user", "content": intent})
        artifact_path = path or _path_from_intent(intent)
        if self.mode == "agent":
            return VisualReconstructionTurnResult(
                session_id=self.session_log.session_id,
                status="agent_required",
                handoff={
                    "status": "agent_required",
                    "tool": "visual_to_editable_ppt",
                    "path_candidate": artifact_path or "",
                    "output_path_candidate": output_path or "",
                    "tool_schema": self.registry.schemas()[0],
                    "instruction": (
                        "确认源路径、输出路径、覆盖权限和远程上传权限后调用 "
                        "visual_to_editable_ppt"
                    ),
                },
                snapshot=self._snapshot(),
            )
        if not artifact_path:
            return VisualReconstructionTurnResult(
                session_id=self.session_log.session_id,
                status="agent_required",
                handoff={
                    "status": "agent_required",
                    "reason": "artifact_path_required",
                    "tool": "visual_to_editable_ppt",
                    "tool_schema": self.registry.schemas()[0],
                },
                snapshot=self._snapshot(),
            )
        call_arguments = {"path": artifact_path, **arguments}
        if output_path:
            call_arguments["output_path"] = output_path
        return self.execute(call_arguments)

    def _snapshot(self) -> dict[str, Any]:
        return {
            "events": len(self.session_log.events()),
            "tool_names": self.registry.names(),
        }

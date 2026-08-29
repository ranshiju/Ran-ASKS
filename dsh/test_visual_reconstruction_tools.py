#!/usr/bin/env python3
"""Regression tests for DSH visual reconstruction registration and routing."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import fitz

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.agent_loop import AgentLoop, IngestAgentLoop
from dsh.dispatch import dispatch_loop
from dsh.harness import SessionLog, ToolExecution, ToolRegistry
from dsh.visual_agent_loop import VisualAgentLoop
from dsh.visual_reconstruction_agent_loop import VisualReconstructionAgentLoop
from dsh.visual_reconstruction_tools import build_visual_reconstruction_tools


def _vector_pdf(path: Path) -> None:
    with fitz.open() as document:
        page = document.new_page(width=640, height=360)
        page.draw_rect(
            fitz.Rect(70, 100, 270, 290),
            color=(0.1, 0.3, 0.8), fill=(0.75, 0.85, 0.97), width=2,
        )
        page.draw_circle(
            fitz.Point(450, 195), 70,
            color=(0.8, 0.2, 0.2), fill=(0.97, 0.75, 0.75), width=2,
        )
        page.insert_text((90, 70), "EDITABLE VECTOR", fontsize=24)
        document.save(path)


def test_tool_schema() -> None:
    tools = build_visual_reconstruction_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "visual_to_editable_ppt"
    assert tool.input_schema["required"] == ["path"]
    properties = tool.input_schema["properties"]
    assert properties["mode"]["enum"] == ["faithful", "balanced", "editable"]
    assert "output_path" in properties
    assert "overwrite" in properties
    assert "allow_remote" in properties
    assert "deterministic_only" in properties


def test_tool_executes_without_modifying_source(tmp: Path) -> None:
    source = tmp / "source.pdf"
    output = tmp / "output.pptx"
    _vector_pdf(source)
    before = source.read_bytes()
    registry = ToolRegistry()
    registry.register(build_visual_reconstruction_tools()[0])
    result = registry.execute(
        ToolExecution(name="visual_to_editable_ppt", arguments={
            "path": str(source),
            "output_path": str(output),
            "receipt_root": str(tmp / "receipts"),
            "deterministic_only": True,
        }),
        SessionLog(),
    )
    assert result.is_error is False
    parsed = json.loads(result.content)
    assert parsed["status"] == "complete"
    assert parsed["fully_editable"] is True
    assert parsed["assembly"]["picture_elements"] == 0
    assert output.is_file()
    assert source.read_bytes() == before


def test_loop_is_isolated_and_runs(tmp: Path) -> None:
    source = tmp / "loop.pdf"
    output = tmp / "loop.pptx"
    _vector_pdf(source)
    loop = VisualReconstructionAgentLoop(mode="api")
    assert loop.registry.names() == ["visual_to_editable_ppt"]
    assert len(loop.registry._post_execute) == 0
    result = loop.run(
        f'把 "{source}" 转换为可编辑 PPT',
        output_path=str(output),
        receipt_root=str(tmp / "loop-receipts"),
        deterministic_only=True,
    )
    assert result.status == "complete"
    assert result.summary["fully_editable"] is True
    assert output.is_file()


def test_agent_mode_handoff(tmp: Path) -> None:
    source = tmp / "handoff.pdf"
    _vector_pdf(source)
    loop = VisualReconstructionAgentLoop(mode="agent")
    result = loop.run(f'严格复刻 "{source}" 为可编辑ppt')
    assert result.status == "agent_required"
    assert result.handoff
    assert result.handoff["tool"] == "visual_to_editable_ppt"
    assert result.handoff["path_candidate"] == str(source)
    assert "覆盖权限" in result.handoff["instruction"]


def test_dispatch_is_specific() -> None:
    assert isinstance(dispatch_loop("把图片转换成可编辑ppt"), VisualReconstructionAgentLoop)
    assert isinstance(dispatch_loop("PDF 转 PPT 并对象化"), VisualReconstructionAgentLoop)
    assert isinstance(dispatch_loop("严格复刻成 ppt"), VisualReconstructionAgentLoop)
    assert isinstance(dispatch_loop("视觉检查这张图"), VisualAgentLoop)
    assert isinstance(dispatch_loop("检查 PDF 页面排版"), VisualAgentLoop)
    assert isinstance(dispatch_loop("摄入 inbox 中的论文 PDF"), IngestAgentLoop)
    assert isinstance(dispatch_loop("查询这篇论文 PDF 的作者"), AgentLoop)


def main() -> None:
    test_tool_schema()
    (REPO / "temp").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="dsh-visual-reconstruction-test-", dir=REPO / "temp"
    ) as tmp_dir:
        tmp = Path(tmp_dir)
        test_tool_executes_without_modifying_source(tmp)
        test_loop_is_isolated_and_runs(tmp)
        test_agent_mode_handoff(tmp)
    test_dispatch_is_specific()
    print("dsh visual reconstruction tools regression: PASS")


if __name__ == "__main__":
    main()

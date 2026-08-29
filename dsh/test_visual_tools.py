#!/usr/bin/env python3
"""Regression tests for DSH visual tool registration and intent routing."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.agent_loop import AgentLoop, IngestAgentLoop
from dsh.dispatch import dispatch_loop
from dsh.harness import SessionLog, ToolExecution, ToolRegistry
from dsh.visual_agent_loop import VisualAgentLoop
from dsh.visual_tools import build_visual_tools


def _image(path: Path) -> None:
    image = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 100, 1100, 700), fill=(235, 240, 250), outline="black", width=3)
    draw.rectangle((220, 220, 520, 600), fill=(50, 110, 190))
    draw.rectangle((650, 300, 1000, 600), fill=(210, 90, 70))
    image.save(path)


def test_visual_tool_schema() -> None:
    tools = build_visual_tools()
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "visual_check"
    schema = tool.input_schema
    assert schema["required"] == ["path"]
    assert "deterministic_only" in schema["properties"]
    assert "allow_remote" in schema["properties"]


def test_visual_tool_executes_deterministic_check(tmp: Path) -> None:
    artifact = tmp / "figure.png"
    _image(artifact)
    registry = ToolRegistry()
    registry.register(build_visual_tools()[0])
    result = registry.execute(
        ToolExecution(name="visual_check", arguments={
            "path": str(artifact),
            "deterministic_only": True,
            "receipt_root": str(tmp / "receipts"),
        }),
        SessionLog(),
    )
    assert result.is_error is False
    parsed = json.loads(result.content)
    assert parsed["status"] == "completed"
    assert parsed["checked_pages"] == 1
    assert Path(parsed["summary_path"]).is_file()


def test_visual_loop_is_isolated_and_runs(tmp: Path) -> None:
    artifact = tmp / "figure.png"
    _image(artifact)
    loop = VisualAgentLoop(mode="api")
    assert loop.registry.names() == ["visual_check"]
    assert len(loop.registry._post_execute) == 0
    result = loop.run(
        f'视觉检查 "{artifact}"',
        deterministic_only=True,
        receipt_root=str(tmp / "loop-receipts"),
    )
    assert result.status == "completed"
    assert result.summary["checked_pages"] == 1
    assert "visual_check" in result.snapshot["tool_names"]


def test_agent_mode_handoff(tmp: Path) -> None:
    artifact = tmp / "figure.png"
    _image(artifact)
    loop = VisualAgentLoop(mode="agent")
    result = loop.run(f'请做视觉 QA "{artifact}"')
    assert result.status == "agent_required"
    assert result.handoff
    assert result.handoff["tool"] == "visual_check"
    assert result.handoff["path_candidate"] == str(artifact)
    assert "修改指令依赖可见状态" in result.handoff["instruction"]


def test_dispatch_is_specific() -> None:
    assert isinstance(dispatch_loop("视觉检查这张图"), VisualAgentLoop)
    assert isinstance(dispatch_loop("检查 PDF 页面排版"), VisualAgentLoop)
    assert isinstance(dispatch_loop("幻灯片质检"), VisualAgentLoop)
    assert isinstance(dispatch_loop("把图1的图例往下移动一点"), VisualAgentLoop)
    assert isinstance(dispatch_loop("把图2往左移动并缩小一点"), VisualAgentLoop)
    assert isinstance(dispatch_loop("调整图3的配色"), VisualAgentLoop)
    assert isinstance(dispatch_loop("第10页有大块空白，让后面的正文移动上去"), VisualAgentLoop)
    assert isinstance(dispatch_loop("把 PPT 第3页的标题字号调大并和图形对齐"), VisualAgentLoop)
    assert isinstance(dispatch_loop("Move the figure legend down and fix the spacing"), VisualAgentLoop)
    assert isinstance(dispatch_loop("摄入 inbox 中的论文 PDF"), IngestAgentLoop)
    assert isinstance(dispatch_loop("查询这篇论文 PDF 的作者"), AgentLoop)
    assert isinstance(dispatch_loop("研究张量网络历史"), AgentLoop)
    assert isinstance(dispatch_loop("修改论文摘要的第三句话"), AgentLoop)
    assert not isinstance(dispatch_loop("编译论文 PDF"), VisualAgentLoop)


def main() -> None:
    test_visual_tool_schema()
    with tempfile.TemporaryDirectory(prefix="dsh-visual-test-") as tmp_dir:
        tmp = Path(tmp_dir)
        test_visual_tool_executes_deterministic_check(tmp)
        test_visual_loop_is_isolated_and_runs(tmp)
        test_agent_mode_handoff(tmp)
    test_dispatch_is_specific()
    print("dsh visual tools regression: PASS")


if __name__ == "__main__":
    main()

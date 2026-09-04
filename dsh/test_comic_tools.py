#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".scripts"))

from dsh.comic_tools import build_comic_tools
from dsh.harness import SessionLog, ToolExecution, ToolRegistry
from dsh.tools import build_tools


def test_comic_tool_schemas() -> None:
    tools = build_comic_tools()
    assert [tool.name for tool in tools] == ["comic_models", "comic_generate", "comic_batch"]
    generate = tools[1].input_schema
    assert generate["required"] == ["article_id", "asset_id", "prompt", "model"]
    assert "allow_remote" in generate["properties"]
    assert "oneOf" in generate


def test_query_tools_do_not_gain_write_capability() -> None:
    assert "comic_generate" not in [tool.name for tool in build_tools()]
    assert "comic_batch" not in [tool.name for tool in build_tools()]


def test_dry_run_arguments_reach_shared_generator() -> None:
    tool = build_comic_tools()[1]
    registry = ToolRegistry()
    registry.register(tool)
    expected = {"status": "dry_run", "output_directory": "/repo/projects/demo/outputs/a/images"}
    with mock.patch("dsh.comic_tools.generate_asset", return_value=expected) as generate:
        result = registry.execute(
            ToolExecution(
                name="comic_generate",
                arguments={
                    "project": "demo",
                    "article_id": "a",
                    "asset_id": "cover",
                    "prompt": "one concept",
                    "model": "Test-Image",
                    "dry_run": True,
                },
            ),
            SessionLog(),
        )
    assert not result.is_error
    assert json.loads(result.content)["status"] == "dry_run"
    assert generate.call_args.kwargs["allow_remote"] is False
    assert generate.call_args.kwargs["dry_run"] is True


def test_batch_uses_explicit_output_root() -> None:
    tool = build_comic_tools()[2]
    with tempfile.TemporaryDirectory() as directory:
        storyboard = Path(directory) / "storyboard.yaml"
        storyboard.write_text("article_id: a\nassets: []\n", encoding="utf-8")
        expected = {"status": "dry_run", "assets": []}
        with mock.patch("dsh.comic_tools.generate_batch", return_value=expected) as generate:
            content = tool.execute_fn({
                "output_root": "teaching/outputs",
                "storyboard": str(storyboard),
                "model": "Test-Image",
                "dry_run": True,
            })
    assert json.loads(content)["status"] == "dry_run"
    assert generate.call_args.kwargs["project"] is None
    assert generate.call_args.kwargs["explicit_output_root"] == Path("teaching/outputs")


def main() -> int:
    tests = [
        test_comic_tool_schemas,
        test_query_tools_do_not_gain_write_capability,
        test_dry_run_arguments_reach_shared_generator,
        test_batch_uses_explicit_output_root,
    ]
    for test in tests:
        test()
    print(f"OK: {len(tests)} comic DSH tool checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""DSH capability seam for read-only visual artifact QA."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

from visual_qa import run_visual_qa
from dsh.harness import ToolDefinition


def _visual_check(arguments: dict) -> str:
    result = run_visual_qa(
        arguments.get("path", ""),
        pages=arguments.get("pages", "all"),
        profile=arguments.get("profile", "auto"),
        context_path=arguments.get("context_path") or None,
        resume=bool(arguments.get("resume", True)),
        allow_remote=bool(arguments.get("allow_remote", False)),
        deterministic_only=bool(arguments.get("deterministic_only", False)),
        receipt_root=arguments.get("receipt_root") or None,
        model=arguments.get("model") or None,
        fallback_model=arguments.get("fallback_model") or None,
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def build_visual_tools() -> list[ToolDefinition]:
    """Return the isolated visual tool set (not part of query_actions)."""
    return [
        ToolDefinition(
            name="visual_check",
            description=(
                "检查图片、PDF 页面或 PPT/PPTX 静态页面；执行确定性检查并按需调用视觉模型，"
                "逐页保存可断点续做 receipt，不修改原文件。用户显式要求检查时调用；"
                "修改请求依赖布局、位置、颜色、遮挡、比例或页面流等可见状态且需先检查才能可靠理解时也调用；"
                "常规文字修改、编译和每轮交付不自动调用"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "图片、PDF、PPT 或 PPTX 路径",
                    },
                    "pages": {
                        "type": "string",
                        "description": "all 或 1,3-5 形式的一基页码",
                        "default": "all",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["auto", "figure", "paper", "slides", "document"],
                        "default": "auto",
                    },
                    "context_path": {
                        "type": "string",
                        "description": "可选的作图数据/说明文本路径",
                    },
                    "resume": {
                        "type": "boolean",
                        "default": True,
                    },
                    "allow_remote": {
                        "type": "boolean",
                        "description": "显式允许 raw/inbox/private 材料或 paper-profile PDF 发送到远程视觉 API",
                        "default": False,
                    },
                    "deterministic_only": {
                        "type": "boolean",
                        "description": "仅本地确定性检查，不调用远程模型",
                        "default": False,
                    },
                    "receipt_root": {
                        "type": "string",
                        "description": "可选 receipt/cache 根目录",
                    },
                    "model": {
                        "type": "string",
                        "description": "主视觉模型，默认 GLM-4.6V",
                    },
                    "fallback_model": {
                        "type": "string",
                        "description": "回退视觉模型，默认 GLM-4.5V",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            execute_fn=_visual_check,
            timeout_ms=None,
        )
    ]

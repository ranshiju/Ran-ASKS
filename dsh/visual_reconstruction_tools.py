"""DSH capability seam for explicit image/PDF to editable-PPT reconstruction."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

from visual_to_editable_ppt import run_visual_to_editable_ppt
from dsh.harness import ToolDefinition


def _visual_to_editable_ppt(arguments: dict) -> str:
    result = run_visual_to_editable_ppt(
        arguments.get("path", ""),
        output_path=arguments.get("output_path") or None,
        pages=arguments.get("pages", "all"),
        mode=arguments.get("mode", "balanced"),
        profile=arguments.get("profile", "auto"),
        resume=bool(arguments.get("resume", True)),
        overwrite=bool(arguments.get("overwrite", False)),
        allow_remote=bool(arguments.get("allow_remote", False)),
        deterministic_only=bool(arguments.get("deterministic_only", False)),
        receipt_root=arguments.get("receipt_root") or None,
        model=arguments.get("model") or None,
        fallback_model=arguments.get("fallback_model") or None,
        ocr_language=arguments.get("ocr_language", "auto"),
        dpi=int(arguments.get("dpi", 180)),
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def build_visual_reconstruction_tools() -> list[ToolDefinition]:
    """Return the isolated write-capable visual reconstruction tool set."""
    return [
        ToolDefinition(
            name="visual_to_editable_ppt",
            description=(
                "将图片或 PDF 尽量忠实地重建为可编辑 PPTX：矢量内容直接转原生对象，"
                "位图使用 OCR/图形检测，无法可靠对象化的像素明确保留为 fallback；"
                "不修改源文件，逐页保存可断点续做 checkpoint"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "源图片或 PDF 路径",
                    },
                    "output_path": {
                        "type": "string",
                        "description": (
                            "目标 .pptx 路径；省略时写入 "
                            "projects/visual-reconstruction/outputs/"
                        ),
                    },
                    "pages": {
                        "type": "string",
                        "description": "all 或 1,3-5 形式的一基页码",
                        "default": "all",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["faithful", "balanced", "editable"],
                        "default": "balanced",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["auto", "figure", "paper", "slides", "document"],
                        "default": "auto",
                    },
                    "resume": {"type": "boolean", "default": True},
                    "overwrite": {
                        "type": "boolean",
                        "description": "显式允许原子替换既有目标 PPTX",
                        "default": False,
                    },
                    "allow_remote": {
                        "type": "boolean",
                        "description": "允许敏感路径/论文 PDF 页面发送给远程视觉模型",
                        "default": False,
                    },
                    "deterministic_only": {
                        "type": "boolean",
                        "description": "只使用本地 PDF/OCR/图形算法，不调用远程模型",
                        "default": False,
                    },
                    "receipt_root": {
                        "type": "string",
                        "description": "必须位于仓库 temp/ 下的 checkpoint 根目录",
                    },
                    "ocr_language": {
                        "type": "string",
                        "description": "Tesseract 语言；默认 auto",
                        "default": "auto",
                    },
                    "dpi": {
                        "type": "integer",
                        "minimum": 96,
                        "maximum": 400,
                        "default": 180,
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
            execute_fn=_visual_to_editable_ppt,
            timeout_ms=None,
        )
    ]

"""DSH capability seam for explicit API-backed comic image generation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from comic_generation import generate_asset, generate_batch, image_candidates
from dsh.harness import ToolDefinition


def _scope(arguments: dict) -> tuple[str | None, Path | None]:
    project = str(arguments.get("project") or "").strip() or None
    output_root_value = str(arguments.get("output_root") or "").strip()
    output_root = Path(output_root_value) if output_root_value else None
    return project, output_root


def _comic_models(arguments: dict) -> str:
    return json.dumps(
        {"status": "ok", "candidates": image_candidates()},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _comic_generate(arguments: dict) -> str:
    project, output_root = _scope(arguments)
    result = generate_asset(
        project=project,
        explicit_output_root=output_root,
        article_id=str(arguments.get("article_id") or ""),
        asset_id=str(arguments.get("asset_id") or ""),
        prompt=str(arguments.get("prompt") or ""),
        model=str(arguments.get("model") or ""),
        size=str(arguments.get("size") or "").strip() or None,
        parameters=arguments.get("parameters") or {},
        allow_remote=bool(arguments.get("allow_remote", False)),
        overwrite=bool(arguments.get("overwrite", False)),
        dry_run=bool(arguments.get("dry_run", False)),
        timeout=float(arguments.get("timeout") or 180.0),
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _comic_batch(arguments: dict) -> str:
    project, output_root = _scope(arguments)
    result = generate_batch(
        project=project,
        explicit_output_root=output_root,
        storyboard_path=Path(str(arguments.get("storyboard") or "")),
        model=str(arguments.get("model") or "").strip() or None,
        size=str(arguments.get("size") or "").strip() or None,
        allow_remote=bool(arguments.get("allow_remote", False)),
        overwrite=bool(arguments.get("overwrite", False)),
        dry_run=bool(arguments.get("dry_run", False)),
        timeout=float(arguments.get("timeout") or 180.0),
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


def _scope_properties() -> dict:
    return {
        "project": {
            "type": "string",
            "description": "projects/ 下的研究或写作项目名；与 output_root 二选一",
        },
        "output_root": {
            "type": "string",
            "description": "仓库内已存在且目录名为 outputs 的输出根；与 project 二选一",
        },
    }


def _generation_properties() -> dict:
    return {
        **_scope_properties(),
        "model": {
            "type": "string",
            "description": "llm-models.yaml image_generation.candidates 中的模型",
        },
        "size": {"type": "string", "description": "供应商接受的图片尺寸"},
        "allow_remote": {
            "type": "boolean",
            "description": "显式允许把 Prompt 发送到远程图片 API",
            "default": False,
        },
        "overwrite": {"type": "boolean", "default": False},
        "dry_run": {
            "type": "boolean",
            "description": "只校验模型、输入和输出路径，不联网、不写文件",
            "default": False,
        },
        "timeout": {"type": "number", "default": 180.0, "minimum": 1},
    }


def build_comic_tools() -> list[ToolDefinition]:
    scope_rule = {
        "oneOf": [
            {"required": ["project"], "not": {"required": ["output_root"]}},
            {"required": ["output_root"], "not": {"required": ["project"]}},
        ]
    }
    return [
        ToolDefinition(
            name="comic_models",
            description="列出已登记的 API 图片生成候选；不联网、不修改文件",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            execute_fn=_comic_models,
        ),
        ToolDefinition(
            name="comic_generate",
            description=(
                "通过已配置的远程图片 API 生成一幅漫画图片；只写明确允许的 outputs，"
                "真实调用必须 allow_remote=true，默认不覆盖既有图片"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_generation_properties(),
                    "article_id": {"type": "string"},
                    "asset_id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "parameters": {"type": "object", "default": {}},
                },
                "required": ["article_id", "asset_id", "prompt", "model"],
                **scope_rule,
                "additionalProperties": False,
            },
            execute_fn=_comic_generate,
            timeout_ms=None,
        ),
        ToolDefinition(
            name="comic_batch",
            description=(
                "读取仓库内 YAML/JSON storyboard 并逐图调用远程图片 API；"
                "真实调用必须 allow_remote=true，中途失败不自动切换模型"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_generation_properties(),
                    "storyboard": {"type": "string"},
                },
                "required": ["storyboard"],
                **scope_rule,
                "additionalProperties": False,
            },
            execute_fn=_comic_batch,
            timeout_ms=None,
        ),
    ]

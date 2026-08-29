#!/usr/bin/env python3
"""research_project.py — 研究项目模板初始化与结构校验。

通用研究项目能力：从 projects/_templates/research 生成新项目骨架，
并校验既有研究项目的必需文件、schema 和产物边界。

用法:
  python3 .scripts/research_project.py init <project> [--name 名称] [--topic 主题] [--stage ideation]
  python3 .scripts/research_project.py validate <project> [--strict]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECTS_DIR = Path(os.environ.get("WIKIGRAPH_PROJECTS_DIR", REPO / "projects"))
TEMPLATE_DIR = PROJECTS_DIR / "_templates" / "research"

REQUIRED_FILES = [
    "README.md",
    "AGENTS.md",
    "schema.yaml",
    "notes/status.md",
    "codes/README.md",
    "codes/docs/README.md",
    "codes/docs/PHYSICS.md",
    "codes/experiments/README.md",
    "codes/analysis/README.md",
    "codes/tests/README.md",
]

REQUIRED_SCHEMA_KEYS = ["project", "structure", "compute", "artifact_policy", "code_management"]
PROJECT_KEYS = ["id", "name", "type", "stage", "topic"]

GENERATED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".npz", ".pt", ".pth",
    ".ckpt", ".json", ".txt", ".log", ".csv", ".out", ".aux", ".toc",
}


def load_yaml(path: Path) -> dict:
    """读取 YAML；无 PyYAML 时返回空 dict 并提示。"""
    try:
        import yaml
    except Exception:
        print(f"WARN: 未安装 PyYAML，跳过 {path} 内容校验", file=sys.stderr)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data or {}
    except Exception as exc:
        print(f"ERROR: 无法解析 {path}: {exc}", file=sys.stderr)
        return {}


def render_text(text: str, values: dict[str, str]) -> str:
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def ensure_project_absent(target: Path) -> None:
    if target.exists():
        raise SystemExit(f"ERROR: 项目目录已存在: {target}")


def init_project(args: argparse.Namespace) -> int:
    project = args.project
    if not re.fullmatch(r"[^/\\]+", project):
        raise SystemExit("ERROR: 项目名不能包含路径分隔符")

    target = PROJECTS_DIR / project
    ensure_project_absent(target)

    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"ERROR: 研究项目模板不存在: {TEMPLATE_DIR}")

    values = {
        "PROJECT_ID": project,
        "PROJECT_NAME": args.name or project,
        "TOPIC": args.topic or "待填写研究主题",
        "STAGE": args.stage,
    }

    shutil.copytree(TEMPLATE_DIR, target)

    for path in sorted(target.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = render_text(text, values)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")

    (target / ".research-memory" / "entries").mkdir(parents=True, exist_ok=True)
    profile = {
        "topic": values["TOPIC"],
        "keywords": [],
        "stage": values["STAGE"],
        "active_questions": [],
        "updated_at": datetime.now().isoformat(),
    }
    (target / ".research-memory" / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"[init] 已创建研究项目: {target}")
    print(f"  schema: {target / 'schema.yaml'}")
    print(f"  规则: {target / 'AGENTS.md'}")
    print(f"  代码文档: {target / 'codes' / 'README.md'}")
    print("下一步：完善 topic、compute 策略和 codes/docs/PHYSICS.md。")
    return 0


def validate_project(args: argparse.Namespace) -> int:
    project = args.project
    target = PROJECTS_DIR / project
    if not target.is_dir():
        raise SystemExit(f"ERROR: 研究项目不存在: {target}")

    errors: list[str] = []
    warnings: list[str] = []

    # 1. 必需文件
    for rel in REQUIRED_FILES:
        path = target / rel
        if not path.is_file():
            errors.append(f"缺少必需文件: {rel}")

    # 2. 必需目录
    for rel in ["outputs", ".research-memory", "codes/experiments", "codes/analysis", "codes/tests"]:
        path = target / rel
        if not path.is_dir():
            errors.append(f"缺少必需目录: {rel}/")

    # 3. schema 内容
    schema_path = target / "schema.yaml"
    schema = load_yaml(schema_path) if schema_path.is_file() else {}
    if schema:
        for key in REQUIRED_SCHEMA_KEYS:
            if key not in schema:
                errors.append(f"schema.yaml 缺少顶层字段: {key}")
        project_data = schema.get("project") or {}
        for key in PROJECT_KEYS:
            if key not in project_data:
                errors.append(f"schema.yaml project 缺少字段: {key}")
        if project_data.get("type") != "research":
            errors.append("schema.yaml project.type 应为 research")
        if project_data.get("id") != project:
            errors.append(f"schema.yaml project.id 应为 {project}，实际为 {project_data.get('id')!r}")
        artifact = schema.get("artifact_policy") or {}
        if artifact.get("all_products_inside_project") is not True:
            errors.append("artifact_policy.all_products_inside_project 应为 true")
    else:
        warnings.append("schema.yaml 未成功解析；仅检查文件存在性")

    # 4. 产物边界扫描
    for rel_root, label in [(".", "项目根目录"), ("codes", "codes/")]:
        root = target / rel_root
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(target).as_posix()
            if path.suffix.lower() not in GENERATED_SUFFIXES:
                continue
            if rel.startswith("outputs/") or rel.startswith(".research-memory/"):
                continue
            if rel.startswith("codes/formulas/") or rel.startswith("codes/docs/") or rel.startswith("codes/experiments/") or rel.startswith("codes/analysis/") or rel.startswith("codes/tests/"):
                continue
            if path.name == ".gitkeep":
                continue
            msg = f"生成物未在 outputs/：{rel}"
            if args.strict:
                errors.append(msg)
            else:
                warnings.append(msg)

    # 5. 输出
    print(f"[validate] {target}")
    for w in sorted(set(warnings)):
        print(f"  WARN: {w}")
    for e in sorted(set(errors)):
        print(f"  ERROR: {e}")
    if errors:
        print(f"结果: FAIL ({len(errors)} errors, {len(set(warnings))} warnings)")
        return 1
    print(f"结果: OK ({len(set(warnings))} warnings)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="从通用模板创建研究项目")
    p_init.add_argument("project")
    p_init.add_argument("--name", help="项目显示名，默认同目录名")
    p_init.add_argument("--topic", help="一句话研究主题")
    p_init.add_argument("--stage", default="ideation",
                        choices=["ideation", "experiment", "writing", "revision", "submission", "unknown"])
    p_init.set_defaults(func=init_project)

    p_val = sub.add_parser("validate", help="校验研究项目结构")
    p_val.add_argument("project")
    p_val.add_argument("--strict", action="store_true",
                       help="将生成物未放 outputs/ 也视为错误")
    p_val.set_defaults(func=validate_project)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

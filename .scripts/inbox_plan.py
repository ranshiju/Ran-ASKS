#!/usr/bin/env python3
"""Produce a read-only, machine-readable inbox intake plan.

The plan is deliberately conservative: it records what is known from filenames and
contents without moving, extracting, or deleting any inbox file.  An executor must
use this plan rather than infer a create/batch route from an unfiltered directory.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBPROJECTS = ("academic", "admin", "teaching", "business")
PAPER_SUFFIXES = {".pdf"}
MEETING_SUFFIXES = {".txt"}
DOCUMENT_SUFFIXES = {".md", ".docx", ".xlsx", ".pptx"}


def slugify(value: str) -> str:
    value = value.lower().replace("’", "").replace("'", "")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "unclassified-document"


def fact_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines()
               if re.match(r"^- \[\d{4}-\d{2}-\d{2}\]", line))


def classify(path: Path) -> dict:
    suffix = path.suffix.lower()
    if path.name == "facts-pending.md":
        return {
            "kind": "user-assertions", "subproject": "cross-domain", "mode": "update",
            "content": "other", "source_kind": "ordinary", "requires_review": False,
            "reason": "累积型用户申明事实，只有存在条目时才归档。",
            "fact_entries": fact_entries(path),
        }
    if suffix in PAPER_SUFFIXES:
        return {
            "kind": "paper-pdf", "subproject": "academic", "mode": "create",
            "content": "paper", "source_kind": "ordinary", "requires_review": False,
            "reason": "PDF 默认按学术参考论文临时提取；最终归属由提取的题名/作者确认。",
            "proposed_id": slugify(path.stem),
        }
    if suffix in MEETING_SUFFIXES:
        return {
            "kind": "text", "subproject": None, "mode": "create", "content": "other",
            "source_kind": "ordinary", "requires_review": True,
            "reason": "普通文本无法仅凭文件名判定是否为会议纪要或所属子项目。",
        }
    if suffix in DOCUMENT_SUFFIXES:
        return {
            "kind": "document", "subproject": None, "mode": "create", "content": "other",
            "source_kind": "ordinary", "requires_review": True,
            "reason": "需从内容判断子项目和页面类型。",
        }
    return {
        "kind": "unknown", "subproject": None, "mode": None, "content": None,
        "source_kind": None, "requires_review": True, "reason": "不支持的文件类型。",
    }


def build_plan(inbox: Path) -> dict:
    items = []
    for path in sorted(inbox.iterdir() if inbox.exists() else [], key=lambda item: item.name.lower()):
        if path.name == ".gitkeep" or not path.is_file():
            continue
        details = classify(path)
        items.append({"path": str(path.relative_to(REPO)), "size_bytes": path.stat().st_size, **details})
    ordinary = [item for item in items if item["kind"] != "user-assertions"]
    batch_candidates = [item for item in ordinary if item["subproject"] == "academic" and item["content"] == "paper"]
    batch_eligible = len(ordinary) >= 3 and len(batch_candidates) == len(ordinary)
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inbox": str(inbox.relative_to(REPO)),
        "items": items,
        "routing": {
            "batch_eligible": batch_eligible,
            "batch_reason": "至少三份同类 academic paper 文件" if batch_eligible else "不满足至少三份同类 academic paper 文件；逐项使用 create。",
            "default_mode": "batch" if batch_eligible else "create",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", default="inbox", help="仓库内收件箱目录")
    parser.add_argument("--output", help="写入 JSON；省略时输出 stdout")
    args = parser.parse_args()
    inbox = (REPO / args.inbox).resolve()
    if REPO not in inbox.parents and inbox != REPO:
        raise SystemExit("ERROR: --inbox 必须位于仓库内")
    plan = build_plan(inbox)
    output = json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        target = (REPO / args.output).resolve()
        if REPO not in target.parents:
            raise SystemExit("ERROR: --output 必须位于仓库内")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()

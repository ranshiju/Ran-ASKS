#!/usr/bin/env python3
"""长文摄入的只读规划器：用长度和标题树触发语义拆分评估。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CHAR_THRESHOLD = 8_000
DEFAULT_SECTION_THRESHOLD = 4
MIN_SECTION_CHARS = 400
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_sections(text: str) -> list[dict]:
    """提取二级标题及其正文长度；二级标题是默认可拆分候选。"""
    lines = text.splitlines()
    sections: list[dict] = []
    current = None
    for line_number, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == 2:
            if current is not None:
                current["char_count"] = len("\n".join(current.pop("body")).strip())
                sections.append(current)
            current = {"title": match.group(2), "line": line_number, "body": []}
        elif current is not None:
            current["body"].append(line)
    if current is not None:
        current["char_count"] = len("\n".join(current.pop("body")).strip())
        sections.append(current)
    return sections


def keyword_budget(unit_count: int) -> dict:
    """关键词预算随语义单元增长，不随原文长度线性膨胀。"""
    return {
        "overview": {"min": 3, "max": min(8, 3 + unit_count)},
        "section": {"min": 2, "max": 5},
        "selection_rule": "密度只作候选信号；优先标题、定义/结论、跨单元覆盖和现有 Hub 匹配，需人工或 LLM 确认后入图。",
    }


def make_plan(path: Path, *, char_threshold: int = DEFAULT_CHAR_THRESHOLD,
              section_threshold: int = DEFAULT_SECTION_THRESHOLD) -> dict:
    text = path.read_text(encoding="utf-8")
    sections = parse_sections(text)
    substantive = [section for section in sections if section["char_count"] >= MIN_SECTION_CHARS]
    candidate_trigger = len(text) >= char_threshold or len(sections) >= section_threshold
    semantic_review_required = candidate_trigger and len(substantive) >= 2
    split_candidates = [
        {key: section[key] for key in ("title", "line", "char_count")}
        for section in substantive
    ]
    try:
        raw_path = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        raw_path = str(path.resolve())
    unit_count = len(split_candidates) if semantic_review_required else 1
    return {
        "version": 1,
        "raw": raw_path,
        "read_only": True,
        "char_count": len(text),
        "heading_count": len(sections),
        "substantive_section_count": len(substantive),
        "thresholds": {"char_count": char_threshold, "heading_count": section_threshold,
                       "min_section_chars": MIN_SECTION_CHARS},
        "candidate_trigger": candidate_trigger,
        "semantic_review_required": semantic_review_required,
        "recommended_structure": "overview-plus-sections" if semantic_review_required else "single-source-page",
        "split_candidates": split_candidates,
        "keyword_budget": keyword_budget(unit_count),
        "next_step": (
            "一次 LLM 语义审查确认候选是否可独立回答；确认后创建总览页和最多一层章节页。"
            if semantic_review_required else "创建单一来源页；不因长度不足而强行拆分。"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="只读规划长文 wiki 颗粒度与关键词预算")
    parser.add_argument("raw", help="仓库内已归档的 Markdown/TXT 原文")
    parser.add_argument("--char-threshold", type=int, default=DEFAULT_CHAR_THRESHOLD)
    parser.add_argument("--section-threshold", type=int, default=DEFAULT_SECTION_THRESHOLD)
    args = parser.parse_args()
    path = (REPO / args.raw).resolve()
    if not path.is_file() or REPO not in path.parents:
        raise SystemExit("ERROR: raw 必须是仓库内的已归档文件")
    if args.char_threshold < 1 or args.section_threshold < 1:
        raise SystemExit("ERROR: 阈值必须为正整数")
    print(json.dumps(make_plan(path, char_threshold=args.char_threshold,
                               section_threshold=args.section_threshold), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

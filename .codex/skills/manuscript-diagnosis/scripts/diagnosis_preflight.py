#!/usr/bin/env python3
"""Read-only discovery for a WikiGraph manuscript-diagnosis workspace."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


MANUSCRIPT_SUFFIXES = {".pdf", ".tex"}
REPORT_SUFFIXES = {".docx", ".md", ".txt", ".rtf"}
REPORT_MARKERS = ("reviewer", "referee", "report", "review", "diagnosis", "diagnostic", "诊断", "意见")


def journal_hint(folder_name: str) -> str:
    hint = re.sub(r"^\d{4,8}(?:[-_. ]+)?", "", folder_name).strip(" -_.")
    return hint or folder_name


def is_report(path: Path) -> bool:
    lowered = path.stem.casefold()
    return path.suffix.casefold() in REPORT_SUFFIXES and any(
        marker in lowered for marker in REPORT_MARKERS
    )


def style_rank(path: Path) -> tuple[int, str]:
    lowered = path.stem.casefold()
    if any(marker in lowered for marker in ("_ran", "final", "最终")):
        priority = 0
    elif any(marker in lowered for marker in ("revised", "revision", "修改")):
        priority = 1
    else:
        priority = 2
    return priority, path.as_posix()


def prefer_readable_format(paths: list[Path]) -> list[Path]:
    selected: dict[tuple[Path, str], Path] = {}
    format_priority = {".md": 0, ".txt": 1, ".docx": 2, ".rtf": 3}
    for path in paths:
        key = (path.parent, path.stem.casefold())
        current = selected.get(key)
        if current is None or format_priority[path.suffix.casefold()] < format_priority[current.suffix.casefold()]:
            selected[key] = path
    return list(selected.values())


def diagnosis_root(target: Path) -> Path:
    for candidate in (target, *target.parents):
        if candidate.name == "稿件诊断" and candidate.parent.name == "projects":
            return candidate
    raise ValueError("target must be inside a projects/稿件诊断 directory")


def relative(path: Path, base: Path) -> str:
    return path.relative_to(base).as_posix()


def preflight(target_arg: str) -> dict:
    target = Path(target_arg).expanduser().resolve()
    if not target.is_dir():
        raise ValueError(f"target directory does not exist: {target}")

    root = diagnosis_root(target)
    base_style_guide = root / "style-library" / "稿件诊断写作风格指南.md"
    style_profile = root / "STYLE.md"
    manuscripts = sorted(
        path for path in target.iterdir()
        if path.is_file() and path.suffix.casefold() in MANUSCRIPT_SUFFIXES
    )
    existing_reports = sorted(
        prefer_readable_format([
            path for path in target.iterdir() if path.is_file() and is_report(path)
        ]),
        key=style_rank,
    )
    style_samples = sorted(
        prefer_readable_format([
            path for path in root.rglob("*")
            if path.is_file()
            and is_report(path)
            and path not in existing_reports
            and path not in {base_style_guide, style_profile}
        ]),
        key=style_rank,
    )

    return {
        "ok": bool(manuscripts),
        "target": str(target),
        "folder_label": target.name,
        "journal_hint": journal_hint(target.name),
        "base_style_guide": relative(base_style_guide, root) if base_style_guide.is_file() else None,
        "style_profile": relative(style_profile, root) if style_profile.is_file() else None,
        "manuscripts": [relative(path, root) for path in manuscripts],
        "existing_reports": [relative(path, root) for path in existing_reports],
        "style_samples": [relative(path, root) for path in style_samples],
        "warnings": [] if manuscripts else ["no PDF or TeX manuscript found"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="manuscript-diagnosis subfolder under projects/稿件诊断")
    args = parser.parse_args()
    try:
        payload = preflight(args.target)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build a cheap, non-destructive incremental ingest plan."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from derivation_state import sha256_file


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"raw": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {"raw": {}}


def raw_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file() and ".git" not in path.parts)


def make_plan(raw_root: Path, wiki_root: Path, state: dict) -> dict:
    previous = state.get("raw", {})
    current = {}
    changed = []
    for path in raw_files(raw_root):
        relative = path.relative_to(raw_root).as_posix()
        digest = sha256_file(path)
        current[relative] = digest
        if previous.get(relative) != digest:
            changed.append(relative)
    removed = sorted(set(previous) - set(current))
    affected_pages = []
    for relative in changed:
        stem = Path(relative).stem
        if wiki_root.exists():
            affected_pages.extend(
                path.relative_to(wiki_root).with_suffix("").as_posix()
                for path in wiki_root.rglob(f"{stem}.md")
            )
    return {
        "version": 1,
        "changed_raw": changed,
        "removed_raw": removed,
        "affected_wiki_pages": sorted(set(affected_pages)),
        "requires_llm": bool(changed),
        "raw": current,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成非破坏性的增量摄入计划")
    parser.add_argument("--raw-root", default="academic/raw")
    parser.add_argument("--wiki-root", default="academic/wiki")
    parser.add_argument("--state", default="cross-domain/ingest-state.json")
    parser.add_argument("--write-state", action="store_true", help="仅在计划确认后保存当前 raw 指纹")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    raw_root = (repo / args.raw_root).resolve()
    wiki_root = (repo / args.wiki_root).resolve()
    state_path = (repo / args.state).resolve()
    plan = make_plan(raw_root, wiki_root, load_state(state_path))
    if args.write_state:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"version": 1, "raw": plan["raw"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in plan.items() if key != "raw"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

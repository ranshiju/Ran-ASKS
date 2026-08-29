#!/usr/bin/env python3
"""Replace project-internal absolute symlinks with portable relative symlinks.

Run without --apply to inspect proposed changes. The script refuses to change
links whose absolute target lies outside the selected project root.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def project_root(value: str | None) -> Path:
    return Path(value).resolve() if value else Path(__file__).resolve().parent.parent


def internal_absolute_links(root: Path) -> list[tuple[Path, Path, str]]:
    changes: list[tuple[Path, Path, str]] = []
    for link_path in root.rglob("*"):
        if not link_path.is_symlink():
            continue
        target = os.readlink(link_path)
        if not os.path.isabs(target):
            continue
        target_path = Path(target)
        try:
            target_path.relative_to(root)
        except ValueError:
            continue
        changes.append((link_path, target_path, os.path.relpath(target_path, link_path.parent)))
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="project root; defaults to this script's parent directory")
    parser.add_argument("--apply", action="store_true", help="replace eligible links")
    args = parser.parse_args()

    root = project_root(args.root)
    if not (root / "AGENTS.md").is_file():
        parser.error(f"not a WikiRan project root: {root}")

    changes = internal_absolute_links(root)
    for link_path, target_path, relative_target in changes:
        print(f"{link_path.relative_to(root)}: {target_path} -> {relative_target}")

    if not args.apply:
        print(f"Dry run: {len(changes)} internal absolute symlink(s) would be converted.")
        return 0

    for link_path, _, relative_target in changes:
        link_path.unlink()
        link_path.symlink_to(relative_target)
    print(f"Converted: {len(changes)} internal absolute symlink(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

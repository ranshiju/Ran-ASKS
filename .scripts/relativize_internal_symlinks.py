#!/usr/bin/env python3
"""Replace project-internal absolute symlinks with portable relative symlinks.

Run without --apply to inspect proposed changes. The script refuses to change
links whose absolute target lies outside the selected project root.
The explicit --materialize-link mode separately repairs one broken public Raw
link from a verified same-folder original, without accessing the old target.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from platform_compat import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid


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


def materialize_broken_link(root: Path, link: Path, source: Path,
                            expected_sha256: str, *, apply: bool = False) -> dict:
    """Restore one explicitly selected public Raw link from a same-folder original.

    Never follows the old target. A prepared receipt preserves its exact text;
    exceptions after replacement restore the link. Source bytes remain untouched.
    """
    lexical_root = Path(os.path.abspath(root))
    root = lexical_root.resolve()
    def checked_path(path: Path) -> Path:
        path = Path(os.path.abspath(lexical_root / path))
        try:
            rel = path.relative_to(lexical_root)
        except ValueError:
            rel = path.relative_to(root)
        path = root / rel
        if len(rel.parts) < 3 or rel.parts[0] not in {
            "academic", "admin", "teaching", "business"} or rel.parts[1] != "raw":
            raise ValueError("repair paths must be inside public Raw")
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("symlink ancestor is not allowed")
        return path

    link, source = checked_path(link), checked_path(source)
    if link == source or link.parent != source.parent or link.suffix != source.suffix:
        raise ValueError("source must be a different same-folder file of the same type")
    if source.is_symlink() or not source.is_file():
        raise ValueError("source must be an existing regular original")
    def digest(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    if digest(source) != expected_sha256:
        raise ValueError("source SHA-256 mismatch")
    if not link.is_symlink():
        if link.is_file() and digest(link) == expected_sha256:
            return {"status": "already_materialized", "path": str(link.relative_to(root)),
                    "sha256": expected_sha256}
        raise ValueError("destination must be a broken symlink")
    if link.exists():
        raise ValueError("refusing to replace a working symlink")
    original_target = os.readlink(link)
    plan = {"schema": "raw-link-repair-v1", "status": "planned",
            "path": str(link.relative_to(root)), "source": str(source.relative_to(root)),
            "old_target": original_target, "sha256": expected_sha256,
            "created_at": datetime.now(timezone.utc).isoformat()}
    if not apply:
        return plan
    receipt_dir = root / "temp" / "raw-link-repair"
    for directory in [root / "temp", receipt_dir]:
        if directory.is_symlink():
            raise ValueError("receipt directory must not be a symlink")
        directory.mkdir(exist_ok=True)
    receipt = receipt_dir / (uuid.uuid4().hex + ".json")
    plan["receipt"] = str(receipt.relative_to(root))
    def save(status: str) -> None:
        plan["status"] = status
        tmp = receipt.with_suffix(".tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, receipt)
    # A common repair lock serializes competing explicit repairs.
    lock = receipt_dir / ".lock"
    if lock.is_symlink():
        raise ValueError("repair lock must not be a symlink")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if not link.is_symlink() or os.readlink(link) != original_target or link.exists():
            raise ValueError("destination changed since planning")
        fd, staged = tempfile.mkstemp(prefix="raw-copy-", dir=receipt_dir)
        os.close(fd)
        staged = Path(staged)
        replaced = False
        try:
            shutil.copy2(source, staged)
            if digest(staged) != expected_sha256 or digest(source) != expected_sha256:
                raise ValueError("source changed during copy")
            save("prepared")
            os.replace(staged, link)
            replaced = True
            if link.is_symlink() or digest(link) != expected_sha256:
                raise ValueError("restored file verification failed")
            save("completed")
        except Exception:
            if replaced:
                staged.symlink_to(original_target)
                os.replace(staged, link)
                save("rolled_back")
            raise
        finally:
            staged.unlink(missing_ok=True)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="project root; defaults to this script's parent directory")
    parser.add_argument("--apply", action="store_true", help="replace eligible links")
    parser.add_argument("--materialize-link", help="repair only this broken public Raw link as a real file")
    parser.add_argument("--source-file", help="explicit same-folder original for --materialize-link")
    parser.add_argument("--expected-sha256", help="verified original hash for --materialize-link")
    args = parser.parse_args()

    root = project_root(args.root)
    if not (root / "AGENTS.md").is_file():
        parser.error(f"not a WikiRan project root: {root}")

    if args.materialize_link:
        if not args.source_file or not args.expected_sha256:
            parser.error("--materialize-link requires --source-file and --expected-sha256")
        result = materialize_broken_link(root, Path(args.materialize_link),
                                        Path(args.source_file), args.expected_sha256,
                                        apply=args.apply)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.source_file or args.expected_sha256:
        parser.error("repair source/hash require --materialize-link")

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

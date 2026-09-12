#!/usr/bin/env python3
"""Persistent lifecycle policy for chat originals; never a knowledge evidence source."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SCHEMA = "inbox-source-retention-v1"


def _location(repo: Path, source: Path) -> tuple[Path, str] | None:
    repo = repo.resolve()
    source = Path(os.path.abspath(source))  # Retention follows the original path, even if replaced by a symlink.
    try:
        relative = source.relative_to(repo / "inbox").as_posix()
    except ValueError:
        return None
    directory = repo / "inbox" / ".source-retention"
    if directory.is_symlink():
        raise ValueError("source retention directory must not be a symlink")
    key = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return directory / f"{key}.json", relative


def is_retained(repo: Path, source: Path) -> bool:
    location = _location(repo, source)
    if location is None:
        return False
    marker, relative = location
    if marker.is_symlink():
        raise ValueError("source retention marker must not be a symlink")
    if not marker.exists():
        return False
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data != {"schema": SCHEMA, "source": relative, "policy": "retain_original"}:
        raise ValueError("invalid source retention marker; refusing source cleanup")
    return True


def retain_original(repo: Path, source: Path) -> None:
    """Retain an inbox original across transactions and future scans, even if edited."""
    location = _location(repo, source)
    if location is None or is_retained(repo, source):
        return
    marker, relative = location
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never overwrite a pre-existing lifecycle policy.
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA, "source": relative, "policy": "retain_original"},
                      handle, ensure_ascii=False)
    except FileExistsError:
        if not is_retained(repo, source):
            raise ValueError("unable to retain inbox original")

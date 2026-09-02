#!/usr/bin/env python3
"""Rebuildable source-file fingerprint index for inbox deduplication."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from derivation_state import sha256_file


REPO = Path(__file__).resolve().parent.parent
CROSS_DOMAIN_DB = REPO / "cross-domain" / "source-fingerprints.db"
PRIVATE_DB = REPO / "private" / "source-fingerprints.db"
RAW_ROOTS = tuple(REPO / domain / "raw" for domain in ("academic", "admin", "teaching", "business"))
PRIVATE_RAW_ROOTS = (REPO / "private" / "raw",)
SIDECAR_NAMES = {"source.yaml", "parse_meta.yaml", "manifest.json", "entity-resolution.json"}
SIDECAR_PATTERNS = (re.compile(r"^corrected(?:[-.].*)?$", re.I),)
TEXT_COMPANION_SUFFIXES = {".md", ".txt"}
BINARY_SOURCE_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".jpg", ".jpeg", ".png", ".wav", ".mp3", ".m4a", ".mp4",
}


def db_for_path(path: Path, repo: Path = REPO) -> Path:
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return CROSS_DOMAIN_DB
    return PRIVATE_DB if rel.parts and rel.parts[0] == "private" else CROSS_DOMAIN_DB


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_fingerprints (
            raw_path TEXT PRIMARY KEY,
            binary_sha256 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            text_sha256 TEXT NOT NULL DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT '',
            indexed_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_binary ON source_fingerprints(binary_sha256, size_bytes)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_text ON source_fingerprints(text_sha256)"
    )
    return conn


def normalized_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _stored_path(path: Path, repo: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve()))
    except ValueError:
        return str(path.resolve())


def register_source(
    source_path: Path,
    *,
    raw_path: str | None = None,
    text_path: Path | None = None,
    source_kind: str = "",
    db_path: Path | None = None,
    repo: Path = REPO,
) -> dict:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    target_db = db_path or db_for_path(source_path, repo)
    stat = source_path.stat()
    digest = sha256_file(source_path)
    text_digest = normalized_text_sha256(text_path) if text_path and text_path.is_file() else ""
    stored = raw_path or _stored_path(source_path, repo)
    indexed_at = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = _connect(target_db)
    try:
        conn.execute(
            """
            INSERT INTO source_fingerprints
                (raw_path,binary_sha256,size_bytes,mtime_ns,text_sha256,source_kind,indexed_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(raw_path) DO UPDATE SET
                binary_sha256=excluded.binary_sha256,
                size_bytes=excluded.size_bytes,
                mtime_ns=excluded.mtime_ns,
                text_sha256=excluded.text_sha256,
                source_kind=excluded.source_kind,
                indexed_at=excluded.indexed_at
            """,
            (stored, digest, stat.st_size, stat.st_mtime_ns, text_digest, source_kind, indexed_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "raw_path": stored,
        "binary_sha256": digest,
        "size_bytes": stat.st_size,
        "text_sha256": text_digest,
    }


def lookup_exact(
    source_path: Path,
    *,
    db_path: Path | None = None,
    repo: Path = REPO,
) -> dict | None:
    source_path = source_path.resolve()
    if not source_path.is_file():
        return None
    target_db = db_path or db_for_path(source_path, repo)
    if not target_db.is_file():
        return None
    size = source_path.stat().st_size
    digest = sha256_file(source_path)
    conn = _connect(target_db)
    try:
        row = conn.execute(
            """SELECT * FROM source_fingerprints
               WHERE binary_sha256=? AND size_bytes=?
               ORDER BY length(raw_path), raw_path LIMIT 1""",
            (digest, size),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    result = dict(row)
    result["match"] = "binary_sha256"
    return result


def lookup_text_candidate(
    text_path: Path,
    *,
    db_path: Path = CROSS_DOMAIN_DB,
) -> dict | None:
    if not text_path.is_file() or not db_path.is_file():
        return None
    digest = normalized_text_sha256(text_path)
    if not digest:
        return None
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM source_fingerprints WHERE text_sha256=?
               ORDER BY length(raw_path), raw_path LIMIT 1""",
            (digest,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    result = dict(row)
    result["match"] = "normalized_text_sha256"
    return result


def _is_source_artifact(path: Path) -> bool:
    if not path.is_file() or path.name in SIDECAR_NAMES or path.name.endswith(".bak"):
        return False
    if any(pattern.match(path.name) for pattern in SIDECAR_PATTERNS):
        return False
    if path.name == "paper.md" and (path.parent / "paper.pdf").is_file():
        return False
    if path.suffix.lower() in TEXT_COMPANION_SUFFIXES and any(
        sibling.is_file()
        and sibling.stem == path.stem
        and sibling.suffix.lower() in BINARY_SOURCE_SUFFIXES
        for sibling in path.parent.iterdir()
    ):
        return False
    return True


def _iter_source_artifacts(roots: tuple[Path, ...]):
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if _is_source_artifact(path):
                yield path


def rebuild(
    *,
    db_path: Path = CROSS_DOMAIN_DB,
    roots: tuple[Path, ...] = RAW_ROOTS,
    repo: Path = REPO,
) -> dict:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM source_fingerprints")
        conn.commit()
    finally:
        conn.close()
    indexed = 0
    for source_path in _iter_source_artifacts(roots):
        text_path = None
        if source_path.name == "paper.pdf" and (source_path.parent / "paper.md").is_file():
            text_path = source_path.parent / "paper.md"
        elif source_path.suffix.lower() in {".md", ".txt"}:
            text_path = source_path
        register_source(
            source_path,
            text_path=text_path,
            source_kind=source_path.suffix.lower().lstrip("."),
            db_path=db_path,
            repo=repo,
        )
        indexed += 1
    return {"status": "rebuilt", "db": str(db_path), "indexed": indexed}


def ensure_index(*, private: bool = False) -> dict | None:
    db_path = PRIVATE_DB if private else CROSS_DOMAIN_DB
    if db_path.is_file():
        return None
    roots = PRIVATE_RAW_ROOTS if private else RAW_ROOTS
    return rebuild(db_path=db_path, roots=roots)


def main() -> None:
    parser = argparse.ArgumentParser(description="可重建的摄入源文件 SHA-256/文本指纹索引")
    parser.add_argument("action", choices=("rebuild", "lookup", "register"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--text-path")
    args = parser.parse_args()
    db_path = PRIVATE_DB if args.private else CROSS_DOMAIN_DB
    if args.action == "rebuild":
        roots = PRIVATE_RAW_ROOTS if args.private else RAW_ROOTS
        result = rebuild(db_path=db_path, roots=roots)
    else:
        if not args.path:
            parser.error("lookup/register 需要 path")
        path = (REPO / args.path).resolve()
        if args.action == "lookup":
            ensure_index(private=args.private)
            result = lookup_exact(path, db_path=db_path) or {"status": "not_found"}
        else:
            text_path = (REPO / args.text_path).resolve() if args.text_path else None
            result = register_source(path, text_path=text_path, db_path=db_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

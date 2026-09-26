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
from source_locator import IMAGE_SUFFIXES


REPO = Path(__file__).resolve().parent.parent
CROSS_DOMAIN_DB = REPO / "cross-domain" / "source-fingerprints.db"
PRIVATE_DB = REPO / "private" / "source-fingerprints.db"
RAW_ROOTS = tuple(REPO / domain / "raw" for domain in ("academic", "admin", "teaching", "business"))
PRIVATE_RAW_ROOTS = (REPO / "private" / "raw",)
SIDECAR_NAMES = {"source.yaml", "parse_meta.yaml", "manifest.json", "entity-resolution.json"}
SIDECAR_PATTERNS = (
    re.compile(r"^corrected(?:[-.].*)?$", re.I),
    re.compile(r"^.+\.source\.json$", re.I),
)
TEXT_COMPANION_SUFFIXES = {".md", ".txt"}
BINARY_SOURCE_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".wav", ".mp3", ".m4a", ".mp4",
} | IMAGE_SUFFIXES


def db_for_path(path: Path, repo: Path = REPO) -> Path:
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except ValueError:
        return repo / "cross-domain/source-fingerprints.db"
    return (
        repo / "private/source-fingerprints.db"
        if rel.parts and rel.parts[0] == "private"
        else repo / "cross-domain/source-fingerprints.db"
    )


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


def _stored_path_exists(raw_path: str, repo: Path) -> bool:
    path = Path(raw_path)
    if not path.is_absolute():
        path = repo / path
    return path.is_file()


def _text_path_for_source(source_path: Path) -> Path | None:
    if source_path.name == "paper.pdf" and source_path.with_name("paper.md").is_file():
        return source_path.with_name("paper.md")
    if source_path.suffix.lower() in {".md", ".txt"}:
        return source_path
    companion = source_path.with_suffix(".md")
    return companion if companion.is_file() else None


def _fingerprint_record(
    source_path: Path,
    *,
    raw_path: str | None = None,
    text_path: Path | None = None,
    source_kind: str = "",
    repo: Path = REPO,
) -> dict:
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    before = source_path.stat()
    digest = sha256_file(source_path)
    text_digest = normalized_text_sha256(text_path) if text_path and text_path.is_file() else ""
    after = source_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed while hashing: {source_path}")
    return {
        "raw_path": raw_path or _stored_path(source_path, repo),
        "binary_sha256": digest,
        "size_bytes": after.st_size,
        "mtime_ns": after.st_mtime_ns,
        "text_sha256": text_digest,
        "source_kind": source_kind or source_path.suffix.lower().lstrip("."),
        "indexed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _upsert_records(conn: sqlite3.Connection, records: list[dict]) -> None:
    conn.executemany(
        """
        INSERT INTO source_fingerprints
            (raw_path,binary_sha256,size_bytes,mtime_ns,text_sha256,source_kind,indexed_at)
        VALUES (:raw_path,:binary_sha256,:size_bytes,:mtime_ns,:text_sha256,:source_kind,:indexed_at)
        ON CONFLICT(raw_path) DO UPDATE SET
            binary_sha256=excluded.binary_sha256,
            size_bytes=excluded.size_bytes,
            mtime_ns=excluded.mtime_ns,
            text_sha256=excluded.text_sha256,
            source_kind=excluded.source_kind,
            indexed_at=excluded.indexed_at
        """,
        records,
    )


def register_source(
    source_path: Path,
    *,
    raw_path: str | None = None,
    text_path: Path | None = None,
    source_kind: str = "",
    db_path: Path | None = None,
    repo: Path = REPO,
) -> dict:
    record = _fingerprint_record(
        source_path,
        raw_path=raw_path,
        text_path=text_path,
        source_kind=source_kind,
        repo=repo,
    )
    target_db = db_path or db_for_path(source_path, repo)
    conn = _connect(target_db)
    try:
        with conn:
            _upsert_records(conn, [record])
    finally:
        conn.close()
    return {
        key: record[key]
        for key in ("raw_path", "binary_sha256", "size_bytes", "text_sha256")
    }


def lookup_digest(
    binary_sha256: str,
    size_bytes: int,
    *,
    db_path: Path = CROSS_DOMAIN_DB,
    repo: Path = REPO,
) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{64}", binary_sha256) or size_bytes < 0:
        raise ValueError("invalid SHA-256 or size")
    if not db_path.is_file():
        return None
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM source_fingerprints
               WHERE binary_sha256=? AND size_bytes=?
               ORDER BY length(raw_path), raw_path""",
            (binary_sha256, size_bytes),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        if _stored_path_exists(row["raw_path"], repo):
            result = dict(row)
            result["match"] = "binary_sha256"
            return result
    return None


def lookup_exact(
    source_path: Path,
    *,
    db_path: Path | None = None,
    repo: Path = REPO,
    binary_sha256: str | None = None,
    size_bytes: int | None = None,
) -> dict | None:
    source_path = source_path.resolve()
    if not source_path.is_file():
        return None
    target_db = db_path or db_for_path(source_path, repo)
    if not target_db.is_file():
        return None
    size = source_path.stat().st_size
    if size_bytes is not None and size_bytes != size:
        return None
    digest = binary_sha256 or sha256_file(source_path)
    return lookup_digest(digest, size, db_path=target_db, repo=repo)


def lookup_text_candidate(
    text_path: Path,
    *,
    db_path: Path = CROSS_DOMAIN_DB,
    repo: Path | None = None,
) -> dict | None:
    if not text_path.is_file() or not db_path.is_file():
        return None
    digest = normalized_text_sha256(text_path)
    if not digest:
        return None
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """SELECT * FROM source_fingerprints WHERE text_sha256=?
               ORDER BY length(raw_path), raw_path""",
            (digest,),
        ).fetchall()
    finally:
        conn.close()
    repository = repo if repo is not None else db_path.parent.parent
    for row in rows:
        if _stored_path_exists(row["raw_path"], repository):
            result = dict(row)
            result["match"] = "normalized_text_sha256"
            return result
    return None


def _is_source_artifact(path: Path) -> bool:
    if (not path.is_file() or path.name.startswith(".")
            or path.name in SIDECAR_NAMES or path.name.endswith(".bak")):
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
    records = []
    for source_path in _iter_source_artifacts(roots):
        records.append(_fingerprint_record(
            source_path,
            text_path=_text_path_for_source(source_path),
            repo=repo,
        ))
    conn = _connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM source_fingerprints")
            _upsert_records(conn, records)
    finally:
        conn.close()
    return {"status": "rebuilt", "db": str(db_path), "indexed": len(records)}


def reconcile(
    *,
    db_path: Path = CROSS_DOMAIN_DB,
    roots: tuple[Path, ...] = RAW_ROOTS,
    repo: Path = REPO,
) -> dict:
    """Incrementally align the rebuildable cache with current Raw source artifacts."""
    if not db_path.is_file():
        return rebuild(db_path=db_path, roots=roots, repo=repo)
    current = {
        _stored_path(path, repo): path
        for path in _iter_source_artifacts(roots)
    }
    conn = _connect(db_path)
    try:
        rows = {
            row["raw_path"]: dict(row)
            for row in conn.execute(
                "SELECT raw_path,size_bytes,mtime_ns FROM source_fingerprints"
            ).fetchall()
        }
    finally:
        conn.close()
    added = sorted(set(current) - set(rows))
    removed = sorted(set(rows) - set(current))
    updated = sorted(
        raw_path for raw_path in set(current) & set(rows)
        if (
            current[raw_path].stat().st_size != rows[raw_path]["size_bytes"]
            or current[raw_path].stat().st_mtime_ns != rows[raw_path]["mtime_ns"]
        )
    )
    records = [
        _fingerprint_record(
            current[raw_path],
            raw_path=raw_path,
            text_path=_text_path_for_source(current[raw_path]),
            repo=repo,
        )
        for raw_path in added + updated
    ]
    if records or removed:
        conn = _connect(db_path)
        try:
            with conn:
                _upsert_records(conn, records)
                conn.executemany(
                    "DELETE FROM source_fingerprints WHERE raw_path=?",
                    [(raw_path,) for raw_path in removed],
                )
        finally:
            conn.close()
    return {
        "status": "reconciled",
        "db": str(db_path),
        "indexed": len(current),
        "added": len(added),
        "updated": len(updated),
        "removed": len(removed),
        "unchanged": len(current) - len(added) - len(updated),
    }


def ensure_index(
    *,
    private: bool = False,
    db_path: Path | None = None,
    roots: tuple[Path, ...] | None = None,
    repo: Path = REPO,
) -> dict:
    target_db = db_path or (PRIVATE_DB if private else CROSS_DOMAIN_DB)
    target_roots = roots or (PRIVATE_RAW_ROOTS if private else RAW_ROOTS)
    return reconcile(db_path=target_db, roots=target_roots, repo=repo)


def main() -> None:
    parser = argparse.ArgumentParser(description="可重建的摄入源文件 SHA-256/文本指纹索引")
    parser.add_argument("action", choices=("rebuild", "reconcile", "lookup", "register"))
    parser.add_argument("path", nargs="?")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--text-path")
    args = parser.parse_args()
    db_path = PRIVATE_DB if args.private else CROSS_DOMAIN_DB
    if args.action in {"rebuild", "reconcile"}:
        roots = PRIVATE_RAW_ROOTS if args.private else RAW_ROOTS
        function = rebuild if args.action == "rebuild" else reconcile
        result = function(db_path=db_path, roots=roots)
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

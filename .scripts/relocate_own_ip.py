#!/usr/bin/env python3
"""Hash-bound managed relocation of misfiled, user-confirmed intellectual property."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime
from uuid import uuid4

import graph_lib as gl
import ingest_check
import source_fingerprints as fingerprints
from derivation_state import sha256_file


REPO = Path(__file__).resolve().parent.parent
SOURCE_DIR = Path("academic/raw/reference-documents")
TARGET_DIRS = {Path("academic/raw/works/patents"), Path("academic/raw/works/software")}
GRAPH_COLUMNS = {
    "nodes": ("path",),
    "edges": ("subject", "object", "source"),
    "aliases": ("node_path",),
    "edge_evidence": ("source",),
    "edge_origins": ("origin_page", "source"),
    "managed_nodes": ("node_path", "created_origin_page"),
    "node_origins": ("node_path", "origin_page", "source"),
    "node_glosses": ("node_path", "origin_page", "source"),
    "node_description_reviews": ("node_path", "origin_page", "source"),
    "temporal_facts": ("subject", "object", "source"),
}


def digest_bytes(content):
    return hashlib.sha256(content).hexdigest()


def local_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe relative path: {relative}")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError(f"symlink not allowed: {relative}")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes repository: {relative}")
    return candidate


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_receipt(path, receipt):
    atomic_write(path, (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode())


def require_hash(path, expected):
    if not path.is_file() or path.is_symlink() or sha256_file(path) != expected:
        raise ValueError(f"missing file or SHA-256 mismatch: {path}")


def check_references(root, prefixes, declared):
    roots = [name for name in ("academic", "admin", "teaching", "business", "projects", "cross-domain")
             if (root / name).is_dir()]
    command = ["rg", "-l", "-F", "--glob", "!*.db*", "--glob", "!*.jsonl",
               "--glob", "!**/log.md", "--glob", "!**/ingest-reports/**"]
    for prefix in prefixes:
        command.extend(["-e", prefix])
    result = subprocess.run(command + ["--"] + roots, cwd=root, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError(f"reference discovery failed: {result.stderr}")
    extra = set(result.stdout.splitlines()) - set(declared)
    if extra:
        raise ValueError(f"undeclared references require review: {sorted(extra)}")


def prepare(root, manifest):
    if manifest.get("schema") != "own-ip-relocation-v1" or not manifest.get("reason", "").strip():
        raise ValueError("manifest requires own-ip-relocation-v1 and an explicit reason")
    if not isinstance(manifest.get("items"), list) or not manifest["items"]:
        raise ValueError("manifest.items must be nonempty")
    files, references, mapping = [], {}, {}
    for item in manifest["items"]:
        source, destination = Path(item["source"]), Path(item["destination"])
        if (source.parent != SOURCE_DIR or destination.parent not in TARGET_DIRS
                or source.suffix != ".pdf" or destination.name != source.name):
            raise ValueError("only same-name reference-document PDF pairs to own patents/software are supported")
        if str(source) in mapping:
            raise ValueError("duplicate source in manifest")
        for suffix, field in ((".pdf", "sha256"), (".md", "companion_sha256")):
            old, new = str(source.with_suffix(suffix)), str(destination.with_suffix(suffix))
            source_path, target_path = local_path(root, old), local_path(root, new)
            require_hash(source_path, item[field])
            if target_path.exists():
                raise ValueError(f"destination already exists: {new}")
            if not target_path.parent.is_dir():
                raise ValueError(f"destination directory must already exist: {new}")
            files.append({"source": old, "destination": new, "sha256": item[field]})
            mapping[old] = new
        old_node, new_node = str(source.with_suffix("")), str(destination.with_suffix(""))
        mapping[old_node] = new_node
        wiki = item["wiki"]
        if not wiki.startswith("academic/wiki/") or not wiki.endswith(".md"):
            raise ValueError("wiki must be an academic Markdown page")
        wiki_path = local_path(root, wiki)
        require_hash(wiki_path, item["wiki_sha256"])
        content = wiki_path.read_text(encoding="utf-8")
        if str(source.with_suffix(".md")) not in content:
            raise ValueError(f"wiki does not cite this source: {wiki}")
        references[wiki] = content
    if len({item["destination"] for item in files}) != len(files):
        raise ValueError("duplicate destination in manifest")
    check_references(root, [item["source"][:-4] for item in files if item["source"].endswith(".pdf")], references)
    rewrites = {}
    for wiki, content in references.items():
        updated = content
        for item in files:
            updated = updated.replace(item["source"], item["destination"])
        rewrites[wiki] = (content.encode(), updated.encode())
    log = "academic/wiki/log.md"
    log_path = local_path(root, log)
    original = log_path.read_bytes()
    moves = "; ".join(f"{item['source']} → {item['destination']}" for item in manifest["items"])
    entry = f"\n- {datetime.now().astimezone().isoformat(timespec='seconds')} | raw-relocation | {manifest['reason']} | {moves} | 原件与文本层字节不变；Wiki 来源、Graph lineage 与源指纹同步。\n"
    rewrites[log] = (original, original + entry.encode())
    return files, mapping, rewrites


def remap_graph(conn, mapping):
    conn.execute("PRAGMA defer_foreign_keys = ON")
    raw_nodes = {old[:-4]: new[:-4] for old, new in mapping.items() if old.endswith(".pdf")}
    for old, new in raw_nodes.items():
        row = conn.execute("SELECT type FROM nodes WHERE path=?", (old,)).fetchone()
        if row is None or row[0] != "raw":
            raise ValueError(f"expected existing Raw node: {old}")
        if conn.execute("SELECT 1 FROM nodes WHERE path=?", (new,)).fetchone():
            raise ValueError(f"destination graph node already exists: {new}")
    for table, columns in GRAPH_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for column in set(columns) & existing:
            for old, new in mapping.items():
                conn.execute(
                    f'UPDATE "{table}" SET "{column}"=? || substr("{column}", ?) '
                    f'WHERE "{column}"=? OR substr("{column}", 1, ?)=?',
                    (new, len(old) + 1, old, len(old) + 1, old + "#"),
                )
    for old, new in raw_nodes.items():
        conn.execute("INSERT OR IGNORE INTO aliases(alias,node_path) VALUES (?,?)", (old, new))
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise ValueError("graph foreign key validation failed")


def validate_pages(root, receipt, connection):
    warnings = []
    for relative in receipt["wiki_pages"]:
        page = root / relative
        errors, page_warnings = ingest_check.check_file(page, set(), set())
        graph_errors, graph_warnings = ingest_check.graph_checks(page, connection=connection)
        if errors or graph_errors:
            raise ValueError(f"validation failed for {relative}: {errors + graph_errors}")
        warnings.extend(page_warnings + graph_warnings)
    return warnings


def rollback_files(root, receipt_path, receipt):
    for rewrite in receipt["rewrites"]:
        path = local_path(root, rewrite["path"])
        if sha256_file(path) not in {rewrite["before_sha256"], rewrite["after_sha256"]}:
            raise ValueError(f"rollback would overwrite a later edit: {path}")
        require_hash(receipt_path.parent / rewrite["backup"], rewrite["before_sha256"])
    for item in receipt["files"]:
        require_hash(local_path(root, item["source"]), item["sha256"])
        target = local_path(root, item["destination"])
        if target.exists():
            require_hash(target, item["sha256"])
    for rewrite in receipt["rewrites"]:
        atomic_write(root / rewrite["path"], (receipt_path.parent / rewrite["backup"]).read_bytes())
    for item in receipt["files"]:
        local_path(root, item["destination"]).unlink(missing_ok=True)
    receipt["status"] = "rolled_back"
    save_receipt(receipt_path, receipt)


def finish(root, receipt_path, receipt, connection):
    for item in receipt["files"]:
        require_hash(local_path(root, item["destination"]), item["sha256"])
        old = local_path(root, item["source"])
        if old.exists():
            require_hash(old, item["sha256"])
    receipt["warnings"] = validate_pages(root, receipt, connection)
    fingerprint_db = root / "cross-domain/source-fingerprints.db"
    for item in receipt["files"]:
        if item["destination"].endswith(".pdf"):
            target = root / item["destination"]
            fingerprints.register_source(target, text_path=target.with_suffix(".md"),
                                         db_path=fingerprint_db, repo=root)
    index = fingerprints._connect(fingerprint_db)
    try:
        for item in receipt["files"]:
            index.execute("DELETE FROM source_fingerprints WHERE raw_path=?", (item["source"],))
        index.commit()
    finally:
        index.close()
    receipt["status"] = "cleanup_pending"
    save_receipt(receipt_path, receipt)
    for item in receipt["files"]:
        local_path(root, item["source"]).unlink(missing_ok=True)
    receipt["status"] = "completed"
    save_receipt(receipt_path, receipt)
    return receipt


def relocate(root, manifest=None, apply=False, resume=None):
    root = root.resolve()
    db_path = local_path(root, "cross-domain/graph.db")
    if not db_path.is_file():
        raise ValueError("existing public graph.db required")
    with gl.graph_writer_lock(db_path):
        conn = gl.connect(db_path)
        try:
            if resume:
                receipt_path = local_path(root, resume)
                if not receipt_path.is_relative_to(root / "temp/raw-relocations"):
                    raise ValueError("resume receipt must be in temp/raw-relocations")
                receipt = json.loads(receipt_path.read_text())
                if receipt.get("schema") != "own-ip-relocation-receipt-v1":
                    raise ValueError("invalid relocation receipt schema")
                if receipt.get("marker") != f"raw-relocation:{receipt_path.parent.name}":
                    raise ValueError("receipt transaction marker mismatch")
                for item in receipt["files"]:
                    source, target = Path(item["source"]), Path(item["destination"])
                    if (source.parent != SOURCE_DIR or target.parent not in TARGET_DIRS
                            or source.suffix not in {".pdf", ".md"} or source.name != target.name):
                        raise ValueError("receipt file escapes own-IP migration scope")
                for number, rewrite in enumerate(receipt["rewrites"]):
                    if (rewrite["backup"] != f"before-{number}.bin"
                            or not rewrite["path"].startswith("academic/wiki/")
                            or not rewrite["path"].endswith(".md")):
                        raise ValueError("invalid receipt rewrite path")
                marker = gl.get_metadata(conn, receipt["marker"])
                if marker == receipt["manifest_sha256"]:
                    return finish(root, receipt_path, receipt, conn)
                if marker:
                    raise ValueError("relocation marker mismatch")
                rollback_files(root, receipt_path, receipt)
                return receipt
            files, mapping, rewrites = prepare(root, manifest)
            if not apply:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    remap_graph(conn, mapping)
                finally:
                    conn.rollback()
                return {"status": "planned", "files": files, "wiki_pages": list(rewrites)[:-1],
                        "graph_mapping": mapping}
            transaction = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:10]
            directory = root / "temp/raw-relocations" / transaction
            directory.mkdir(parents=True)
            receipt_path = directory / "receipt.json"
            receipt = {"schema": "own-ip-relocation-receipt-v1", "status": "prepared",
                       "transaction_id": transaction, "reason": manifest["reason"],
                       "manifest_sha256": digest_bytes(json.dumps(manifest, sort_keys=True).encode()),
                       "marker": f"raw-relocation:{transaction}", "files": files,
                       "graph_mapping": mapping, "wiki_pages": list(rewrites)[:-1], "rewrites": [],
                       "receipt_path": str(receipt_path.relative_to(root))}
            for number, (relative, (before, after)) in enumerate(rewrites.items()):
                backup = f"before-{number}.bin"
                atomic_write(directory / backup, before)
                receipt["rewrites"].append({"path": relative, "backup": backup,
                                            "before_sha256": digest_bytes(before),
                                            "after_sha256": digest_bytes(after)})
            save_receipt(receipt_path, receipt)
            try:
                conn.execute("BEGIN IMMEDIATE")
                remap_graph(conn, mapping)
                for number, item in enumerate(files):
                    source, target = root / item["source"], root / item["destination"]
                    require_hash(source, item["sha256"])
                    staged = directory / f"raw-{number}.bin"
                    shutil.copy2(source, staged)
                    require_hash(staged, item["sha256"])
                    with staged.open("rb") as handle:
                        os.fsync(handle.fileno())
                    os.link(staged, target)
                    staged.unlink()
                    require_hash(target, item["sha256"])
                for rewrite in receipt["rewrites"]:
                    path = root / rewrite["path"]
                    require_hash(path, rewrite["before_sha256"])
                    atomic_write(path, rewrites[rewrite["path"]][1])
                receipt["warnings"] = validate_pages(root, receipt, conn)
                gl.set_metadata(conn, receipt["marker"], receipt["manifest_sha256"])
                conn.commit()
            except BaseException:
                conn.rollback()
                if gl.get_metadata(conn, receipt["marker"]) != receipt["manifest_sha256"]:
                    rollback_files(root, receipt_path, receipt)
                raise
            receipt["status"] = "committed"
            save_receipt(receipt_path, receipt)
            return finish(root, receipt_path, receipt, conn)
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--resume", help="repository-relative receipt path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text()) if args.manifest else None
    result = relocate(REPO, manifest, apply=args.apply, resume=args.resume)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

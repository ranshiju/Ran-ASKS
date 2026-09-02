#!/usr/bin/env python3
"""Governed remediation for byte-identical duplicate paper ingests."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

import graph_lib as gl
import source_fingerprints as sf


REPO = Path(__file__).resolve().parent.parent


def _page_id(value: str) -> str:
    page = str(value or "").strip().removesuffix(".md")
    if page.startswith("/") or ".." in Path(page).parts:
        raise ValueError(f"非法 Wiki 路径: {value}")
    parts = Path(page).parts
    if len(parts) < 4 or parts[1:3] != ("wiki", "papers"):
        raise ValueError(f"仅支持 <domain>/wiki/papers/<id>: {value}")
    return page


def _local_sources(frontmatter: dict) -> list[str]:
    return [
        str(source).split("#", 1)[0].strip()
        for source in gl.parse_list_field(frontmatter, "sources")
        if str(source).strip() and not str(source).startswith(("http://", "https://", "synology://"))
    ]


def _source_pdf(repo: Path, page: str, frontmatter: dict) -> tuple[str, Path, str]:
    for source in _local_sources(frontmatter):
        source_path = repo / source
        candidates = [source_path]
        if source_path.suffix.lower() != ".pdf":
            candidates.extend((source_path.with_suffix(".pdf"), source_path.parent / "paper.pdf"))
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                return source, candidate, gl.raw_node_path(source, page)
    raise ValueError(f"页面无法解析到 Raw PDF: {page}")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def build_plan(
    canonical_page: str,
    duplicate_page: str,
    *,
    repo: Path = REPO,
    graph_db: Path | None = None,
) -> dict:
    canonical_page = _page_id(canonical_page)
    duplicate_page = _page_id(duplicate_page)
    if canonical_page == duplicate_page:
        raise ValueError("canonical 与 duplicate 不能相同")
    canonical_file = repo / f"{canonical_page}.md"
    duplicate_file = repo / f"{duplicate_page}.md"
    if not canonical_file.is_file() or not duplicate_file.is_file():
        raise FileNotFoundError("canonical 或 duplicate Wiki 页面不存在")
    canonical_fm = gl.read_frontmatter(canonical_file)
    duplicate_fm = gl.read_frontmatter(duplicate_file)
    if canonical_fm.get("type") != "paper-summary" or duplicate_fm.get("type") != "paper-summary":
        raise ValueError("两个页面都必须是 paper-summary")
    canonical_source, canonical_pdf, canonical_raw = _source_pdf(
        repo, canonical_page, canonical_fm)
    duplicate_source, duplicate_pdf, duplicate_raw = _source_pdf(
        repo, duplicate_page, duplicate_fm)
    if not canonical_raw or not duplicate_raw or canonical_raw == duplicate_raw:
        raise ValueError("两个页面必须映射到不同 Raw 包")
    canonical_stat = canonical_pdf.stat()
    duplicate_stat = duplicate_pdf.stat()
    canonical_hash = sf.sha256_file(canonical_pdf)
    duplicate_hash = sf.sha256_file(duplicate_pdf)
    if canonical_stat.st_size != duplicate_stat.st_size or canonical_hash != duplicate_hash:
        raise ValueError("源 PDF 并非字节级完全一致，禁止治理")

    db_path = graph_db or gl.graph_db_for(canonical_page)
    blockers = []
    if Path(db_path).is_file():
        conn = gl.connect(db_path)
        try:
            if _table_exists(conn, "nodes"):
                for node in (canonical_page, duplicate_page, canonical_raw, duplicate_raw):
                    if not conn.execute("SELECT 1 FROM nodes WHERE path=?", (node,)).fetchone():
                        blockers.append(f"graph 缺节点: {node}")
            if _table_exists(conn, "edges"):
                rows = conn.execute(
                    "SELECT subject,predicate,object FROM edges WHERE subject=? OR object=?",
                    (duplicate_raw, duplicate_raw),
                ).fetchall()
                for row in rows:
                    triple = (row["subject"], row["predicate"], row["object"])
                    if triple != (duplicate_page, "来源", duplicate_raw):
                        blockers.append("duplicate Raw 存在非来源关系边: " + " | ".join(triple))
        finally:
            conn.close()
    if blockers:
        raise ValueError("; ".join(blockers))
    return {
        "status": "ready",
        "canonical_page": canonical_page,
        "duplicate_page": duplicate_page,
        "canonical_wiki": str(canonical_file.relative_to(repo)),
        "duplicate_wiki": str(duplicate_file.relative_to(repo)),
        "canonical_source": canonical_source,
        "duplicate_source": duplicate_source,
        "canonical_raw": canonical_raw,
        "duplicate_raw": duplicate_raw,
        "canonical_pdf": str(canonical_pdf.relative_to(repo)),
        "duplicate_pdf": str(duplicate_pdf.relative_to(repo)),
        "binary_sha256": canonical_hash,
        "size_bytes": canonical_stat.st_size,
        "graph_db": str(Path(db_path)),
        "raw_policy": "read_only_preserved",
    }


def _remove_graph_duplicate(conn: sqlite3.Connection, plan: dict) -> dict:
    page = plan["duplicate_page"]
    raw_node = plan["duplicate_raw"]
    canonical_page = plan["canonical_page"]
    canonical_raw = plan["canonical_raw"]
    direct_ids = [
        row["id"] for row in conn.execute(
            "SELECT id FROM edges WHERE subject=? OR object=?", (page, page)
        )
    ]
    origin_ids = []
    if _table_exists(conn, "edge_origins"):
        origin_ids = [
            row["edge_id"] for row in conn.execute(
                "SELECT edge_id FROM edge_origins WHERE origin_page=?", (page,)
            )
        ]
        conn.execute("DELETE FROM edge_origins WHERE origin_page=?", (page,))
    removed_edges = 0
    for edge_id in sorted(set(direct_ids + origin_ids)):
        has_other_origin = (
            _table_exists(conn, "edge_origins")
            and conn.execute("SELECT 1 FROM edge_origins WHERE edge_id=?", (edge_id,)).fetchone()
        )
        if edge_id in direct_ids or not has_other_origin:
            if _table_exists(conn, "edge_evidence"):
                conn.execute("DELETE FROM edge_evidence WHERE edge_id=?", (edge_id,))
            if _table_exists(conn, "edge_origins"):
                conn.execute("DELETE FROM edge_origins WHERE edge_id=?", (edge_id,))
            removed_edges += conn.execute("DELETE FROM edges WHERE id=?", (edge_id,)).rowcount
    if _table_exists(conn, "temporal_facts"):
        conn.execute("DELETE FROM temporal_facts WHERE subject=? OR object=?", (page, page))

    for source_node, target_node in ((page, canonical_page), (raw_node, canonical_raw)):
        aliases = [row["alias"] for row in conn.execute(
            "SELECT alias FROM aliases WHERE node_path=?", (source_node,)
        )]
        aliases.append(source_node)
        for alias in dict.fromkeys(aliases):
            conn.execute(
                "INSERT OR IGNORE INTO aliases(alias,node_path) VALUES(?,?)",
                (alias, target_node),
            )
        conn.execute("DELETE FROM aliases WHERE node_path=?", (source_node,))
        conn.execute("DELETE FROM nodes WHERE path=?", (source_node,))
    return {"edges_removed": removed_edges, "aliases_preserved": 2}


def apply_plan(plan: dict, *, repo: Path = REPO, graph_db: Path | None = None) -> dict:
    duplicate_file = repo / plan["duplicate_wiki"]
    if not duplicate_file.is_file():
        raise FileNotFoundError(duplicate_file)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    archive = (
        repo / "cross-domain" / "duplicate-remediation" / "archive" / stamp
        / plan["duplicate_wiki"]
    )
    while archive.exists():
        archive = archive.with_name(f"{archive.stem}-{uuid.uuid4().hex[:6]}{archive.suffix}")
    db_path = Path(graph_db or plan["graph_db"])
    work = repo / "temp" / "duplicate-remediation" / uuid.uuid4().hex
    backup_db = work / "graph-before.sqlite"
    work.mkdir(parents=True, exist_ok=False)
    index_path = repo / plan["duplicate_page"].split("/wiki/", 1)[0] / "wiki" / "index.md"
    wiki_log = index_path.with_name("log.md")
    old_index = index_path.read_text(encoding="utf-8") if index_path.is_file() else None
    old_log = wiki_log.read_text(encoding="utf-8") if wiki_log.is_file() else None
    raw_paths = [repo / plan["canonical_pdf"], repo / plan["duplicate_pdf"]]
    raw_before = {path: sf.sha256_file(path) for path in raw_paths}

    conn = gl.connect(db_path)
    backup_conn = sqlite3.connect(backup_db)
    conn.backup(backup_conn)
    backup_conn.close()
    graph_result = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        graph_result = _remove_graph_duplicate(conn, plan)
        conn.commit()
        conn.close()
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.replace(duplicate_file, archive)
        if old_index is not None:
            duplicate_link = f"[[papers/{Path(plan['duplicate_page']).name}]]"
            kept = [line for line in old_index.splitlines() if duplicate_link not in line]
            index_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        audit = {
            **plan,
            "status": "remediated",
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "archive": str(archive.relative_to(repo)),
            "graph": graph_result,
        }
        log_dir = repo / "cross-domain" / "duplicate-remediation"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")
        if old_log is not None:
            with wiki_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\n- {audit['timestamp'][:10]}: 精确重复治理 `{plan['duplicate_page']}` -> "
                    f"`{plan['canonical_page']}`（SHA-256 `{plan['binary_sha256']}`；Raw 保留不变）。\n"
                )
        raw_after = {path: sf.sha256_file(path) for path in raw_paths}
        if raw_after != raw_before:
            raise RuntimeError("Raw 文件在治理期间发生变化，已中止")
        shutil.rmtree(work)
        return audit
    except Exception:
        if conn:
            try:
                conn.rollback()
                conn.close()
            except Exception:
                pass
        restore = gl.connect(db_path)
        snapshot = sqlite3.connect(backup_db)
        snapshot.backup(restore)
        snapshot.close()
        restore.close()
        if archive.is_file() and not duplicate_file.exists():
            duplicate_file.parent.mkdir(parents=True, exist_ok=True)
            os.replace(archive, duplicate_file)
        if old_index is not None:
            index_path.write_text(old_index, encoding="utf-8")
        if old_log is not None:
            wiki_log.write_text(old_log, encoding="utf-8")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="字节级同源论文的 Raw 不可变重复治理")
    parser.add_argument("--canonical-page", required=True)
    parser.add_argument("--duplicate-page", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        plan = build_plan(args.canonical_page, args.duplicate_page)
        result = apply_plan(plan) if args.apply else {**plan, "status": "dry_run"}
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

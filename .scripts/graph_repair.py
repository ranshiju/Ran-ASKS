#!/usr/bin/env python3
"""graph_repair.py — graph.db 确定性存量修复

修复范围:
  1. 将旧 edge confidence `[可追溯]` 归一为 `可追溯`，`medium` 归一为 `推断`。
  2. 将旧 ``Raw → 事实支撑 → Wiki`` 迁移为 ``Wiki → 来源 → Raw``，并补齐所有
     Wiki/Raw 文档文件的节点、同 stem Raw 包 aliases 与来源直连。
  3. 合并完全重复的同一语义边，保留最早 id 和一个可选 locator。

安全约束:
  - 默认 --dry-run，只报告；--apply 才写入。
  - 不改 raw/wiki；只改派生主数据 graph.db。
  - 每项修复都基于确定性规则，不调用 LLM。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

MISSING_CONFIDENCE_VALUES = (None, "")
CONFIDENCE_REPAIRS = {
    "[可追溯]": "可追溯",
    "medium": "推断",
}


def confidence_counts(conn):
    out = {}
    for old, new in CONFIDENCE_REPAIRS.items():
        count = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence=?", (old,)).fetchone()[0]
        if count:
            out[old] = count
    missing = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE confidence IS NULL OR confidence=''"
    ).fetchone()[0]
    if missing:
        out["null"] = missing
    return out


def duplicate_counts(conn):
    rows = conn.execute(
        "SELECT subject, predicate, object, COALESCE(confidence,'') AS confidence, COUNT(*) AS n "
        "FROM edges GROUP BY subject, predicate, object, COALESCE(confidence,'') HAVING n > 1"
    ).fetchall()
    removed = sum(r["n"] - 1 for r in rows)
    return len(rows), removed


def repair_confidence(conn, apply):
    result = {}
    for old, new in CONFIDENCE_REPAIRS.items():
        count = conn.execute("SELECT COUNT(*) FROM edges WHERE confidence=?", (old,)).fetchone()[0]
        result[old] = count
        if apply and count:
            conn.execute("UPDATE edges SET confidence=? WHERE confidence=?", (new, old))
    return result


def migrate_raw_links(conn, apply):
    """Reverse legacy Raw support edges; keep only a precise locator if present."""
    rows = conn.execute(
        "SELECT e.id,e.subject,e.object,e.source,e.is_sr FROM edges e "
        "JOIN nodes r ON r.path=e.subject AND r.type='raw' "
        "WHERE e.predicate='事实支撑'"
    ).fetchall()
    created = reused = removed = 0
    for row in rows:
        locator = str(row["source"] or "")
        if locator.endswith("#全篇"):
            locator = ""
        existing = conn.execute(
            "SELECT id,source FROM edges WHERE subject=? AND predicate='来源' AND object=?",
            (row["object"], row["subject"]),
        ).fetchone()
        if existing:
            reused += 1
            new_id = existing["id"]
            if apply and locator and not str(existing["source"] or "").strip():
                conn.execute("UPDATE edges SET source=? WHERE id=?", (locator, new_id))
        else:
            created += 1
            new_id = None
            if apply:
                new_id = conn.execute(
                    "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
                    "VALUES (?,?,?,?,?,?)",
                    (row["object"], "来源", row["subject"], "可追溯", locator, row["is_sr"]),
                ).lastrowid
        if apply:
            gl.add_edge_origin(conn, new_id, row["object"], locator)
            conn.execute("DELETE FROM edge_evidence WHERE edge_id=?", (row["id"],))
            conn.execute("DELETE FROM edge_origins WHERE edge_id=?", (row["id"],))
            conn.execute("DELETE FROM edges WHERE id=?", (row["id"],))
        removed += 1
    return {"legacy": len(rows), "created": created, "reused": reused, "removed": removed}


def sync_file_nodes_and_source_edges(conn, apply):
    """让所有 Wiki 文件和其 Raw sources 符合文件节点模型。"""
    pages = gl.collect_pages()
    stats = {
        "wiki_files": len(pages),
        "wiki_nodes_created": 0,
        "raw_files": 0,
        "raw_packages": 0,
        "sourced_raw_packages": 0,
        "raw_nodes_created": 0,
        "source_edges_created": 0,
        "source_edges_reused": 0,
        "aliases_created": 0,
    }
    raw_packages = set()
    sourced_raw_packages = set()
    missing_raw_counted = set()
    missing_alias_counted = set()
    for page in pages:
        fm = gl.read_frontmatter(page)
        if not gl.node_exists(conn, page):
            stats["wiki_nodes_created"] += 1
            if apply:
                ptype = fm.get("type", "")
                node_type = (
                    "hub" if ptype == "topic-hub" else
                    "timeline-summary" if ptype == "timeline-summary" else
                    "people" if ptype == "people" else "page"
                )
                gl.ensure_node(
                    conn, page, fm.get("title", Path(page).name), node_type,
                    fm.get("source_type", ""), str(fm.get("date", "")),
                    fm.get("status", "current"), 1 if gl.has_raw_source(fm) else 0,
                )
        elif apply:
            conn.execute(
                "UPDATE nodes SET has_raw_source=? WHERE path=?",
                (1 if gl.has_raw_source(fm) else 0, page),
            )

        seen_for_page = set()
        for source in gl.parse_list_field(fm, "sources"):
            raw_path = gl.raw_node_path(source, page)
            if not raw_path or raw_path == page or raw_path in seen_for_page:
                continue
            seen_for_page.add(raw_path)
            raw_packages.add(raw_path)
            sourced_raw_packages.add(raw_path)
            if not gl.node_exists(conn, raw_path) and raw_path not in missing_raw_counted:
                missing_raw_counted.add(raw_path)
                stats["raw_nodes_created"] += 1
                if apply:
                    gl.ensure_node(conn, raw_path, gl.raw_node_title(raw_path), "raw")

            qualified_source_file = gl.raw_file_path(source, page)
            aliases = [qualified_source_file]
            source_target = gl.REPO / qualified_source_file
            if source_target.parent.is_dir():
                aliases.extend(
                    str(candidate.relative_to(gl.REPO))
                    for candidate in source_target.parent.glob(f"{source_target.stem}.*")
                    if candidate.is_file()
                )
            for alias in dict.fromkeys(a for a in aliases if a and a != raw_path):
                alias_key = (alias, raw_path)
                alias_row = conn.execute(
                    "SELECT 1 FROM aliases WHERE alias=? AND node_path=?", alias_key
                ).fetchone()
                if not alias_row and alias_key not in missing_alias_counted:
                    missing_alias_counted.add(alias_key)
                    stats["aliases_created"] += 1
                    if apply:
                        conn.execute(
                            "INSERT OR REPLACE INTO aliases(alias,node_path) VALUES (?,?)",
                            (alias, raw_path),
                        )

            existing = conn.execute(
                "SELECT id FROM edges WHERE subject=? AND predicate='来源' AND object=?",
                (page, raw_path),
            ).fetchone()
            locator = str(source).strip() if "#" in str(source) else ""
            if existing:
                stats["source_edges_reused"] += 1
                if apply:
                    gl.add_edge_origin(conn, existing["id"], page, locator)
            else:
                stats["source_edges_created"] += 1
                if apply:
                    edge_id = conn.execute(
                        "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
                        "VALUES (?,?,?,?,?,0)",
                        (page, "来源", raw_path, gl.DEFAULT_CONFIDENCE, locator),
                    ).lastrowid
                    gl.add_edge_origin(conn, edge_id, page, locator)
    raw_files = []
    for domain in gl.SUBPROJECTS:
        raw_root = gl.REPO / domain / "raw"
        if not raw_root.is_dir():
            continue
        raw_files.extend(
            path for path in raw_root.rglob("*")
            if path.is_file() and path.suffix.lower() in gl.RAW_DOCUMENT_SUFFIXES
        )
    stats["raw_files"] = len(raw_files)
    for raw_file in raw_files:
        alias = str(raw_file.relative_to(gl.REPO)).replace("\\", "/")
        raw_path = gl.raw_node_path(alias)
        if not raw_path:
            continue
        raw_packages.add(raw_path)
        if not gl.node_exists(conn, raw_path) and raw_path not in missing_raw_counted:
            missing_raw_counted.add(raw_path)
            stats["raw_nodes_created"] += 1
            if apply:
                gl.ensure_node(conn, raw_path, gl.raw_node_title(raw_path), "raw")
        alias_key = (alias, raw_path)
        alias_row = conn.execute(
            "SELECT 1 FROM aliases WHERE alias=? AND node_path=?", alias_key
        ).fetchone()
        if (alias != raw_path and not alias_row
                and alias_key not in missing_alias_counted):
            missing_alias_counted.add(alias_key)
            stats["aliases_created"] += 1
            if apply:
                conn.execute(
                    "INSERT OR REPLACE INTO aliases(alias,node_path) VALUES (?,?)", alias_key
                )
    stats["raw_packages"] = len(raw_packages)
    stats["sourced_raw_packages"] = len(sourced_raw_packages)
    return stats


def merge_duplicate_edges(conn, apply):
    rows = conn.execute(
        "SELECT subject, predicate, object, COALESCE(confidence,'') AS confidence, MIN(id) AS keep_id, "
        "COUNT(*) AS n FROM edges GROUP BY subject, predicate, object, COALESCE(confidence,'') HAVING n > 1"
    ).fetchall()
    groups = 0
    removed = 0
    for row in rows:
        groups += 1
        group_rows = conn.execute(
            "SELECT id, source, is_sr FROM edges WHERE subject=? AND predicate=? AND object=? "
            "AND COALESCE(confidence,'')=? ORDER BY id",
            (row["subject"], row["predicate"], row["object"], row["confidence"]),
        ).fetchall()
        keep_id = row["keep_id"]
        if apply:
            keep_source = str(group_rows[0]["source"] or "")
            for gr in group_rows:
                if gr["id"] == keep_id:
                    continue
                if not keep_source and gr["source"]:
                    keep_source = str(gr["source"])
                    conn.execute("UPDATE edges SET source=? WHERE id=?", (keep_source, keep_id))
                for evidence in conn.execute(
                    "SELECT source,evidence_quote,is_sr FROM edge_evidence WHERE edge_id=?",
                    (gr["id"],),
                ).fetchall():
                    gl.add_edge_evidence(
                        conn, keep_id, evidence["source"], evidence["evidence_quote"], evidence["is_sr"])
                for origin in conn.execute(
                    "SELECT origin_page,source FROM edge_origins WHERE edge_id=?",
                    (gr["id"],),
                ).fetchall():
                    gl.add_edge_origin(conn, keep_id, origin["origin_page"], origin["source"])
                conn.execute("DELETE FROM edges WHERE id=?", (gr["id"],))
        removed += row["n"] - 1
    return {"groups": groups, "removed": removed}


def build_plan(conn):
    conf_counts = confidence_counts(conn)
    raw_links = migrate_raw_links(conn, False)
    file_model = sync_file_nodes_and_source_edges(conn, False)
    dups = duplicate_counts(conn)
    return {
        "confidence": conf_counts,
        "raw_links": raw_links,
        "file_model": file_model,
        "duplicates": {"groups": dups[0], "removed": dups[1]},
    }


def execute(conn):
    conn.execute("BEGIN")
    try:
        conf_result = repair_confidence(conn, True)
        raw_link_result = migrate_raw_links(conn, True)
        file_model_result = sync_file_nodes_and_source_edges(conn, True)
        duplicate_result = merge_duplicate_edges(conn, True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"confidence": conf_result, "raw_links": raw_link_result,
            "file_model": file_model_result, "duplicates": duplicate_result}


def execute_raw_links(conn, apply):
    if not apply:
        return {"confidence": {}, "raw_links": migrate_raw_links(conn, False),
                "file_model": sync_file_nodes_and_source_edges(conn, False),
                "duplicates": {"groups": 0, "removed": 0}}
    conn.execute("BEGIN")
    try:
        result = migrate_raw_links(conn, True)
        file_model = sync_file_nodes_and_source_edges(conn, True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"confidence": {}, "raw_links": result, "file_model": file_model,
            "duplicates": {"groups": 0, "removed": 0}}


def format_plan(plan, applied=False):
    lines = []
    prefix = "已执行" if applied else "待执行"
    lines.append(f"[{prefix}] graph.db 存量修复 plan")
    conf = plan["confidence"]
    if conf:
        labels = []
        for label, count in conf.items():
            target = CONFIDENCE_REPAIRS.get(label, "推断" if label == "null" else label)
            labels.append(f"'{label}'→'{target}'={count}")
        lines.append("confidence: " + ", ".join(labels))
    raw_links = plan.get("raw_links", {})
    if raw_links.get("legacy"):
        lines.append(
            "raw_links: legacy={legacy} create={created} reuse={reused} remove={removed}".format(
                **raw_links))
    file_model = plan.get("file_model", {})
    file_changes = sum(file_model.get(key, 0) for key in (
        "wiki_nodes_created", "raw_nodes_created", "source_edges_created", "aliases_created"
    ))
    if file_changes:
        lines.append(
            "file_model: wiki_nodes={wiki_nodes_created} raw_nodes={raw_nodes_created} "
            "source_edges={source_edges_created} aliases={aliases_created} "
            "(wiki_files={wiki_files} raw_files={raw_files} raw_packages={raw_packages} "
            "sourced_raw_packages={sourced_raw_packages})".format(**file_model)
        )
    du = plan["duplicates"]
    if du.get("groups"):
        lines.append(f"duplicates: groups={du.get('groups', 0)} removed={du.get('removed', 0)}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="graph.db 路径，默认 cross-domain/graph.db")
    parser.add_argument("--apply", action="store_true", help="实际写入；默认 dry-run")
    parser.add_argument("--dry-run", action="store_true", help="只输出修复计划，不写入")
    parser.add_argument("--raw-links-only", action="store_true",
                        help="只迁移/补齐文件节点与 Wiki→来源→Raw 模型")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else gl.GRAPH_DB
    if not db_path.exists():
        print(f"ERROR: graph.db 不存在: {db_path}", file=sys.stderr)
        return 2
    conn = gl.connect(str(db_path))
    try:
        if args.raw_links_only:
            apply = args.apply and not args.dry_run
            result = execute_raw_links(conn, apply)
            print(format_plan(result, applied=apply))
        elif args.apply and not args.dry_run:
            result = execute(conn)
            print(format_plan(result, applied=True))
        else:
            plan = build_plan(conn)
            print(format_plan(plan, applied=False))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

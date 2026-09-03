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
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

MISSING_CONFIDENCE_VALUES = (None, "")
CONFIDENCE_REPAIRS = {
    "[可追溯]": "可追溯",
    "medium": "推断",
}
GENERIC_SYMBOL_NAMES = {
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi", "rho",
    "sigma", "tau", "upsilon", "phi", "chi", "psi", "omega",
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


def targeted_orphan_nodes(conn, node_paths, apply=False):
    """Remove only explicitly named, disconnected entity nodes."""
    results = []
    for node_path in dict.fromkeys(str(path) for path in node_paths if str(path)):
        row = conn.execute(
            "SELECT path,title,type FROM nodes WHERE path=?", (node_path,)
        ).fetchone()
        if not row:
            results.append({"path": node_path, "decision": "missing", "removed": False})
            continue
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE subject=? OR object=?",
            (node_path, node_path),
        ).fetchone()[0]
        temporal_count = conn.execute(
            "SELECT COUNT(*) FROM temporal_facts WHERE subject=? OR object=?",
            (node_path, node_path),
        ).fetchone()[0]
        aliases = [item[0] for item in conn.execute(
            "SELECT alias FROM aliases WHERE node_path=? ORDER BY alias", (node_path,)
        ).fetchall()]
        eligible = row["type"] == "entity" and edge_count == 0 and temporal_count == 0
        result = {
            "path": node_path,
            "title": row["title"],
            "type": row["type"],
            "edge_count": edge_count,
            "temporal_count": temporal_count,
            "aliases": aliases,
            "decision": "remove" if eligible else "blocked",
            "removed": False,
        }
        if apply and eligible:
            conn.execute("DELETE FROM aliases WHERE node_path=?", (node_path,))
            conn.execute("DELETE FROM nodes WHERE path=?", (node_path,))
            result["removed"] = True
        results.append(result)
    return results


def keyword_description_issue(title, description):
    """Return a conservative defect label; valid short domain definitions survive."""
    title = str(title or "").strip()
    description = str(description or "").strip()
    if not description:
        return "missing"
    normalized_title = re.sub(r"[\W_]+", "", title, flags=re.UNICODE).casefold()
    normalized_description = re.sub(
        r"[\W_]+", "", description, flags=re.UNICODE,
    ).casefold()
    if normalized_description == normalized_title:
        return "title_only"
    if re.match(
        r"^(?:在)?(?:本文|本论文|本研究|该文档|该论文|该文章|该研究)"
        r"(?:中|里|所|的|提出|研究|关于|用于|建议|使用)",
        description,
    ):
        return "deictic_context"
    if re.match(r"^Agent-confirmed abbreviation kind\s*:", description, re.I):
        return "identity_metadata"
    if re.search(r"TODO|待补充|待完善|暂无说明|unknown|placeholder|<--", description, re.I):
        return "placeholder"
    if "\n" in description or description.startswith(("-", "*", "#")):
        return "not_single_sentence"
    if len(description) > 300:
        return "too_long"
    return ""


def keyword_identity_issue(title):
    """Reject only deterministic non-concept labels from description generation."""
    text = str(title or "").strip()
    if not text:
        return "empty_title"
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return "year_label"
    if re.fullmatch(r"[\d\W_]+", text, re.UNICODE):
        return "numeric_or_symbol_label"
    if (
        re.fullmatch(r"[A-Za-z]", text)
        or re.fullmatch(r"[\u0370-\u03ff]", text)
        or text.casefold() in GENERIC_SYMBOL_NAMES
    ):
        return "generic_symbol_label"
    if re.match(r"https?://", text, re.I):
        return "url_label"
    return ""


def _keyword_identity_issues(rows):
    """Return deterministic and review-only identity defects keyed by node path."""
    issues = {}
    titles = {}
    for row in rows:
        path = str(row["path"])
        title = str(row["title"] or "").strip()
        issue = keyword_identity_issue(title) or keyword_identity_issue(path)
        if issue:
            issues[path] = issue
        if title:
            titles.setdefault(title.casefold(), []).append(path)

    for paths in titles.values():
        if len(paths) > 1:
            for path in paths:
                issues.setdefault(path, "duplicate_title")

    # Historical ingestion sometimes created both the Chinese label and a second
    # node whose title concatenates that label with its English rendering.  This
    # is a review gate only: it blocks description generation but never merges.
    for row in rows:
        path = str(row["path"])
        title = str(row["title"] or "").strip()
        match = re.fullmatch(
            r"(?P<prefix>.*[\u3400-\u9fff])(?P<suffix>[^\u3400-\u9fff]+)",
            title,
        )
        if not match or len(re.findall(r"[A-Za-z]", match.group("suffix"))) < 3:
            continue
        peers = [candidate for candidate in titles.get(match.group("prefix").casefold(), []) if candidate != path]
        if not peers:
            continue
        issues.setdefault(path, "possible_bilingual_duplicate")
        for peer in peers:
            issues.setdefault(peer, "possible_bilingual_duplicate")
    return issues


def _node_origin_pages(conn, node_path):
    pages = [str(row[0]) for row in conn.execute(
        "SELECT origin_page FROM node_origins WHERE node_path=? ORDER BY origin_page",
        (node_path,),
    )]
    pages.extend(str(row[0]) for row in conn.execute(
        "SELECT DISTINCT eo.origin_page FROM edge_origins eo "
        "JOIN edges e ON e.id=eo.edge_id "
        "WHERE e.subject=? OR e.object=? ORDER BY eo.origin_page",
        (node_path, node_path),
    ))
    pages.extend(str(row[0]) for row in conn.execute(
        "SELECT DISTINCT CASE WHEN e.subject=? THEN e.object ELSE e.subject END AS page "
        "FROM edges e JOIN nodes n ON n.path=CASE WHEN e.subject=? THEN e.object ELSE e.subject END "
        "WHERE (e.subject=? OR e.object=?) AND n.type='page' ORDER BY page",
        (node_path, node_path, node_path, node_path),
    ))
    return list(dict.fromkeys(page for page in pages if page))


def _page_raw_inputs(page):
    target = gl.REPO / f"{page}.md"
    if not target.is_file():
        return []
    fm = gl.read_frontmatter(target)
    inputs = []
    for source in gl.parse_list_field(fm, "sources"):
        source_file = gl.raw_file_path(source, page)
        if not source_file or not (gl.REPO / source_file).is_file():
            continue
        inputs.append(source_file)
    return list(dict.fromkeys(inputs))


def semantic_description_audit(conn, details=False):
    """Audit legacy keyword descriptions and produce a source-backed re-ingest set."""
    rows = conn.execute(
        "SELECT path,title,description FROM nodes "
        "WHERE type='entity' AND entity_subtype='keyword' ORDER BY path"
    ).fetchall()
    reason_counts = {}
    identity_issue_counts = {}
    items = []
    reingest_pages = {}
    identity_issues = _keyword_identity_issues(rows)
    for row in rows:
        reason = keyword_description_issue(row["title"], row["description"])
        if not reason:
            continue
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        identity_issue = identity_issues.get(str(row["path"]), "")
        if identity_issue:
            identity_issue_counts[identity_issue] = identity_issue_counts.get(identity_issue, 0) + 1
        gloss_count = conn.execute(
            "SELECT COUNT(*) FROM node_glosses WHERE node_path=?", (row["path"],)
        ).fetchone()[0]
        origins = _node_origin_pages(conn, row["path"])
        backed_origins = []
        for page in (origins if not identity_issue else []):
            raw_inputs = _page_raw_inputs(page)
            if not raw_inputs:
                continue
            backed_origins.append({"page": page, "raw_inputs": raw_inputs})
            reingest_pages.setdefault(page, set()).update(raw_inputs)
        items.append({
            "node": str(row["path"]),
            "title": str(row["title"] or ""),
            "issue": reason,
            "identity_issue": identity_issue,
            "gloss_count": gloss_count,
            "origin_count": len(origins),
            "source_backed_origins": backed_origins,
            "decision": (
                "identity_review" if identity_issue else
                "reingest_sources" if backed_origins else
                "insufficient_lineage"
            ),
        })
    recoverable = sum(item["decision"] == "reingest_sources" for item in items)
    lineage_blocked = sum(item["decision"] == "insufficient_lineage" for item in items)
    result = {
        "keyword_count": len(rows),
        "valid_description_count": len(rows) - len(items),
        "issue_count": len(items),
        "issues": reason_counts,
        "identity_issues": identity_issue_counts,
        "identity_review_nodes": sum(item["decision"] == "identity_review" for item in items),
        "source_recoverable_nodes": recoverable,
        "lineage_blocked_nodes": lineage_blocked,
        "reingest_page_count": len(reingest_pages),
    }
    if details:
        result["nodes"] = items
        result["reingest_pages"] = [
            {"page": page, "raw_inputs": sorted(raw_inputs)}
            for page, raw_inputs in sorted(reingest_pages.items())
        ]
    return result


def execute_targeted_orphans(conn, node_paths, apply=False):
    if not apply:
        return targeted_orphan_nodes(conn, node_paths, False)
    conn.execute("BEGIN")
    try:
        result = targeted_orphan_nodes(conn, node_paths, True)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


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
    parser.add_argument("--orphan-node", action="append", default=[],
                        help="显式检查无边、无时态事实的 entity；可重复，配合 --apply 删除")
    parser.add_argument("--description-audit", action="store_true",
                        help="只读审计 keyword description/gloss 与可重摄入来源")
    parser.add_argument("--details", action="store_true", help="审计时输出逐节点详情")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else gl.GRAPH_DB
    if not db_path.exists():
        print(f"ERROR: graph.db 不存在: {db_path}", file=sys.stderr)
        return 2
    conn = gl.connect(str(db_path))
    try:
        if args.description_audit:
            print(json.dumps(
                semantic_description_audit(conn, details=args.details),
                ensure_ascii=False,
                indent=2,
            ))
        elif args.orphan_node:
            apply = args.apply and not args.dry_run
            result = execute_targeted_orphans(conn, args.orphan_node, apply)
            print(json.dumps({"applied": apply, "nodes": result}, ensure_ascii=False, indent=2))
        elif args.raw_links_only:
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

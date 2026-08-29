#!/usr/bin/env python3
"""graph_validate.py — graph.db 只读结构约束校验

责任边界:
- 只校验 graph.db 中可机械确认的结构与约束;
- 不写 graph.db、不改页面、不做语义判断;
- 页面 frontmatter 结构仍由 ingest_check.py 负责,本工具是图侧补充。

用法:
  graph_validate.py                     # 校验 cross-domain/graph.db
  graph_validate.py --details           # 附最多 max_details 条样例
  graph_validate.py --db private/graph.db
  graph_validate.py --config <yaml> --json

退出码:
  0 = 无 ERROR(WARN 可有)
  1 = 有 ERROR
  2 = 用法或配置/数据库不可读
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

try:
    import yaml
except ImportError:
    yaml = None

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO / "operations" / "config" / "graph-schema.yaml"

DEFAULTS = {
    "version": 1,
    "node_types": ["page", "people", "entity", "hub", "raw", "timeline-summary"],
    "entity_subtypes": [
        "keyword", "person", "proposition", "institution",
        "venue", "citation-only",
    ],
    "edge_confidence": {
        "canonical": ["可追溯", "推断", "存疑"],
        "legacy_aliases": {
            "[可追溯]": "可追溯",
        },
    },
}

CHECKS = {
    "unknown_node_type": "error",
    "unknown_entity_subtype": "error",
    "empty_predicate": "error",
    "dangling_edge_endpoint": "error",
    "unknown_edge_confidence": "warn",
    "legacy_edge_confidence": "warn",
    "duplicate_semantic_edge": "warn",
    "dangling_temporal_fact_endpoint": "error",
    "invalid_temporal_date": "error",
    "inverted_temporal_window": "error",
}


def _merged(config: dict) -> dict:
    """把 YAML 覆盖到保守默认值上，保持未知字段的向前兼容。"""
    merged = json.loads(json.dumps(DEFAULTS, ensure_ascii=False))
    if not config:
        return merged
    merged["node_types"] = config.get("node_types", merged["node_types"])
    merged["entity_subtypes"] = config.get("entity_subtypes", merged["entity_subtypes"])
    edge_conf = merged["edge_confidence"]
    edge_conf.update(config.get("edge_confidence") or {})
    return merged


def load_config(path: Path | None = None) -> dict:
    path = Path(path or DEFAULT_CONFIG)
    if not path.exists():
        print(f"ERROR: 图约束配置不存在: {path}", file=sys.stderr)
        sys.exit(2)
    if yaml is None:
        print("ERROR: 缺少 PyYAML", file=sys.stderr)
        sys.exit(2)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _merged(loaded)


def _add(findings: dict, level: str, check: str, message: str, sample: dict | None = None):
    severity = CHECKS.get(check, "warn")
    if level == "error" and severity != "error":
        raise ValueError(f"check {check} is not configured as error")
    bucket = findings["errors"] if level == "error" else findings["warnings"]
    entry = {"check": check, "message": message}
    if sample:
        sample = dict(sample)
        entry["sample"] = {str(k): v for k, v in sample.items()}
        entry["sample"] = {k: v for k, v in entry["sample"].items() if v is not None}
    bucket.append(entry)


def validate_graph(conn, config: dict) -> dict:
    """执行全部图侧检查，返回 {'errors': [...], 'warnings': [...], 'counts': {...}}"""
    findings = {"errors": [], "warnings": []}
    node_types = set(config["node_types"])
    entity_subtypes = set(config["entity_subtypes"])
    canonical_confidence = set(config["edge_confidence"]["canonical"])
    legacy_aliases = config["edge_confidence"].get("legacy_aliases", {})

    type_counts = {key: 0 for key in CHECKS}
    sample_limit = 12

    for row in conn.execute("SELECT path, type, entity_subtype FROM nodes"):
        node_type = row["type"]
        subtype = row["entity_subtype"]
        if not node_type or node_type not in node_types:
            type_counts["unknown_node_type"] += 1
            if len(findings["errors"]) < sample_limit:
                _add(findings, "error", "unknown_node_type", f"未知节点类型: {node_type}", row)
        if node_type == "entity":
            if subtype and subtype not in entity_subtypes:
                type_counts["unknown_entity_subtype"] += 1
                if len(findings["errors"]) < sample_limit:
                    _add(findings, "error", "unknown_entity_subtype", f"未知 entity_subtype: {subtype}", row)

    for row in conn.execute("SELECT id, subject, predicate, object, confidence FROM edges"):
        predicate = (row["predicate"] or "").strip()
        if not predicate:
            type_counts["empty_predicate"] += 1
            if len(findings["errors"]) < sample_limit:
                _add(findings, "error", "empty_predicate", f"空谓词边 id={row['id']}", row)
        conf = row["confidence"]
        if conf and conf not in canonical_confidence:
            if conf in legacy_aliases:
                type_counts["legacy_edge_confidence"] += 1
                if len(findings["warnings"]) < sample_limit:
                    sample = dict(row)
                    sample["normalized_confidence"] = legacy_aliases[conf]
                    _add(findings, "warn", "legacy_edge_confidence", f"旧置信度标记: {conf}", sample)
            else:
                type_counts["unknown_edge_confidence"] += 1
                if len(findings["warnings"]) < sample_limit:
                    _add(findings, "warn", "unknown_edge_confidence", f"未知置信度: {conf!r}", row)

    # 孤儿边。当前建表有外键，这里是防御性检查；真实库不应出现。
    for column in ("subject", "object"):
        count = conn.execute(
            f"SELECT COUNT(*) FROM edges e LEFT JOIN nodes n ON e.{column}=n.path "
            "WHERE n.path IS NULL"
        ).fetchone()[0]
        if count:
            type_counts["dangling_edge_endpoint"] += count
            _add(findings, "error", "dangling_edge_endpoint", f"{column} 侧存在 {count} 条孤儿边")

    for row in conn.execute(
        "SELECT id, subject, object, valid_from, valid_until FROM temporal_facts"
    ):
        for column in ("subject", "object"):
            if not conn.execute(
                "SELECT 1 FROM nodes WHERE path=?", (row[column],)
            ).fetchone():
                type_counts["dangling_temporal_fact_endpoint"] += 1
                if len(findings["errors"]) < sample_limit:
                    _add(findings, "error", "dangling_temporal_fact_endpoint",
                         f"temporal_fact id={row['id']} {column}={row[column]} 无对应节点", row)
        valid_from = row["valid_from"]
        valid_until = row["valid_until"]
        parsed_from = None
        parsed_until = None
        if valid_from:
            try:
                parsed_from = date.fromisoformat(valid_from)
            except ValueError:
                type_counts["invalid_temporal_date"] += 1
                if len(findings["errors"]) < sample_limit:
                    _add(findings, "error", "invalid_temporal_date",
                         f"temporal_fact id={row['id']} valid_from={valid_from}", row)
        if valid_until:
            try:
                parsed_until = date.fromisoformat(valid_until)
            except ValueError:
                type_counts["invalid_temporal_date"] += 1
                if len(findings["errors"]) < sample_limit:
                    _add(findings, "error", "invalid_temporal_date",
                         f"temporal_fact id={row['id']} valid_until={valid_until}", row)
        if parsed_from and parsed_until and parsed_from > parsed_until:
            type_counts["inverted_temporal_window"] += 1
            if len(findings["errors"]) < sample_limit:
                _add(findings, "error", "inverted_temporal_window",
                     f"temporal_fact id={row['id']} valid_from={valid_from} > valid_until={valid_until}", row)

    duplicate_rows = conn.execute(
        "SELECT subject, predicate, object, COALESCE(confidence,'') AS confidence, COUNT(*) AS n "
        "FROM edges GROUP BY subject, predicate, object, COALESCE(confidence,'') HAVING n > 1"
    ).fetchall()
    if duplicate_rows:
        type_counts["duplicate_semantic_edge"] += len(duplicate_rows)
        for row in duplicate_rows[:sample_limit]:
            _add(findings, "warn", "duplicate_semantic_edge", f"{row['subject']} → {row['predicate']} → {row['object']} 重复 {row['n']} 条", dict(row))

    return {"errors": findings["errors"], "warnings": findings["warnings"], "counts": type_counts}


def format_report(report: dict, details: bool, json_mode: bool) -> str:
    if json_mode:
        return json.dumps(report, ensure_ascii=False, indent=2)
    error_checks = {k for k, v in CHECKS.items() if v == "error"}
    warn_checks = {k for k, v in CHECKS.items() if v == "warn"}
    error_count = sum(report["counts"].get(k, 0) for k in error_checks)
    warn_count = sum(report["counts"].get(k, 0) for k in warn_checks)
    lines = []
    lines.append(f"图校验: {error_count} ERROR, {warn_count} WARN")
    checks = {k: v for k, v in report["counts"].items() if v}
    if checks:
        lines.append("检查命中: " + ", ".join(f"{k}={v}" for k, v in sorted(checks.items())))
    if details:
        for level in ("errors", "warnings"):
            if not report[level]:
                continue
            lines.append(f"-- {level.upper()} 样例 --")
            shown = report[level]
            if not report.get("details_all") and len(shown) > 12:
                shown = shown[:12]
                lines.append("(仅显示前 12 条)")
            for item in shown:
                sample = item.get("sample", {})
                sample_text = " | ".join(f"{k}:{v}" for k, v in sample.items()) if sample else ""
                lines.append(f"- [{item['check']}] {item['message']}" + (f" | {sample_text}" if sample_text else ""))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="graph.db 路径，默认 cross-domain/graph.db")
    parser.add_argument("--config", type=Path, default=None, help="图约束配置，默认 operations/config/graph-schema.yaml")
    parser.add_argument("--details", action="store_true", help="显示样例明细")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    db_path = Path(args.db) if args.db else gl.GRAPH_DB
    if not db_path.exists():
        print(f"ERROR: graph.db 不存在: {db_path}", file=sys.stderr)
        return 2
    conn = gl.connect(str(db_path))
    try:
        report = validate_graph(conn, config)
    finally:
        conn.close()
    print(format_report(report, args.details, args.json))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""审计 graph.db 中已填写的可选 locator；不修改 raw 或 graph.db。"""
import argparse, json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
from source_locator import split_locator, resolve_path, locator_status

def main():
    ap = argparse.ArgumentParser(description="审计已填写的可选 edge locator")
    ap.add_argument("--db", default=str(gl.GRAPH_DB))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="显式深审计所有历史非空 locator；默认只统计，不判失败")
    args = ap.parse_args()
    conn = gl.connect(args.db)
    report = {"edges": 0, "without_locator": 0, "with_locator": 0,
              "missing_path": [], "missing_locator": [],
              "invalid_locator": [], "unverifiable_locator": []}
    for row in conn.execute("SELECT id,subject,predicate,object,source FROM edges ORDER BY id"):
        report["edges"] += 1
        source = (row["source"] or "").strip()
        if not source:
            report["without_locator"] += 1
            continue
        report["with_locator"] += 1
        if not args.strict:
            continue
        path, locator = split_locator(source)
        target = resolve_path(path)
        if target is None:
            report["missing_path"].append({**dict(row), "path": path}); continue
        status = locator_status(locator, target)
        if status == "missing":
            report["missing_locator"].append({**dict(row), "path": path})
        elif status == "unverifiable":
            report["unverifiable_locator"].append({**dict(row), "path": path, "locator": locator})
        elif status != "present":
            report["invalid_locator"].append({**dict(row), "path": path, "locator": locator})
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"edges={report['edges']} with_locator={report['with_locator']} "
              f"without_locator={report['without_locator']}")
        for key in ("missing_path", "missing_locator", "invalid_locator", "unverifiable_locator"):
            print(f"{key}={len(report[key])}")
        for key in ("missing_path", "missing_locator", "invalid_locator", "unverifiable_locator"):
            for item in report[key][:10]:
                print(f"  {key}: edge={item.get('id')} {item.get('predicate')} source={item.get('source') or '-'}")
    if not args.strict:
        return 0
    return 1 if report["missing_path"] or report["missing_locator"] or report["invalid_locator"] else 0

if __name__ == "__main__": raise SystemExit(main())

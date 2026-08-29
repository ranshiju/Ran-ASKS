#!/usr/bin/env python3
"""为可机械确定的 graph.db 来源补充 locator。默认只报告，--apply 才写图。"""
import argparse, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
from source_locator import split_locator, resolve_path

SAFE = {
    "作者": "authors",
    "通讯作者": "authors",
    "引用": "references",
    "参会": "participants",
}

def proposal(row):
    source = (row["source"] or "").strip()
    if not source or "#" in source or row["predicate"] not in SAFE:
        return None
    if resolve_path(source) is None:
        return None
    return f"{source}#{SAFE[row['predicate']]}"

def main():
    ap = argparse.ArgumentParser(description="迁移可机械确定的来源 locator")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=str(gl.GRAPH_DB))
    args = ap.parse_args()
    conn = gl.connect(args.db)
    rows = list(conn.execute("SELECT id,subject,predicate,object,source FROM edges"))
    proposals = [(r["id"], proposal(r)) for r in rows]
    proposals = [(i, s) for i, s in proposals if s]
    if args.apply:
        conn.executemany("UPDATE edges SET source=? WHERE id=?", ((s, i) for i, s in proposals))
        conn.commit()
    print(f"proposed={len(proposals)} applied={'yes' if args.apply else 'no'}")
    for edge_id, source in proposals[:20]:
        print(f"  edge={edge_id} -> {source}")

if __name__ == "__main__": main()

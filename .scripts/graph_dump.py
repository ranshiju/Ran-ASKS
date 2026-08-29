#!/usr/bin/env python3
"""graph_dump.py — graph.db 文本快照(替代被删的 md Core Triples 段人可读性)

用法:
  graph_dump.py              # 全图快照(nodes + aliases + edges)
  graph_dump.py --node <path>  # 单节点的所有边(出+入)+ aliases
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl


def dump_full(conn):
    n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"=== graph.db 快照 (节点 {n}, 边 {e}) ===\n")
    print("--- nodes ---")
    for r in conn.execute("SELECT path,title,type,entity_subtype,source_type,has_raw_source,status FROM nodes ORDER BY type,path"):
        raw = " [raw源]" if r["has_raw_source"] else ""
        subtype = f"/{r['entity_subtype']}" if r["entity_subtype"] else ""
        print(f"  {r['type']}{subtype:16} {r['path']}{raw}  ({r['title']})")
    print(f"\n--- aliases ---")
    rows = conn.execute("SELECT node_path, alias FROM aliases ORDER BY alias").fetchall()
    if rows:
        for r in rows:
            print(f"  {r['alias']} → {r['node_path']}")
    else:
        print("  (无)")
    print(f"\n--- edges (知识边) ---")
    for r in conn.execute(
        "SELECT e.id,e.subject,e.predicate,e.object,e.confidence,e.source,e.is_sr, "
        "GROUP_CONCAT(ev.source, ' | ') AS evidence_sources "
        "FROM edges e LEFT JOIN edge_evidence ev ON ev.edge_id=e.id "
        "GROUP BY e.id "
        "ORDER BY subject,predicate,object"
    ):
        sr = " [SR]" if r["is_sr"] else ""
        print(f"  {r['subject']} --{r['predicate']}--> {r['object']} "
              f"[{r['confidence'] or ''}]{sr}  src: {r['evidence_sources'] or r['source'] or '-'}")



def dump_node(conn, path):
    if not gl.node_exists(conn, path):
        print(f"节点不存在: {path}", file=sys.stderr)
        sys.exit(1)
    r = conn.execute("SELECT * FROM nodes WHERE path=?", (path,)).fetchone()
    print(f"=== 节点: {r['title']} ({r['path']}) ===")
    print(f"  type: {r['type']} | entity_subtype: {r['entity_subtype'] or '-'} | source_type: {r['source_type']} | status: {r['status']} | raw源: {r['has_raw_source']}")
    aliases = conn.execute("SELECT alias FROM aliases WHERE node_path=?", (path,)).fetchall()
    if aliases:
        print(f"  aliases: {', '.join(a['alias'] for a in aliases)}")
    print(f"\n--- 出边 ({path} → ?) ---")
    for e in conn.execute(
        "SELECT e.id,e.predicate,e.object,e.confidence,e.source,e.is_sr,GROUP_CONCAT(ev.source, ' | ') AS evidence_sources "
        "FROM edges e LEFT JOIN edge_evidence ev ON ev.edge_id=e.id WHERE e.subject=? GROUP BY e.id ORDER BY e.predicate",
        (path,)
    ):
        sr = " [SR]" if e["is_sr"] else ""
        print(f"  --{e['predicate']}--> {e['object']} [{e['confidence'] or ''}]{sr}  src: {e['evidence_sources'] or e['source'] or '-'}")
    print(f"\n--- 入边 (? → {path}) ---")
    for e in conn.execute(
        "SELECT e.id,e.subject,e.predicate,e.confidence,e.source,e.is_sr,GROUP_CONCAT(ev.source, ' | ') AS evidence_sources "
        "FROM edges e LEFT JOIN edge_evidence ev ON ev.edge_id=e.id WHERE e.object=? GROUP BY e.id ORDER BY e.predicate",
        (path,)
    ):
        sr = " [SR]" if e["is_sr"] else ""
        print(f"  {e['subject']} --{e['predicate']}--> [{e['confidence'] or ''}]{sr}  src: {e['evidence_sources'] or e['source'] or '-'}")


def dump_jsonl(conn, out_path):
    """输出 jsonl 审计快照(进 git,供 diff 审查)。
    非主数据——主数据是 graph.db,jsonl 是可读的派生快照。"""
    import json
    lines = []
    # nodes
    for r in conn.execute("SELECT path,title,type,source_type,date,status,has_raw_source FROM nodes ORDER BY path"):
        lines.append(json.dumps({"_t": "node", **dict(r)}, ensure_ascii=False))
    # aliases
    for r in conn.execute("SELECT alias,node_path FROM aliases ORDER BY alias"):
        lines.append(json.dumps({"_t": "alias", **dict(r)}, ensure_ascii=False))
    # edges
    for r in conn.execute("SELECT id,subject,predicate,object,confidence,source,is_sr FROM edges ORDER BY id"):
        lines.append(json.dumps({"_t": "edge", **dict(r)}, ensure_ascii=False))
    out = Path(out_path)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"jsonl快照: {out} ({len(lines)} 行)")


def main():
    ap = argparse.ArgumentParser(description="graph.db 文本快照")
    ap.add_argument("--node", help="查单节点")
    ap.add_argument("--jsonl", help="输出 jsonl 审计快照到指定路径(进 git)")
    args = ap.parse_args()
    conn = gl.connect()
    if args.jsonl:
        dump_jsonl(conn, args.jsonl)
    elif args.node:
        dump_node(conn, args.node)
    else:
        dump_full(conn)
    conn.close()


if __name__ == "__main__":
    main()

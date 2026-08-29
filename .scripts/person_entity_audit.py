#!/usr/bin/env python3
"""审计并标记无 people page 的人物 entity。"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

WARN = 1600
LIMIT = 2000
PERSON_PREDICATES = {"作者", "通讯作者", "参会", "指导", "师从", "受指导于", "所属", "任职于"}

def main():
    ap = argparse.ArgumentParser(description="人物 entity 数量治理审计")
    ap.add_argument("--apply", action="store_true", help="写入 entity_subtype=person 标记")
    ap.add_argument("--db", default=str(gl.GRAPH_DB))
    args = ap.parse_args()
    conn = gl.connect(args.db)
    candidates = set()
    for row in conn.execute("SELECT subject, object, predicate FROM edges"):
        if row[2] not in PERSON_PREDICATES:
            continue
        for path in (row[0], row[1]):
            node = conn.execute("SELECT type FROM nodes WHERE path=?", (path,)).fetchone()
            if node and node[0] == "entity":
                candidates.add(path)
    if args.apply:
        conn.executemany("UPDATE nodes SET entity_subtype='person' WHERE path=?", ((p,) for p in candidates))
        conn.commit()
    count = len(candidates)
    level = "OK" if count < WARN else ("WARN" if count < LIMIT else "LIMIT")
    print(f"person-entity: {count} | level={level} | warn={WARN} | limit={LIMIT}")
    print(f"marked={'yes' if args.apply else 'no'}")
    if count >= LIMIT:
        print("低优先级人物 entity 应进入 people-pending 队列；共同作者和明确高价值关系不阻断。")
    elif count >= WARN:
        print("接近治理线，建议合并别名、清理孤立节点并处理 pending 队列。")

if __name__ == "__main__":
    main()

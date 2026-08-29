#!/usr/bin/env python3
"""graph_metrics.py — 图论诊断(连通性/孤岛/紧密簇/谓词报告)

图论诊断:发现知识缺口与隐含结构。开发期即时提示(图重建后),用户指令修补。
确定性算法(连通分量,无 Leiden 社区——Leiden 留远期)。

子命令:
  connectivity        图连通性统计:从 Raw 文档包 BFS；只把有 sources 却不可达的 Wiki 视为结构缺口
  tight_clusters      紧密簇检测:无 Hub 覆盖的连通分量(>=3 节点),提示建 Hub
  predicates          谓词报告:GROUP BY predicate,发现低频/可合并谓词
  all                 全部检查

紧密簇状态:存 cross-domain/.graph-state.json,只提示本次新增(不重复刷屏)。
形态 B(suggested questions 的知识库版):紧密簇→Hub 触发器,L3 领域地图触发器。

用法:
  graph_metrics.py connectivity
  graph_metrics.py tight_clusters [--min-size 3] [--apply-state]
  graph_metrics.py predicates
  graph_metrics.py all [--apply-state]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GRAPH_DB = REPO / "cross-domain" / "graph.db"
STATE_FILE = REPO / "cross-domain" / ".graph-state.json"


def connect():
    if not GRAPH_DB.exists():
        print(f"错误: 图数据库不存在 {GRAPH_DB}", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(GRAPH_DB))
    conn.row_factory = sqlite3.Row
    return conn


def all_nodes(conn):
    return [r["path"] for r in conn.execute("SELECT path FROM nodes")]


def neighbors(conn, node):
    """节点的所有邻居:语义边正反向 BFS(纯 subject/object 双向)。
    v5 文件节点模型:Wiki 通过 ``来源`` 边直连 Raw 文档包，其他节点主要连 Wiki；
    连通性只沿实际 subject/object，不再用 has_raw_source 模拟隐式来源边。"""
    nbs = set()
    for r in conn.execute(
        "SELECT object, subject FROM edges WHERE subject=? OR object=?",
        (node, node)
    ):
        nbs.add(r["object"])
        nbs.add(r["subject"])
    return nbs


def bfs_from_seeds(conn, seeds):
    """从种子集 BFS,返回可达集。"""
    visited = set(seeds)
    frontier = set(seeds)
    while frontier:
        nxt = set()
        for node in frontier:
            for nb in neighbors(conn, node):
                if nb not in visited:
                    visited.add(nb)
                    nxt.add(nb)
        frontier = nxt
    return visited


def check_connectivity(conn):
    """从 Raw 文档包统计可达性；无 Raw 的纯导航/实体节点允许存在。"""
    all_n = set(all_nodes(conn))
    seeds = {r["path"] for r in conn.execute(
        "SELECT path FROM nodes WHERE type='raw'")}
    if not seeds:
        return {"error": "无 Raw 文档包节点"}
    reachable = bfs_from_seeds(conn, seeds)
    orphans = all_n - reachable
    rows = conn.execute(
        "SELECT path,type,has_raw_source FROM nodes WHERE path IN ({})".format(
            ",".join("?" for _ in orphans)
        ), tuple(orphans)
    ).fetchall() if orphans else []
    def wiki_file_exists(path):
        return (REPO / f"{path}.md").is_file()

    broken_source_pages = [
        r["path"] for r in rows if r["has_raw_source"] and wiki_file_exists(r["path"])
    ]
    stale_page_nodes = [
        r["path"] for r in rows
        if r["has_raw_source"] and not wiki_file_exists(r["path"])
    ]
    informational_nodes = [r["path"] for r in rows if not r["has_raw_source"]]
    return {
        "total_nodes": len(all_n),
        "raw_seeds": len(seeds),
        "reachable": len(reachable),
        "orphans_total": len(orphans),
        "broken_source_pages": broken_source_pages[:30],
        "stale_page_nodes": stale_page_nodes[:30],
        "informational_unlinked": informational_nodes[:30],
        "connected_ratio": round(len(reachable) / len(all_n), 3) if all_n else 0,
    }


def connected_components(conn):
    """求连通分量(确定性)。返回 [{nodes: [...], size: N}, ...]。"""
    all_n = set(all_nodes(conn))
    visited = set()
    components = []
    for start in all_n:
        if start in visited:
            continue
        comp = bfs_from_seeds(conn, {start})
        visited |= comp
        components.append(comp)
    return components


def find_tight_clusters(conn, min_size=3):
    """紧密簇:无 Hub 覆盖的连通分量(>=min_size 节点)。
    提示建 Hub(L3 领域地图触发器)。形态 B:suggested questions 的知识库版。
    """
    comps = connected_components(conn)
    hubs = {r["path"] for r in conn.execute(
        "SELECT path FROM nodes WHERE type='hub'")}
    clusters = []
    for comp in comps:
        if len(comp) < min_size:
            continue
        # 该簇是否有 Hub 覆盖(簇内含 Hub 节点)
        has_hub = bool(comp & hubs)
        if has_hub:
            continue
        # 区分簇内页面 vs 实体
        pages_in = [n for n in comp if "/" in n and not n.startswith("cross-domain/topics")]
        clusters.append({
            "nodes": sorted(comp),
            "size": len(comp),
            "page_count": len(pages_in),
        })
    clusters.sort(key=lambda c: c["size"], reverse=True)
    return clusters


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"tight_clusters": []}
    return {"tight_clusters": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_clusters(current, prev):
    """本次新增的紧密簇(按节点集指纹比对)。"""
    prev_fps = {frozenset(c["nodes"]) for c in prev}
    new_clusters = []
    for c in current:
        if frozenset(c["nodes"]) not in prev_fps:
            new_clusters.append(c)
    return new_clusters


def predicate_report(conn):
    """谓词报告:GROUP BY predicate,发现低频/可合并谓词。"""
    rows = conn.execute(
        "SELECT predicate, COUNT(*) cnt FROM edges GROUP BY predicate ORDER BY cnt DESC"
    ).fetchall()
    predicates = [{"predicate": r["predicate"], "count": r["cnt"]} for r in rows]
    # 低频谓词(count=1)可能待归一化或合并
    low_freq = [p for p in predicates if p["count"] == 1]
    return {"total_predicates": len(predicates), "predicates": predicates,
            "low_freq_count": len(low_freq), "low_freq": low_freq[:20]}


def check_unsplit_phrases(conn):
    """扫描含主谓宾结构但没有「包含」边的 entity 节点(ADR-003: 拆分已统一为包含)（历史存量检查）。
    判据：is_descriptive_phrase 命中（长度>20且含触发词或含标点），
    且该节点没有 predicate='包含' 的出边。"""
    import graph_ingest
    from graph_ingest import is_descriptive_phrase
    rows = conn.execute("SELECT path FROM nodes WHERE type='entity'").fetchall()
    flagged = []
    for (path,) in rows:
        if not is_descriptive_phrase(path):
            continue
        has_split = conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate='包含' LIMIT 1", (path,)
        ).fetchone()
        if not has_split:
            edge_count = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE subject=? OR object=?", (path, path)
            ).fetchone()[0]
            flagged.append({"path": path, "edge_count": edge_count})
    return {"total_entities": len(rows), "flagged_count": len(flagged), "flagged": flagged}


def fmt_unsplit_phrases(r):
    lines = [f"主谓宾结构未拆分检查(查包含边)({r['flagged_count']}/{r['total_entities']} 个 entity 需拆分):"]
    for f in r["flagged"][:20]:
        lines.append(f"  [{f['edge_count']}边] {f['path']}")
    if r["flagged_count"] > 20:
        lines.append(f"  ... 共 {r['flagged_count']} 个")
    if r["flagged_count"]:
        lines.append("\n提示:这些节点含主谓宾结构但未拆分。摄入时 3.3b/3.6b 应已处理，")
        lines.append("此处是历史存量；建议 LINT 时人工确认后补包含边。")
    return "\n".join(lines)




def fmt_connectivity(r):
    if "error" in r:
        return f"连通性: {r['error']}"
    lines = [f"图连通性校验:",
             f"  总节点: {r['total_nodes']} | Raw 文档包种子: {r['raw_seeds']} | 可达: {r['reachable']} ({r['connected_ratio']*100:.1f}%)",
             f"  未连 Raw 文档包: {r['orphans_total']}（无 sources 的导航/实体节点允许存在）"]
    if r["broken_source_pages"]:
        lines.append(f"    结构缺口:有 sources 但不可达的 Wiki({len(r['broken_source_pages'])} 个):")
        for p in r["broken_source_pages"][:10]:
            lines.append(f"      - {p}")
    else:
        lines.append("    结构缺口: 0")
    if r["stale_page_nodes"]:
        lines.append(f"    历史无文件节点(信息项): {len(r['stale_page_nodes'])}")
    if r["informational_unlinked"]:
        lines.append(f"    信息项示例({len(r['informational_unlinked'])} 个):")
        for node in r["informational_unlinked"][:10]:
            lines.append(f"      - {node}")
    return "\n".join(lines)


def fmt_clusters(r, new_only=True):
    clusters = r["new_clusters"] if new_only else r["clusters"]
    label = "新增" if new_only else "全部"
    lines = [f"紧密簇检测({label} {len(clusters)} 个,无 Hub 覆盖的连通分量 >= {r['min_size']}):"]
    if new_only and r.get("new_count", 0) == 0:
        lines.append("  (本次无新增紧密簇)")
    for c in clusters[:15]:
        lines.append(f"  簇(size={c['size']}, 页面 {c['page_count']}): {', '.join(c['nodes'][:8])}")
        if c['size'] > 8:
            lines.append(f"    ... 共 {c['size']} 节点")
    if new_only:
        lines.append(f"\n提示:新增紧密簇={r['new_count']}。建议建 Hub 覆盖(用户指令修补,不自动建)。")
    return "\n".join(lines)


def fmt_predicates(r):
    lines = [f"谓词报告(共 {r['total_predicates']} 种):"]
    for p in r["predicates"][:15]:
        lines.append(f"  {p['count']:3d}  {p['predicate']}")
    if len(r["predicates"]) > 15:
        lines.append(f"  ... 共 {len(r['predicates'])} 种")
    if r["low_freq_count"]:
        lines.append(f"\n低频谓词(count=1,可能待归一化/合并): {r['low_freq_count']} 个")
        for p in r["low_freq"][:10]:
            lines.append(f"  - {p['predicate']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="图论诊断")
    ap.add_argument("cmd", choices=["connectivity", "tight_clusters", "predicates", "unsplit_phrases", "all"])
    ap.add_argument("--min-size", type=int, default=3, help="紧密簇最小节点数")
    ap.add_argument("--apply-state", action="store_true", help="更新紧密簇状态文件")
    ap.add_argument("--all-clusters", action="store_true", help="显示全部紧密簇(非仅新增)")
    args = ap.parse_args()

    conn = connect()
    out = []
    if args.cmd in ("connectivity", "all"):
        out.append(fmt_connectivity(check_connectivity(conn)))
        out.append("")
    if args.cmd in ("tight_clusters", "all"):
        clusters = find_tight_clusters(conn, args.min_size)
        prev = load_state().get("tight_clusters", [])
        new_clusters = diff_clusters(clusters, prev)
        if args.all_clusters:
            out.append(fmt_clusters({"min_size": args.min_size, "clusters": clusters,
                                     "new_clusters": clusters, "new_count": len(clusters)}, new_only=False))
        else:
            out.append(fmt_clusters({"min_size": args.min_size, "clusters": clusters,
                                     "new_clusters": new_clusters, "new_count": len(new_clusters)}, new_only=True))
        if args.apply_state:
            save_state({"tight_clusters": clusters})
            out.append("(已更新 .graph-state.json)")
        out.append("")
    if args.cmd in ("predicates", "all"):
        out.append(fmt_predicates(predicate_report(conn)))
        out.append("")
    if args.cmd in ("unsplit_phrases", "all"):
        out.append(fmt_unsplit_phrases(check_unsplit_phrases(conn)))
    conn.close()
    print("\n".join(out))


if __name__ == "__main__":
    main()

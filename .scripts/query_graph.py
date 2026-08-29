#!/usr/bin/env python3
"""query_graph.py — 图查询脚本接口(LLM 通过 exec 调用,非 MCP)

边语义:关联链非因果链。BFS=关联召回(查询时发现某实体相关的页/关系),
非 debug 爆炸半径。查询结果按 confidence 确定性排序(top-k 仅预算压力下)。

核心契约:图边是导航关系非答案。edge source/locator 可指向 Wiki section 或 Raw，
也可为空；通常先读取相邻 Wiki section 及其 raw_citations，再沿精确 Raw locator 核验。
图只回答"有关联什么"，事实答案最终必回 Raw。

子命令:
  node <path>                      节点详情(属性 + 来源)
  neighbors <node> [--depth N]      关联召回 BFS(默认 2 跳,按 confidence 排序)
  relations <node> [--predicate P]  某节点的关系边(可按谓词过滤)
  hub_of <page>                     查某页属于哪个 Hub(沿语义边反向)
  search <keyword> [--granularity G]  按关键词查节点(title/keywords/aliases)
                                    granularity: keyword=导航聚合, proposition=精确推理
  path_exists <from> <to>           两节点是否连通(辅助验证)
  temporal --at DATE [--subject S] [--object O] [--predicate P]
                                    查某时点的时态事实(不与普通 edges 混用)

选项:
  --json          输出 JSON(默认人类可读文本)
  --top-k N       限制返回边数(默认不限;预算压力下用)
  --similar-topk N 每节点相似边上限(动态K;0=排除第一轮,-1=全部,默认5)
  --include-hub   neighbors 含 Hub 节点(默认排除,省 token;显式查才返回)
  --granularity   search 按颗粒度过滤(keyword/proposition;对应查询意图)
  --db PATH       图数据库路径(默认 cross-domain/graph.db)

用法示例:
  query_graph.py neighbors "张明远" --depth 2
  query_graph.py hub_of "academic/wiki/papers/sarthi-2024-raptor"
  query_graph.py search "MPS"
"""
import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

GRAPH_DB = Path(__file__).resolve().parent.parent / "cross-domain" / "graph.db"

# confidence 排序权重(确定性,不存边属性):可追溯 > 推断 > 存疑
CONF_ORDER = {"可追溯": 0, "推断": 1, "存疑": 2, None: 3}
# status 排序:current 优先于 deprecated
STATUS_ORDER = {"current": 0, "active": 0, "deprecated": 1, "dormant": 2, "archived": 3}


def connect(db_path):
    if not Path(db_path).exists():
        print(f"错误: 图数据库不存在 {db_path}。先运行 graph_build.py --build --apply", file=sys.stderr)
        sys.exit(1)
    import graph_lib as gl
    return gl.connect(str(db_path))


def node_info(conn, path):
    """节点详情。aliases 从独立 aliases 表读(主数据化 v4)。"""
    row = conn.execute("SELECT * FROM nodes WHERE path=?", (path,)).fetchone()
    if not row:
        return None
    aliases = [r["alias"] for r in conn.execute(
        "SELECT alias FROM aliases WHERE node_path=?", (path,))]
    return {
        "path": row["path"],
        "title": row["title"],
        "type": row["type"],
        "source_type": row["source_type"],
        "date": row["date"],
        "status": row["status"],
        "aliases": aliases,
        "has_raw_source": bool(row["has_raw_source"]),
    }


# ADR-003: 相似边动态截断——依据当前节点的相似边 score 分布计算阈值
# 保留与 top score 差距 ≤ SIMILAR_SCORE_MARGIN 的边(分数接近=同档相似),
# 再按 similar_topk 上限裁剪。K 由数据涌现,非定死。
SIMILAR_SCORE_MARGIN = 0.03


def edge_record(row):
    """Expose the legacy ``source`` column as an optional edge locator."""
    edge = dict(row)
    edge["locator"] = str(edge.get("source") or "")
    return edge


def _filter_similar_edges(similar_rows, max_cap=5):
    """动态过滤相似边。max_cap 语义(多轮渐进式):
      max_cap=0   → 完全排除相似边(第一轮:纯知识边优先)
      max_cap=-1  → 不过滤(保留全部)
      max_cap=N>0 → 动态K:保留 score ≥ (top - margin) 的边,上限 N,下限 1

    K 由当前节点的相似边 score 分布决定(涌现):
      - 有明显高分赢家 → 只保留接近 top 的少数(如 iPEPS 7条保留1)
      - score 聚集平坦 → 保留更多(上限 max_cap)
    """
    if not similar_rows:
        return []
    if max_cap == 0:      # 第一轮:完全排除
        return []
    if max_cap < 0:       # -1:不过滤
        return similar_rows
    similar_rows.sort(key=lambda e: e.get("score") or 0.0, reverse=True)
    top_score = similar_rows[0].get("score") or 0.0
    cutoff = top_score - SIMILAR_SCORE_MARGIN
    kept = [e for e in similar_rows if (e.get("score") or 0.0) >= cutoff]
    kept = kept[:max_cap]
    return kept if kept else similar_rows[:1]


def neighbors_bfs(conn, start, depth=2, top_k=None, include_hub=False, similar_topk=5):
    """关联召回 BFS。返回 (节点集, 边集)。
    BFS 沿知识边正反向遍历；Hub 节点默认按 type 过滤。
    ADR-003: 相似边优先级最低(知识边先扩散);每节点相似边由 _filter_similar_edges
    动态截断(按 score 分布算阈值,非定死 K),防止过度扩散。相似边全量存图,仅导航时过滤。
    """
    if not node_exists(conn, start):
        return {"error": f"节点不存在: {start}"}
    visited = {start}
    frontier = {start}
    edges = []
    for d in range(depth):
        next_frontier = set()
        for node in frontier:
            # ADR-003: 知识边(非相似)优先全取;相似边每节点仅取 top-K(按 score 降序)
            knowledge_rows = []
            similar_rows = []
            for r in conn.execute(
                "SELECT * FROM edges WHERE subject=? OR object=?", (node, node)
            ):
                if r["predicate"] == "相似":
                    similar_rows.append(edge_record(r))
                else:
                    knowledge_rows.append(edge_record(r))
            # ADR-003: 相似边动态截断(按 score 分布算阈值,非固定 K)
            similar_rows = _filter_similar_edges(similar_rows, similar_topk)
            for r in knowledge_rows + similar_rows:
                if not include_hub:
                    other = r["object"] if r["subject"] == node else r["subject"]
                    other_type = conn.execute(
                        "SELECT type FROM nodes WHERE path=?", (other,)
                    ).fetchone()
                    if other_type and other_type[0] == "hub":
                        continue
                edges.append(r)
                other = r["object"] if r["subject"] == node else r["subject"]
                if other not in visited:
                    next_frontier.add(other)
        visited |= next_frontier
        frontier = next_frontier
    # 边去重 + confidence/status 排序(相似边排最后)
    seen = set()
    unique_edges = []
    for e in edges:
        key = (e["subject"], e["predicate"], e["object"])
        if key not in seen:
            seen.add(key)
            unique_edges.append(e)
    unique_edges.sort(key=lambda e: (
        1 if e["predicate"] == "相似" else 0,  # 相似边排最后
        CONF_ORDER.get(e["confidence"], 3),
        STATUS_ORDER.get(get_node_status(conn, e["object"]), 9),
    ))
    if top_k:
        unique_edges = unique_edges[:top_k]
    # 节点信息(去掉起点)
    nodes = []
    for n in visited - {start}:
        ni = node_info(conn, n)
        if ni:
            nodes.append(ni)
    return {"start": start, "depth": depth, "nodes": nodes, "edges": unique_edges}


def relations(conn, node, predicate=None, top_k=None):
    """某节点的所有关系边(可按谓词过滤)。"""
    if predicate:
        rows = conn.execute(
            "SELECT * FROM edges WHERE (subject=? OR object=?) AND predicate=? "
            "ORDER BY confidence",
            (node, node, predicate)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM edges WHERE (subject=? OR object=?) "
            "ORDER BY confidence",
            (node, node)
        ).fetchall()
    edges = [edge_record(r) for r in rows]
    edges.sort(key=lambda e: CONF_ORDER.get(e["confidence"], 3))
    if top_k:
        edges = edges[:top_k]
    return {"node": node, "predicate_filter": predicate, "edges": edges}


def temporal_at(conn, at_date, subject=None, obj=None, predicate=None, top_k=None):
    """查某时点 effective 的 temporal_facts。

    temporal_facts 与普通 edges 分表保存；默认不会被 neighbors/search 混入。
    effective 判定:
      - (valid_from IS NULL OR valid_from <= at)
      - (valid_until IS NULL OR valid_until >= at)
      - 未被后续事实取代：superseded_by IS NULL 或后续 valid_from > at
    """
    try:
        parsed = date.fromisoformat(at_date)
        at_value = parsed.isoformat()
    except ValueError:
        return {"error": f"无效日期: {at_date}，应使用 YYYY-MM-DD"}
    query = (
        "SELECT tf.* FROM temporal_facts tf "
        "LEFT JOIN temporal_facts succ ON succ.id = tf.superseded_by "
        "WHERE (tf.valid_from IS NULL OR tf.valid_from <= ?) "
        "AND (tf.valid_until IS NULL OR tf.valid_until >= ?) "
        "AND (tf.superseded_by IS NULL OR succ.valid_from IS NULL OR succ.valid_from > ?) "
    )
    params = [at_value, at_value, at_value]
    if subject:
        query += "AND tf.subject = ? "
        params.append(subject)
    if obj:
        query += "AND tf.object = ? "
        params.append(obj)
    if predicate:
        query += "AND tf.predicate = ? "
        params.append(predicate)
    query += "ORDER BY tf.valid_from IS NOT NULL, tf.valid_from, tf.id"
    if top_k:
        query += " LIMIT ?"
        params.append(top_k)
    rows = conn.execute(query, params).fetchall()
    return {
        "at": at_value,
        "subject": subject,
        "object": obj,
        "predicate": predicate,
        "count": len(rows),
        "facts": [dict(r) for r in rows],
    }


def hub_of(conn, page):
    """查某页所属 Hub(沿语义导航边反向查 type=hub 节点)。"""
    rows = conn.execute(
        "SELECT CASE WHEN e.subject=? THEN e.object ELSE e.subject END AS hub_path, "
        "e.predicate FROM edges e "
        "JOIN nodes n ON n.path=CASE WHEN e.subject=? THEN e.object ELSE e.subject END "
        "WHERE (e.subject=? OR e.object=?) AND n.type='hub'",
        (page, page, page, page)
    ).fetchall()
    hubs = []
    for r in rows:
        ni = node_info(conn, r["hub_path"])
        if ni:
            ni["predicate"] = r["predicate"]
            hubs.append(ni)
    return {"page": page, "hubs": hubs}


def search_nodes(conn, keyword, granularity=None):
    """按关键词查节点(title/aliases/path 匹配,主数据化 v4:aliases 独立表)。

    granularity: 按颗粒度过滤(对应查询意图)。
      - keyword  : 只返回概念节点(导航聚合类查询:哪些文献涉及某主题)
      - proposition : 只返回论断节点(精确推理类查询:谁验证/反对某 claim)
      - None : 不过滤(默认,返回所有颗粒度)
    """
    kw = f"%{keyword}%"
    query = (
        "SELECT DISTINCT n.path, n.title, n.type, n.status, n.entity_subtype "
        "FROM nodes n "
        "LEFT JOIN aliases a ON a.node_path = n.path "
        "WHERE (n.title LIKE ? OR n.path LIKE ? OR a.alias LIKE ?) "
    )
    params = [kw, kw, kw]
    if granularity:
        query += "AND n.entity_subtype = ? "
        params.append(granularity)
    query += "LIMIT 50"
    rows = conn.execute(query, params).fetchall()
    results = []
    for r in rows:
        results.append({
            "path": r["path"], "title": r["title"], "type": r["type"],
            "status": r["status"], "granularity": r["entity_subtype"] or "unspecified",
        })
    return {"keyword": keyword, "granularity": granularity, "count": len(results), "nodes": results}


def path_exists(conn, frm, to):
    """两节点是否连通(BFS)。辅助验证。"""
    if not (node_exists(conn, frm) and node_exists(conn, to)):
        return {"connected": False, "reason": "节点不存在"}
    if frm == to:
        return {"connected": True, "path": [frm]}
    visited = {frm}
    frontier = {frm}
    while frontier:
        nxt = set()
        for node in frontier:
            for r in conn.execute(
                "SELECT object FROM edges WHERE subject=? "
                "UNION SELECT subject FROM edges WHERE object=?", (node, node)
            ):
                o = r["object"]
                if o == to:
                    return {"connected": True, "from": frm, "to": to}
                if o not in visited:
                    visited.add(o)
                    nxt.add(o)
        frontier = nxt
    return {"connected": False, "from": frm, "to": to}


def node_exists(conn, path):
    return conn.execute("SELECT 1 FROM nodes WHERE path=?", (path,)).fetchone() is not None


def get_node_status(conn, path):
    r = conn.execute("SELECT status FROM nodes WHERE path=?", (path,)).fetchone()
    return r["status"] if r else "unknown"


def fmt_text(result, cmd):
    """人类可读文本输出。"""
    if "error" in result:
        return f"错误: {result['error']}"
    if cmd == "node":
        n = result
        lines = [f"节点: {n['title']} ({n['type']})", f"  path: {n['path']}",
                 f"  status: {n['status']} | source_type: {n['source_type']} | date: {n['date']}",
                 f"  has_raw_source: {n['has_raw_source']}"]
        if n.get("aliases"): lines.append(f"  aliases: {', '.join(n['aliases'])}")
        return "\n".join(lines)
    if cmd == "neighbors":
        lines = [f"关联召回(BFS depth={result['depth']}) 起点: {result['start']}",
                 f"命中节点 {len(result['nodes'])} 个,边 {len(result['edges'])} 条:"]
        for e in result["edges"][:20]:
            sr = " [SR]" if e["is_sr"] else ""
            cf = e["confidence"] or "-"
            lines.append(f"  {e['subject']} --{e['predicate']}--> {e['object']} "
                         f"[{cf}]{sr} (来源: {e['source'] or '-'})")
        return "\n".join(lines)
    if cmd == "relations":
        lines = [f"节点 {result['node']} 的关系边({len(result['edges'])} 条):"]
        for e in result["edges"]:
            sr = " [SR]" if e["is_sr"] else ""
            lines.append(f"  {e['subject']} --{e['predicate']}--> {e['object']} "
                         f"[{e['confidence'] or '-'}]{sr} (来源: {e['source']})")
        return "\n".join(lines)
    if cmd == "hub_of":
        if not result["hubs"]:
            return f"页面 {result['page']} 不属于任何 Hub"
        lines = [f"页面 {result['page']} 属于 {len(result['hubs'])} 个 Hub:"]
        for h in result["hubs"]:
            lines.append(
                f"  - {h['title']} ({h['predicate']}; {h['path']}, status={h['status']})"
            )
        return "\n".join(lines)
    if cmd == "search":
        lines = [f"搜索 '{result['keyword']}': 命中 {result['count']} 个节点"]
        for n in result["nodes"][:20]:
            lines.append(f"  - {n['title']} [{n['type']}] {n['path']} ({n['status']})")
        return "\n".join(lines)
    if cmd == "path_exists":
        return f"{result['from']} → {result['to']}: {'连通' if result['connected'] else '不连通'}"
    if cmd == "temporal":
        lines = [f"时态事实 at {result['at']}: {result['count']} 条"]
        for f in result["facts"]:
            sr = " [SR]" if f.get("is_sr") else ""
            lines.append(
                f"  {f['subject']} --{f['predicate']}--> {f['object']}"
                f" [{f.get('valid_from') or '-'} ~ {f.get('valid_until') or '∞'}]{sr}"
                f" (来源: {f.get('source') or '-'})"
            )
        return "\n".join(lines)
    return json.dumps(result, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="图查询接口")
    ap.add_argument("cmd", choices=["node", "neighbors", "relations", "hub_of", "search", "path_exists", "temporal"])
    ap.add_argument("args", nargs="*", help="命令参数")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--predicate", default=None)
    ap.add_argument("--subject", default=None, help="temporal 命令专用：过滤 subject")
    ap.add_argument("--object", default=None, help="temporal 命令专用：过滤 object")
    ap.add_argument("--at", default=None, help="temporal 命令专用：YYYY-MM-DD")
    ap.add_argument("--top-k", type=int, default=None)
    ap.add_argument("--similar-topk", type=int, default=5,
                    help="每节点相似边上限(动态K;0=排除,-1=全部,默认5)")
    ap.add_argument("--include-hub", action="store_true")
    ap.add_argument("--granularity", default=None,
                   choices=["keyword", "proposition"],
                   help="按颗粒度过滤(search 命令专用):keyword=导航聚合,proposition=精确推理")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(GRAPH_DB))
    args = ap.parse_args()
    min_args = {"node": 1, "neighbors": 1, "relations": 1, "hub_of": 1, "search": 1, "path_exists": 2, "temporal": 0}
    if len(args.args) < min_args[args.cmd]:
        ap.error(f"{args.cmd} 需要至少 {min_args[args.cmd]} 个位置参数")

    conn = connect(args.db)
    result = None
    if args.cmd == "node":
        result = node_info(conn, args.args[0])
        if result is None:
            result = {"error": f"节点不存在: {args.args[0]}"}
    elif args.cmd == "neighbors":
        result = neighbors_bfs(conn, args.args[0], args.depth, args.top_k, args.include_hub, args.similar_topk)
    elif args.cmd == "relations":
        result = relations(conn, args.args[0], args.predicate, args.top_k)
    elif args.cmd == "hub_of":
        result = hub_of(conn, args.args[0])
    elif args.cmd == "search":
        result = search_nodes(conn, args.args[0], args.granularity)
    elif args.cmd == "path_exists":
        result = path_exists(conn, args.args[0], args.args[1])
    elif args.cmd == "temporal":
        if not args.at:
            print("错误: temporal 必须提供 --at YYYY-MM-DD", file=sys.stderr)
            return 2
        result = temporal_at(conn, args.at, args.subject, args.object, args.predicate, args.top_k)

    conn.close()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if isinstance(result, dict) and "error" in result:
            print(f"错误: {result['error']}", file=sys.stderr); sys.exit(1)
        print(fmt_text(result, args.cmd))


if __name__ == "__main__":
    sys.exit(main())

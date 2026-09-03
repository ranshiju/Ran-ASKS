#!/usr/bin/env python3
"""sync_keyword_aliases.py — 扫描 graph.db entity 节点，按归一化名称分组，
同组多节点→取边数最多的为规范名，其余写入 aliases 表（幂等）。

归一化匹配复用 graph_lib._normalize_name_for_match：
  - 提取缩写（括号内 ≥2 大写字母，或纯缩写名）
  - 去括号内容+标点+空格，得纯中英文拼接
分组规则（防误报）：
  - 缩写相同 + 去标点名相同 → 同组
  - 仅缩写相同但去标点名差异大 → 不归组（如 CNN→卷积神经网络 vs 经典卷积神经网络）

用法:
  python3 .scripts/sync_keyword_aliases.py              # dry-run 预览
  python3 .scripts/sync_keyword_aliases.py --apply       # 写入 aliases 表
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl


def _edge_count(conn, path):
    return conn.execute(
        "SELECT COUNT(*) FROM edges WHERE subject=? OR object=?", (path, path)
    ).fetchone()[0]


def find_alias_groups(conn):
    """扫描 entity 节点，按归一化名称分组。
    返回 [(canonical, [aliases]), ...]，每组 ≥2 节点才报告。"""
    nodes = conn.execute("SELECT path FROM nodes WHERE type='entity'").fetchall()
    # 按 (abbr, stripped) 二元组分组——缩写和去标点名都相同才算同组
    groups = {}
    for (path,) in nodes:
        abbr, stripped = gl._normalize_name_for_match(path)
        if not abbr and not stripped:
            continue
        # 优先用缩写做一级 key，但 stripped 必须也一致
        key = (abbr, stripped) if abbr else (None, stripped)
        groups.setdefault(key, []).append((path, _edge_count(conn, path)))
    result = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # 规范名 = 边数最多的；平手时取名字最长的
        members.sort(key=lambda x: (-x[1], -len(x[0])))
        canonical = members[0][0]
        aliases = [m[0] for m in members[1:]]
        result.append((canonical, aliases))
    # 前缀匹配:无缩写节点(如「CP分解」)是缩写节点(如「CP分解canonical polyadic decomposition(CP)」)的前缀 → 归组
    norm_list = []
    for (path,) in nodes:
        abbr, stripped = gl._normalize_name_for_match(path)
        if stripped:
            norm_list.append((path, abbr, stripped, _edge_count(conn, path)))
    grouped = {m[0] for key in groups for m in groups[key] if len(groups[key]) >= 2}
    for path_a, abbr_a, stripped_a, _ in norm_list:
        if abbr_a or path_a in grouped:
            continue
        hits = [(path_b, ec_b) for path_b, abbr_b, stripped_b, ec_b in norm_list
                if abbr_b and stripped_b.startswith(stripped_a) and len(stripped_a) < len(stripped_b)]
        if len(hits) == 1:
            result.append((hits[0][0], [path_a]))
    return result


def sync(conn, apply=False, *, commit=True):
    groups = find_alias_groups(conn)
    added = 0
    skipped = 0
    for canonical, aliases in groups:
        for alias in aliases:
            existing = conn.execute(
                "SELECT 1 FROM aliases WHERE alias=? AND node_path=?", (alias, canonical)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            if apply:
                conn.execute(
                    "INSERT INTO aliases (alias, node_path) VALUES (?, ?)",
                    (alias, canonical)
                )
            added += 1
    if apply and commit:
        conn.commit()
    return len(groups), added, skipped


def resolve_abbreviation_todo(conn, repo=None):
    """消解 abbreviation-todo.jsonl 中的裸缩写：若图里已有含该缩写括号释义的全称节点，
    则补 alias（裸缩写→全称节点），并从 todo 列表移除已消解项。

    轻量自动流程：每次摄入后由 graph_ingest 调用。零 LLM token。
    返回 (resolved_count, remaining_count)。
    """
    import json as _json
    import re as _re
    from pathlib import Path as _Path

    if repo is None:
        repo = gl.REPO
    todo_path = repo / "cross-domain" / "abbreviation-todo.jsonl"
    if not todo_path.exists():
        return 0, 0

    # 读取 todo
    entries = []
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                entries.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    if not entries:
        return 0, 0

    # 建索引：所有 entity 节点的括号内容 → [node_path]
    # 用宽正则捕获所有括号内容（含驼峰式如 TransE，不全大写也捕获）
    paren_index = {}  # {paren_content: [node_path]}
    for (path,) in conn.execute("SELECT path FROM nodes WHERE type='entity'").fetchall():
        for m in _re.finditer(r'[\(（]([^)）]+)[\)）]', path):
            content = m.group(1).strip()
            if content:
                paren_index.setdefault(content, []).append(path)

    # 逐条消解
    resolved = 0
    remaining = []
    seen_resolved = set()  # 去重：同一 bare_abbr 只处理一次
    for entry in entries:
        bare = entry.get("object", "").strip()
        if not bare or bare in seen_resolved:
            continue
        # 查括号索引
        candidates = paren_index.get(bare, [])
        # 排除裸缩写自身节点（path == bare 的情况）
        candidates = [p for p in candidates if p != bare]
        if len(candidates) == 1:
            target = candidates[0]
            # 补 alias：bare → target（幂等）
            existing = conn.execute(
                "SELECT 1 FROM aliases WHERE alias=? AND node_path=?", (bare, target)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO aliases (alias, node_path) VALUES (?, ?)", (bare, target)
                )
            seen_resolved.add(bare)
            resolved += 1
        elif len(candidates) > 1:
            # 歧义：多个全称节点含同一缩写，保留 todo
            remaining.append(entry)
        else:
            # 无匹配：全称节点尚未出现，保留 todo
            remaining.append(entry)

    conn.commit()

    # 写回未消解项
    if resolved > 0:
        todo_path.write_text(
            "\n".join(_json.dumps(e, ensure_ascii=False) for e in remaining) + ("\n" if remaining else ""),
            encoding="utf-8"
        )

    return resolved, len(remaining)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="同步 keyword 别名到 graph.db aliases 表")
    ap.add_argument("--apply", action="store_true", help="写入 aliases 表（默认 dry-run）")
    ap.add_argument("--resolve-todo", action="store_true", help="消解 abbreviation-todo.jsonl 中的裸缩写")
    args = ap.parse_args()
    conn = gl.connect()

    if args.resolve_todo:
        resolved, remaining = resolve_abbreviation_todo(conn)
        print(f"消解 {resolved} 个裸缩写，剩余 {remaining} 个待补全。")
        conn.close()
        return
    groups = find_alias_groups(conn)
    if not groups:
        print("无别名碎片（所有 entity 节点归一化后均唯一）。")
        return
    print(f"发现 {len(groups)} 组别名碎片：\n")
    for canonical, aliases in groups:
        ec = _edge_count(conn, canonical)
        print(f"  规范名 [{ec}边]: {canonical}")
        for a in aliases:
            ac = _edge_count(conn, a)
            print(f"    别名 [{ac}边]: {a}")
        print()
    found, added, skipped = sync(conn, apply=args.apply)
    if args.apply:
        print(f"已写入 {added} 条别名（跳过已存在 {skipped} 条）。")
    else:
        print(f"待写入 {added} 条别名（已存在 {skipped} 条）。加 --apply 执行写入。")
    conn.close()


if __name__ == "__main__":
    main()

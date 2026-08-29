#!/usr/bin/env python3
"""resolve_abbreviations.py — 知识库内校验命题裸缩写，识别无法消解者报 warning。

双层校验（均限本知识库，不拉外部知识）：
  1. 图层：裸缩写 → resolve_bare_name 查 alias 表 + keyword 节点；命中则已建立关联
  2. raw 层：反查命题源页 → extract_abbreviations 提取该论文 raw 关键段的缩写定义；
     命中说明 raw 里有全称，可据此建 keyword 节点（仍限知识库内）
  两层均 miss → warning（缩写在知识库内无全称可溯，需人工判断是否新建概念）

非阻断后置：摄入照常跑完，agent 用本工具 --list 看哪些缩写待处理。

用法:
  python3 .scripts/resolve_abbreviations.py --list
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_ingest as gi


def _extract_abbr_tokens(text):
    """从文本提取裸缩写候选（≥2 连续大写字母开头的串，排除括号内已释义的）。"""
    if not text:
        return []
    no_paren = re.sub(r"[（(][^)）]*[)）]", "", text)
    return re.findall(r"[A-Z]{2,}[A-Za-z0-9]*", no_paren)


def _source_page(conn, prop_path):
    """反查命题节点的源页（subject=page, predicate=命题谓词, object=prop）。"""
    r = conn.execute(
        "SELECT subject FROM edges WHERE object=? AND predicate IN (?,?,?) LIMIT 1",
        (prop_path, "局限性", "核心创新点", "未来展望"),
    ).fetchone()
    return r["subject"] if r else None


def list_pending(conn):
    """双层校验，列出无法在知识库内消解的裸缩写（warning）。"""
    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE entity_subtype='proposition'"
    ).fetchall()
    pending = {}
    raw_cache = {}
    for row in rows:
        r = dict(row)
        title = r["title"] or ""
        if not gi.is_bare_abbreviation(title):
            continue
        page = _source_page(conn, r["path"])
        if page and page not in raw_cache:
            try:
                import extract_abbreviations as ea
                pairs, _ = ea.extract_for_page(page + ".md")
                raw_cache[page] = {a: f for a, f in pairs}
            except Exception:
                raw_cache[page] = {}
        raw_defs = raw_cache.get(page, {})
        for tok in _extract_abbr_tokens(title):
            # 层1：图 resolve
            resolved, _ambig = gl.resolve_bare_name(tok, title_idx, alias_idx, suffix_idx)
            if resolved:
                sub = conn.execute(
                    "SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)
                ).fetchone()
                if sub and sub["entity_subtype"] == "keyword":
                    continue
            # 层2：raw 提取
            if tok in raw_defs:
                pending.setdefault(tok, {"raw": raw_defs[tok], "props": []})
                pending[tok]["props"].append((r["path"], title))
                continue
            # 双层 miss → warning
            pending.setdefault(tok, {"raw": None, "props": []})
            pending[tok]["props"].append((r["path"], title))
    if not pending:
        print("无需消解的裸缩写（所有命题缩写已 resolve 或无裸缩写）。")
        return
    warned = 0
    resolved_raw = 0
    for tok in sorted(pending):
        info = pending[tok]
        if info["raw"]:
            print(f"  〔raw 有定义〕{tok} = {info['raw'][0]}（{len(info['props'])} 处命题，可用 extract_abbreviations 建节点）")
            resolved_raw += 1
        else:
            print(f"  ⚠ {tok}（{len(info['props'])} 处命题，知识库内无全称可溯 → warning）")
            warned += 1
        for path, title in info["props"][:2]:
            print(f"      └ {title}")
        if len(info["props"]) > 2:
            print(f"      … 等 {len(info['props'])} 处")
        print()
    print(f"汇总：raw 可消解 {resolved_raw}，warning {warned}")


def _replace_abbr_in_title(title, abbr, kid):
    """把 title 中的裸缩写替换为 keyword id（词边界保护，不拆 GPT-2 这类复合词）。"""
    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(abbr) + r"(?![A-Za-z0-9])")
    return pattern.sub(kid, title)


def _rename_node(conn, old_path, new_path):
    """重命名节点 path 并迁移所有关联边。返回是否执行了重命名。

    edges.subject/object 对 nodes(path) 有立即 FK 且无 ON UPDATE 动作，
    不能直接先改父节点 path。改为复制节点行到 new_path → 迁移边 → 删除旧节点，
    三步在每个中间态都满足 FK。
    """
    if old_path == new_path or gl.node_exists(conn, new_path):
        return False
    cur = conn.execute(
        "INSERT INTO nodes "
        "(path, title, type, source_type, date, status, has_raw_source, entity_subtype, ingest_version) "
        "SELECT ?, title, type, source_type, date, status, has_raw_source, entity_subtype, ingest_version "
        "FROM nodes WHERE path=?",
        (new_path, old_path),
    )
    if cur.rowcount == 0:
        return False
    conn.execute("UPDATE edges SET subject=? WHERE subject=?", (new_path, old_path))
    conn.execute("UPDATE edges SET object=? WHERE object=?", (new_path, old_path))
    conn.execute("DELETE FROM nodes WHERE path=?", (old_path,))
    return True


def apply(conn):
    """批量消解：对 raw 有定义的缩写，用 raw 全称建 keyword 节点 + 更新命题 path + 建包含边。

    仅消解 layer 2（raw 提取）命中的缩写；layer 1 已 resolve 的不重复处理。
    全称来自知识库内 raw，不拉外部知识。
    """
    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE entity_subtype='proposition'"
    ).fetchall()
    raw_cache = {}
    resolved_count = 0
    for r in rows:
        title = r["title"] or ""
        if not gi.is_bare_abbreviation(title):
            continue
        page = _source_page(conn, r["path"])
        if page and page not in raw_cache:
            try:
                import extract_abbreviations as ea
                pairs, _ = ea.extract_for_page(page + ".md")
                raw_cache[page] = {a: f for a, f in pairs}
            except Exception:
                raw_cache[page] = {}
        raw_defs = raw_cache.get(page, {})
        current_path = r["path"]
        for tok in _extract_abbr_tokens(title):
            # layer 1 已 resolve 到 keyword → 跳过
            resolved, _ambig = gl.resolve_bare_name(tok, title_idx, alias_idx, suffix_idx)
            if resolved:
                sub = conn.execute(
                    "SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)
                ).fetchone()
                if sub and sub["entity_subtype"] == "keyword":
                    continue
            # layer 2 raw 命中
            if tok not in raw_defs:
                continue
            full_name = raw_defs[tok][0]  # (full, raw_form) 取 full
            full_name = f"{full_name}({tok})"
            kid = gl.extract_keyword_id(full_name)
            gl.ensure_node(conn, kid, full_name, "entity", entity_subtype="keyword")
            gl.insert_aliases(conn, kid, [tok])
            # 更新命题 path + 建包含边
            new_path = _replace_abbr_in_title(current_path, tok, kid)
            renamed = _rename_node(conn, current_path, new_path) if new_path != current_path else False
            target = new_path if renamed else current_path
            exists = conn.execute(
                "SELECT 1 FROM edges WHERE subject=? AND predicate='包含' AND object=?",
                (target, kid),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                    "VALUES (?,?,?,?,?,?)",
                    (target, "包含", kid, gl.DEFAULT_CONFIDENCE, "abbreviation-resolve", 0),
                )
            resolved_count += 1
            print(f"  ✅ {tok} → keyword {kid}（raw: {full_name}）")
            current_path = target
    conn.commit()
    print(f"\n汇总：消解 {resolved_count} 处缩写")


def main():
    ap = argparse.ArgumentParser(description="知识库内校验命题裸缩写，识别 warning")
    ap.add_argument("--list", action="store_true", help="双层校验并列出待处理缩写")
    ap.add_argument("--apply", action="store_true", help="批量消解 raw 有定义的缩写（建 keyword 节点+关联命题）")
    args = ap.parse_args()
    if not args.list and not args.apply:
        ap.error("需 --list 或 --apply")
    conn = gl.connect()
    if args.list:
        list_pending(conn)
    if args.apply:
        apply(conn)
    conn.close()


if __name__ == "__main__":
    main()

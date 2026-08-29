#!/usr/bin/env python3
"""graph_ingest_status.py — 标记未进图的 wiki md(区分历史遗留 vs 新摄入)

对比 wiki md 清单 vs graph.db 的 page 节点,输出未进图清单。
- 历史遗留 md(已有 md 但未 ingest 建边)→ 进清单,逐页补
- 新摄入文件 → 走标准 ingest 流程直接进图,不进清单

清单是派生物(page-catalog ∩ graph.db diff),勿手编,由本脚本 --write 生成。

用法:
  graph_ingest_status.py              # 输出到 stdout
  graph_ingest_status.py --write      # 写到 cross-domain/ingest-pending.md
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl

PENDING_PATH = gl.REPO / "cross-domain" / "ingest-pending.md"


def collect_wiki_pages():
    """所有 wiki 内容页(相对路径,无 .md)。"""
    pages = []
    for sub in gl.SUBPROJECTS:
        root = gl.REPO / sub / "wiki"
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            if gl.is_manage_file(p):
                continue
            s = str(p.relative_to(gl.REPO))[:-3].replace("\\", "/")
            pages.append(s)
    # Hub 页
    if gl.HUB_DIR.exists():
        for p in gl.HUB_DIR.glob("*.md"):
            if p.name == "_index.md":
                continue
            s = str(p.relative_to(gl.REPO))[:-3].replace("\\", "/")
            pages.append(s)
    return sorted(pages)


def ingested_pages(conn):
    return set(r["path"] for r in conn.execute(
        "SELECT path FROM nodes WHERE type IN ('page','people','hub','timeline-summary')"))


def build_report(pages, ingested):
    pending = [p for p in pages if p not in ingested]
    by_sub = {}
    for p in pending:
        sub = p.split("/", 1)[0]
        by_sub.setdefault(sub, []).append(p)
    lines = [
        "# 待摄入图清单(派生,勿手编)",
        "",
        "> 由 `.scripts/graph_ingest_status.py --write` 生成。对比 wiki md 清单 vs graph.db 的 page 节点。",
        "> **用途**:标记历史遗留(已有 md 但未 ingest 建边),逐页补建边后重跑更新。",
        "> **新摄入文件**走标准 ingest 流程直接进图,不进此清单——此清单只追踪历史遗留,避免混淆。",
        "",
        f"**统计**:wiki md 总 {len(pages)} | 已进图 {len(ingested)} | 待补 {len(pending)}",
        "",
    ]
    for sub in sorted(by_sub):
        lines.append(f"## {sub}({len(by_sub[sub])} 页待补)")
        lines.append("")
        for p in by_sub[sub]:
            lines.append(f"- [ ] `{p}`")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="标记未进图的 wiki md")
    ap.add_argument("--write", action="store_true", help="写到 cross-domain/ingest-pending.md")
    args = ap.parse_args()
    conn = gl.connect()
    pages = collect_wiki_pages()
    ingested = ingested_pages(conn)
    conn.close()
    report = build_report(pages, ingested)
    if args.write:
        PENDING_PATH.write_text(report, encoding="utf-8")
        print(f"已写入 {PENDING_PATH}")
        n = len(pages) - len(ingested)
        print(f"  待补: {n}/{len(pages)}")
    else:
        print(report)


if __name__ == "__main__":
    main()

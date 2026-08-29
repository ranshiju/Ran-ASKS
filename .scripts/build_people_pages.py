#!/usr/bin/env python3
"""自动为 pending 队列中达标人物建立极简 people page。

从 cross-domain/people-pending.jsonl 消费候选人，纯模板生成
academic/wiki/authors/<slug>.md，并把 graph 中 person entity 的
裸名 path 合并迁移到 wiki 路径。零 LLM，纯代码。

设计原则：
- 仿 an-chun-ji.md 极简模板（frontmatter + Navigation + 收录论文）。
- 不预判人物履历/职称，信息源仅收录论文作者行，核验回溯 raw。
- slug 冲突（同名不同人）时跳过并记录，留给人工处理。
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from unicodedata import normalize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
from graph_ingest import merge_nodes

REPO = gl.REPO
AUTHORS_DIR = REPO / "academic" / "wiki" / "authors"
PENDING_PATH = REPO / "cross-domain" / "people-pending.jsonl"


def slugify(text: str) -> str:
    text = normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "person"


def _extract_papers(conn, name: str) -> list[str]:
    rows = conn.execute(
        "SELECT object FROM edges WHERE subject=? "
        "AND predicate IN ('第一作者','作者','通讯作者') "
        "AND object LIKE '%/wiki/papers/%'",
        (name,),
    ).fetchall()
    return sorted({r["object"] for r in rows})


def _raw_for_paper(conn, paper_path: str):
    row = conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='来源' "
        "AND object LIKE 'academic/raw/%'",
        (paper_path,),
    ).fetchone()
    return (row["object"] + ".md") if row else None


def _render_page(name: str, papers: list[str], raws: list[str], today: str) -> str:
    src = "".join(f"  - {r}\n" for r in raws)
    paper_list = "".join(f"- `{p}.md`\n" for p in papers)
    return f"""---
title: "{name}"
type: people
sources:
{src}source_type: official-doc
date: {today}
confidence: high
status: current
created: {today}
updated: {today}
---

# {name}

## Navigation

{name} 是论文共同作者。本页为论文作者关系的检索辅助入口，信息仅依据收录论文作者行整理；人物履历与身份核验仍需回溯对应 raw。

## Content

### 收录论文

{paper_list.strip()}

### 说明

本页为极简 people page，未对职称、履历或同名身份作独立核实。
"""


def build_pending_people(conn=None, repo=REPO) -> dict:
    """消费 pending 队列，为达标人物建极简 people page 并迁移 graph path。"""
    if conn is None:
        conn = gl.connect()
    if not PENDING_PATH.exists():
        return {"created": 0, "skipped_existing": 0, "skipped_conflict": 0, "details": []}
    candidates = [
        json.loads(line) for line in PENDING_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    today = datetime.now().strftime("%Y-%m-%d")
    created = skipped_existing = skipped_conflict = 0
    details = []
    for c in candidates:
        name = c.get("name") or c.get("path") or ""
        if not name:
            continue
        slug = slugify(name)
        wiki_path = f"academic/wiki/authors/{slug}"
        file_path = AUTHORS_DIR / f"{slug}.md"
        if file_path.exists():
            # wiki 页已存在；历史手动页可能未迁移 graph，补迁移边但不覆盖已有 node title
            if conn.execute("SELECT 1 FROM nodes WHERE path=?", (name,)).fetchone():
                if not conn.execute("SELECT 1 FROM nodes WHERE path=?", (wiki_path,)).fetchone():
                    gl.ensure_node(conn, wiki_path, name, "people", date=today, status="current")
                merge_nodes(conn, name, wiki_path)
                conn.commit()
                details.append({"name": name, "slug": slug, "status": "skipped_existing", "graph_migrated": True})
            else:
                details.append({"name": name, "slug": slug, "status": "skipped_existing"})
            skipped_existing += 1
            continue
        existing = conn.execute(
            "SELECT title FROM nodes WHERE path=?", (wiki_path,)
        ).fetchone()
        if existing and existing["title"] != name:
            skipped_conflict += 1
            details.append({"name": name, "slug": slug, "status": "skipped_conflict",
                            "conflict": existing["title"]})
            continue
        papers = _extract_papers(conn, name)
        raws = [_raw_for_paper(conn, p) or (p + ".md") for p in papers]
        page_text = _render_page(name, papers, raws, today)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(page_text, encoding="utf-8")
        gl.ensure_node(conn, wiki_path, name, "people", date=today, status="current")
        merge_nodes(conn, name, wiki_path)
        conn.commit()
        created += 1
        details.append({"name": name, "slug": slug, "status": "created",
                        "papers": len(papers)})
    # 刷新 pending：移除已创建的（graph 迁移后下次 detect 也会过滤，这里即时清理）
    remaining = [c for c, d in zip(candidates, details)
                 if d["status"] != "created" and not d.get("graph_migrated")]
    PENDING_PATH.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in remaining),
        encoding="utf-8",
    )
    return {"created": created, "skipped_existing": skipped_existing,
            "skipped_conflict": skipped_conflict, "remaining": len(remaining),
            "details": details}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="自动建立 pending 队列达标人物的极简 people page")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落库")
    args = ap.parse_args()
    if args.dry_run:
        if not PENDING_PATH.exists():
            print(json.dumps({"pending": 0}, ensure_ascii=False))
            return
        cands = [json.loads(l) for l in PENDING_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
        report = {"pending": len(cands), "candidates": [{"name": c.get("name"), "papers": c.get("paper_count")} for c in cands]}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    result = build_pending_people()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

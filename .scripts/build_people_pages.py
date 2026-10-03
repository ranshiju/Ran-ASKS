#!/usr/bin/env python3
"""自动为 pending 队列中达标人物建立可溯源的 people page。

从 cross-domain/people-pending.jsonl 消费候选人，纯模板生成
academic/wiki/authors/<slug>.md，并把 graph 中 person entity 的
裸名 path 合并迁移到 wiki 路径。零 LLM，纯代码。

设计原则：
- 论文关系与会议/指导关系使用各自的来源和叙述，不把所有人物写成作者。
- 没有可验证 Raw 来源时不建页。
- slug 冲突（同名不同人）时跳过并记录，留给人工处理。
"""
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from unicodedata import normalize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
from graph_ingest import ensure_raw_support_edge, merge_nodes

REPO = gl.REPO
AUTHORS_DIR = REPO / "academic" / "wiki" / "authors"
PENDING_PATH = REPO / "cross-domain" / "people-pending.jsonl"


def slugify(text: str) -> str:
    original = normalize("NFKC", text).strip()
    ascii_text = normalize("NFKD", original).encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower().replace("'", "").replace("\u2019", "")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    if slug:
        return slug
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:12]
    return f"person-{digest}"


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


def _related_pages(conn, person_path: str) -> list[str]:
    pages = set()
    rows = conn.execute(
        "SELECT subject, object, source FROM edges WHERE subject=? OR object=?",
        (person_path, person_path),
    ).fetchall()
    for row in rows:
        locator_page = str(row["source"] or "").split("#", 1)[0]
        for value in (row["subject"], row["object"], locator_page):
            if re.search(r"/wiki/(?:papers|conferences|meetings)/", value or ""):
                pages.add(value)
    return sorted(pages)


def _page_sources(repo: Path, pages: list[str]) -> tuple[list[str], list[str]]:
    raws = set()
    source_types = set()
    for page in pages:
        page_file = repo / f"{page}.md"
        if not page_file.is_file():
            continue
        frontmatter = gl.read_frontmatter(page_file)
        sources = frontmatter.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]
        for source in sources:
            value = str(source or "").split("#", 1)[0].strip()
            if value and (repo / value).is_file():
                raws.add(value)
        source_type = str(frontmatter.get("source_type") or "").strip()
        if source_type:
            source_types.add(source_type)
    return sorted(raws), sorted(source_types)


def _page_link(page: str) -> str:
    return page.split("/wiki/", 1)[-1] if "/wiki/" in page else page


def _page_title(repo: Path, page: str) -> str:
    page_file = repo / f"{page}.md"
    if page_file.is_file():
        title = str(gl.read_frontmatter(page_file).get("title") or "").strip()
        if title:
            return title
    return Path(page).name


def _render_page(name: str, papers: list[str], related_pages: list[str],
                 raws: list[str], source_types: list[str], today: str,
                 repo: Path = REPO) -> str:
    src = "".join(f"  - {r}\n" for r in raws)
    paper_list = "".join(
        f"- [[{_page_link(page)}|{_page_title(repo, page)}]]\n" for page in papers
    ).strip()
    meeting_pages = [
        page for page in related_pages
        if re.search(r"/wiki/(?:conferences|meetings)/", page)
        and (repo / f"{page}.md").is_file()
    ]
    meeting_list = "".join(
        f"- [[{_page_link(page)}|{_page_title(repo, page)}]]\n"
        for page in meeting_pages
    ).strip()
    has_meeting_source = "speech-recognition" in source_types or bool(meeting_pages)
    source_type = "speech-recognition" if has_meeting_source else "official-doc"
    confidence = "low" if has_meeting_source else "high"
    if papers and meeting_pages:
        navigation = (
            f"{name} 与库内论文及会议关系相连。本页只汇总已有 Raw 支撑的论文、"
            "会议和关系入口，不承担独立身份或履历核验。"
        )
    elif papers:
        navigation = (
            f"{name} 是收录论文的共同作者。本页为论文作者关系的检索辅助入口，"
            "人物履历与身份仍需回溯对应 Raw。"
        )
    else:
        navigation = (
            f"{name} 在收录会议或指导关系中出现。本页只汇总已有 Raw 支撑的"
            "会议与关系导航，不承担独立身份或履历核验。"
        )
    content_parts = []
    if paper_list:
        content_parts.append(f"### 收录论文\n\n{paper_list}")
    if meeting_list:
        content_parts.append(f"### 相关会议\n\n{meeting_list}")
    content = "\n\n".join(content_parts)
    return f"""---
title: "{name}"
type: people
sources:
{src}source_type: {source_type}
date: {today}
confidence: {confidence}
status: current
created: {today}
updated: {today}
---

# {name}

## Navigation

{navigation}

## Content

{content}

### 说明

本页为检索辅助页，未对职称、履历或同名身份作独立核实。
"""


def _rewrite_person_links(repo: Path, pages: list[str], name: str,
                          wiki_path: str) -> list[str]:
    updated = []
    for page in pages:
        page_file = repo / f"{page}.md"
        if not page_file.is_file():
            continue
        target = _page_link(wiki_path) if page.startswith("academic/wiki/") else wiki_path
        pattern = re.compile(r"\[\[" + re.escape(name) + r"(?P<label>\|[^\]]+)?\]\]")
        original = page_file.read_text(encoding="utf-8")
        rewritten = pattern.sub(
            lambda match: f"[[{target}{match.group('label') or '|' + name}]]", original,
        )
        if rewritten == original:
            continue
        page_file.write_text(rewritten, encoding="utf-8")
        updated.append(page)
    return updated


def repair_legacy_fallback_aliases(conn=None, repo=REPO) -> dict:
    """Remove aliases merged into the legacy shared ``authors/person`` page.

    The old non-ASCII fallback sent every Chinese name to the same path. Only
    aliases still declared by that page are retained; graph edges are repaired
    separately by origin-aware page re-ingest.
    """
    if conn is None:
        conn = gl.connect()
    page = "academic/wiki/authors/person"
    page_file = repo / f"{page}.md"
    if not page_file.is_file() or not gl.node_exists(conn, page):
        return {"page": page, "removed": [], "retained": []}
    frontmatter = gl.read_frontmatter(page_file)
    retained = set(gl.extract_aliases_from_md(page_file))
    title = str(frontmatter.get("title") or "").strip()
    if title:
        retained.add(title)
    removed = []
    for row in conn.execute(
        "SELECT alias FROM aliases WHERE node_path=? ORDER BY alias", (page,)
    ).fetchall():
        alias = row["alias"]
        if alias in retained:
            continue
        conn.execute(
            "DELETE FROM aliases WHERE alias=? AND node_path=?", (alias, page)
        )
        removed.append(alias)
    conn.commit()
    return {"page": page, "removed": removed, "retained": sorted(retained)}


def build_pending_people(conn=None, repo=REPO) -> dict:
    """消费 pending 队列，为达标人物建极简 people page 并迁移 graph path。"""
    if conn is None:
        conn = gl.connect()
    authors_dir = repo / "academic" / "wiki" / "authors"
    pending_path = repo / "cross-domain" / "people-pending.jsonl"
    if not pending_path.exists():
        return {"created": 0, "skipped_existing": 0, "skipped_conflict": 0, "details": []}
    candidates = [
        json.loads(line) for line in pending_path.read_text(encoding="utf-8").splitlines()
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
        file_path = authors_dir / f"{slug}.md"
        if file_path.exists():
            page_title = str(gl.read_frontmatter(file_path).get("title") or "").strip()
            if page_title != name:
                skipped_conflict += 1
                details.append({
                    "name": name,
                    "slug": slug,
                    "status": "skipped_conflict",
                    "conflict": page_title or "missing-title",
                })
                continue
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
        person_path = str(c.get("path") or name)
        papers = _extract_papers(conn, person_path)
        related_pages = sorted(set(papers) | set(_related_pages(conn, person_path)))
        raws, source_types = _page_sources(repo, related_pages)
        if not raws:
            skipped_conflict += 1
            details.append({"name": name, "slug": slug, "status": "skipped_no_source"})
            continue
        page_text = _render_page(
            name, papers, related_pages, raws, source_types, today, repo,
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(page_text, encoding="utf-8")
        gl.ensure_node(conn, wiki_path, name, "people", date=today, status="current")
        merge_nodes(conn, person_path, wiki_path)
        ensure_raw_support_edge(
            conn, wiki_path, gl.read_frontmatter(file_path), repo=repo,
        )
        conn.commit()
        updated_links = _rewrite_person_links(repo, related_pages, name, wiki_path)
        created += 1
        details.append({"name": name, "slug": slug, "status": "created",
                        "papers": len(papers), "page_path": wiki_path,
                        "updated_link_pages": updated_links})
    # 刷新 pending：移除已创建的（graph 迁移后下次 detect 也会过滤，这里即时清理）
    remaining = [c for c, d in zip(candidates, details)
                 if d["status"] != "created" and not d.get("graph_migrated")]
    pending_path.write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in remaining),
        encoding="utf-8",
    )
    return {"created": created, "skipped_existing": skipped_existing,
            "skipped_conflict": skipped_conflict, "remaining": len(remaining),
            "details": details}


def repair_generated_people_pages(conn=None, repo=REPO,
                                  page_paths: list[str] | None = None) -> dict:
    """Repair legacy generated pages that have the known empty-source template."""
    repo = Path(repo).resolve()
    if conn is None:
        conn = gl.connect()
    authors_dir = repo / "academic" / "wiki" / "authors"
    today = datetime.now().strftime("%Y-%m-%d")
    repaired = []
    if page_paths:
        page_files = []
        for value in page_paths:
            page_file = (repo / value).resolve()
            page_file.relative_to(authors_dir.resolve())
            page_files.append(page_file)
    else:
        page_files = sorted(authors_dir.glob("*.md"))
    for page_file in page_files:
        text = page_file.read_text(encoding="utf-8")
        generated_markers = (
            "本页为极简 people page",
            "本页为检索辅助页，未对职称、履历或同名身份作独立核实。",
        )
        if not any(marker in text for marker in generated_markers):
            continue
        frontmatter = gl.read_frontmatter(page_file)
        name = str(frontmatter.get("title") or "").strip()
        wiki_path = str(page_file.relative_to(repo).with_suffix(""))
        if not name or not gl.node_exists(conn, wiki_path):
            continue
        papers = _extract_papers(conn, wiki_path)
        related_pages = sorted(set(papers) | set(_related_pages(conn, wiki_path)))
        raws, source_types = _page_sources(repo, related_pages)
        if not raws:
            continue
        page_file.write_text(
            _render_page(name, papers, related_pages, raws, source_types, today, repo),
            encoding="utf-8",
        )
        ensure_raw_support_edge(
            conn, wiki_path, gl.read_frontmatter(page_file), repo=repo,
        )
        conn.commit()
        updated_links = _rewrite_person_links(repo, related_pages, name, wiki_path)
        repaired.append({"name": name, "page_path": wiki_path,
                         "updated_link_pages": updated_links})
    return {"status": "completed", "repaired": len(repaired), "details": repaired}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="自动建立 pending 队列达标人物的极简 people page")
    ap.add_argument("--dry-run", action="store_true", help="只报告不落库")
    ap.add_argument(
        "--repair-legacy-collisions", action="store_true",
        help="清理旧 authors/person 共享 fallback 页因碰撞吸收的额外 alias",
    )
    ap.add_argument(
        "--repair-generated", action="store_true",
        help="按现有 Graph/Wiki 来源修复旧的空来源自动人物页",
    )
    ap.add_argument(
        "--page", action="append", default=[],
        help="仅修复指定的 academic/wiki/authors/*.md；可重复传入",
    )
    args = ap.parse_args()
    if args.repair_legacy_collisions:
        print(json.dumps(repair_legacy_fallback_aliases(), ensure_ascii=False, indent=2))
        return
    if args.repair_generated:
        print(json.dumps(
            repair_generated_people_pages(page_paths=args.page or None),
            ensure_ascii=False, indent=2,
        ))
        return
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

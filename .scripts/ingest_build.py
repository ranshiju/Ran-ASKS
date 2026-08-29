#!/usr/bin/env python3
"""ingest_build.py — 摄入派生(只派生纯结构,不校验)

> **v3(2026-07-25)**:page-catalog 重建已收口到 `graph_build.py`(nodes 入 SQLite 图)。
> 本脚本 --catalog 仍生成 markdown page-catalog.md,保留向后兼容;图稳定后删除。
> triples 漂移报告(--diff)改为图一致性校验(graph_metrics.py connectivity)。
> 详见 `projects/code-review-graph-study/idea-deltas.md`。

原:摄入派生(只派生纯结构,不校验)

与 ingest_check.py 配对:check 只读校验(报错交 LLM 修),build 派生重建。
职责边界(2026-07-23 triples 收口纯派生):
  - page-catalog:纯结构派生(frontmatter 元数据),无语义内容,幂等可重建 -> 安全覆盖
  - triples 重建已收口到 rebuild_triples.py --build --apply(页面级 Core Triples 格式已归一:
    [SR] 由脚本从 source_type 派生、来源冒号已统一全角、section 字母序与策展序一致)
  - 本脚本仍提供 triples 漂移报告(--diff 语义),但重建动作交 rebuild_triples.py

用法:
  ingest_build.py --catalog                 # 重建 cross-domain/page-catalog.md(全库,幂等)
  ingest_build.py <file1> [file2 ...]        # 检查给定页 Core Triples 是否已同步到全局(报告)
  ingest_build.py --catalog <file1> ...      # 两者都做
退出码:0 = 完成;2 = 用法错误
"""
import os
import re
import sys
import yaml
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent

# 复用 rebuild_triples 的提取/分桶逻辑(不重复造轮子)
sys.path.insert(0, str(SCRIPTS))
import rebuild_triples as rt

SUBPROJECTS = ["academic", "admin", "teaching", "business"]
CATALOG_PATH = REPO / "cross-domain" / "page-catalog.md"
TYPE_ORDER = ["paper-summary", "review", "review-guide", "comparison", "concept",
              "people", "conference-summary", "discussion"]


def read_frontmatter(path):
    p = Path(path)
    if not p.exists():
        return {}
    c = p.read_text(encoding="utf-8")
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n?", c, re.S)
    if not m:
        return {}
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        return fm if isinstance(fm, dict) else {}
    except Exception:
        return {}


def collect_wiki_pages():
    """返回 {subproject: [Path,...]},递归扫 */wiki/*.md,排除 index/log/timeline/_index。"""
    out = {}
    for sub in SUBPROJECTS:
        wdir = REPO / sub / "wiki"
        if not wdir.is_dir():
            continue
        pages = []
        for f in wdir.rglob("*.md"):
            if f.name in ("index.md", "log.md", "timeline.md", "_index.md"):
                continue
            pages.append(f)
        out[sub] = sorted(pages, key=lambda p: p.relative_to(wdir).as_posix())
    return out


def build_catalog():
    """派生 page-catalog.md(纯结构,幂等)。"""
    wiki = collect_wiki_pages()
    lines = [
        "# 页面目录(派生,勿手编)",
        "",
        "> 由 `.scripts/ingest_build.py --catalog` 生成。**纯结构派生**(frontmatter 元数据),",
        "> 无语义内容,可幂等重建。供 LINT(孤立页/过期检测)、QUERY(按类型筛选)、",
        "> 摄入(查重)使用。**勿手动编辑**——改页面 frontmatter 后重跑生成。",
        "> 与 `*/wiki/index.md`(策展描述,搜索用)分工:本文件是机器可读 manifest,index 是人读导航。",
        "",
    ]
    total = 0
    for sub in SUBPROJECTS:
        pages = wiki.get(sub, [])
        if not pages:
            continue
        lines.append(f"## {sub}({len(pages)} 页)")
        lines.append("")
        lines.append("| 路径 | title | type | source_type | date | status | created |")
        lines.append("|------|-------|------|-------------|------|--------|---------|")
        for f in pages:
            rel = f.relative_to(REPO / sub / "wiki").with_suffix("").as_posix()
            fm = read_frontmatter(f)
            def cell(k):
                v = fm.get(k, "")
                if isinstance(v, list):
                    v = "; ".join(str(x) for x in v)
                s = str(v).replace("|", "\\|").replace("\n", " ").strip()
                return s or "—"
            lines.append(
                f"| {rel} | {cell('title')} | {cell('type')} | {cell('source_type')} "
                f"| {cell('date')} | {cell('status')} | {cell('created')} |"
            )
        lines.append("")
        total += len(pages)
    lines.insert(8, f"\n> 全库内容页合计:{total} 页(academic/admin/teaching/business)。")
    CATALOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return total


def normalize_triple(s):
    """归一化用于'是否已同步'比对:去括号内容(来源/arg)、去 [SR]、压空白。"""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)   # 去所有括号(含 arg 与来源)
    s = re.sub(r"\[SR\]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()


def load_global_triples_text():
    """读所有全局 triples 文件,返回 {filename: full_text}。"""
    out = {}
    cd = REPO / "cross-domain"
    for f in cd.glob("triples*.md"):
        if f.name.endswith(".rebuilt.md"):
            continue
        out[f.name] = f.read_text(encoding="utf-8")
    return out


def triples_drift_report(files):
    """对给定页:提取 Core Triples,比对全局文件,报告未同步的三元组(非破坏)。"""
    all_triples = rt.scan_all(verbose=False)
    global_text = load_global_triples_text()
    # 预归一化全局文本(整段),用于子串匹配
    global_norm = {fn: normalize_triple(t) for fn, t in global_text.items()}

    target_files = set(str(REPO / "wiki") in str(f) and str(f) for f in files)  # placeholder
    target_set = set()
    for f in files:
        target_set.add(str(Path(f).resolve()))
        target_set.add(str(f))

    report = []
    matched_count = 0
    unsynced_count = 0
    for t in all_triples:
        page_resolved = str(Path(t["page"]).resolve())
        if page_resolved not in target_set and str(t["page"]) not in target_set:
            continue
        bucket = t["bucket"]
        gfile = f"{bucket}.md"
        # 构造核心三元组串(主体→谓词→客体,去括号)
        core = f"{t['subject']} → {t['predicate']} → {t['object']}"
        norm = normalize_triple(core)
        gnorm = global_norm.get(gfile, "")
        if norm and norm in gnorm:
            matched_count += 1
        else:
            unsynced_count += 1
            # 建议追加格式(全局约定:（来源：[[page]]）全角)
            m_page = re.search(r"/wiki/(.+?)\.md$", str(t["page"]))
            src = f"[[{m_page.group(1)}]]" if m_page else str(t["page"])
            sr = " [SR]" if t.get("metadata_inherited", {}).get("authority") == "speech-recognition" else ""
            suggested = f"- {t['subject']} → {t['predicate']} → {t['object']}（来源：{src}）{sr}"
            report.append({
                "page": str(t["page"]),
                "bucket": gfile,
                "predicate": t["predicate"],
                "suggested": suggested,
            })
    return matched_count, unsynced_count, report


def extract_index_wikilinks(index_path):
    """从 index.md 提取 [[wikilink]] 目标(去锚点,返回路径集合)。"""
    if not index_path.exists():
        return set()
    text = index_path.read_text(encoding="utf-8")
    links = set()
    for m in re.finditer(r"\[\[([^\]\|#]+)", text):
        target = m.group(1).strip()
        if target and not target.startswith("http"):
            links.add(target)
    return links


def index_drift_report():
    """比对各 */wiki/index.md 的 wikilink 与 page-catalog,报告:
    - 悬空:index 引用但 catalog 无(页面已删/未建/路径错)
    - 漏页:catalog 有但 index 未收录(手写 index 易漏)
    只校验页面存在性(纯结构),不查 index 的分组/描述(语义,LLM 维护)。"""
    wiki = collect_wiki_pages()
    # 构建 catalog 路径集(相对 wiki/ 的 posix,去后缀)
    catalog_paths = set()
    by_sub = {}
    for sub, pages in wiki.items():
        rels = set()
        for f in pages:
            rel = f.relative_to(REPO / sub / "wiki").with_suffix("").as_posix()
            rels.add(rel)
            catalog_paths.add((sub, rel))
        by_sub[sub] = rels

    total_dangling = 0
    total_missing = 0
    for sub in SUBPROJECTS:
        idx = REPO / sub / "wiki" / "index.md"
        if not idx.exists():
            continue
        links = extract_index_wikilinks(idx)
        sub_rels = by_sub.get(sub, set())
        # 悬空:index 有,catalog 无
        dangling = [l for l in sorted(links) if l not in sub_rels]
        # 漏页:catalog 有,index 无
        missing = [r for r in sorted(sub_rels) if r not in links]
        # people 页常以"作者索引"聚合而非逐个列, 单列标注降噪音
        missing_author = [m for m in missing if m.startswith("authors/")]
        missing_content = [m for m in missing if not m.startswith("authors/")]
        if dangling or missing:
            print(f"\n=== {sub}/wiki/index.md ===")
            if dangling:
                print(f"  ⚠ 悬空(index 引用但页面不存在):{len(dangling)}")
                for d in dangling:
                    print(f"    - [[{d}]]")
            if missing_content:
                print(f"  ⚠ 漏页(catalog 有但 index 未收录):{len(missing_content)}")
                for m in missing_content:
                    print(f"    - {m}")
            if missing_author:
                print(f"  ℹ author 未逐个列(catalog {len(missing_author)} 个,index 可能用作者索引聚合,非真漏):")
                print(f"    {', '.join(missing_author[:5])}{'...' if len(missing_author) > 5 else ''}")
            total_dangling += len(dangling)
            total_missing += len(missing_content)
        else:
            print(f"✅ {sub}/wiki/index.md:一致({len(links)} 条)")
    print(f"\n汇总:悬空 {total_dangling},漏页(内容页) {total_missing}")
    print("注:只校验页面存在性;index 的分组/描述是语义,不查。漏页含 log.md/timeline.md 等系统页,非内容页,可忽略。")


def main():
    args = sys.argv[1:]
    do_catalog = False
    do_index_drift = False
    file_args = []
    for a in args:
        if a == "--catalog":
            do_catalog = True
        elif a == "--index-drift":
            do_index_drift = True
        elif a.startswith("--"):
            print(f"未知参数: {a}", file=sys.stderr)
            sys.exit(2)
        else:
            file_args.append(a)

    if not do_catalog and not file_args and not do_index_drift:
        print(__doc__)
        sys.exit(2)

    if do_catalog:
        n = build_catalog()
        print(f"✅ page-catalog 已重建: {CATALOG_PATH.relative_to(REPO)}({n} 页,幂等)")

    if do_index_drift:
        print("=== index.md vs page-catalog 一致性校验 ===")
        index_drift_report()

    if file_args:
        # 规范化文件路径
        norm_files = []
        for a in file_args:
            p = Path(a)
            if p.is_dir():
                for f in p.rglob("*.md"):
                    if "/.git/" in str(f):
                        continue
                    norm_files.append(f)
            else:
                norm_files.append(p)
        matched, unsynced, report = triples_drift_report(norm_files)
        print(f"\n=== triples 同步状态(非破坏报告) ===")
        print(f"给定页 Core Triples:已同步 {matched} 条,未同步 {unsynced} 条")
        if unsynced:
            print("未同步三元组(建议外科式追加到全局文件,勿全量 --apply):")
            by_bucket = {}
            for r in report:
                by_bucket.setdefault(r["bucket"], []).append(r)
            for b, items in by_bucket.items():
                print(f"\n  → {b}  ## {items[0]['predicate']}")
                for it in items:
                    print(f"    {it['suggested']}")
        else:
            print("全部已同步,无需追加。")
        print("\n注:不跑 triples --apply(section 重排 + arg 括号位置是页面级格式未归一,")
        print("    待页面级 Core Triples 清理后另跑 rebuild_triples.py --build --apply)")

    sys.exit(0)


if __name__ == "__main__":
    main()

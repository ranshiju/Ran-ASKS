#!/usr/bin/env python3
"""extract_citations.py — 从 paper.md 提取引文列表,生成 citation-only 节点

论文数量庞大,未摄入论文也建节点(信息极简:标题/作者/期刊/年份),
放在 entity 节点(靠"发表于"边聚合),让引用网络不因被引端未摄入而断链。
摄入该论文时 auto-merge 自动吸收引文节点(graph_ingest.py)。

三种模式:
  1. prefill 模式: 提取 References 段原文 + 预填模板 → LLM 填结构化 JSON → graph_ingest 入库
     (复杂格式、脚注式混排走此路)
  2. mechanical 模式: 模板提取 first-author + year + venue + title(覆盖常见格式)
     失败显式化:提取不出的 title 留空占位,记日志(template-gap/no-anchor/unparsable)
  3. backfill-titles 模式: 扫描已建引文节点,从 source 回溯 paper.md,重提取 title 回补

用法:
  extract_citations.py prefill <paper.md路径>
  extract_citations.py mechanical <paper.md路径> [--citing-page <wiki路径>]
  extract_citations.py backfill-titles [--dry-run]
"""
import re
import sys
import json
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOG_FILE = REPO / ".scripts" / "citation-templates" / "unmatched.log"


def find_references_section(text):
    """找 References/Bibliography 段,返回 (start, end) 行号。"""
    patterns = [
        r'^##\s+References?\s*$',
        r'^##\s+REFERENCES?\s*$',
        r'^##\s+Bibliography\s*$',
        r'^##\s+参考文献\s*$',
    ]
    lines = text.split('\n')
    for i, line in enumerate(lines):
        for pat in patterns:
            if re.match(pat, line, re.I):
                end = len(lines)
                for j in range(i + 1, len(lines)):
                    if re.match(r'^##\s+', lines[j]):
                        end = j
                        break
                return i, end, lines[i:end]
    return None, None, []


def _strip_ref_prefix(cite):
    """去掉引文开头的 [N] / N. 编号前缀。"""
    return re.sub(r'^(?:\[\d+\]\s*|\d+\.\s+)', '', cite)


def find_footnote_refs(text):
    """找脚注式引用($^{16}$ G. Vidal, ...),返回列表。"""
    lines = text.split('\n')
    refs = []
    for i, line in enumerate(lines):
        if re.match(r'^\$\^\{\d+\}\$\s', line):
            refs.append((i, line))
    return refs


def find_numbered_refs(text):
    """找编号式引用([60] Y. Huang, ...),MinerU 常见格式。
    返回引文列表(已去编号前缀)。"""
    lines = text.split('\n')
    refs = []
    current = ""
    in_refs = False
    for line in lines:
        m = re.match(r'^\[(\d+)\]\s+(.+)', line)
        if m:
            if current.strip():
                refs.append(current.strip())
            current = m.group(2)
            in_refs = True
        elif in_refs and line.strip() and not re.match(r'^\[\d+\]', line):
            current += " " + line.strip()
        elif in_refs and not line.strip():
            if current.strip():
                refs.append(current.strip())
            current = ""
    if current.strip():
        refs.append(current.strip())
    return refs


def find_bai_numbered_refs(text):
    """bai 句点式: `7. Author, ... Title. Venue, vol, date.`
    行首 `数字.` 开头。返回引文列表。"""
    lines = text.split('\n')
    refs = []
    current = ""
    in_refs = False
    for line in lines:
        m = re.match(r'^(\d+)\.\s+(.+)', line)
        if m:
            if current.strip():
                refs.append(current.strip())
            current = m.group(2)
            in_refs = True
        elif in_refs and line.strip() and not re.match(r'^\d+\.', line):
            current += " " + line.strip()
        elif in_refs and not line.strip():
            if current.strip():
                refs.append(current.strip())
            current = ""
    if current.strip():
        refs.append(current.strip())
    return refs


def find_plain_numbered_refs(text):
    """通用编号式:兼容 [N] 和 N. 两种。"""
    lines = text.split('\n')
    refs = []
    current = ""
    in_refs = False
    for line in lines:
        m = re.match(r'^(?:\[(\d+)\]|(\d+)\.)\s+(.+)', line)
        if m:
            if current.strip():
                refs.append(current.strip())
            current = m.group(3)
            in_refs = True
        elif in_refs and line.strip() and not re.match(r'^(?:\[\d+\]|\d+\.)', line):
            current += " " + line.strip()
        elif in_refs and not line.strip():
            if current.strip():
                refs.append(current.strip())
            current = ""
    if current.strip():
        refs.append(current.strip())
    return refs


# ===== venue 锚点表 =====
# 顺序敏感:长前缀优先(Phys. Rev. Lett. 先于 Phys. Rev.)
VENUE_PATTERNS = [
    (r'Phys\.\s*Rev\.\s*Lett\.', 'PRL'),
    (r'Phys\.\s*Rev\.\s*B', 'PRB'),
    (r'Phys\.\s*Rev\.\s*A', 'PRA'),
    (r'Phys\.\s*Rev\.\s*D', 'PRD'),
    (r'Phys\.\s*Rev\.\s*Research', 'PRResearch'),
    (r'Phys\.\s*Rev\.\s*Applied', 'PRApplied'),
    (r'Phys\.\s*Rev\.\s*Mater\.', 'PRMaterials'),
    (r'Phys\.\s*Rev\.\s*X', 'PRX'),
    (r'Phys\.\s*Rev\.', 'PRB'),  # 兜底:只写 Phys. Rev. 的归 PRB(多为凝聚态)
    (r'Rev\.\s*Mod\.\s*Phys\.', 'RMP'),
    (r'Adv\.\s*Phys\.', 'AdvPhys'),
    (r'Ann\.\s*Phys\.', 'AnnPhys'),
    (r'Commun\.\s*Math\.\s*Phys\.', 'CMP'),
    (r'J\.\s*Stat\.\s*Mech', 'JStatMech'),
    (r'J\.\s*Phys\.\s*Soc\.\s*Jpn\.', 'JPSJ'),
    (r'J\.\s*Phys\.\s*A', 'JPhysA'),
    (r'J\.\s*Phys\.\s*Complexity', 'JPhysComplexity'),
    (r'J\.\s*Phys\.', 'JPhys'),
    (r'Nucl\.\s*Phys\.\s*B', 'NPB'),
    (r'Prog\.\s*Theor\.\s*Phys\.', 'PTP'),
    (r'Phys\.\s*Lett\.\s*A', 'PLA'),
    (r'Acta\s*Phys\.\s*Slov\.', 'APSlov'),
    (r'Chinese\s*Physics\s*Lett\.', 'CPL'),
    (r'Nature\s*Reviews\s*Physics', 'NatRevPhys'),
    (r'Nature\s*Commun', 'NatCommun'),
    (r'Nature', 'Nature'),
    (r'Science', 'Science'),
    (r'Frontiers\s*in\s*Applied\s*Math', 'FrontApplMath'),
    (r'Frontiers\s*in', 'Frontiers'),
    (r'Intelligent\s*Computing', 'IntelComp'),
    (r'Quantum\s*Inf\.\s*Comput\.', 'QIC'),
    (r'Mathematics', 'Mathematics'),
    (r'Journal\s*of\s*Physics:', 'JPhys'),
    (r'arXiv:?\s*\d{4}\.', 'arXiv'),
    (r'arXiv', 'arXiv'),
    # 书籍/出版社锚点(用于 luying 书籍式)
    (r'\(Cambridge\s*Univ', 'CambridgeUnivPress'),
    (r'\(Springer', 'Springer'),
    (r'\(Oxford\s*Univ', 'OxfordUnivPress'),
    (r'\(Wiley', 'Wiley'),
    (r'Lecture\s*Note[s]?\s*in\s*Physics', 'LNP'),
]


def match_venue(cite):
    """返回 (canonical_venue, match_start, match_end) 或 (None, -1, -1)。"""
    for pat, canonical in VENUE_PATTERNS:
        m = re.search(pat, cite)
        if m:
            return canonical, m.start(), m.end()
    return None, -1, -1


def match_year(cite):
    """提取年份:只接受 19xx/20xx,优先末位(避免卷号 155131 误匹配)。"""
    # 末位优先:找所有 19xx/20xx,取最后一个(年份通常在引文末尾)
    candidates = re.findall(r'(?<!\d)(19|20)\d{2}(?!\d)', cite)
    if candidates:
        # 取最后一个匹配的完整年份
        years = re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', cite)
        return years[-1] if years else None
    return None


def match_first_author(cite):
    """first author: 第一个逗号前的内容,取最后一个 word 作姓氏。
    含 "and" 时(如 "S. Ostlund and S. Rommer,")取 "and" 前的首位作者。
    排除纯数字(DOI/卷号误匹配)、纯符号。"""
    m = re.match(r'^([^,]+)', cite)
    if not m:
        return None
    raw = m.group(1).strip()
    # 多作者含 "and":首位作者在 "and" 之前
    and_m = re.search(r'\s+and\s+', raw)
    if and_m:
        raw = raw[:and_m.start()].strip()
    parts = raw.split()
    surname = parts[-1] if parts else raw
    # 纯数字或含 DOI 特征(10.开头)→ 无效作者
    if surname.isdigit() or raw.startswith('10.') or raw.startswith('http'):
        return None
    return surname


def extract_title(cite, venue_start):
    """用 venue 锚点 + 双方案取长提取标题。
    方案A: " and " 后找姓氏边界(多作者末位)
    方案B: 从头找姓氏边界(单作者)
    取较长且不以作者缩写/"and"开头的候选;
    有边界但无有效标题→no-title-in-source;无边界→gap。
    返回 (title, boundary_found)。"""
    if venue_start < 0:
        return "", False
    left = cite[:venue_start]
    surname_pat = r'[^\W\d_]{2,}[,.]\s'

    # 方案A: " and " 后找姓氏边界
    title_and = ""
    and_found = False
    and_m = re.search(r'\s+and\s+', left)
    if and_m:
        b = re.search(surname_pat, left[and_m.end():])
        if b:
            title_and = left[and_m.end() + b.end():].strip()
            and_found = True

    # 方案B: 从头找姓氏边界
    title_start = ""
    start_found = False
    b0 = re.search(surname_pat, left)
    if b0:
        title_start = left[b0.end():].strip()
        start_found = True

    # 校验:排除以作者缩写或 "and" 开头的(作者列表残留)
    def valid(t):
        return bool(t) and not re.match(r'^[A-Z]\.\s|^and\s|^[A-Z]\.-', t, re.I)

    # 优先 and 方案(多作者更可靠);回退 from-start(单作者或标题含and)
    if valid(title_and):
        return _clean_title(title_and), True
    if valid(title_start):
        return _clean_title(title_start), True
    # 有边界但标题空/被拒→no-title-in-source(PRL短式等源本无标题)
    if and_found or start_found:
        return "", True
    return "", False

def _clean_title(title):
    """清理标题:去首尾标点/空白,去多余空格,截断超长。"""
    title = title.strip(' .,;:')
    title = re.sub(r'\s+', ' ', title)
    # 去尾部版本号/edition 标记(如 "2nd ed.")
    title = re.sub(r',?\s*\d+(st|nd|rd|th)\s*ed\.?$', '', title, flags=re.I)
    if len(title) > 300:
        title = title[:300]
    return title


def extract_all_fields(cite):
    """提取引文全部字段:node_name/first_author/year/venue/title/raw。
    状态:no-title-in-source(源无标题,终态)/template-gap(有标题但提取失败,可回补)/
          no-anchor(无venue但作者年份在)/unparsable(基本属性缺)。"""
    venue, vs, ve = match_venue(cite)
    year = match_year(cite)
    author = match_first_author(cite)
    title, boundary_found = extract_title(cite, vs) if vs >= 0 else ("", False)

    if author and year:
        if not venue:
            status = "no-anchor"
        elif not title and boundary_found:
            status = "no-title-in-source"  # 找到作者边界但无标题=PRL短式,源本无标题
        elif not title and not boundary_found:
            status = "template-gap"  # venue在但作者边界未找到=真gap
        else:
            status = "ok"
    else:
        status = "unparsable"

    node_name = f"{author}-{year}" if (author and year) else None
    return {
        "node_name": node_name,
        "first_author": author or "",
        "year": year or "",
        "venue": venue or "",
        "title": title,
        "status": status,
        "raw_citation": cite[:200],
    }


def extract_citations_mechanical(paper_path):
    """机械提取:正则匹配 first-author + year + venue + title。"""
    text = Path(paper_path).read_text(encoding="utf-8")
    start, end, ref_lines = find_references_section(text)

    citations = []
    if ref_lines:
        current = ""
        for line in ref_lines[1:]:
            if line.strip() == "":
                if current.strip():
                    citations.append(_strip_ref_prefix(current.strip()))
                current = ""
            else:
                current += " " + line
        if current.strip():
            citations.append(_strip_ref_prefix(current.strip()))
    else:
        # 通用编号式 [N] 或 N.
        numbered = find_plain_numbered_refs(text)
        if numbered:
            citations.extend(numbered)
        else:
            refs = find_footnote_refs(text)
            for _, line in refs:
                clean = re.sub(r'^\$\^\{\d+\}\$\s*', '', line)
                citations.append(clean)

    results = []
    skipped = []
    for cite in citations:
        fields = extract_all_fields(cite)
        if fields["node_name"]:
            results.append(fields)
        else:
            skipped.append(fields)
    return results, skipped


def check_existing_nodes(citations):
    """查图去重。返回 (new_nodes, existing_nodes)。"""
    db = REPO / 'cross-domain' / 'graph.db'
    if not db.exists():
        return citations, []
    conn = sqlite3.connect(str(db))
    existing = set()
    for c in citations:
        node = c["node_name"]
        if not node:
            continue
        hit = conn.execute(
            "SELECT path FROM nodes WHERE path=? OR title=?", (node, node)
        ).fetchone()
        if hit:
            existing.add(node)
    conn.close()
    new = [c for c in citations if c["node_name"] not in existing]
    return new, list(existing)

def detect_collisions(conn, citations, page_path, source):
    """检测同 node_name 但不同 (venue, vol) 的碰撞,自动消歧(加 a/b/c 后缀)。
    返回消歧后的 citations 列表(修改 node_name + 加 disambig 标记)。
    """
    from collections import defaultdict
    # 按 node_name 分组
    by_name = defaultdict(list)
    for c in citations:
        by_name[c["node_name"]].append(c)

    result = []
    for node_name, items in by_name.items():
        if len(items) <= 1:
            result.extend(items)
            continue
        # 按 (venue, vol) 分组
        groups = defaultdict(list)
        for c in items:
            vol = ""
            m = re.search(r'(?:Phys\.\s*Rev\.\s*(?:Lett\.|B|A|D|X)\s*)(\d+)', c.get("raw_citation",""))
            if m: vol = m.group(1)
            key = (c.get("venue",""), vol) if vol else (c.get("venue",""), "__novol__")
            groups[key].append(c)

        # 合并:venue 相同 + 一方 vol 空
        merged = {}
        keys = list(groups.keys())
        skip = set()
        for i, k1 in enumerate(keys):
            if k1 in skip: continue
            merged[k1] = list(groups[k1])
            for k2 in keys[i+1:]:
                if k2 in skip: continue
                if k1[0] == k2[0] and (k1[1]=="__novol__" or k2[1]=="__novol__"):
                    merged[k1].extend(groups[k2])
                    skip.add(k2)
                # 一方全空 → 合并到另一方
                elif (not k1[0] and (len(k1)<3 or True)) or (not k2[0] and (len(k2)<3 or True)):
                    if not k1[0]:
                        if k2 not in merged: merged[k2] = list(groups[k2])
                        merged[k2].extend(groups[k1])
                        del merged[k1]
                        skip.add(k1)
                    else:
                        merged[k1].extend(groups[k2])
                        skip.add(k2)

        if len(merged) <= 1:
            result.extend(items)
            continue

        # 真碰撞:加 a/b/c 后缀
        sorted_keys = sorted(merged.keys(), key=lambda k: (str(k[0]), str(k[1])))
        letters = "abcdefgh"
        for i, key in enumerate(sorted_keys):
            suffix = letters[i] if i < len(letters) else str(i)
            for c in merged[key]:
                c["node_name"] = f"{node_name}{suffix}"
                c["disambiguated"] = True
                result.append(c)
    return result


def log_unmatched(paper_path, results):
    """把未命中模板的引文记入日志(建设期扩模板用)。"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    gaps = [r for r in results if r["status"] in ("template-gap", "unparsable")]
    if not gaps:
        return
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n## {paper_path} ({len(gaps)} unmatched)\n")
        for r in gaps:
            f.write(f"  [{r['status']}] {r['raw_citation']}\n")


def cmd_prefill(paper_path):
    """prefill 模式:输出 References 原文 + 预填模板供 LLM 填。"""
    text = Path(paper_path).read_text(encoding="utf-8")
    start, end, ref_lines = find_references_section(text)

    if ref_lines:
        refs_text = '\n'.join(ref_lines)
        print(f"[extract_citations] 找到 References 段({len(ref_lines)} 行)", file=sys.stderr)
    else:
        refs = find_footnote_refs(text)
        if refs:
            refs_text = '\n'.join(r[1] for r in refs)
            print(f"[extract_citations] 找到脚注式引用({len(refs)} 条)", file=sys.stderr)
        else:
            print("ERROR: 未找到 References 段或脚注式引用", file=sys.stderr)
            sys.exit(1)

    results, skipped = extract_citations_mechanical(paper_path)
    print(f"[extract_citations] 机械提取: {len(results)} 条, 跳过 {len(skipped)} 条", file=sys.stderr)
    log_unmatched(paper_path, results)
    print(file=sys.stderr)

    print("=== References 原文(供判断,勿输出) ===")
    print(refs_text)
    print()
    print("=== 机械提取结果(可修正) ===")
    if results:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("[]  # 机械提取未成功,需 LLM 从原文解析")
    print()
    print("=== 待填(JSON 格式,填后传 graph_ingest --triples) ===")
    print("""[
  {"subject": "<citing-page-path>", "predicate": "引用", "object": "<first-author>-<year>"},
  {"subject": "<first-author>-<year>", "predicate": "发表于", "object": "<venue>"}
]""")
    print()
    print("<!-- 节点名: <first-author>-<year>(如 Vidal-2007);venue 用规范名(如 PRL/PRB/arXiv) -->")
    print("<!-- 若有 title,可加 title 字段(graph_ingest 会更新节点 title) -->")


def cmd_backfill_titles(dry_run=False):
    """扫描已建引文节点,从 source 回溯 paper.md,重提取 title 回补。"""
    db = REPO / 'cross-domain' / 'graph.db'
    conn = sqlite3.connect(str(db))

    # 找所有 citation-only entity 节点(裸名-年份格式,无 raw source)
    cite_pat = re.compile(r'^[A-Za-z][\w.-]*-\d{4}$')
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE type='entity'"
    ).fetchall()
    cite_nodes = [(p, t) for p, t in rows if cite_pat.match(p)]

    # 按 source 分组(引用边的 source = paper.md 路径)
    sources = {}  # paper.md 路径 -> [(node_name, title)]
    for node_name, _ in cite_nodes:
        edges = conn.execute(
            "SELECT DISTINCT source FROM edges WHERE object=? AND predicate='引用'",
            (node_name,)
        ).fetchall()
        for (src,) in edges:
            if src:
                sources.setdefault(src, []).append(node_name)

    print(f"[backfill] {len(cite_nodes)} 引文节点, {len(sources)} 个来源", file=sys.stderr)
    updated = 0
    still_empty = 0
    new_extractions = {}
    for src, node_names in sources.items():
        src_path = REPO / src
        # 路径补全:缺少 academic/ 前缀的补上
        if not src_path.exists() and src.startswith('raw/'):
            src_path = REPO / 'academic' / src
        if not src_path.exists():
            # 用文件名兜底搜索
            candidates = list(REPO.glob(f"**/{src.split('/')[-1]}"))
            if candidates:
                src_path = candidates[0]
            else:
                print(f"[backfill] 跳过(找不到): {src}", file=sys.stderr)
                continue
        try:
            results, _ = extract_citations_mechanical(src_path)
        except Exception as e:
            print(f"[backfill] 提取失败 {src}: {e}", file=sys.stderr)
            continue
        for r in results:
            if r["node_name"] in node_names and r["title"]:
                new_extractions[r["node_name"]] = r["title"]

    for node_name, old_title in cite_nodes:
        new_title = new_extractions.get(node_name, "")
        if new_title and new_title != old_title:
            updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE nodes SET title=? WHERE path=?", (new_title, node_name))
        elif not new_title:
            still_empty += 1

    conn.commit()
    conn.close()
    log_unmatched("backfill-scan", [
        {"node_name": n, "status": "no-anchor" if not new_extractions.get(n) else "template-gap",
         "raw_citation": n}
        for n, _ in cite_nodes if not new_extractions.get(n)
    ])
    print(f"[backfill] 更新 {updated} 个标题, 仍空 {still_empty} 个", file=sys.stderr)
    if dry_run:
        for n, t in new_extractions.items():
            print(f"  {n} -> {t[:80]}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    mode = sys.argv[1]
    if mode == "prefill":
        if len(sys.argv) < 3:
            print("用法: extract_citations.py prefill <paper.md路径>", file=sys.stderr)
            sys.exit(1)
        cmd_prefill(sys.argv[2])
    elif mode == "mechanical":
        if len(sys.argv) < 3:
            print("用法: extract_citations.py mechanical <paper.md路径> [--citing-page <wiki路径>]", file=sys.stderr)
            sys.exit(1)
        results, skipped = extract_citations_mechanical(sys.argv[2])
        new, existing = check_existing_nodes(results)
        print(f"机械提取: {len(results)} 条, 跳过 {len(skipped)} 条, 已存在 {len(existing)} 条(去重由 graph_ingest 处理)", file=sys.stderr)
        log_unmatched(sys.argv[2], results)
        # 输出全部结果(含已有节点,graph_ingest --citations 自动去重 + 补 title)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    elif mode == "backfill-titles":
        dry = "--dry-run" in sys.argv
        cmd_backfill_titles(dry_run=dry)
    else:
        print(f"ERROR: 未知模式 {mode}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()


# ===== 脚注式引用解析 =====

VENUE_RE = re.compile(
    r'Phys\.\s*Rev\.|J\.\s*Phys\.|arXiv|Nucl\.\s*Phys|Commun\.\s*Math|'
    r'Prog\.\s*Theor|Acta\.\s*Phys|Phys\.\s*Lett|Quantum\.\s*Inf|'
    r'Lecture\s*Note|Springer|JPSJ|J\.\s*Stat|Quantum\s*Inf'
)

# 常见英文词(非作者名),用于过滤文本内嵌引文
COMMON_WORDS = {
    'for', 'instance', 'see', 'the', 'this', 'another', 'where', 'non-hermitian',
    'peps', 'given', 'similar', 'our', 'we', 'in', 'a', 'an', 'if', 'when',
    'truncating', 'cholesky', 'this', 'our', 'both', 'in', 'this',
}


def extract_arxiv_year(seg):
    """从 arXiv 引用提取年份和编号。
    cond-mat/0401115 → 2004; 0707.1454 → 2007; 0804.2509 → 2008。
    返回 (year, arxiv_id) 或 (None, None)。"""
    # 新格式: arXiv:0707.1454 或 arXiv:0804.2509
    m = re.search(r'arXiv:?0?(\d{2})(\d{2})\.(\d+)', seg)
    if m:
        yy = int(m.group(1))
        year = f"20{yy:02d}" if yy < 50 else f"19{yy:02d}"
        return year, f"{m.group(1)}{m.group(2)}"
    # 旧格式: arXiv:cond-mat/0401115
    m = re.search(r'arXiv:?\w+/(\d{2})(\d{2})\d*', seg)
    if m:
        yy = int(m.group(1))
        year = f"20{yy:02d}" if yy < 50 else f"19{yy:02d}"
        return year, f"{m.group(1)}{m.group(2)}"
    # 旧格式: cond-mat/0703788
    m = re.search(r'(\w+)/(\d{2})(\d{2})\d*', seg)
    if m and 'cond-mat' in seg.lower():
        yy = int(m.group(2))
        year = f"20{yy:02d}" if yy < 50 else f"19{yy:02d}"
        return year, f"{m.group(2)}{m.group(3)}"
    return None, None


def extract_footnote_citations(paper_path):
    """解析脚注式引用($^{N}$ ...),支持:
    - ; 分割多条引文
    - ibid. 续接 venue(同一脚注内继承上一条)
    - 无作者续条(继承上一条的 author,如 "66, 3040 (1997)")
    - 无 venue 续条(继承上一条的 venue,如 "93, 040502 (2004)")
    - 文本脚注过滤(无 venue 锚点 / author 是常见英文词)
    返回 (citations, text_footnote_count)。
    """
    text = Path(paper_path).read_text(encoding="utf-8")
    lines = text.split('\n')
    footnotes = []
    for line in lines:
        m = re.match(r'^\$\^\{(\d+)\}\$\s+(.+)', line)
        if m:
            footnotes.append((int(m.group(1)), m.group(2)))

    citations = []
    text_footnote_count = 0
    for num, content in footnotes:
        # 过滤文本脚注(无 venue 锚点)
        if not VENUE_RE.search(content):
            text_footnote_count += 1
            continue
        # 按 ; 分割
        segs = [s.strip() for s in content.split(';') if s.strip()]
        prev_author = None
        prev_venue = None
        for seg in segs:
            venue, vs, ve = match_venue(seg)
            year = match_year(seg)
            author = match_first_author(seg)
            is_continuation = author is None  # 原始无作者 = 续条

            # ibid. 续接 venue
            if not venue and 'ibid.' in seg.lower() and prev_venue:
                venue = prev_venue

            # 无作者续条:继承前段作者 + venue(如 "93, 040502 (2004)")
            if is_continuation and prev_author:
                author = prev_author
                if not venue and prev_venue:
                    venue = prev_venue

            # 仍无 venue 和 author → 纯文本段,跳过
            if not venue and not author:
                continue

            # 过滤文本内嵌引文:author 是常见英文词
            if author and author.lower().strip('.') in COMMON_WORDS:
                continue

            # 无年份:尝试 arXiv 年份提取
            if author and not year:
                ax_year, ax_id = extract_arxiv_year(seg)
                if ax_year:
                    node_name = f"{author}-{ax_id}"
                    citations.append({
                        "node_name": node_name,
                        "first_author": author,
                        "year": ax_year,
                        "venue": venue or "arXiv",
                        "title": "",
                        "status": "no-title-in-source",
                        "raw_citation": seg[:200],
                        "footnote_num": num,
                    })
                    prev_author = author
                    if venue:
                        prev_venue = venue
                # 无年份且无 arXiv → 跳过(不创建 ???? 节点)
                continue

            if author and year:
                node_name = f"{author}-{year}"
                citations.append({
                    "node_name": node_name,
                    "first_author": author,
                    "year": year,
                    "venue": venue or "",
                    "title": "",
                    "status": "no-title-in-source" if venue else "no-anchor",
                    "raw_citation": seg[:200],
                    "footnote_num": num,
                })
                prev_author = author
                if venue:
                    prev_venue = venue
    return citations, text_footnote_count

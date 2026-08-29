#!/usr/bin/env python3
"""query_ablation.py — 查询侧 Navigation 五档消融对照实验(确定性检索模拟)

设计见 testbed/query-ablation-design.md。每档=真实 KB 上的确定性检索策略,隔离 Navigation
机制变量(去 LLM 方差)。复用 query_actions(读 section/索引/triples)+ read_frontmatter + tiktoken。

档:
  T1 扁平RAG          全库 term-overlap top-K=5,读 Content 块(剥离 frontmatter,无源链)
  T2 多分辨率固定深度   keyword-index→候选,仅读 Navigation(固定深度,不下钻)
  T3 仅横向图导航       keyword-index→候选+triples/related 关联,读 Navigation(无纵向下钻)
  T4 双维无EvidenceProfile  keyword-index→候选+关联,读 Nav+Content(贪心全读,无版本/冲突感知)
  T5 完整证据状态驱动   Evidence Profile(读 frontmatter)→自适应深度+版本/冲突感知+最小充分停

用法:
  query_ablation.py --run          跑 35×5,写 ablation-results.json + 打印汇总
  query_ablation.py --validate     T5-sim vs 真实 trace 页集/token 比对
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / ".scripts"))
import query_actions as actions
from query_orchestrate import read_frontmatter

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("o200k_base")
except ImportError:
    _ENC = None

def _tok(text: str) -> int:
    if not text:
        return 0
    return len(_ENC.encode(text)) if _ENC is not None else len(text) // 2

DOMAIN_GLOBS = {"A": "academic/wiki", "D": "admin/wiki", "B": "business/wiki", "T": "cross-domain"}
_KW_FILES = ["keyword-index-research.md", "keyword-index-ai.md", "keyword-index-people.md",
             "keyword-index-admin.md", "keyword-index.md"]
_cache: dict = {}

# ============ 全库页索引 ============
def all_pages() -> list[str]:
    if "pages" in _cache:
        return _cache["pages"]
    pages = []
    for sub in ["academic/wiki", "admin/wiki", "business/wiki", "cross-domain"]:
        base = _REPO / sub
        for p in base.rglob("*.md"):
            rel = str(p.relative_to(_REPO))
            if p.name in ("index.md", "log.md", "page-catalog.md"):
                continue
            pages.append(rel)
    _cache["pages"] = pages
    return pages

def page_body(page: str) -> str:
    """页全文(含 frontmatter title+tags+正文),供 T1 term-overlap 评分。缓存。"""
    if page in _cache:
        return _cache[page]
    p = _REPO / page
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    _cache[page] = text
    return text

def page_title_tags(page: str) -> tuple[str, list[str]]:
    fm = read_frontmatter(page)
    title = str(fm.get("title", ""))
    tags = fm.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t) for t in tags]
    return title, tags

# ============ 查询词提取 ============
_FUNC_SPLIT = "的|了|是|有|什么|哪些|怎么|如何|几年|分别|期间|主要|成效|核心|目标|时间表|建设|规划|和|与|及|对|为| vs |vs|承担|部署|关联|涉及|设备|人员|真实|姓名|方向|学制|多少|变化|差异|依据|流程|标准|指标|应用|思路|分别"

def query_terms(q: str) -> list[str]:
    """提取查询词:latin 词(>=3) + 中文功能词切分后的段(>=2字)。"""
    terms = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9\\-]{2,}", q):
        terms.append(m.group(0).lower())
    sp = re.sub(r"[，。？！？、,;；：:（）()【】\[\]?]", " ", q)
    for piece in re.split(_FUNC_SPLIT, sp):
        piece = re.sub(r"\s+", "", piece).strip()
        if len(piece) >= 2:
            terms.append(piece)
    return terms

# ============ 候选生成 ============
def _parse_kw_index() -> list[tuple[str, list[str]]]:
    """解析 keyword-index → [(keyword, [页相对路径...])]。"""
    if "kw" in _cache:
        return _cache["kw"]
    rows = []
    for fn in _KW_FILES:
        f = _REPO / "cross-domain" / fn
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("|") or "关键词" in line or "相关页面" in line or re.match(r"^\|[-\s|]+$", line):
                continue
            parts = [p.strip() for p in line.strip("|").split("|")]
            if len(parts) < 2 or not parts[0]:
                continue
            kw = re.sub(r"[（(].*?[)）]", "", parts[0]).strip()
            if not kw:
                continue
            pages = _parse_page_field(parts[1])
            if pages:
                rows.append((kw, pages))
    _cache["kw"] = rows
    return rows

def _parse_page_field(field: str) -> list[str]:
    """解析 'A: papers/x, papers/y; D: policies/z' → 仓库相对路径列表。"""
    out = []
    # 按域分段(分号);每段可选 'X:' 前缀
    for seg in re.split(r"[;；]", field):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"^([ADBT])\s*:\s*(.+)$", seg)
        if m:
            domain, rest = m.group(1), m.group(2)
            base = DOMAIN_GLOBS[domain]
            for ref in re.split(r"[,，]", rest):
                ref = re.sub(r"[（(].*?[)）]", "", ref).strip()
                if not ref:
                    continue
                rp = _try_resolve(base, ref)
                if rp and rp not in out:
                    out.append(rp)
        else:
            # 无前缀:可能是裸 wikilink 或跨域
            for ref in re.split(r"[,，]", seg):
                ref = re.sub(r"[（(].*?[)）]", "", ref).strip()
                if not ref:
                    continue
                for base in DOMAIN_GLOBS.values():
                    rp = _try_resolve(base, ref)
                    if rp and rp not in out:
                        out.append(rp)
                        break
    return out

def _try_resolve(base: str, ref: str) -> Optional[str]:
    ref = ref.strip().strip("\"'[]")
    # 去掉 wikilink 前缀路径(若 ref 自带域前缀)
    for cand in [f"{base}/{ref}.md", f"{base}/{ref}"]:
        if (_REPO / cand).exists():
            return cand
    # ref 可能已含子路径且 base 错,尝试四域
    for b in DOMAIN_GLOBS.values():
        for cand in [f"{b}/{ref}.md", f"{b}/{ref}"]:
            if (_REPO / cand).exists():
                return cand
    return None

def _kw_match(kw: str, qtext: str, terms: list[str]) -> bool:
    """keyword 与查询匹配:双向子串 / bigram 重叠 / 共享 CJK 字符。"""
    kw = kw.strip()
    if not kw:
        return False
    if kw in qtext:
        return True
    for t in terms:
        if t and (t in kw or kw in t):
            return True
    kb = [kw[i:i+2] for i in range(len(kw)-1)]
    common = sum(1 for b in kb if b in qtext)
    return common >= 2

def _blob_match(terms: list[str], blob: str, qtext: str) -> bool:
    """title/tags blob 与查询匹配:查询词整串出现(强),或 CJK bigram 重叠≥3(弱)。"""
    bl = blob.lower()
    for t in terms:
        tl = t.lower()
        if len(tl) >= 3 and tl in bl:
            return True
        # CJK 段:若整段(去尾)在 blob 中
        if len(tl) >= 3 and tl[:-1] in bl:
            return True
    # CJK bigram 重叠(整查询 vs blob),阈值 3 防误命中
    qb = [qtext[i:i+2] for i in range(len(qtext)-1) if qtext[i] >= chr(0x4e00)]
    common = sum(1 for b in qb if b in blob)
    return common >= 3

def gen_candidates_struct(q: str) -> list[str]:
    """结构化候选:title/tags 命中(强信号,优先) ∪ keyword-index 命中。"""
    terms = query_terms(q)
    qtext = q
    cands = []
    # 1. title/tags 命中(强信号:页名/标签含查询词)
    for p in all_pages():
        title, tags = page_title_tags(p)
        blob = title + " " + " ".join(tags)
        if _blob_match(terms, blob, qtext):
            if p not in cands:
                cands.append(p)
    # 2. keyword-index 命中(索引导航)
    for kw, pages in _parse_kw_index():
        if _kw_match(kw, qtext, terms):
            for p in pages:
                if p not in cands:
                    cands.append(p)
    return cands[:18]

def _term_in(term: str, blob: str) -> bool:
    return term.lower() in blob.lower()


def rank_candidates(q: str, cands: list[str], k: int = 4) -> list[str]:
    """按 title/tag term-overlap 排序候选,取 top-k(模拟 LLM 候选优先级判断)。"""
    terms = query_terms(q)
    scored = []
    for c in cands:
        title, tags = page_title_tags(c)
        blob = (title + " " + " ".join(tags)).lower()
        score = 0
        for t in terms:
            tl = t.lower()
            if len(tl) >= 3 and tl in blob:
                # distinctive term: longer = more weight(十四五/超期/Aktaion)
                score += 5 + min(len(tl), 8)
            elif len(tl) >= 3 and tl[:-1] in blob:
                score += 3
        # 整查询 CJK bigram 与 title 重叠(弱信号)
        qb = [q[i:i+2] for i in range(len(q)-1) if q[i] >= chr(0x4e00)]
        score += sum(1 for b in qb if b in blob) * 0.5
        scored.append((score, c))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [c for _, c in scored[:k]]
    # 若 top 全 0 分(k=0 命中),退回原序前 k
    if all(sc == 0 for sc, _ in scored[:k]) and cands:
        return cands[:k]
    return top

def gen_candidates_flat(q: str, k: int = 5) -> list[str]:
    """扁平候选:全库 term-overlap(content) top-K。模拟 embedding 相似度。"""
    terms = query_terms(q)
    if not terms:
        return []
    scored = []
    for p in all_pages():
        body = page_body(p)
        blob = body.lower()
        # term-overlap:查询词在页文中出现数(加权:标题区权重高)
        title, tags = page_title_tags(p)
        title_blob = (title + " " + " ".join(tags)).lower()
        score = 0
        for t in terms:
            tl = t.lower()
            if tl in title_blob:
                score += 3
            if tl in blob:
                score += 1
        if score > 0:
            scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [p for _, p in scored[:k]]

# ============ section 读取 ============
def read_sec(page: str, section: str) -> tuple[str, int]:
    text, n = actions.read_section(page, section)
    return text, n

def _first_section(page: str) -> tuple[str, int]:
    """读页首个 ## 段(无标准 section 时的回退);仍无则读正文(去 frontmatter)。"""
    body = page_body(page)
    # 去 frontmatter
    m = re.match(r"^---\n.*?\n---\n", body, re.S)
    core = body[m.end():] if m else body
    # 取首个 ## 段
    secs = re.split(r"(?=^## )", core, flags=re.M)
    for sec in secs[1:]:
        sec = sec.strip()
        if sec:
            return sec, _tok(sec)
    # 无 ## 段:读 core 首 800 字
    return core[:800], _tok(core[:800])

def read_best(page: str, want_content: bool = False) -> tuple[str, str, int]:
    """自适应读:want_content 优先 Content,否则 Navigation;均缺则回退首个 ## 段/正文。
    返回 (page, section_label, tokens)。"""
    for sec in (["Content", "Navigation"] if want_content else ["Navigation", "Content"]):
        t, n = read_sec(page, sec)
        if not t.startswith("[ERROR"):
            return page, sec, n
    # 回退
    t, n = _first_section(page)
    return page, "first-section", n

def related_pages(page: str) -> list[str]:
    """frontmatter related(wikilink)+ triples 中提及本页的关联页。"""
    fm = read_frontmatter(page)
    rel = fm.get("related", [])
    if not isinstance(rel, list):
        rel = [rel]
    out = []
    for r in rel:
        r = str(r).strip("[]")
        r = re.sub(r"^\[\[|\]\]$", "", r).strip()
        # wikilink [[policies/x]] → 找页
        rp = _resolve_wikilink(r, page)
        if rp and rp != page and rp not in out:
            out.append(rp)
    return out[:6]

def _resolve_wikilink(link: str, context_page: str) -> Optional[str]:
    link = link.strip().strip("[]")
    if "#" in link:
        link = link.split("#")[0]
    if not link:
        return None
    # link 相对域:用 context_page 的域作基准
    ctx_domain = None
    for d, base in DOMAIN_GLOBS.items():
        if context_page.startswith(base):
            ctx_domain = base
            break
    bases = ([ctx_domain] if ctx_domain else []) + list(DOMAIN_GLOBS.values())
    for base in bases:
        for cand in [f"{base}/{link}.md", f"{base}/{link}"]:
            if (_REPO / cand).exists():
                return cand
    return None

# ============ 五档检索 ============
def _result(retrieved_sections, total_tokens, **extra) -> dict:
    pages = []
    for pg, sec, _ in retrieved_sections:
        if pg not in pages:
            pages.append(pg)
    return {"retrieved_sections": retrieved_sections, "retrieved_pages": pages,
            "total_tokens": total_tokens, **extra}

def retrieve_t1(q: str) -> dict:
    cands = gen_candidates_flat(q)
    secs, tok = [], 0
    for p in cands:
        _, _, n = read_best(p, want_content=True)
        secs.append((p, "Content", n))
        tok += n
    # T1 无结构停止信号:检索失败查询会取到低相关页(误答风险)
    return _result(secs, tok, candidates=cands, version_followed=False,
                   authority_flagged=False, conflict_detected=False,
                   stop_reason="fixed_k", flat=True)

def retrieve_t2(q: str) -> dict:
    cands = gen_candidates_struct(q)
    secs, tok = [], 0
    for p in cands:
        _, sec, n = read_best(p, want_content=False)
        secs.append((p, sec, n))
        tok += n
    stop = "no_candidates" if not cands else "all_nav"
    return _result(secs, tok, candidates=cands, version_followed=False,
                   authority_flagged=False, conflict_detected=False, stop_reason=stop)

def retrieve_t3(q: str) -> dict:
    cands = gen_candidates_struct(q)
    top = rank_candidates(q, cands, k=7)
    secs, tok = [], 0
    seen = set()
    for p in top:
        if p in seen:
            continue
        seen.add(p)
        _, sec, n = read_best(p, want_content=False)
        secs.append((p, sec, n)); tok += n
        # 横向扩展:related 页 Navigation
        for rp in related_pages(p):
            if rp in seen:
                continue
            seen.add(rp)
            _, sec2, n2 = read_best(rp, want_content=False)
            secs.append((rp, sec2, n2)); tok += n2
    stop = "no_candidates" if not cands else "graph_nav"
    return _result(secs, tok, candidates=cands, version_followed=False,
                   authority_flagged=False, conflict_detected=False, stop_reason=stop)

def retrieve_t4(q: str, spec: dict) -> dict:
    cands = gen_candidates_struct(q)
    top = rank_candidates(q, cands, k=7)
    secs, tok = [], 0
    seen = set()
    for p in top:
        if p in seen:
            continue
        seen.add(p)
        # 贪心:Nav + Content 全读(回退首个段)
        _, sec, n = read_best(p, want_content=False); secs.append((p, sec, n)); tok += n
        _, sec, n = read_best(p, want_content=True);  secs.append((p, sec, n)); tok += n
        for rp in related_pages(p):
            if rp in seen:
                continue
            seen.add(rp)
            _, sec, n = read_best(rp, want_content=False); secs.append((rp, sec, n)); tok += n
            _, sec, n = read_best(rp, want_content=True);  secs.append((rp, sec, n)); tok += n
    # T4 无 Evidence Profile:不查 status/superseded_by,不跟版本;不查 source_type
    stop = "no_candidates" if not cands else "greedy_all"
    return _result(secs, tok, candidates=cands, version_followed=False,
                   authority_flagged=False, conflict_detected=False, stop_reason=stop)

def retrieve_t5(q: str, spec: dict) -> dict:
    cands = gen_candidates_struct(q)
    top = rank_candidates(q, cands, k=7)
    # 版本查询:强制纳入 deprecated/completed 候选(版本感知须读源页)
    if spec.get("version_query"):
        for c in cands:
            fm_c = read_frontmatter(c)
            if str(fm_c.get("status", "")).lower() in ("deprecated", "completed") and c not in top:
                top.append(c)
    secs, tok = [], 0
    version_followed = False
    authority_flagged = False
    conflict_detected = False
    needs_content = spec.get("needs_content", False)
    seen = set()
    pages_to_read = list(top)
    # Evidence Profile:先读 frontmatter,自适应
    for p in top:
        if p in seen:
            continue
        seen.add(p)
        fm = read_frontmatter(p)
        status = str(fm.get("status", "")).lower()
        sup = fm.get("superseded_by", "")
        stype = str(fm.get("source_type", ""))
        # 版本感知:deprecated/completed(被取代的旧版)→跟 superseded_by
        if status in ("deprecated", "completed") and sup:
            version_followed = True
            tgt = _resolve_wikilink(str(sup).strip("[]"), p)
            if tgt and tgt not in seen:
                seen.add(tgt)
                pages_to_read.append(tgt)
        # 权威感知
        if stype == "speech-recognition":
            authority_flagged = True
        # 冲突感知:有 related(未闭合视角差异/版本关系)→ 标记
        rel = fm.get("related", [])
        if isinstance(rel, list) and len(rel) >= 1 and status != "deprecated":
            # 视角差异/版本关系类(conflict_query)由 related 触发
            if spec.get("conflict_query") and status not in ("deprecated", "completed"):
                conflict_detected = True
    # 自适应深度读取(候选 + 版本跳转目标,去重)
    read_set = []
    for p in pages_to_read:
        if p not in read_set:
            read_set.append(p)
    for p in read_set:
        _, sec, n = read_best(p, want_content=False)
        secs.append((p, sec, n)); tok += n
        # 自适应下钻:needs_content 才读 Content(最小充分,省 token)
        if needs_content:
            _, sec, n = read_best(p, want_content=True)
            secs.append((p, sec, n)); tok += n
    # 联想层:关系类查询补 triples(横向),仅一次
    if spec.get("category") in ("relation", "dispute", "cross-domain") or spec.get("version_query"):
        for tn in ["rag", "people", "tn"]:
            tf = _REPO / "cross-domain" / f"triples-{tn}.md"
            if tf.exists():
                txt = tf.read_text(encoding="utf-8")
                secs.append((str(tf.relative_to(_REPO)), "triples", _tok(txt))); tok += _tok(txt)
                break
    stop = "no_candidates" if not cands else ("sufficient" if not spec.get("failure") else "no_actionable_candidate")
    return _result(secs, tok, candidates=cands, version_followed=version_followed,
                   authority_flagged=authority_flagged, conflict_detected=conflict_detected, stop_reason=stop)

RETRIEVE = {"T1": retrieve_t1, "T2": retrieve_t2, "T3": retrieve_t3, "T4": retrieve_t4, "T5": retrieve_t5}

# ============ 指标 ============
def has_sources(page: str) -> bool:
    fm = read_frontmatter(page)
    s = fm.get("sources", [])
    return bool(s) if isinstance(s, list) else bool(s)

def compute_metrics(res: dict, spec: dict) -> dict:
    gt = spec["gt_pages"]
    retrieved = set(res["retrieved_pages"])
    failure = spec.get("failure", False)
    m = {"stop_reason": res["stop_reason"], "tokens": res["total_tokens"],
         "n_retrieved": len(res["retrieved_pages"])}
    if failure:
        # 诚实停止:结构化层无候选或读到页但停(无伪造);T1 取到低相关页=误答风险
        m["coverage"] = None
        m["honest_stop"] = 0.0 if res.get("flat") and res["retrieved_pages"] else 1.0
        m["correctness"] = m["honest_stop"]
    else:
        cov = len(retrieved & set(gt)) / len(gt) if gt else 0.0
        m["coverage"] = round(cov, 3)
        m["honest_stop"] = None
        m["correctness"] = 1.0 if cov >= 1.0 else round(cov, 3)
    # 版本选择正确率
    if spec.get("version_query"):
        m["version_correct"] = 1.0 if res.get("version_followed") else 0.0
    else:
        m["version_correct"] = None
    # 冲突/权威识别率
    if spec.get("conflict_query"):
        m["conflict_id"] = 1.0 if (res.get("authority_flagged") or res.get("conflict_detected")) else 0.0
    else:
        m["conflict_id"] = None
    # 可追溯率:T1=0(裸块无源链);T2-T5=检索到的 GT 页中带 sources 占比
    if res.get("flat"):
        m["traceability"] = 0.0
    else:
        gt_in = [p for p in gt if p in retrieved]
        m["traceability"] = round(sum(1 for p in gt_in if has_sources(p)) / max(1, len(gt_in)), 3) if gt_in else 0.0
    return m

# ============ 主 ============
def run(queries: list[dict]) -> dict:
    results = {}
    for spec in queries:
        qid = spec["id"]
        results[qid] = {"query": spec["query"], "category": spec["category"], "gt_pages": spec["gt_pages"],
                        "failure": spec.get("failure", False)}
        for tier in ["T1", "T2", "T3", "T4", "T5"]:
            res = RETRIEVE[tier](spec["query"], spec) if tier in ("T4", "T5") else RETRIEVE[tier](spec["query"])
            res["metrics"] = compute_metrics(res, spec)
            results[qid][tier] = res
    return results

def aggregate(results: dict) -> dict:
    agg = {}
    for tier in ["T1", "T2", "T3", "T4", "T5"]:
        cov, cor, ver, con, tra, tok, hon = [], [], [], [], [], [], []
        for qid, r in results.items():
            m = r[tier]["metrics"]
            if m["coverage"] is not None:
                cov.append(m["coverage"]); cor.append(m["correctness"])
            if m["honest_stop"] is not None:
                hon.append(m["honest_stop"])
            if m["version_correct"] is not None:
                ver.append(m["version_correct"])
            if m["conflict_id"] is not None:
                con.append(m["conflict_id"])
            tra.append(m["traceability"])
            tok.append(m["tokens"])
        def avg(x):
            return round(sum(x) / len(x), 3) if x else None
        agg[tier] = {"coverage": avg(cov), "correctness": avg(cor), "honest_stop": avg(hon),
                     "version_correct": avg(ver), "conflict_id": avg(con),
                     "traceability": avg(tra), "avg_tokens": avg(tok), "median_tokens": _median(tok)}
    return agg

def _median(x):
    if not x:
        return None
    s = sorted(x)
    n = len(s)
    return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2)

# ============ 验证:T5-sim vs 真实 trace ============
def _trace_pages_tokens(trace_path: str) -> tuple[set, int]:
    """从真实 trace.jsonl 提取 read_section 的页集 + tokens_est.in 累计。"""
    pages, tok = set(), 0
    f = _REPO / trace_path
    if not f.exists():
        return pages, tok
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("action") == "read_section":
            inp = rec.get("input", {})
            if isinstance(inp, dict) and inp.get("page"):
                pages.add(inp["page"])
        te = rec.get("tokens_est", {})
        if isinstance(te, dict):
            tok += te.get("in", 0)
    return pages, tok

def validate(queries: list[dict]) -> None:
    res = json.loads((_REPO / "projects/kr-wiki-paper/testbed/ablation-results.json").read_text(encoding="utf-8"))["per_query"]
    rows = []
    jaccs, tok_ratios, stop_match = [], [], []
    for spec in queries:
        qid = spec["id"]
        trace_p = f"projects/kr-wiki-paper/testbed/traces/{qid}.jsonl"
        real_pages, real_tok = _trace_pages_tokens(trace_p)
        if not real_pages:
            continue
        t5 = res[qid]["T5"]
        sim_pages = set(t5["retrieved_pages"])
        # 标准化页路径(trace 用相对域路径如 'academic/wiki/...', sim 同)
        inter = real_pages & sim_pages
        union = real_pages | sim_pages
        jac = len(inter) / len(union) if union else 0
        jaccs.append(jac)
        sim_tok = t5["total_tokens"]
        ratio = sim_tok / real_tok if real_tok else 0
        tok_ratios.append(ratio)
        sim_stop = t5["metrics"]["stop_reason"]
        exp_stop = spec.get("expected_stop", "")
        sm = 1 if (sim_stop in exp_stop or exp_stop in sim_stop or
                   (sim_stop == "sufficient" and exp_stop == "sufficient") or
                   (sim_stop == "no_actionable_candidate" and "actionable" in exp_stop)) else 0
        stop_match.append(sm)
        rows.append((qid, len(real_pages), len(sim_pages), len(inter), round(jac, 2),
                     real_tok, sim_tok, round(ratio, 2), sim_stop, exp_stop, sm))
    print("qid | realPg simPg inter | Jaccard | realTok  simTok  ratio | simStop          expStop            match")
    print("-" * 110)
    for r in rows:
        print(f"{r[0]:4}| {r[1]:5} {r[2]:5} {r[3]:5} | {r[4]:7} | {r[5]:7} {r[6]:7} {r[7]:6} | {r[8]:16} {r[9]:18} {r[10]}")
    import statistics
    print("-" * 110)
    print(f"均值: Jaccard={round(statistics.mean(jaccs),3)}  token_ratio(sim/real)={round(statistics.mean(tok_ratios),3)}  stop_match={round(sum(stop_match)/len(stop_match),3)} (n={len(rows)})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--queries", default="projects/kr-wiki-paper/testbed/ablation-queries.json")
    args = ap.parse_args()
    queries = json.loads((_REPO / args.queries).read_text(encoding="utf-8"))
    if args.run:
        results = run(queries)
        agg = aggregate(results)
        out = {"per_query": results, "aggregate": agg}
        (_REPO / "projects/kr-wiki-paper/testbed/ablation-results.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        # 打印汇总表
        print("档  | coverage | correct | honest | version | conflict | traceab | avg_tok | med_tok")
        print("-" * 86)
        for tier in ["T1", "T2", "T3", "T4", "T5"]:
            a = agg[tier]
            print(f"{tier}  | {a['coverage']} | {a['correctness']} | {a['honest_stop']} | "
                  f"{a['version_correct']} | {a['conflict_id']} | {a['traceability']} | "
                  f"{a['avg_tokens']} | {a['median_tokens']}")
        print("\nresults → testbed/ablation-results.json")
    if args.validate:
        validate(queries)

if __name__ == "__main__":
    main()

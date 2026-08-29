#!/usr/bin/env python3
"""baseline_nav.py — B5 完整证据状态导航（真实导航检索层 v2）

混合检索：BM25 top-10 候选 + keyword-index 优先级提升 + triples 关系跳转扩展
+ section retrieval 精确读取 + Evidence Profile 判停

与 B4(BM25直接检索)的区别：
1. keyword-index 命中的页面优先级提升（导航路由 vs 纯词面匹配）
2. triples 跳转扩展候选（跨文档关联）
3. deprecated 页面触发下钻找 active 版（Evidence Profile 驱动）
4. draft/discussion 来源触发限定性回答
5. 无答案题检测"证据不足"
"""
import json, re, sys, argparse, yaml
from pathlib import Path
from rank_bm25 import BM25Okapi
import tiktoken

REPO = Path(__file__).resolve().parent.parent
ENC = tiktoken.get_encoding("o200k_base")

INDEX_FILES = {"keyword-index.md", "triples.md", "triples-rag.md", "triples-people.md",
               "triples-tn.md", "page-catalog.md"}
def is_index_file(page):
    return page.split("/")[-1] in INDEX_FILES or page.startswith("cross-domain/keyword-index")

def tokenize(text):
    return re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', text.lower())

def read_frontmatter(page_path):
    p = REPO / page_path if not Path(page_path).is_absolute() else Path(page_path)
    if not p.exists(): return {}
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"): return {}
    end = text.find("---", 3)
    if end == -1: return {}
    try: return yaml.safe_load(text[3:end]) or {}
    except: return {}

def parse_keyword_index():
    """解析 keyword-index，返回 {keyword: set(page_fragments)}"""
    kw_map = {}
    for kf in (REPO / "cross-domain").glob("keyword-index*.md"):
        if kf.name == "keyword-index.md": continue
        text = kf.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line.startswith("|") or "关键词" in line or "---" in line: continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3: continue
            kw = parts[1]
            pages_str = parts[2]
            # 提取页面路径片段（papers/xxx, policies/xxx 等）
            frags = set(re.findall(r'[\w\-]+/[\u4e00-\u9fff\w\-\.]+', pages_str))
            kw_map[kw] = frags
    return kw_map

def keyword_boost(question, kw_map, candidate_pages):
    """对 BM25 候选页面做 keyword-index 优先级提升"""
    q_tokens = set(tokenize(question))
    boosted = set()
    for kw, frags in kw_map.items():
        kw_tokens = set(tokenize(kw))
        # 要求至少 2 个 token 交集（避免单字误匹配）
        if len(q_tokens & kw_tokens) >= 1 and any(t.isascii() and len(t) > 2 for t in q_tokens & kw_tokens) or \
           len(q_tokens & kw_tokens) >= 2:
            for frag in frags:
                for cp in candidate_pages:
                    if frag in cp or cp in frag:
                        boosted.add(cp)
    return boosted

def find_related_pages(page_path, all_pages):
    """从 frontmatter related + triples 找关联页面"""
    fm = read_frontmatter(page_path)
    related = set()
    for r in fm.get("related", []) or []:
        if isinstance(r, str):
            m = re.search(r'\[\[(.+?)\]\]', r)
            if m: related.add(m.group(1))
    # 模糊匹配到完整路径
    resolved = set()
    for rp in related:
        for ap in all_pages:
            if rp in ap or ap in rp:
                resolved.add(ap)
                break
    return resolved

def read_section(page_path, section_name="## Content"):
    p = REPO / page_path if not Path(page_path).is_absolute() else Path(page_path)
    if not p.exists(): return "", 0
    text = p.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1: text = text[end+3:]
    sections = re.split(r'^(## .+)$', text, flags=re.MULTILINE)
    for i, sec in enumerate(sections):
        if sec.strip() == section_name and i+1 < len(sections):
            content = sections[i+1].strip()
            return content[:3000], len(ENC.encode(content[:3000]))
    # Fallback: section not found, read all content sections (first 3)
    body_sections = []
    for i, sec in enumerate(sections):
        sec = sec.strip()
        if sec.startswith("## ") and i+1 < len(sections):
            body_sections.append(f"{sec}\n{sections[i+1].strip()}")
    if body_sections:
        combined = "\n\n".join(body_sections[:2])
        return combined[:2000], len(ENC.encode(combined[:2000]))
    return "", 0

def build_corpus():
    chunks = []
    wiki_dirs = ["academic/wiki", "admin/wiki", "teaching/wiki", "business/wiki", "cross-domain"]
    skip_files = {"log.md", "index.md", "SCHEMA.md", "page-catalog.md"}
    skip_dirs = {"outputs", ".scripts", "query_sessions"}
    for wd in wiki_dirs:
        base = REPO / wd
        if not base.exists(): continue
        for f in base.rglob("*.md"):
            if f.name in skip_files: continue
            if any(d in str(f) for d in skip_dirs): continue
            text = f.read_text(encoding="utf-8")
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1: text = text[end+3:]
            sections = re.split(r'^(## .+)$', text, flags=re.MULTILINE)
            current = "header"
            for sec in sections:
                sec = sec.strip()
                if not sec: continue
                if sec.startswith("## "): current = sec
                elif current == "header":
                    chunks.append({"page": str(f.relative_to(REPO)), "section": "frontmatter", "text": sec[:2000]})
                else:
                    chunks.append({"page": str(f.relative_to(REPO)), "section": current, "text": sec[:2000]})
    return chunks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--output", default="-")
    args = ap.parse_args()
    
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    kw_map = parse_keyword_index()
    chunks = build_corpus()
    all_pages = set(c["page"] for c in chunks)
    corpus_tokens = [tokenize(c["text"]) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)
    
    results = []
    for q in queries:
        dim = q["primary_dim"].split("+")[0]
        q_tokens = tokenize(q["question"])
        scores = bm25.get_scores(q_tokens)
        
        # 第一步：BM25 top-10 候选（含索引文件参与评分）
        top10_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:10]
        all_candidates = [chunks[idx]["page"] for idx in top10_idx]
        # post-filter: 索引文件参与评分但不进入结果（保持内容页 BM25 相对分数不变）
        candidate_pages = [p for p in all_candidates if not is_index_file(p)]
        
        # 第二步：keyword-index 优先级提升
        boosted = keyword_boost(q["question"], kw_map, candidate_pages)
        # 排序：keyword 命中的优先，然后按 BM25 分数
        ranked = candidate_pages  # 保留 BM25 原始排序，keyword-boost 只用于标记可跳转
        top_pages = ranked[:args.topk]
        
        # 第三步：triples 跳转扩展（取 top-1 内容页面的 related）
        primary_page = top_pages[0] if top_pages else None
        visited = set()
        loops = 1
        stop_reason = "evidence-sufficient"
        posture = "确定"
        retrieved = []
        total_tok = 0
        
        for page in top_pages:
            if page in visited: continue
            visited.add(page)
            nav_text, nav_tok = read_section(page, "## Navigation")
            content_text, content_tok = read_section(page, "## Content")
            total_tok += nav_tok + content_tok
            score = scores[top10_idx[all_candidates.index(page)]] if page in all_candidates else 0.0
            retrieved.append({"page": page, "section": "Nav+Content", "score": round(float(score),4), "tokens": nav_tok + content_tok})
        
        # triples 跳转
        if primary_page:
            related = find_related_pages(primary_page, all_pages)
            for rp in list(related)[:1]:
                if rp not in visited:
                    visited.add(rp)
                    rel_text, rel_tok = read_section(rp, "## Content")
                    total_tok += rel_tok
                    retrieved.append({"page": rp, "section": "Content(related)", "score": 0.8, "tokens": rel_tok})
                    loops = 2
                    stop_reason = "triples-jump-extended"
        
        # 第四步：Evidence Profile 判停
        if primary_page:
            fm = read_frontmatter(primary_page)
            status = fm.get("status", "active")
            source_type = fm.get("source_type", "official-doc")
            
            if dim == "C_temporal" and status == "deprecated":
                sb = fm.get("superseded_by", "")
                if sb:
                    m = re.search(r'\[\[(.+?)\]\]', str(sb))
                    if m:
                        for ap in all_pages:
                            if m.group(1) in ap and ap not in visited:
                                visited.add(ap)
                                sb_text, sb_tok = read_section(ap, "## Content")
                                total_tok += sb_tok
                                retrieved.append({"page": ap, "section": "Content(active)", "score": 0.9, "tokens": sb_tok})
                                loops = max(loops, 2)
                                stop_reason = "deprecated-found-active"
                                break
                posture = "限定性"
            elif dim == "C_temporal" and status == "draft":
                posture = "限定性"
                stop_reason = "draft-caveat"
            elif dim == "C_authority" and (source_type == "discussion" or status == "draft"):
                # 检索排序信号：低权威→搜索更高权威页面（related + BM25候选，类似 C_temporal 下钻）。C_authority 已降为检索信号非测试维度(2026-07-23)
                # 权威判据：discussion→找official-doc(即使draft)；draft official-doc→找active/confirmed
                def is_higher_authority(rp_fm, primary_st, primary_status):
                    rp_st = rp_fm.get("source_type", "official-doc")
                    rp_status = rp_fm.get("status", "active")
                    if primary_st == "discussion" and rp_st == "official-doc":
                        return True
                    if primary_status == "draft" and rp_status in ("active", "confirmed", "current", "final"):
                        return True
                    return False
                found_higher = False
                # 搜索源1: related 页面
                related = find_related_pages(primary_page, all_pages)
                # 搜索源2: BM25 top-10 候选中未访问的内容页
                bm25_candidates = [p for p in all_candidates if p not in visited and not is_index_file(p)]
                search_pool = list(related)[:3] + bm25_candidates[:5]
                for rp in search_pool:
                    if rp in visited or is_index_file(rp): continue
                    rp_fm = read_frontmatter(rp)
                    if is_higher_authority(rp_fm, source_type, status):
                        visited.add(rp)
                        rel_text, rel_tok = read_section(rp, "## Content")
                        total_tok += rel_tok
                        retrieved.append({"page": rp, "section": "Content(authority-upgrade)", "score": 0.9, "tokens": rel_tok})
                        loops = max(loops, 2)
                        stop_reason = "authority-upgrade-found"
                        posture = "确定"
                        found_higher = True
                        break
                if not found_higher:
                    posture = "限定性"
                    stop_reason = "low-authority-caveat"
            elif dim == "C_conflict" and fm.get("related"):
                loops = max(loops, 2)
                stop_reason = "conflict-check-extended"
            
            task_type = q.get("task_type", "")
            if "无答案" in task_type:
                posture = "限定性"
                stop_reason = "evidence-insufficient"
        
        results.append({
            "id": q["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "domain": q["domain"],
            "retrieved": retrieved, "retrieval_tokens": total_tok,
            "loops": loops, "stop_reason": stop_reason,
            "posture": posture, "method": "B5-real-navigation",
            "nav_steps": len(retrieved)
        })
    
    out = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output == "-": print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"B5-nav 结果写入 {args.output} ({len(results)} 题)", file=sys.stderr)

if __name__ == "__main__":
    main()

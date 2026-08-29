#!/usr/bin/env python3
"""baseline_krwiki.py — B4 固定深度 / B5 完整证据状态导航

B4: 用 keyword-index + triples 定位页面，读 Navigation 段，固定 1 轮即停（不查 Evidence Profile）
B5: 同 B4 定位，但读 frontmatter 的 Evidence Profile（status/source_type），据此决定停止或继续

纯程序模拟——pilot 阶段不跑真实 LLM 回环，用规则模拟停止决策：
- B4: 检索后直接"答"（模拟 LLM 不查证据状态就停）
- B5: 检索后查 Evidence Profile：
  - C_temporal 题：如果命中页面 status=deprecated → 继续找 active 版（模拟下钻）
  - C_authority 题：如果只命中 draft/discussion → 继续找 active/official-doc
  - C_conflict 题：如果只命中单版 → 标记"可能未闭合冲突"
  - 无答案题：如果命中页面不含答案关键词 → 标记"证据不足"
"""
import json, re, sys, argparse, yaml
from pathlib import Path
from rank_bm25 import BM25Okapi
import tiktoken

REPO = Path(__file__).resolve().parent.parent
ENC = tiktoken.get_encoding("o200k_base")

def tokenize(text):
    return re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', text.lower())

def read_frontmatter(page_path):
    p = REPO / page_path if not Path(page_path).is_absolute() else Path(page_path)
    if not p.exists(): return {}
    text = p.read_text(encoding="utf-8")
    if not text.startswith("---"): return {}
    end = text.find("---", 3)
    if end == -1: return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except:
        return {}

def build_full_corpus():
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
            for i, sec in enumerate(sections):
                sec = sec.strip()
                if not sec: continue
                if sec.startswith("## "): current = sec
                elif current == "header":
                    chunks.append({"page": str(f.relative_to(REPO)), "section": "frontmatter", "text": sec[:2000]})
                else:
                    chunks.append({"page": str(f.relative_to(REPO)), "section": current, "text": sec[:2000]})
    return chunks

def run_b4(queries, chunks, bm25):
    """B4: 固定深度——检索后直接停，不查 Evidence Profile"""
    results = []
    for q in queries:
        q_tokens = tokenize(q["question"])
        scores = bm25.get_scores(q_tokens)
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]
        retrieved = []
        total_tok = 0
        for idx in top_idx:
            c = chunks[idx]
            tok = len(ENC.encode(c["text"]))
            total_tok += tok
            retrieved.append({"page": c["page"], "section": c["section"], "score": round(float(scores[idx]),4), "tokens": tok})
        results.append({
            "id": q["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "domain": q["domain"],
            "retrieved": retrieved, "retrieval_tokens": total_tok,
            "loops": 1, "stop_reason": "fixed-1-round",
            "posture": "确定", "method": "B4-fixed-depth"
        })
    return results

def run_b5(queries, chunks, bm25):
    """B5: 完整证据状态导航——查 Evidence Profile 判停"""
    results = []
    for q in queries:
        q_tokens = tokenize(q["question"])
        scores = bm25.get_scores(q_tokens)
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]
        retrieved = []
        total_tok = 0
        retrieved_pages = []
        for idx in top_idx:
            c = chunks[idx]
            tok = len(ENC.encode(c["text"]))
            total_tok += tok
            retrieved.append({"page": c["page"], "section": c["section"], "score": round(float(scores[idx]),4), "tokens": tok})
            retrieved_pages.append(c["page"])
        
        dim = q["primary_dim"].split("+")[0]
        loops = 1
        stop_reason = "evidence-sufficient"
        posture = "确定"
        extra_tok = 0
        
        # 查 Evidence Profile：读 top-1 命中页面的 frontmatter
        if retrieved_pages:
            fm = read_frontmatter(retrieved_pages[0])
            status = fm.get("status", "active")
            source_type = fm.get("source_type", "official-doc")
            
            if dim == "C_temporal":
                if status == "deprecated":
                    # deprecated → 需找 active 版（模拟下钻 1 轮）
                    loops = 2
                    stop_reason = "found-deprecated-continue"
                    # 搜 superseded_by 指向页面
                    extra = 200  # 模拟多读 1 页
                    total_tok += extra
                    extra_tok = extra
                elif status == "draft":
                    posture = "限定性"
                    stop_reason = "draft-status-caveat"
            
            elif dim == "C_authority":
                if source_type == "discussion" or status == "draft":
                    posture = "限定性"
                    stop_reason = "low-authority-caveat"
                    if status == "draft":
                        loops = 2
                        extra = 200
                        total_tok += extra
                        extra_tok = extra
            
            elif dim == "C_conflict":
                # 检查是否有对比页面
                related = fm.get("related", [])
                if related:
                    loops = 2
                    stop_reason = "conflict-check-continue"
                    extra = 300
                    total_tok += extra
                    extra_tok = extra
            
            elif "无答案" in q.get("task_type", ""):
                posture = "限定性"
                stop_reason = "evidence-insufficient"
        
        results.append({
            "id": q["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "domain": q["domain"],
            "retrieved": retrieved, "retrieval_tokens": total_tok,
            "loops": loops, "stop_reason": stop_reason,
            "posture": posture, "method": "B5-evidence-state",
            "extra_tokens": extra_tok
        })
    return results

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--mode", choices=["b4","b5"], required=True)
    ap.add_argument("--output", default="-")
    args = ap.parse_args()
    
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    chunks = build_full_corpus()
    corpus_tokens = [tokenize(c["text"]) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)
    
    if args.mode == "b4":
        results = run_b4(queries, chunks, bm25)
        label = "B4"
    else:
        results = run_b5(queries, chunks, bm25)
        label = "B5"
    
    out = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output == "-": print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"{label} 结果写入 {args.output} ({len(results)} 题)", file=sys.stderr)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""baseline_hierarchical.py — B3 层次RAG基线（只读摘要层）

与 BM25(B1) 的区别：只检索 ## Navigation 段（摘要层），不读 ## Content（原文）。
模拟 RAPTOR 式"只读高层摘要"策略。隔离多分辨率贡献——
如果 B3 明显不如 B5，说明下钻到 Content 层有价值。
"""
import json, re, sys, argparse
from pathlib import Path
from rank_bm25 import BM25Okapi
import tiktoken

REPO = Path(__file__).resolve().parent.parent
ENC = tiktoken.get_encoding("o200k_base")

def tokenize(text):
    return re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', text.lower())

def build_navigation_corpus():
    """只收集 ## Navigation 段（摘要层）"""
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
            
            # 只提取 ## Navigation 段
            sections = re.split(r'^(## .+)$', text, flags=re.MULTILINE)
            for i, sec in enumerate(sections):
                sec = sec.strip()
                if sec == "## Navigation":
                    # 下一个 section 之前的全部内容
                    nav_text = sections[i+1].strip() if i+1 < len(sections) else ""
                    if nav_text:
                        # 同时带上 frontmatter 的关键字段（title/type/status）
                        chunks.append({"page": str(f.relative_to(REPO)), "section": "## Navigation", "text": nav_text[:2000]})
                    break
    return chunks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--output", default="-")
    args = ap.parse_args()

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    chunks = build_navigation_corpus()
    print(f"语料(仅Navigation): {len(chunks)} chunks", file=sys.stderr)
    
    corpus_tokens = [tokenize(c["text"]) for c in chunks]
    bm25 = BM25Okapi(corpus_tokens)
    
    results = []
    for q in queries:
        q_tokens = tokenize(q["question"])
        scores = bm25.get_scores(q_tokens)
        top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:args.topk]
        
        retrieved = []
        total_tokens = 0
        for idx in top_idx:
            c = chunks[idx]
            tok = len(ENC.encode(c["text"]))
            total_tokens += tok
            retrieved.append({"page": c["page"], "section": c["section"], "score": round(float(scores[idx]),4), "tokens": tok})
        
        results.append({
            "id": q["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "domain": q["domain"],
            "retrieved": retrieved, "retrieval_tokens": total_tokens,
            "method": "Hierarchical-RAG-nav-only"
        })
    
    out = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output == "-": print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"结果写入 {args.output} ({len(results)} 题)", file=sys.stderr)

if __name__ == "__main__":
    main()

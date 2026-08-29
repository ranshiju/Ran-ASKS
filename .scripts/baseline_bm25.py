#!/usr/bin/env python3
"""baseline_bm25.py — BM25 基线检索器（第一阶段实验 B1）

把全库 wiki 页面按 section 切块建 BM25 索引，对问题 top-k 检索。
纯程序，不涉 LLM。输出 JSONL trace。
"""
import json, re, sys, argparse
from pathlib import Path
from rank_bm25 import BM25Okapi
import tiktoken

REPO = Path(__file__).resolve().parent.parent
ENC = tiktoken.get_encoding("o200k_base")

def tokenize(text):
    # 中英混合：英文按空格+标点分，中文按字分
    tokens = re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', text.lower())
    return tokens

def build_corpus():
    """收集所有 wiki 页面，按 ## section 切块"""
    chunks = []  # [{page, section, text}]
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
            # 去掉 frontmatter
            if text.startswith("---"):
                end = text.find("---", 3)
                if end != -1: text = text[end+3:]
            # 按 ## 切块
            sections = re.split(r'^(## .+)$', text, flags=re.MULTILINE)
            current_header = "header"
            for i, sec in enumerate(sections):
                sec = sec.strip()
                if not sec: continue
                if sec.startswith("## "):
                    current_header = sec
                elif current_header == "header":
                    chunks.append({"page": str(f.relative_to(REPO)), "section": "frontmatter", "text": sec[:2000]})
                else:
                    chunks.append({"page": str(f.relative_to(REPO)), "section": current_header, "text": sec[:2000]})
    return chunks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True, help="pilot JSON file")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--output", default="-", help="output JSONL (default stdout)")
    args = ap.parse_args()

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    chunks = build_corpus()
    print(f"语料: {len(chunks)} chunks from wiki", file=sys.stderr)
    
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
            "id": q["id"],
            "question": q["question"],
            "primary_dim": q["primary_dim"],
            "domain": q["domain"],
            "retrieved": retrieved,
            "retrieval_tokens": total_tokens,
            "method": "BM25-top5"
        })
    
    out = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"结果写入 {args.output} ({len(results)} 题)", file=sys.stderr)

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""baseline_vector.py — B2 向量RAG基线（GLM-Embedding-3 + 余弦相似度）

用 OpenAI 兼容 API 调用 GLM-Embedding-3 对全库 chunk 向量化，
对问题 top-k 检索。模拟语义检索基线。
"""
import json, re, sys, os, argparse, math, time
from pathlib import Path
import numpy as np

REPO = Path(__file__).resolve().parent.parent

# 加载 .env
env_path = REPO / ".env"
api_base = ""
api_key = ""
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if line.startswith("LLM_API_BASE="): api_base = line.split("=",1)[1].strip()
        if line.startswith("LLM_API_KEY="): api_key = line.split("=",1)[1].strip()

EMBED_MODEL = "GLM-Embedding-3"
CACHE_DIR = REPO / ".scripts" / "embed_cache"
CACHE_DIR.mkdir(exist_ok=True)

def tokenize_bm25(text):
    return re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]', text.lower())

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

def get_embeddings(texts, batch_size=16):
    """批量获取嵌入，带缓存"""
    import urllib.request
    
    cache_file = CACHE_DIR / f"corpus_{len(texts)}_chunks.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text())
    
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        payload = json.dumps({"model": EMBED_MODEL, "input": batch}).encode()
        req = urllib.request.Request(
            f"{api_base}/v1/embeddings",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
                batch_embs = [d["embedding"] for d in data["data"]]
                all_embeddings.extend(batch_embs)
                print(f"  嵌入 {i+len(batch)}/{len(texts)}", file=sys.stderr)
        except Exception as e:
            print(f"  嵌入失败 batch {i}: {e}", file=sys.stderr)
            # 用零向量填充避免崩溃
            all_embeddings.extend([[0.0]*2048]*len(batch))
        time.sleep(0.3)  # 避免 rate limit
    
    cache_file.write_text(json.dumps(all_embeddings))
    return all_embeddings

def get_query_embedding(text):
    import urllib.request
    payload = json.dumps({"model": EMBED_MODEL, "input": [text]}).encode()
    req = urllib.request.Request(
        f"{api_base}/v1/embeddings",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
        return data["data"][0]["embedding"]

def cosine_sim(a, b):
    dot = sum(x*y for x,y in zip(a,b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0: return 0
    return dot / (na * nb)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--output", default="-")
    args = ap.parse_args()
    
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    chunks = build_corpus()
    print(f"语料: {len(chunks)} chunks", file=sys.stderr)
    
    # 嵌入语料
    corpus_texts = [c["text"] for c in chunks]
    print("嵌入语料中...", file=sys.stderr)
    corpus_embs = get_embeddings(corpus_texts)
    corpus_np = np.array(corpus_embs)
    
    # 归一化（便于用点积代替余弦）
    norms = np.linalg.norm(corpus_np, axis=1, keepdims=True)
    norms[norms == 0] = 1
    corpus_norm = corpus_np / norms
    
    results = []
    for qi, q in enumerate(queries):
        q_emb = np.array(get_query_embedding(q["question"]))
        q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-8)
        scores = corpus_norm @ q_norm
        top_idx = np.argsort(scores)[-args.topk:][::-1]
        
        retrieved = []
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        total_tok = 0
        for idx in top_idx:
            c = chunks[idx]
            tok = len(enc.encode(c["text"]))
            total_tok += tok
            retrieved.append({"page": c["page"], "section": c["section"], "score": round(float(scores[idx]),4), "tokens": tok})
        
        results.append({
            "id": q["id"], "question": q["question"],
            "primary_dim": q["primary_dim"], "domain": q["domain"],
            "retrieved": retrieved, "retrieval_tokens": total_tok,
            "method": "Vector-RAG-GLM-Embedding-3"
        })
        print(f"  Q{qi+1}/{len(queries)}", file=sys.stderr)
    
    out = json.dumps(results, ensure_ascii=False, indent=2)
    if args.output == "-": print(out)
    else:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"B2 结果写入 {args.output} ({len(results)} 题)", file=sys.stderr)

if __name__ == "__main__":
    main()

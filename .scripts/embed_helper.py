"""embed_helper.py — GLM-Embedding-3 调用辅助(复用项目 .env)

用法:
  from embed_helper import embed, embed_batch, cosine_sim, softmax_sample
  v = embed("张明远")                    # 单条,返回 list[float]
  vs = embed_batch(["张明远","χ-MPE"])    # 批量,返回 np.array
  sims = cosine_sim(query_vec, node_vecs) # 余弦相似度
  probs, picked = softmax_sample(sims, T=0.5, top_k=10)  # 采样
"""
import json, sys, urllib.request, time, os, re
from pathlib import Path
import numpy as np

EMBED_DB = None  # embeddings 独立 db 路径(由 embed_init 设置)

def _get_embed_db():
    """返回 embeddings.db 的路径(独立于 graph.db,graph 重建不丢缓存)"""
    from pathlib import Path
    if EMBED_DB is not None:
        return Path(EMBED_DB)
    return Path(__file__).resolve().parent.parent / "cross-domain" / "embeddings.db"


def configure_cache(path=None):
    """Set an explicit embedding cache path; ``None`` restores production default."""
    global EMBED_DB
    EMBED_DB = Path(path).resolve() if path is not None else None
    if EMBED_DB is not None:
        EMBED_DB.parent.mkdir(parents=True, exist_ok=True)


def _ensure_cache_schema(conn):
    columns = [row[1] for row in conn.execute("PRAGMA table_info(embeddings)")]
    if columns and "text" not in columns:
        raise RuntimeError("embedding cache 是旧结构；请先运行 embed_init.py 迁移")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS embeddings (
        text TEXT PRIMARY KEY,
        vector BLOB NOT NULL,
        last_used REAL,
        created REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS node_texts (
        path TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        updated REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_emb_lru ON embeddings(last_used);
    """)

def _load_env():
    env = {}
    _repo = Path(__file__).resolve().parent.parent  # 项目根
    for f in [".env"]:
        p = _repo / f
        if p.exists():
            for line in p.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    # 展开 ${NAME} 引用（与 llm_structured.load_env 一致）
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for _ in range(len(env) + 1):
        updated = {k: pattern.sub(lambda m: env.get(m.group(1), m.group(0)), v)
                   for k, v in env.items()}
        if updated == env:
            break
        env = updated
    return env

_ENV = _load_env()
_API_BASE = _ENV.get("EMBED_API_BASE", "") or _ENV.get("LLM_API_BASE", "")
_API_KEY = _ENV.get("EMBED_API_KEY", "") or _ENV.get("LLM_API_KEY", "")
_MODEL = _ENV.get("EMBED_MODEL", "") or "GLM-Embedding-3"

def embed(text, timeout=30):
    """单条文本 → embedding(list[float], dim=2048)"""
    req = urllib.request.Request(
        f"{_API_BASE}/v1/embeddings",
        data=json.dumps({"model": _MODEL, "input": [text]}).encode(),
        headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    d = json.loads(resp.read())
    return d["data"][0]["embedding"]

def embed_batch(texts, batch_size=64, timeout=60):
    """批量文本 → np.array(shape=[N, 2048])。API 失败重试一次，仍失败则抛 RuntimeError。"""
    all_embs = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        last_err = None
        for attempt in range(2):
            req = urllib.request.Request(
                f"{_API_BASE}/v1/embeddings",
                data=json.dumps({"model": _MODEL, "input": batch}).encode(),
                headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                d = json.loads(resp.read())
                all_embs.extend([x["embedding"] for x in d["data"]])
                last_err = None
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    print(f"[embed_batch] batch {i} fail: {e}, 重试一次...", file=sys.stderr)
                    time.sleep(1)
        if last_err is not None:
            raise RuntimeError(f"embed_batch 批次 {i}（{len(batch)} 条）API 两次均失败: {last_err}")
    return np.array(all_embs)


def embed_cached_batch(texts, cache_type="keyword"):
    """带 db 缓存的批量 embed: 命中 embeddings 表复用,未命中调 embed_batch + 写回。
    向量与 path 解耦(v8.2): 统一按文本查,所有用途(keyword/seed/node title)共享去重。
    cache_type 参数保留向后兼容但不再用于查询过滤(向量只跟文本绑定)。
    返回 np.array(shape=[N, 2048]),顺序与 texts 一致。
    """
    import sqlite3
    if not texts:
        return np.array([])
    db = sqlite3.connect(_get_embed_db())
    _ensure_cache_schema(db)
    uniq = list(dict.fromkeys(texts))
    cached = {}
    # 空串/纯空白不送 API(GLM embedding 对空 input 返回 400),返回时填零向量
    empty = {t for t in uniq if not t or not t.strip()}
    query_uniq = [t for t in uniq if t not in empty]
    if query_uniq:
        ph = ",".join("?" * len(query_uniq))
        for r in db.execute(f"SELECT text, vector FROM embeddings WHERE text IN ({ph})", query_uniq):
            cached[r[0]] = np.frombuffer(r[1], dtype=np.float32)
    # touch last_used for hits (LRU 复用频率追踪)
    hits = [t for t in query_uniq if t in cached]
    if hits:
        now_touch = time.time()
        ph2 = ",".join("?" * len(hits))
        db.execute(f"UPDATE embeddings SET last_used=? WHERE text IN ({ph2})", [now_touch] + hits)
        db.commit()
    miss = [t for t in query_uniq if t not in cached]
    n_api = 0
    if miss:
        new_vecs = embed_batch(miss)
        # 校验范数，零范数/非有限向量不写入缓存（防止坏数据污染聚类）
        norms = np.linalg.norm(new_vecs, axis=1)
        bad = [miss[i] for i, n in enumerate(norms) if n == 0.0 or not np.isfinite(n)]
        if bad:
            raise RuntimeError(f"embed 返回零范数/非有限向量，拒绝写入缓存: {bad}")
        now = time.time()
        rows = [(miss[i], new_vecs[i].astype(np.float32).tobytes(), None, now) for i in range(len(miss))]
        db.executemany(
            "INSERT OR REPLACE INTO embeddings(text, vector, last_used, created) VALUES(?,?,?,?)",
            rows,
        )
        db.commit()
        for i, t in enumerate(miss):
            cached[t] = new_vecs[i]
        n_api = len(miss)
    db.close()
    if n_api:
        import sys
        print(f"[embed_cached_batch] 命中{len(cached)-n_api} 新增{n_api}", file=sys.stderr)
    dim = next((v.shape[0] for v in cached.values()), 2048)
    return np.array([np.zeros(dim, dtype=np.float32) if t in empty else cached[t] for t in texts], dtype=np.float32)


DEFAULT_CAP_MB = 50  # embeddings.db 向量存储上限(MB)


def enforce_size_cap(max_mb=DEFAULT_CAP_MB):
    """向量存储上限: 超过 max_mb 按 last_used 升序删减(最久未用先删),VACUUM 回收空间。
    NULL last_used 视为最旧(从未被读取)。node_texts 映射不受影响。
    """
    import sqlite3
    db = sqlite3.connect(_get_embed_db())
    max_bytes = max_mb * 1024 * 1024
    # 防御性清理：顺带删除零范数/非有限向量（历史坏数据或并发写入异常产物）
    bad_deleted = 0
    for (text, vector) in db.execute("SELECT text, vector FROM embeddings"):
        v = np.frombuffer(vector, dtype=np.float32)
        n = np.linalg.norm(v)
        if n == 0.0 or not np.all(np.isfinite(v)):
            db.execute("DELETE FROM embeddings WHERE text=?", (text,))
            bad_deleted += 1
    if bad_deleted:
        db.commit()
    total = db.execute("SELECT COALESCE(SUM(LENGTH(vector)),0) FROM embeddings").fetchone()[0]
    if total <= max_bytes:
        if bad_deleted:
            db.execute("VACUUM")
        db.close()
        return {"capped": False, "total_mb": total / 1048576, "deleted": 0, "bad_deleted": bad_deleted}
    target = int(max_bytes * 0.9)  # 删到 90% 避免频繁触发
    deleted = 0
    # NULL last_used 优先删(从未读取的死缓存),其次按 last_used 升序
    for (text,) in db.execute(
        "SELECT text FROM embeddings ORDER BY (last_used IS NULL) DESC, last_used ASC"
    ):
        if total <= target:
            break
        vlen = db.execute("SELECT LENGTH(vector) FROM embeddings WHERE text=?", (text,)).fetchone()[0] or 0
        db.execute("DELETE FROM embeddings WHERE text=?", (text,))
        total -= vlen
        deleted += 1
    db.commit()
    db.execute("VACUUM")
    db.close()
    return {"capped": True, "total_mb": total / 1048576, "deleted": deleted, "bad_deleted": bad_deleted}


def cosine_sim(query_vec, node_vecs):
    """query vs N 节点 → 相似度数组(shape=[N])"""
    q = np.array(query_vec)
    nv = np.array(node_vecs)
    if nv.ndim == 1: nv = nv[None, :]
    qn = q / (np.linalg.norm(q) + 1e-9)
    nn = nv / (np.linalg.norm(nv, axis=1, keepdims=True) + 1e-9)
    return nn @ qn

def softmax_sample(sims, T=0.5, top_k=None, cutoff=None):
    """两阶段采样:相似度 → top-k 截断 → softmax(温度T)→ 概率 + 采样

    两阶段设计(2026-07-25 修正):直接在全图 softmax 会让高分被稀释成 ~0.001,
    低分节点反被采中。先截断再 softmax,保高分优先 + 留探索空间。

    参数:
      sims: 相似度数组(shape=[N])
      T: softmax 温度,T 高=探索(分布平),T 低=利用(偏高分)
      cutoff: 截断阈值数,只对 top cutoff 个相似节点算 softmax(默认 50;预算紧=10,松=100)
      top_k: 最终采样个数(None=只返回概率不采样)
    返回 (probs_array_full 或截断后, picked_indices_or_None, cutoff_indices)
    """
    sims = np.array(sims)
    n = len(sims)
    k_cutoff = min(cutoff or 50, n)
    # 阶段1: top-cutoff 截断(保留最相关的子集)
    cutoff_idx = np.argsort(sims)[::-1][:k_cutoff]
    sub_sims = sims[cutoff_idx]
    # 阶段2: 在子集内 softmax
    logits = sub_sims / max(T, 1e-9)
    logits = logits - logits.max()
    exp = np.exp(logits)
    sub_probs = exp / exp.sum()
    # 全图概率(截断外为 0,便于索引)
    probs_full = np.zeros(n)
    probs_full[cutoff_idx] = sub_probs
    if top_k is None:
        return probs_full, None, cutoff_idx.tolist()
    # 在 cutoff 子集内采样 top_k 个(不重复)
    picked_local = np.random.choice(k_cutoff, size=min(top_k, k_cutoff), replace=False, p=sub_probs)
    picked_global = sorted([int(cutoff_idx[i]) for i in picked_local])
    return probs_full, picked_global, cutoff_idx.tolist()

if __name__ == "__main__":
    # 自测
    import time
    t = time.time()
    names = ["张明远", "χ-MPE(矩阵乘积纠缠)", "量子多体纠缠", "2026-mpe-prl", "Pan Zhang"]
    embs = embed_batch(names)
    print(f"embed {len(names)} names: {int((time.time()-t)*1000)}ms, shape={embs.shape}")
    q = embed("张明远的合作者")
    sims = cosine_sim(q, embs)
    print("\nquery='张明远的合作者' vs 节点名:")
    for n, s in zip(names, sims):
        print(f"  {n:30} sim={s:.3f}")
    probs, picked, cutoff_idx = softmax_sample(sims, T=0.5, top_k=3, cutoff=5)
    print(f"\n两阶段(cutoff=5) softmax(T=0.5) top-3 采样:")
    print(f"  cutoff 子集: {[names[i] for i in cutoff_idx]}")
    print(f"  采中: {[f'{names[i]}={probs[i]:.3f}' for i in picked]}")


def match_by_embedding(new_texts, existing_texts, threshold=0.9):
    """通用 embedding 匹配：新文本批量对比已有文本，返回超阈值的配对。

    统一对齐框架的基础设施：keyword/proposition/people 等所有颗粒度的
    节点对齐都经此函数做 embedding 相似度匹配。

    返回 {new_text: (existing_text, cosine_score)} 仅含命中(>=threshold)的。
    """
    if not new_texts or not existing_texts:
        return {}
    new_texts = list(dict.fromkeys(new_texts))
    existing_texts = list(dict.fromkeys(existing_texts))
    all_texts = new_texts + existing_texts
    vecs = embed_cached_batch(all_texts)
    if vecs.ndim == 1:
        vecs = vecs[None, :]
    new_vecs = vecs[:len(new_texts)]
    existing_vecs = vecs[len(new_texts):]
    matches = {}
    for i, nt in enumerate(new_texts):
        sims = cosine_sim(new_vecs[i], existing_vecs)
        best_idx = int(np.argmax(sims))
        best_score = float(sims[best_idx])
        if best_score >= threshold:
            matches[nt] = (existing_texts[best_idx], best_score)
    return matches

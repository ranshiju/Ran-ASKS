"""embed_helper.py — embedding API 调用辅助(复用项目 .env)

用法:
  from embed_helper import embed, embed_batch, cosine_sim, softmax_sample
  v = embed("张明远")                    # 单条,返回 list[float]
  vs = embed_batch(["张明远","χ-MPE"])    # 批量,返回 np.array
  sims = cosine_sim(query_vec, node_vecs) # 余弦相似度
  probs, picked = softmax_sample(sims, T=0.5, top_k=10)  # 采样
"""
import hashlib
import json, sys, urllib.error, urllib.request, time
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar

import env_config

EMBED_DB = None  # embeddings 独立 db 路径(由 embed_init 设置)
_OFFLINE = ContextVar("embedding_offline", default=False)


def require_numpy():
    """Load numeric support only when needed, before provider/cache effects."""
    try:
        import numpy
    except ModuleNotFoundError as exc:
        if exc.name != "numpy":
            raise
        raise ModuleNotFoundError(
            "此操作需要 NumPy；请在仓库目录运行 uv sync --frozen --extra analysis "
            "（或 python -m pip install '.[analysis]'）。",
            name="numpy",
        ) from exc
    return numpy


class EmbeddingAPIError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@contextmanager
def offline():
    """Explicit local-only operation: no provider calls or cache mutations."""
    token = _OFFLINE.set(True)
    try:
        yield
    finally:
        _OFFLINE.reset(token)

def _require_online():
    if _OFFLINE.get():
        raise RuntimeError("embedding disabled for local-only operation")

def _get_embed_db():
    """返回 embeddings.db 的路径(独立于 graph.db,graph 重建不丢缓存)"""
    import graph_lib as gl
    scope = gl.scoped_graph_path()
    if gl.private_graph_path(scope):
        target = Path(EMBED_DB) if EMBED_DB is not None else scope.parent / "embeddings.db"
        if not target.resolve().is_relative_to(scope.parent):
            raise ValueError("private embedding cache escapes private storage")
        return target
    if EMBED_DB is not None:
        if gl.private_graph_path(EMBED_DB):
            raise ValueError("public embedding operation cannot use private cache")
        return Path(EMBED_DB)
    target = gl.REPO / "cross-domain" / "embeddings.db"
    if gl.private_graph_path(target):
        raise ValueError("public embedding operation cannot use private cache")
    return target


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
    if columns and "namespace" not in columns:
        suffix = 0
        while True:
            legacy = "embeddings_legacy_text_only" + (f"_{suffix}" if suffix else "")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
            ).fetchone()
            if not exists:
                break
            suffix += 1
        conn.execute(f'ALTER TABLE embeddings RENAME TO "{legacy}"')
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS embeddings (
        namespace TEXT NOT NULL,
        text TEXT NOT NULL,
        vector BLOB NOT NULL,
        last_used REAL,
        created REAL NOT NULL,
        PRIMARY KEY(namespace, text)
    );
    CREATE TABLE IF NOT EXISTS node_texts (
        path TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        updated REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_emb_namespace_lru
        ON embeddings(namespace, last_used);
    """)

def _load_env():
    repo = Path(__file__).resolve().parent.parent
    return env_config.load_env(repo / ".env", keys={
        "EMBED_API_BASE", "EMBED_API_PATH", "EMBED_API_KEY", "EMBED_MODEL",
        "LLM_API_BASE", "LLM_API_KEY",
    })

_ENV = _load_env()
_API_BASE = _ENV.get("EMBED_API_BASE", "") or _ENV.get("LLM_API_BASE", "")
_API_KEY = _ENV.get("EMBED_API_KEY", "") or _ENV.get("LLM_API_KEY", "")
_API_PATH = _ENV.get("EMBED_API_PATH", "") or "/v1/embeddings"
DEFAULT_MODEL = "GLM-Embedding-3"
_MODEL = _ENV.get("EMBED_MODEL", "") or DEFAULT_MODEL


def _embedding_endpoint():
    if not _API_BASE:
        raise RuntimeError("embedding API 未配置 EMBED_API_BASE/LLM_API_BASE")
    return env_config.join_api_url(_API_BASE, _API_PATH)


def _provider_error_detail(exc: Exception) -> str:
    if not isinstance(exc, urllib.error.HTTPError):
        return str(exc)
    try:
        body = exc.read(2000).decode("utf-8", errors="replace").strip()
    except Exception:
        body = ""
    return f"HTTP {exc.code}: {body}" if body else str(exc)


def _cache_namespace():
    endpoint = _embedding_endpoint() if _API_BASE else f"unconfigured:{_API_PATH}"
    identity = json.dumps(
        {"endpoint": endpoint, "model": _MODEL}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()

def embed(text, timeout=30):
    """单条文本 → embedding(list[float], dim=2048)"""
    _require_online()
    req = urllib.request.Request(
        _embedding_endpoint(),
        data=json.dumps({"model": _MODEL, "input": [text]}).encode(),
        headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=timeout)
    d = json.loads(resp.read())
    return d["data"][0]["embedding"]

def embed_batch(texts, batch_size=64, timeout=60, *, retries=1, events=None):
    """Batch embeddings, with optional per-request accounting and bounded retries."""
    _require_online()
    np = require_numpy()
    if not 1 <= batch_size <= 64 or not 0 <= retries <= 1:
        raise ValueError("embedding batch_size must be 1..64 and retries 0..1")
    all_embs = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                _embedding_endpoint(),
                data=json.dumps({"model": _MODEL, "input": batch}).encode(),
                headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
            )
            event = {"kind": "request", "batch_start": i, "texts": len(batch), "attempt": attempt + 1,
                     "input_chars": sum(map(len, batch)), "model": _MODEL, "namespace": _cache_namespace()}
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    event["http_status"] = resp.status
                    d = json.loads(resp.read())
                event["usage"] = d.get("usage")
                data = d["data"]
                if len(data) != len(batch):
                    raise ValueError("embedding response count mismatch")
                if any("index" in item for item in data):
                    if sorted(item.get("index", -1) for item in data) != list(range(len(batch))):
                        raise ValueError("embedding response index mismatch")
                    data = sorted(data, key=lambda item: item["index"])
                vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
                if vectors.ndim != 2 or not vectors.shape[1] or not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) == 0).any():
                    raise ValueError("embedding response contains invalid vectors")
                if all_embs and len(all_embs[0]) != vectors.shape[1]:
                    raise ValueError("embedding response dimension changed")
                all_embs.extend(vectors)
                event["status"] = "ok"
                break
            except Exception as exc:
                status = getattr(exc, "code", None)
                event.update(status="error", http_status=status or event.get("http_status"), error_type=type(exc).__name__)
                detail = _provider_error_detail(exc)
                # Configuration/provider rejections and malformed successes are not transport retries.
                if attempt >= retries or status in {400, 401, 403, 404, 413, 422, 429} or isinstance(exc, (ValueError, KeyError, TypeError)):
                    raise EmbeddingAPIError(f"embedding batch {i} failed: {detail}", status_code=status) from exc
                time.sleep(1)
            finally:
                event["elapsed_seconds"] = round(time.perf_counter() - started, 6)
                if events is not None:
                    events.append(event)
    return np.array(all_embs)


def embed_cached_batch(texts, cache_type="keyword", *, request_options=None, events=None):
    """带 db 缓存的批量 embed: 命中 embeddings 表复用,未命中调 embed_batch + 写回。
    向量与 path 解耦(v8.2): 统一按文本查,所有用途(keyword/seed/node title)共享去重。
    cache_type 参数保留向后兼容但不再用于查询过滤(向量只跟文本绑定)。
    返回 np.array(shape=[N, 2048]),顺序与 texts 一致。
    """
    import sqlite3
    np = require_numpy()
    if not texts:
        return np.array([])
    _require_online()
    cache_path = _get_embed_db()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cache_path)
    try:
        _ensure_cache_schema(db)
        namespace = _cache_namespace()
        uniq = list(dict.fromkeys(texts))
        cached = {}
        # 空串/纯空白不送 API(GLM embedding 对空 input 返回 400),返回时填零向量
        empty = {t for t in uniq if not t or not t.strip()}
        query_uniq = [t for t in uniq if t not in empty]
        if query_uniq:
            ph = ",".join("?" * len(query_uniq))
            for r in db.execute(
                f"SELECT text, vector FROM embeddings WHERE namespace=? AND text IN ({ph})",
                [namespace] + query_uniq,
            ):
                cached[r[0]] = np.frombuffer(r[1], dtype=np.float32)
        # touch last_used for hits (LRU 复用频率追踪)
        hits = [t for t in query_uniq if t in cached]
        if hits:
            now_touch = time.time()
            ph2 = ",".join("?" * len(hits))
            db.execute(
                f"UPDATE embeddings SET last_used=? WHERE namespace=? AND text IN ({ph2})",
                [now_touch, namespace] + hits,
            )
            db.commit()
        miss = [t for t in query_uniq if t not in cached]
        if events is not None:
            events.append({"kind": "cache", "requested": len(texts), "unique": len(query_uniq),
                           "hits": len(hits), "misses": len(miss), "namespace": namespace})
        n_api = 0
        if miss:
            options = dict(request_options or {})
            if events is not None:
                options["events"] = events
            new_vecs = embed_batch(miss, **options) if options else embed_batch(miss)
            # 校验范数，零范数/非有限向量不写入缓存（防止坏数据污染聚类）
            norms = np.linalg.norm(new_vecs, axis=1)
            bad = [miss[i] for i, n in enumerate(norms) if n == 0.0 or not np.isfinite(n)]
            if bad:
                raise RuntimeError(f"embed 返回零范数/非有限向量，拒绝写入缓存: {bad}")
            now = time.time()
            rows = [
                (namespace, miss[i], new_vecs[i].astype(np.float32).tobytes(), None, now)
                for i in range(len(miss))
            ]
            db.executemany(
                "INSERT OR REPLACE INTO embeddings(namespace, text, vector, last_used, created) VALUES(?,?,?,?,?)",
                rows,
            )
            db.commit()
            for i, t in enumerate(miss):
                cached[t] = new_vecs[i]
            n_api = len(miss)
    finally:
        db.close()
    if n_api:
        import sys
        print(f"[embed_cached_batch] 命中{len(cached)-n_api} 新增{n_api}", file=sys.stderr)
    dim = next((v.shape[0] for v in cached.values()), 2048)
    return np.array([np.zeros(dim, dtype=np.float32) if t in empty else cached[t] for t in texts], dtype=np.float32)


DEFAULT_CAP_MB = 50  # embeddings.db 向量存储上限(MB)


def invalidate_cached(texts):
    """Delete current endpoint/model cache rows for the supplied texts."""
    import sqlite3
    unique = list(dict.fromkeys(texts))
    if not unique:
        return 0
    db = sqlite3.connect(_get_embed_db())
    _ensure_cache_schema(db)
    placeholders = ",".join("?" * len(unique))
    cursor = db.execute(
        f"DELETE FROM embeddings WHERE namespace=? AND text IN ({placeholders})",
        [_cache_namespace()] + unique,
    )
    db.commit()
    deleted = cursor.rowcount
    db.close()
    return deleted


def enforce_size_cap(max_mb=DEFAULT_CAP_MB):
    """向量存储上限: 超过 max_mb 按 last_used 升序删减(最久未用先删),VACUUM 回收空间。
    NULL last_used 视为最旧(从未被读取)。node_texts 映射不受影响。
    """
    import sqlite3
    np = require_numpy()
    db = sqlite3.connect(_get_embed_db())
    _ensure_cache_schema(db)
    max_bytes = max_mb * 1024 * 1024
    # 防御性清理：顺带删除零范数/非有限向量（历史坏数据或并发写入异常产物）
    bad_deleted = 0
    for namespace, text, vector in db.execute(
        "SELECT namespace, text, vector FROM embeddings"
    ):
        v = np.frombuffer(vector, dtype=np.float32)
        n = np.linalg.norm(v)
        if n == 0.0 or not np.all(np.isfinite(v)):
            db.execute(
                "DELETE FROM embeddings WHERE namespace=? AND text=?", (namespace, text)
            )
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
    for namespace, text in db.execute(
        "SELECT namespace,text FROM embeddings ORDER BY (last_used IS NULL) DESC, last_used ASC"
    ):
        if total <= target:
            break
        vlen = db.execute(
            "SELECT LENGTH(vector) FROM embeddings WHERE namespace=? AND text=?",
            (namespace, text),
        ).fetchone()[0] or 0
        db.execute(
            "DELETE FROM embeddings WHERE namespace=? AND text=?", (namespace, text)
        )
        total -= vlen
        deleted += 1
    db.commit()
    db.execute("VACUUM")
    db.close()
    return {"capped": True, "total_mb": total / 1048576, "deleted": deleted, "bad_deleted": bad_deleted}


def cosine_sim(query_vec, node_vecs):
    """query vs N 节点 → 相似度数组(shape=[N])"""
    np = require_numpy()
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
    np = require_numpy()
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


def match_by_embedding(new_texts, existing_texts, threshold=0.9, *, extra_texts=()):
    """通用 embedding 匹配：新文本批量对比已有文本，返回超阈值的配对。

    统一对齐框架的基础设施：keyword/proposition/people 等所有颗粒度的
    节点对齐都经此函数做 embedding 相似度匹配。

    返回 {new_text: (existing_text, cosine_score)} 仅含命中(>=threshold)的。
    extra_texts 仅预热 caller 后续必需的文本，不参与当前候选或评分；失败回退原请求，401/403/429 保留失败。
    """
    if not new_texts or not existing_texts:
        return {}
    np = require_numpy()
    new_texts = list(dict.fromkeys(new_texts))
    existing_texts = list(dict.fromkeys(existing_texts))
    all_texts = new_texts + existing_texts
    required = set(all_texts)
    additional = [text for text in dict.fromkeys(extra_texts) if text not in required]
    if additional:
        try:
            vecs = embed_cached_batch(all_texts + additional)[:len(all_texts)]
        except Exception as error:
            if isinstance(error, EmbeddingAPIError) and error.status_code in {401, 403, 429}:
                raise
            vecs = embed_cached_batch(all_texts)
    else:
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

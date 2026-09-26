"""embed_init.py — 文本向量缓存初始化 + 增量同步(解耦版)

向量与 path 解耦，并按 endpoint/model 隔离:
  - embeddings 表: namespace + 文本 → 向量(纯缓存,同配置内去重)
  - node_texts 表: path → 文本(title) 映射(标注 graph 节点,随图增删)
  - 同一文本只存一份向量: keyword / arxiv-direction seed / node title 命中即复用

旧结构自动保留为 legacy 备份；因 provider/model 身份未知，不复用旧向量。
"""
import sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from embed_helper import _ensure_cache_schema, enforce_size_cap

DB = Path(__file__).resolve().parent.parent / "cross-domain" / "embeddings.db"
GRAPH_DB = Path(__file__).resolve().parent.parent / "cross-domain" / "graph.db"


def _has_legacy_type(conn):
    """检测 embeddings 表是否为旧结构(含 type 列)。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)").fetchall()]
    return "type" in cols


def init_table(conn):
    """Create the endpoint/model-namespaced cache schema idempotently."""
    _ensure_cache_schema(conn)


def migrate_from_legacy(conn, gconn):
    """Quarantine typed legacy vectors whose provider/model identity is unknown."""
    if not _has_legacy_type(conn):
        return None
    print("=== 检测到旧结构(type 列),隔离未知配置向量 ===")
    suffix = 0
    while True:
        legacy = "embeddings_legacy_typed" + (f"_{suffix}" if suffix else "")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (legacy,)
        ).fetchone()
        if not exists:
            break
        suffix += 1
    conn.execute(f'ALTER TABLE embeddings RENAME TO "{legacy}"')
    _ensure_cache_schema(conn)
    path_title = {r[0]: (r[1] or r[0]) for r in gconn.execute("SELECT path, title FROM nodes").fetchall()}
    now = time.time()
    conn.executemany(
        "INSERT OR REPLACE INTO node_texts(path, text, updated) VALUES(?,?,?)",
        [(p, t, now) for p, t in path_title.items()])
    conn.commit()
    nt = conn.execute("SELECT COUNT(*) FROM node_texts").fetchone()[0]
    print(f"  旧向量保留为 {legacy}；当前 namespace 从空缓存开始，node_texts {nt} 条")
    return 0


def get_status(conn, gconn):
    n_vec = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    n_nt = conn.execute("SELECT COUNT(*) FROM node_texts").fetchone()[0]
    total_nodes = gconn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    return n_vec, n_nt, total_nodes


def sync_node_embeddings(conn, gconn, rebuild=False):
    """同步 node_texts(path→text) 映射。
    v8.2: 不预计算 node 向量(无消费者,死缓存)。向量由 embed_cached_batch 按需懒算,
    避免 embed_init 反复生成与 size_cap 互相 churn。
    """
    if rebuild:
        conn.execute("DELETE FROM node_texts")
        print("  清空 node_texts(rebuild)")
    cur_nodes = {r[0]: (r[1] or r[0]) for r in gconn.execute("SELECT path, title FROM nodes").fetchall()}
    now = time.time()
    cached_paths = dict(conn.execute("SELECT path, text FROM node_texts").fetchall())
    to_add, to_update = [], []
    for path, text in cur_nodes.items():
        if path not in cached_paths:
            to_add.append((path, text))
        elif cached_paths[path] != text:
            to_update.append((path, text))
    to_delete = [p for p in cached_paths if p not in cur_nodes]
    if to_delete:
        conn.executemany("DELETE FROM node_texts WHERE path=?", [(p,) for p in to_delete])
        print(f"  清理失效 node_texts: {len(to_delete)}")
    for path, text in to_add + to_update:
        conn.execute("INSERT OR REPLACE INTO node_texts(path, text, updated) VALUES(?,?,?)", (path, text, now))
    conn.commit()
    if not (to_add or to_update or to_delete):
        print("  无变更,node_texts 已是最新")
    else:
        print(f"  node_texts: +{len(to_add)} ~{len(to_update)} -{len(to_delete)}")
    return len(to_add) + len(to_update), 0


def main():
    args = sys.argv[1:]
    status_only = "--status" in args
    rebuild = "--rebuild" in args
    if not GRAPH_DB.exists():
        print(f"ERR: {GRAPH_DB} 不存在,先跑 graph_build.py --build --apply")
        return
    conn = sqlite3.connect(DB)
    gconn = sqlite3.connect(GRAPH_DB)
    migrate_from_legacy(conn, gconn)
    init_table(conn)
    n_vec, n_nt, total = get_status(conn, gconn)
    print("=== embeddings 缓存状态(解耦版) ===")
    print(f"  向量总数: {n_vec}(文本去重)")
    print(f"  node_texts(path→text): {n_nt}/{total} 节点")
    if status_only:
        return
    print("\n=== 同步 node 向量 ===")
    sync_node_embeddings(conn, gconn, rebuild=rebuild)
    n_vec, n_nt, total = get_status(conn, gconn)
    print(f"\n完成: 向量 {n_vec}, node_texts {n_nt}/{total}")
    cap = enforce_size_cap()
    print(f"存储上限: {cap['total_mb']:.1f}MB ({'触发删减 '+str(cap['deleted'])+' 条' if cap['capped'] else '未超限'})")


if __name__ == "__main__":
    main()

"""embed_init.py — 文本向量缓存初始化 + 增量同步(解耦版)

向量与 path 解耦(v8.2, 2026-07-28):
  - embeddings 表: 文本 → 向量(纯缓存,去重,所有用途共享)
  - node_texts 表: path → 文本(title) 映射(标注 graph 节点,随图增删)
  - 同一文本只存一份向量: keyword / arxiv-direction seed / node title 命中即复用

旧结构(含 type 列)自动迁移,旧表保留为 embeddings_legacy。
"""
import sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from embed_helper import enforce_size_cap

DB = Path(__file__).resolve().parent.parent / "cross-domain" / "embeddings.db"
GRAPH_DB = Path(__file__).resolve().parent.parent / "cross-domain" / "graph.db"


def _has_legacy_type(conn):
    """检测 embeddings 表是否为旧结构(含 type 列)。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)").fetchall()]
    return "type" in cols


def init_table(conn):
    """建解耦表结构(幂等): embeddings(文本→向量) + node_texts(path→文本)"""
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


def migrate_from_legacy(conn, gconn):
    """从旧结构(含 type 列)迁移到解耦结构。幂等:已迁移则跳过。返回迁移向量数。"""
    if not _has_legacy_type(conn):
        return None  # 已是新结构
    print("=== 检测到旧结构(type 列),开始迁移 ===")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS embeddings_new (
        text TEXT PRIMARY KEY, vector BLOB NOT NULL,
        last_used REAL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS node_texts (
        path TEXT PRIMARY KEY, text TEXT NOT NULL, updated REAL NOT NULL);
    """)
    # keyword / arxiv-direction / query: node_id 即文本,直接转
    conn.execute("""
        INSERT OR IGNORE INTO embeddings_new(text, vector, last_used, created)
        SELECT node_id, vector, last_used, created FROM embeddings
        WHERE type IN ('keyword','arxiv-direction','query') AND node_id IS NOT NULL
    """)
    # node: node_id=path, 需补 text=title(从 graph.db 查)
    path_title = {r[0]: (r[1] or r[0]) for r in gconn.execute("SELECT path, title FROM nodes").fetchall()}
    now = time.time()
    for path, vec, created in conn.execute(
        "SELECT node_id, vector, created FROM embeddings WHERE type='node'"
    ).fetchall():
        text = path_title.get(path, path)
        conn.execute(
            "INSERT OR IGNORE INTO embeddings_new(text, vector, last_used, created) VALUES(?,?,?,?)",
            (text, vec, None, created))
    # 建 node_texts: 全图节点 path→title
    conn.executemany(
        "INSERT OR REPLACE INTO node_texts(path, text, updated) VALUES(?,?,?)",
        [(p, t, now) for p, t in path_title.items()])
    # 交换表名
    conn.execute("ALTER TABLE embeddings RENAME TO embeddings_legacy")
    conn.execute("ALTER TABLE embeddings_new RENAME TO embeddings")
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    nt = conn.execute("SELECT COUNT(*) FROM node_texts").fetchone()[0]
    print(f"  迁移完成: 向量 {total} 条(去重), node_texts {nt} 条, 旧表保留为 embeddings_legacy")
    return total


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

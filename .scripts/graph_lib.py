#!/usr/bin/env python3
"""graph_lib.py — 图操作共享库(主数据化 v4,2026-07-25)

架构切换:graph.db 是边的唯一源(不再从 md Core Triples 段派生)。
  - md 存节点属性(wiki 内容 + frontmatter)
  - graph.db 存语义知识边和 aliases
本库提供:连接、schema、节点收集、resolve 索引、去重、alias 抽取等可复用纯函数。

被 graph_ingest.py(增量建边)、graph_dump.py(快照)、query_graph.py(查询)复用。
"""
import json
import os
import re
import sqlite3
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUBPROJECTS = ["academic", "admin", "teaching", "business"]  # 主聚合清单;private 物理隔离,不纳入
PRIVATE_DIR = REPO / "private"
HUB_DIR = REPO / "cross-domain" / "topics"
GRAPH_DB = REPO / "cross-domain" / "graph.db"
PRIVATE_GRAPH_DB = PRIVATE_DIR / "graph.db"

# 域 → 所属 graph.db。主库四域统一用 cross-domain/graph.db(聚合);
# private 物理隔离,独立 graph.db,不进主聚合。
DOMAIN_GRAPH_DB = {
    "academic": GRAPH_DB,
    "admin": GRAPH_DB,
    "teaching": GRAPH_DB,
    "business": GRAPH_DB,
    "private": PRIVATE_GRAPH_DB,
}


def graph_db_for(page_path):
    """按 wiki 页面路径所属域返回应操作的 graph.db。

    private/* 路径 → private/graph.db(物理隔离);
    其余 → cross-domain/graph.db(主库聚合)。
    """
    if isinstance(page_path, str) and page_path.startswith("private/"):
        return PRIVATE_GRAPH_DB
    return GRAPH_DB


def domain_of_path(page_path):
    """从 wiki 路径首段提取域前缀(含 private)。无匹配返回 None。"""
    if not isinstance(page_path, str) or not page_path:
        return None
    first = page_path.split("/", 1)[0]
    if first in DOMAIN_GRAPH_DB:
        return first
    return None

CONFIDENCE_VALUES = {"可追溯", "推断", "存疑"}
DEFAULT_CONFIDENCE = "可追溯"

# 管道版本号：影响 wiki/图边输出的建设变更才 bump。
# 纯改名/重构不 bump；skeleton 模板/建边逻辑/prompt 调整等影响已入库内容的 bump。
# re_ingest --outdated 据此判断哪些论文需重新摄入。
CURRENT_PIPELINE_VERSION = 9  # v9: bounded sparse retry + node-origin lineage

RAW_DOCUMENT_SUFFIXES = {
    ".md", ".txt", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".csv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm",
    ".tex", ".epub", ".xls", ".xlsx",
}


def raw_file_path(source, page_path=""):
    """Return a domain-qualified repository path for a Raw source spelling."""
    value = str(source or "").split("#", 1)[0].strip()
    if not value or value.startswith(("http://", "https://", "synology://")):
        return ""
    domain = domain_of_path(str(page_path or ""))
    if domain and value.startswith(("raw/", "wiki/")):
        value = f"{domain}/{value}"
    return Path(value).as_posix()


def raw_node_path(source, page_path=""):
    """Map an original file and same-stem companion to one domain-qualified Raw node."""
    value = raw_file_path(source, page_path)
    if not value:
        return ""
    path = Path(value)
    if path.suffix.lower() in RAW_DOCUMENT_SUFFIXES:
        path = path.with_suffix("")
    return path.as_posix()


def raw_node_title(raw_path):
    path = Path(str(raw_path or ""))
    if path.name.casefold() in {"paper", "document", "doc"} and path.parent.name:
        return path.parent.name
    return path.name or str(raw_path or "")

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# 双向冗余消除:逆谓词对。A--p-->B 与 B--q-->A 互逆时只保留高优先方向。
INVERSE_PAIRS = [
    ("指导", "师从"),
    ("指导", "受指导于"),
    ("合作者", "合作者"),
]
PRIORITY = {"指导": 3, "受指导于": 2, "师从": 1, "合作者": 0}


def _ensure_aliases_many_to_many(conn):
    """把历史 alias 单主键迁移为 ``(alias,node_path)`` 复合主键。

    同一表述可以合法指向多个节点；唯一命中才可确定性解析，多命中交由上下文
    消歧。历史空 node_path 本来会被 ingest 清理，迁移时不保留。
    """
    info = conn.execute("PRAGMA table_info(aliases)").fetchall()
    if not info:
        return
    pk = {row[1]: row[5] for row in info}
    if pk.get("alias") == 1 and pk.get("node_path") == 2:
        return
    try:
        conn.executescript("""
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS aliases_many_to_many;
            CREATE TABLE aliases_many_to_many (
                alias TEXT NOT NULL,
                node_path TEXT NOT NULL,
                PRIMARY KEY (alias, node_path)
            );
            INSERT OR IGNORE INTO aliases_many_to_many(alias,node_path)
            SELECT alias,node_path FROM aliases
            WHERE COALESCE(alias,'') != '' AND COALESCE(node_path,'') != '';
            DROP TABLE aliases;
            ALTER TABLE aliases_many_to_many RENAME TO aliases;
            CREATE INDEX IF NOT EXISTS idx_aliases_node ON aliases(node_path);
            COMMIT;
        """)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def connect(db_path=None):
    conn = sqlite3.connect(db_path or GRAPH_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    # 加性迁移：ingest_version（管道版本戳，re_ingest 判断是否需重新摄入）
    if columns and "ingest_version" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN ingest_version INTEGER DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_ingest_version ON nodes(ingest_version)")
        conn.commit()
    if columns and "entity_subtype" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN entity_subtype TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_entity_subtype ON nodes(entity_subtype)")
        conn.commit()
    if columns and "description" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN description TEXT DEFAULT ''")
        conn.commit()
    _ensure_aliases_many_to_many(conn)
    has_edges = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='edges'"
    ).fetchone()
    # ADR-003: edges.score 列(相似边的余弦分数,加性迁移,现有行自动 NULL)
    if has_edges:
        edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        if edge_cols and "score" not in edge_cols:
            conn.execute("ALTER TABLE edges ADD COLUMN score REAL")
            conn.commit()
    if has_edges:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS edge_evidence (
                edge_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                evidence_quote TEXT DEFAULT '',
                is_sr INTEGER DEFAULT 0,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                superseded_by TEXT,
                PRIMARY KEY (edge_id, source),
                FOREIGN KEY (edge_id) REFERENCES edges(id) ON DELETE CASCADE
            )
        """)
        evidence_cols = {row[1] for row in conn.execute("PRAGMA table_info(edge_evidence)")}
        if evidence_cols and "recorded_at" not in evidence_cols:
            conn.execute("ALTER TABLE edge_evidence ADD COLUMN recorded_at TEXT")
        if evidence_cols and "superseded_by" not in evidence_cols:
            conn.execute("ALTER TABLE edge_evidence ADD COLUMN superseded_by TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_evidence_source ON edge_evidence(source)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS edge_origins (
                edge_id INTEGER NOT NULL,
                origin_page TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (edge_id, origin_page, source),
                FOREIGN KEY (edge_id) REFERENCES edges(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_origins_page ON edge_origins(origin_page)")
        conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS managed_nodes (
            node_path TEXT PRIMARY KEY,
            created_origin_page TEXT NOT NULL,
            recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (node_path) REFERENCES nodes(path) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_origins (
            node_path TEXT NOT NULL,
            origin_page TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (node_path, origin_page, source),
            FOREIGN KEY (node_path) REFERENCES nodes(path) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_managed_nodes_origin ON managed_nodes(created_origin_page)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_node_origins_page ON node_origins(origin_page)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS temporal_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            valid_from TEXT,
            valid_until TEXT,
            superseded_by INTEGER,
            source TEXT,
            is_sr INTEGER DEFAULT 0,
            recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_temporal_facts_subject ON temporal_facts(subject)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_temporal_facts_object ON temporal_facts(object)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_temporal_facts_valid ON temporal_facts(valid_from, valid_until)")
    conn.commit()
    return conn


def init_schema(conn):
    """幂等建表和受控加性迁移。graph.db 主数据化 v4 schema。

    对已初始化或含数据的库重复调用是安全的：不删除 nodes/edges 数据；历史
    aliases 单主键表会原子改建为多对多复合主键并原样迁移有效映射。
    需要清空重建时,删除 graph.db 文件后重新 init,或走独立的 reset 流程。"""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (
        path TEXT PRIMARY KEY,
        title TEXT,
        type TEXT,             -- page / entity / hub / timeline-summary (hub 含 topic/venue/institution)
        entity_subtype TEXT,   -- entity 子类型: person / citation-only / venue / institution 等
        source_type TEXT,
        date TEXT,
       status TEXT,
        has_raw_source INTEGER, -- 1=页面 sources 字段非空,直连 raw
        ingest_version INTEGER DEFAULT 0, -- 管道版本戳（re_ingest 判断是否需重新摄入）
        description TEXT DEFAULT '' -- 可选导航性消歧说明；不是事实证据
    );

    CREATE TABLE IF NOT EXISTS aliases (
        alias TEXT NOT NULL,
        node_path TEXT NOT NULL,
        PRIMARY KEY (alias, node_path)    -- 同一表述可指向多个节点，由上下文消歧
    );
    CREATE INDEX IF NOT EXISTS idx_aliases_node ON aliases(node_path);

    CREATE TABLE IF NOT EXISTS edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
       subject TEXT NOT NULL,
       predicate TEXT NOT NULL,  -- 自由字符串语义谓词
       object TEXT NOT NULL,
       confidence TEXT,          -- 可追溯/推断/存疑
       source TEXT,              -- 可选 locator；历史行可能存旧 source
       is_sr INTEGER DEFAULT 0,  -- 来自语音识别来源标 [SR]
       score REAL,               -- ADR-003: 相似边的余弦分数;知识边为 NULL
       FOREIGN KEY (subject) REFERENCES nodes(path),
        FOREIGN KEY (object) REFERENCES nodes(path)
    );

    CREATE TABLE IF NOT EXISTS edge_evidence (
        edge_id INTEGER NOT NULL,
        source TEXT NOT NULL,
        evidence_quote TEXT DEFAULT '',
        is_sr INTEGER DEFAULT 0,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        superseded_by TEXT,
        PRIMARY KEY (edge_id, source),
        FOREIGN KEY (edge_id) REFERENCES edges(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS edge_origins (
        edge_id INTEGER NOT NULL,
        origin_page TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (edge_id, origin_page, source),
        FOREIGN KEY (edge_id) REFERENCES edges(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS managed_nodes (
        node_path TEXT PRIMARY KEY,
        created_origin_page TEXT NOT NULL,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (node_path) REFERENCES nodes(path) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS node_origins (
        node_path TEXT NOT NULL,
        origin_page TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (node_path, origin_page, source),
        FOREIGN KEY (node_path) REFERENCES nodes(path) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS temporal_facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        valid_from TEXT,
        valid_until TEXT,
        superseded_by INTEGER,
        source TEXT,
        is_sr INTEGER DEFAULT 0,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_edges_subject ON edges(subject);
    CREATE INDEX IF NOT EXISTS idx_edges_object ON edges(object);
    CREATE INDEX IF NOT EXISTS idx_edges_predicate ON edges(predicate);
    CREATE INDEX IF NOT EXISTS idx_edge_evidence_source ON edge_evidence(source);
    CREATE INDEX IF NOT EXISTS idx_edge_origins_page ON edge_origins(origin_page);
    CREATE INDEX IF NOT EXISTS idx_managed_nodes_origin ON managed_nodes(created_origin_page);
    CREATE INDEX IF NOT EXISTS idx_node_origins_page ON node_origins(origin_page);
    CREATE INDEX IF NOT EXISTS idx_temporal_facts_subject ON temporal_facts(subject);
    CREATE INDEX IF NOT EXISTS idx_temporal_facts_object ON temporal_facts(object);
    CREATE INDEX IF NOT EXISTS idx_temporal_facts_valid ON temporal_facts(valid_from, valid_until);
    CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
    CREATE INDEX IF NOT EXISTS idx_nodes_entity_subtype ON nodes(entity_subtype);
    CREATE INDEX IF NOT EXISTS idx_nodes_has_raw ON nodes(has_raw_source);
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)")}
    if "description" not in columns:
        conn.execute("ALTER TABLE nodes ADD COLUMN description TEXT DEFAULT ''")
    _ensure_aliases_many_to_many(conn)


def add_edge_evidence(conn, edge_id, source, evidence_quote="", is_sr=False):
    """追加一条可追溯证据，不复制同一语义边。"""
    if not source:
        return
    conn.execute(
        "INSERT OR IGNORE INTO edge_evidence (edge_id, source, evidence_quote, is_sr, recorded_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (edge_id, source, evidence_quote or "", 1 if is_sr else 0),
    )


def add_edge_origin(conn, edge_id, origin_page, source=""):
    """记录某页面摄入对语义边的贡献，供 re-ingest 精确撤销。

    origin 只是派生 lineage，不是事实证据；同一语义边可由多个页面共同贡献。
    """
    if not origin_page:
        return
    conn.execute(
        "INSERT OR IGNORE INTO edge_origins (edge_id, origin_page, source, recorded_at) "
        "VALUES (?,?,?,datetime('now'))",
        (edge_id, origin_page, source or ""),
    )


def add_node_origin(conn, node_path, origin_page, source="", managed=False):
    """记录页面对 entity 节点的使用；仅本次新建节点可标为 managed。"""
    if not node_path or not origin_page:
        return
    if managed:
        conn.execute(
            "INSERT OR IGNORE INTO managed_nodes "
            "(node_path, created_origin_page, recorded_at) VALUES (?,?,datetime('now'))",
            (node_path, origin_page),
        )
    conn.execute(
        "INSERT OR IGNORE INTO node_origins "
        "(node_path, origin_page, source, recorded_at) VALUES (?,?,?,datetime('now'))",
        (node_path, origin_page, source or ""),
    )


def base_predicate(predicate):
    """取谓词前缀(去掉括号描述),用于逆谓词匹配。"""
    return re.split(r"[（(]", predicate, maxsplit=1)[0].strip()


def read_frontmatter(page_path):
    """读 wiki 页 frontmatter。page_path 可为 Path 或相对路径(无 .md)。"""
    p = Path(page_path)
    if not p.is_absolute():
        p = REPO / (str(page_path).removesuffix(".md") + ".md")
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    try:
        import yaml
        return yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}


def parse_list_field(fm, key):
    val = fm.get(key)
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [x.strip() for x in re.split(r"[,，]", val) if x.strip()]
    return []


def has_raw_source(fm):
    src = fm.get("sources")
    if src is None:
        return False
    if isinstance(src, list):
        return len(src) > 0
    if isinstance(src, str):
        return bool(src.strip())
    return False


MANAGE_FILES = {"log.md", "index.md"}
MANAGE_DIRS = {"outputs"}


def is_manage_file(p):
    if p.name in MANAGE_FILES:
        return True
    if any(part in MANAGE_DIRS for part in p.parts):
        return True
    return False


def collect_pages(domains=None):
    """收集 wiki 知识内容页(相对仓库根路径,无扩展名)。

    domains=None 默认用 SUBPROJECTS(主库聚合,private 不在内,保持隔离);
    传入 ["private"] 仅收集 private 域。
    """
    pages = []
    for sub in (domains if domains is not None else SUBPROJECTS):
        sub_root = REPO / sub / "wiki"
        if not sub_root.exists():
            continue
        for p in sub_root.rglob("*.md"):
            if is_manage_file(p):
                continue
            s = str(p.relative_to(REPO))[:-3].replace(os.sep, "/")
            pages.append(s)
    return pages


def collect_hubs():
    """收集 Topic Hub 页。返回 [(path, Path, fm)]。"""
    hubs = []
    if not HUB_DIR.exists():
        return hubs
    for p in HUB_DIR.glob("*.md"):
        if p.name == "_index.md":
            continue
        fm = read_frontmatter(p)
        if fm.get("type") == "topic-hub":
            s = str(p.relative_to(REPO))[:-3].replace(os.sep, "/")
            hubs.append((s, p, fm))
    return hubs


def collect_timeline_summaries(domains=None):
    results = []
    for sub in (domains if domains is not None else SUBPROJECTS):
        sub_root = REPO / sub / "wiki"
        if not sub_root.exists():
            continue
        for p in sub_root.rglob("*.md"):
            if is_manage_file(p):
                continue
            fm = read_frontmatter(p)
            if fm.get("type") == "timeline-summary":
                s = str(p.relative_to(REPO))[:-3].replace(os.sep, "/")
                results.append((s, p, fm))
    return results


def resolve_wikilink_target(hub_path, target):
    """Hub 正文相对 wikilink → 相对仓库根的页面路径。"""
    hub_dir = Path(hub_path).parent
    resolved = (hub_dir / target).resolve()
    try:
        rel = resolved.relative_to(REPO.resolve())
    except ValueError:
        return None
    s = str(rel)
    if s.endswith(".md"):
        s = s[:-3]
    return s.replace(os.sep, "/")


def build_name_index(conn):
    """从 graph.db 构建 resolve 索引(主数据化后 alias/title 在图里)。
    返回 (title_idx, alias_idx, suffix_idx):
      title_idx: {title: [path]}
      alias_idx: {alias: [path]}
      suffix_idx: {path_suffix: [path]}(路径最后一段)

    proposition 节点（entity_subtype='proposition'）是描述性命题，非概念端点，
    不应作为 resolve 命中目标（避免概念全名误匹配到命题全名）。故 suffix_idx 排除之；
    title_idx 仍收录（精确名匹配合法，用于命题去重）。
    """
    title_idx = {}
    alias_idx = {}
    suffix_idx = {}
    for r in conn.execute("SELECT path, title, entity_subtype FROM nodes"):
        path = r["path"]
        title = (r["title"] or "").strip()
        if title:
            title_idx.setdefault(title, []).append(path)
        # proposition 不进 suffix_idx（描述性命题，非概念端点，避免 suffix-prefix 误匹配）
        if r["entity_subtype"] == "proposition":
            continue
        suffix = path.rsplit("/", 1)[-1] if "/" in path else path
        suffix_idx.setdefault(suffix, []).append(path)
    # proposition 节点是描述性命题,非概念端点;其 alias(含 decompose 拆出的
    # 句内片段)不进 resolve 索引,避免与概念 alias 冲突造成歧义(同 suffix_idx)
    for r in conn.execute(
        "SELECT a.alias, a.node_path FROM aliases a "
        "JOIN nodes n ON a.node_path = n.path "
        "WHERE COALESCE(n.entity_subtype, '') != 'proposition'"
    ):
        if not is_resolvable_alias(r["alias"]):
            continue
        alias_idx.setdefault(r["alias"], []).append(r["node_path"])
    return title_idx, alias_idx, suffix_idx


def resolve_bare_name(name, title_idx, alias_idx, suffix_idx):
    """裸名 → page path。规则:title→alias→suffix→suffix-prefix→归一化匹配。
    返回 (resolved_path | None, ambiguity_flag)。
    """
    if not name:
        return None, False
    name = name.strip()
    # 1-3. 精确匹配
    for idx in (title_idx, alias_idx, suffix_idx):
        if name in idx:
            paths = idx[name]
            if len(paths) == 1:
                return paths[0], False
            return None, True
    # 4. suffix-prefix:裸名是某 path 后缀的前缀(如 2026-fdz-kagome → 2026-fdz-kagome-magnetic-platform)
    matches = [p for suf, ps in suffix_idx.items() for p in ps if suf.startswith(name) and suf != name]
    if len(matches) == 1:
        return matches[0], False
    if len(matches) > 1:
        return None, True
    # 5. 归一化匹配:提取名称的缩写/中文/英文做比对，解决同一概念不同写法碎片化
    resolved = _resolve_normalized(name, title_idx, alias_idx, suffix_idx)
    if resolved is not None:
        return resolved, False
    return None, False


def _normalize_name_for_match(name):
    """归一化名称为可比形式:提取缩写(括号内大写字母) + 去标点空格。
    如「矩阵乘积态matrix product state(MPS)」→(缩写MPS, 去标点矩阵乘积态matrixproductstate)"""
    import re
    s = name.strip()
    # 提取缩写:括号内 ≥2 连续大写字母
    abbr = None
    m = re.search(r'[\(（]([A-Z]{2,}[A-Za-z0-9]*)[\)）]', s)
    if m:
        abbr = m.group(1)
    else:
        # 无括号缩写但本身是缩写(如 MPS / PEPS)
        if re.fullmatch(r'[A-Z]{2,}[A-Za-z0-9]*', s):
            abbr = s
    # 去括号内容 + 标点 + 空格，得到纯中英文拼接
    stripped = re.sub(r'[\(（][^\)）]*[\)）]', '', s)
    stripped = re.sub(r'[\s\-_/\·]', '', stripped)
    return abbr, stripped.lower() if stripped else None


_NON_IDENTITY_ALIASES = frozenset({
    "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or",
    "the", "to", "with",
})


def is_resolvable_alias(alias):
    """Whether an alias carries enough identity information for exact reuse."""
    value = str(alias or "").strip().casefold()
    return bool(value) and value not in _NON_IDENTITY_ALIASES


def decompose_name_to_aliases(name):
    """把拼接概念名（如「矩阵乘积态matrix product state(MPS)」）拆为多形态 alias。
    返回 [缩写, 中文, 英文全称] 去重列表（不含 name 本身）。
    使 resolver 输入任一形态（缩写/英文全称/中文）都能命中同一节点。
    """
    s = name.strip()
    out = []
    # 缩写：括号内 ≥2 连续大写字母
    m = re.search(r'[(（]([A-Z]{2,}[A-Za-z0-9\-]*)[)）]', s)
    if m:
        out.append(m.group(1))
    # 去括号内容后拆中文/英文
    no_paren = re.sub(r'[(（][^)）]*[)）]', '', s).strip()
    chinese_match = re.findall(r'[\u4e00-\u9fff]+', no_paren)
    if chinese_match:
        out.append(''.join(chinese_match))
    # 只有双语拼接名或显式缩写才拆英文全称。纯英文长标题/会议名中的年份会
    # 截断旧正则并产生 ``the`` 一类伪 alias，进而把不同实体错误归并。
    if chinese_match or m:
        en = re.sub(r'[\u4e00-\u9fff]+', '', no_paren)
        en = re.sub(r'^[\s:：/\-]+|[\s:：/\-]+$', '', en)
        if re.search(r'[A-Za-z]{3,}', en):
            out.append(en)
    # 去重保序，排除 name 本身
    seen = set()
    result = []
    for x in out:
        if is_resolvable_alias(x) and x != name and x not in seen:
            seen.add(x)
            result.append(x)
    return result


def extract_keyword_id(full_name):
    """从完整格式名提取最短形式作为 keyword ID。
    从缩写(括号内)、中文、英文中选最短；≤15 字直接用原名。
    """
    s = full_name.strip()
    if len(s) <= 15:
        return s
    # 复用 alias 拆解规则：纯英文长标题若无显式缩写，不再被年份截成首词。
    candidates = [s, *decompose_name_to_aliases(s)]
    return min(candidates, key=len)


def extract_descriptive_id(text, concept_map, abbr_map=None):
    """把命题文本中已抽取的概念名替换为其 keyword ID,得到 proposition 节点 path。

    concept_map: {概念全名: keyword_id}(来自 LLM 抽取的主宾概念)。
    按全名长度降序替换,避免短名误吃长名子串;全名与 ID 相同则跳过。
    未匹配的概念保持原文。纯字符串函数(无 DB 依赖),用于子图构建期。

    例: text="证明ANTN表达能力超越矩阵乘积态matrix product state(MPS)",
        concept_map={"矩阵乘积态matrix product state(MPS)":"MPS","ANTN":"ANTN"}
        -> "证明ANTN表达能力超越MPS"
    """
    if not text:
        return text
    pairs = []
    for full_name, kid in (concept_map or {}).items():
        if full_name and full_name != kid:
            pairs.append((full_name, kid))
    concept_kids = set((concept_map or {}).values())
    for abbr, node_path in (abbr_map or {}).items():
        if abbr and abbr != node_path and abbr not in concept_kids:
            pairs.append((abbr, node_path))
    if not pairs:
        return text
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    out = text
    for key, val in pairs:
        if key in out:
            out = out.replace(key, val)
    return out


def _resolve_normalized(name, title_idx, alias_idx, suffix_idx):
    """归一化匹配:按缩写/去标点中英文/多形态拆解比对已有节点名。
    仅在精确匹配 miss 后触发，避免歧义(多候选→None)。"""
    abbr, stripped = _normalize_name_for_match(name)
    if not abbr and not stripped:
        return None
    # 收集所有候选节点名(path 作名)
    candidates = {}
    for idx in (title_idx, alias_idx, suffix_idx):
        for key, paths in idx.items():
            if len(paths) == 1:  # 只对唯一节点做归一化比对
                candidates.setdefault(key, paths[0])
    matches = set()
    for key, path in candidates.items():
        k_abbr, k_stripped = _normalize_name_for_match(key)
        if abbr and k_abbr == abbr:
            matches.add(path)
        elif stripped and k_stripped and k_stripped == stripped:
            matches.add(path)
        elif name in decompose_name_to_aliases(key):
            matches.add(path)
    if len(matches) == 1:
        return matches.pop()
    return None


def has_inverse_edge(conn, subject, predicate, object_):
    """检查图里是否已有反向逆边(B--q-->A),用于增量去重。
    若存在且本边优先级 ≤ 反向边,返回 True(应跳过)。
    """
    bp = base_predicate(predicate)
    for bp1, bp2 in INVERSE_PAIRS:
        if bp != bp1:
            continue
        rev = conn.execute(
            "SELECT predicate FROM edges WHERE subject=? AND object=?",
            (object_, subject),
        ).fetchall()
        for r in rev:
            bp_rev = base_predicate(r["predicate"])
            if bp_rev == bp2:
                pri_self = PRIORITY.get(bp, 0)
                pri_rev = PRIORITY.get(bp2, 0)
                if pri_rev >= pri_self:
                    return True
    return False


def dedup_batch(flat, conn):
    """批量去重:待加边列表内部的互逆冗余 + 与图里已有边的互逆冗余。
    flat: [(meta, triple_dict), ...]
    返回应跳过的索引集合。
    """
    idx = {}
    for i, (_, t) in enumerate(flat):
        key = (t["subject"], base_predicate(t["predicate"]), t["object"])
        idx.setdefault(key, []).append(i)
    skip = set()
    for i, (_, t) in enumerate(flat):
        if i in skip:
            continue
        # 与图里已有反向边去重
        if has_inverse_edge(conn, t["subject"], t["predicate"], t["object"]):
            skip.add(i)
            continue
        bp = base_predicate(t["predicate"])
        for bp1, bp2 in INVERSE_PAIRS:
            if bp != bp1:
                continue
            rev_key = (t["object"], bp2, t["subject"])
            for j in idx.get(rev_key, []):
                if j == i or j in skip:
                    continue
                pri_i = PRIORITY.get(bp1, 0)
                pri_j = PRIORITY.get(bp2, 0)
                if pri_i > pri_j:
                    skip.add(j)
                elif pri_j > pri_i:
                    skip.add(i)
                else:
                    skip.add(max(i, j))
    return skip


def extract_aliases_from_md(page_path):
    """层1机械抽取:从 wiki md 自动识别 alias 候选(零 token)。
    来源:
      - title 括注(中英文):基于...(MixT 专利) → MixT 专利
      - 正文 wikilink 显示名:[[papers/x|MixT]] → MixT
    返回 alias 候选列表(已去重)。
    """
    p = Path(page_path)
    if not p.is_absolute():
        p = REPO / (str(page_path).removesuffix(".md") + ".md")
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8")
    fm = read_frontmatter(p)
    title = str(fm.get("title", ""))
    candidates = []
    # title 括注:抓 (中文/英文短词) 和 （中文）
    for m in re.finditer(r"[（(]([^（）()]{1,30})[)）]", title):
        cand = m.group(1).strip()
        if cand and cand not in title[:title.index(m.group(0))]:
            candidates.append(cand)
    # wikilink 显示名(管道后)
    # 仅自引用 wikilink（目标 = 当前页）才提取显示名作 alias；
    # 指向其他页面的 wikilink 显示名属于目标页，不应作当前页 alias
    page_str = str(page_path).removesuffix(".md")
    rel_path = page_str.split("/wiki/", 1)[1] if "/wiki/" in page_str else page_str
    for m in WIKILINK_RE.finditer(text):
        full = m.group(0)
        if "|" in full:
            target = m.group(1)
            if target != rel_path and target != page_str:
                continue
            disp = full.split("|")[1].rstrip("]]").strip()
            if disp and len(disp) <= 30:
                candidates.append(disp)
    # 去重保序
    seen = set()
    out = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def insert_aliases(conn, node_path, aliases):
    """插入 alias→node 映射；同 alias 可多指向，返回既有歧义映射。"""
    conflicts = []
    for alias in aliases:
        alias = alias.strip()
        if not is_resolvable_alias(alias):
            continue
        existing_paths = [row["node_path"] for row in conn.execute(
            "SELECT node_path FROM aliases WHERE alias=?", (alias,)
        )]
        conflicts.extend(
            (alias, existing_path, node_path)
            for existing_path in existing_paths
            if existing_path != node_path
        )
        conn.execute(
            "INSERT OR IGNORE INTO aliases (alias, node_path) VALUES (?,?)",
            (alias, node_path),
        )
    return conflicts


def node_exists(conn, path):
    return conn.execute("SELECT 1 FROM nodes WHERE path=?", (path,)).fetchone() is not None


def ensure_node(
    conn, path, title, node_type, source_type="", date="", status="current",
    has_raw=0, entity_subtype=None, ingest_version=None, description=None,
):
    """UPSERT 节点(不存在则建)。"""
    # 未显式提供的派生字段沿用旧值；UPSERT 不触发外键 ON DELETE 级联。
    existing = conn.execute(
        "SELECT ingest_version,description FROM nodes WHERE path=?", (path,)
    ).fetchone()
    if ingest_version is None:
        ingest_version = existing["ingest_version"] if existing else 0
    if description is None:
        description = existing["description"] if existing else ""
    conn.execute(
        "INSERT INTO nodes "
        "(path, title, type, entity_subtype, source_type, date, status, has_raw_source, ingest_version,description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "title=excluded.title, type=excluded.type, entity_subtype=excluded.entity_subtype, "
        "source_type=excluded.source_type, date=excluded.date, status=excluded.status, "
        "has_raw_source=excluded.has_raw_source, ingest_version=excluded.ingest_version, "
        "description=excluded.description",
        (path, str(title), node_type, entity_subtype, str(source_type), str(date),
         str(status), has_raw, ingest_version, str(description or "")),
    )


def get_metadata(conn, key, default=""):
    """读取 metadata 表的 key-value。"""
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_metadata(conn, key, value):
    """写入 metadata 表的 key-value（UPSERT）。"""
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()


# ===== ADR-003: Embedding-driven emergent resolve =====

def load_embed_config():
    """加载 embedding-resolve 配置(阈值+模型)。"""
    import yaml
    cfg_path = REPO / "operations" / "config" / "embedding-resolve.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_entity_embedding_index(conn, emb_conn):
    """构建 entity 节点的 embedding 索引(排除 person/proposition)。

    返回 (paths, texts, vectors) 或 None(构建失败)。
    向量来自 embeddings.db 缓存(命中秒返,未命中调 API 批量 embed)。
    """
    try:
        import numpy as np
        import sys as _sys
        _sys.path.insert(0, str(REPO / ".scripts"))
        from embed_helper import embed_cached_batch
    except Exception:
        return None

    # 取 entity 节点(排除 person/proposition — 非概念端点)
    rows = conn.execute(
        "SELECT path, COALESCE(title, path) FROM nodes "
        "WHERE type='entity' AND (entity_subtype IS NULL OR entity_subtype NOT IN ('person','proposition'))"
    ).fetchall()
    if not rows:
        return None
    paths = [r[0] for r in rows]
    texts = [r[1] for r in rows]

    # 从 node_texts 取文本(优先用 title 缓存),回退到 path 作 text
    for i, p in enumerate(paths):
        cached = emb_conn.execute(
            "SELECT text FROM node_texts WHERE path=?", (p,)
        ).fetchone()
        if cached:
            texts[i] = cached[0]

    vecs = embed_cached_batch(texts)
    return (paths, np.array(vecs, dtype=np.float32))


def embedding_resolve(name, entity_index, thresholds):
    """ADR-003: embedding-based resolve(三档分流)。

    参数:
      name: 待 resolve 的实体名
      entity_index: build_entity_embedding_index 的返回值
      thresholds: dict with 'auto' and 'floor' keys

    返回 (resolved_path, similar_candidates):
      resolved_path: str(auto-alias 到唯一已有节点)或 None
      similar_candidates: [(path, score), ...](重叠区,建相似边)
    """
    if not entity_index:
        return None, []
    try:
        import numpy as np
        import sys as _sys
        _sys.path.insert(0, str(REPO / ".scripts"))
        from embed_helper import embed_cached_batch, cosine_sim
    except Exception:
        return None, []

    paths, vecs = entity_index
    auto = thresholds.get("auto", 0.85)
    floor = thresholds.get("floor", 0.72)

    # embed query
    qvec = embed_cached_batch([name])
    if qvec is None or len(qvec) == 0:
        return None, []
    sims = cosine_sim(qvec[0], vecs)

    # 三档分流
    above_floor = [(paths[i], float(sims[i])) for i in range(len(paths)) if sims[i] >= floor]
    if not above_floor:
        return None, []  # 无候选 → 新建节点

    above_auto = [(p, s) for p, s in above_floor if s >= auto]
    if len(above_auto) == 1:
        # 唯一高置信候选 → auto-alias
        return above_auto[0][0], []

    # 多候选或重叠区 → 全部建相似边(两节点并存)
    # 最多取 top-5 避免边爆炸
    above_floor.sort(key=lambda x: x[1], reverse=True)
    return None, above_floor[:5]

#!/usr/bin/env python3
"""graph_ingest.py — 增量建边入口(主数据化 v4,2026-07-25)

架构切换:边只在 graph.db(不再从 md Core Triples 段派生)。
两种 ingest 模式:
  1. 预填+语义模式(推荐,省 token):prefill 输出模板 → LLM 填语义槽 → ingest --semantic
     - 机械边(作者/引用)自动从 frontmatter 提取(0 token)
     - LLM 只填语义槽(研究方向/keyword 等),格式极简
     - 代码补 confidence/source/is_sr,做 resolve+去重+入库
  2. 直接模式(兼容):ingest --triples <json> 或 --triples-json <json>
用法:
  graph_ingest.py init
  graph_ingest.py prefill --page <wiki路径>           # 生成模板
  graph_ingest.py ingest --page <wiki路径> --semantic <语义文件>
  graph_ingest.py ingest --page <wiki路径> --triples <JSON文件>
  graph_ingest.py ingest --page <wiki路径> --triples-json '<JSON字符串>'
"""
import argparse
import json
import re
import datetime
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_delta as gd
import hub_semantics as hs
import wiki_locator as wl


class IngestResult(NamedTuple):
    """add_knowledge_edges 的返回值（按名字取，兼容位置索引）。"""
    edges_added: int
    dedup_skipped: int
    dup_skipped: int
    resolve_hits: int
    resolve_ambig: int
    nodes_created: int
    descriptive_warnings: list


class HubAssignResult(NamedTuple):
    """assign_keyword_hubs 的返回值（按名字取，兼容位置索引）。"""
    synced: int
    unrecognized_directions: list
    hub_fallback_keywords: list


class HubAssignMeetingResult(NamedTuple):
    """assign_keyword_hubs_meeting_admin 的返回值（按名字取，兼容位置索引）。"""
    synced: int
    catch_all_added: int

ADMIN_NAV_PREDICATES = {
    "涉及", "讨论", "形成决策", "依据", "替代", "汇报", "发布者",
    "负责人", "承办部门", "推动", "申请事项", "适用对象",
}
ADMIN_KEYWORD_LIMIT = 15
ADMIN_RELATION_LIMIT = 8

# 教学文档谓词
TEACHING_NAV_PREDICATES = {
    "涉及", "讨论", "涵盖", "考核", "前置", "后续", "依据",
    "适用", "开课单位", "主讲人",
}
TEACHING_KW_PREDICATES = {"涉及", "讨论", "涵盖", "考核"}

# 商业文档谓词
BUSINESS_NAV_PREDICATES = {
    "涉及", "讨论", "分析", "规划", "依据", "竞争", "合作",
    "替代", "发布者", "负责人", "承办部门",
}
BUSINESS_KW_PREDICATES = {"涉及", "讨论", "分析", "规划"}

# 域 → (nav_predicates, kw_predicates) 映射，供 parse_semantic_text / cmd_ingest 通用判断
# 注意: ADMIN_KW_PREDICATES 在下方定义，此处用 lambda 延迟求值避免前向引用
# private 域谓词(物理隔离领域,沿用通用文档域谓词集)
PRIVATE_NAV_PREDICATES = {
    "涉及", "讨论", "属于", "基于", "相关于", "依据", "对应",
    "属于分类", "影响", "反映", "记录于", "来源", "作用于",
}
PRIVATE_KW_PREDICATES = {"涉及", "讨论", "属于", "相关于", "基于"}

_DOMAIN_PREDICATES = {
    "admin": (ADMIN_NAV_PREDICATES, {"涉及", "讨论", "形成决策", "推动", "申请事项", "适用对象"}),
    "teaching": (TEACHING_NAV_PREDICATES, TEACHING_KW_PREDICATES),
    "business": (BUSINESS_NAV_PREDICATES, BUSINESS_KW_PREDICATES),
    "private": (PRIVATE_NAV_PREDICATES, PRIVATE_KW_PREDICATES),
}

# 明确会“生效/废止”的轻量时态页面类型：先只接入行政与教学侧。
# temporal_facts 独立于 edges，只在本函数写入，避免污染普通导航语义。
_DOMAIN_TEMPORAL_PAGE_TYPES = {
    "admin": {"policy", "procedure", "decision"},
    "teaching": {"course"},
}
TEMPORAL_PAGE_PREDICATE = "生效"


def _get_domain_from_path(page_path):
    """从 wiki 路径提取域前缀: admin/teaching/business/private。academic 走论文专用流程。"""
    for domain in ("admin", "teaching", "business", "private"):
        if page_path.startswith(f"{domain}/wiki/"):
            return domain
    return None

def _connect_for(args):
    """按 args 选择 graph.db: --db 显式优先;否则按 page 所属域(private→private 库)。
    保持物理隔离:private 页只写 private/graph.db,主库页只写 cross-domain/graph.db。
    """
    db = getattr(args, "db", None)
    if db:
        return gl.connect(db)
    page = getattr(args, "page", None)
    if page:
        return gl.connect(gl.graph_db_for(page))
    return gl.connect()

# ===== 谓词 tier 映射 (论文→研究方向 hub 边) =====
_PREDICATE_TIERS = None  # lazy cache

def load_predicate_tiers():
    """加载 predicate_tiers.yaml。返回 (pred2tier_dict, default_tier)。
    LLM 只选语义谓词, tier 由程序查表补(派生, 不入 edges)。
    未登记谓词 → WARN(失败显性化) + 默认 tier。
    """
    global _PREDICATE_TIERS
    if _PREDICATE_TIERS is not None:
        return _PREDICATE_TIERS
    import yaml
    cfg_path = Path(__file__).parent / "predicate_tiers.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    _PREDICATE_TIERS = (cfg.get("predicates", {}), cfg.get("default_tier", 1))
    return _PREDICATE_TIERS


def get_predicate_tier(predicate):
    """查谓词 tier。未登记返回 (default_tier, False) 供 WARN。"""
    pred2tier, default = load_predicate_tiers()
    t = pred2tier.get(predicate)
    if t is not None:
        return t, True
    return default, False



_ARXIV_DIRECTIONS = None
_ARXIV_DIRECTION_SCOPES = None


def load_arxiv_directions():
    """加载 operations/config/arxiv-directions.yaml。返回标准方向语义名 set。
    用于 ensure_research_hub 新建 hub 时校验方向名是否在 arXiv 标准集。
    """
    global _ARXIV_DIRECTIONS
    if _ARXIV_DIRECTIONS is not None:
        return _ARXIV_DIRECTIONS
    import yaml
    cfg_path = gl.REPO / "operations" / "config" / "arxiv-directions.yaml"
    if not cfg_path.exists():
        _ARXIV_DIRECTIONS = set()
        return _ARXIV_DIRECTIONS
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    directions = cfg.get("directions", [])
    # v7 配置为 {name, code, seeds} 对象；兼容早期 "语义名#code" 字符串格式。
    _ARXIV_DIRECTIONS = {
        (d.get("name", "") if isinstance(d, dict) else str(d).split("#")[0]).strip()
        for d in directions
    }
    _ARXIV_DIRECTIONS.discard("")
    return _ARXIV_DIRECTIONS


def load_arxiv_direction_scopes():
    """根方向创建时使用的 Agent 审核 Scope bootstrap；Hub 页落位后以页为准。"""
    global _ARXIV_DIRECTION_SCOPES
    if _ARXIV_DIRECTION_SCOPES is not None:
        return _ARXIV_DIRECTION_SCOPES
    cfg_path = gl.REPO / "operations" / "config" / "arxiv-directions.yaml"
    if not cfg_path.exists():
        _ARXIV_DIRECTION_SCOPES = {}
        return _ARXIV_DIRECTION_SCOPES
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    _ARXIV_DIRECTION_SCOPES = {
        str(item.get("name") or "").strip(): str(item.get("scope") or "").strip()
        for item in cfg.get("directions", []) if isinstance(item, dict)
    }
    return {key: value for key, value in _ARXIV_DIRECTION_SCOPES.items() if key and value}


# ===== 辅助函数 =====

def is_descriptive_phrase(obj):
    """机械近似:客体疑似描述性短语 → WARN。判据见 INGEST.md「描述性短语校验」:
    脱离论文能否独立指代;机械近似为长度>阈值且含谓词结构或中文标点。
    双门槛(2026-08-03调优,降低误伤):
      - 长度<=15:合法概念名直接放过(原8太严,10-15字概念名常见)
      - 长度16-20:仅标点判定(谓语词在此区间多为概念名修饰,误伤率高)
      - 长度>20且含触发词:判为描述性短语(真正的描述性短语又长又含谓语句式)"""
    if len(obj) <= 15:
        return False
    # ADR-003: 公式内逗号(括号内如 R(ρ,σ))不触发;去括号内容再查
    no_paren = re.sub(r'[（(][^)）]*[)）]', '', obj)
    if re.search(r'[。，,；;]', no_paren):
        return True
    if len(obj) > 20 and re.search(r'(为|是|导致|采用|基于|表明|说明|表示|揭示|发现|应用于|实现|利用|开发|证明|探索|提出|推广到|扩展至|结合|改进)', obj):
        return True
    return False


# ADR-003: 谓词结构触发词(区分论断 vs 纯名词短语;proposition 仅保留论断型)
PREDICATE_TRIGGERS = re.compile(
    r'(为|是|导致|采用|基于|表明|说明|表示|揭示|发现|应用于|实现|利用|开发|证明|探索|提出'
    r'|推广到|扩展至|结合|改进|映射|捕获|构造|引入|限制|约束|避免|编码|参数化|刻画|丢弃'
    r'|识别|判定|优化|制备|融合|解纠缠|控制|重入|分裂|退化|需设|待探索|未能|无法|不适用)'
)

def has_predicate_structure(text):
    """检测文本是否含谓词结构(动词),用于区分论断 vs 纯名词短语。
    ADR-003 Tier 2: proposition 节点仅保留论断型(含动词);
    纯名词短语(如「两步法」「张量网络」)降级为 keyword 节点。"""
    s = (text or "").strip()
    if not s:
        return False
    return bool(PREDICATE_TRIGGERS.search(s))


# keyword 谓词集(这些 object 适用裸缩写校验)
KW_PREDICATES = {"研究基础", "核心方法", "核心创新点", "局限性", "未来展望", "研究关键词"}
MEETING_KW_PREDICATES = {"讨论", "涉及", "汇报", "规划", "决策"}  # 会议 keyword 谓词
ADMIN_KW_PREDICATES = {"涉及", "讨论", "形成决策", "推动", "申请事项", "适用对象"}  # 行政 keyword 谓词

def is_bare_abbreviation(obj):
    """keyword 裸缩写规则:含英文缩写(≥2连续大写字母)且无括号释义 → 裸缩写。
    新格式要求「中文英文(缩写)」,缩写须在括号内。见 INGEST.md「keyword 写法」。返回 True 表示需 WARN。
    翻译对照全称词豁免:obj 含中文且某 token 重复出现≥2次(如「XX模型XX model」),
    说明该 token 是中英对照的全称词(模型符号),非缩写,不报。"""
    if not obj or re.search(r"[（(][^)）]*[)）]", obj):
        return False  # 有括号释义,合规
    no_paren = re.sub(r"[（(][^)）]*[)）]", "", obj)
    tokens = re.findall(r"[A-Z]{2,}[A-Za-z0-9]*", no_paren)
    if not tokens:
        return False
    # 翻译对照全称词: obj 含中文且 token 重复出现≥2次 → 全称词非缩写,放过
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", obj))
    for tok in tokens:
        if has_chinese and no_paren.count(tok) >= 2:
            continue
        return True
    return False


def is_citation_fragment(name):
    """检测引文残片：MinerU 把参考文献条目误建为实体（如 2019;1:538–550.-2019、1992;69(19):2863–2866.-1992）。
    判据：无中文 + 含「年份;」+「:页码」的纯引文格式。返回 True 表示应跳过建节点。"""
    s = (name or "").strip()
    if len(s) < 6 or re.search(r'[\u4e00-\u9fff]', s):
        return False
    # 典型引文格式：年份;卷(期):页码 或 年份;卷:页码-年份
    if re.search(r'\d{4};', s) and re.search(r':\d', s):
        return True
    # author-year 引文残片:Author-YYYY(无中文,纯引文 key 如 Verstraete-2006/A-1988/SJ-2020)
    if re.fullmatch(r'[A-Za-z]+-\d{4}', s):
        return True
    return False


def is_fragment_token(name):
    """检测碎片 token：LLM/OCR 把单词拆出的裸小写 ASCII 残片（如 Rényi→nyi、Bose→ose）。
    判据：纯小写 ASCII 字母（无中文/数字/大写/符号），len ≤ 4。
    合法小写术语最短 5 字符（anyon/fusion/dropout）。返回 True 表示应跳过建节点。"""
    s = (name or "").strip()
    if not s or len(s) > 4 or re.search(r'[\u4e00-\u9fff]', s):
        return False
    return s.isascii() and s.isalpha() and s.islower()


def cleanup_ghost_hubs(conn):
    """清理 ghost hub：.md 已不存在但 graph.db 仍残留的 type='hub' 节点 + 关联边。
    返回被清理的 hub path 列表。hub 合并/删除应经 merge_hubs（已清节点），此函数兜底任何遗漏路径。"""
    cleaned = []
    for (path,) in conn.execute("SELECT path FROM nodes WHERE type='hub'").fetchall():
        if (gl.REPO / (path + ".md")).exists():
            continue
        conn.execute("DELETE FROM edges WHERE subject=? OR object=?", (path, path))
        conn.execute("DELETE FROM nodes WHERE path=?", (path,))
        cleaned.append(path)
    if cleaned:
        conn.commit()
    return cleaned


def cleanup_orphan_references(conn):
    """图健康前置检查：清理孤儿 alias（指向已删节点）+ 孤儿边（引用已删节点）。

    防止上一篇摄入/回滚遗留的脏数据阻塞下一篇的外键约束（FK constraint failed）。
    幂等，每次摄入前清扫。返回清理计数 dict。
    """
    cleaned = {"orphan_aliases": 0, "orphan_edges": 0, "fk_violations": 0}
    # 1. 孤儿 alias：node_path 不在 nodes 表（含 NULL/空）
    cur = conn.execute(
        "DELETE FROM aliases WHERE node_path IS NULL OR node_path = '' "
        "OR node_path NOT IN (SELECT path FROM nodes)"
    )
    cleaned["orphan_aliases"] = cur.rowcount
    # 2. 孤儿边：subject/object 不在 nodes 表（FK 违规）
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    cleaned["fk_violations"] = len(violations)
    for table, rowid, *_ in violations:
        if table == "edges":
            conn.execute("DELETE FROM edge_evidence WHERE edge_id=?", (rowid,))
            conn.execute("DELETE FROM edges WHERE id=?", (rowid,))
            cleaned["orphan_edges"] += 1
        elif table == "edge_evidence":
            conn.execute("DELETE FROM edge_evidence WHERE rowid=?", (rowid,))
            cleaned["orphan_edges"] += 1
    if any(cleaned.values()):
        conn.commit()
    return cleaned

def clean_page_edges(conn, page: str) -> dict:
    """re-ingest:撤销本页贡献的直接边和间接派生边。

    新摄入用 edge_origins 精确追踪；历史边没有 lineage 时，以本页 raw source
    为保守后备。共享语义边只移除本页 evidence/origin，仍有其他来源则保留。
    """
    direct_ids = {row[0] for row in conn.execute(
        "SELECT id FROM edges WHERE subject=? OR object=?", (page, page)
    )}
    origin_rows = list(conn.execute(
        "SELECT edge_id, source FROM edge_origins WHERE origin_page=?", (page,)
    ))
    origin_ids = {row[0] for row in origin_rows}

    fm = gl.read_frontmatter(page)
    raw_sources = set()
    sources = fm.get("sources", []) if isinstance(fm, dict) else []
    if not isinstance(sources, list):
        sources = [sources]
    for source in sources:
        base = str(source or "").split("#", 1)[0].strip()
        if base:
            raw_sources.add(base)

    # 历史兼容：lineage 上线前的概念间边按本页 raw evidence 认领。
    legacy_ids = set()
    for raw_source in raw_sources:
        like = f"{raw_source}#%"
        legacy_ids.update(row[0] for row in conn.execute(
            "SELECT id FROM edges WHERE source=? OR source LIKE ?", (raw_source, like)
        ))
        legacy_ids.update(row[0] for row in conn.execute(
            "SELECT edge_id FROM edge_evidence WHERE source=? OR source LIKE ?", (raw_source, like)
        ))

    target_ids = direct_ids | origin_ids | legacy_ids
    conn.execute("DELETE FROM edge_origins WHERE origin_page=?", (page,))

    removed_evidence = 0
    for edge_id, source in origin_rows:
        if not source:
            continue
        still_owned = conn.execute(
            "SELECT 1 FROM edge_origins WHERE edge_id=? AND source=? LIMIT 1",
            (edge_id, source),
        ).fetchone()
        if not still_owned:
            removed_evidence += conn.execute(
                "DELETE FROM edge_evidence WHERE edge_id=? AND source=?", (edge_id, source)
            ).rowcount
    for edge_id in legacy_ids:
        for raw_source in raw_sources:
            removed_evidence += conn.execute(
                "DELETE FROM edge_evidence WHERE edge_id=? AND (source=? OR source LIKE ?)",
                (edge_id, raw_source, f"{raw_source}#%"),
            ).rowcount

    edges_removed = 0
    lineage_edges_removed = 0
    for edge_id in target_ids:
        if edge_id in direct_ids:
            edges_removed += conn.execute("DELETE FROM edges WHERE id=?", (edge_id,)).rowcount
            continue
        has_origin = conn.execute(
            "SELECT 1 FROM edge_origins WHERE edge_id=? LIMIT 1", (edge_id,)
        ).fetchone()
        evidence = conn.execute(
            "SELECT source FROM edge_evidence WHERE edge_id=? ORDER BY recorded_at DESC LIMIT 1",
            (edge_id,),
        ).fetchone()
        if not has_origin and not evidence:
            lineage_edges_removed += conn.execute("DELETE FROM edges WHERE id=?", (edge_id,)).rowcount
        elif evidence:
            conn.execute("UPDATE edges SET source=? WHERE id=?", (evidence[0], edge_id))
    # temporal_facts 没有外键级联，须显式清理，避免 re-ingest 后残留旧时态窗口。
    temporal = conn.execute(
        "DELETE FROM temporal_facts WHERE subject=? OR object=?", (page, page)
    ).rowcount
    conn.commit()
    return {
        "edges_removed": edges_removed,
        "lineage_edges_removed": lineage_edges_removed,
        "edge_evidence_removed": removed_evidence,
        "temporal_facts_removed": temporal,
    }


def _temporal_date_value(value):
    """把 frontmatter 中的日期规范为 ISO 字符串；无效值返回 (None, error)。"""
    if value in (None, ""):
        return None, None
    if isinstance(value, datetime.datetime):
        return value.date().isoformat(), None
    if isinstance(value, datetime.date):
        return value.isoformat(), None
    text = str(value).strip()
    if not text:
        return None, None
    try:
        return datetime.date.fromisoformat(text).isoformat(), None
    except ValueError:
        return None, f"无法解析日期: {value!r}"


def sync_page_temporal_fact(conn, page: str, fm: dict) -> dict:
    """为明确会过期的页面写一条 canonical 时态事实。

    subject/object 均指向本页、predicate=“生效”；查询时用页路径作为节点定位，
    避免在 frontmatter 外侧再造一个“有效期”伪节点。同一页面重复摄入会替换旧记录；
    effective_from/effective_to 为空时不写入。
    """
    domain = _get_domain_from_path(page)
    page_types = _DOMAIN_TEMPORAL_PAGE_TYPES.get(domain) or set()
    if fm.get("type") not in page_types:
        return {"removed": 0, "added": 0, "warnings": []}

    valid_from, from_error = _temporal_date_value(fm.get("effective_from"))
    valid_until, until_error = _temporal_date_value(fm.get("effective_to"))
    warnings = [w for w in (from_error, until_error) if w]
    removed = conn.execute(
        "DELETE FROM temporal_facts WHERE subject=? AND predicate=? AND object=?",
        (page, TEMPORAL_PAGE_PREDICATE, page),
    ).rowcount
    if valid_from and valid_until and valid_until < valid_from:
        warnings.append("effective_to 早于 effective_from")
        return {"removed": removed, "added": 0, "warnings": warnings}
    if valid_from is None and valid_until is None:
        return {"removed": removed, "added": 0, "warnings": warnings}
    sources = gl.parse_list_field(fm, "sources")
    source = sources[0] if sources else ""
    conn.execute(
        "INSERT INTO temporal_facts "
        "(subject, predicate, object, valid_from, valid_until, source, is_sr) "
        "VALUES (?,?,?,?,?,?,0)",
        (page, TEMPORAL_PAGE_PREDICATE, page, valid_from, valid_until, source),
    )
    return {"removed": removed, "added": 1, "warnings": warnings}

def extract_name(author_str):
    """从 frontmatter authors[] 提取干净人名。
    "Da-zhi Fang (方大智)" → "方大智"
    无括号 → 返回原文。
    """
    m = re.search(r'[（(]([^）)]+)[）)]', str(author_str))
    return m.group(1).strip() if m else str(author_str).strip()


def extract_section_text(text, section_name):
    """从 wiki md 正文提取 ## section 段文本(已去 frontmatter)。"""
    if text.startswith("---"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            text = parts[2]
    pattern = rf'^## {re.escape(section_name)}\s*\n(.*?)(?=\n## |\Z)'
    m = re.search(pattern, text, re.M | re.S)
    if m:
        content = m.group(1).strip()
        content = re.sub(r'\{:#\S+\}', '', content).strip()
        return content
    return ""


def extract_contribution(text):
    """从 wiki md 提取主要贡献子段(### 主要贡献/### 三、主要贡献等)。
    比 Content 其他子段更贴近核心导航,作为提取来源补充。"""
    if text.startswith("---"):
        parts = text.split("---\n", 2)
        if len(parts) >= 3:
            text = parts[2]
    pattern = r'^###[^#\n]*主要贡献[^#\n]*\n(.*?)(?=\n### |\n## |\Z)'
    m = re.search(pattern, text, re.M | re.S)
    if m:
        content = m.group(1).strip()
        content = re.sub(r'\{:#\S+\}', '', content).strip()
        return content
    return ""


def resolve_related_path(page_path, target):
    """将 frontmatter related 中的 wikilink 目标解析为仓库相对路径。
    [[papers/2026-mpe-prl]] → academic/wiki/papers/2026-mpe-prl
    [[../admin/wiki/xxx]]   → admin/wiki/xxx
    """
    target = target.replace("[[", "").replace("]]", "").strip()
    if not target:
        return None
    if target.startswith("../"):
        return target.lstrip("./")
    sub = page_path.split("/")[0]
    if target.startswith(f"{sub}/"):
        return target
    return f"{sub}/wiki/{target}"


# ===== 机械边提取 =====

def extract_mechanical_edges(page_path):
    """从 frontmatter 机械提取边(0 token)。
    authors[] → 人 --作者--> 论文
    related[]  → 论文 --引用--> 目标
    返回 [{subject, predicate, object}, ...]
    """
    fm = gl.read_frontmatter(page_path)
    edges = []
    for author in gl.parse_list_field(fm, "authors"):
        name = extract_name(author)
        if name:
            edges.append({
                "subject": name, "predicate": "作者", "object": page_path,
                "object_is_canonical": True,
            })
    for rel in gl.parse_list_field(fm, "related"):
        resolved = resolve_related_path(page_path, rel)
        if resolved:
            edges.append({
                "subject": page_path, "predicate": "引用", "object": resolved,
                "subject_is_canonical": True, "object_is_canonical": True,
            })
    return edges


# ===== 预填模板 =====

def cmd_prefill(args):
    """生成预填模板:机械边已提取 + 论文摘要 + 待填语义槽。"""
    conn = _connect_for(args)
    page = args.page.removesuffix(".md")
    page_path = page
    node_type, conflicts = upsert_page_node(conn, page)
    conn.commit()

    fm = gl.read_frontmatter(page)
    mech = extract_mechanical_edges(page)

    # Navigation 段(论文摘要供判断)
    p = gl.REPO / (page + ".md")
    text = p.read_text(encoding="utf-8")
    nav = extract_section_text(text, "Navigation")

    # 已有边(重摄时提示)
    existing = conn.execute(
        "SELECT predicate, object FROM edges WHERE subject=? OR object=?",
        (page, page)
    ).fetchall()

    print(f"=== 论文: {fm.get('title', page)} ===")
    print(f"=== 页面: {page} ===\n")

    print("--- 已自动提取的机械边(无需填写) ---")
    authors = [e for e in mech if e["predicate"] == "作者"]
    if authors:
        print("作者:")
        for a in authors:
            print(f"  {a['subject']}")
    refs = [e for e in mech if e["predicate"] == "引用"]
    if refs:
        print("引用:")
        for r in refs:
            print(f"  → {r['object']}")
    if not mech:
        print("  (无 frontmatter authors/related,全部语义边需填写)")
    print()

    if existing:
        print(f"--- 图中已有边({len(existing)}条,增量会自动去重) ---")
        for pred, obj in existing:
            print(f"  --{pred}--> {obj}")
        print()

    print("--- 论文摘要(供判断,勿输出) ---")
    if nav:
        print("[Navigation]")
        print(nav)
    else:
        print("(无 Navigation 段)")
    contrib = extract_contribution(text)
    if contrib:
        print()
        print("[主要贡献]")
        print(contrib)
    elif not nav:
        print("(无主要贡献段,建议读 raw 摘要)")
    print()

    venue = fm.get("venue", "")
    print("--- 待填语义槽(填入文件后传 --semantic) ---")
    _prefill_domain = _get_domain_from_path(page)
    if _prefill_domain is not None:
        _nav_preds, _ = _DOMAIN_PREDICATES[_prefill_domain]
        print("三元组（主体|谓词|客体；主体用本文件/本文档；谓词限: " + "/".join(sorted(_nav_preds)) + "）:")
        print("  ")
        print("查询别名（可选；每行一个，只有来源明确或页面明确使用的简称）:")
        print("  ")
        return
    # 研究方向由程序从 keyword embedding 匹配派生(LLM 不填,见 direction_matcher.py)
    if fm.get("type") == "conference-summary" or "/wiki/conferences/" in f"/{page_path}":
        print("会议关键词 (每行: 讨论|主题词；可用 涉及/汇报/规划/决策):")
        print()
        conn.close()
        return
    if venue:
        print(f"期刊 (venue原文: {venue}):")
        print()
    else:
        print("期刊:")
        print()
    print("研究基础 (每行一个):")
    print()
    print("核心方法:")
    print()
    print("核心创新点:")
    print()
    print("局限性:")
    print()
    print("未来展望:")
    print()
    print("研究关键词 (兜底,无法归入以上类别; 含英文缩写须写「中文英文(缩写)」如 密度矩阵重整化群density matrix renormalization group(DMRG)):")
    print()
    print("对比方法:")
    print()
    print("通讯作者 (人名,如有则替换该作者的\"作者\"谓词为\"通讯作者\"):")
    print()
    print("自由边 (格式: 主体|谓词|客体, 每行一条, 用于就读/所属等):")
    print()

    conn.close()


# ===== 语义槽解析 =====

# 核心词谓词集(这些谓词的 object 是核心词,用于提取 keywords + 裸缩写校验)
KW_PREDICATES = {"研究基础", "核心方法", "核心创新点", "局限性", "未来展望", "研究关键词", "对比方法"}

# 命题谓词集(其 object 是论断/proposition,不进 hub 关键词,作为 proposition 节点入图)
PROPOSITION_PREDICATES = {"核心创新点", "局限性", "未来展望"}
# 概念谓词集(其 object 是概念名,进 hub 关键词)
CONCEPT_KW_PREDICATES = KW_PREDICATES - PROPOSITION_PREDICATES
# 结构性谓词集(程序发射的派生边,非 LLM 生成,不过谓词治理白名单)
STRUCTURAL_PREDICATES = {"包含", "相似"}
# 标题型谓词: object 是论文/文献标题等合法长实体(天然含标点),跳过描述性短语检测
TITLE_OBJECT_PREDICATES = {"引用"}
# frontmatter 确定性书目对象允许 ICLR/NeurIPS 等规范简称，不做裸缩写审计
METADATA_PREDICATES = {"发表于"}
SIMILAR_PREDICATE = "相似"  # ADR-003: embedding resolve 的相似边,对称关系,score 列存余弦分数

# 方向谓词集(论文→研究方向 hub 的谓词,用于提取 direction_predicates)
DIRECTION_PREDICATES = {"主要研究", "基于", "紧密相关于", "应用于", "贡献于", "延伸至", "涉及", "探索", "属于"}

# 语义槽已知的 section header 集合(论文/行政/会议类全覆盖)
SEMANTIC_SECTION_HEADERS = {"期刊", "第一作者", "其他作者", "通讯作者", "三元组",
    "行政主题", "行政关系", "查询别名", "会议关键词", "参会者", "汇报者", "决策", "待办"}


def detect_inline_section_headers(text):
    """方案1: 诊断语义槽中"header: content"同行格式(未被解析为 section)。

    parse_semantic_text 用正则 `^.+[:：]$` 识别 header(要求整行以冒号结尾);
    "期刊: Phys. Rev. B" 不以冒号结尾,被当作上一段的 item 静默吞掉。
    本函数扫描:已知 section 名 + 冒号 + 同行非空内容 → 告警。
    返回告警字符串列表(空列表 = 无问题)。
    """
    warns = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r'^(\S+?)\s*[:：]\s*(\S.*)$', stripped)
        if m:
            name = m.group(1)
            content = m.group(2)
            if name in SEMANTIC_SECTION_HEADERS:
                warns.append(
                    f"语义槽格式: '{name}: {content[:30]}' 使用同行格式, "
                    f"未被识别为 section header(内容被并入上一段静默丢弃)。"
                    f"改为 '{name}:' 独立成行 + 内容下一行。"
                )
    return warns


_PLACEHOLDER_VALUE_RE = re.compile(
    r"(?:未提供|未给出|未知|不详|待补|待定|wiki未|not(?:provided|available|specified))",
    re.I,
)


def is_placeholder_value(value):
    return bool(_PLACEHOLDER_VALUE_RE.search(str(value or "").replace(" ", "")))


def canonical_venue_name(value):
    """从展示型 venue 中取稳定导航名；保留会议名，裁掉期刊卷页年份。"""
    text = str(value or "").strip().strip('"\'')
    if not text or is_placeholder_value(text):
        return ""
    # 常见期刊格式：Phys. Rev. A 91, 032306 (2015) / Nature 620, 123 (2023)
    match = re.match(r"^(.+?)(?:\s+\d+\s*,\s*[A-Za-z]?\d+|\s*\(\d{4}\))", text)
    if match:
        return match.group(1).strip().rstrip(",")
    return text


def _frontmatter_authors(fm):
    authors = fm.get("authors", []) if isinstance(fm, dict) else []
    if not isinstance(authors, list):
        authors = [authors]
    return [str(name).strip() for name in authors
            if str(name).strip() and not is_placeholder_value(name)]


def parse_semantic_text(text, page_path):
    """解析 LLM 填的语义槽文本。
    返回 (triples, keywords, main_direction, corresponding, cross_directions, direction_predicates)
    triples: [{subject, predicate, object}, ...]
    keywords: [keyword_name, ...] (代码从三元组提取,去重)
    main_direction: str or None
    cross_directions: [str, ...] 交叉方向名
    direction_predicates: [(direction, predicate), ...] 方向+语义谓词(用于 keyword 归属判断)
    """
    triples = []
    sections = {}
    current_section = None
    current_items = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r'^.+[:：]$', stripped):
            header = re.sub(r'\s*[（(].*$', '', stripped).rstrip(":：").strip()
            if header and "|" not in header:
                if current_section:
                    sections[current_section] = current_items
                current_section = header
                current_items = []
                continue
        if current_section:
            current_items.append(stripped)
    if current_section:
        sections[current_section] = current_items

    # 通用文档域: admin/teaching/business — 统一三元组格式
    domain = _get_domain_from_path(page_path)
    if domain is not None:
        nav_preds, kw_preds = _DOMAIN_PREDICATES[domain]
        # 域内合法页面类型(用于校验 type 字段，但不阻断解析)
        for item in sections.get("三元组", []):
            parts = [x.strip() for x in item.split("|", 2)]
            if len(parts) == 3 and all(parts):
                subject, predicate, object_name = parts
                # 主体代词替换: admin/business 用"本文件"，teaching 用"本文档"
                if subject in ("本文件", "本文档"):
                    subject = page_path
                if predicate in nav_preds:
                    triples.append({"subject": subject, "predicate": predicate, "object": object_name})
        keywords = [t["object"] for t in triples
                    if t.get("predicate") in kw_preds and t.get("subject") == page_path]
        keywords = list(dict.fromkeys(keywords))[:ADMIN_KEYWORD_LIMIT]
        return triples, keywords, None, set(), [], []

    fm = gl.read_frontmatter(page_path)
    if fm.get("type") == "conference-summary" or "/wiki/conferences/" in f"/{page_path}":
        keywords = []
        # 参会者 → 参会 三元组 (人 → 参会 → 会议)
        for item in sections.get("参会者", []):
            name = item.strip()
            if name:
                triples.append({"subject": name, "predicate": "参会", "object": page_path})
        # 汇报者: 人 | 议题 → 人→汇报→议题（修复人-议题断链）
        for item in sections.get("汇报者", []):
            parts = [x.strip() for x in item.split("|", 1)]
            if len(parts) == 2 and all(parts):
                person_field, topic = parts
                for person in [p.strip() for p in person_field.split(",") if p.strip()]:
                    triples.append({"subject": person, "predicate": "汇报", "object": topic})
                keywords.append(topic)
        # 决策: 决策内容（每行一条）→ 本会议→决策→内容（内容作 proposition 节点）
        for item in sections.get("决策", []):
            decision = item.strip()
            if decision:
                triples.append({"subject": page_path, "predicate": "决策", "object": decision})
        # 待办: 任务 | 负责人 → 人→待办→任务（任务作 keyword，不建独立节点）
        for item in sections.get("待办", []):
            parts = [x.strip() for x in item.split("|", 1)]
            if len(parts) == 2 and all(parts):
                task, person_field = parts
                for person in [p.strip() for p in person_field.split(",") if p.strip()]:
                    triples.append({"subject": person, "predicate": "待办", "object": task})
                keywords.append(task)
        # 三元组: 主体|谓词|客体 (统一格式，同论文)
        for item in sections.get("三元组", []):
            parts = [x.strip() for x in item.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                continue
            subj, pred, obj = parts
            if subj == "本会议":
                subj = page_path
            triples.append({"subject": subj, "predicate": pred, "object": obj})
            # 提取 keywords: 会议 keyword 谓词的 object 是关键词
            if pred in MEETING_KW_PREDICATES:
                keywords.append(obj)
        keywords = list(dict.fromkeys(keywords))
        return triples, keywords, None, set(), [], []

    # ===== 论文页: 统一三元组格式 =====
    main_direction = None
    cross_directions = []
    direction_predicates = []
    corresponding = set()
    keywords = []

    # 书目与作者由确定性 frontmatter 驱动；仅测试/旧页面缺字段时兼容语义槽。
    venue = canonical_venue_name(fm.get("venue", ""))
    if not venue:
        venue = next((canonical_venue_name(item) for item in sections.get("期刊", [])
                      if canonical_venue_name(item)), "")
    if venue:
        triples.append({"subject": page_path, "predicate": "发表于", "object": venue})

    authors = _frontmatter_authors(fm)
    if authors:
        triples.append({"subject": authors[0], "predicate": "第一作者", "object": page_path})
        corresponding.add(authors[0])
        for name in authors[1:]:
            triples.append({"subject": name, "predicate": "作者", "object": page_path})
        author_keys = {re.sub(r"\s+", "", name).casefold(): name for name in authors}
        for item in sections.get("通讯作者", []):
            key = re.sub(r"\s+", "", item).casefold()
            name = author_keys.get(key)
            if name and name != authors[0]:
                triples.append({"subject": name, "predicate": "通讯作者", "object": page_path})
                corresponding.add(name)
    else:
        for item in sections.get("第一作者", []):
            name = item.strip()
            if name and not is_placeholder_value(name):
                triples.append({"subject": name, "predicate": "第一作者", "object": page_path})
                corresponding.add(name)
        for item in sections.get("其他作者", []):
            name = item.strip()
            if name and not is_placeholder_value(name):
                triples.append({"subject": name, "predicate": "作者", "object": page_path})
        for item in sections.get("通讯作者", []):
            name = item.strip()
            if name and not is_placeholder_value(name):
                triples.append({"subject": name, "predicate": "通讯作者", "object": page_path})
                corresponding.add(name)

    # 三元组: 主体|谓词|客体 (统一段位,含论文→核心词/核心词→核心词/论文→方向)
    for item in sections.get("三元组", []):
        parts = [x.strip() for x in item.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            continue
        subj, pred, obj = parts
        if subj == "本论文":
            subj = page_path
        triples.append({"subject": subj, "predicate": pred, "object": obj})
        # 提取 keywords: 仅概念谓词的 object 是概念名(进 hub 关键词)；
        # 命题谓词(核心创新点/局限性/未来展望)的 object 是命题，作为 proposition 节点入图，
        # 不进 hub 关键词（避免污染 hub ## 关键词 段）
        if pred in CONCEPT_KW_PREDICATES:
            keywords.append(obj)
        # 提取 direction_predicates: 论文→方向 的三元组
        if subj == page_path and pred in DIRECTION_PREDICATES:
            direction_predicates.append((obj, pred))
            if main_direction is None:
                main_direction = obj
            elif obj != main_direction:
                cross_directions.append(obj)

    # keywords 去重
    keywords = list(dict.fromkeys(keywords))

    return triples, keywords, main_direction, corresponding, cross_directions, direction_predicates


def attach_wiki_section_sources(triples, page_path):
    """Optionally annotate Wiki-centered edges with a section locator.

    Raw citations remain in the Wiki page.  Graph edges do not duplicate them
    into edge_evidence; ``edges.source`` is now only an optional locator.
    """
    page_file = gl.REPO / str(page_path)
    if not page_file.suffix:
        page_file = page_file.with_suffix(".md")
    if not page_file.is_file():
        return {"located_edges": 0, "unlocated_edges": len(triples)}
    sections, _definitions = wl.parse_wiki_page(page_file)
    if not any(section.footnote_ids for section in sections):
        return {"located_edges": 0, "unlocated_edges": len(triples)}
    errors = wl.validate_wiki_page(page_file)
    if errors:
        raise ValueError("Wiki locator 校验失败: " + "; ".join(errors[:5]))

    attached = 0
    for triple in triples:
        wiki_source, _citations = wl.graph_wiki_source(
            page_file, triple.get("subject", ""), triple.get("object", ""))
        if not wiki_source:
            continue
        triple["source"] = wiki_source
        triple.pop("evidence_sources", None)
        triple.pop("evidence_quote", None)
        attached += 1
    return {
        "located_edges": attached,
        "unlocated_edges": len(triples) - attached,
    }


SEMANTIC_DIRECTION_HUB_THRESHOLD = 0.85


def resolve_semantic_direction_hub(conn, direction_name):
    """将语义槽给出的方向匹配到既有正式 Hub，不创建新 Hub。"""
    hubs = [
        (path, title) for path, title in conn.execute(
            "SELECT path, title FROM nodes WHERE type='hub' AND path LIKE '%/wiki/hubs/%'"
        )
        if Path(path).stem != "未归类关键词"
    ]
    exact = [hub_path for hub_path, title in hubs
             if direction_name == title or direction_name == hub_path]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    if not hubs:
        return None
    try:
        import numpy as np
        from embed_helper import embed_cached_batch
        vectors = embed_cached_batch(
            [direction_name, *[title for _, title in hubs]], cache_type="hub-direction"
        )
        direction_vector = vectors[0]
        direction_unit = direction_vector / (np.linalg.norm(direction_vector) + 1e-9)
        hub_vectors = vectors[1:]
        hub_units = hub_vectors / (np.linalg.norm(hub_vectors, axis=1, keepdims=True) + 1e-9)
        scores = hub_units @ direction_unit
        best_index = int(np.argmax(scores))
        if float(scores[best_index]) >= SEMANTIC_DIRECTION_HUB_THRESHOLD:
            return hubs[best_index][0]
    except Exception:
        pass
    return None


def normalize_semantic_directions(conn, triples, keywords, page_path, document_text=""):
    """方向三元组仅可指向既有 Hub；未匹配对象降级为关键词。
    LLM 直接填的方向边也过文本锚点检查(与 direction_matcher 派生路径一致)，
    无锚点的方向不写边，object 降级为 keyword，由 keyword→hub 兜底承担连通性。"""
    normalized = []
    direction_predicates = []
    from direction_matcher import direction_has_document_support as _direction_support
    for triple in triples:
        if triple["subject"] != page_path or triple["predicate"] not in DIRECTION_PREDICATES:
            normalized.append(triple)
            continue
        hub_path = resolve_semantic_direction_hub(conn, triple["object"])
        if hub_path:
            # 文本锚点检查(与 direction_matcher 派生路径一致):
            # 方向名或其 seeds 至少一个在 title/Navigation 字面出现, 否则降级为 keyword。
            _dir_name = hub_path.rsplit("/", 1)[-1]
            if _direction_support(_dir_name, document_text):
                triple = {**triple, "object": hub_path}
                normalized.append(triple)
                direction_predicates.append((hub_path, triple["predicate"]))
            else:
                keywords.append(triple["object"])
        else:
            keywords.append(triple["object"])
    keywords[:] = list(dict.fromkeys(keywords))
    return normalized, direction_predicates


def ensure_research_hub(conn, direction_name, page_path):
    """确保研究方向 hub 页 + 节点存在,返回 (hub page path, is_new)。
    方向名 → hub 页路径 <subproject>/wiki/hubs/<direction>.md。
    页不存在则创建(frontmatter + ## 关键词 空段);节点 UPSERT(type=hub, title=方向名),
    使 LLM 写的裸名方向(论文→属于(主/交叉)→方向名)经 title 解析命中 hub 节点。
    返回 is_new 标记本次是否新建(供调用者做 arXiv 合规校验)。
    """
    if not direction_name:
        return None, False
    parts = page_path.split("/")
    sub = parts[0] if parts else "academic"
    hub_path = f"{sub}/wiki/hubs/{direction_name}"
    hub_file = gl.REPO / (hub_path + ".md")
    is_new = False
    if not hub_file.exists():
        scope = load_arxiv_direction_scopes().get(direction_name, "")
        if not scope:
            return None, False
        hub_file.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        content = (
            f"---\ntitle: \"{direction_name}\"\ntype: topic-hub\nhub_subtype: research-direction\n"
            f"parent: null\n"
            f"status: active\ncreated: {today}\nupdated: {today}\n---\n\n"
            f"# {direction_name}\n\n## Scope\n\n{scope}\n"
        )
        hub_file.write_text(content, encoding="utf-8")
        is_new = True
    gl.ensure_node(
        conn, hub_path, direction_name, "hub", "", "", "current", 0,
        description=hs.read_hub_scope(hub_path),
    )
    return hub_path, is_new


def sync_hub_keywords_to_hub(hub_path, keywords):
    """把 keyword 写进指定 hub 页的 ## 关键词 段(去重追加)。返回新追加数。
    通用版: 接受 hub_path (已 ensure_research_hub 建)。
    """
    if not hub_path or not keywords:
        return 0
    hub_file = gl.REPO / (hub_path + ".md")
    if not hub_file.exists():
        return 0
    text = hub_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("## "):
            if ln.strip() == "## 关键词":
                start = i
            elif start is not None:
                end = i
                break
    if start is None:
        return 0
    if end is None:
        end = len(lines)
    existing = set()
    last_item = None
    for i in range(start, end):
        ls = lines[i].strip()
        if ls.startswith("- "):
            existing.add(ls[2:].strip())
            last_item = i
    added = [kw for kw in keywords if kw not in existing]
    if not added:
        return 0
    insert_at = (last_item + 1) if last_item is not None else end
    new_lines = lines[:insert_at] + [f"- {kw}" for kw in added] + lines[insert_at:]
    hub_file.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
    return len(added)


def find_keyword_hubs(conn, keyword):
    """查 keyword 已在哪些 hub 正文 ## 关键词 段出现过(历史归属,稳定事实)。
    返回 hub page path 列表。程序查表,零 token。
    """
    hubs = []
    for (hub_path,) in conn.execute("SELECT path FROM nodes WHERE type='hub' AND path LIKE '%/wiki/hubs/%'"):
        hub_file = gl.REPO / (hub_path + ".md")
        if not hub_file.exists():
            continue
        text = hub_file.read_text(encoding="utf-8")
        if f"- {keyword}" not in text:
            continue
        kws_lines = text.splitlines()
        in_kw = False
        for ln in kws_lines:
            if ln.strip().startswith("## "):
                in_kw = (ln.strip() == "## 关键词")
            elif in_kw and ln.strip() == f"- {keyword}":
                hubs.append(hub_path)
                break
    return hubs


def _get_descendant_dirs(conn, directions):
    """获取方向及其所有后代（沿子方向边，不算姻亲）。BFS 遍历。"""
    result = set(directions)
    queue = list(directions)
    while queue:
        d = queue.pop(0)
        children = [r[0] for r in conn.execute(
            "SELECT object FROM edges WHERE subject=? AND predicate='子方向'",
            (d,)
        ).fetchall()]
        for c in children:
            if c not in result:
                result.add(c)
                queue.append(c)
    return result


def _find_hub_by_title(conn, title):
    """按 title 精确查找既有研究方向 hub(零 embedding 成本)。
    子方向 hub 的 path 是自动分裂名(如 子方向-fc16f1),title 才是语义名;
    ensure_research_hub 仅按 path 判断存在,若不先按 title 命中,会为已存在的
    同名子方向再建一个根 hub(时变变分模拟 重复 hub 的根因,2026-08-24 修复)。"""
    row = conn.execute(
        "SELECT path FROM nodes WHERE type='hub' AND title=? "
        "AND path LIKE '%/wiki/hubs/%' LIMIT 1",
        (title,)
    ).fetchone()
    return row[0] if row else None


def assign_keyword_hubs(conn, keywords, direction_predicates, page_path):
    """keyword 归属两层判断。
    层1 已存在 keyword: 查历史 hub(稳定归属), 程序零 token——不重判。
    层2 新 keyword: 在候选范围(本论文研究方向+后代,不算姻亲)内 embedding 匹配
       → 归入匹配 hub(s,可多归属,满足近亲互斥)；未命中时归入主研究方向 hub 兜底。
    返回 (synced_count, unrecognized_directions, hub_fallback_kws)。
    hub_fallback_kws: direction 未命中、被主方向 hub 兜底接管的 keyword 列表。
    """
    if not keywords or not direction_predicates:
        return HubAssignResult(0, [], [])
    paper_dirs = [d for d, _ in direction_predicates]
    candidate_dirs = _get_descendant_dirs(conn, paper_dirs)
    candidate_hubs = {}
    unrecognized = []
    arxiv_names = load_arxiv_directions()
    for d, _pred in direction_predicates:
        existing_hub = conn.execute(
            "SELECT path FROM nodes WHERE (path=? OR title=?) AND type='hub'", (d, d)
        ).fetchone()
        if existing_hub:
            hp, is_new = existing_hub[0], False
        elif d in arxiv_names:
            hp, is_new = ensure_research_hub(conn, d, page_path)
        else:
            hp, is_new = None, False
        if hp:
            candidate_hubs[d] = hp
            if is_new and d not in arxiv_names:
                unrecognized.append(d)
    main_dir = direction_predicates[0][0] if direction_predicates else None
    main_hub = candidate_hubs.get(main_dir)
    synced = 0
    new_kws = []
    hub_fallback_kws = []
    for kw in keywords:
        existing = find_keyword_hubs(conn, kw)
        if existing:
            for hp in existing:
                synced += sync_hub_keywords_to_hub(hp, [kw])
        else:
            new_kws.append(kw)
    # 新 keyword: embedding 匹配方向(零 LLM token)
    if new_kws:
        try:
            from direction_matcher import classify_keywords
            # 限制候选范围: 本论文研究方向+后代(不算姻亲)
            kw_dirs, _dir_kw, _unmatched = classify_keywords(new_kws, candidate_dirs=candidate_dirs)
        except Exception:
            kw_dirs = {}
        for kw in new_kws:
            matched = kw_dirs.get(kw, [])
            if matched:
                for d, _score in matched:
                    hp = candidate_hubs.get(d)
                    if not hp:
                        hp = _find_hub_by_title(conn, d)
                        if hp:
                            candidate_hubs[d] = hp
                    if not hp:
                        hp, _ = ensure_research_hub(conn, d, page_path)
                        if hp:
                            candidate_hubs[d] = hp
                    if hp:
                        synced += sync_hub_keywords_to_hub(hp, [kw])
            elif main_hub:
                synced += sync_hub_keywords_to_hub(main_hub, [kw])
                hub_fallback_kws.append(kw)
    return HubAssignResult(synced, unrecognized, hub_fallback_kws)



def _align_propositions(sem_triples, conn, page_path):
    """proposition 对齐（统一对齐框架覆盖 proposition 颗粒度）。

    双阈值梯度（比 keyword 更保守，因误合并会污染事实层）：
    - cosine > 0.98：合并——新 proposition 用已有 proposition 名替换（不建重复节点）
    - 0.9 ≤ cosine ≤ 0.98：建关联边——new | 关联 | existing（各自保留独立，跨文档关系可见）
    - < 0.9：不动

    与 keyword 的 _dedup_semantic_keywords 平行，参数差异体现在阈值（0.98 vs 0.92）
    和多一档关联边（keyword 只有合并档）。
    """
    # 收集本次 sem_triples 中的 proposition object
    prop_predicates = PROPOSITION_PREDICATES | {"决策"}
    new_props = []
    for t in sem_triples:
        pred = t.get("predicate", "")
        if pred in prop_predicates and t.get("object"):
            new_props.append(t["object"])
    if not new_props:
        return {"merged": 0, "linked": 0, "mapping": {}}
    # 查图中已有 proposition 节点（排除本次页面自己的，避免自匹配）
    rows = conn.execute(
        "SELECT title FROM nodes WHERE type='entity' AND entity_subtype='proposition' "
        "AND title != '' AND title IS NOT NULL"
    ).fetchall()
    existing_props = [r[0] for r in rows if r[0]]
    if not existing_props:
        return {"merged": 0, "linked": 0, "mapping": {}}
    try:
        from embed_helper import match_by_embedding
    except Exception:
        return {"merged": 0, "linked": 0, "mapping": {}}
    # 先用低阈值(0.9)找出所有候选，再按分数分档
    matches = match_by_embedding(new_props, existing_props, threshold=0.9)
    if not matches:
        return {"merged": 0, "linked": 0, "mapping": {}}
    merge_map = {}   # new -> existing (>0.98 合并)
    link_pairs = []  # (new, existing) (0.9-0.98 关联边)
    for new_prop, (existing_prop, score) in matches.items():
        if score > 0.98:
            merge_map[new_prop] = existing_prop
        else:
            link_pairs.append((new_prop, existing_prop))
    # 应用合并：重写 sem_triples 的 object
    if merge_map:
        for t in sem_triples:
            obj = t.get("object", "")
            if obj in merge_map:
                t["object"] = merge_map[obj]
    # 应用关联边：新增 new | 关联 | existing 三元组
    for new_prop, existing_prop in link_pairs:
        sem_triples.append({
            "subject": new_prop, "predicate": "关联", "object": existing_prop,
            "confidence": "可追溯", "source": page_path, "is_sr": 0,
        })
    return {"merged": len(merge_map), "linked": len(link_pairs),
            "mapping": merge_map, "links": link_pairs}


def assign_keyword_hubs_meeting_admin(conn, keywords, page_path):
    """会议/行政 keyword 归属: 精确匹配 → embedding 匹配 hub seeds → catch-all。
    返回 HubAssignMeetingResult(synced_count, catch_all_added)。
    """
    synced = 0
    to_embed = []
    for kw in keywords:
        existing = find_keyword_hubs(conn, kw)
        if existing:
            for hp in existing:
                synced += sync_hub_keywords_to_hub(hp, [kw])
        else:
            to_embed.append(kw)
    # embedding 匹配 hub seeds(所有方向,零 LLM token)
    still_unmatched = list(to_embed)
    if to_embed:
        try:
            from direction_matcher import classify_keywords
            kw_dirs, _dir_kw, unmatched = classify_keywords(to_embed)
            for kw, dirs in kw_dirs.items():
                for d, _score in dirs:
                    hp, _ = ensure_research_hub(conn, d, page_path)
                    if hp:
                        synced += sync_hub_keywords_to_hub(hp, [kw])
            still_unmatched = list(unmatched)
        except Exception:
            pass
    # catch-all
    catch_all = 0
    if still_unmatched:
        catch_all = append_catch_all_keywords(still_unmatched)
    return HubAssignMeetingResult(synced, catch_all)


def append_catch_all_keywords(keywords):
    """将会议等无预定义方向的新主题词写入既有未归类队列，不建立图 Hub 边。"""
    hub_path = gl.REPO / "academic/wiki/hubs/未归类关键词.md"
    return sync_hub_keywords_to_hub("academic/wiki/hubs/未归类关键词", keywords) if hub_path.exists() else 0




def fill_defaults(triples, fm):
    """补 confidence/is_sr；边 locator 可选，不再从 Raw sources 强制派生。"""
    fm_cache = {}

    def get_fm_source(page_path):
        if page_path not in fm_cache:
            try:
                page_fm = gl.read_frontmatter(page_path)
                srcs = gl.parse_list_field(page_fm, "sources")
                st = page_fm.get("source_type", "")
                fm_cache[page_path] = (srcs, 1 if st == "speech-recognition" else 0)
            except Exception:
                fm_cache[page_path] = ([], 0)
        return fm_cache[page_path]

    default_sr = 1 if fm.get("source_type", "") == "speech-recognition" else 0

    for t in triples:
        if not t.get("confidence"):
            t["confidence"] = "推断" if t.get("predicate") in STRUCTURAL_PREDICATES else "可追溯"
        page = t.pop("page", None)
        if page:
            _page_sources, sr = get_fm_source(page)
            t.setdefault("is_sr", sr)
        else:
            t.setdefault("is_sr", default_sr)
        if not t.get("source") and t.get("locator"):
            t["source"] = str(t["locator"]).strip()
        t["source"] = str(t.get("source") or "").strip()
        t.pop("evidence_sources", None)
    return triples


# ===== 页面节点 + alias =====

def upsert_page_node(conn, page_path):
    """UPSERT 一个 page/hub/timeline-summary 节点 + alias 自动识别(层1)。"""
    fm = gl.read_frontmatter(page_path)
    ptype = fm.get("type", "")
    if ptype == "topic-hub":
        node_type = "hub"
    elif ptype == "timeline-summary":
        node_type = "timeline-summary"
    elif ptype == "people":
        node_type = "people"
    else:
        node_type = "page"
    title = fm.get("title", Path(page_path).name)
    source_type = fm.get("source_type", "")
    date = str(fm.get("date", ""))
    status = fm.get("status", "current")
    has_raw = 1 if gl.has_raw_source(fm) else 0
    description = hs.read_hub_scope(page_path) if node_type == "hub" else None
    # 写入当前管道版本戳（re_ingest --outdated 据此判断是否需重新摄入）
    gl.ensure_node(conn, page_path, title, node_type, source_type, date, status, has_raw,
                   ingest_version=gl.CURRENT_PIPELINE_VERSION, description=description)
    aliases = gl.extract_aliases_from_md(page_path)
    conflicts = gl.insert_aliases(conn, page_path, aliases)
    return node_type, conflicts


# ===== Raw 文档包节点 + Wiki 来源边 =====

def ensure_raw_support_edge(conn, page_path):
    """从 Wiki sources 机械建 Raw 文档包节点和 ``Wiki → 来源 → Raw`` 边。

    sources 如 ["academic/raw/works/papers/2010-ltrg/paper.md"]
    → Raw 节点路径 = "academic/raw/works/papers/2010-ltrg/paper"
    → 原件 paper.pdf 与 locator companion paper.md 通过同词干归为一个节点
    → 建 ``page --来源--> raw``；locator 只在 source 自带 ``#...`` 时可选记录。
    """
    fm = gl.read_frontmatter(page_path)
    sources = gl.parse_list_field(fm, "sources")
    if not sources:
        return []
    raw_nodes = []
    for src in sources:
        raw_path = gl.raw_node_path(src, page_path)
        if not raw_path or raw_path == page_path:
            continue
        if raw_path in raw_nodes:
            continue
        title = gl.raw_node_title(raw_path)
        gl.ensure_node(conn, raw_path, title, "raw", "", "", "current", 0)
        # Every same-stem file path resolves to this Raw package node via aliases.
        qualified_source_file = gl.raw_file_path(src, page_path)
        aliases = [qualified_source_file]
        source_target = gl.REPO / qualified_source_file
        if source_target.parent.is_dir():
            aliases.extend(
                str(candidate.relative_to(gl.REPO))
                for candidate in source_target.parent.glob(f"{source_target.stem}.*")
                if candidate.is_file()
            )
        for alias in dict.fromkeys(alias for alias in aliases if alias and alias != raw_path):
            conn.execute(
                "INSERT OR IGNORE INTO aliases(alias,node_path) VALUES (?,?)",
                (alias, raw_path),
            )

        # Remove the old reverse model locally when this page is re-ingested.
        old_edges = conn.execute(
            "SELECT id FROM edges WHERE subject=? AND predicate='事实支撑' AND object=?",
            (raw_path, page_path),
        ).fetchall()
        for old_edge in old_edges:
            conn.execute("DELETE FROM edge_evidence WHERE edge_id=?", (old_edge["id"],))
            conn.execute("DELETE FROM edge_origins WHERE edge_id=?", (old_edge["id"],))
            conn.execute("DELETE FROM edges WHERE id=?", (old_edge["id"],))

        locator = str(src).strip() if "#" in str(src) else ""
        existing = conn.execute(
            "SELECT id FROM edges WHERE subject=? AND predicate=? AND object=?",
            (page_path, "来源", raw_path),
        ).fetchone()
        if existing:
            if locator:
                conn.execute(
                    "UPDATE edges SET source=CASE WHEN COALESCE(source,'')='' THEN ? ELSE source END WHERE id=?",
                    (locator, existing["id"]),
                )
            gl.add_edge_origin(conn, existing["id"], page_path, locator)
        else:
            conn.execute(
                "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                "VALUES (?,?,?,?,?,?)",
                (page_path, "来源", raw_path, gl.DEFAULT_CONFIDENCE, locator, 0),
            )
            edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            gl.add_edge_origin(conn, edge_id, page_path, locator)
        raw_nodes.append(raw_path)
    return raw_nodes


# ===== 连通性兜底 =====

def navigation_connectivity_candidates(triples, page_path, corresponding):
    """报告未由论文核心导航边直连的概念，不伪造“研究关键词”事实边。"""
    # 已有直连的概念集（page 作为 subject，KW 谓词的 object）
    connected = set()
    for t in triples:
        if (t.get("subject") == page_path
                and t.get("predicate") in KW_PREDICATES):
            connected.add(t.get("object", "").strip())
    # 人物谓词 → subject 是人名，排除
    person_preds = {"作者", "通讯作者", "指导", "师从", "受指导于", "任职于", "参会", "所属", "第一作者"}
    # 方向谓词 → object 是 hub，排除
    direction_preds = DIRECTION_PREDICATES
    # 元数据谓词 → object 是期刊名等非研究概念（论文→发表于→期刊名），排除
    metadata_object_preds = {"发表于"}
    # 收集需要连通的概念
    candidates = []
    seen = set()
    for t in triples:
        for role in ("subject", "object"):
            name = t.get(role, "").strip()
            if not name or name == page_path or name == "本论文":
                continue
            if name in seen:
                continue
            seen.add(name)
            # 排除人名：人物谓词的 subject
            if role == "subject" and t.get("predicate") in person_preds:
                continue
            # 排除方向 hub：论文→方向谓词 的 object（subject 须是 page 才算方向边）
            if role == "object" and t.get("subject") == page_path and t.get("predicate") in direction_preds:
                continue
            # 排除元数据 object：期刊名等非研究概念（论文→发表于→期刊名）
            if role == "object" and t.get("subject") == page_path and t.get("predicate") in metadata_object_preds:
                continue
            # 排除已有直连的
            if name in connected:
                continue
            # 排除结构边（包含/相似）：两端经复合节点桥接或对称概念边，不需补 page→concept
            if t.get("predicate") in STRUCTURAL_PREDICATES:
                continue
            candidates.append(name)
    return list(dict.fromkeys(candidates))


def ensure_keyword_connectivity(triples, page_path, corresponding):
    """兼容旧调用；稀疏导航模式不再自动补论文级研究关键词边。"""
    navigation_connectivity_candidates(triples, page_path, corresponding)
    return 0


# ===== 知识边入库 =====

def _ensure_entity_node(conn, name, node_path, entity_subtype, title_idx, alias_idx, t, key):
    """建 entity 节点的公共流程：ensure_node + alias + 索引更新 + t[key] 重写。
    person/proposition/keyword 三路共享，消除重复。返回更新后的 node_path。
    """
    gl.ensure_node(conn, node_path, name, "entity", entity_subtype=entity_subtype)
    if node_path != name:
        # 注册原始名 + 拆解出的多形态 alias（中文/英文/缩写各自独立登记）
        aliases = [name] + gl.decompose_name_to_aliases(name)
        gl.insert_aliases(conn, node_path, aliases)
        t[key] = node_path
        title_idx.setdefault(name, []).append(node_path)
        # proposition 是描述性命题,非概念端点;其 decompose alias 不进 resolve 索引
        # (避免句内片段如「MPS」与概念 alias 冲突造成 resolve 歧义,同 build_name_index)
        if entity_subtype != "proposition":
            for a in aliases:
                alias_idx.setdefault(a, []).append(node_path)
    return node_path


def _is_proposition_slot(name, triple, key):
    """判断 name 是否应建为 proposition 节点（三路判据，方向 A）。

    1. 命题谓词（核心创新点/局限性/未来展望）的 object → 直接判 proposition：
       谓词本身已表明是论断，跳过 is_descriptive_phrase 的长度+trigger 双门槛
       （22字含「证明」的短命题不被长度门槛误拦）。
    2. 结构性谓词（包含）的 subject → proposition（包含边 subject 按定义是命题）。
    3. 其他（自由边 object/subject）→ 回退 is_descriptive_phrase（trigger+长度门槛兜底）。
    """
    pred = triple.get("predicate", "")
    if pred in PROPOSITION_PREDICATES and key == "object":
        # 谓词本身已定义语义角色（局限性/创新点/展望 = 论断），直接判 proposition
        # 不走 has_predicate_structure（只认中文触发词，漏 NP-hard 等英文谓词含义）
        return True
    # 会议决策谓词的 object 是论断（会议说了什么），作 proposition 节点
    if pred == "决策" and key == "object":
        return has_predicate_structure(name)
    if pred == "包含" and key == "subject":  # 仅包含;相似(对称概念边)subject 非命题
        return True
    return is_descriptive_phrase(name)


def _build_subgraph(triples, page_path):
    """子图构建（纯函数，零 DB 依赖）：残片过滤 + 审计。

    输入 triples: [{subject, predicate, object, ...}]
    返回 (normalized_triples, descriptive_warns)
    - normalized_triples: 通过过滤的三元组（逗号拆分在 ingest_paper normalize_slots 侧完成）
    - descriptive_warns: 审计 warning（非阻断，供统计/LINT）

    职责边界：只做纯数据变换，不 resolve、不建节点、不建边。
    """
    normalized = []
    warns = []
    for t in triples:
        t = dict(t)
        subj_raw = t.get("subject", "").strip()
        obj_raw = t.get("object", "").strip()
        # 引文残片跳过：MinerU 把参考文献条目误建为实体，不建节点不连边
        if is_citation_fragment(subj_raw) or is_citation_fragment(obj_raw):
            warns.append({"subject": subj_raw, "predicate": t.get("predicate", ""),
                          "object": obj_raw, "issue": "citation_fragment"})
            continue
        # 碎片 token 跳过：LLM/OCR 把单词拆出的裸小写 ASCII 残片（如 Rényi→nyi）
        # subject 排除 page_path（页面自身标识，非实体）
        if (subj_raw != page_path and is_fragment_token(subj_raw)) or is_fragment_token(obj_raw):
            warns.append({"subject": subj_raw, "predicate": t.get("predicate", ""),
                          "object": obj_raw, "issue": "fragment_token"})
            continue
        pred = t.get("predicate", "")
        # 描述性短语审计（非阻断）：结构性谓词+命题谓词跳过
        # ADR-003: 命题谓词(核心创新点/局限性/未来展望/决策)的 object 本身是论断,跳过
        if pred not in STRUCTURAL_PREDICATES and pred not in PROPOSITION_PREDICATES \
                and pred not in TITLE_OBJECT_PREDICATES and pred != "决策" \
                and is_descriptive_phrase(obj_raw):
            warns.append({"subject": t.get("subject", ""), "predicate": pred,
                          "object": obj_raw, "issue": "descriptive_phrase"})
        # keyword 裸缩写校验（缩写须在括号内，格式「中文英文(缩写)」）
        if pred in KW_PREDICATES and is_bare_abbreviation(obj_raw):
            warns.append({"subject": t.get("subject", ""), "predicate": pred,
                          "object": obj_raw, "issue": "bare_abbreviation",
                          "field": "object", "value": obj_raw})
        # 自由边（非 keyword 谓词）裸缩写校验：subject/object 含英文缩写但无括号释义。
        # 跳过结构性谓词(包含/相似)：其端点来自命题/概念节点，已在命题谓词或 keyword
        # 谓词审计过，重复检查只会对同一 proposition 的多条包含边重复告警(噪声)。
        if pred not in KW_PREDICATES and pred not in STRUCTURAL_PREDICATES \
                and pred not in METADATA_PREDICATES:
            for _field, _val in (("subject", subj_raw), ("object", obj_raw)):
                if _val and is_bare_abbreviation(_val):
                    warns.append({"subject": subj_raw, "predicate": pred,
                                  "object": obj_raw, "issue": "bare_abbreviation",
                                  "field": _field, "value": _val})
        normalized.append(t)
    return normalized, warns




def bare_tokens_resolvable(text, conn, title_idx, alias_idx, suffix_idx) -> bool:
    """文本中的裸缩写 token 是否全部能 resolve 到 keyword 节点。
    slot 校验与图融合期复查的公共判定：缩写已注册 alias → 已建立关联，不 warn。
    无 token 返回 False（交回 is_bare_abbreviation 原始判定，保留 warning）。
    resolve 到 proposition 不算（非概念端点，防歧义）。
    """
    if not text:
        return False
    no_paren = re.sub(r"[（(][^)）]*[)）]", "", text)
    tokens = re.findall(r"[A-Z]{2,}[A-Za-z0-9]*", no_paren)
    if not tokens:
        return False
    for tok in tokens:
        resolved, _ambig = gl.resolve_bare_name(tok, title_idx, alias_idx, suffix_idx)
        if not resolved:
            return False
        _r = conn.execute(
            "SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)
        ).fetchone()
        if not _r or _r["entity_subtype"] != "keyword":
            return False
    return True


def _revisit_bare_abbreviations(warns, conn, title_idx, alias_idx, suffix_idx):
    """融合期复查 bare_abbreviation warning：调 bare_tokens_resolvable 公共判定。

    语义从「含裸缩写字符串」改为「含无法 resolve 到 keyword 的裸缩写」：
    缩写能 resolve（图里已有对应 keyword 节点/alias）→ 已建立关联，不 warn；
    resolve miss → 保留 warning，交后置 agent 补全全称及中文。
    """
    out = []
    for w in warns:
        if w.get("issue") != "bare_abbreviation":
            out.append(w)
            continue
        field = w.get("field", "object")
        text = w.get(field, "") or w.get("object", "") or ""
        if bare_tokens_resolvable(text, conn, title_idx, alias_idx, suffix_idx):
            continue
        out.append(w)
    return out


def _surface_occurs(text, surface):
    """精确检查概念 surface 是否出现在命题中；ASCII 使用词边界。"""
    surface = (surface or "").strip()
    if not surface or len(surface) < 2:
        return False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+_\-/]*", surface):
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(surface)}(?![A-Za-z0-9])", text,
            flags=re.IGNORECASE,
        ) is not None
    return surface in text


def _existing_concept_surface_index(conn):
    """返回唯一精确 title/alias → 既有 concept path；歧义 surface 静默排除。"""
    surfaces = {}
    for row in conn.execute(
        "SELECT path, title FROM nodes WHERE type='entity' "
        "AND entity_subtype IN ('keyword','concept')"
    ):
        title = (row["title"] or "").strip()
        if title:
            surfaces.setdefault(title, set()).add(row["path"])
    for row in conn.execute(
        "SELECT a.alias, a.node_path FROM aliases a JOIN nodes n ON n.path=a.node_path "
        "WHERE n.type='entity' AND n.entity_subtype IN ('keyword','concept')"
    ):
        alias = (row["alias"] or "").strip()
        if alias:
            surfaces.setdefault(alias, set()).add(row["node_path"])
    return {
        surface: next(iter(paths))
        for surface, paths in surfaces.items()
        if len(paths) == 1
    }


def _sparse_proposition_targets(prop_text, confirmed_concepts, existing_surfaces):
    """确定性找 proposition 的概念目标，不创建新节点、不返回歧义候选。"""
    targets = set()
    for name, node_path in confirmed_concepts.items():
        surfaces = [name, *gl.decompose_name_to_aliases(name)]
        if any(_surface_occurs(prop_text, surface) for surface in surfaces):
            targets.add(node_path)
    for surface, node_path in existing_surfaces.items():
        if _surface_occurs(prop_text, surface):
            targets.add(node_path)
    return targets


def add_knowledge_edges(conn, page_path, triples, page_source_note="", attach_plan=None):
    """按 GraphDelta attach plan 编译节点并写知识边。

    GraphDelta 已在调用方完成规范化、确定性对齐计划和 query probes；本函数负责：
    - `_build_subgraph` 做残片过滤与既有描述性检查；
    - 按 attach plan 复用唯一目标，歧义/未命中保持本地节点；
    - 建节点、去重、写边及派生的包含关系。
      · 第一遍扫描建 concept_map（概念全名→keyword_id，供 proposition path 计算）
      · 第二遍扫描：trigger 路径→proposition 节点（extract_descriptive_id 算 path），
        否则→keyword 节点（extract_keyword_id 算 path）；包含边跳过审计

    triples: [{subject, predicate, object, confidence, source, is_sr}]
    返回 IngestResult。
    """
    # ── 阶段一:子图构建（纯数据变换） ──
    normalized_triples, descriptive_warns = _build_subgraph(triples, page_path)

    # ── 阶段二:融合进主图 ──
    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    existing_concept_surfaces = _existing_concept_surface_index(conn)
    confirmed_concept_names = {
        t.get("object", "").strip()
        for t in normalized_triples
        if t.get("subject", "").strip() == page_path
        and t.get("predicate") in CONCEPT_KW_PREDICATES
        and t.get("object", "").strip()
    }
    # GraphDelta 路径由确定性 attach plan 决定，不允许 embedding 改写融合目标。
    planned = {
        item["mention"]: item
        for item in (attach_plan or {}).get("decisions", [])
    }
    # 身份上下文不足时不猜、不制造同名碰撞节点：整条歧义边软跳过。
    abstained = set((attach_plan or {}).get("abstained", []))
    skipped_ambiguous_mentions = {
        endpoint
        for triple in normalized_triples
        for endpoint in (triple.get("subject", "").strip(), triple.get("object", "").strip())
        if endpoint in abstained
    }
    if abstained:
        normalized_triples = [
            triple for triple in normalized_triples
            if triple.get("subject", "").strip() not in abstained
            and triple.get("object", "").strip() not in abstained
        ]
    # 第一遍扫描：建 concept_map（概念全名→keyword_id），供 proposition path 替换内嵌概念
    concept_map = {}
    for t in normalized_triples:
        for key in ("subject", "object"):
            name = t.get(key, "").strip()
            # 仅概念（非命题）进 concept_map：命题谓词 object / 包含边 subject 是命题，排除
            if name and not _is_proposition_slot(name, t, key):
                kid = gl.extract_keyword_id(name)
                if kid:
                    concept_map[name] = kid
    # abbr_map：图里已有 keyword 的纯缩写 alias → node_path，供命题 path 把裸缩写替换为 keyword id。
    # 排除 concept_map 的 kid（避免全名→kid→node_path 回环）；仅指向 keyword 节点的纯缩写 alias。
    concept_kids = set(concept_map.values())
    abbr_map = {}
    for alias, paths in alias_idx.items():
        if not re.fullmatch(r"[A-Z]{2,}[A-Za-z0-9]*", alias):
            continue
        if alias in concept_kids or len(paths) != 1:
            continue
        node_path = paths[0]
        _r = conn.execute(
            "SELECT type, entity_subtype FROM nodes WHERE path=?", (node_path,)
        ).fetchone()
        if _r and _r["type"] == "entity" and _r["entity_subtype"] == "keyword":
            abbr_map[alias] = node_path
    # 本批新建 keyword 节点（全名→path），供第二层传递包含扫描
    new_keywords = {}
    propositions = []  # (prop_path,完整 prop_text)
    flat = []
    resolve_hits = nodes_created = 0
    resolve_ambig = len(skipped_ambiguous_mentions)
    for t in normalized_triples:
        t = dict(t)
        subj_raw = t.get("subject", "").strip()
        obj_raw = t.get("object", "").strip()
        for key in ("subject", "object"):
            name = t.get(key, "").strip()
            if not name:
                continue
            decision = planned.get(name)
            if decision and decision["action"].startswith("reuse"):
                target = decision["target"]
                t[key] = target
                if is_person_reference(t, key):
                    conn.execute(
                        "UPDATE nodes SET entity_subtype='person' "
                        "WHERE path=? AND type='entity' AND "
                        "COALESCE(entity_subtype,'') IN ('','keyword','concept')",
                        (target,),
                    )
                elif _is_proposition_slot(name, t, key):
                    conn.execute(
                        "UPDATE nodes SET entity_subtype='proposition' "
                        "WHERE path=? AND type='entity' AND "
                        "(entity_subtype IS NULL OR entity_subtype='')",
                        (target,),
                    )
                resolve_hits += 1
                continue
            if decision and decision["action"].startswith("abstain"):
                # 正常情况下已在上方整边过滤；保留防御性分支。
                continue
            if gl.node_exists(conn, name):
                if is_person_reference(t, key):
                    conn.execute(
                        "UPDATE nodes SET entity_subtype='person' "
                        "WHERE path=? AND type='entity' AND "
                        "COALESCE(entity_subtype,'') IN ('','keyword','concept')",
                        (name,),
                    )
                elif _is_proposition_slot(name, t, key):
                    # 历史节点已存在但无 subtype（改革前摄入）：补 proposition 标记
                    conn.execute(
                        "UPDATE nodes SET entity_subtype='proposition' "
                        "WHERE path=? AND type='entity' AND "
                        "(entity_subtype IS NULL OR entity_subtype='')",
                        (name,),
                    )
                continue
            if decision:
                # create_local 不做近似匹配或自动 merge。
                resolved, ambig = None, False
            else:
                resolved, ambig = gl.resolve_bare_name(name, title_idx, alias_idx, suffix_idx)
            # proposition 不应被概念 resolve 命中（归一化匹配会因 abbr 相同误匹配：
            # 如概念「矩阵乘积态...(MPS)」abbr=MPS 误命中命题 title 含 (MPS)）。discard 继续建 keyword。
            if resolved:
                _r = conn.execute("SELECT entity_subtype FROM nodes WHERE path=?", (resolved,)).fetchone()
                if _r and _r["entity_subtype"] == "proposition":
                    resolved = None
            if resolved:
                t[key] = resolved
                resolve_hits += 1
            else:
                subtype = "person" if is_person_reference(t, key) else None
                if subtype:
                    # 人物节点直接用原名
                   gl.ensure_node(conn, name, name, "entity", entity_subtype=subtype)
                elif _is_proposition_slot(name, t, key):
                    # proposition 节点：用 extract_descriptive_id 算 path（替换内嵌概念为 ID）
                    prop_path = gl.extract_descriptive_id(name, concept_map, abbr_map)
                    existing_title = conn.execute(
                        "SELECT title FROM nodes WHERE path=?", (prop_path,)
                    ).fetchone()
                    if existing_title and existing_title[0] != name:
                        prop_path = name
                    _ensure_entity_node(conn, name, prop_path, "proposition", title_idx, alias_idx, t, key)
                else:
                    # keyword 节点
                    keyword_id = gl.extract_keyword_id(name)
                    if keyword_id != name:
                        existing_title = conn.execute(
                            "SELECT title FROM nodes WHERE path=?", (keyword_id,)
                        ).fetchone()
                        if existing_title and existing_title[0] != name:
                            keyword_id = name
                    _ensure_entity_node(conn, name, keyword_id, "keyword", title_idx, alias_idx, t, key)
                    new_keywords[name] = t[key]
                if ambig:
                    resolve_ambig += 1
                else:
                    nodes_created += 1
            if not _is_proposition_slot(name, t, key):
                concept_map[name] = t[key]
        if t.get("predicate") in (PROPOSITION_PREDICATES | {"决策"}) \
                and _is_proposition_slot(obj_raw, t, "object"):
            propositions.append((t["object"], obj_raw))
        flat.append((page_path, t))
    skip_dedup = gl.dedup_batch(flat, conn)
    added = skipped_dup = 0
    for i, (_, t) in enumerate(flat):
        if i in skip_dedup:
            continue
        exists = conn.execute(
            "SELECT id FROM edges WHERE subject=? AND predicate=? AND object=?",
            (t["subject"], t["predicate"], t["object"])
        ).fetchone()
        if exists:
            locator = str(t.get("source") or "")
            if locator:
                conn.execute(
                    "UPDATE edges SET source=CASE WHEN COALESCE(source,'')='' THEN ? ELSE source END WHERE id=?",
                    (locator, exists["id"]),
                )
            gl.add_edge_origin(conn, exists["id"], page_path, locator)
            skipped_dup += 1
            continue
        conf = t.get("confidence") or gl.DEFAULT_CONFIDENCE
        conn.execute(
            "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)",
            (t["subject"], t["predicate"], t["object"], conf,
             t.get("source", ""), 1 if t.get("is_sr") else 0)
        )
        edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        gl.add_edge_origin(conn, edge_id, page_path, str(t.get("source") or ""))
        added += 1
    # 第二层传递包含（代码匹配）：本批新建 keyword 全名扫描 concept_map，
    # 若含其他概念全名（非自身）则建 concept | 包含 | subconcept 边。
    # 匹配范围 = concept_map（已有 keyword ∪ 本批新概念），确定性、零 LLM 成本。
    for name, kid in new_keywords.items():
        for other_name, other_kid in concept_map.items():
            if other_name == name or other_kid == kid:
                continue  # 防自包含
            if other_name in name and len(other_name) >= 2:
                if not gl.node_exists(conn, kid) or not gl.node_exists(conn, other_kid):
                    continue
                # 已存在则跳过（避免重复建边）
                exists = conn.execute(
                    "SELECT id FROM edges WHERE subject=? AND predicate=? AND object=?",
                    (kid, "包含", other_kid)
                ).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                        "VALUES (?,?,?,?,?,?)",
                        (kid, "包含", other_kid, "推断", page_source_note, 0)
                    )
                    edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    added += 1
                else:
                    edge_id = exists["id"]
                gl.add_edge_origin(conn, edge_id, page_path, page_source_note)
    # proposition → concept 稀疏包含边（程序化、零 LLM）。候选只来自：
    # 1) 本页已确认概念及其确定性中英/缩写形态；2) 主图唯一精确 title/alias。
    # 未命中或歧义静默跳过，绝不从 proposition 片段创建 concept。
    confirmed_concepts = {
        name: concept_map[name]
        for name in confirmed_concept_names
        if name in concept_map and gl.node_exists(conn, concept_map[name])
    }
    for prop_path, prop_text in dict.fromkeys(propositions):
        for concept_path in _sparse_proposition_targets(
                prop_text, confirmed_concepts, existing_concept_surfaces):
            if concept_path == prop_path or not gl.node_exists(conn, concept_path):
                continue
            exists = conn.execute(
                "SELECT id FROM edges WHERE subject=? AND predicate=? AND object=?",
                (prop_path, "包含", concept_path)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                    "VALUES (?,?,?,?,?,?)",
                    (prop_path, "包含", concept_path, "推断", page_source_note, 0)
                )
                edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                added += 1
            else:
                edge_id = exists["id"]
            gl.add_edge_origin(conn, edge_id, page_path, page_source_note)
    descriptive_warns = _revisit_bare_abbreviations(
        descriptive_warns, conn, title_idx, alias_idx, suffix_idx)
    return IngestResult(added, len(skip_dedup), skipped_dup, resolve_hits, resolve_ambig, nodes_created, descriptive_warns)


def is_person_reference(triple, role):
    """判断新建裸实体是否由人物关系指向，避免把所有 entity 当人物。"""
    predicate = triple.get("predicate", "")
    if predicate in {"指导", "师从", "受指导于"}:
        return True  # 师生关系：主体和客体都是人
    if predicate in {"第一作者", "作者", "通讯作者", "任职于", "所属"}:
        return role == "subject"  # 主体是人，客体是论文/机构
    if predicate in {"参会", "汇报", "待办"}:
        return role == "subject"  # 主体是人，客体是会议/议题/任务
    return False


def merge_nodes(conn, src_node, tgt_node):
    """把 src_node 合并到 tgt_node:迁移边 + 加 alias + 去重 + 删 src 节点。
    用于中英文同名/重复节点合并。返回去重条数。"""
    conn.execute("UPDATE edges SET subject=? WHERE subject=?", (tgt_node, src_node))
    conn.execute("UPDATE edges SET object=? WHERE object=?", (tgt_node, src_node))
    dups = conn.execute(
        "SELECT subject, predicate, object, MIN(id) as keep_id FROM edges "
        "WHERE subject=? GROUP BY subject, predicate, object HAVING COUNT(*) > 1",
        (tgt_node,)
    ).fetchall()
    dup_count = 0
    for s, p, o, keep_id in dups:
        deleted = conn.execute(
            "DELETE FROM edges WHERE subject=? AND predicate=? AND object=? AND id != ?",
            (s, p, o, keep_id)
        )
        dup_count += deleted.rowcount
    conn.execute(
        "INSERT OR IGNORE INTO aliases (alias, node_path) VALUES (?, ?)",
        (src_node, tgt_node),
    )
    for row in list(conn.execute("SELECT alias FROM aliases WHERE node_path=?", (src_node,))):
        conn.execute(
            "INSERT OR IGNORE INTO aliases(alias,node_path) VALUES (?,?)",
            (row["alias"], tgt_node),
        )
    conn.execute("DELETE FROM aliases WHERE node_path=?", (src_node,))
    conn.execute("DELETE FROM nodes WHERE path=?", (src_node,))
    return dup_count


# ===== 命令入口 =====

def cmd_ingest(args):
    conn = _connect_for(args)
    page = args.page.removesuffix(".md")
    # --clean: re-ingest 模式，清本页旧边后重建（单事务原子，无风险窗口）
    if getattr(args, "clean", False):
        clean_report = clean_page_edges(conn, page)
        report = {"cleaned_page": page, **clean_report}
    else:
        report = {}
    # 同步 keyword 别名（幂等），确保 resolve 命中已知变体
    try:
        import sync_keyword_aliases
        _g, _a, _s = sync_keyword_aliases.sync(conn, apply=True)
        if _a:
            report["keyword_aliases_synced"] = _a
    except Exception:
        pass
    # 兜底清理 ghost hub（.md 已删但节点残留），每次摄入前清扫保持图整洁
    ghost = cleanup_ghost_hubs(conn)
    if ghost:
        report["ghost_hubs_cleaned"] = ghost
    # 图健康前置检查：清理孤儿 alias（指向已删节点）+ 孤儿边（引用已删节点）
   # 防止上一篇摄入/回滚遗留的脏数据阻塞下一篇的外键约束
    integrity = cleanup_orphan_references(conn)
    if integrity:
        report["graph_integrity_cleaned"] = integrity
    node_type, conflicts = upsert_page_node(conn, page)
    report.update({"page": page, "node_type": node_type, "alias_conflicts": conflicts})

    # Raw 文档包节点 + Wiki→来源→Raw（机械、零 token）
    raw_nodes = ensure_raw_support_edge(conn, page)
    if raw_nodes:
        report["raw_nodes"] = raw_nodes

    # 轻量时态事实：仅对明确会过期的页面类型，按 frontmatter 有效期写入。
    # 普通语义边和 temporal_facts 分表保存，不改变 neighbors/search 的导航语义。
    temporal_report = sync_page_temporal_fact(conn, page, gl.read_frontmatter(page))
    if temporal_report.get("removed") or temporal_report.get("added"):
        report["temporal_facts_removed"] = temporal_report["removed"]
        report["temporal_facts_added"] = temporal_report["added"]
    if temporal_report.get("warnings"):
        report["temporal_warnings"] = temporal_report["warnings"]

    # auto-merge: 检测是否存在与本页 title/alias 同名的 citation-only entity 节点
    # (摄入论文时,若该论文此前作为引文节点存在,自动吸收:迁移引用边+加alias+删引文节点)
    fm = gl.read_frontmatter(page)
    page_title = fm.get("title", "")
    page_aliases = gl.parse_list_field(fm, "authors")  # 作者不作为论文节点匹配键
    # 从 aliases 表查本页已注册的 alias
    registered_aliases = [r[0] for r in conn.execute(
        "SELECT alias FROM aliases WHERE node_path=?", (page,))]
    match_keys = set()
    if page_title:
        match_keys.add(page_title)
    match_keys.update(registered_aliases)
    # 也试 paper-id (路径最后一段,如 orus-2008-itebd-beyond-unitary)
    match_keys.add(page.split("/")[-1])

    merged = []
    for key in match_keys:
        if not key:
            continue
        # 查 entity 节点(path 或 title 匹配,且 type=entity 即无页的引文/概念节点)
        for row in conn.execute(
            "SELECT path FROM nodes WHERE type='entity' AND (path=? OR title=?)",
            (key, key)
        ):
            entity_path = row[0]
            if entity_path and entity_path != page:
                dups = merge_nodes(conn, entity_path, page)
                merged.append({"absorbed": entity_path, "dups_removed": dups})
    if merged:
        report["auto_merged"] = merged

    if args.semantic:
        # 预填+语义模式
        fm = gl.read_frontmatter(page)
        text = (gl.REPO / (page + ".md")).read_text(encoding="utf-8")
        nav = extract_section_text(text, "Navigation")
        mechanical = extract_mechanical_edges(page)
        sem_text = Path(args.semantic).read_text(encoding="utf-8")
        sem_triples, keywords, main_dir, corresponding, cross_dirs, dir_preds = parse_semantic_text(sem_text, page)
        _is_paper = fm.get("type") == "paper-summary" or page.startswith("academic/wiki/papers/")
        if _is_paper:
            # 新论文不接受语义槽自报方向或通用“研究关键词”标签。方向只由可定位的
            # 研究方向定位句与 Hub Scope 路由；其余有明确谓词的概念边仍可保留。
            retired_triples = [
                triple for triple in sem_triples
                if (triple.get("subject") == page and triple.get("predicate") in DIRECTION_PREDICATES)
                or triple.get("predicate") == "研究关键词"
            ]
            sem_triples = [triple for triple in sem_triples if triple not in retired_triples]
            if retired_triples:
                report["retired_semantic_tags_ignored"] = len(retired_triples)
            keywords = [
                triple["object"] for triple in sem_triples
                if triple.get("subject") == page
                and triple.get("predicate") in CONCEPT_KW_PREDICATES
            ]
            dir_preds = []
        elif not _get_domain_from_path(page) and fm.get("type") != "conference-summary":
            _doc_text = f"{fm.get('title', '')}\n{nav}"
            sem_triples, dir_preds = normalize_semantic_directions(conn, sem_triples, keywords, page, _doc_text)
        sem_format_warns = detect_inline_section_headers(sem_text)
        # proposition 对齐: 统一对齐框架覆盖 proposition 颗粒度
        # >0.98 合并节点, 0.9-0.98 建关联边(比 keyword 更保守,防污染事实层)
        _prop_report = _align_propositions(sem_triples, conn, page)
        if _prop_report["merged"] or _prop_report["linked"]:
            report["proposition_align"] = _prop_report
        # 作者角色替换:从机械边中移除第一作者/通讯作者的"作者"边(已有专属谓词)
        mechanical = [
            t for t in mechanical
            if not (t["predicate"] == "作者" and t["subject"] in corresponding)
        ]
        if _is_paper:
            scope_route = hs.route_paper(conn, page)
            report["hub_scope_route"] = scope_route
            if scope_route.get("decision") == "resolved":
                hub_path = scope_route["node_id"]
                dir_preds = [(hub_path, "主要研究")]
                sem_triples.append({
                    "subject": page,
                    "predicate": "主要研究",
                    "object": hub_path,
                    "source": scope_route["profile"]["locator"],
                    "subject_is_canonical": True,
                    "object_is_canonical": True,
                })
        # 论文→方向边: 检查谓词 tier。Hub 关键词与 catch-all 已退休；
        # 下方的零值字段仅保留报告 Schema 兼容，不产生任何 Hub 正文写入。
        tier_warns = []
        for d, pred in dir_preds:
            tier, registered = get_predicate_tier(pred)
            if not registered:
                tier_warns.append({"direction": d, "predicate": pred, "default_tier": tier})
        _doc_domain = _get_domain_from_path(page)
        # 补充 derived_directions 报告: 区分 direction 层未命中 vs hub 兜底 vs 真正悬空
        _dd = report.get("derived_directions")
        if _dd is not None:
            _dir_unmatched = _dd.get("direction_unmatched", [])
            if _dd.get("main") is None:
                _dd["truly_orphan_keywords"] = _dir_unmatched
            else:
                _dd["truly_orphan_keywords"] = []
        # 合并 + 可选 Wiki section locator + 补默认值。
        all_triples = mechanical + sem_triples
        report["edge_locators"] = attach_wiki_section_sources(all_triples, page)
        fill_defaults(all_triples, fm)
        # 稀疏导航：命题内部概念只经 proposition/包含边可达，不提升成论文级关键词。
        navigation_candidates = navigation_connectivity_candidates(all_triples, page, corresponding)
        if navigation_candidates:
            report["navigation_connectivity_candidates"] = navigation_candidates
        # 入库
        delta = gd.build_document_delta(
            page,
            fm,
            all_triples,
            deterministic_triple_count=len(mechanical),
        )
        writer_triples = gd.knowledge_edges(delta)
        attach_plan = gd.plan_attachment(conn, delta)
        _ir, delta_report = gd.fuse_with_savepoint(
            conn,
            delta,
            lambda: add_knowledge_edges(
                conn, page, writer_triples, attach_plan=attach_plan
            ),
            inspection=gd.inspect_delta(conn, delta, attach_plan=attach_plan),
        )
        report["graph_delta"] = delta_report
        report.update({
            "edges_added": _ir.edges_added, "dedup_skipped": _ir.dedup_skipped, "dup_skipped": _ir.dup_skipped,
            "resolve_hits": _ir.resolve_hits, "resolve_ambig": _ir.resolve_ambig, "nodes_created": _ir.nodes_created,
            "descriptive_warnings": _ir.descriptive_warnings,
            "mechanical_edges": len(mechanical),
            "semantic_edges": len(sem_triples),
            "semantic_format_warnings": sem_format_warns,
            "unregistered_predicates": tier_warns,
            "doc_navigation_mode": _doc_domain is not None,
            "doc_keywords_capped": max(0, len(keywords) - ADMIN_KEYWORD_LIMIT) if _doc_domain is not None else 0,
            "doc_relations_capped": max(0, len([t for t in sem_triples if t.get("predicate") in (_DOMAIN_PREDICATES[_doc_domain][0] if _doc_domain else set())]) - ADMIN_RELATION_LIMIT) if _doc_domain is not None else 0,
        })
    else:
        # 兼容:直接 JSON / 引文自动模式
        triples = []
        if args.citations:
            # 引文自动模式:从 citations JSON 转 triples(引用+发表于)
            citations = json.loads(Path(args.citations).read_text(encoding="utf-8"))
            for c in citations:
                node = c.get("node_name", "")
                if not node:
                    continue
                triples.append({"subject": page, "predicate": "引用", "object": node})
                venue = c.get("venue", "")
                if venue:
                    triples.append({"subject": node, "predicate": "发表于", "object": venue})
            report["citations_processed"] = len(citations)
        elif args.triples:
            triples = json.loads(Path(args.triples).read_text(encoding="utf-8"))
        elif args.triples_json:
            triples = json.loads(args.triples_json)
        # 兼容模式也补默认值(修复 source 填空 bug:语义模式调了 fill_defaults,兼容模式漏调)
        fm = gl.read_frontmatter(page)
        report["edge_locators"] = attach_wiki_section_sources(triples, page)
        fill_defaults(triples, fm)
        deterministic_count = len(triples) if args.citations else 0
        delta = gd.build_document_delta(
            page, fm, triples, deterministic_triple_count=deterministic_count
        )
        writer_triples = gd.knowledge_edges(delta)
        attach_plan = gd.plan_attachment(conn, delta)
        _ir, delta_report = gd.fuse_with_savepoint(
            conn,
            delta,
            lambda: add_knowledge_edges(
                conn, page, writer_triples, attach_plan=attach_plan
            ),
            inspection=gd.inspect_delta(conn, delta, attach_plan=attach_plan),
        )
        report["graph_delta"] = delta_report
        report.update({
            "edges_added": _ir.edges_added, "dedup_skipped": _ir.dedup_skipped, "dup_skipped": _ir.dup_skipped,
            "resolve_hits": _ir.resolve_hits, "resolve_ambig": _ir.resolve_ambig, "nodes_created": _ir.nodes_created,
            "descriptive_warnings": _ir.descriptive_warnings,
        })

    conn.commit()
    # Hub 动力学只局部刷新可重建的“普通节点→聚类于→Hub”边。生命周期
    # 始终只产候选；embedding 不可用或节点无画像时静默保留原归属。
    try:
        hub_dynamics = hs.refresh_after_ingest(conn, page)
        conn.commit()
        if hub_dynamics.get("affected_nodes"):
            report["hub_dynamics"] = hub_dynamics
    except Exception:
        conn.rollback()
    # 消解 abbreviation-todo：若本次新建节点含全称，自动补 alias 并移除已消解项
    try:
        import sync_keyword_aliases
        _r, _rem = sync_keyword_aliases.resolve_abbreviation_todo(conn)
        if _r:
            report["abbreviation_todo_resolved"] = _r
    except Exception:
        pass
    conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))


def cmd_merge(args):
    conn = _connect_for(args)
    dup = merge_nodes(conn, args.src, args.tgt)
    conn.commit()
    conn.close()
    print(json.dumps({"merged": args.src, "into": args.tgt, "dups_removed": dup}, ensure_ascii=False, indent=2))


def cmd_init(args):
    db = getattr(args, "db", None)
    conn = gl.connect(db) if db else gl.connect()
    had_nodes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
    ).fetchone() is not None
    node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] if had_nodes else 0
    # 幂等:仅 CREATE IF NOT EXISTS,绝不 DROP,重复执行不清空已有数据
    gl.init_schema(conn)
    conn.commit()
    conn.close()
    target = db or str(gl.GRAPH_DB)
    if not had_nodes:
        print(f"[OK] 已创建新数据库: {target} (schema v4)")
    elif node_count == 0:
        print(f"[OK] 数据库已经初始化(空): {target} (schema v4)")
    else:
        print(f"[OK] 数据库已经初始化，已含 {node_count} 个节点；init 不会清空数据: {target}")


def cmd_cleanup_ghosts(args):
    """手动清理 ghost hub（摄入时已自动清扫，此为独立入口）。"""
    conn = getattr(args, 'db', None) and gl.connect(args.db) or gl.connect()
    cleaned = cleanup_ghost_hubs(conn)
    conn.close()
    if cleaned:
        print(f"已清理 {len(cleaned)} 个 ghost hub 节点：")
        for p in cleaned:
            print(f"  {p}")
    else:
        print("无 ghost hub（所有 type='hub' 节点均有对应 .md）")


def cmd_cleanup_orphans(args):
    """清理孤儿 alias（指向已删节点）+ 孤儿边（引用已删节点）+ FK 违规。"""
    conn = getattr(args, 'db', None) and gl.connect(args.db) or gl.connect()
    cleaned = cleanup_orphan_references(conn)
    conn.close()
    print(json.dumps({
        "orphan_aliases": cleaned["orphan_aliases"],
        "orphan_edges": cleaned["orphan_edges"],
        "fk_violations": cleaned["fk_violations"],
    }, ensure_ascii=False, indent=2))


def main():
    ap = argparse.ArgumentParser(description="增量建边入口(graph.db 主数据化)")
    ap.add_argument("--db", default=None,
                    help="graph.db 路径(默认按 page 所属域: private→private/graph.db,其余→cross-domain/graph.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_i = sub.add_parser("init", help="幂等初始化 graph.db(不删已有数据)")
    p_i.set_defaults(func=cmd_init)
    p_cg = sub.add_parser("cleanup-ghosts", help="清理 .md 已删但 graph.db 残留的 ghost hub 节点+边")
    p_cg.set_defaults(func=cmd_cleanup_ghosts)
    p_co = sub.add_parser("cleanup-orphans", help="清理孤儿 alias/边/FK 违规(摄入前图健康兜底)")
    p_co.set_defaults(func=cmd_cleanup_orphans)
    p_m = sub.add_parser("merge", help="合并节点(src→tgt,迁移边+加alias+去重)")
    p_m.add_argument("--src", required=True, help="被合并的节点名")
    p_m.add_argument("--tgt", required=True, help="合并到的目标节点名")
    p_m.set_defaults(func=cmd_merge)
    p_pf = sub.add_parser("prefill", help="生成预填模板(机械边+语义槽)")
    p_pf.add_argument("--page", required=True)
    p_pf.set_defaults(func=cmd_prefill)
    p_g = sub.add_parser("ingest", help="ingest 一页建边")
    p_g.add_argument("--page", required=True)
    p_g.add_argument("--triples", help="LLM 临时片段文件路径(JSON,兼容模式)")
    p_g.add_argument("--triples-json", help="LLM 临时 JSON 字符串(兼容模式)")
    p_g.add_argument("--semantic", help="LLM 填的语义槽文件(预填模式)")
    p_g.add_argument("--citations", help="引文 JSON(含 title),用于跨论文补全")
    p_g.add_argument("--clean", action="store_true", help="re-ingest 模式:先删本页旧边再重建(原子)")
    p_g.set_defaults(func=cmd_ingest)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

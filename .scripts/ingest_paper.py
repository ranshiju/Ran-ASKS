#!/usr/bin/env python3
"""ingest_paper.py — 代码驱动的学术论文 PDF 摄入编排器。

3.3 分两阶段调用 LLM (call_text)：3.3a 撰写 wiki（带 paper.md）→ 3.4 校验通过 →
3.3b 抽取语义槽（单轮，只带 wiki，不带 paper.md）。其余步骤全纯代码。
agent 模式(INGEST_BACKEND=agent)下 3.3 合并为单次任务：prompt 用文件路径
替代论文全文(~15K→~600 token)，一次输出 wiki+语义槽，省一轮程序往返。
流程: 3.1 dedup_check → 3.2 extract → 3.3a write_wiki → 3.4 validate_wiki →
3.3b write_slots → 3.5 fill_semantics → 3.6 validate_semantics → 落位 →
3.7 update_graph → 3.8 validate_graph → 3.9 finalize_tail
修复循环: wiki 硬错误回 3.3a 重写；语义槽结构硬错误早停并交接人工；
warning 走 3.6b 局部修复。各阶段独立重试，最多 3 次。
状态: temp/inbox-state/<txn-id>.json，可从任意步骤恢复。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from unicodedata import normalize

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import inbox_state
import trash_util
from predicate_governance import DEFAULT_CONFIG, govern as govern_predicates, normalize_predicate
from llm_structured import call_json, call_text, ingest_mode
import ingest_common as ic
import ingest_pipeline
import recovery_policy as rp
import source_fingerprints as sf
import wiki_locator as wl
from ingest_common import (parse_meta_block, validate_meta,
                           progress, parse_delimited, parse_check_errors,
                           set_progress_file, set_progress_log_path, get_progress_log_path,
                           close_progress_file)
from wiki_skeleton import extract_authors_from_text

TEMP_EXTRACT = REPO / "temp" / "inbox-extract"
MIN_SEMANTIC_TRIPLES = 4
MAX_SPARSE_SLOT_RETRIES = 1
PAPER_RECOVERY_LIMITS = rp.normalize_limits({
    "wiki_revision": 1,
    "semantic_revision": 1,
    "deterministic_repair": 1,
    "subagent": 1,
})
_PAPER_CONTEXT_PROFILE = ic.CONTEXT_PROFILES["paper"]
FULL_TEXT_MAX_CHARS = _PAPER_CONTEXT_PROFILE["full_text_max_chars"]
REDUCED_CONTEXT_MAX_CHARS = _PAPER_CONTEXT_PROFILE["reduced_context_max_chars"]
SECTION_CHAR_CAP = _PAPER_CONTEXT_PROFILE["section_char_cap"]

NON_BLOCKING_ISSUES = ("bare_abbreviation", "descriptive_phrase")


def _resume_cmd(state: dict) -> str:
    return f"python3 .scripts/ingest_paper.py --resume {state['transaction_id']}"


def _validate_cmd(state: dict) -> str:
    return f"python3 .scripts/ingest_paper.py --validate {state['transaction_id']}"
WIKI_DELIMITER = "<<<WIKI>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"
STOP_WORDS = {"of", "and", "the", "a", "an", "in", "on", "for", "to", "with",
              "from", "by", "at", "as", "is", "are", "via", "using"}

LEGACY_SLOT_SECTIONS = {
    "期刊", "研究基础", "核心方法", "核心创新点", "局限性", "未来展望",
    "研究关键词", "对比方法", "通讯作者", "自由边", "研究方向",
}
CURRENT_SLOT_SECTIONS = {
    "期刊", "第一作者", "其他作者", "通讯作者", "三元组", "概念说明",
}
KNOWN_SECTIONS = LEGACY_SLOT_SECTIONS | CURRENT_SLOT_SECTIONS
PAPER_SEMANTIC_CONTRACT_VERSION = "paper-semantic-v1"
SEMANTIC_PREDICATES = {
    "作者", "通讯作者", "引用", "发表于", "主要研究", "涉及", "研究基础", "核心方法",
    "核心创新点", "局限性", "未来展望", "研究关键词", "对比方法", "所属", "就读", "导师",
    "指导", "基于", "紧密相关于", "应用于", "贡献于", "延伸至", "探索", "属于", "第一作者",
    "改进", "结合", "对比", "推广", "替代", "扩展",
}
PREDICATE_CANDIDATES_PATH = REPO / "cross-domain" / "predicate-candidates.jsonl"

# agent 模式 prompt 引用的格式示例（已有论文页，供 agent 参考 section 结构）
FORMAT_EXAMPLE = "academic/wiki/papers/2019-cheng-ttn-generative.md"

# 摄入流水线阶段（plan 用）。needs_agent=True 的阶段需 agent 介入，其余为纯代码步骤。
# agent 模式 3 步（中间需 agent 写 wiki+slots）；api 模式 1 步（代码+API LLM 全自动，agent 零介入）。
# agent 在主体流程开始前用 update_plan 落地步骤，减少对纯代码步骤的无效干预。
PIPELINE_PLAN_AGENT = [
    {"step": "判断重复 + 提取全文", "needs_agent": False,
     "desc": "dedup(查图+查raw/DOI/arxiv匹配) → MinerU 解析 PDF 为 paper.md，一次程序调用完成"},
    {"step": "书目预审门", "needs_agent": True,
     "desc": "agent 接管：裁决程序候选或提交 Raw 定位约束的作者修正，并向 temp/<txn>/bibliographic-review.json 输出受约束 JSON；manual_required 禁止落位"},
    {"step": "撰写 wiki 与语义槽", "needs_agent": True,
     "desc": "agent 接管：读 paper.md → 填骨架 Navigation/Content → 抽取语义槽，一次输出 <<<WIKI>>> + <<<SLOTS>>>"},
    {"step": "更新 Graph + 校验 + 收尾", "needs_agent": False,
     "desc": "validate→落位→graph_ingest 建边→validate_graph→finalize_tail(log/index/派生同步)，--resume 一次调用完成"},
]

# api 模式：全程代码+API LLM 驱动，agent 零介入，合并为单步
PIPELINE_PLAN_API = [
    {"step": "摄入论文（代码+API 全自动）", "needs_agent": False,
     "desc": "dedup→extract(MinerU)→wiki(API)→slots(API)→validate→落位→建图→图校验→收尾，--pdf 一条命令完成；agent 仅读最终 JSON 确认 paper-id/路径/边数"},
]

def pipeline_plan_for(mode: str) -> list[dict]:
    """按摄入后端模式返回对应流水线 plan。"""
    return {"agent": PIPELINE_PLAN_AGENT, "api": PIPELINE_PLAN_API}.get(mode, PIPELINE_PLAN_AGENT)


def is_valid_predicate_candidate(predicate: str) -> bool:
    """候选谓词须是短的关系词，不能是句子或结构化文本。"""
    return bool(re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{1,12}", predicate))


def semantic_predicate_guide() -> str:
    registry = REPO / ".scripts" / "predicate-registry.json"
    try:
        dynamic = set(json.loads(registry.read_text(encoding="utf-8")).get("formal", []))
    except (OSError, json.JSONDecodeError):
        dynamic = set()
    return "、".join(sorted(SEMANTIC_PREDICATES | dynamic))


def record_predicate_candidates(state: dict) -> None:
    """将本次合格的新谓词追加到待审议队列，事务内幂等。"""
    if state.get("predicate_candidates_recorded") or not state.get("predicate_candidates"):
        return
    PREDICATE_CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PREDICATE_CANDIDATES_PATH.open("a", encoding="utf-8") as handle:
        for candidate in state["predicate_candidates"]:
            handle.write(json.dumps({
                "transaction_id": state["transaction_id"],
                "paper_id": state.get("paper_id", ""),
                "wiki_path": state.get("wiki_path", ""),
                "source": state.get("source", ""),
                **candidate,
            }, ensure_ascii=False) + "\n")
    state["predicate_candidates_recorded"] = True
    govern_predicates()


# ===== 工具函数 =====

def run(command: list[str]) -> str:
    return ic.run(command, REPO)


def deaccent(text: str) -> str:
    return normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slugify(text: str) -> str:
    text = deaccent(text).lower().replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "paper"


def identifier_slug(text: str) -> str:
    """Prefer the established ASCII slug, with a stable Unicode fallback."""
    ascii_slug = slugify(text)
    has_cjk = bool(re.search(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    if (ascii_slug != "paper" and not has_cjk) or not text.strip():
        return ascii_slug
    normalized = normalize("NFKC", text).lower().replace("'", "").replace("\u2019", "")
    unicode_slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE)
    unicode_slug = unicode_slug.replace("_", "-").strip("-")
    return unicode_slug[:48] or "paper"


def inbox_pdf_paths() -> list[Path]:
    """返回 inbox 中待摄入的 PDF，按路径稳定排序。"""
    inbox = REPO / "inbox"
    return sorted(
        (path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.name.casefold(),
    ) if inbox.is_dir() else []


def new_state_for_pdf(pdf_path: Path) -> dict:
    """为 inbox PDF 创建独立、可恢复的摄入事务。"""
    return {
        "transaction_id": datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + slugify(pdf_path.stem)[:20],
        "status": "dedup_check",
        "source": str(pdf_path.relative_to(REPO)),
        "retry_count": 0,
        "errors": [],
    }


def new_state_for_raw(raw_path: Path) -> dict:
    """为已入库 raw（网上下载等非 inbox 来源）创建摄入事务。

    raw 已在 academic/raw/{references,works/papers}/<paper-id>/，跳过 dedup+extract，
    从 write_wiki 开始复用 inbox pipeline。raw_dir 不可变，paper_id 从目录名取。
    """
    import shutil
    paper_id = raw_path.parent.name
    raw_md_rel = str(raw_path.relative_to(REPO))
    txn = "raw-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + slugify(paper_id)[:30]
    extract_dir = REPO / "temp" / "raw-extract" / txn
    extract_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(raw_path, extract_dir / "paper.md")
    raw_dir = str(raw_path.parent.relative_to(REPO))
    md_text = raw_path.read_text(encoding="utf-8")
    bibliography, corrections = repair_archived_bibliography(
        load_bibliographic_metadata(raw_path.parent), md_text)
    return {
        "transaction_id": txn,
        "status": "write_wiki",
        "source": raw_md_rel,
        "extract_dir": str(extract_dir.relative_to(REPO)),
        "raw_dir": raw_dir,
        "wiki_path": f"academic/wiki/papers/{paper_id}",
        "paper_id": paper_id,
        "bibliographic_meta": bibliography,
        "bibliographic_corrections": corrections,
        "from_raw": True,
        "retry_count": 0,
        "errors": [],
        "wiki_retry": 0,
        "slots_retry": 0,
    }


# ===== paper-id 生成 =====

_JOURNAL_HEADER_RE = re.compile(r"(?i)\b(?:VOLUME|VOL\.?)\s*\d")
_APS_TITLE_PREFIX_RE = re.compile(
    r"(?i)^PHYSICAL REVIEW\s+(?:LETTERS|[A-Z])\s+\d+\s*,\s*"
    r"[A-Z0-9.]+\s*\((?:19|20)\d{2}\)\s*"
)


def _clean_title_candidate(title: str) -> str:
    text = " ".join(str(title or "").split()).strip()
    stripped = _APS_TITLE_PREFIX_RE.sub("", text).strip()
    if stripped != text:
        return stripped
    if _JOURNAL_HEADER_RE.search(text):
        return ""
    return text

def extract_title_from_md(md_text: str) -> str:
    for line in md_text.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            title = _clean_title_candidate(line[2:])
            if title:
                return title
    # 回退:h1 未找到时取首个 h2(MinerU 偶尔只输出 h2)
    for line in md_text.splitlines():
        line = line.strip()
        if line.startswith("## "):
            return line[3:].strip()
    return ""


def extract_year_from_md(md_text: str) -> str:
    """四级提取年份：DOI/Published → arxiv 编号 → 第一页 → 全文兜底。以正式发表信息为准。"""
    # 限前50行：论文自己的发表信息在标题块，正文引用/图表年份在后面
    _head50 = "\n".join(md_text.splitlines()[:50])
    # 1. Published 信息（正式发表年优先；排除"between"避免误匹配正文范围描述）
    m = re.search(r"Published\s+(?!between\b).{0,30}?(20\d{2})", _head50, re.IGNORECASE)
    if m:
        return m.group(1)
    # 2. arxiv 编号：arXiv:1307.0401 或 1307.0401，前 4 位 YYMM → 20YY
    m = re.search(r"(?:arxiv[:\s]?)?(\d{2})(\d{2})\.\d{4,5}", _head50, re.IGNORECASE)
    if m:
        yy = int(m.group(1))
        if 0 <= yy <= 30:  # 00-30 → 2000-2030
            return f"20{yy:02d}"
    # 3. 第一页（前 60 行）找第一个 20XX
    for line in md_text.splitlines()[:60]:
        m = re.search(r"\b(20\d{2})\b", line)
        if m:
            return m.group(1)
    # 4. 全文兜底：第一个 20XX
    m = re.search(r"\b(20\d{2})\b", md_text)
    if m:
        return m.group(1)
    return ""


def extract_arxiv_id(text: str) -> str:
    """从文本提取 arxiv ID（含版本号），如 0907.0401 或 0907.0401v2。"""
    m = re.search(r"arxiv[:\s]?(\d{4}\.\d{4,5}(?:v\d+)?)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{4}\.\d{4,5}(?:v\d+)?)\b", text)
    if m:
        return m.group(1)
    return ""


def extract_doi(text: str) -> str:
    """从文本提取 DOI，如 10.1103/PhysRevLett.106.127202。"""
    m = re.search(r"\b(10\.\d{4,}/[^\s\"<>]+)", text)
    if m:
        return m.group(1).rstrip(".,;)")
    return ""


def bibliographic_identity_region(md_text: str, max_lines: int = 160) -> str:
    """Return the front-matter region that can identify the current paper.

    DOI/arXiv strings below a references heading identify cited papers, not the
    paper being ingested.  The hard line bound also keeps candidate extraction
    aligned with the first-page evidence used elsewhere in this pipeline.
    """
    lines = md_text.splitlines()[:max_lines]
    for index, line in enumerate(lines):
        if re.match(
            r"^\s*#{1,6}\s*(?:references|bibliography|参考文献)\s*$",
            line,
            re.IGNORECASE,
        ):
            lines = lines[:index]
            break
    return "\n".join(lines)


SUPPLEMENT_KEYWORDS = ("supplement", "supp_", "si-", "si.", "supporting", "appendix")
TRANSLATION_FILE_TOKENS = {"zh", "cn", "chinese", "translation", "translated"}


def detect_raw_relationship(state: dict, dup_graph: list) -> dict:
    """检测新论文与已有论文的 raw 关系（版本/补充材料/翻译）。

    返回 {"type": "version"|"supplementary"|"translation"|"duplicate"|None,
          "target_page": str, "uncertain": bool, ...}
    - "duplicate" (certain): 真重复，应停止摄入
    - "duplicate" (uncertain): 标题极相似但无 ID 确认，agent 介入
    - "version"/"supplementary"/"translation" (certain): 继续摄入，建 raw 关系边
    - "version" (uncertain): 标题相似但无 ID 确认，agent 介入
    - None: 无关系，正常摄入
    """
    import graph_lib as gl
    pdf_path = REPO / state["source"]
    filename = pdf_path.name.lower()
    filename_tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", pdf_path.stem.lower()))
    # 从 PDF 第一页提取文本
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        first_page_text = doc[0].get_text("text")
        doc.close()
    except Exception:
        first_page_text = ""
    new_arxiv = extract_arxiv_id(first_page_text)
    new_doi = extract_doi(first_page_text)

    def _read_existing_raw(cand):
        """读已有论文的 raw MD 文本（优先 raw_path，回退 page sources）。"""
        raw_path = cand.get("raw_path")
        if raw_path:
            raw_p = REPO / raw_path
            if raw_p.exists():
                return raw_p.read_text(encoding="utf-8")
            return ""
        page_path = cand.get("path")
        if not page_path:
            return ""
        fm = gl.read_frontmatter(page_path)
        sources = gl.parse_list_field(fm, "sources")
        for src in sources:
            raw_p = REPO / src.split("#")[0]
            if raw_p.exists():
                return raw_p.read_text(encoding="utf-8")
        return ""

    # 1. 补充材料：文件名含 supplement/SI/supporting
    if any(kw in filename for kw in SUPPLEMENT_KEYWORDS) and dup_graph:
        best = max(dup_graph, key=lambda x: x["ratio"])
        return {"type": "supplementary", "target_page": best["path"], "uncertain": False}

    # 2. 翻译：文件名含 zh/cn/翻译
    is_translation_file = bool(filename_tokens & TRANSLATION_FILE_TOKENS) or any(
        marker in pdf_path.stem for marker in ("翻译", "译文")
    )
    if is_translation_file and dup_graph:
        best = max(dup_graph, key=lambda x: x["ratio"])
        return {"type": "translation", "target_page": best["path"], "uncertain": False}

    # 3. 版本：arxiv ID 匹配
    if new_arxiv:
        base_id = re.sub(r"v\d+$", "", new_arxiv)
        for dup in dup_graph:
            raw_text = _read_existing_raw(dup)
            if not raw_text:
                continue
            existing_arxiv = extract_arxiv_id(raw_text)
            if not existing_arxiv:
                continue
            existing_base = re.sub(r"v\d+$", "", existing_arxiv)
            if existing_base == base_id and existing_arxiv != new_arxiv:
                return {"type": "version", "target_page": dup["path"],
                        "new_arxiv": new_arxiv, "existing_arxiv": existing_arxiv,
                        "uncertain": False}
            if existing_arxiv == new_arxiv:
                return {"type": "duplicate", "target_page": dup["path"], "uncertain": False}

    # 4. DOI 匹配
    if new_doi:
        for dup in dup_graph:
            raw_text = _read_existing_raw(dup)
            if not raw_text:
                continue
            existing_doi = extract_doi(raw_text)
            if existing_doi and existing_doi == new_doi:
                return {"type": "duplicate", "target_page": dup["path"], "uncertain": False}

    # 5. 标题高度相似但无 ID 匹配（候选已过 0.95 标题门槛）
    # 若已提取 DOI 或 arxiv ID 但均未匹配 → ID 是唯一标识，判为非重复（放行）
    if new_doi or new_arxiv:
        return {"type": None}
    # 无 ID 可用，标题 > 0.95 → 保守判为不确定重复，交 agent 确认
    if dup_graph:
        best = max(dup_graph, key=lambda x: x["ratio"])
        if best["ratio"] > 0.95:
            target = best.get("path") or best.get("raw_path") or best.get("dir") or ""
            result = {"type": "duplicate", "target_page": best.get("path") or "",
                      "uncertain": True}
            if not result["target_page"] and best.get("dir"):
                result["target_raw_dir"] = best["dir"]
            return result

    return {"type": None}


RELATION_DECISION_PROTOCOL = "relation-id-v1"


def relationship_decision_schema(value) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "protocol_version", "review_status", "selection", "review_notes",
    }:
        return False
    if value.get("protocol_version") != RELATION_DECISION_PROTOCOL:
        return False
    if value.get("review_status") not in {"decided", "manual_required"}:
        return False
    if not isinstance(value.get("review_notes"), list) or not all(
        isinstance(item, str) for item in value["review_notes"]
    ):
        return False
    selection = value.get("selection")
    if not isinstance(selection, dict) or set(selection) != {
        "candidate_id", "relation", "status",
    }:
        return False
    return (
        isinstance(selection.get("candidate_id"), str)
        and selection.get("relation") in {"version", "unrelated", "ambiguous"}
        and selection.get("status") in {"confirmed", "ambiguous"}
    )


def _candidate_raw_path(candidate: dict) -> str:
    raw_path = str(candidate.get("raw_path") or "")
    if raw_path:
        return raw_path
    page_path = str(candidate.get("path") or "")
    if not page_path:
        return ""
    try:
        import graph_lib as gl
        frontmatter = gl.read_frontmatter(page_path)
        sources = gl.parse_list_field(frontmatter, "sources")
    except Exception:
        return ""
    return str(sources[0]).split("#", 1)[0] if sources else ""


def build_relationship_candidate_catalog(state: dict, paper_md: Path) -> dict:
    items = []
    for index, candidate in enumerate(state.get("relation_candidates") or [], 1):
        raw_path = _candidate_raw_path(candidate)
        bibliography = _bibliography_for_raw_artifact(raw_path) if raw_path else {}
        excerpt = ""
        if raw_path:
            try:
                excerpt = (REPO / raw_path).read_text(encoding="utf-8")[:1600]
            except (OSError, UnicodeError):
                pass
        items.append({
            "id": f"relation-{index:02d}",
            "title": str(candidate.get("title") or bibliography.get("title") or ""),
            "similarity": candidate.get("ratio"),
            "bibliographic": {
                field: bibliography.get(field)
                for field in ("title", "authors", "year", "venue", "doi", "arxiv_id")
            },
            "opening_excerpt": excerpt,
        })
    current = state.get("bibliographic_meta") or {}
    return {
        "protocol_version": RELATION_DECISION_PROTOCOL,
        "current": {
            "bibliographic": {
                field: current.get(field)
                for field in ("title", "authors", "year", "venue", "doi", "arxiv_id")
            },
            "opening_excerpt": paper_md.read_text(encoding="utf-8")[:1600],
        },
        "candidates": items,
    }


def _relationship_input_hash(catalog: dict) -> str:
    return hashlib.sha256(
        json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _relationship_cache_path(transaction_id: str) -> Path | None:
    if not transaction_id:
        return None
    return REPO / "temp" / "inbox-state" / f"{transaction_id}-relationship-decision.json"


def _load_relationship_cache(transaction_id: str, input_hash: str) -> dict | None:
    path = _relationship_cache_path(transaction_id)
    if not path or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    if (payload.get("protocol_version") != RELATION_DECISION_PROTOCOL
            or payload.get("input_hash") != input_hash
            or not relationship_decision_schema(decision)):
        return None
    return decision


def _save_relationship_cache(transaction_id: str, input_hash: str, decision: dict) -> None:
    path = _relationship_cache_path(transaction_id)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol_version": RELATION_DECISION_PROTOCOL,
        "input_hash": input_hash,
        "decision": decision,
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temp_path.replace(path)


def _deterministic_relationship_decision(catalog: dict) -> dict | None:
    current = _bibliographic_identity(catalog.get("current", {}).get("bibliographic"))
    matches = []
    for item in catalog.get("candidates", []):
        existing = _bibliographic_identity(item.get("bibliographic"))
        same_title = bool(current[0] and current[0] == existing[0])
        same_authors = bool(current[1] and current[1] == existing[1])
        same_year = bool(current[2] and current[2] == existing[2])
        if same_title and same_authors and same_year:
            matches.append(item["id"])
    if len(matches) != 1:
        return None
    return {
        "protocol_version": RELATION_DECISION_PROTOCOL,
        "review_status": "decided",
        "selection": {
            "candidate_id": matches[0], "relation": "version", "status": "confirmed",
        },
        "review_notes": ["deterministic_fast_path: same locked title/authors/year"],
    }


def _compile_relationship_decision(decision: dict, catalog: dict) -> dict:
    if not relationship_decision_schema(decision):
        raise ValueError("relation-id-v1 决策不符合 schema")
    selection = decision["selection"]
    indexes = {item["id"]: item for item in catalog.get("candidates", [])}
    candidate_id = selection["candidate_id"]
    if candidate_id not in indexes:
        raise ValueError(f"未知 relationship candidate_id: {candidate_id}")
    if (decision["review_status"] == "manual_required"
            or selection["relation"] == "ambiguous"
            or selection["status"] == "ambiguous"):
        raise ValueError("关系裁决仍为 ambiguous")
    return {
        "candidate_id": candidate_id,
        "candidate_index": int(candidate_id.rsplit("-", 1)[-1]) - 1,
        "relation": selection["relation"],
        "review_notes": decision["review_notes"],
    }


def build_relationship_review_prompt(catalog: dict) -> str:
    return f"""你是受程序约束的论文关系裁决 Worker。程序已排除二进制重复、DOI/arXiv 精确重复和 normalized-text 重复；你无权返回 duplicate 或删除任何来源。

候选目录：
{json.dumps(catalog, ensure_ascii=False)}

只输出 JSON：
{{
  "protocol_version": "{RELATION_DECISION_PROTOCOL}",
  "review_status": "decided|manual_required",
  "selection": {{"candidate_id": "relation-01", "relation": "version|unrelated|ambiguous", "status": "confirmed|ambiguous"}},
  "review_notes": []
}}

约束：只能选择一个现有 candidate ID；同一研究工作的修订稿、作者稿或出版稿选 version；仅标题近似但研究工作不同选 unrelated；证据不足选 ambiguous/manual_required。不得输出路径、locator 或书目值。"""


def review_uncertain_relationship(state: dict, paper_md: Path) -> dict:
    catalog = build_relationship_candidate_catalog(state, paper_md)
    input_hash = _relationship_input_hash(catalog)
    worker = {
        "protocol_version": RELATION_DECISION_PROTOCOL,
        "input_hash": input_hash,
        "api_called": False,
        "cache_hit": False,
        "skipped": False,
        "skip_reason": "",
    }
    decision = _deterministic_relationship_decision(catalog)
    if decision:
        worker["skipped"] = True
        worker["skip_reason"] = "deterministic_fast_path"
    else:
        decision = _load_relationship_cache(state.get("transaction_id", ""), input_hash)
        if decision:
            worker["cache_hit"] = True
            worker["skip_reason"] = "transaction_cache"
    prompt = build_relationship_review_prompt(catalog)
    if not decision:
        result = call_json(
            prompt,
            relationship_decision_schema,
            max_tokens=600,
            retries=0,
            operation="ingest_relation_review",
            transaction_id=state.get("transaction_id", ""),
            system="你是论文关系候选 ID 裁决 Worker，只输出 JSON。",
        )
        worker["api_called"] = bool(result.get("history"))
        if result.get("status") == "agent_required":
            return {
                "ok": False, "status": "agent_required", "prompt": result.get("prompt", prompt),
                "catalog": catalog, "input_hash": input_hash, "worker": worker,
            }
        if not result.get("ok"):
            return {
                "ok": False, "status": "agent_required", "prompt": prompt,
                "error": result.get("error", "关系 Worker 失败"), "catalog": catalog,
                "input_hash": input_hash, "worker": worker,
            }
        decision = result.get("parsed")
    try:
        compiled = _compile_relationship_decision(decision, catalog)
    except ValueError as exc:
        return {
            "ok": False, "status": "agent_required", "prompt": prompt, "error": str(exc),
            "decision": decision, "catalog": catalog, "input_hash": input_hash,
            "worker": worker,
        }
    source_candidates = state.get("relation_candidates") or []
    candidate_index = compiled.pop("candidate_index")
    if candidate_index < 0 or candidate_index >= len(source_candidates):
        return {
            "ok": False, "status": "agent_required", "prompt": prompt,
            "error": "relationship candidate index 越界", "decision": decision,
            "catalog": catalog, "input_hash": input_hash, "worker": worker,
        }
    source_candidate = source_candidates[candidate_index]
    compiled["target_page"] = str(source_candidate.get("path") or "")
    if not compiled["target_page"] and source_candidate.get("dir"):
        compiled["target_raw_dir"] = str(source_candidate["dir"])
    if not worker["skipped"]:
        _save_relationship_cache(state.get("transaction_id", ""), input_hash, decision)
    return {
        "ok": True, "status": "ok", "compiled": compiled, "decision": decision,
        "catalog": catalog, "input_hash": input_hash, "worker": worker,
    }


def _apply_relationship_review(state: dict, compiled: dict) -> None:
    relation = compiled.get("relation")
    if relation == "version":
        state["raw_relationship"] = {
            "type": "version",
            "target_page": compiled.get("target_page", ""),
            "target_raw_dir": compiled.get("target_raw_dir", ""),
            "uncertain": False,
            "confirmed_by": RELATION_DECISION_PROTOCOL,
        }
    else:
        state["raw_relationship"] = {
            "type": None, "uncertain": False,
            "confirmed_by": RELATION_DECISION_PROTOCOL,
        }


def _relationship_review_draft_path(state: dict) -> Path:
    return REPO / state["extract_dir"] / "relationship-review.json"


def _resume_relationship_review(state: dict) -> bool:
    review_state = state.get("relationship_review") or {}
    if state.get("status") != "agent_required" or review_state.get("status") != "agent_required":
        return False
    draft_path = _relationship_review_draft_path(state)
    if not draft_path.is_file():
        return False
    try:
        decision = json.loads(draft_path.read_text(encoding="utf-8"))
        compiled = _compile_relationship_decision(decision, review_state.get("catalog") or {})
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        state["errors"] = [f"关系裁决草稿无效: {exc}"]
        return False
    source_candidates = state.get("relation_candidates") or []
    candidate_index = compiled.pop("candidate_index")
    if candidate_index < 0 or candidate_index >= len(source_candidates):
        state["errors"] = ["relationship candidate index 越界"]
        return False
    source_candidate = source_candidates[candidate_index]
    compiled["target_page"] = str(source_candidate.get("path") or "")
    if not compiled["target_page"] and source_candidate.get("dir"):
        compiled["target_raw_dir"] = str(source_candidate["dir"])
    _save_relationship_cache(
        state.get("transaction_id", ""), review_state.get("input_hash", ""), decision,
    )
    _apply_relationship_review(state, compiled)
    state["relationship_review"] = {
        "status": "ok",
        "decision": decision,
        "compiled": compiled,
        "catalog": review_state.get("catalog") or {},
        "input_hash": review_state.get("input_hash", ""),
        "worker": review_state.get("worker") or {},
    }
    persist_bibliographic_metadata(REPO / state["extract_dir"], state.get("bibliographic_meta"))
    inbox_state.transition(state, "write_wiki", reason="relationship_review_accepted")
    state["agent_required"] = False
    state["agent_prompt"] = ""
    state["agent_write_to"] = ""
    state["pre_handoff_status"] = ""
    state["errors"] = []
    return True


def title_to_slug(title: str) -> str:
    words = re.split(r"\s+", title.lower())
    significant = [w for w in words if w and w not in STOP_WORDS]
    return identifier_slug(" ".join(significant[:4]))


def generate_paper_id(md_text: str, arxiv_id: str = "", year_hint: str = "",
                     title_hint: str | None = None, authors_hint: list[str] | None = None) -> str:
    title = (title_hint.strip() if title_hint else extract_title_from_md(md_text))
    authors = list(authors_hint) if authors_hint is not None else extract_authors_from_text(md_text)
    # PDF metadata / 第一页发表页脚是离论文最近的确定性证据，优先于
    # MinerU 文本中的引用年份；无预提取结果时再退到 arXiv 与 paper.md。
    year = year_hint if re.fullmatch(r"(?:19|20)\d{2}", year_hint or "") else ""
    if not year and arxiv_id:
        m = re.match(r"(\d{2})(\d{2})\.", arxiv_id)
        if m and 0 <= int(m.group(1)) <= 30:
            year = f"20{m.group(1)}"
    if not year:
        year = extract_year_from_md(md_text)
    surname = "paper"
    if authors:
        parts = authors[0].split()
        surname = identifier_slug(parts[-1]) if parts else "paper"
    slug = title_to_slug(title)
    year_part = year or "0000"
    return f"{surname}-{year_part}-{slug}"


def ensure_unique_paper_id(paper_id: str) -> str:
    """检查 raw 目录/wiki 页是否已存在，冲突则追加 -2/-3 消歧。"""
    raw_refs = REPO / "academic" / "raw" / "references"
    wiki_papers = REPO / "academic" / "wiki" / "papers"
    candidate = paper_id
    suffix = 2
    while (raw_refs / candidate).exists() or (wiki_papers / f"{candidate}.md").exists():
        candidate = f"{paper_id}-{suffix}"
        suffix += 1
    return candidate


# ===== PDF 标题提取 (3.1) =====

def extract_title_from_pdf(pdf_path: Path) -> str:
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        try:
            # 优先使用 PDF metadata title（最可靠，避免误提期刊抬头/页眉）
            meta_title = _clean_title_candidate((doc.metadata or {}).get("title", ""))
            if meta_title and len(meta_title) > 10:
                return meta_title
            page = doc[0]
            text = page.get_text("text")
        finally:
            doc.close()
    except Exception:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in lines[:5]:
        if len(line) > 10 and not re.match(r"^(arXiv|doi|http|vol\.|page|published|\d+$)", line, re.I) \
           and not re.search(r"vol\.?\s*\d|volume\s*\d", line, re.I):
            return _clean_title_candidate(line)
    return _clean_title_candidate(lines[0])


def extract_pdf_bibliography(pdf_path: Path) -> dict:
    """在 LLM/MinerU 前读取 PDF metadata 与第一页发表页脚。

    返回的字段均带近端证据来源；不联网、不根据正文引用猜测 venue/year。
    第一页页脚优先于 metadata subject/creationDate，文件名仅作最后前的弱提示。
    """
    result = {
        "title": "", "authors": [], "year": "", "venue": "",
        "arxiv_id": "", "doi": "", "evidence": {},
    }
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        try:
            metadata = doc.metadata or {}
            first_page_text = doc[0].get_text("text") if len(doc) else ""
            evidence_scope = "pdf_first_page"
            # Some publisher downloads prepend a one-page access wrapper; the
            # actual journal header then starts on PDF page 2.  Treat that page
            # as front matter only when the wrapper is explicitly recognizable,
            # avoiding a broad two-page scan that could pick up cited DOIs.
            if (len(doc) > 1 and re.search(
                    r"downloaded from IOPscience|scroll down to see the full text",
                    first_page_text, re.I)):
                first_page_text += "\n" + doc[1].get_text("text")
                evidence_scope = "pdf_front_matter"
        finally:
            doc.close()
    except Exception:
        return result

    title = _clean_title_candidate(metadata.get("title") or "")
    if len(title) > 10:
        result["title"] = title
        result["evidence"]["title"] = "pdf_metadata.title"

    author_text = str(metadata.get("author") or "").strip()
    if author_text:
        separator = r"\s*;\s*" if ";" in author_text else r"\s+(?:and|&)\s+"
        authors = [part.strip() for part in re.split(separator, author_text, flags=re.I) if part.strip()]
        if authors:
            result["authors"] = authors
            result["evidence"]["authors"] = "pdf_metadata.author"

    lines = [" ".join(line.split()) for line in first_page_text.splitlines() if line.strip()]
    evidence_lines = []
    for line in lines:
        if re.search(
                r"\bProceedings of\b|\bPublished\b|\bTo cite this article\b|"
                r"Association for Computational Linguistics|[©Ⓒ]", line, re.I):
            evidence_lines.append(line)
        venue_match = re.search(
            r"\bProceedings of\s+(.+?)(?:,\s*(?:pages?|pp\.?)\b|$)", line, re.I)
        if not venue_match:
            venue_match = re.search(
                r"\bPublished as (?:an?\s+)?(?:conference|workshop|journal) paper at\s+"
                r"(.+?\b(?:19|20)\d{2})\b",
                line, re.I,
            )
        if venue_match and not result["venue"]:
            result["venue"] = venue_match.group(1).strip(" .")
            result["evidence"]["venue"] = evidence_scope
        if not result["venue"] and re.search(
                r"^EPL\s*,\s*\d+\s*\((?:19|20)\d{2}\)\s*\d+\s*$", line, re.I):
            result["venue"] = "EPL"
            result["evidence"]["venue"] = evidence_scope
        iop_match = re.search(
            r"\b((?:19|20)\d{2})\s+New\s+J\.\s*Phys\.\s+(\d+)\s+([A-Za-z0-9]+)\b",
            line, re.I,
        )
        if not result["venue"] and iop_match:
            year, volume, article = iop_match.groups()
            result["venue"] = f"New J. Phys. {volume}, {article} ({year})"
            result["evidence"]["venue"] = evidence_scope

    year_candidates: list[tuple[str, str]] = []
    # 同一 APS 页脚常同时含 received/published；发表年是论文书目事实，必须优先。
    for line in evidence_lines:
        match = re.search(r"\bpublished\b.{0,64}?\b((?:19|20)\d{2})\b", line, re.I)
        if match:
            year_candidates.append((match.group(1), f"{evidence_scope}.published"))
            break
    for line in evidence_lines:
        if year_candidates:
            break
        match = re.search(r"\b((?:19|20)\d{2})\b", line)
        if match:
            year_candidates.append((match.group(1), evidence_scope))
            break
    subject = str(metadata.get("subject") or "")
    match = re.search(r"\b((?:19|20)\d{2})\b", subject)
    if match:
        year_candidates.append((match.group(1), "pdf_metadata.subject"))
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", pdf_path.stem)
    if match:
        year_candidates.append((match.group(1), "source_filename"))
    creation_date = str(metadata.get("creationDate") or "")
    match = re.search(r"D:((?:19|20)\d{2})", creation_date)
    if match:
        year_candidates.append((match.group(1), "pdf_metadata.creationDate"))
    if year_candidates:
        result["year"], result["evidence"]["year"] = year_candidates[0]

    result["arxiv_id"] = extract_arxiv_id(first_page_text)
    result["doi"] = extract_doi(first_page_text)
    if not result["venue"]:
        result["venue"] = aps_venue_from_doi(result["doi"], result["year"])
        if result["venue"]:
            result["evidence"]["venue"] = "doi_aps"
    if evidence_lines:
        result["first_page_evidence"] = evidence_lines[:3]
    return result


APS_JOURNALS = {
    "PhysRevA": "Phys. Rev. A",
    "PhysRevB": "Phys. Rev. B",
    "PhysRevC": "Phys. Rev. C",
    "PhysRevD": "Phys. Rev. D",
    "PhysRevE": "Phys. Rev. E",
    "PhysRevLett": "Phys. Rev. Lett.",
    "RevModPhys": "Rev. Mod. Phys.",
    "PhysRevX": "Phys. Rev. X",
    "PRXQuantum": "PRX Quantum",
}


def aps_venue_from_doi(doi: str, year: str = "") -> str:
    """从 APS DOI 确定性生成期刊卷页；不联网、不读取参考文献。"""
    match = re.fullmatch(
        r"10\.1103/([A-Za-z]+)\.(\d+)\.([A-Za-z0-9]+)", (doi or "").strip(), re.I)
    if not match:
        return ""
    journal_code, volume, article = match.groups()
    journal = next((name for code, name in APS_JOURNALS.items()
                    if code.casefold() == journal_code.casefold()), "")
    if not journal:
        return ""
    suffix = f" ({year})" if re.fullmatch(r"(?:19|20)\d{2}", year or "") else ""
    return f"{journal} {volume}, {article}{suffix}"


def apply_bibliographic_frontmatter(markdown: str, bibliography: dict | None) -> str:
    """把已锁定书目的 title/authors/year/venue 回填到骨架或 LLM wiki。

    这里只引用 state 中的锁定结果，避免 skeleton、wiki、graph 再次从 paper.md
    各自解析作者，从而杜绝机构片段被再次当作作者。
    """
    if not bibliography:
        return markdown
    updated = markdown
    title = str(bibliography.get("title") or "").strip()
    if title:
        title_yaml = json.dumps(title, ensure_ascii=False)
        updated = re.sub(r"(?m)^title:\s*.*$", f"title: {title_yaml}", updated, count=1)
        updated = re.sub(r"(?m)^#\s+.*$", f"# {title}", updated, count=1)
    authors = [str(author).strip() for author in (bibliography.get("authors") or [])]
    if authors:
        authors_yaml = json.dumps(authors, ensure_ascii=False)
        if re.search(r"(?m)^authors:\s*", updated):
            updated = re.sub(r"(?m)^authors:\s*.*$", f"authors: {authors_yaml}", updated, count=1)
        else:
            updated = re.sub(r"(?m)^(confidence:\s*.*)$", f"authors: {authors_yaml}\n\1", updated, count=1)
    year = str(bibliography.get("year") or "").strip()
    if re.fullmatch(r"(?:19|20)\d{2}", year):
        updated = re.sub(r"(?m)^date:\s*.*$", f"date: {year}", updated, count=1)
    venue = str(bibliography.get("venue") or "").strip()
    if venue:
        escaped = venue.replace("\\", "\\\\").replace('"', '\\"')
        updated = re.sub(r"(?m)^venue:\s*.*$", f'venue: "{escaped}"', updated, count=1)
    publication = venue or year
    author_text = "、".join(authors) if authors else ""
    if author_text:
        updated = re.sub(
            r"(?m)^(> \*\*作者\*\*：).*?(\| \*\*发表\*\*：.*)$",
            lambda match: match.group(1) + author_text + " " + match.group(2),
            updated,
            count=1,
        )
    if publication:
        updated = re.sub(
            r"(?m)^(> \*\*作者\*\*：.*?\| \*\*发表\*\*：).*$",
            lambda match: match.group(1) + publication,
            updated,
            count=1,
        )
    return updated


def salvage_wiki_without_delimiter(text: str) -> str:
    """仅在输出本身是完整 wiki 时容错缺失 <<<WIKI>>>，后续仍走全量结构校验。"""
    candidate = (text or "").strip()
    if ic.META_END in candidate:
        candidate = candidate.split(ic.META_END, 1)[1].strip()
    candidate = re.sub(r"^```(?:markdown)?\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    if (candidate.startswith("---\n") and candidate.count("\n---\n") >= 1
            and "\n## Navigation\n" in candidate
            and "\n## 研究方向定位\n" in candidate
            and "\n## Content\n" in candidate):
        return candidate
    return ""


def salvage_slots_without_delimiter(text: str) -> str:
    """Recover a structurally valid slots body from misplaced/malformed wrappers.

    This only removes SLOTS wrapper lines.  The caller must still normalize and
    run the full semantic parser/validator before accepting the content.
    """
    candidate = (text or "").strip()
    candidate = re.sub(r"^```(?:text|markdown)?\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"\s*```$", "", candidate).strip()
    candidate = re.sub(
        r"(?im)^\s*<+\s*\n\s*/?SLOTS\s*>+\s*$", "", candidate,
    ).strip()
    candidate = re.sub(r"(?im)^\s*<+\s*/?SLOTS\s*>+\s*$", "", candidate).strip()
    candidate = re.sub(r"(?im)^\s*<<<END>>>\s*$", "", candidate).strip()
    if re.search(r"(?m)^三元组[:：]\s*$", candidate) and re.search(
            r"(?m)^\s*[^|\n]+\|[^|\n]+\|[^|\n]+\s*$", candidate):
        return candidate
    lines = [line.strip() for line in candidate.splitlines() if line.strip()]
    if lines and all(
        len(parts := [part.strip() for part in line.split("|")]) == 3
        and all(parts)
        for line in lines
    ):
        return "三元组:\n" + candidate
    return ""


def persist_bibliographic_metadata(extract_dir: Path, bibliography: dict | None) -> None:
    """把已锁定的书目预审结果写入事务 source.yaml；不再从 paper.md 二次补作者。"""
    if not bibliography or not any(bibliography.get(key) for key in ("title", "authors", "year", "venue")):
        return
    source_path = extract_dir / "source.yaml"
    source = {}
    if source_path.is_file():
        source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
    source["bibliographic"] = bibliography
    source_path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_bibliographic_metadata(raw_dir: Path) -> dict:
    """为 from_raw/re-ingest 复用已归档书目；旧 raw 无记录时只读相邻 PDF 补取。"""
    stored = {}
    source_path = raw_dir / "source.yaml"
    if source_path.is_file():
        try:
            source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
            if isinstance(source.get("bibliographic"), dict):
                stored = dict(source["bibliographic"])
        except Exception:
            stored = {}
    pdf_path = raw_dir / "paper.pdf"
    detected = extract_pdf_bibliography(pdf_path) if pdf_path.is_file() else {}
    if not stored:
        return detected
    merged = dict(stored)
    review_locked = bool((stored.get("review") or {}).get("locked"))
    for key, value in detected.items():
        # 已通过书目预审锁定的值优先；未锁定的旧缓存仍由 PDF 近端证据修正。
        if review_locked and key in BIBLIOGRAPHIC_REVIEW_FIELDS and value and merged.get(key):
            continue
        # 重新摄入时，PDF 近端 published/DOI 证据优先于旧版错误缓存；raw 文件本身不改。
        detected_evidence = detected.get("evidence") or {}
        stronger = (key == "year" and detected_evidence.get("year") == "pdf_first_page.published") \
            or (key == "venue" and detected_evidence.get("venue") == "doi_aps")
        if value and (not merged.get(key) or stronger):
            merged[key] = value
    if detected.get("evidence") and not review_locked:
        merged["evidence"] = detected["evidence"]
    if detected.get("first_page_evidence"):
        merged["first_page_evidence"] = detected["first_page_evidence"]
    return merged


BIBLIOGRAPHIC_REVIEW_OPERATION = "ingest_bibliographic_review"
BIBLIOGRAPHIC_REVIEW_FIELDS = ("title", "authors", "year", "venue", "doi", "arxiv_id")
BIBLIOGRAPHIC_DECISION_PROTOCOL = "candidate-id-v2"
BIBLIOGRAPHIC_SCALAR_FIELDS = ("title", "year", "venue", "doi", "arxiv_id")
AFFILIATION_HINT_RE = re.compile(
    r"\b(?:university|universit[aä]t|institute|institution|laborator(?:y|ies)|lab\.?|"
    r"department|faculty|school|college|academy|center|centre|corporation|company|"
    r"foundation|hospital|observatory|consortium)\b|大学|学院|研究所|实验室|中心|公司|医院",
    re.I,
)
_AUTHOR_TOKEN_RE = re.compile(r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’.\-]*$")


def _unique_nonempty(values: list) -> list:
    """按原序去重并去掉空字符串。"""
    result = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _repeated_title_authors(md_text: str) -> list[str]:
    """Extract a full author line after a repeated publisher-wrapper title."""
    title = extract_title_from_md(md_text)
    if not title:
        return []
    normalized_title = " ".join(title.casefold().split())
    lines = md_text.splitlines()
    groups = []
    for index, line in enumerate(lines):
        if not line.startswith("# "):
            continue
        candidate_title = _clean_title_candidate(line[2:])
        if " ".join(candidate_title.casefold().split()) != normalized_title:
            continue
        for item in lines[index + 1:index + 12]:
            author_line = item.strip()
            if not author_line:
                continue
            if author_line.startswith("#") or re.fullmatch(
                    r"(?:PAPER|OPEN ACCESS|ARTICLE|RESEARCH ARTICLE)", author_line, re.I):
                continue
            if re.search(
                    r"\b(?:To cite this article|RECEIVED|REVISED|ACCEPTED|PUBLISHED)\b",
                    author_line, re.I):
                break
            author_line = re.sub(r"<sup\b[^>]*>.*?</sup>", "", author_line, flags=re.I)
            authors = []
            for value in re.split(r",\s*|\s+and\s+", author_line):
                value = " ".join(value.split()).strip(" ,")
                value = re.sub(r"[^\wÀ-ÿ'’.\-\s]+$", "", value).strip()
                tokens = value.split()
                if not 2 <= len(tokens) <= 5 or AFFILIATION_HINT_RE.search(value):
                    continue
                if all(_AUTHOR_TOKEN_RE.fullmatch(token) for token in tokens):
                    authors.append(value)
            if authors:
                groups.append(authors)
            break
    return max(groups, key=len, default=[])


def bibliographic_quality_warnings(bibliography: dict, md_text: str) -> list[dict]:
    """Report deterministic bibliography defects that should degrade file quality."""
    warnings = []
    title = str(bibliography.get("title") or "").strip()
    cleaned_title = _clean_title_candidate(title)
    if title and cleaned_title != title:
        warnings.append({
            "issue": "bibliographic_title_contaminated",
            "detail": f"title contains publisher header: {title}",
        })
    expected_authors = _repeated_title_authors(md_text)
    selected_authors = {str(author).strip() for author in bibliography.get("authors") or []}
    missing_authors = [author for author in expected_authors if author not in selected_authors]
    if missing_authors:
        warnings.append({
            "issue": "bibliographic_authors_incomplete",
            "detail": (
                f"locked authors omit {len(missing_authors)} names from the title author block: "
                + ", ".join(missing_authors[:5])
            ),
        })
    return warnings


def repair_archived_bibliography(bibliography: dict, md_text: str) -> tuple[dict, list[dict]]:
    """Repair deterministic defects in a runtime copy; archived Raw stays immutable."""
    repaired = dict(bibliography or {})
    corrections = []
    title = str(repaired.get("title") or "").strip()
    cleaned_title = _clean_title_candidate(title)
    if title and cleaned_title and cleaned_title != title:
        repaired["title"] = cleaned_title
        corrections.append({
            "field": "title", "reason": "publisher_header_removed",
            "before": title, "after": cleaned_title,
        })

    selected = [str(author).strip() for author in repaired.get("authors") or [] if str(author).strip()]
    expected = _repeated_title_authors(md_text)
    if (expected and len(expected) > len(selected)
            and set(selected).issubset(set(expected))):
        repaired["authors"] = expected
        corrections.append({
            "field": "authors", "reason": "complete_repeated_title_author_block",
            "before": selected, "after": expected,
        })
    return repaired, corrections


def _record_bibliographic_quality_warnings(state: dict, md_text: str) -> None:
    retained = [
        warning for warning in state.get("quality_warnings", [])
        if not str(warning.get("issue") or "").startswith("bibliographic_")
    ]
    state["quality_warnings"] = retained + bibliographic_quality_warnings(
        state.get("bibliographic_meta") or {}, md_text,
    )


def _first_page_venue_candidates(evidence_lines) -> list[str]:
    """保留 ACL 类首页证据中的完整 venue 原文，供书目预审选择。"""
    candidates = []
    for line in evidence_lines or []:
        match = re.search(
            r"\b(?:Proceedings|Findings)\s+of\s+.+?(?=,\s*(?:pages?\b|pp\.?\b)|$)",
            str(line), re.I,
        )
        if match:
            candidates.append(match.group(0).strip(" ."))
    return _unique_nonempty(candidates)


def build_bibliographic_candidates(bibliography: dict | None, md_text: str) -> dict:
    """从 PDF 近端证据与 MinerU 文本生成程序候选。

    PDF metadata 与 paper.md 机械提取都可能混入机构，因此后续 Worker 优先
    选择候选 ID；只有作者候选不完整时才可提交带 Raw locator 的逐人修正。
    """
    bibliography = bibliography or {}
    pdf_authors = [str(author).strip() for author in (bibliography.get("authors") or [])]
    md_authors = _unique_nonempty(
        [*extract_authors_from_text(md_text), *_repeated_title_authors(md_text)]
    )
    first_page_evidence = bibliography.get("first_page_evidence") or []
    identity_region = bibliographic_identity_region(md_text)
    return {
        "doc_type": "paper",
        "title": _unique_nonempty([bibliography.get("title"), extract_title_from_md(md_text)]),
        "authors": _unique_nonempty(pdf_authors + [str(author) for author in md_authors]),
        "year": _unique_nonempty([bibliography.get("year"), extract_year_from_md(md_text)]),
        "venue": _unique_nonempty(
            [bibliography.get("venue")] + _first_page_venue_candidates(first_page_evidence)
        ),
        "doi": _unique_nonempty([bibliography.get("doi"), extract_doi(identity_region)]),
        "arxiv_id": _unique_nonempty(
            [bibliography.get("arxiv_id"), extract_arxiv_id(identity_region)]
        ),
        "evidence": bibliography.get("evidence") or {},
        "first_page_evidence": first_page_evidence,
    }


def _bibliographic_scalar_field(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"value", "evidence", "status"}:
        return False
    return (
        isinstance(value.get("value"), str)
        and isinstance(value.get("evidence"), str)
        and value.get("status") in {"confirmed", "corrected", "ambiguous"}
    )


def bibliographic_review_schema(value) -> bool:
    """书目预审 JSON 结构校验；值的边界由 validate_bibliographic_review 再约束。"""
    if not isinstance(value, dict) or set(value) != {
        "doc_type", "review_status", "bibliographic", "conflicts", "review_notes"
    }:
        return False
    if value.get("doc_type") not in {"paper", "document", "ambiguous"}:
        return False
    if value.get("review_status") not in {"clean", "corrected", "ambiguous", "manual_required"}:
        return False
    if not isinstance(value.get("conflicts"), list) or not isinstance(value.get("review_notes"), list):
        return False
    if not all(isinstance(item, (dict, str)) for item in value.get("conflicts", [])):
        return False
    if not all(isinstance(item, str) for item in value.get("review_notes", [])):
        return False
    bib = value.get("bibliographic")
    if not isinstance(bib, dict) or set(bib) != set(BIBLIOGRAPHIC_REVIEW_FIELDS):
        return False
    if not _bibliographic_scalar_field(bib.get("title")):
        return False
    if not _bibliographic_scalar_field(bib.get("venue")):
        return False
    if not _bibliographic_scalar_field(bib.get("doi")):
        return False
    if not _bibliographic_scalar_field(bib.get("arxiv_id")):
        return False
    year = bib.get("year")
    if not isinstance(year, dict) or set(year) != {"value", "evidence", "kind", "status"}:
        return False
    if not (isinstance(year.get("value"), str) and isinstance(year.get("evidence"), str)):
        return False
    if year.get("kind") not in {"published", "accepted", "received", "revised", "unknown"}:
        return False
    if year.get("status") not in {"confirmed", "corrected", "ambiguous"}:
        return False
    authors = bib.get("authors")
    if not isinstance(authors, dict) or set(authors) != {"value", "evidence", "rejected", "status"}:
        return False
    if not all(isinstance(item, str) and item.strip() for item in authors.get("value", [])):
        return False
    if not all(isinstance(item, str) for item in authors.get("rejected", [])):
        return False
    if authors.get("status") not in {"confirmed", "corrected", "ambiguous"}:
        return False
    return True


def bibliographic_decision_schema(value) -> bool:
    """Validate the compact candidate-ID plus evidence-bound author response."""
    if not isinstance(value, dict) or set(value) != {
        "protocol_version", "doc_type", "review_status", "selections",
        "conflicts", "review_notes",
    }:
        return False
    if value.get("protocol_version") != BIBLIOGRAPHIC_DECISION_PROTOCOL:
        return False
    if value.get("doc_type") not in {"paper", "document", "ambiguous"}:
        return False
    if value.get("review_status") not in {
        "clean", "corrected", "ambiguous", "manual_required",
    }:
        return False
    if not isinstance(value.get("conflicts"), list) or not all(
        isinstance(item, (dict, str)) for item in value["conflicts"]
    ):
        return False
    if not isinstance(value.get("review_notes"), list) or not all(
        isinstance(item, str) for item in value["review_notes"]
    ):
        return False
    selections = value.get("selections")
    if not isinstance(selections, dict) or set(selections) != set(BIBLIOGRAPHIC_REVIEW_FIELDS):
        return False
    for field in ("title", "venue", "doi", "arxiv_id"):
        item = selections.get(field)
        if not isinstance(item, dict) or set(item) != {"candidate_id", "status"}:
            return False
        if not isinstance(item.get("candidate_id"), str):
            return False
        if item.get("status") not in {"confirmed", "corrected", "ambiguous"}:
            return False
    year = selections.get("year")
    if not isinstance(year, dict) or set(year) != {"candidate_id", "kind", "status"}:
        return False
    if not isinstance(year.get("candidate_id"), str):
        return False
    if year.get("kind") not in {"published", "accepted", "received", "revised", "unknown"}:
        return False
    if year.get("status") not in {"confirmed", "corrected", "ambiguous"}:
        return False
    authors = selections.get("authors")
    if not isinstance(authors, dict) or set(authors) != {
        "accepted_ids", "rejected_ids", "proposed", "status",
    }:
        return False
    if not isinstance(authors.get("accepted_ids"), list) or not isinstance(
        authors.get("rejected_ids"), list
    ):
        return False
    if not all(isinstance(item, str) for item in authors.get("accepted_ids", [])):
        return False
    if not all(isinstance(item, str) for item in authors.get("rejected_ids", [])):
        return False
    proposed = authors.get("proposed")
    if not isinstance(proposed, list) or not all(
        isinstance(item, dict)
        and set(item) == {"value", "evidence"}
        and isinstance(item.get("value"), str)
        and bool(item["value"].strip())
        and isinstance(item.get("evidence"), str)
        and bool(item["evidence"].strip())
        for item in proposed
    ):
        return False
    return authors.get("status") in {"confirmed", "corrected", "ambiguous"}


def _candidate_evidence(value: str, field: str, bibliography: dict, md_text: str) -> str:
    """Locate candidate evidence deterministically; never ask the worker for locators."""
    source_value = bibliography.get(field)
    source_values = source_value if isinstance(source_value, list) else [source_value]
    if value in [str(item or "").strip() for item in source_values]:
        evidence = (bibliography.get("evidence") or {}).get(field)
        if evidence:
            return str(evidence)
        return "pdf_metadata"
    needle = _bibliographic_text_key(value)
    lines = md_text.splitlines()
    max_window = 4 if field == "title" else 2
    for start in range(min(len(lines), 160)):
        for end in range(start, min(start + max_window, len(lines), 160)):
            haystack = _bibliographic_text_key(_bibliographic_evidence_text(lines[start:end + 1]))
            if needle and needle in haystack:
                first, last = start + 1, end + 1
                return f"paper.md#L{first}" if first == last else f"paper.md#L{first}-L{last}"
    return "program_candidate"


def build_bibliographic_candidate_catalog(
    candidates: dict, bibliography: dict | None, md_text: str,
) -> dict:
    """Assign stable IDs and program-owned evidence to every candidate."""
    bibliography = bibliography or {}
    fields = {}
    for field in BIBLIOGRAPHIC_REVIEW_FIELDS:
        prefix = "author" if field == "authors" else field
        fields[field] = [
            {
                "id": f"{prefix}-{index:02d}",
                "value": value,
                "evidence": _candidate_evidence(value, field, bibliography, md_text),
            }
            for index, value in enumerate(candidates.get(field) or [], 1)
        ]
    return {"protocol_version": BIBLIOGRAPHIC_DECISION_PROTOCOL, "fields": fields}


def compile_bibliographic_decision(
    decision: dict, catalog: dict, md_text: str = "",
) -> dict:
    """Compile candidate IDs and validated author proposals into locked metadata."""
    if not bibliographic_decision_schema(decision):
        raise ValueError("candidate-id 书目裁决不符合 schema")
    indexes = {
        field: {item["id"]: item for item in catalog.get("fields", {}).get(field, [])}
        for field in BIBLIOGRAPHIC_REVIEW_FIELDS
    }

    def selected(field: str, candidate_id: str) -> tuple[str, str]:
        if not candidate_id:
            return "", ""
        item = indexes[field].get(candidate_id)
        if not item:
            raise ValueError(f"{field} 引用了未知 candidate_id: {candidate_id}")
        return item["value"], item["evidence"]

    selections = decision["selections"]
    bibliographic = {}
    for field in ("title", "venue", "doi", "arxiv_id"):
        candidate_id = selections[field]["candidate_id"]
        status = selections[field]["status"]
        if field in {"venue", "doi", "arxiv_id"} and not indexes[field] and not candidate_id:
            # Empty optional fields contain no Worker decision: the program owns
            # both the empty value and its uncertainty state.
            status = "ambiguous"
        elif not candidate_id and status != "ambiguous":
            raise ValueError(f"{field} 未选择 candidate_id 时 status 必须为 ambiguous")
        value, evidence = selected(field, candidate_id)
        bibliographic[field] = {
            "value": value, "evidence": evidence, "status": status,
        }
    if not selections["year"]["candidate_id"] and selections["year"]["status"] != "ambiguous":
        raise ValueError("year 未选择 candidate_id 时 status 必须为 ambiguous")
    year_value, year_evidence = selected("year", selections["year"]["candidate_id"])
    bibliographic["year"] = {
        "value": year_value,
        "evidence": year_evidence,
        "kind": selections["year"]["kind"],
        "status": selections["year"]["status"],
    }
    accepted_ids = selections["authors"]["accepted_ids"]
    rejected_ids = selections["authors"]["rejected_ids"]
    proposed = selections["authors"]["proposed"]
    if len(set(accepted_ids)) != len(accepted_ids) or len(set(rejected_ids)) != len(rejected_ids):
        raise ValueError("authors candidate_id 不得重复")
    if set(accepted_ids) & set(rejected_ids):
        raise ValueError("同一 author candidate_id 不能同时接受和拒绝")
    if proposed and accepted_ids:
        raise ValueError("authors.proposed 非空时 accepted_ids 必须为空")
    if proposed and (
            selections["authors"]["status"] != "corrected"
            or decision["review_status"] != "corrected"):
        raise ValueError("authors.proposed 必须将 authors.status 与 review_status 标为 corrected")
    if not accepted_ids and not proposed and selections["authors"]["status"] != "ambiguous":
        raise ValueError("authors 未接受 candidate_id 或 proposed 时 status 必须为 ambiguous")
    proposal_errors = _validate_author_proposals(proposed, md_text)
    if proposal_errors:
        raise ValueError("authors.proposed 校验失败: " + "; ".join(proposal_errors))
    accepted = (
        [(item["value"].strip(), item["evidence"].strip()) for item in proposed]
        if proposed else
        [selected("authors", candidate_id) for candidate_id in accepted_ids]
    )
    rejected = [selected("authors", candidate_id) for candidate_id in rejected_ids]
    author_evidence = ", ".join(dict.fromkeys(
        evidence for _value, evidence in accepted + rejected if evidence
    ))
    bibliographic["authors"] = {
        "value": [value for value, _evidence in accepted],
        "evidence": author_evidence,
        "rejected": [value for value, _evidence in rejected],
        "status": selections["authors"]["status"],
    }
    return {
        "doc_type": decision["doc_type"],
        "review_status": decision["review_status"],
        "bibliographic": bibliographic,
        "conflicts": decision["conflicts"],
        "review_notes": decision["review_notes"],
    }


def _deterministic_bibliographic_decision(
    bibliography: dict | None, candidates: dict, catalog: dict,
) -> dict | None:
    """Skip the worker only for a conservative, strong-identifier singleton case."""
    bibliography = bibliography or {}
    if not (candidates.get("doi") or candidates.get("arxiv_id")):
        return None
    if any(len(candidates.get(field) or []) > 1 for field in BIBLIOGRAPHIC_SCALAR_FIELDS):
        return None
    if not candidates.get("title") or not candidates.get("year"):
        return None
    pdf_authors = _unique_nonempty(list(bibliography.get("authors") or []))
    candidate_authors = list(candidates.get("authors") or [])
    if not pdf_authors or candidate_authors != pdf_authors:
        return None
    if any(AFFILIATION_HINT_RE.search(author) for author in candidate_authors):
        return None
    fields = catalog["fields"]
    year_evidence = str((bibliography.get("evidence") or {}).get("year") or "").casefold()
    year_kind = "published" if "published" in year_evidence else "unknown"

    def only_id(field: str) -> str:
        return fields[field][0]["id"] if fields[field] else ""

    return {
        "protocol_version": BIBLIOGRAPHIC_DECISION_PROTOCOL,
        "doc_type": "paper",
        "review_status": "clean",
        "selections": {
            "title": {"candidate_id": only_id("title"), "status": "confirmed"},
            "authors": {
                "accepted_ids": [item["id"] for item in fields["authors"]],
                "rejected_ids": [],
                "proposed": [],
                "status": "confirmed",
            },
            "year": {
                "candidate_id": only_id("year"), "kind": year_kind, "status": "confirmed",
            },
            "venue": {
                "candidate_id": only_id("venue"),
                "status": "confirmed" if fields["venue"] else "ambiguous",
            },
            "doi": {
                "candidate_id": only_id("doi"),
                "status": "confirmed" if fields["doi"] else "ambiguous",
            },
            "arxiv_id": {
                "candidate_id": only_id("arxiv_id"),
                "status": "confirmed" if fields["arxiv_id"] else "ambiguous",
            },
        },
        "conflicts": [],
        "review_notes": ["deterministic_fast_path: strong identifier and singleton candidates"],
    }


def _bibliographic_worker_input_hash(catalog: dict, md_text: str) -> str:
    title_view, evidence_view = _paper_md_review_view(md_text)
    payload = {
        "protocol_version": BIBLIOGRAPHIC_DECISION_PROTOCOL,
        "catalog": catalog,
        "title_view": title_view,
        "evidence_view": evidence_view,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _bibliographic_decision_cache_path(transaction_id: str) -> Path | None:
    if not transaction_id:
        return None
    return REPO / "temp" / "inbox-state" / f"{transaction_id}-bibliographic-decision.json"


def _load_bibliographic_decision_cache(transaction_id: str, input_hash: str) -> dict | None:
    path = _bibliographic_decision_cache_path(transaction_id)
    if not path or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision")
    if (payload.get("protocol_version") != BIBLIOGRAPHIC_DECISION_PROTOCOL
            or payload.get("input_hash") != input_hash
            or not bibliographic_decision_schema(decision)):
        return None
    return decision


def _save_bibliographic_decision_cache(
    transaction_id: str, input_hash: str, decision: dict,
) -> None:
    path = _bibliographic_decision_cache_path(transaction_id)
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "protocol_version": BIBLIOGRAPHIC_DECISION_PROTOCOL,
        "input_hash": input_hash,
        "decision": decision,
    }
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _bibliographic_text_key(value: str) -> str:
    """Compare evidence-bound titles across harmless PDF/Markdown typography."""
    text = normalize("NFKC", str(value or ""))
    text = re.sub(r"</?sup\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s*[-‐‑–—]\s*", "-", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _bibliographic_evidence_text(lines: list[str]) -> str:
    """Join evidence lines while discarding Markdown heading syntax only."""
    return " ".join(re.sub(r"^\s*#{1,6}\s*", "", line) for line in lines)


def _bibliographic_evidence_contains(value: str, locator: str, md_text: str) -> bool:
    """Accept only when one explicitly declared paper.md line range contains the value."""
    locator_text = str(locator or "").strip()
    locator_parts = re.split(r"\s*[,;]\s*", locator_text)
    if len(locator_parts) > 1:
        return all(locator_parts) and any(
            _bibliographic_evidence_contains(value, part, md_text)
            for part in locator_parts
        )
    match = re.fullmatch(r"paper\.md#L(\d+)(?:-L?(\d+))?", locator_text)
    if not match or not md_text:
        return False
    lines = md_text.splitlines()
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end < start or end > len(lines) or end - start > 3:
        return False
    needle = _bibliographic_text_key(value)
    haystack = _bibliographic_text_key(_bibliographic_evidence_text(lines[start - 1:end]))
    return len(needle) >= 2 and needle in haystack


def _author_value_issue(value: str) -> str:
    """Return a deterministic reason when one author value is not one person."""
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return "empty author"
    if re.search(r"\bet\s+al\.?\b", text, re.I):
        return "et al is not an author identity"
    if AFFILIATION_HINT_RE.search(text):
        return "affiliation-like author"
    if re.search(r"\s+(?:and|&)\s+", text, re.I) or ";" in text or "、" in text:
        return "multiple authors in one value"
    if text.count(",") + text.count("，") >= 2:
        return "multiple authors in one comma-delimited value"
    comma_parts = [part.strip() for part in re.split(r"[,，]", text)]
    if len(comma_parts) == 2 and all(len(part.split()) >= 2 for part in comma_parts):
        return "multiple authors in one comma-delimited value"
    return ""


def _validate_author_proposals(proposed: list[dict], md_text: str) -> list[str]:
    """Validate Worker author expansions against the bounded title-page evidence."""
    errors = []
    seen = set()
    order_keys = []
    lines = md_text.splitlines()
    for item in proposed:
        value = str(item.get("value") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        match = re.fullmatch(r"paper\.md#L(\d+)(?:-L?(\d+))?", evidence)
        if not match or int(match.group(2) or match.group(1)) > 40:
            errors.append(f"{value}: evidence 必须位于 Worker 标题邻域前 40 行")
            continue
        if not _bibliographic_evidence_contains(value, evidence, md_text):
            errors.append(f"{value}: 未在 {evidence} 逐字命中")
        else:
            start = int(match.group(1))
            end = int(match.group(2) or start)
            haystack = _bibliographic_text_key(
                _bibliographic_evidence_text(lines[start - 1:end]))
            order_keys.append((start, haystack.find(_bibliographic_text_key(value))))
        issue = _author_value_issue(value)
        if issue:
            errors.append(f"{value}: {issue}")
        key = _bibliographic_text_key(value)
        if key in seen:
            errors.append(f"{value}: proposed 作者重复")
        seen.add(key)
    if len(order_keys) == len(proposed) and order_keys != sorted(order_keys):
        errors.append("proposed 作者顺序与 Raw 证据不一致")
    return errors


def repair_bibliographic_evidence_locators(review: dict, md_text: str) -> list[dict]:
    """Relocate wrong title/venue evidence only when one exact local window matches."""
    lines = md_text.splitlines()
    repairs = []
    bib = review.get("bibliographic") or {}
    for field in ("title", "venue"):
        item = bib.get(field) or {}
        value = str(item.get("value") or "").strip()
        old_locator = str(item.get("evidence") or "").strip()
        if not value or _bibliographic_evidence_contains(value, old_locator, md_text):
            continue
        needle = _bibliographic_text_key(value)
        hits = []
        limit = min(len(lines), 160)
        for start in range(limit):
            first_line = _bibliographic_text_key(_bibliographic_evidence_text([lines[start]]))
            if not first_line:
                continue
            # MinerU may split one title across headings separated by blank
            # lines.  Search the same maximum four-line evidence window that
            # the validator accepts, but retain only the smallest matching
            # window whose occurrence begins on its first line.  This avoids
            # manufacturing several hits from windows that merely precede the
            # actual occurrence.
            for end in range(start, min(start + 4, limit)):
                haystack = _bibliographic_text_key(
                    _bibliographic_evidence_text(lines[start:end + 1])
                )
                offset = haystack.find(needle)
                if offset < 0 or offset >= len(first_line):
                    continue
                hits.append((start + 1, end + 1))
                break
        if len(hits) != 1:
            continue
        start, end = hits[0]
        new_locator = f"paper.md#L{start}" if start == end else f"paper.md#L{start}-L{end}"
        item["evidence"] = new_locator
        repairs.append({
            "field": f"{field}.evidence",
            "action": "relocate_unique_exact_evidence",
            "from": old_locator,
            "to": new_locator,
            "value": value,
        })
    return repairs


def normalize_bibliographic_review(review: dict, candidates: dict) -> list[dict]:
    """Constrain harmless negative author annotations to the candidate boundary."""
    normalizations = []
    bib = review.setdefault("bibliographic", {})
    authors = bib.setdefault("authors", {})
    candidate_authors = set(candidates.get("authors") or [])
    rejected = list(authors.get("rejected") or [])
    outside = [value for value in rejected if value not in candidate_authors]
    if outside:
        authors["rejected"] = [value for value in rejected if value in candidate_authors]
        notes = review.setdefault("review_notes", [])
        notes.append("候选外 affiliation 已从 authors.rejected 移除: " + "; ".join(outside))
        normalizations.append({
            "field": "authors.rejected",
            "action": "move_out_of_candidate_values_to_review_notes",
            "values": outside,
        })
    for field in ("doi", "arxiv_id"):
        values = list(candidates.get(field) or [])
        item = bib.setdefault(field, {})
        if len(values) == 1 and not str(item.get("value") or "").strip():
            item["value"] = values[0]
            item["evidence"] = "program_candidate"
            item["status"] = "confirmed"
            normalizations.append({
                "field": field,
                "action": "lock_unique_program_candidate",
                "value": values[0],
            })
    return normalizations


def validate_bibliographic_review(
    review: dict,
    candidates: dict,
    md_text: str = "",
) -> list[str]:
    """在 schema 之外复查候选边界与基础格式。"""
    errors = []
    bib = review.get("bibliographic", {})
    candidate_sets = {
        field: set(candidates.get(field) or [])
        for field in BIBLIOGRAPHIC_REVIEW_FIELDS
    }
    for field in ("title", "venue", "doi", "arxiv_id"):
        item = bib.get(field)
        raw_value = (item or {}).get("value", "").strip()
        if not raw_value:
            continue
        evidence_bound = field in {"title", "venue"} and _bibliographic_evidence_contains(
            raw_value, (item or {}).get("evidence", ""), md_text,
        )
        if not candidate_sets[field] and not evidence_bound:
            errors.append(f"{field} 程序候选为空却生成值")
        elif raw_value not in candidate_sets[field] and not (
            field == "title" and any(
                _bibliographic_text_key(raw_value) == _bibliographic_text_key(candidate)
                for candidate in candidate_sets[field]
            )
        ) and not evidence_bound:
            errors.append(f"{field} 值不在程序候选中")
        if field == "doi" and not re.fullmatch(r"10\.\d{4,}/[^\s\"<>]+", raw_value, re.I):
            errors.append("doi 格式错误")
        if field == "arxiv_id" and not re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", raw_value):
            errors.append("arxiv_id 格式错误")
    year = bib.get("year") or {}
    year_value = str(year.get("value") or "").strip()
    if year_value and not re.fullmatch(r"(?:19|20)\d{2}", year_value):
        errors.append("year 格式错误")
    if year_value and not candidate_sets["year"]:
        errors.append("year 程序候选为空却生成值")
    elif year_value and year_value not in candidate_sets["year"]:
        errors.append("year 值不在程序候选中")
    authors = bib.get("authors") or {}
    author_candidates = candidate_sets["authors"]
    accepted = set(authors.get("value") or [])
    rejected = set(authors.get("rejected") or [])
    unknown_authors = {
        author for author in accepted - author_candidates
        if not _bibliographic_evidence_contains(
            author, authors.get("evidence", ""), md_text,
        )
    }
    unknown_rejected = {
        fragment for fragment in rejected - author_candidates
        if not _bibliographic_evidence_contains(
            fragment, authors.get("evidence", ""), md_text,
        )
    }
    if unknown_authors:
        errors.append("authors 包含程序候选之外的值: " + ", ".join(sorted(unknown_authors)))
    if unknown_rejected:
        errors.append("rejected 包含未出现在作者候选项中的片段: " + ", ".join(sorted(unknown_rejected)))
    if accepted & rejected:
        errors.append("authors 不能同时出现在 value 和 rejected")
    for author in authors.get("value") or []:
        issue = _author_value_issue(author)
        if issue:
            errors.append(f"authors 值不是单一人物: {author} ({issue})")
    expected_authors = _repeated_title_authors(md_text)
    missing_authors = [author for author in expected_authors if author not in accepted]
    if missing_authors:
        errors.append("authors 缺少标题作者块中的姓名: " + ", ".join(missing_authors))
    if (review.get("doc_type") == "paper"
            and review.get("review_status") != "manual_required"
            and not bib.get("title", {}).get("value", "").strip()):
        errors.append("paper 必须给出 title 或标 ambiguous")
    return errors


def _paper_md_review_view(md_text: str) -> tuple[str, str]:
    """生成标题邻域和发表证据行窗口，不把全文送入书目预审。"""
    lines = md_text.splitlines()
    title_lines = []
    for index, line in enumerate(lines[:40], 1):
        title_lines.append(f"paper.md#L{index}: {line.strip()}")
    evidence_pattern = re.compile(
        r"\bDOI\b|doi\.org|10\.\d{4,}/\s|\bPublished\b|\bReceived\b|\bRevised\b|"
        r"\bAccepted\b|\bProceedings of\b|Association for Computational Linguistics|[©Ⓒ]",
        re.IGNORECASE,
    )
    evidence_lines = []
    for index, line in enumerate(lines, 1):
        if evidence_pattern.search(line):
            evidence_lines.append(f"paper.md#L{index}: {line.strip()}")
            if len(evidence_lines) >= 20:
                break
    if not evidence_lines:
        evidence_lines = title_lines[-10:]
    return "\n".join(title_lines), "\n".join(evidence_lines)


def build_bibliographic_review_prompt(catalog: dict, md_text: str) -> str:
    title_view, evidence_view = _paper_md_review_view(md_text)
    return f"""你是受程序约束的论文书目裁决 Worker。一次处理整篇书目，优先返回候选 ID；只有作者候选不完整时可从给定标题邻域逐字提出带 evidence locator 的作者。其他字段不得复制、改写或生成候选外字符串或 locator。

目标：锁定本篇论文的书目事实；排除机构/实验室/大学/公司等 affiliation 片段；year 区分 published/accepted/received/revised，发表年份优先。

候选目录（value/evidence 只用于判断，输出只能引用 id）：
{json.dumps(catalog, ensure_ascii=False)}

标题邻域：
{title_view}

发表证据行：
{evidence_view}

只输出 JSON，不得输出解释：
{{
  "protocol_version": "{BIBLIOGRAPHIC_DECISION_PROTOCOL}",
  "doc_type": "paper|document|ambiguous",
  "review_status": "clean|corrected|ambiguous|manual_required",
  "selections": {{
    "title": {{"candidate_id": "title-01", "status": "confirmed|corrected|ambiguous"}},
    "authors": {{"accepted_ids": ["author-01"], "rejected_ids": ["author-02"], "proposed": [{{"value": "Raw 中的单个作者名", "evidence": "paper.md#L12"}}], "status": "confirmed|corrected|ambiguous"}},
    "year": {{"candidate_id": "year-01", "kind": "published|accepted|received|revised|unknown", "status": "confirmed|corrected|ambiguous"}},
    "venue": {{"candidate_id": "", "status": "ambiguous"}},
    "doi": {{"candidate_id": "", "status": "ambiguous"}},
    "arxiv_id": {{"candidate_id": "", "status": "ambiguous"}}
  }},
  "conflicts": [],
  "review_notes": []
}}

约束：
1. 只能引用候选目录中对应字段的 id；不选择时 candidate_id 写空字符串。
2. authors.accepted_ids/rejected_ids 只能引用 author-*，不得重复或交叉。
3. 候选作者完整时 proposed=[]；候选缺失或含多人合并值时，accepted_ids=[]，proposed 必须给出标题邻域中完整、有序、逐人拆分的作者列表，每项 value 必须逐字出现在 evidence 指向的前 40 行窗口。
4. proposed 不得包含 et al、机构或多人合并值；使用 proposed 时 authors.status 与 review_status 都必须为 corrected。
5. rejected_ids 只标记候选目录中误识别为作者的机构或多人合并值；候选外机构仅可在 review_notes 描述。
6. 无法裁决时对应 status=ambiguous 且 review_status=manual_required；不要猜测。
7. 一次返回所有字段，禁止建议后续逐字段调用。"""


def merge_bibliographic_review(bibliography: dict | None, review: dict) -> dict:
    """把 LLM 裁决结果合并进最终书目元数据，并标记为 locked。"""
    merged = dict(bibliography or {})
    review_bib = review.get("bibliographic", {})
    review_evidence = {}
    for field in BIBLIOGRAPHIC_REVIEW_FIELDS:
        item = review_bib.get(field)
        if not isinstance(item, dict):
            continue
        field_value = item.get("value")
        if field == "authors":
            merged["authors"] = [str(author) for author in (field_value or [])]
            merged["authors_rejected"] = [str(author) for author in (item.get("rejected") or [])]
        else:
            merged[field] = field_value
        review_evidence[field] = item.get("evidence", "")
    merged["doc_type"] = review.get("doc_type", "paper")
    merged["review"] = {
        "status": review.get("review_status", "ambiguous"),
        "doc_type": review.get("doc_type", "paper"),
        "locked": True,
        "conflicts": review.get("conflicts", []),
        "review_notes": review.get("review_notes", []),
    }
    merged["review_evidence"] = review_evidence
    return merged


def review_bibliographic_metadata(bibliography: dict | None, md_text: str,
                                  transaction_id: str = "") -> dict:
    """在 paper.md 落盘后、persist 前执行轻量书目预审。

    返回字典中的 status：
    - ok: merged 为锁定书目
    - agent_required: 应由当前 agent 处理 prompt
    - bibliographic_review_required: 类型不是 paper 或无法形成可恢复裁决
    - validation_error: 结构/候选边界未通过
    """
    bibliography = bibliography or {}
    candidates = build_bibliographic_candidates(bibliography, md_text)
    catalog = build_bibliographic_candidate_catalog(candidates, bibliography, md_text)
    input_hash = _bibliographic_worker_input_hash(catalog, md_text)
    worker = {
        "protocol_version": BIBLIOGRAPHIC_DECISION_PROTOCOL,
        "input_hash": input_hash,
        "api_called": False,
        "cache_hit": False,
        "skipped": False,
        "skip_reason": "",
    }
    decision = _deterministic_bibliographic_decision(bibliography, candidates, catalog)
    if decision:
        worker["skipped"] = True
        worker["skip_reason"] = "deterministic_fast_path"
    else:
        decision = _load_bibliographic_decision_cache(transaction_id, input_hash)
        if decision:
            worker["cache_hit"] = True
            worker["skip_reason"] = "transaction_cache"
    prompt = build_bibliographic_review_prompt(catalog, md_text)
    if not decision:
        result = call_json(
            prompt,
            bibliographic_decision_schema,
            max_tokens=1000,
            retries=0,
            operation=BIBLIOGRAPHIC_REVIEW_OPERATION,
            transaction_id=transaction_id,
            system=(
                "你是证据约束的书目裁决 Worker。优先引用候选 ID；仅作者候选不完整时，"
                "可从给定 paper.md 标题邻域逐字提出带 locator 的作者，并输出 JSON。"
            ),
        )
        worker["api_called"] = bool(result.get("history"))
        if result.get("status") == "agent_required":
            return {
                "ok": False,
                "status": "agent_required",
                "agent_prompt": result.get("prompt", prompt),
                "candidates": candidates,
                "catalog": catalog,
                "input_hash": input_hash,
                "worker": worker,
            }
        if not result.get("ok"):
            return {
                "ok": False,
                "status": "validation_error",
                "error": result.get("error", "书目候选 ID 裁决调用失败"),
                "review": result.get("parsed") or {},
                "candidates": candidates,
                "catalog": catalog,
                "input_hash": input_hash,
                "prompt": prompt,
                "worker": worker,
            }
        decision = result.get("parsed")
    try:
        review = compile_bibliographic_decision(decision, catalog, md_text)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "validation_error",
            "error": str(exc),
            "review": decision,
            "candidates": candidates,
            "catalog": catalog,
            "input_hash": input_hash,
            "prompt": prompt,
            "worker": worker,
        }
    normalizations = normalize_bibliographic_review(review, candidates)
    errors = validate_bibliographic_review(review, candidates, md_text)
    if errors:
        return {
            "ok": False,
            "status": "validation_error",
            "error": "书目预审候选校验失败: " + "; ".join(errors),
            "review": decision,
            "compiled_review": review,
            "candidates": candidates,
            "catalog": catalog,
            "input_hash": input_hash,
            "prompt": prompt,
            "normalizations": normalizations,
            "worker": worker,
        }
    if not worker["skipped"]:
        _save_bibliographic_decision_cache(transaction_id, input_hash, decision)
    if review.get("doc_type") == "paper" and review.get("review_status") == "manual_required":
        return {
            "ok": False,
            "status": "agent_required",
            "agent_prompt": (
                prompt
                + "\n\nWorker 无法锁定作者或书目字段。请作为 Agent 复核上述有限证据，"
                + f"仍按 {BIBLIOGRAPHIC_DECISION_PROTOCOL} schema 输出；"
                + "作者候选不完整时可使用 evidence-bound proposed。"
                + "\n\nWorker 未决裁决：\n"
                + json.dumps(decision, ensure_ascii=False, indent=2)
            ),
            "review": review,
            "decision": decision,
            "candidates": candidates,
            "catalog": catalog,
            "input_hash": input_hash,
            "worker": worker,
        }
    if (review.get("doc_type") != "paper"
            or review.get("review_status") == "ambiguous"):
        reason = f"review_status={review.get('review_status')}" if review.get("doc_type") == "paper" else f"doc_type={review.get('doc_type')}"
        return {
            "ok": False,
            "status": "bibliographic_review_required",
            "error": f"书目预审无法锁定论文书目（{reason}）",
            "review": review,
            "candidates": candidates,
            "catalog": catalog,
            "input_hash": input_hash,
            "worker": worker,
        }
    return {
        "ok": True,
        "status": "ok",
        "bibliographic": merge_bibliographic_review(bibliography, review),
        "review": review,
        "candidates": candidates,
        "catalog": catalog,
        "decision": decision,
        "input_hash": input_hash,
        "worker": worker,
    }


def _bibliographic_review_draft_path(state: dict) -> Path:
    """agent 模式书目预审的交接草稿文件，位于当前事务临时目录。"""
    return REPO / state["extract_dir"] / "bibliographic-review.json"


def _resume_bibliographic_review(state: dict) -> bool:
    """离线重校验已有 review 或读取 agent 草稿；未就绪返回 False。"""
    review_state = state.get("bibliographic_review") or {}
    stored_validation_error = (
        state.get("status") == "bibliographic_review_required"
        and review_state.get("status") == "validation_error"
        and isinstance(review_state.get("review"), dict)
    )
    if stored_validation_error:
        draft_path = REPO / str(review_state.get("draft_path") or "")
        if review_state.get("draft_path") and draft_path.is_file():
            try:
                review = json.loads(draft_path.read_text(encoding="utf-8"))
            except Exception as exc:
                state["errors"] = [f"书目预审草稿读取失败: {exc}"]
                return False
        else:
            review = review_state["review"]
    else:
        if state.get("status") != "agent_required" or review_state.get("status") != "agent_required":
            return False
        draft_path = _bibliographic_review_draft_path(state)
        if not draft_path.is_file():
            return False
        try:
            review = json.loads(draft_path.read_text(encoding="utf-8"))
        except Exception as exc:
            state["errors"] = [f"书目预审草稿读取失败: {exc}"]
            return False
    candidates = review_state.get("candidates") or build_bibliographic_candidates(
        state.get("bibliographic_meta"), (REPO / state["extract_dir"] / "paper.md").read_text(encoding="utf-8"))
    md_text = (REPO / state["extract_dir"] / "paper.md").read_text(encoding="utf-8")
    catalog = review_state.get("catalog") or build_bibliographic_candidate_catalog(
        candidates, state.get("bibliographic_meta"), md_text,
    )
    decision = review if bibliographic_decision_schema(review) else None
    if decision:
        try:
            review = compile_bibliographic_decision(decision, catalog, md_text)
        except ValueError as exc:
            state["errors"] = [str(exc)]
            return False
    elif not bibliographic_review_schema(review):
        state["errors"] = ["书目预审草稿不符合 candidate-id 或 legacy JSON schema"]
        return False
    normalize_bibliographic_review(review, candidates)
    errors = validate_bibliographic_review(review, candidates, md_text)
    if errors:
        state["errors"] = errors
        return False
    if (review.get("doc_type") != "paper"
            or review.get("review_status") in {"ambiguous", "manual_required"}):
        state["bibliographic_review_required"] = True
        inbox_state.transition(
            state, "bibliographic_review_required",
            reason="bibliographic_review_requires_human_decision",
        )
        state["errors"] = ["书目预审无法锁定论文书目"]
        return True
    state["bibliographic_meta"] = merge_bibliographic_review(state.get("bibliographic_meta"), review)
    _record_bibliographic_quality_warnings(state, md_text)
    persist_bibliographic_metadata(REPO / state["extract_dir"], state["bibliographic_meta"])
    input_hash = review_state.get("input_hash") or _bibliographic_worker_input_hash(catalog, md_text)
    if decision:
        _save_bibliographic_decision_cache(state["transaction_id"], input_hash, decision)
    state["bibliographic_review"] = {
        "status": "ok",
        "review": review,
        "decision": decision,
        "candidates": candidates,
        "catalog": catalog,
        "input_hash": input_hash,
        "worker": review_state.get("worker", {}),
    }
    state["bibliographic_review_required"] = False
    inbox_state.transition(state, "write_wiki", reason="bibliographic_review_accepted")
    state["agent_required"] = False
    state["agent_prompt"] = ""
    state["pre_handoff_status"] = ""
    state["errors"] = []
    return True


def normalize_title(title: str) -> str:
    return deaccent(title).lower().strip()


def _bibliographic_identity(value: dict | None) -> tuple:
    value = value or {}
    authors = tuple(
        re.sub(r"\s+", " ", str(author)).strip().casefold()
        for author in value.get("authors", []) if str(author).strip()
    )
    return (
        normalize_title(str(value.get("title") or "")),
        authors,
        str(value.get("year") or "").strip(),
        re.sub(r"v\d+$", "", str(value.get("arxiv_id") or "").strip().casefold()),
        str(value.get("doi") or "").strip().casefold(),
    )


def _bibliography_for_raw_artifact(raw_path: str) -> dict:
    artifact = REPO / raw_path
    source_yaml = artifact.parent / "source.yaml"
    if not source_yaml.is_file():
        return {}
    try:
        payload = yaml.safe_load(source_yaml.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return payload.get("bibliographic") or {}


def _post_extract_duplicate(state: dict, paper_md: Path) -> dict | None:
    """Confirm a normalized-text candidate with locked bibliographic identity."""
    candidate = sf.lookup_text_candidate(paper_md)
    if not candidate:
        return None
    existing = _bibliography_for_raw_artifact(candidate.get("raw_path", ""))
    current_key = _bibliographic_identity(state.get("bibliographic_meta"))
    existing_key = _bibliographic_identity(existing)
    current_title, current_authors, current_year, current_arxiv, current_doi = current_key
    existing_title, existing_authors, existing_year, existing_arxiv, existing_doi = existing_key
    strong_id = bool(
        (current_doi and current_doi == existing_doi)
        or (current_arxiv and current_arxiv == existing_arxiv)
    )
    same_bibliography = bool(
        current_title and current_title == existing_title
        and current_authors and current_authors == existing_authors
        and current_year and current_year == existing_year
    )
    return candidate if strong_id or same_bibliography else None


def _graph_retry_context(state: dict) -> str:
    paths = [REPO / "cross-domain" / "graph.db"]
    for key in ("wiki_path", "semantic_path"):
        if state.get(key):
            paths.append(REPO / str(state[key]))
    snapshot = []
    for path in paths:
        if path.is_file():
            stat = path.stat()
            snapshot.append((str(path.relative_to(REPO)), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode("utf-8")).hexdigest()


def _mark_graph_failure(state: dict) -> None:
    if state.get("status") != "failed" or state.get("resume_from") != "graph_ready":
        return
    state["failure_signature"] = hashlib.sha256(
        json.dumps(state.get("errors", []), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    state["failure_context"] = _graph_retry_context(state)
    state["retryable"] = False
    state["next_action"] = "repair_graph_identity_or_metadata_then_resume"
    inbox_state.save(state["transaction_id"], state)


# ===== 3.1 dedup_check =====

TITLE_DEDUP_GATE = 0.95  # 标题相似度门槛：超过此值才作为候选进入 metadata 判断


def step_dedup_check(state: dict) -> tuple[bool, str]:
    """判断论文是否已被摄入：标题提取 + 查图 + 查 raw 目录。

    判重规则（2026-08-02）：标题相似度 > 0.95 才作为候选，
    再通过 metadata（DOI/arXiv ID）确认，之后才能判定重复。
    标题相似度 ≤ 0.95 时不判重，正常摄入。
    """
    import difflib
    pdf_path = REPO / state["source"]
    try:
        sf.ensure_index()
        fingerprint_match = sf.lookup_exact(pdf_path)
    except Exception as exc:
        fingerprint_match = None
        state.setdefault("quality_warnings", []).append({
            "issue": "fingerprint_index_unavailable", "detail": str(exc),
        })
    if fingerprint_match:
        state["source_fingerprint"] = {
            "binary_sha256": fingerprint_match["binary_sha256"],
            "size_bytes": fingerprint_match["size_bytes"],
        }
        state["dedup_title"] = pdf_path.name
        state["dedup_result"] = {
            "duplicate": True,
            "match": "binary_sha256",
            "raw_path": fingerprint_match["raw_path"],
            "binary_sha256": fingerprint_match["binary_sha256"],
        }
        return True, f"源文件 SHA-256 已存在: {fingerprint_match['raw_path']}"
    bibliography = extract_pdf_bibliography(pdf_path)
    state["bibliographic_meta"] = bibliography
    title = bibliography.get("title") or extract_title_from_pdf(pdf_path)
    if not title:
        return False, "无法从 PDF 提取标题"
    normalized = normalize_title(title)
    state["dedup_title"] = title
    state["dedup_normalized"] = normalized
    # 供判重与 paper-id 使用；与标题/年份共用同一次 PDF 预读取。
    state["arxiv_id"] = bibliography.get("arxiv_id", "")
    state["doi"] = bibliography.get("doi", "")
    # 查图（标题 vs 节点真实标题，> 0.95 才入候选）
    dup_graph = []
    try:
        import graph_lib as gl
        conn = gl.connect()
        rows = conn.execute(
            "SELECT path, title FROM nodes WHERE type IN ('page','entity') AND title != ''"
        ).fetchall()
        conn.close()
        for row in rows:
            node_title = normalize_title(row["title"] or "")
            if not node_title:
                continue
            ratio = difflib.SequenceMatcher(None, normalized, node_title).ratio()
            if ratio > TITLE_DEDUP_GATE:
                dup_graph.append({"path": row["path"], "title": row["title"], "ratio": round(ratio, 2)})
    except Exception:
        pass
    # 查 raw 目录（宽松预筛 → 读 paper.md 真实标题 → 0.95 精筛）
    dup_raw = []
    raw_refs = REPO / "academic" / "raw" / "references"
    if raw_refs.exists():
        for d in raw_refs.iterdir():
            if not d.is_dir():
                continue
            dir_name = d.name.lower()
            # 宽松预筛（避免读全部 paper.md）
            pre_ratio = difflib.SequenceMatcher(None, normalized, dir_name).ratio()
            if pre_ratio <= 0.5 and normalized not in dir_name and dir_name not in normalized:
                continue
            raw_md = d / "paper.md"
            if not raw_md.is_file():
                continue
            try:
                existing_title = normalize_title(
                    extract_title_from_md(raw_md.read_text(encoding="utf-8")))
            except Exception:
                continue
            if not existing_title:
                continue
            ratio = difflib.SequenceMatcher(None, normalized, existing_title).ratio()
            if ratio > TITLE_DEDUP_GATE:
                dup_raw.append({"dir": d.name, "title": existing_title,
                                "ratio": round(ratio, 2),
                                "raw_path": str(raw_md.relative_to(REPO))})
    if dup_graph or dup_raw:
        state["dedup_result"] = {"graph": dup_graph, "raw": dup_raw}
        state["relation_candidates"] = dup_graph + dup_raw
        # 候选已过 0.95 标题门槛 → 通过 metadata（DOI/arXiv ID）确认
        rel = detect_raw_relationship(state, dup_graph + dup_raw)
        state["raw_relationship"] = rel
        if rel["type"] == "duplicate":
            if not rel.get("uncertain"):
                target = rel.get("target_page") or rel.get("target_raw_dir") or ""
                return True, f"已确认摄入: title={title}, target={target}"
            state["dedup_result"] = {
                "duplicate": False,
                "reason": "title>0.95 without strong identifier; defer until post-extract",
                "relation_candidates": dup_graph + dup_raw,
            }
            return False, ""
        if rel["type"] in ("version", "supplementary", "translation") and not rel.get("uncertain"):
            state["dedup_result"] = {"duplicate": False, "raw_relationship": rel}
            return False, ""
        if rel["type"] in ("version",) and rel.get("uncertain"):
            target = rel.get("target_page") or rel.get("target_raw_dir") or ""
            return True, f"疑似版本关系（需 agent 确认）: title={title}, target={target}"
        # 标题 > 0.95 但 metadata 未确认 → 不判重，正常摄入
        state["dedup_result"] = {"duplicate": False, "reason": "title>0.95 but no metadata match"}
        return False, ""
    state["dedup_result"] = {"duplicate": False}
    return False, ""


# ===== 3.2 extract =====

def step_extract(state: dict) -> tuple[bool, str]:
    """封装 extractor.py，提取 PDF 为 paper.md；书目预审通过后写 manifest/persist。"""
    import shutil
    txn = state["transaction_id"]
    extract_dir = TEMP_EXTRACT / txn
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = (REPO / state["source"]).resolve()
    run([sys.executable, str(REPO / ".scripts/extractor.py"), "--external-pdf", str(pdf_path),
         "--paper", txn, "--papers-dir", "temp/inbox-extract"])
    paper_md = extract_dir / "paper.md"
    if not paper_md.is_file():
        return False, "提取未生成 paper.md"
    raw_files = [name for name in ("paper.pdf", "paper.md", "source.yaml", "parse_meta.yaml")
                 if (extract_dir / name).is_file()]
    if not {"paper.pdf", "paper.md"}.issubset(raw_files):
        return False, "提取未生成 paper.pdf 和 paper.md"
    (extract_dir / "manifest.json").write_text(
        json.dumps({"raw_files": raw_files, "wiki_file": "wiki.md"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    engine = "unknown"
    meta_path = extract_dir / "parse_meta.yaml"
    if meta_path.is_file():
        meta = meta_path.read_text(encoding="utf-8")
        m = re.search(r"preferred:\s*(\S+)", meta)
        if m:
            engine = m.group(1)
    state["extract_dir"] = str(extract_dir.relative_to(REPO))
    state["engine"] = engine
    md_text = paper_md.read_text(encoding="utf-8")
    review_result = review_bibliographic_metadata(
        state.get("bibliographic_meta"), md_text, state.get("transaction_id", ""))
    if review_result.get("status") == "agent_required":
        draft_rel = str(extract_dir.relative_to(REPO) / "bibliographic-review.json")
        state["agent_required"] = True
        state["pre_handoff_status"] = "extract"
        state["agent_prompt"] = (
            review_result.get("agent_prompt", "")
            + f"\n\n请将符合 {BIBLIOGRAPHIC_DECISION_PROTOCOL} schema 的书目裁决 JSON 写入 `{draft_rel}`，"
            + f"然后运行 `{_resume_cmd(state)}`。"
        )
        state["bibliographic_review"] = {
            "status": "agent_required",
            "review": review_result.get("review", {}),
            "decision": review_result.get("decision"),
            "candidates": review_result.get("candidates", {}),
            "catalog": review_result.get("catalog", {}),
            "input_hash": review_result.get("input_hash", ""),
            "worker": review_result.get("worker", {}),
            "draft_path": draft_rel,
        }
        return False, "需要 agent 接管书目预审"
    if not review_result.get("ok"):
        draft_rel = str(extract_dir.relative_to(REPO) / "bibliographic-review.json")
        if isinstance(review_result.get("review"), dict):
            (extract_dir / "bibliographic-review.json").write_text(
                json.dumps(review_result["review"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        state["bibliographic_review_required"] = True
        state["bibliographic_review"] = {
            "status": review_result.get("status", "bibliographic_review_required"),
            "error": review_result.get("error", ""),
            "review": review_result.get("review", {}),
            "compiled_review": review_result.get("compiled_review", {}),
            "candidates": review_result.get("candidates", {}),
            "catalog": review_result.get("catalog", {}),
            "input_hash": review_result.get("input_hash", ""),
            "worker": review_result.get("worker", {}),
            "draft_path": draft_rel,
        }
        state["retryable"] = False
        state["next_action"] = "repair_bibliographic_review_then_resume"
        return False, review_result.get("error", "书目预审未通过，禁止 Raw/Wiki/Graph 提交")
    state["bibliographic_meta"] = review_result.get("bibliographic")
    _record_bibliographic_quality_warnings(state, md_text)
    state["bibliographic_review"] = {
        "status": "ok",
        "review": review_result.get("review"),
        "decision": review_result.get("decision"),
        "candidates": review_result.get("candidates"),
        "catalog": review_result.get("catalog"),
        "input_hash": review_result.get("input_hash"),
        "worker": review_result.get("worker"),
    }
    duplicate = _post_extract_duplicate(state, paper_md)
    if duplicate:
        state["post_extract_duplicate"] = True
        state["dedup_title"] = state["bibliographic_meta"].get("title", "")
        state["dedup_result"] = {
            "duplicate": True,
            "match": duplicate["match"],
            "raw_path": duplicate["raw_path"],
            "text_sha256": duplicate["text_sha256"],
            "confirmed_by": "locked_bibliography",
        }
        return False, f"提取后确认重复: {duplicate['raw_path']}"
    pending_relation = state.get("raw_relationship") or {}
    if pending_relation.get("uncertain") and state.get("relation_candidates"):
        relationship_result = review_uncertain_relationship(state, paper_md)
        if not relationship_result.get("ok"):
            draft_path = extract_dir / "relationship-review.json"
            if isinstance(relationship_result.get("decision"), dict):
                draft_path.write_text(
                    json.dumps(relationship_result["decision"], ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            state["agent_required"] = True
            state["pre_handoff_status"] = "extract"
            state["agent_write_to"] = str(draft_path.relative_to(REPO))
            state["agent_prompt"] = (
                relationship_result.get("prompt", "")
                + f"\n\n请将符合 {RELATION_DECISION_PROTOCOL} 的裁决 JSON 写入 "
                + f"`{draft_path.relative_to(REPO)}`，然后运行 `{_resume_cmd(state)}`。"
            )
            state["relationship_review"] = {
                "status": "agent_required",
                "error": relationship_result.get("error", ""),
                "decision": relationship_result.get("decision"),
                "catalog": relationship_result.get("catalog") or {},
                "input_hash": relationship_result.get("input_hash", ""),
                "worker": relationship_result.get("worker") or {},
            }
            return False, "需要 agent 接管论文关系裁决"
        _apply_relationship_review(state, relationship_result["compiled"])
        state["relationship_review"] = {
            "status": "ok",
            "decision": relationship_result.get("decision"),
            "compiled": relationship_result.get("compiled"),
            "catalog": relationship_result.get("catalog") or {},
            "input_hash": relationship_result.get("input_hash", ""),
            "worker": relationship_result.get("worker") or {},
        }
    persist_bibliographic_metadata(extract_dir, state.get("bibliographic_meta"))
    if engine != "mineru":
        print(f"⚠️  WARNING: 提取引擎为 {engine}（非 MinerU），结果可能需要人工复核")
    return True, ""


# ===== 3.3 write_wiki+slots =====

def build_paper_evidence_packet(md_text: str) -> str:
    """从论文全文抽取近端证据，帮助弱模型先锚定核心主张与限定条件。"""
    paragraphs = [" ".join(block.split()) for block in re.split(r"\n\s*\n", md_text)
                  if block.strip()]
    selected = []
    seen = set()

    def add(block: str) -> None:
        if block and block not in seen:
            selected.append(block)
            seen.add(block)

    for block in paragraphs[:4]:
        add(block)
    for block in paragraphs:
        if re.match(r"^(Theorem \d+|Conclusion[.—])", block, re.IGNORECASE):
            add(block)
    for block in paragraphs[-4:]:
        if re.match(r"^Conclusion[.—]", block, re.IGNORECASE):
            add(block)
    return "\n\n".join(selected[:8])


def _clip_paper_section(content: str, cap: int) -> str:
    """超长 section 保留头部为主、尾部兜底，避免整段吞掉上下文预算。"""
    content = content.strip()
    if len(content) <= cap:
        return content
    head = content[: int(cap * 0.8)]
    tail = content[-int(cap * 0.2):]
    return f"{head}\n\n[...中段省略...]\n\n{tail}"


def build_paper_context(md_text: str, paper_md_path=None, *, force_reduced: bool = False) -> str:
    """普通调用可保留全文；API wiki 生成默认 force_reduced，切关键 section 控制 token。

    具体预算和提取策略由 ingest_common.CONTEXT_PROFILES["paper"] 统一注册。
    """
    return ic.build_source_context(
        "paper", md_text, source_path=paper_md_path, force_reduced=force_reduced)


def build_wiki_prompt(md_text: str, skeleton: str, errors: list[str] | None = None,
                      paper_md_path=None) -> str:
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    paper_context = build_paper_context(md_text, paper_md_path, force_reduced=True)
    source_match = re.search(r'^\s*-\s*["\']?([^"\'\n]+paper\.md)["\']?\s*$', skeleton, re.M)
    raw_source = source_match.group(1).strip() if source_match else "RAW"
    paper_context = wl.annotate_context_lines(paper_context, md_text, raw_source)
    return f"""你是知识库摄入组件。基于以下论文定向摘要 (paper.md)，撰写自然、简洁的 wiki 页面。

[论文定向摘要]
{paper_context}

[页面骨架（已填确定性字段：frontmatter/title/authors/section 标题）]
{skeleton}
{error_section}

[要求]
1. 基于论文定向摘要填写 Navigation（2-4 句简短 lead）、研究方向定位和 Content；Content 按本论文自身主题选择 2-6 个自然小标题，不套固定五段模板。
2. `## 研究方向定位` 只写一句话，明确“研究对象 + 核心问题 + 方法或场景”，句末必须引用一个精确 Raw locator；它是论文与 Hub Scope 匹配的唯一输入，不写 Hub 名或分类标签。
3. Content 用连贯主题组织；可以使用短段落或列表，不为填模板重复同一事实。
4. 对定理、等式、性能结论，必须保留原文的对象、条件和比较基准；不要把特定过程、维度、二分方式或“已知最优”等限定泛化为普遍结论。
5. 只有作者明确标出的局限、近似代价或未来工作才写入相应主题；研究对象、实验设置和适用场景本身不是局限。
6. 不得补充论文未提及的事实、不得编造作者或日期。证据不充分时删去该项，不以常识补全。缩写须按论文原文展开为全称，不得自行编造缩写或全称。
7. 骨架 frontmatter 中带 `# <-- LLM 填 -->` 注释的字段由你填实值，填完后删除该注释；程序已确定性填好的 date/venue 等字段必须原样保留；`related` 若本次无可填 `[]`。
8. 上下文每个可引用行前都有程序提供的 `<raw-path#Lx>`。每个事实段落或事实列表项末尾用 `[^rN]` 引用一个或多个这些 handle；不得自造行号，也不得引用 `#全篇`。
9. 页面末尾写 `## Sources`，每个定义严格为单独一行 `[^rN]: raw-path#Lx`。定义只能复制上下文中实际出现的 handle，正文不复制 `<...>` 标记。
10. 输出完整 wiki markdown（含 frontmatter），用 <<<WIKI>>> 分隔符包裹；不要另行输出元信息块，程序只采用已锁定书目。

[输出格式]
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）"""


def build_paper_semantic_contract() -> str:
    """Compile the backend-independent paper-to-Graph semantic policy."""
    return f"""[CONTRACT {PAPER_SEMANTIC_CONTRACT_VERSION}]
1. 关系类型决定 object 形态：
   - `研究基础`、`核心方法`、`对比方法`指向可复用的规范概念名；
   - `核心创新点`、`局限性`、`未来展望`指向论文明确陈述的完整、简洁 proposition，不把其中片段另建概念；
   - 核心概念之间的自由关系，subject/object 都必须是可独立指代的规范概念名。
2. 规范概念使用「中文英文(缩写)」格式，如 矩阵乘积态matrix product state(MPS)；无公认缩写不写括号，无对应中文或英文时只保留存在的一种。缩写及全称必须来自论文，不得编造。
3. 只抽取 Wiki 明确陈述且论文证据支持的核心关系，宁少勿多；不得从“关联/构造/表示”自行推导“基于”等方向关系。旧式“叙述节点 + 拆分边”不再生成。
4. `局限性`只记录作者明确说明的限制或近似代价；研究对象、模型维度、实验设置和适用场景不得标为局限性。
5. 期刊、作者、日期由程序从锁定书目生成，禁止在语义槽中重复填写或猜测；研究方向也不写入三元组，由程序使用 Wiki 定位句匹配 Hub Scope。
6. 为三元组中的每个 keyword 概念写一句文档局部说明，只概括当前 Wiki 有 Raw 脚注支持的定义、角色或适用语境；不为 proposition 写 gloss，不输出 locator，locator 由程序机械绑定。
7. 主体用“本论文”代表当前论文。论文到概念建议谓词：研究基础/核心方法/对比方法；论文到 proposition 建议谓词：核心创新点/局限性/未来展望；概念间建议谓词：基于/改进/结合/对比/推广/替代/扩展。
8. 优先使用以下已登记谓词：{semantic_predicate_guide()}。确有必要的新谓词只能是 1-12 个汉字或英文字母组成的简短关系词，不得是句子、短语或带标点描述。"""


def build_agent_wiki_slots_prompt(paper_md_path: Path, skeleton: str, errors: list[str] | None = None) -> str:
    """agent 模式专用 prompt：用文件路径替代论文全文，合并 wiki+语义槽为单次任务。

    省 token 原理：API 模式 prompt 嵌入论文全文（~15K token），agent 模式下会被
    stdout 截断导致 agent 重读文件。改用路径引用后 prompt 仅 ~600 token。
    合并两阶段为一次输出省一轮程序往返。
    """
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    source_match = re.search(r'^\s*-\s*["\']?([^"\'\n]+paper\.md)["\']?\s*$', skeleton, re.M)
    raw_source = source_match.group(1).strip() if source_match else "RAW"
    return f"""你是知识库摄入组件。请一次性完成论文 wiki 页面撰写 + 语义槽抽取。

[论文全文]
请用读取工具打开：{paper_md_path}
最终 Raw locator 基址：{raw_source}（读取工具显示的行号 Lx 即程序提供的 locator handle）

[页面骨架（已填确定性字段：frontmatter/title/authors/section 标题）]
{skeleton}

[格式参考（可选，如需查看已有论文页结构）]
{FORMAT_EXAMPLE}
{error_section}

[要求]
A. 撰写 wiki（填充骨架中的 Navigation + 研究方向定位 + Content）：
   1. Navigation：2-4 句导航概述（80-200 tokens），末尾不得接 ## Content 标题，必须分行。
   2. 研究方向定位：只写一句话，明确“研究对象 + 核心问题 + 方法或场景”，句末引用精确 Raw locator；不得填写 Hub 名或分类标签。
   3. Content 按本论文自身主题选择 2-6 个自然小标题；可以使用短段落或列表，不套固定五段模板，不重复同一事实。
   3. 不得补充论文未提及的事实、不得编造作者或日期。
   4. frontmatter 中带 `# <-- LLM 填 -->` 注释的字段由你填实值，填完后删除注释；程序已确定性填好的 date/venue 等字段必须原样保留；related 若无可填 []。
   5. 每个事实段落或事实列表项末尾用 `[^rN]` 引用读取工具实际显示的 Raw 行号；页面末尾写 `## Sources`，定义严格为 `[^rN]: {raw_source}#Lx`。不得自造行号或使用 `#全篇`。

B. 抽取语义槽（基于你刚写好的 wiki）：
{build_paper_semantic_contract()}

只输出以下短格式：

三元组:
<主体|谓词|客体，每行一条>

概念说明:
<概念名 | 一句文档局部说明，每个 keyword 概念一行；不要填写 locator>

[输出格式]
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）
<<<SLOTS>>>
（语义槽）"""


def record_legacy_paper_meta(state: dict, text: str) -> None:
    """Keep old transaction META as audit-only data; never mutate locked identity."""
    meta = parse_meta_block(text)
    if not meta:
        return
    bibliography = state.get("bibliographic_meta") or {}
    expected_year = str(bibliography.get("year") or "")
    if not expected_year:
        parts = str(state.get("paper_id") or "").split("-")
        expected_year = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
    state["legacy_meta_audit"] = {
        "ignored": True,
        "reason": "deterministic_bibliography_is_authoritative",
        "meta": meta,
        "mismatches": validate_meta(meta, {"doc_type": "paper", "year": expected_year}),
    }


def build_slots_prompt(
        wiki_content: str, errors: list[str] | None = None,
        previous_slots: str = "") -> str:
    """第二次对话 prompt：基于已写好的 wiki 抽取语义槽。不带 paper.md 全文。"""
    error_section = ""
    if errors:
        error_section = "\n\n[上次语义槽的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    previous_section = ""
    if previous_slots:
        previous_section = (
            "\n\n[上次语义槽输出]\n"
            "保留其中正确关系，只修正上述问题并补足缺失关系：\n"
            f"{previous_slots.strip()}"
        )
    return f"""基于你刚写好的 wiki 页面，为这篇论文抽取语义槽。

[已写好的 wiki 页面]
<<<WIKI>>>
{wiki_content}
{error_section}
{previous_section}

[语义契约]
{build_paper_semantic_contract()}

[要求]
用 <<<SLOTS>>> 分隔符包裹输出语义槽，只输出以下短格式：

三元组:
<主体|谓词|客体，每行一条>

[语义槽填写示例（仅示格式，内容勿照搬）]
三元组:
本论文 | 研究基础 | 矩阵乘积态matrix product state(MPS)
本论文 | 研究基础 | 纠缠熵
本论文 | 核心方法 | 横向光锥收缩transverse light cone contraction(TLCC)
本论文 | 核心创新点 | 利用精确光锥结构收缩最小网络
本论文 | 局限性 | 主要适用于一维局域格点模型
本论文 | 未来展望 | 推广到二维张量网络
本论文 | 对比方法 | 矩阵乘积态直接演化
横向光锥收缩transverse light cone contraction(TLCC) | 基于 | 矩阵乘积态matrix product state(MPS)
Miguel Frías-Pérez | 所属 | Max-Planck-Institut für Quantenoptik

概念说明:
矩阵乘积态matrix product state(MPS) | 本文用作沿时间方向压缩影响泛函及相关对象的张量网络表示。
横向光锥收缩transverse light cone contraction(TLCC) | 本文用于利用精确光锥结构缩减需要收缩的张量网络区域。

[输出格式：必须逐字保留开标记、section 标头和闭标记]
<<<SLOTS>>>
三元组:
<主体 | 谓词 | 客体，每行一条>
概念说明:
<概念名 | 一句文档局部说明，每个 keyword 概念一行；不要填写 locator>
<<</SLOTS>>>
不得输出 `<<<END>>>`，不得省略 `三元组:`。"""
# parse_delimited → ic.parse_delimited (shared)


def step_write_wiki(state: dict) -> tuple[bool, str]:
    """3.3 第一阶段：生成 paper-id + 骨架 + 调用 LLM 撰写 wiki page。

    只输出 wiki（<<<WIKI>>>），不产语义槽。对话历史存入 state 供第二阶段续接。
    """
    if state.pop("_skip_wiki_for_slots_resume", False):
        return (True, "") if state.get("wiki_content") else (False, "无 wiki_content，无法恢复 slots 阶段")
    extract_dir = REPO / state["extract_dir"]
    paper_md = extract_dir / "paper.md"
    md_text = paper_md.read_text(encoding="utf-8")
    # 生成 paper-id（仅首次）
    if "paper_id" not in state:
        bibliography = state.get("bibliographic_meta") or {}
        base_id = generate_paper_id(
            md_text,
            state.get("arxiv_id", ""),
            str(bibliography.get("year") or ""),
            title_hint=str(bibliography.get("title") or "") or None,
            authors_hint=bibliography.get("authors"),
        )
        paper_id = ensure_unique_paper_id(base_id)
        state["paper_id"] = paper_id
        state["raw_dir"] = f"academic/raw/references/{paper_id}"
        state["wiki_path"] = f"academic/wiki/papers/{paper_id}"
    source_ref = f"{state['raw_dir']}/paper.md"
    # 生成骨架（保存到 skeleton.md，wiki.md 由 LLM 输出覆盖）
    skeleton_path = extract_dir / "skeleton.md"
    if not skeleton_path.exists():
        run([sys.executable, str(REPO / ".scripts/wiki_skeleton.py"), "--page", state["wiki_path"],
             "--raw", str(paper_md.relative_to(REPO)), "--source", source_ref,
             "--output", str(skeleton_path.relative_to(REPO))])
    skeleton = apply_bibliographic_frontmatter(
        skeleton_path.read_text(encoding="utf-8"), state.get("bibliographic_meta"))
    skeleton_path.write_text(skeleton, encoding="utf-8")
    # agent handoff 的合并输出写入事务目录；resume 只消费该文件。
    errors = state.get("wiki_errors", []) if state.get("wiki_retry", 0) > 0 else None
    agent_output = extract_dir / "agent-wiki-slots.txt"
    resumed_agent_output = bool(state.get("_awaiting_agent_wiki_slots"))
    is_agent = ingest_mode() == "agent" or resumed_agent_output
    if resumed_agent_output:
        if not agent_output.is_file():
            state["agent_required"] = True
            return False, f"agent 输出尚未写入: {agent_output.relative_to(REPO)}"
        text = agent_output.read_text(encoding="utf-8")
    else:
        prompt = (build_agent_wiki_slots_prompt(paper_md, skeleton, errors) if is_agent
                  else build_wiki_prompt(md_text, skeleton, errors, paper_md))
        result = call_text(prompt, max_tokens=32768, retries=0,
                           recovery_limits=rp.LLM_DEFAULT_LIMITS,
                           operation="ingest_wiki_write",
                           reasoning_context={
                               "document_kind": "paper",
                               "input_chars": len(md_text),
                               "retry": state.get("wiki_retry", 0),
                               "validation_errors": errors or [],
                           },
                           transaction_id=state.get("transaction_id", ""),
                           system="你是受程序约束的知识库摄入组件，基于论文定向摘要撰写 wiki 页面。")
        if result.get("status") == "agent_required":
            state["_awaiting_agent_wiki_slots"] = True
            state["pre_handoff_status"] = "write_wiki"
            state["agent_required"] = True
            state["agent_prompt"] = result.get("prompt", "")
            state["agent_write_to"] = str(agent_output.relative_to(REPO))
            return False, "需要 agent 接管（INGEST_BACKEND=agent）"
        if not result.get("ok"):
            return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
        text = result.get("text", "")
    # Old agent/API artifacts may still contain META. It is no longer independent
    # evidence and therefore cannot override the locked bibliography or identity.
    record_legacy_paper_meta(state, text)
    wiki_content = parse_delimited(text, WIKI_DELIMITER)
    if not wiki_content:
        wiki_content = salvage_wiki_without_delimiter(text)
        if not wiki_content:
            if resumed_agent_output:
                state["agent_required"] = True
            return False, "LLM 输出缺少 <<<WIKI>>> 段"
        state["wiki_delimiter_salvaged"] = True
    wiki_content = apply_bibliographic_frontmatter(
        wiki_content, state.get("bibliographic_meta"))
    # sources 回填：年份/type 纠正后 raw_dir 已变，用最终路径覆盖（与 ingest_document 一致）
    correct_source = f"{state['raw_dir']}/paper.md"
    wiki_content = re.sub(
        r'(sources:\s*\n\s*-\s*)(?:path:\s*)?"?[^\n]+"?',
        f'\\1"{correct_source}"', wiki_content, count=1)
    (extract_dir / "wiki.md").write_text(wiki_content, encoding="utf-8")
    state["wiki_content"] = wiki_content
    # agent 模式：合并任务已同时产出语义槽，提前存入 state 供第二阶段跳过
    if is_agent:
        slots_content = parse_delimited(text, SLOTS_DELIMITER)
        if slots_content:
            state["slots_content"] = slots_content
    if resumed_agent_output:
        state.pop("_awaiting_agent_wiki_slots", None)
        state["agent_required"] = False
        state["agent_prompt"] = ""
        state.pop("agent_write_to", None)
    return True, ""


def step_write_slots(state: dict) -> tuple[bool, str]:
    """3.3 第二阶段：单轮调用，基于已写好的 wiki 抽取语义槽（不带 paper.md 全文）。

    wiki 已通过 3.4 校验；若 wiki 曾被修复，传修复后的 wiki_content。
    语义槽硬错误只重写本阶段，不回第一阶段。
    """
    # agent 合并模式已产出语义槽 → 跳过 LLM 调用
    if state.get("slots_content"):
        return True, ""
    wiki_content = state.get("wiki_content", "")
    if not wiki_content:
        return False, "无 wiki_content，需先完成第一阶段"
    # 构建单轮 prompt（build_slots_prompt 已把 wiki 放进 prompt，不带 paper.md）
    errors = state.get("slots_errors", []) if state.get("slots_retry", 0) > 0 else None
    agent_output = REPO / state["extract_dir"] / "agent-slots.txt"
    resumed_agent_output = bool(state.get("_awaiting_agent_slots"))
    if resumed_agent_output:
        if not agent_output.is_file():
            state["agent_required"] = True
            return False, f"agent 输出尚未写入: {agent_output.relative_to(REPO)}"
        text = agent_output.read_text(encoding="utf-8")
    else:
        previous_slots = str(
            (state.get("_sparse_slots_best") or {}).get("content") or ""
        )
        prompt = build_slots_prompt(wiki_content, errors, previous_slots)
        result = call_text(prompt, max_tokens=32768, retries=0,
                           recovery_limits=rp.LLM_DEFAULT_LIMITS,
                           operation="ingest_semantic_extract",
                           reasoning_context={
                               "document_kind": "paper",
                               "input_chars": len(wiki_content),
                               "retry": state.get("slots_retry", 0),
                               "validation_errors": errors or [],
                               "failure_kind": "semantic" if errors else "",
                           },
                           transaction_id=state.get("transaction_id", ""),
                           system="你是受程序约束的知识库摄入组件，基于 wiki 页面抽取语义槽。")
        if result.get("status") == "agent_required":
            state["_awaiting_agent_slots"] = True
            state["pre_handoff_status"] = "write_slots"
            state["agent_required"] = True
            state["agent_prompt"] = result.get("prompt", "")
            state["agent_write_to"] = str(agent_output.relative_to(REPO))
            return False, "需要 agent 接管（INGEST_BACKEND=agent）"
        if not result.get("ok"):
            return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
        text = result.get("text", "")
    slots_content = parse_delimited(text, SLOTS_DELIMITER)
    if not slots_content:
        slots_content = salvage_slots_without_delimiter(text)
        if not slots_content:
            if resumed_agent_output:
                state["agent_required"] = True
            return False, "LLM 输出缺少 <<<SLOTS>>> 段"
        state["slots_delimiter_salvaged"] = True
    state["slots_content"] = slots_content
    if resumed_agent_output:
        state.pop("_awaiting_agent_slots", None)
        state["agent_required"] = False
        state["agent_prompt"] = ""
        state.pop("agent_write_to", None)
    return True, ""


# ===== 3.4 validate_wiki =====

# parse_check_errors → ic.parse_check_errors (shared)

def step_validate_wiki(state: dict) -> list[str]:
    """运行 ingest_check.py（结构校验，无 --graph），返回 ERROR 列表。"""
    extract_dir = REPO / state["extract_dir"]
    wiki_path = extract_dir / "wiki.md"
    result = subprocess.run([sys.executable, str(REPO / ".scripts/ingest_check.py"),
                             str(wiki_path.relative_to(REPO))],
                            cwd=REPO, text=True, capture_output=True)
    errors = [] if result.returncode == 0 else parse_check_errors(result.stdout + result.stderr)
    final_raw = f"{state.get('raw_dir', '')}/paper.md"
    raw_overrides = {final_raw: extract_dir / "paper.md"} if final_raw.strip("/") else {}
    errors.extend(wl.validate_wiki_page(
        wiki_path, require_citations=True, raw_overrides=raw_overrides))
    direction_section = wl.get_wiki_section(wiki_path, "研究方向定位")
    if direction_section is None:
        errors.append("缺少 ## 研究方向定位")
    elif not direction_section.raw_citations:
        errors.append("研究方向定位没有精确 Raw locator 脚注")
    return errors


# ===== 3.5 fill_semantics =====

def _split_on_comma(obj: str) -> list[str]:
    """在括号外的逗号/分号处拆分客体，返回拆分后的列表。"""
    parts = []
    depth = 0
    current = []
    for ch in obj:
        if ch in '（(':
            depth += 1
            current.append(ch)
        elif ch in '）)':
            depth = max(0, depth - 1)
            current.append(ch)
        elif depth == 0 and ch in ',，;；':
            part = ''.join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(ch)
    last = ''.join(current).strip()
    if last:
        parts.append(last)
    return parts if len(parts) > 1 else [obj]


def normalize_slots(text: str) -> str:
    """归一化语义槽格式：已知 section 的同行格式拆为两行；逗号/分号拆分客体；去重完全相同的三角组行。"""
    lines = text.splitlines()
    result = []
    seen_triples = set()
    current_section = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in {"<<<SLOTS>>>", "<<</SLOTS>>>", "<<<END>>>"}:
            continue
        m = re.match(r"^(\S+?)\s*[:：]\s*(\S.*)$", stripped)
        if m and "|" not in stripped:
            header = m.group(1)
            content = m.group(2)
            if header in KNOWN_SECTIONS:
                result.append(f"{header}:")
                result.append(content)
                current_section = header
                continue
        if re.match(r"^.+[:：]\s*$", stripped) and "|" not in stripped:
            current_section = stripped.rstrip(":：").strip()
        if "|" in stripped:
            parts = [part.strip() for part in stripped.split("|")]
            if len(parts) == 3 and current_section != "概念说明":
                normalized_predicate = normalize_predicate(parts[1], DEFAULT_CONFIG)
                if normalized_predicate != parts[1]:
                    parts[1] = normalized_predicate
                    stripped = " | ".join(parts)
                # 逗号/分号拆分：客体含逗号/分号（括号外）→ 拆为多条三元组
                obj_parts = _split_on_comma(parts[2])
                if len(obj_parts) > 1:
                    for op in obj_parts:
                        split_line = f"{parts[0]} | {parts[1]} | {op}"
                        split_key = (parts[0], parts[1], op)
                        if split_key not in seen_triples:
                            seen_triples.add(split_key)
                            result.append(split_line)
                    continue
                # 机械去重：完全相同的三角组只留首条（防 LLM 生成/修复产生重复行）
                key = tuple(parts)
                if key in seen_triples:
                    continue
                seen_triples.add(key)
        result.append(stripped)
    has_section = any(re.match(r"^三元组[:：]\s*$", line) for line in result)
    bare_triples = [
        line for line in result
        if len(parts := [part.strip() for part in line.split("|", 2)]) == 3
        and all(parts)
    ]
    if not has_section and result and len(bare_triples) == len(result):
        result.insert(0, "三元组:")
    return "\n".join(result) + "\n"


# step_fill_semantics → ic.step_fill_semantics(state, REPO, normalize_slots)


# ===== 3.6 validate_semantics =====

def is_clearly_descriptive(obj: str) -> bool:
    """与 graph_ingest 一致的描述性对象判据，确保问题在写图前局部修复。"""
    import graph_ingest
    return graph_ingest.is_descriptive_phrase(obj)


def step_validate_semantics(state: dict) -> tuple[list[str], list[dict]]:
    """校验语义槽合法性。返回 (hard_errors, slot_warnings)。

    hard_errors: 结构性错误（谓词非法、解析失败），需回 3.3 全量重生成。
    slot_warnings: 客体内容问题（描述性短语、裸缩写），可走局部修复。
    """
    semantic_path = REPO / state["semantic_path"]
    sem_text = semantic_path.read_text(encoding="utf-8")
    # 三段式裸缩写消解第二步: alias 未命中时从 raw paper.md 查全称,自动补全为 full(ABBR) 格式
    _raw_abbr_map = ic.load_raw_abbr_map(state.get("wiki_path", ""))
    if _raw_abbr_map:
        _patched = ic.autofix_bare_abbreviations(sem_text, _raw_abbr_map)
        if _patched != sem_text:
            semantic_path.write_text(_patched, encoding="utf-8")
            sem_text = _patched
            state["slots_content"] = _patched
    hard_errors = []
    slot_warnings = []
    candidates = []
    # lazy resolve 上下文：仅首次命中裸缩写时才开图(~7ms)+构建索引(~7ms)
    resolve_ctx = None
    def _resolve_ctx():
        nonlocal resolve_ctx
        if resolve_ctx is None:
            import graph_lib as gl
            from graph_ingest import bare_tokens_resolvable as _btr
            conn = gl.connect()
            ti, ai, si = gl.build_name_index(conn)
            resolve_ctx = (_btr, conn, ti, ai, si)
        return resolve_ctx
    # 格式检查（同行 header）→ 硬错误，需回 3.3
    try:
        import graph_ingest
        warns = graph_ingest.detect_inline_section_headers(sem_text)
        hard_errors.extend(warns)
    except Exception:
        pass
    # 解析语义槽
    try:
        import graph_ingest
        from graph_ingest import is_descriptive_phrase, is_bare_abbreviation, KW_PREDICATES
        page_path = state["wiki_path"]
        sections, diagnostics = graph_ingest.parse_semantic_sections(sem_text)
        state["semantic_slot_diagnostics"] = diagnostics
        if (not diagnostics["triple_section_present"]
                and not diagnostics["bare_triples_recovered"]):
            hard_errors.append(
                "语义槽缺少 `三元组:` section；"
                f"检测到 {len(diagnostics['triple_like_outside_section'])} 条 section 外三元组形状行、"
                f"共 {diagnostics['meaningful_line_count']} 条有效文本行"
            )
        if diagnostics["malformed_triple_lines"]:
            hard_errors.append(
                "`三元组:` section 含无法解析的行: "
                + "；".join(diagnostics["malformed_triple_lines"][:3])
            )
        if diagnostics["malformed_gloss_lines"]:
            hard_errors.append(
                "`概念说明:` section 必须为 `概念名 | 一句局部说明`: "
                + "；".join(diagnostics["malformed_gloss_lines"][:3])
            )
        triples, keywords, main_dir, corresponding, cross_dirs, dir_preds = \
            graph_ingest.parse_semantic_text(sem_text, page_path)
        state["semantic_triple_count"] = diagnostics["semantic_triple_count"]
        state["concept_gloss_count"] = diagnostics["concept_gloss_count"]
        # 登记谓词直接通过；格式合格的新谓词进入候选池，异常文本仍是硬错误。
        allowed = set(SEMANTIC_PREDICATES)
        registry = REPO / ".scripts" / "predicate-registry.json"
        try:
            allowed.update(json.loads(registry.read_text(encoding="utf-8")).get("formal", []))
        except (OSError, json.JSONDecodeError):
            pass
        try:
            import yaml
            tiers = yaml.safe_load((REPO / ".scripts/predicate_tiers.yaml").read_text(encoding="utf-8"))
            for pred_name in (tiers.get("predicates") or {}):
                allowed.add(pred_name)
        except Exception:
            pass
        # 重复三元组检测（normalize_slots 已机械去重，此处为修复后回归安全网）
        _seen_triples = set()
        for _t in triples:
            _key = (_t.get("subject", ""), _t.get("predicate", ""), _t.get("object", "").strip())
            if _key in _seen_triples:
                slot_warnings.append({
                    "section": "三元组",
                    "line": f"{_key[0]} | {_key[1]} | {_key[2]}",
                    "issue": "duplicate_line",
                    "reason": "重复三元组，应删除重复行",
                    "is_triple": True,
                })
            else:
                _seen_triples.add(_key)
        for t in triples:
            pred = t.get("predicate", "")
            subj = t.get("subject", "")
            obj = t.get("object", "").strip()
            if pred and pred not in allowed:
                if is_valid_predicate_candidate(pred):
                    candidates.append({"predicate": pred, "subject": subj, "object": obj})
                else:
                    hard_errors.append(f"谓词格式不合法: {pred} (主体={subj}, 客体={obj})")
            # 程序从 frontmatter/source metadata 生成的书目与作者边不交 LLM 修复。
            if pred in {"发表于", "作者", "第一作者", "通讯作者"}:
                continue
            # 三元组若由普通字段（期刊/作者等）派生，warning 指向源字段行而非生成的三元组行，
            # 使 patch_semantic_lines 能定位并修正源字段（见 holmes-2022 期刊裸缩写案例）
            source_section = find_slot_section(pred, sem_text, obj or subj)
            is_field_sourced = bool(source_section) and source_section != "三元组"
            line = obj if is_field_sourced else f"{subj} | {pred} | {obj}"
            is_triple = not is_field_sourced
            # 主体/客体内容检查 → 可局部修 warning（field 标记哪列出问题）
            if subj and is_clearly_descriptive(subj):
                slot_warnings.append({
                    "section": source_section or find_slot_section(pred, sem_text, subj),
                    "line": line,
                    "issue": "descriptive_phrase",
                    "field": "subject",
                    "reason": "主体含逗号/句号等标点，应为规范概念名/实体名",
                    "is_triple": is_triple,
                })
            if obj and is_clearly_descriptive(obj):
                slot_warnings.append({
                    "section": source_section or find_slot_section(pred, sem_text, obj),
                    "line": line,
                    "issue": "descriptive_phrase",
                    "field": "object",
                    "reason": "客体含逗号/句号等标点，应为规范概念名/实体名（不含卷号页码、多从句描述）",
                    "is_triple": is_triple,
                })
            # 裸缩写校验（field 标记哪列）：keyword 谓词只查 object；自由边（非 KW）subject 与 object 都查
            # resolve 复查：缩写已注册 alias（图中已有 keyword）→ 不 warn；resolve miss → 保留 warning
            def _abbr_unresolved(text):
                if not is_bare_abbreviation(text):
                    return False
                _ctx = _resolve_ctx()
                return not _ctx[0](text, _ctx[1], _ctx[2], _ctx[3], _ctx[4])
            if pred in KW_PREDICATES:
                if _abbr_unresolved(obj):
                    slot_warnings.append({
                        "section": source_section or find_slot_section(pred, sem_text, obj),
                        "line": line,
                        "issue": "bare_abbreviation",
                        "field": "object",
                        "reason": "含英文缩写但未放入括号，应为「中文英文(缩写)」格式",
                        "is_triple": is_triple,
                    })
            else:
                if subj and _abbr_unresolved(subj):
                    slot_warnings.append({
                        "section": source_section or find_slot_section(pred, sem_text, subj),
                        "line": line,
                        "issue": "bare_abbreviation",
                        "field": "subject",
                        "reason": "主体含英文缩写但未放入括号，应为「中文英文(缩写)」格式",
                        "is_triple": is_triple,
                    })
                if _abbr_unresolved(obj):
                    slot_warnings.append({
                        "section": source_section or find_slot_section(pred, sem_text, obj),
                        "line": line,
                        "issue": "bare_abbreviation",
                        "field": "object",
                        "reason": "客体含英文缩写但未放入括号，应为「中文英文(缩写)」格式",
                        "is_triple": is_triple,
                    })
    except Exception as exc:
        hard_errors.append(f"语义槽解析失败: {exc}")
    state["predicate_candidates"] = candidates
    return hard_errors, slot_warnings


def _handle_sparse_slots(state: dict) -> str:
    """Retry one sparse slots extraction and retain the higher-coverage result."""
    count = int(state.get("semantic_triple_count") or 0)
    content = str(state.get("slots_content") or "")
    best = state.get("_sparse_slots_best") or {}
    if count > int(best.get("count") or -1):
        best = {"count": count, "content": content}
        state["_sparse_slots_best"] = best

    attempts = int(state.get("sparse_slots_retry") or 0)
    if (count < MIN_SEMANTIC_TRIPLES
            and attempts < MAX_SPARSE_SLOT_RETRIES
            and rp.consume(
                state, "semantic_revision", PAPER_RECOVERY_LIMITS,
                f"semantic coverage {count} < {MIN_SEMANTIC_TRIPLES}",
            )):
        state["sparse_slots_retry"] = attempts + 1
        state["slots_errors"] = [
            "语义槽内容覆盖不足：格式校验已通过，"
            f"成功解析 {count} 条 Worker 三元组，至少需要 {MIN_SEMANTIC_TRIPLES} 条。"
            "请保留上版正确关系，仅补充 Wiki 明确支持的核心关系"
        ]
        state["slots_content"] = ""
        state["_skip_wiki_for_slots_resume"] = True
        state["semantic_coverage"] = {
            "minimum": MIN_SEMANTIC_TRIPLES,
            "first_count": best["count"],
            "retry_count": attempts + 1,
            "status": "retrying",
        }
        return "retry"

    selected = best if int(best.get("count") or -1) > count else {
        "count": count, "content": content,
    }
    action = "accepted"
    if selected["content"] != content:
        state["slots_content"] = selected["content"]
        (REPO / state["semantic_path"]).write_text(selected["content"], encoding="utf-8")
        state["semantic_triple_count"] = selected["count"]
        action = "restored"
    state["semantic_coverage"] = {
        "minimum": MIN_SEMANTIC_TRIPLES,
        "selected_count": selected["count"],
        "retry_count": attempts,
        "status": "complete" if selected["count"] >= MIN_SEMANTIC_TRIPLES else "sparse",
    }
    state.pop("_sparse_slots_best", None)
    return action


def find_slot_section(predicate: str, sem_text: str, obj: str) -> str:
    """根据谓词反查客体所在的语义槽 section 名。"""
    pred_to_section = {
        "研究基础": "三元组", "核心方法": "三元组", "核心创新点": "三元组",
        "局限性": "三元组", "未来展望": "三元组", "研究关键词": "三元组",
        "对比方法": "三元组", "期刊": "期刊", "通讯作者": "通讯作者",
        "第一作者": "第一作者", "发表于": "期刊", "所属": "三元组",
        "基于": "三元组", "改进": "三元组", "结合": "三元组",
        "对比": "三元组", "推广": "三元组", "替代": "三元组", "扩展": "三元组",
    }
    section = pred_to_section.get(predicate, "")
    if section:
        return section
    # 兜底：在语义槽文本里找含 obj 的 section
    current = ""
    for line in sem_text.splitlines():
        stripped = line.strip()
        if re.match(r"^.+[:：]$", stripped) and "|" not in stripped:
            current = stripped.rstrip(":：").strip()
        if obj in line:
            return current
    return ""


# ===== 3.6b 局部修复语义槽（轻量 LLM）=====

def step_repair_slots(state: dict, warnings: list[dict]) -> tuple[bool, str]:
    """机械修复优先；剩余阻断项整批最多一次 semantic-patch-v1。"""
    progress("[3.6b] 局部修复（确定性优先，Worker 上限1次）...", flush=True, end=" ")
    ok, message = ic.repair_slots(
        state, REPO, warnings, step_validate_semantics,
        non_blocking_issues=NON_BLOCKING_ISSUES,
        patch_fn=patch_semantic_lines,
    )
    progress("通过" if ok else message, flush=True)
    return ok, message


def patch_semantic_lines(sem_text: str, repaired_text: str, warnings: list[dict]) -> str | None:
    """把 LLM 修复的行 patch 回语义槽文本。

    repaired_text 每行格式为「section名: 内容」或「主体|谓词|客体」。
    匹配策略：按 warnings 里的原 line 在 sem_text 中定位并替换。
    碰撞安全：同一 (主体,谓词) 多条用 deque 保序消费，避免后写覆盖前写导致丢失；
    按 (谓词,客体) 精确定位行（桥接「本论文」与全路径主体），避免一条修复套到多行造成重复。
    duplicate_line warning 触发机械去重（删除重复三角组行，保留首条）。
    """
    from collections import defaultdict, deque
    lines = sem_text.splitlines()
    changed = False
    # 机械去重：duplicate_line warning 兜底（normalize_slots 未覆盖的修复后引入）
    if any(w.get("issue") == "duplicate_line" for w in warnings):
        seen = set()
        deduped = []
        for ln in lines:
            s = ln.strip()
            if "|" in s:
                parts = tuple(p.strip() for p in s.split("|"))
                if len(parts) == 3:
                    if parts in seen:
                        continue
                    seen.add(parts)
            deduped.append(ln)
        if len(deduped) != len(lines):
            lines = deduped
            changed = True
    # 解析 repaired 行为有序列表（每项带 consumed 标记），碰撞时保序消费；
    # 末尾未消费的行 = LLM 拆解长客体时补的新三元组，追加到三元组段
    all_repairs = []  # [{line, parts, consumed}]
    section_repairs = defaultdict(deque)
    for line in repaired_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 3:
                all_repairs.append({"line": line, "parts": parts, "consumed": False})
        elif ":" in line or "：" in line:
            m = re.split(r"[:：]", line, 1)
            if len(m) == 2:
                section_repairs[m[0].strip()].append(m[1].strip())

    def _consume_repair(subj_keys, pred):
        """按 subj_keys 优先级找第一个未消费且谓词匹配的修复行。"""
        for r in all_repairs:
            if r["consumed"] or r["parts"][1] != pred:
                continue
            for sk_subj, sk_pred in subj_keys:
                if sk_pred != pred:
                    continue
                if sk_subj is None or r["parts"][0] == sk_subj:
                    r["consumed"] = True
                    return r["line"]
        return None

    warning_lines = {w["line"] for w in warnings}
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 三角组行：按 (谓词, 原客体) 定位 warning，消费下一条匹配的修复行
        if "|" in stripped:
            current = [p.strip() for p in stripped.split("|")]
            if len(current) != 3:
                continue
            cur_subj, cur_pred, cur_obj = current
            matched = None
            for w in warnings:
                if w.get("is_triple"):
                    original = [p.strip() for p in w["line"].split("|")]
                    if len(original) != 3:
                        continue
                    # warning 的 object 是修复前原值，与 sem 行一致；不按 subject 匹配
                    # （parse 后 warning subject 是全路径，sem 行是「本论文」）
                    if original[1] == cur_pred and original[2] == cur_obj:
                        matched = w
                        break
                elif w.get("line") and w["line"] in stripped:
                    matched = w
                    break
            if matched is None:
                continue
            # 消费修复：先试 warning 主体，再试当前主体，最后 None 兜底（LLM 改了 subject）
            if matched.get("is_triple"):
                original = [p.strip() for p in matched["line"].split("|")]
                subj_keys = ((original[0], original[1]), (cur_subj, cur_pred), (None, cur_pred))
            else:
                subj_keys = ((cur_subj, cur_pred), (None, cur_pred))
            repaired_line = _consume_repair(subj_keys, cur_pred)
            if repaired_line is not None:
                # 按 field 只替换对应列，其余列保留原行
                repaired_parts = [p.strip() for p in repaired_line.split("|")]
                field = matched.get("field", "object")
                if len(repaired_parts) == 3:
                    if field == "subject":
                        lines[i] = f"{repaired_parts[0]} | {cur_pred} | {cur_obj}"
                    else:
                        lines[i] = f"{cur_subj} | {cur_pred} | {repaired_parts[2]}"
                else:
                    lines[i] = repaired_line
                changed = True
        elif stripped in warning_lines:
            current_section = ""
            for j in range(i - 1, -1, -1):
                s = lines[j].strip()
                if re.match(r"^.+[:：]$", s) and "|" not in s:
                    current_section = s.rstrip(":：").strip()
                    break
            q = section_repairs.get(current_section)
            if q:
                lines[i] = q.popleft()
                changed = True
    # 拆解产生的新三元组：未消费的修复行追加到三元组段末尾
    new_triples = [r["line"] for r in all_repairs if not r["consumed"]]
    if new_triples:
        insert_at = len(lines)
        for idx in range(len(lines) - 1, -1, -1):
            if lines[idx].strip() and "|" in lines[idx].strip():
                insert_at = idx + 1
                break
        for nt in new_triples:
            lines.insert(insert_at, nt)
            insert_at += 1
        changed = True
    if not changed:
        return None
    return "\n".join(lines) + "\n"


def step_extract_propositions(state: dict) -> tuple[bool, str]:
    """登记完整命题，概念链接留给 graph_ingest 的确定性稀疏编译。

    兼容保留原函数名，避免中断已有恢复事务。这里不调用 LLM、不改 semantic，
    也不从命题片段创建概念节点；无唯一精确概念匹配时，完整命题仍正常入图。
    """
    import graph_ingest as _gi
    semantic_path = REPO / state["semantic_path"]
    sem_text = semantic_path.read_text(encoding="utf-8")
    page_path = state["wiki_path"]

    # 解析已有三元组，收集命题谓词的 object（经逗号拆分）
    triples, _kw, *_ = _gi.parse_semantic_text(sem_text, page_path)
    propositions = []
    prop_seen = set()
    for t in triples:
        if t.get("predicate") in _gi.PROPOSITION_PREDICATES:
            obj = t.get("object", "").strip()
            for part in _split_on_comma(obj):
                if part and part not in prop_seen:
                    prop_seen.add(part)
                    propositions.append(part)
    if not propositions:
        state["proposition_status"] = "no_propositions"
        state["proposition_details"] = {
            "proposition_count": 0,
            "execution_mode": "deterministic",
        }
        return True, ""
    progress(f"[3.6c] 稀疏命题编译（{len(propositions)}条，零 LLM）", flush=True)
    state["proposition_status"] = f"sparse: {len(propositions)} propositions"
    state["proposition_details"] = {
        "proposition_count": len(propositions),
        "execution_mode": "deterministic",
        "concept_links": "deterministic_graph_ingest",
    }
    return True, ""


# ===== 落位（委托 ingest_common）=====

FINALIZE_CONFIG = {
    "doc_id_key": "paper_id",
    "manifest_files": None,
    "copy_source": False,
}

FINALIZE_TAIL_CONFIG = {
    "doc_id_key": "paper_id",
    "get_log_path": lambda state, REPO: REPO / "academic" / "wiki" / "log.md",
    "get_index_path": lambda state, REPO: REPO / "academic" / "wiki" / "index.md",
    "index_section": "## 论文",
    "entry_prefix": "papers/",
    "frontier_capture": True,
    "frontier_capture_limit": 3,
    "frontier_answer": True,
    "build_log_entry": lambda ctx: (
        "\n## [" + ctx["today"] + "] ingest | ingest_paper.py 摄入 " + ctx["doc_id"] + "\n"
        + (
            "- **来源与归档**：raw 已在位（网上下载），代码驱动流水线摄入。\n"
            if ctx["state"].get("from_raw")
            else "- **来源与归档**：inbox PDF 经 MinerU 提取后实体复制至 `academic/raw/references/"
            + ctx["doc_id"] + "/`。\n"
        )
        + "- **来源页**：新建 `papers/" + ctx["doc_id"] + ".md`（paper-summary），"
        + ctx["title"] + "。\n"
        + "- **图谱巩固**：增量写入 " + str(ctx["edges"]) + " 条边"
        + ("，主方向「" + (ctx["report"].get("derived_directions") or {}).get("main", "") + "」"
           if (ctx["report"].get("derived_directions") or {}).get("main") else "") + "。\n"
        + "- **验证**：`ingest_check --graph` PASS（ERROR=0）。\n"
    ),
    "build_entry": lambda ctx: (
        "- [[papers/" + ctx["page_name"] + "]] — "
        + ctx["title"][:60] + ("…" if len(ctx["title"]) > 60 else "")
        + ("（" + ctx["fm"].get("venue", "") + "）" if ctx["fm"].get("venue") else "")
        + "\n"
    ),
}


def step_finalize(state: dict) -> tuple[bool, str]:
    return ic.step_finalize(state, REPO, FINALIZE_CONFIG)


def _record_graph_quality_warnings(state: dict) -> None:
    graph_delta = ((state.get("graph_report") or {}).get("graph_delta") or {})
    probes = graph_delta.get("query_probes") or {}
    subgraph = graph_delta.get("subgraph") or {}
    generated_issues = {
        "graph_navigation_incomplete",
        "graph_navigation_ambiguous",
        "graph_semantic_coverage_sparse",
    }
    retained = [
        warning for warning in state.get("quality_warnings", [])
        if str(warning.get("issue") or "") not in generated_issues
    ]
    total = int(probes.get("boundary_total") or 0)
    reachable = int(probes.get("boundary_reachable_within_2") or 0)
    ambiguous = int(probes.get("ambiguous_mentions") or 0)
    semantic_edges = int(
        (state.get("semantic_coverage") or {}).get("selected_count")
        or subgraph.get("semantic_edges")
        or 0
    )
    if semantic_edges < MIN_SEMANTIC_TRIPLES:
        retained.append({
            "issue": "graph_semantic_coverage_sparse",
            "detail": (
                f"only {semantic_edges} semantic edges generated "
                f"(minimum {MIN_SEMANTIC_TRIPLES})"
            ),
        })
    if total and reachable < total:
        retained.append({
            "issue": "graph_navigation_incomplete",
            "detail": f"{reachable}/{total} boundary nodes reachable within 2 hops",
        })
    if ambiguous:
        retained.append({
            "issue": "graph_navigation_ambiguous",
            "detail": f"{ambiguous} boundary mentions remain ambiguous",
        })
    state["quality_warnings"] = retained


def step_update_graph(state: dict) -> tuple[bool, str]:
    """Route semantic and Raw relationships through the shared graph writer."""
    ok, msg = ic.step_update_graph(state, REPO, clean=state.get("reingest", False))
    if not ok:
        return False, msg
    _record_graph_quality_warnings(state)
    return True, ""


def resume_after_semantic_fix(state: dict) -> bool:
    """Load a hand-fixed semantic file and resume from commit validation."""
    if state.get("status") != "agent_required":
        return False
    if (state.get("_awaiting_agent_wiki_slots") or state.get("_awaiting_agent_slots")
            or (state.get("bibliographic_review") or {}).get("status") == "agent_required"
            or (state.get("relationship_review") or {}).get("status") == "agent_required"):
        return False
    semantic_path = REPO / state.get("semantic_path", "")
    if not semantic_path.is_file():
        return False
    state["slots_content"] = semantic_path.read_text(encoding="utf-8")
    state["agent_required"] = False
    state["agent_prompt"] = ""
    state["errors"] = []
    # 恢复到 handoff 前阶段：落位后(graph_ready)handoff 不重跑落位
    inbox_state.transition(
        state, state.get("pre_handoff_status", "finalize"),
        reason="resume_after_semantic_fix",
    )
    return True


def resume_after_agent_generation(state: dict) -> bool:
    """Resume a wiki/slots handoff only after its declared output exists."""
    if state.get("status") != "agent_required":
        return False
    if state.get("_awaiting_agent_wiki_slots"):
        expected = REPO / state.get("agent_write_to", "")
        resume_status = "write_wiki"
    elif state.get("_awaiting_agent_slots"):
        expected = REPO / state.get("agent_write_to", "")
        resume_status = "write_slots"
    else:
        return False
    if not expected.is_file():
        state["errors"] = [f"agent 输出尚未写入: {state.get('agent_write_to', '')}"]
        return False
    inbox_state.transition(
        state, state.get("pre_handoff_status") or resume_status,
        reason="consume_agent_generation",
    )
    if resume_status == "write_slots":
        # run_prepare 的循环入口仍是 write_wiki；用一次性标记跳过生成并直接进入 slots。
        inbox_state.transition(
            state, "write_wiki", reason="route_slots_resume_through_loop_entry",
            allowed_from={"write_slots"},
        )
        state["_skip_wiki_for_slots_resume"] = True
    state["agent_required"] = False
    state["errors"] = []
    return True


def step_validate_graph(state: dict) -> list[str]:
    return ic.step_validate_graph(state, REPO)


def step_finalize_tail(state: dict) -> tuple[bool, str]:
    success, message = ic.step_finalize_tail(state, REPO, FINALIZE_TAIL_CONFIG)
    if not success:
        return success, message
    raw_dir = REPO / str(state.get("raw_dir") or "")
    paper_pdf = raw_dir / "paper.pdf"
    paper_md = raw_dir / "paper.md"
    if paper_pdf.is_file():
        try:
            state["source_fingerprint"] = sf.register_source(
                paper_pdf,
                text_path=paper_md if paper_md.is_file() else None,
                source_kind="pdf",
            )
        except Exception as exc:
            state.setdefault("quality_warnings", []).append({
                "issue": "fingerprint_register_failed", "detail": str(exc),
            })
    return True, message

# ===== 主编排循环 =====

def run_prepare(state: dict) -> dict:
    """Phase 1：3.1→3.6b→落位→graph_ready。不写图。

    多篇批量摄入时，所有论文先各自跑到 graph_ready 屏障；全部就绪后才由 run_commit 批量写图，
    避免部分论文已入图、部分卡在 warning 的半提交中间态。单篇（--pdf/--resume）由 run_one
    串联 prepare+commit，行为与原 run_pipeline 等价。
    """
    rp.ensure_state(state, PAPER_RECOVERY_LIMITS)
    # agent wiki/slots 生成交接只恢复到精确阶段，输出由对应 step 消费。
    if state["status"] == "agent_required" and (
            state.get("_awaiting_agent_wiki_slots") or state.get("_awaiting_agent_slots")):
        if not resume_after_agent_generation(state):
            inbox_state.save(state["transaction_id"], state)
            return state
        inbox_state.save(state["transaction_id"], state)
    # 恢复或手工修复后，任何会写入最终目录/图谱的阶段都必须重新通过语义校验。
    if state["status"] in {"finalize", "update_graph", "validate_graph", "finalize_tail", "graph_ready"}:
        validation_errors = ic.validate_before_commit(state, step_validate_semantics, NON_BLOCKING_ISSUES)
        if validation_errors:
            ic.handoff_to_agent(state, "恢复前语义槽校验未通过", step_validate_semantics, _resume_cmd(state), _validate_cmd(state))
            inbox_state.save(state["transaction_id"], state)
            return state
    # agent 模式 3.2 书目预审交接收回
    review_status = (state.get("bibliographic_review") or {}).get("status")
    if ((state["status"] == "agent_required" and review_status == "agent_required")
            or (state["status"] == "bibliographic_review_required"
                and review_status == "validation_error")):
        if not _resume_bibliographic_review(state):
            inbox_state.save(state["transaction_id"], state)
            return state
        inbox_state.save(state["transaction_id"], state)
    # 提取后近似标题关系裁决交接收回；不重跑 MinerU 或书目 Worker。
    relationship_status = (state.get("relationship_review") or {}).get("status")
    if state["status"] == "agent_required" and relationship_status == "agent_required":
        if not _resume_relationship_review(state):
            inbox_state.save(state["transaction_id"], state)
            return state
        inbox_state.save(state["transaction_id"], state)
    # 3.1 dedup_check
    if state["status"] in ("init", "dedup_check"):
        progress(f"\n{'='*60}", flush=True)
        progress("[3.1] 去重检查：提取PDF标题，查图 + 查raw目录...", flush=True)
        is_dup, msg = step_dedup_check(state)
        if is_dup:
            progress(f"  ↳ 已摄入：{state.get('dedup_title', '')}", flush=True)
            state["status"] = "duplicate_found"
            inbox_state.save(state["transaction_id"], state)
            return state
        progress("  ↳ 未摄入，继续", flush=True)
        state["status"] = "extract"
        inbox_state.save(state["transaction_id"], state)

    # 3.2 extract
    if state["status"] == "extract":
        progress("\n[3.2] 提取：MinerU 解析 PDF 为 paper.md（约30-60秒）...", flush=True)
        success, msg = step_extract(state)
        if state.get("agent_required"):
            state["status"] = "agent_required"
            inbox_state.save(state["transaction_id"], state)
            return state
        if not success:
            if state.get("post_extract_duplicate"):
                state["status"] = "duplicate_found"
                state["errors"] = []
                inbox_state.save(state["transaction_id"], state)
                return state
            state["status"] = "bibliographic_review_required" if state.get("bibliographic_review_required") else "failed"
            state["errors"] = [msg]
            inbox_state.save(state["transaction_id"], state)
            return state
        progress(f"  ↳ 引擎: {state.get('engine')}", flush=True)
        state["status"] = "write_wiki"
        inbox_state.save(state["transaction_id"], state)

    # 3.3-3.6 两阶段循环：wiki(第一阶段)与语义槽(第二阶段)分别重试
    if state["status"] == "write_wiki":
        state.setdefault("wiki_retry", 0)
        state.setdefault("slots_retry", 0)
    while state["status"] == "write_wiki":
        # --- 第一阶段：写 wiki ---
        if state.get("wiki_retry", 0) == 0 and not state.get("wiki_content"):
            progress("\n[3.3a] 撰写 wiki（调用LLM，约1-2分钟）...", flush=True)
            progress(f"  paper-id: {state.get('paper_id', '?')}", flush=True)
        elif state.get("wiki_retry", 0) > 0:
            progress(f"\n[3.3a] 撰写 wiki（修订第{state['wiki_retry']}/{PAPER_RECOVERY_LIMITS['wiki_revision']}次）...", flush=True)
        success, msg = step_write_wiki(state)
        if state.get("agent_required"):
            state["status"] = "agent_required"
            inbox_state.save(state["transaction_id"], state)
            return state
        if state.get("type_mismatch"):
            state["status"] = "type_mismatch"
            state["errors"] = [msg]
            inbox_state.save(state["transaction_id"], state)
            return state
        if not success:
            state["errors"] = [msg]
            progress(f"  ↳ wiki LLM调用失败: {msg}", flush=True)
            if ("缺少 <<<" in msg and rp.consume(
                    state, "wiki_revision", PAPER_RECOVERY_LIMITS, msg)):
                state["wiki_content"] = ""
                inbox_state.save(state["transaction_id"], state)
                continue
            state["status"] = "failed"
            inbox_state.save(state["transaction_id"], state)
            return state
        state["errors"] = []
        progress("[3.4] wiki结构校验...", flush=True, end=" ")
        wiki_errors = step_validate_wiki(state)
        progress("通过" if not wiki_errors else f"{len(wiki_errors)}个错误", flush=True)
        if wiki_errors:
            state["wiki_errors"] = wiki_errors
            if not rp.consume(
                    state, "wiki_revision", PAPER_RECOVERY_LIMITS,
                    "; ".join(wiki_errors)):
                wiki_path = REPO / state["extract_dir"] / "wiki.md"
                state["status"] = "agent_required"
                state["pre_handoff_status"] = "write_wiki"
                state["agent_required"] = True
                state["_awaiting_agent_wiki"] = True
                state["agent_write_to"] = str(wiki_path.relative_to(REPO))
                state["agent_prompt"] = (
                    "API wiki 已达格式重写上限。只修复 write_to 现有草稿：\n- "
                    + "\n- ".join(wiki_errors)
                )
                state["errors"] = wiki_errors
                inbox_state.save(state["transaction_id"], state)
                return state
            state["wiki_content"] = ""  # 清空，强制重写
            state["slots_content"] = ""  # agent 合并模式下同步清空
            inbox_state.save(state["transaction_id"], state)
            continue
        # wiki 通过 → 第二阶段
        # --- 第二阶段：写语义槽（续接对话，不带 paper.md） ---
        if state.get("slots_retry", 0) == 0:
            progress("[3.3b] 抽取语义槽（续接对话，调用LLM）...", flush=True, end=" ")
        else:
            progress(f"[3.3b] 抽取语义槽（修订第{state['slots_retry']}/{PAPER_RECOVERY_LIMITS['semantic_revision']}次）...", flush=True, end=" ")
        success, msg = step_write_slots(state)
        if state.get("agent_required"):
            state["status"] = "agent_required"
            inbox_state.save(state["transaction_id"], state)
            return state
        if not success:
            state["errors"] = [msg]
            progress(f"失败: {msg}", flush=True)
            if ("缺少 <<<" in msg and rp.consume(
                    state, "semantic_revision", PAPER_RECOVERY_LIMITS, msg)):
                state["slots_content"] = ""
                state["slots_errors"] = [msg]
                state["_skip_wiki_for_slots_resume"] = True
                inbox_state.save(state["transaction_id"], state)
                continue
            state["status"] = "failed"
            inbox_state.save(state["transaction_id"], state)
            return state
        state["errors"] = []
        progress("完成", flush=True)
        progress("[3.5] 语义槽格式化...", flush=True, end=" ")
        try:
            ic.step_fill_semantics(state, REPO, normalize_slots)
            progress("完成", flush=True)
            progress("[3.6] 语义槽校验...", flush=True, end=" ")
            sem_hard, slot_warnings = step_validate_semantics(state)
            coverage_action = _handle_sparse_slots(state) if not sem_hard else "accepted"
            if coverage_action == "restored":
                sem_hard, slot_warnings = step_validate_semantics(state)
            blocking = [w for w in slot_warnings if ic.is_blocking_warning(w, NON_BLOCKING_ISSUES)]
            n_nonblock = len(slot_warnings) - len(blocking)
            if not sem_hard and not slot_warnings:
                progress("通过", flush=True)
            elif not sem_hard:
                tag = f"{len(slot_warnings)}warning"
                if n_nonblock:
                    tag += f"(阻断{len(blocking)}/非阻断{n_nonblock})"
                progress(f"{len(sem_hard)}硬错误,{tag}", flush=True)
            else:
                progress(f"{len(sem_hard)}硬错误,{len(slot_warnings)}warning", flush=True)
        except Exception as exc:
            sem_hard = [f"语义槽处理失败: {exc}"]
            slot_warnings = []
            blocking = []
            coverage_action = "accepted"
            progress(f"异常: {exc}", flush=True)
        inbox_state.save(state["transaction_id"], state)
        if coverage_action == "retry":
            progress(
                f"  ↳ 语义槽仅 {state['semantic_coverage']['first_count']} 条，保留当前版本并定向重抽取一次",
                flush=True,
            )
            continue
        # 无硬错误且有阻断 warning → 机械修复优先，剩余项至多一次 Worker。
        if not sem_hard and blocking:
            if not rp.consume(
                    state, "deterministic_repair", PAPER_RECOVERY_LIMITS,
                    f"blocking_warnings={len(blocking)}"):
                state["status"] = "agent_required"
                state["agent_required"] = True
                state["errors"] = ["deterministic repair budget exhausted"]
                inbox_state.save(state["transaction_id"], state)
                return state
            repaired, repair_msg = step_repair_slots(state, blocking)
            if state.get("agent_required"):
                state["status"] = "agent_required"
                inbox_state.save(state["transaction_id"], state)
                return state
            if repaired:
                blocking = []
            else:
                state["status"] = "agent_required"
                inbox_state.save(state["transaction_id"], state)
                return state
        inbox_state.save(state["transaction_id"], state)
        if not sem_hard and not blocking:
            state["status"] = "finalize"
            break
        # 结构错误不会因同一提示词重试而自行消失；早停并交接受控修复。
        if sem_hard:
            recovered, recovery_msg = ic.try_semantic_recovery(
                state, REPO, sem_hard, slot_warnings,
                step_validate_semantics, NON_BLOCKING_ISSUES,
            )
            if recovered:
                progress("  ↳ bounded semantic recovery 通过复验", flush=True)
                state["status"] = "finalize"
                inbox_state.save(state["transaction_id"], state)
                break
            progress(f"  ↳ 语义槽结构错误 {len(sem_hard)} 个，停止重复生成并交接修复", flush=True)
            ic.stop_for_semantic_errors(state, sem_hard, _resume_cmd(state))
            state["semantic_recovery_message"] = recovery_msg
            inbox_state.save(state["transaction_id"], state)
            return state
    # 落位
    if state["status"] in ("finalize", "propositions"):
        # 3.6c 子图构建：保留完整命题；概念链接由 graph_ingest 唯一精确匹配。
        step_extract_propositions(state)
        inbox_state.save(state["transaction_id"], state)
        if state.get("reingest"):
            # re-ingest：raw 不可变（红线），跳过原子复制，直接标记就绪
            record_predicate_candidates(state)
            state["status"] = "propositions_done"
            inbox_state.save(state["transaction_id"], state)
            progress("\n[屏障] 已就绪待写图（propositions_done）", flush=True)
        elif state.get("from_raw"):
            # from-raw（网上下载等）：raw 已在位，仅复制 wiki 到最终路径
            import shutil
            wiki_src = REPO / state["extract_dir"] / "wiki.md"
            wiki_dst = REPO / (state["wiki_path"] + ".md")
            shutil.copy2(wiki_src, wiki_dst)
            record_predicate_candidates(state)
            state["status"] = "graph_ready"
            state["errors"] = []
            inbox_state.save(state["transaction_id"], state)
            progress("\n[屏障] 已就绪待写图（graph_ready）", flush=True)
        else:
            progress("\n[落位] 原子复制 raw/wiki 到最终目录...", flush=True, end=" ")
            success, msg = step_finalize(state)
            if not success:
                state["status"] = "failed"
                state["errors"] = [msg]
                inbox_state.save(state["transaction_id"], state)
                return state
            progress("完成", flush=True)
            record_predicate_candidates(state)
            state["status"] = "graph_ready"
            state["errors"] = []
            inbox_state.save(state["transaction_id"], state)
            progress("\n[屏障] 已就绪待写图（graph_ready）", flush=True)

    return state


PAPER_COMMIT_SPEC = {
    "script_name": "ingest_paper.py",
    "preprocess_label": "MinerU PDF 提取",
    "completion_label_key": "paper_id",
    "cleanup_after": "validate_graph",
    "skip_source_cleanup_if": "from_raw",
    "rollback_fn": None,
    "retry_graph_with_clean": True,
    "finalize_tail_failure": "warn",
    "recovery_limits": PAPER_RECOVERY_LIMITS,
    "non_blocking_issues": NON_BLOCKING_ISSUES,
    "normalize_slots": normalize_slots,
    "steps": {
        "dedup_check": step_dedup_check,
        "preprocess": step_extract,
        "write_wiki": step_write_wiki,
        "validate_wiki": step_validate_wiki,
        "write_slots": step_write_slots,
        "validate_semantics": step_validate_semantics,
        "repair_slots": step_repair_slots,
        "finalize": step_finalize,
        "update_graph": step_update_graph,
        "validate_graph": step_validate_graph,
        "finalize_tail": step_finalize_tail,
    },
}


def run_commit(state: dict) -> dict:
    """Phase 2：graph_ready→3.7 写图→3.8 图校验→3.9 收尾。

    与 run_prepare 解耦：批量摄入先全部准备到 graph_ready，再统一写图，
    避免部分入图、部分卡 warning 的半提交中间态。
    """
    return ingest_pipeline.run_pipeline(state, PAPER_COMMIT_SPEC, progress)


def _batch_item_payload(state: dict) -> dict:
    """Build one batch result using the same failure contract as single-item output."""
    payload = {
        "source": state["source"], "status": state["status"],
        "paper_id": state.get("paper_id"), "transaction_id": state["transaction_id"],
        "raw_dir": state.get("raw_dir"), "wiki_path": state.get("wiki_path"),
        "engine": state.get("engine"), "graph_report": state.get("graph_report"),
        "errors": state.get(
            "errors", [state["dedup_reason"]] if state.get("dedup_reason") else []),
        "proposition_status": state.get("proposition_status"),
        "proposition_details": state.get("proposition_details"),
        "bibliographic_worker": (state.get("bibliographic_review") or {}).get("worker"),
        "relationship_worker": (state.get("relationship_review") or {}).get("worker"),
        "semantic_repair_worker": state.get("semantic_repair_worker"),
        "quality_status": "degraded" if state.get("quality_warnings") else "complete",
        "quality_warnings": state.get("quality_warnings", []),
    }
    return inbox_state.output_payload(state, payload)


def run_inbox_batch(verbose: bool) -> int:
    """两阶段批量摄入：Phase 1 全部准备到 graph_ready 屏障；全部就绪后 Phase 2 批量写图。

    任一论文未就绪则屏障保持（不写任何图），输出 partial 交 agent 介入。返回退出码。
    """
    pdf_paths = inbox_pdf_paths()
    if not pdf_paths:
        print(json.dumps({"status": "completed", "items": []}, ensure_ascii=False, indent=2))
        return 0
    # Phase 1：全部准备到 graph_ready 屏障（不写图）
    prepared: list[dict | None] = [None] * len(pdf_paths)
    pending: list[tuple[int, dict]] = []
    for index, pdf_path in enumerate(pdf_paths):
        source = str(pdf_path.relative_to(REPO))
        existing = find_ready_txn(source)
        if existing:
            prepared[index] = existing
            continue
        state = new_state_for_pdf(pdf_path)
        pending.append((index, state))
    # quiet 批量 prepare 可安全并发；verbose 保持串行，避免终端输出交错。
    max_workers = 1 if verbose else min(2, len(pending))
    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(_run_phase, state, verbose, run_prepare): index
                for index, state in pending
            }
            for future in as_completed(futures):
                prepared[futures[future]] = future.result()
    else:
        for index, state in pending:
            prepared[index] = _run_phase(state, verbose, run_prepare)
    prepared = [state for state in prepared if state is not None]
    # 批内交叉去重：未提交的论文互相不可见，写图前按 normalized title 查重
    seen_titles: dict[str, str] = {}
    for s in prepared:
        norm = s.get("dedup_normalized", "")
        if not norm or s["status"] != "graph_ready":
            continue
        if norm in seen_titles:
            s["status"] = "duplicate_found"
            s["dedup_reason"] = f"批内重复: {seen_titles[norm]}"
        else:
            seen_titles[norm] = s.get("paper_id", s["source"])
    items = [_batch_item_payload(state) for state in prepared]
    # 屏障：任一未就绪则不写任何图，交 agent 介入解决 warning 后再写图
    if any(s["status"] not in {"graph_ready", "duplicate_found"} for s in prepared):
        print(json.dumps({
            "status": "partial", "phase": "prepare", "items": items,
            "next": "修正 agent_required 论文的 semantic 文件后逐个 --resume（自动写图）；"
                    "graph_ready 论文 --resume <txn> 提交写图；全部就绪后重跑 --inbox 可批量写图",
        }, ensure_ascii=False, indent=2))
        return 1
    # Phase 2：全部就绪，批量写图
    results = []
    for state in prepared:
        if state["status"] == "graph_ready":
            state = _run_phase(state, verbose, run_commit)
        results.append(_batch_item_payload(state))
    print(json.dumps({
        "status": "completed" if all(r["status"] in {"completed", "duplicate_found"} for r in results) else "partial",
        "phase": "commit", "items": results,
    }, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] in {"completed", "duplicate_found"} for r in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--pdf", help="inbox/ 下的单个 PDF 文件路径")
    source_group.add_argument("--inbox", action="store_true", help="按文件名顺序循环摄入 inbox/ 下的全部 PDF")
    source_group.add_argument("--resume", help="恢复已有事务 ID")
    source_group.add_argument("--validate", help="对事务跑全量语义校验并输出 warning（不跑 pipeline，供修正后自检）")
    source_group.add_argument("--raw", help="已入库 raw paper.md 路径（网上下载等非 inbox 来源）")
    parser.add_argument("--verbose", action="store_true", help="进度打印到 stdout（调试/建设/审计用；默认写日志文件）")
    args = parser.parse_args()
    is_resume = bool(args.resume)
    if args.resume:
        state = inbox_state.load(args.resume)
        if not state:
            raise SystemExit(f"ERROR: 事务不存在: {args.resume}")
        resume_after_semantic_fix(state)
    elif args.raw:
        raw_path = (REPO / args.raw).resolve()
        if raw_path.is_dir():
            raw_path = raw_path / "paper.md"
        if not raw_path.is_file():
            raise SystemExit(f"ERROR: raw 不存在: {args.raw}")
        state = new_state_for_raw(raw_path)
    elif args.pdf:
        pdf_path = (REPO / args.pdf).resolve()
        if not pdf_path.is_file():
            raise SystemExit(f"ERROR: PDF 不存在: {args.pdf}")
        state = new_state_for_pdf(pdf_path)
    elif args.validate:
        state = inbox_state.load(args.validate)
        if not state:
            raise SystemExit(f"ERROR: 事务不存在: {args.validate}")
        print(json.dumps(validate_transaction(state), ensure_ascii=False, indent=2))
        return
    else:
        raise SystemExit(run_inbox_batch(args.verbose))

    result = run_one(state, args.verbose)
    if is_resume:
        maintenance = ic.run_resume_post_maintenance(result)
        if maintenance is not None:
            result["maintenance"] = maintenance
    print_result(result)


def validate_transaction(state: dict) -> dict:
    """对事务跑全量语义校验，输出结构化报告（不跑 pipeline）。供 agent 修正后 --validate 自检。"""
    try:
        sem_hard, slot_warnings = step_validate_semantics(state)
    except Exception as exc:
        return {"status": "error", "transaction_id": state.get("transaction_id", ""),
                "error": f"语义槽校验失败: {exc}"}
    return {
        "status": "pass" if not sem_hard and not [w for w in slot_warnings if ic.is_blocking_warning(w, NON_BLOCKING_ISSUES)] else "fail",
        "transaction_id": state.get("transaction_id", ""),
        "paper_id": state.get("paper_id", ""),
        "semantic_path": state.get("semantic_path", ""),
        "hard_errors": sem_hard,
        "warnings": [
            {"issue": w["issue"], "section": w["section"], "line": w["line"],
             "field": w.get("field", ""), "reason": w["reason"],
             "blocking": ic.is_blocking_warning(w, NON_BLOCKING_ISSUES)}
            for w in slot_warnings
        ],
    }


def find_ready_txn(source: str) -> dict | None:
    """查找该 inbox 源已有的 graph_ready/finalize 事务。

    重跑 --inbox 时复用而非重建事务（避免被去重判为 duplicate_found 而跳过写图）。
    """
    import glob
    for path in glob.glob(str(REPO / "temp" / "inbox-state" / "*.json")):
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("source") == source and d.get("status") in ("graph_ready", "finalize"):
            return d
    return None


def _run_phase(state: dict, verbose: bool, fn) -> dict:
    """执行一个 pipeline 阶段，隔离 quiet 模式进度日志。"""
    set_progress_file(None)
    set_progress_log_path(None)
    if not verbose:
        (REPO / "temp" / "inbox-state").mkdir(parents=True, exist_ok=True)
        log_path = REPO / "temp" / "inbox-state" / f"{state['transaction_id']}.log"
        set_progress_file(log_path.open("a", encoding="utf-8"))
        set_progress_log_path(log_path)
        progress(f"ingest_paper.py 日志: {log_path.relative_to(REPO)}")
    try:
        state = fn(state)
    except Exception as exc:
        state["status"] = "failed"
        state["errors"] = [f"未预期异常: {type(exc).__name__}: {exc}"]
        inbox_state.save(state["transaction_id"], state)
    close_progress_file()
    return state


def run_one(state: dict, verbose: bool) -> dict:
    """执行一个事务全流程（prepare+commit），隔离 quiet 模式进度日志。"""
    # graph validation 失败会留下 failed + resume_from=graph_ready；直接交给
    # commit 状态机恢复，避免 run_prepare 的阶段白名单吞掉恢复请求。
    if state.get("status") == "failed" and state.get("resume_from") == "graph_ready":
        if state.get("failure_context") == _graph_retry_context(state):
            state["retryable"] = False
            state["next_action"] = "repair_graph_identity_or_metadata_then_resume"
            state["resume_blocked"] = "unchanged_failure_context"
            inbox_state.save(state["transaction_id"], state)
            return state
        state.pop("resume_blocked", None)
        state.pop("retryable", None)
        state.pop("next_action", None)
        result = _run_phase(state, verbose, run_commit)
        _mark_graph_failure(result)
        return result
    state = _run_phase(state, verbose, run_prepare)
    if state["status"] == "graph_ready":
        state = _run_phase(state, verbose, run_commit)
    _mark_graph_failure(state)
    return state


def print_result(state: dict) -> None:
    """保持单篇调用的 JSON 输出契约。"""
    if state["status"] == "completed":
        payload = {
            "status": "completed",
            "paper_id": state.get("paper_id"),
            "raw_dir": state.get("raw_dir"),
            "wiki_path": state.get("wiki_path"),
            "engine": state.get("engine"),
            "graph_report": state.get("graph_report"),
            "proposition_status": state.get("proposition_status"),
            "proposition_details": state.get("proposition_details"),
            "bibliographic_worker": (state.get("bibliographic_review") or {}).get("worker"),
            "relationship_worker": (state.get("relationship_review") or {}).get("worker"),
            "semantic_repair_worker": state.get("semantic_repair_worker"),
            "quality_status": "degraded" if state.get("quality_warnings") else "complete",
            "quality_warnings": state.get("quality_warnings", []),
            "transaction_id": state["transaction_id"],
        }
        if state.get("maintenance") is not None:
            payload["maintenance"] = state["maintenance"]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif state["status"] == "duplicate_found":
        print(json.dumps({
            "status": "duplicate_found",
            "title": state.get("dedup_title"),
            "dedup_result": state.get("dedup_result"),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
    elif state["status"] == "agent_required":
        review_agent = (state.get("bibliographic_review") or {}).get("status") == "agent_required"
        relationship_agent = (
            (state.get("relationship_review") or {}).get("status") == "agent_required"
        )
        if review_agent:
            message = "INGEST_BACKEND=agent，需要 agent 接管书目预审"
        elif relationship_agent:
            message = "需要 agent 接管论文关系候选裁决"
        elif state.get("semantic_repair_worker"):
            message = "需要 agent 接管局部语义修补"
        else:
            message = "INGEST_BACKEND=agent，需要 agent 接管 3.3 wiki 撰写"
        review_path = (state.get("bibliographic_review") or {}).get("draft_path", "")
        payload = {
            "status": "agent_required",
            "message": message,
            "prompt": state.get("agent_prompt", ""),
            "write_to": state.get("agent_write_to", "") or review_path,
            "pipeline_plan": PIPELINE_PLAN_AGENT,
            "bibliographic_worker": (state.get("bibliographic_review") or {}).get("worker"),
            "relationship_worker": (state.get("relationship_review") or {}).get("worker"),
            "semantic_repair_worker": state.get("semantic_repair_worker"),
            "transaction_id": state["transaction_id"],
        }
        if review_path:
            payload["review_path"] = review_path
        print(json.dumps(inbox_state.output_payload(state, payload), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(inbox_state.output_payload(state, {
            "status": state["status"],
            "errors": state.get("errors", []),
            "bibliographic_review": state.get("bibliographic_review"),
            "transaction_id": state["transaction_id"],
            "resume_from": state.get("resume_from"),
            "retryable": state.get("retryable"),
            "next_action": state.get("next_action"),
            "failure_signature": state.get("failure_signature"),
        }), ensure_ascii=False, indent=2))
        # 失败时 quiet 模式自动展开进度日志到 stdout，agent 无需额外读日志文件
        log_path = get_progress_log_path()
        if log_path is not None:
            from pathlib import Path
            p = Path(log_path) if not isinstance(log_path, Path) else log_path
            print("\n--- 进度日志 ---", flush=True)
            print(p.read_text(encoding="utf-8"), end="", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

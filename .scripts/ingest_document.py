#!/usr/bin/env python3
"""ingest_document.py — 代码驱动的通用文档摄入编排器（admin/teaching/business）。

3.3 分两阶段调用 LLM (call_text)：3.3a 撰写 wiki（带提取全文）→
3.4 校验通过 → 3.3b 抽取语义槽（续接对话，只带 wiki）。其余步骤全纯代码。
流程: 3.1 dedup_check → 3.2 preprocess（textutil/pandoc 提取）→ 3.3a write_wiki →
3.4 validate_wiki → 3.3b write_slots → 3.5 fill_semantics → 3.6 validate_semantics →
[3.6b repair] → 落位 → 3.7 update_graph → 3.8 validate_graph → 3.9 finalize_tail
修复循环: wiki 硬错误回 3.3a 重写；语义槽硬错误回 3.3b 重写（保留 wiki）；
warning 走 3.6b 局部修复。各阶段独立重试，最多 3 次。
状态: temp/inbox-state/<txn-id>.json，可从任意步骤恢复。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import inbox_state
import trash_util
import ingest_common as ic
import ingest_pipeline
import source_locator as sl
import wiki_locator as wl
from llm_structured import call_text, ingest_mode
from ingest_common import (parse_meta_block, validate_meta, extract_year_from_meta,
                           has_type_mismatch, has_year_mismatch,
                           progress, parse_delimited, set_progress_file, set_progress_log_path)
import yaml
from ingest_check import (STATUS_ENUM_BY_DOMAIN, STATUS_ENUM_ALL, valid_partial_date)

TEMP_EXTRACT = REPO / "temp" / "inbox-extract"
NON_BLOCKING_ISSUES = ("bare_abbreviation", "descriptive_phrase")
MAX_RETRIES = 3
WIKI_DELIMITER = "<<<WIKI>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"

PIPELINE_PLAN_AGENT = [
    {"step": "判断重复 + 提取文本", "needs_agent": False,
     "desc": "dedup(查图+查raw) → textutil/pandoc 提取文档全文(doc.md)，一次程序调用完成"},
    {"step": "撰写 wiki 与语义槽", "needs_agent": True,
     "desc": "agent 接管：读 doc.md → 判断页面类型 + 撰写 wiki → 抽取语义槽，一次输出 <<<WIKI>>> + <<<SLOTS>>>"},
    {"step": "更新 Graph + 校验 + 收尾 + 清理", "needs_agent": False,
     "desc": "validate→落位→graph_ingest 建边→validate_graph→finalize_tail(log/index)+清理 inbox 源，--resume 一次调用完成"},
]

PIPELINE_PLAN_API = [
    {"step": "摄入文档（代码+API 全自动）", "needs_agent": False,
     "desc": "dedup→extract→wiki(API)→slots(API)→validate→落位→建图→图校验→收尾+清理，--file 一条命令完成；agent 仅读最终 JSON 确认 admin-id/路径/边数"},
]

def pipeline_plan_for(mode: str) -> list[dict]:
    """按摄入后端模式返回对应流水线 plan。"""
    return {"agent": PIPELINE_PLAN_AGENT, "api": PIPELINE_PLAN_API}.get(mode, PIPELINE_PLAN_AGENT)

DOMAIN_CONFIG = {
    "admin": {
        "page_types": {"policy", "procedure", "decision", "meeting-summary", "speech", "activity", "application", "profile", "reference"},
        "type_to_subdir": {"policy": "policies", "procedure": "procedures", "decision": "decisions", "meeting-summary": "meetings", "speech": "speeches", "activity": "activities", "application": "applications", "profile": "profile", "reference": "references"},
        "kw_predicates": {"涉及", "讨论", "形成决策", "推动", "申请事项", "适用对象"},
        "nav_predicates": {"涉及", "讨论", "形成决策", "依据", "替代", "汇报", "发布者", "负责人", "承办部门", "推动", "申请事项", "适用对象"},
        "extra_frontmatter": ["department"],
        "temporal_page_types": {"policy", "procedure", "decision"},
        "subject_pronoun": "本文件",
        "domain_name": "行政",
    },
    "teaching": {
        "page_types": {"course", "topic", "lecture", "assessment", "pedagogy"},
        "type_to_subdir": {"course": "courses", "topic": "topics", "lecture": "lectures", "assessment": "assessments", "pedagogy": "pedagogy"},
        "kw_predicates": {"涉及", "讨论", "涵盖", "考核"},
        "nav_predicates": {"涉及", "讨论", "涵盖", "考核", "前置", "后续", "依据", "适用", "开课单位", "主讲人"},
        "extra_frontmatter": ["course", "semester"],
        "temporal_page_types": {"course"},
        "subject_pronoun": "本文档",
        "domain_name": "教学",
    },
    "business": {
        "page_types": {"plan", "research", "competitor", "strategy", "project", "meeting-summary", "contract", "financial"},
        "type_to_subdir": {"plan": "plans", "research": "research", "competitor": "competitors", "strategy": "strategies", "project": "projects", "meeting-summary": "conferences", "contract": "contracts", "financial": "financials"},
        "kw_predicates": {"涉及", "讨论", "分析", "规划"},
        "nav_predicates": {"涉及", "讨论", "分析", "规划", "依据", "竞争", "合作", "替代", "发布者", "负责人", "承办部门"},
        "extra_frontmatter": ["domain"],
        "subject_pronoun": "本文件",
        "domain_name": "商业",
    },
}


# ===== 工具函数 =====

def run(command: list[str]) -> str:
    return ic.run(command, REPO)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:60] if text else "untitled"


def extract_doc_text(source_path: Path, extract_dir: Path | None = None) -> str:
    """从 .pdf/.docx/.doc/.pptx/.txt 提取纯文本。PDF 经 extractor 级联提取。"""
    suffix = source_path.suffix.lower()
    if suffix in (".txt", ".md"):
        return source_path.read_text(encoding="utf-8")
    if suffix in (".docx", ".doc"):
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(source_path)],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else ""
    if suffix == ".pptx":
        result = subprocess.run(
            ["pandoc", "-t", "plain", str(source_path)],
            capture_output=True, text=True,
        )
        return result.stdout if result.returncode == 0 else ""
    if suffix == ".pdf":
        if extract_dir is None:
            return ""
        paper_id = "extern"
        # 默认用 mineru；失败时检测是否扫描件，降级到 blsc_ocr（LLM 视觉模型 OCR）
        result = subprocess.run(
            ["python3", str(REPO / ".scripts/extractor.py"),
             "--paper", paper_id,
             "--external-pdf", str(source_path),
             "--papers-dir", str(extract_dir)],
            capture_output=True, text=True,
        )
        md_path = extract_dir / paper_id / "paper.md"
        if md_path.is_file():
            return md_path.read_text(encoding="utf-8")
        # mineru 失败：检测是否扫描件（PDF 无文本层或极少文本）
        if _is_scanned_pdf(source_path):
            # 降级到 blsc_ocr（LLM 视觉模型逐页 OCR）
            subprocess.run(
                ["python3", str(REPO / ".scripts/extractor.py"),
                 "--paper", paper_id,
                 "--external-pdf", str(source_path),
                 "--papers-dir", str(extract_dir),
                 "--engine", "blsc_ocr"],
                capture_output=True, text=True,
            )
            if md_path.is_file():
                return md_path.read_text(encoding="utf-8")
        return ""
    return ""


def _is_scanned_pdf(pdf_path: Path) -> bool:
    """检测 PDF 是否为扫描件：用 pymupdf 检查每页文本量，平均 <50 字符/页视为扫描件。"""
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        if len(doc) == 0:
            return True
        total_text = sum(len(page.get_text().strip()) for page in doc)
        doc.close()
        return total_text / len(doc) < 50
    except Exception:
        return False


def extract_admin_date(filename: str, doc_text: str = "") -> str:
    """从文件名或文档内容提取日期 YYYY-MM-DD。"""
    stem = Path(filename).stem
    m = re.search(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})", stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if doc_text:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", doc_text)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return datetime.now().strftime("%Y-%m-%d")


def generate_admin_id(filename: str, title: str) -> str:
    """生成 admin-id：YYYYMMDD-title-slug。"""
    date_part = ""
    m = re.search(r"(\d{4})(\d{2})(\d{2})", filename)
    if m:
        date_part = m.group(1) + m.group(2) + m.group(3)
    else:
        date_part = datetime.now().strftime("%Y%m%d")
    slug = slugify(title)[:40] if title else slugify(Path(filename).stem)[:40]
    return f"{date_part}-{slug}"


def ensure_unique_admin_id(admin_id: str, subdir: str, subproject: str = "admin") -> str:
    """冲突自动消歧：加 -2, -3..."""
    base = admin_id
    wiki_dir = REPO / subproject / "wiki" / subdir
    n = 1
    while (wiki_dir / f"{admin_id}.md").exists():
        n += 1
        admin_id = f"{base}-{n}"
    return admin_id


def get_subdir(page_type: str, subproject: str = "admin") -> str:
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    return cfg["type_to_subdir"].get(page_type, cfg["type_to_subdir"].get("reference", "references"))


# ===== 3.1 dedup_check =====

def step_dedup_check(state: dict) -> tuple[bool, str]:
    """查 graph.db + raw 目录是否已摄入同一文档。"""
    import graph_lib as gl
    subproject = state.get("subproject", "admin")
    source_filename = state["source_filename"]
    conn = gl.connect()
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE path LIKE ? AND title LIKE ?",
        (f"{subproject}/wiki/%", f"%{Path(source_filename).stem}%"),
    ).fetchall()
    conn.close()
    if rows:
        state["dedup_result"] = [{"path": r[0], "title": r[1]} for r in rows]
        return True, f"已摄入: {rows[0][0]}"
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    for sub in cfg["type_to_subdir"].values():
        raw_dir = REPO / subproject / "raw" / sub
        if raw_dir.exists():
            for f in raw_dir.iterdir():
                if f.name == source_filename:
                    state["dedup_result"] = [{"path": str(f.relative_to(REPO))}]
                    return True, f"已摄入(raw): {f.name}"
    return False, ""


# ===== 3.2 preprocess =====

def step_preprocess(state: dict) -> tuple[bool, str]:
    """提取全文，并在原文件不支持原生 locator 时暂存 raw companion。"""
    extract_dir = REPO / state["extract_dir"]
    extract_dir.mkdir(parents=True, exist_ok=True)
    source_path = REPO / state["source"]
    doc_text = extract_doc_text(source_path, extract_dir)
    if not doc_text.strip():
        return False, "文档提取失败（空文本）"
    (extract_dir / "doc.md").write_text(doc_text, encoding="utf-8")
    native_kind = sl.native_locator_kind(source_path)
    if native_kind:
        state["raw_locator_kind"] = native_kind
        state["locator_source_filename"] = state["source_filename"]
    else:
        companion_name = sl.locator_companion_name(state["source_filename"])
        companion_path = extract_dir / companion_name
        if companion_path.name != "doc.md":
            companion_path.write_text(doc_text, encoding="utf-8")
        state["raw_locator_kind"] = "companion"
        state["locator_source_filename"] = companion_name
    state["date_str"] = extract_admin_date(state["source_filename"], doc_text)
    return True, ""


# ===== 3.3a write_wiki =====

def build_doc_wiki_prompt(doc_text: str, doc_id: str, date_str: str,
                          subproject: str = "admin",
                          errors: list[str] | None = None) -> str:
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    page_types = "/".join(sorted(cfg["page_types"]))
    extra_fm = "，".join(cfg["extra_frontmatter"])
    temporal_page_types = sorted(cfg.get("temporal_page_types", []))
    temporal_note = ""
    if temporal_page_types:
        temporal_note = (
            f" 若 type 为 {'/'.join(temporal_page_types)}，且原文有明确施行/生效或废止日期，"
            "请在 frontmatter 加 effective_from、effective_to（YYYY-MM-DD；无明确截止则不写或留空）。"
        )
    locator_context = wl.annotate_raw_lines(doc_text, "RAW")
    return f"""你是知识库摄入组件。基于以下{cfg["domain_name"]}文档上下文，撰写自然、简洁的{cfg["domain_name"]} wiki 页面。

[{cfg["domain_name"]}文档上下文]
{locator_context}
{error_section}

[文档 ID] {doc_id}
[日期] {date_str}

[要求]
1. 从文档内容判断页面类型（{page_types}），写入 frontmatter type 字段。
2. frontmatter 必须包含: title, type, sources (指向 raw 路径), source_type: official-doc, date, status(枚举: active|completed|confirmed|deprecated|draft|final；协议/制度已签署生效用 confirmed，进行中用 active，草稿用 draft), created, updated。如有信息加 {extra_fm}，如有关联文档加 related。{temporal_note}
3. 正文结构: # 标题 → ## Navigation（2-4 句导航概述）→ ## Content（用自然标题组织连贯主题，可用短段或列表，不复制原文结构做流水账）。
4. 简写+去冗余，忠实于原文，不编造。
5. 上下文每个非空行前都有程序提供的 `<RAW#Lx>`。每个事实段落或事实列表项末尾用 `[^rN]` 引用这些 handle；不得自造行号或使用 `#全篇`。
6. 页面末尾写 `## Sources`，定义严格为单独一行 `[^rN]: RAW#Lx`。程序落位时会把 RAW 替换为最终 Raw 路径。
7. 输出完整 wiki markdown（含 frontmatter），用 <<<WIKI>>> 分隔符包裹。

[输出格式]
<<<META>>>
doc_date: <文档日期，从内容提取；有什么提什么，如 2024 或 2024-03-15>
title: <文档标题>
doc_type: document
<<</META>>>
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）"""

def build_agent_doc_wiki_slots_prompt(doc_text: str, doc_id: str, date_str: str,
                          subproject: str = "admin",
                          errors: list[str] | None = None) -> str:
    """agent 模式专用 prompt：合并 wiki 撰写 + 语义槽抽取为单次任务，省一轮 LLM 调用。"""
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    page_types = "/".join(sorted(cfg["page_types"]))
    extra_fm = "，".join(cfg["extra_frontmatter"])
    pronoun = cfg["subject_pronoun"]
    kw_preds = "/".join(sorted(cfg["kw_predicates"]))
    rel_preds = "/".join(sorted(cfg["nav_predicates"] - cfg["kw_predicates"]))
    temporal_page_types = sorted(cfg.get("temporal_page_types", []))
    temporal_note = ""
    if temporal_page_types:
        temporal_note = (
            f" 若 type 为 {'/'.join(temporal_page_types)}，且原文有明确施行/生效或废止日期，"
            "请在 frontmatter 加 effective_from、effective_to（YYYY-MM-DD；无明确截止则不写或留空）。"
        )
    locator_context = wl.annotate_raw_lines(doc_text, "RAW")
    return f"""你是知识库摄入组件。请一次性完成{cfg["domain_name"]} wiki 页面撰写 + 语义槽抽取。

[{cfg["domain_name"]}文档上下文]
{locator_context}{error_section}

[文档 ID] {doc_id}
[日期] {date_str}

[要求]
1. 从文档内容判断页面类型（{page_types}），写入 frontmatter type 字段。
2. frontmatter 必须包含: title, type, sources (写任意占位值，落位时程序会按 page_type 自动回填真实 raw 路径), source_type: official-doc, date, status(枚举: active|completed|confirmed|deprecated|draft|final；协议/制度已签署生效用 confirmed，进行中用 active，草稿用 draft), created, updated。如有信息加 {extra_fm}，如有关联文档加 related。{temporal_note}
3. 正文结构: # 标题 → ## Navigation（2-4 句导航概述）→ ## Content（用自然标题组织连贯主题，可用短段或列表）。
4. 简写+去冗余，忠实于原文，不编造。
5. 三元组客体须为规范概念名/实体名：不含逗号、卷号页码、年份或描述性短语；核心词格式统一为「中文英文(缩写)」；无公认缩写则不写括号；无对应中文则只写英文，无对应英文则只写中文。
6. 三元组只填文档明确涉及的核心主题和导航关系，宁少勿多。
7. 每个事实段落或事实列表项末尾用 `[^rN]` 引用上下文实际出现的 `<RAW#Lx>`；页面末尾写 `## Sources`，定义严格为 `[^rN]: RAW#Lx`。不得自造行号或使用 `#全篇`。
8. 用 <<<WIKI>>> 和 <<<SLOTS>>> 两个分隔符分别包裹输出（先 wiki 后语义槽）。

语义槽格式：
三元组:
<主体|谓词|客体，每行一条>
主体用"{pronoun}"代表这份文档；人物/部门关系直接写人名/部门名作主体。
文档→主题 建议谓词: {kw_preds}
文档→实体 建议谓词: {rel_preds}
只使用以上谓词；未列出的谓词不要使用。

[输出格式]
<<<META>>>
doc_date: <文档日期，从内容提取；有什么提什么>
title: <文档标题>
doc_type: document
<<</META>>>
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）
<<<SLOTS>>>
（语义槽）"""


def step_write_wiki(state: dict) -> tuple[bool, str]:
    """3.3a 第一阶段：调 LLM 撰写 wiki。agent 模式下检查 agent 是否已写入文件。"""
    extract_dir = REPO / state["extract_dir"]
    wiki_file = extract_dir / "wiki.md"
    # Agent 模式往返：agent 已将 wiki 写入文件 → 读取跳过 LLM
    if state.pop("_awaiting_agent_wiki", False) and wiki_file.exists():
        wiki_content = wiki_file.read_text(encoding="utf-8")
        state["wiki_content"] = wiki_content
    else:
        doc_path = extract_dir / "doc.md"
        doc_text = doc_path.read_text(encoding="utf-8")
        context_text = ic.build_source_context(
            "document", doc_text, force_reduced=(ingest_mode() == "api"))
        if "admin_id" not in state:
            title = ""
            m = re.search(r"^#\s+(.+)", doc_text, re.M)
            if m:
                title = m.group(1).strip()
            if not title:
                title = Path(state["source_filename"]).stem
            base_id = generate_admin_id(state["source_filename"], title)
            state["admin_id"] = ensure_unique_admin_id(base_id, "references", state.get("subproject", "admin"))
            state["_pending_title"] = title
        errors = state.get("wiki_errors", []) if state.get("wiki_retry", 0) > 0 else None
        prompt = build_doc_wiki_prompt(context_text, state["admin_id"], state.get("date_str", ""), state.get("subproject", "admin"), errors)
        result = call_text(prompt, max_tokens=4096, retries=1, operation="ingest_wiki_write",
                           system="你是受程序约束的知识库摄入组件，基于文档撰写 wiki 页面。")
        if result.get("status") == "agent_required":
            state["_awaiting_agent_wiki"] = True
            state["agent_required"] = True
            state["agent_prompt"] = result.get("prompt", "")
            state["agent_write_to"] = str(wiki_file.relative_to(REPO))
            return False, "需要 agent 接管"
        if not result.get("ok"):
            return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
        text = result.get("text", "")
        # META 交叉校验
        meta = parse_meta_block(text)
        if meta:
            admin_id = state.get("admin_id", "")
            expected_year = admin_id[:4] if len(admin_id) >= 4 and admin_id[:4].isdigit() else ""
            mismatches = validate_meta(meta, {"doc_type": "document", "year": expected_year})
            if has_type_mismatch(mismatches):
                state["type_mismatch"] = True
                state["meta_mismatches"] = mismatches
                state["meta_info"] = meta
                return False, f"doc_type 不一致（程序=document, LLM={meta.get('doc_type', '')}），跳过待 agent 判断"
            if has_year_mismatch(mismatches):
                # 年份不一致→修正 admin_id 中的 YYYYMMDD（保留 MMDD，替换年份）
                llm_year = extract_year_from_meta(meta)
                if llm_year and expected_year:
                    old_id = admin_id
                    parts = admin_id.split("-", 1)
                    date_part = parts[0]  # YYYYMMDD
                    new_date = llm_year + date_part[4:]  # 保留 MMDD
                    corrected_id = new_date + ("-" + parts[1] if len(parts) > 1 else "")
                    state["admin_id"] = ensure_unique_admin_id(corrected_id, "references", state.get("subproject", "admin"))
                    state["meta_year_corrected"] = {"from": expected_year, "to": llm_year, "old_id": old_id}
        wiki_content = parse_delimited(text, WIKI_DELIMITER)
        if not wiki_content:
            return False, "LLM 输出缺少 <<<WIKI>>> 段"
        wiki_file.write_text(wiki_content, encoding="utf-8")
        state["wiki_content"] = wiki_content
    # 从 wiki frontmatter 解析 type，修正 subdir 和路径
    fm_match = re.match(r"^---\n(.*?)\n---", wiki_content, re.S)
    if fm_match:
        fm = fm_match.group(1)
        type_m = re.search(r'^type:\s*(\S+)', fm, re.M)
        if type_m:
            page_type = type_m.group(1).strip()
            subdir = get_subdir(page_type, state.get("subproject", "admin"))
            state["admin_id"] = ensure_unique_admin_id(state["admin_id"], subdir, state.get("subproject", "admin"))
            state["raw_dir"] = f"{state.get("subproject", "admin")}/raw/{subdir}"
            state["wiki_path"] = f"{state.get("subproject", "admin")}/wiki/{subdir}/{state['admin_id']}"
    if "wiki_path" not in state:
        state["raw_dir"] = f"{state.get("subproject", "admin")}/raw/references"
        state["wiki_path"] = f"{state.get("subproject", "admin")}/wiki/references/{state['admin_id']}"
    # sources 回填：raw_dir 确定后，覆盖 LLM 猜测的 sources 路径（消除 memory:// 占位）
    locator_filename = state.get("locator_source_filename", state["source_filename"])
    correct_sources = f"{state['raw_dir']}/{locator_filename}"
    state["raw_locator"] = correct_sources
    wiki_content = re.sub(
        r'(sources:\s*\n\s*-\s*)(?:path:\s*)?"?[^\n]+"?',
        f'\\1"{correct_sources}"', wiki_content, count=1)
    wiki_content = wl.replace_raw_placeholder(wiki_content, correct_sources)
    wiki_file.write_text(wiki_content, encoding="utf-8")
    state["wiki_content"] = wiki_content
    return True, ""


# ===== 3.4 validate_wiki =====

def step_validate_wiki(state: dict) -> list[str]:
    """校验 wiki 结构：frontmatter 必填字段 + 段落。"""
    wiki = state.get("wiki_content", "")
    errors = []
    if not wiki.startswith("---"):
        errors.append("缺少 frontmatter 起始 ---")
        return errors
    fm_match = re.match(r"^---\n(.*?)\n---", wiki, re.S)
    if not fm_match:
        errors.append("frontmatter 格式错误")
        return errors
    fm = fm_match.group(1)
    required = ["title", "type", "sources", "source_type", "date"]
    for field in required:
        if field not in fm:
            errors.append(f"frontmatter 缺字段: {field}")
    subproject = state.get("subproject", "admin")
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    valid_types = cfg["page_types"]
    type_m = re.search(r'^type:\s*(\S+)', fm, re.M)
    if type_m:
        if type_m.group(1).strip() not in valid_types:
            errors.append(f"type 应为 {', '.join(sorted(valid_types))} 之一")
    else:
        errors.append("frontmatter 缺 type 字段")
    if "memory:" in fm:
        errors.append("sources 不得用 memory:// 占位路径，必须指向 raw/ 下的真实文件")
    if "## Navigation" not in wiki:
        errors.append("缺少 ## Navigation 段")
    if "## Content" not in wiki:
        errors.append("缺少 ## Content 段")
    # 前移到落位前:frontmatter 枚举/日期/sources 格式校验(避免 finalize 后半完成)
    fm_parsed = {}
    try:
        fm_match2 = re.match(r"^---\n(.*?)\n---", wiki, re.S)
        if fm_match2:
            parsed = yaml.safe_load(fm_match2.group(1))
            if isinstance(parsed, dict):
                fm_parsed = parsed
    except Exception:
        fm_parsed = {}
    status_enum = STATUS_ENUM_BY_DOMAIN.get(subproject, STATUS_ENUM_ALL)
    if fm_parsed.get("status") and fm_parsed["status"] not in status_enum:
        errors.append(f"status 非法值 '{fm_parsed['status']}'，合法: {sorted(status_enum)}")
    if fm_parsed.get("date") and not valid_partial_date(str(fm_parsed["date"])):
        errors.append(f"date 日期格式非法 '{fm_parsed['date']}'")
    for field in ("created", "updated"):
        if fm_parsed.get(field) and not valid_partial_date(str(fm_parsed[field]), require_day=True):
            errors.append(f"{field} 日期格式非法 '{fm_parsed[field]}'")
    effective_from = None
    effective_to = None
    for field in ("effective_from", "effective_to"):
        if fm_parsed.get(field) and str(fm_parsed[field]).strip():
            val = str(fm_parsed[field])
            if not valid_partial_date(val, require_day=True):
                errors.append(f"{field} 日期格式非法 '{val}'")
            elif field == "effective_from":
                effective_from = val
            else:
                effective_to = val
    if effective_from and effective_to and effective_to < effective_from:
        errors.append("effective_to 早于 effective_from")
    srcs = fm_parsed.get("sources", [])
    if isinstance(srcs, str):
        errors.append("sources 须为 YAML 列表(每项独立引号)，不得写成单字符串")
    else:
        for one_src in (srcs or []):
            ss = str(one_src)
            if any(ch in ss for ch in (",", "，")):
                errors.append(f"sources 项含逗号(会被图摄入按逗号拆分): {ss}")
    extract_dir_value = state.get("extract_dir", "")
    if extract_dir_value:
        extract_dir = REPO / extract_dir_value
        locator_file = extract_dir / state.get(
            "locator_source_filename", state.get("source_filename", ""))
        raw_path = state.get("raw_locator", "")
        overrides = {raw_path: locator_file} if raw_path and locator_file.is_file() else {}
        wiki_file = extract_dir / "wiki.md"
    else:
        wiki_file = None
        overrides = {}
    if wiki_file is not None and wiki_file.is_file():
        errors.extend(wl.validate_wiki_page(
            wiki_file, require_citations=True, raw_overrides=overrides))
    return errors


def rollback_committed(state: dict) -> list[str]:
    """validate_graph 失败时回滚已落位的 wiki/raw 文件与本次刚建的图节点。"""
    import graph_lib as gl
    rolled = []
    wiki_path = REPO / (state["wiki_path"] + ".md")
    if wiki_path.exists():
        trash_util.trash_path(wiki_path)
        rolled.append(state["wiki_path"])
    raw_dir = REPO / state.get("raw_dir", "")
    src_name = state.get("source_filename", "")
    if raw_dir.exists() and src_name:
        raw_file = raw_dir / src_name
        if raw_file.exists():
            trash_util.trash_path(raw_file)
            rolled.append(f"{state['raw_dir']}/{src_name}")
    page = state.get("wiki_path", "")
    report = state.get("graph_report") or {}
    raw_nodes = report.get("raw_nodes", [])
    targets = [t for t in [page] + list(raw_nodes) if t]
    try:
        conn = gl.connect(gl.graph_db_for(page))
        conn.execute("PRAGMA foreign_keys=ON")
        nodes_deleted = 0
        for t in targets:
            eids = [row["id"] for row in conn.execute(
                "SELECT id FROM edges WHERE subject=? OR object=?", (t, t))]
            if eids:
                ph = ",".join("?" * len(eids))
                conn.execute(f"DELETE FROM edge_evidence WHERE edge_id IN ({ph})", eids)
                conn.execute(f"DELETE FROM edges WHERE id IN ({ph})", eids)
            conn.execute("DELETE FROM aliases WHERE node_path=?", (t,))
            nodes_deleted += conn.execute("DELETE FROM nodes WHERE path=?", (t,)).rowcount
        conn.commit()
        conn.close()
        if nodes_deleted:
            rolled.append(f"图节点 {nodes_deleted}")
    except Exception as exc:
        rolled.append(f"图回滚失败: {exc}")
    return rolled


# ===== 3.3b write_slots =====

def build_doc_slots_prompt(wiki_content: str, subproject: str = "admin",
                           errors: list[str] | None = None) -> str:
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    error_section = ""
    if errors:
        error_section = "\n\n[上次语义槽的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    pronoun = cfg["subject_pronoun"]
    kw_preds = "/".join(sorted(cfg["kw_predicates"]))
    rel_preds = "/".join(sorted(cfg["nav_predicates"] - cfg["kw_predicates"]))
    return f"""基于你刚写好的 wiki 页面，为这份{cfg["domain_name"]}文档抽取语义槽。

[已写好的 wiki 页面]
<<<WIKI>>>
{wiki_content}
{error_section}

[要求]
1. 三元组客体须为规范概念名/实体名：不含逗号、卷号页码、年份或描述性短语；核心词格式统一为「中文英文(缩写)」，如 人才培养talent cultivation；无公认缩写则不写括号；无对应中文则只写英文，无对应英文则只写中文。
2. 三元组只填文档明确涉及的核心主题和导航关系，宁少勿多。
3. 用 <<<SLOTS>>> 分隔符包裹输出语义槽，格式如下：

三元组:
<主体|谓词|客体，每行一条>
主体用"{pronoun}"代表这份文档；人物/部门关系直接写人名/部门名作主体。
文档→主题 建议谓词: {kw_preds}
文档→实体 建议谓词: {rel_preds}
只使用以上谓词；未列出的谓词不要使用。

[语义槽填写示例（仅示格式，内容勿照搬）]
三元组:
{pronoun} | 涉及 | 人才培养talent cultivation
{pronoun} | 涉及 | 教学改革teaching reform
{pronoun} | 依据 | 高等教育法
{pronoun} | 发布者 | 物理系
{pronoun} | 负责人 | 张明远

[输出格式]
<<<SLOTS>>>
（语义槽）"""


def step_write_slots(state: dict) -> tuple[bool, str]:
    """3.3b 第二阶段：续接对话，抽语义槽。agent 模式下检查 agent 是否已写入文件。"""
    # agent 合并模式已产出语义槽 → 跳过 LLM 调用
    if state.get("slots_content"):
        return True, ""
    wiki_content = state.get("wiki_content", "")
    if not wiki_content:
        return False, "无 wiki_content"
    slots_file = REPO / state["extract_dir"] / "semantic.txt"
    # Agent 模式往返：agent 已将语义槽写入文件 → 读取跳过 LLM
    if state.pop("_awaiting_agent_slots", False) and slots_file.exists():
        slots_content = slots_file.read_text(encoding="utf-8")
        state["slots_content"] = slots_content
        return True, ""
    errors = state.get("slots_errors", []) if state.get("slots_retry", 0) > 0 else None
    prompt = build_doc_slots_prompt(wiki_content, state.get("subproject", "admin"), errors)
    result = call_text(prompt, max_tokens=4096, retries=1, operation="ingest_wiki_write",
                       system="你是受程序约束的知识库摄入组件，基于 wiki 页面抽取语义槽。")
    if result.get("status") == "agent_required":
        state["_awaiting_agent_slots"] = True
        state["agent_required"] = True
        state["agent_prompt"] = result.get("prompt", "")
        state["agent_write_to"] = str(slots_file.relative_to(REPO))
        return False, "需要 agent 接管"
    if not result.get("ok"):
        return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
    text = result.get("text", "")
    slots_content = parse_delimited(text, SLOTS_DELIMITER)
    if not slots_content:
        return False, "LLM 输出缺少 <<<SLOTS>>> 段"
    slots_file.write_text(slots_content, encoding="utf-8")
    state["slots_content"] = slots_content
    return True, ""


# ===== 3.5 fill_semantics =====

def normalize_slots(text: str) -> str:
    lines = text.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.append(stripped)
    return "\n".join(result) + "\n"


def step_fill_semantics(state: dict) -> tuple[bool, str]:
    return ic.step_fill_semantics(state, REPO, normalize_slots)


# ===== 3.6 validate_semantics =====

def is_clearly_descriptive(obj: str) -> bool:
    if len(obj) <= 8:
        return False
    return bool(re.search(r'[\u3002,\uff0c\uff1b;]', obj))


def _doc_allowed_predicates(state: dict) -> set[str]:
    """文档域合法谓词集（按 subproject 动态 + predicate_tiers.yaml）。"""
    import graph_ingest
    subproject = state.get("subproject", "admin")
    cfg = DOMAIN_CONFIG.get(subproject, DOMAIN_CONFIG["admin"])
    allowed = set(cfg["nav_predicates"])
    try:
        import yaml
        tiers = yaml.safe_load((REPO / ".scripts/predicate_tiers.yaml").read_text(encoding="utf-8"))
        for pred_name in (tiers.get("predicates") or {}):
            allowed.add(pred_name)
    except Exception:
        pass
    return allowed


def step_validate_semantics(state: dict) -> tuple[list[str], list[dict]]:
    """校验语义槽合法性（委托 ingest_common）。"""
    return ic.validate_semantics(state, REPO, _doc_allowed_predicates(state))


def _build_repair_prompt(warnings: list[dict], is_second_pass: bool = False) -> str:
    items = "\n".join(f"- {w['line']} → {w['reason']}" for w in warnings)
    second_note = "\n[注意] 前次修复仍有残留，请更精准地修正。" if is_second_pass else ""
    return f"""请修正以下语义槽中的问题，只输出修正后的行（保持 主体|谓词|客体 格式）：

[问题]
{items}
{second_note}

[要求]
- descriptive_phrase：客体改为规范概念名/实体名，去除逗号、句号、描述性短语
- bare_abbreviation：含英文缩写时写为「中文英文(缩写)」格式
- duplicate_line：删除重复行
只修正有问题的行，不改正确的行。输出修正后的完整行。"""


def step_repair_slots(state: dict, warnings: list[dict]) -> tuple[bool, str]:
    """3.6b 局部修复：两级降级链 → agent 兜底（委托 ingest_common）。"""
    return ic.repair_slots(state, REPO, warnings, _build_repair_prompt,
                          step_validate_semantics, non_blocking_issues=NON_BLOCKING_ISSUES)


# ===== 落位（委托 ingest_common）=====

def _manifest_raw_files(state: dict) -> list[str]:
    files = [state["source_filename"]]
    companion = state.get("locator_source_filename")
    if companion and companion != state["source_filename"]:
        files.append(companion)
    return files


FINALIZE_CONFIG = {
    "doc_id_key": "admin_id",
    "manifest_files": _manifest_raw_files,
    "copy_source": True,
    "allow_existing_raw_dir": True,
}

FINALIZE_TAIL_CONFIG = {
    "doc_id_key": "admin_id",
    "get_log_path": lambda state, REPO: REPO / state.get("subproject", "admin") / "wiki" / "log.md",
    "get_index_path": lambda state, REPO: REPO / state.get("subproject", "admin") / "wiki" / "index.md",
    "index_section": None,
    "entry_prefix": "",
    "index_header": "# 行政文档索引",
    "build_log_entry": lambda ctx: (
        "\n## [" + ctx["today"] + "] ingest | ingest_document.py 摄入 " + ctx["doc_id"] + "\n"
        "- **来源与归档**：inbox 行政文档提取后落位至 `"
        + ctx["state"].get("raw_dir", "") + "/`。\n"
        "- **来源页**：新建 `" + ctx["page_name"] + ".md`，" + ctx["title"] + "。\n"
        "- **图谱巩固**：增量写入 " + str(ctx["edges"]) + " 条边"
        + ("，catch-all 关键词 " + str(ctx["report"].get("catch_all_keywords_added", 0)) + " 个"
           if ctx["report"].get("catch_all_keywords_added") else "") + "。\n"
        "- **验证**：`ingest_check --graph` PASS（ERROR=0）。\n"
    ),
}


def step_finalize(state: dict) -> tuple[bool, str]:
    return ic.step_finalize(state, REPO, FINALIZE_CONFIG)


def step_update_graph(state: dict) -> tuple[bool, str]:
    # --related-to 模式：先正常建图边（创建页面节点 + 语义/机械边），
    # 再建 raw 关系边 + 追加 sources 到目标页
    if state.get("related_to"):
        import graph_lib as gl
        # 1. 正常 graph_ingest：创建页面节点 + 语义/机械边（与普通文档一致）
        ok, msg = ic.step_update_graph(state, REPO)
        if not ok:
            return ok, msg
        # 2. 建 raw 关系边 + 追加 sources 到目标页
        target = state["related_to"]
        rel_type = state.get("relation_type", "supplementary")
        ic.link_raw_relation(state, REPO, target, rel_type)
        # 3. 把新 raw 追加到目标页 sources（frontmatter 解析，不依赖字段顺序）
        new_fm = gl.read_frontmatter(state["wiki_path"])
        new_sources = gl.parse_list_field(new_fm, "sources")
        new_src = new_sources[0] if new_sources else ""
        if new_src:
            ic.append_source_to_page(REPO, target, new_src)
        return True, ""
    return ic.step_update_graph(state, REPO)


def step_validate_graph(state: dict) -> list[str]:
    return ic.step_validate_graph(state, REPO)


def step_finalize_tail(state: dict) -> tuple[bool, str]:
    return ic.step_finalize_tail(state, REPO, FINALIZE_TAIL_CONFIG)

# ===== 主编排循环 =====



DOCUMENT_SPEC = {
    "script_name": "ingest_document.py",
    "preprocess_label": "文档提取（textutil/pandoc）",
    "completion_label_key": None,
    "repair_fail_strategy": "retry",
    "cleanup_after": "finalize_tail",
    "rollback_fn": rollback_committed,
    "finalize_tail_failure": "hard",
    "max_retries": MAX_RETRIES,
    "non_blocking_issues": NON_BLOCKING_ISSUES,
    "normalize_slots": normalize_slots,
    "steps": {
        "dedup_check": step_dedup_check,
        "preprocess": step_preprocess,
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


def run_pipeline(state: dict) -> dict:
    return ingest_pipeline.run_pipeline(state, DOCUMENT_SPEC, progress)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="inbox/ 下的文档文件路径")
    parser.add_argument("--subproject", choices=["admin", "teaching", "business"], default="admin", help="子项目域")
    parser.add_argument("--resume", help="恢复已有事务 ID")
    parser.add_argument("--related-to", help="关联到已有 wiki 页面路径（版本/补充材料），不新建 wiki 页")
    parser.add_argument("--relation-type", choices=["version", "supplementary", "translation"],
                        default="supplementary", help="关联类型（默认 supplementary 补充材料）")
    parser.add_argument("--verbose", action="store_true", help="进度打印到 stdout")
    args = parser.parse_args()
    if args.resume:
        state = inbox_state.load(args.resume)
        if not state:
            raise SystemExit(f"ERROR: 事务不存在: {args.resume}")
    elif args.file:
        file_path = (REPO / args.file).resolve()
        if not file_path.is_file():
            raise SystemExit(f"ERROR: 文件不存在: {args.file}")
        txn_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(file_path.stem)[:20]
        state = {
            "transaction_id": txn_id,
            "status": "dedup_check",
            "source": str(file_path.relative_to(REPO)),
            "subproject": args.subproject,
            "source_filename": file_path.name,
            "date_str": "",
            "extract_dir": f"temp/inbox-extract/{txn_id}",
            "retry_count": 0,
            "errors": [],
            "related_to": args.related_to,
            "relation_type": args.relation_type,
        }
    else:
        parser.error("需要 --file 或 --resume")
    if not args.verbose:
        import os
        os.makedirs("temp/inbox-state", exist_ok=True)
        log_path = f"temp/inbox-state/{state['transaction_id']}.log"
        set_progress_file(open(log_path, "a", encoding="utf-8"))
        set_progress_log_path(log_path)
        progress(f"ingest_document.py 日志: {log_path}")
    try:
        state = run_pipeline(state)
    except Exception as exc:
        state["status"] = "failed"
        state["errors"] = [f"未预期异常: {type(exc).__name__}: {exc}"]
        inbox_state.save(state["transaction_id"], state)
    if state["status"] == "completed":
        print(json.dumps({
            "status": "completed",
            "admin_id": state.get("admin_id"),
            "raw_dir": state.get("raw_dir"),
            "raw_locator": state.get("raw_locator"),
            "raw_locator_kind": state.get("raw_locator_kind"),
            "wiki_path": state.get("wiki_path"),
            "graph_report": state.get("graph_report"),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
    elif state["status"] == "duplicate_found":
        print(json.dumps({
            "status": "duplicate_found",
            "dedup_result": state.get("dedup_result"),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
    elif state["status"] == "agent_required":
        print(json.dumps({
            "status": "agent_required",
            "message": "需要 agent 接管：读取 prompt 生成回答，写入 write_to 指定文件，然后调 --resume",
            "prompt": state.get("agent_prompt", ""),
            "write_to": state.get("agent_write_to", ""),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "status": state["status"],
            "errors": state.get("errors", []),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

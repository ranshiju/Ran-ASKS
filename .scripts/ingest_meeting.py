#!/usr/bin/env python3
"""ingest_meeting.py — 代码驱动的会议纪要摄入编排器。

3.3 分两阶段调用 LLM (call_text)：3.3a 撰写 wiki（带 corrected.txt + entity-resolution）→
3.4 校验通过 → 3.3b 抽取语义槽（单轮，只带 wiki）。其余步骤全纯代码。
流程: 3.1 dedup_check → 3.2 preprocess → 3.3a write_wiki → 3.4 validate_wiki →
3.3b write_slots → 3.5 fill_semantics → 3.6 validate_semantics → 落位 →
3.7 update_graph → 3.8 validate_graph → 3.9 finalize_tail
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
from llm_structured import call_text, ingest_mode
from ingest_common import (parse_meta_block, validate_meta, extract_year_from_meta,
                           has_type_mismatch, has_year_mismatch,
                           progress, parse_delimited, set_progress_file, set_progress_log_path)

TEMP_EXTRACT = REPO / "temp" / "inbox-extract"
NON_BLOCKING_ISSUES = ("bare_abbreviation", "descriptive_phrase")
MAX_RETRIES = 3
WIKI_DELIMITER = "<<<WIKI>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"

PIPELINE_PLAN_AGENT = [
    {"step": "判断重复 + 预处理", "needs_agent": False,
     "desc": "dedup(查图+查raw) → speech_entity_resolver 实体纠错(corrected.txt + entity-resolution.json)，一次程序调用完成"},
    {"step": "撰写 wiki 与语义槽", "needs_agent": True,
     "desc": "agent 接管：读 corrected.txt + entity-resolution.json → 撰写会议 wiki → 抽取语义槽，一次输出 <<<WIKI>>> + <<<SLOTS>>>"},
    {"step": "更新 Graph + 校验 + 收尾", "needs_agent": False,
     "desc": "validate→落位→graph_ingest 建边→validate_graph→finalize_tail(log/index/派生同步)+清理，--resume 一次调用完成"},
]

PIPELINE_PLAN_API = [
    {"step": "摄入会议纪要（代码+API 全自动）", "needs_agent": False,
     "desc": "dedup→preprocess→wiki(API)→slots(API)→validate→落位→建图→图校验→收尾+清理，--txt 一条命令完成；agent 仅读最终 JSON 确认 meeting-id/路径/边数"},
]

def pipeline_plan_for(mode: str) -> list[dict]:
    """按摄入后端模式返回对应流水线 plan。"""
    return {"agent": PIPELINE_PLAN_AGENT, "api": PIPELINE_PLAN_API}.get(mode, PIPELINE_PLAN_AGENT)
KNOWN_SECTIONS = {"参会者", "三元组"}


# ===== 工具函数 =====

def run(command: list[str]) -> str:
    return ic.run(command, REPO)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:60] if text else "untitled"


def extract_meeting_date(filename: str) -> str:
    """从文件名提取日期：0723-xxx.txt → 0723；20260723-xxx → 0723；0804.txt → 0804。"""
    stem = Path(filename).stem
    m = re.match(r"(\d{4})?(\d{4})[-_]", stem)
    if m:
        return m.group(2)
    m = re.match(r"(\d{4})[-_]", stem)
    if m:
        return m.group(1)
    # 纯 MMDD 如 "0804" → 返回 0804
    m = re.match(r"^(\d{4})$", stem)
    return m.group(1) if m else ""


def generate_meeting_id(filename: str, title: str) -> str:
    """生成 meeting-id：MMDD-title-slug。"""
    date_part = extract_meeting_date(filename)
    if not date_part:
        date_part = datetime.now().strftime("%m%d")
    slug = slugify(title)[:40] if title else slugify(Path(filename).stem)[:40]
    return f"{date_part}-{slug}"


def _fallback_meeting_title(corrected_text: str, filename: str) -> str:
    """裸日期文件名没有 `#` 标题时，从修正文本首句提取短主题，避免 MMDD-MMDD。"""
    for raw_line in corrected_text.splitlines():
        line = re.sub(r"^\ufeff", "", raw_line).strip()
        if not line or line.startswith("元宝会议助手"):
            continue
        line = re.sub(r"^(本次会议主要讨论了|本次会议主要讨论|会议主要讨论了|会议主要讨论)",
                      "", line).strip("，。；;：: ")
        if line:
            line = re.split(r"[，。；;]", line, 1)[0].strip()
            return line[:60]
    return Path(filename).stem


# 会议纪要存储路径按来源域区分：
#   academic → academic/raw/conferences/<year>/ + academic/wiki/conferences/
#   admin    → admin/raw/meetings/<year>/        + admin/wiki/meetings/
#   business → business/raw/conferences/<year>/  + business/wiki/conferences/
MEETING_DOMAINS = {
    "academic": {"raw_sub": "conferences", "wiki_sub": "conferences", "log": "academic/wiki/log.md", "index": "academic/wiki/index.md"},
    "admin":    {"raw_sub": "meetings",    "wiki_sub": "meetings",    "log": "admin/wiki/log.md",    "index": "admin/wiki/index.md"},
    "business": {"raw_sub": "conferences", "wiki_sub": "conferences", "log": "business/wiki/log.md", "index": "business/wiki/index.md"},
}

def meeting_paths(subproject: str, meeting_id: str, year: str) -> dict:
    """按来源域返回会议的 raw_dir / wiki_path / log / index 路径。"""
    cfg = MEETING_DOMAINS.get(subproject, MEETING_DOMAINS["academic"])
    return {
        "raw_dir": f"{subproject}/raw/{cfg['raw_sub']}/{year}/{meeting_id}",
        "wiki_path": f"{subproject}/wiki/{cfg['wiki_sub']}/{meeting_id}",
        "log": cfg["log"],
        "index": cfg["index"],
    }


def ensure_unique_meeting_id(meeting_id: str, subproject: str = "academic") -> str:
    """冲突自动消歧：加 -2, -3..."""
    base = meeting_id
    cfg = MEETING_DOMAINS.get(subproject, MEETING_DOMAINS["academic"])
    wiki_dir = REPO / subproject / "wiki" / cfg["wiki_sub"]
    n = 1
    while (wiki_dir / f"{meeting_id}.md").exists():
        n += 1
        meeting_id = f"{base}-{n}"
    return meeting_id


# ===== 3.1 dedup_check =====

def step_dedup_check(state: dict) -> tuple[bool, str]:
    """查 graph.db + raw 目录是否已摄入同一会议。"""
    import graph_lib as gl
    date_part = extract_meeting_date(state["source_filename"])
    if not date_part:
        return False, ""
    conn = gl.connect()
    # 查 graph.db: conference-summary 节点 path 含日期
    pattern = f"%{date_part}%"
    rows = conn.execute(
        "SELECT path, title FROM nodes WHERE type='conference-summary' AND path LIKE ?",
        (pattern,),
    ).fetchall()
    conn.close()
    if rows:
        state["dedup_result"] = [{"path": r[0], "title": r[1]} for r in rows]
        state["dedup_title"] = rows[0][1]
        return True, f"已摄入: {rows[0][0]}"
    # 查 raw 目录（按来源域）
    year = datetime.now().strftime("%Y")
    subproject = state.get("subproject", "academic")
    cfg = MEETING_DOMAINS.get(subproject, MEETING_DOMAINS["academic"])
    raw_base = REPO / subproject / "raw" / cfg["raw_sub"] / year
    if raw_base.exists():
        for d in raw_base.iterdir():
            if date_part in d.name:
                state["dedup_result"] = [{"path": str(d.relative_to(REPO))}]
                return True, f"已摄入(raw): {d.name}"
    return False, ""


# ===== 3.2 preprocess =====

def step_preprocess(state: dict) -> tuple[bool, str]:
    """调 speech_entity_resolver.py 做实体纠错。"""
    extract_dir = REPO / state["extract_dir"]
    extract_dir.mkdir(parents=True, exist_ok=True)
    source_path = REPO / state["source"]
    resolution_path = extract_dir / "entity-resolution.json"
    corrected_path = extract_dir / "corrected.txt"
    output = run([sys.executable, str(REPO / ".scripts/speech_entity_resolver.py"),
                  str(source_path),
                  "--output", str(resolution_path.relative_to(REPO)),
                  "--apply", str(corrected_path.relative_to(REPO))])
    if not corrected_path.exists():
        # --apply 失败时回退用原文
        corrected_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
    if resolution_path.exists():
        state["entity_resolution"] = str(resolution_path.relative_to(REPO))
    else:
        state["entity_resolution"] = ""
    state["corrected_path"] = str(corrected_path.relative_to(REPO))
    return True, ""


def build_agent_meeting_wiki_slots_prompt(corrected_text: str, entity_info: str,
                                        meeting_id: str, date_str: str,
                                        sources_path: str, full_date: str, today: str,
                                        errors: list[str] | None = None) -> str:
    """agent 模式专用 prompt：合并 wiki 撰写 + 语义槽抽取为单次任务，省一轮 LLM 调用。"""
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    return f"""你是知识库摄入组件。请一次性完成会议 wiki 页面撰写 + 语义槽抽取。

[会议纪要上下文]
{corrected_text}

[已解析人物映射（复用，不重复提取人名）]
{entity_info}{error_section}

[会议 ID] {meeting_id}
[日期] {full_date}
[sources 路径] {sources_path}（frontmatter sources 字段必须精确使用此值，不得编造或用 memory:// 等占位）
[今日日期] {today}（created、updated 字段使用此值）

[要求]
1. 撰写 conference-summary 类型 wiki 页面，含 frontmatter 和正文。
2. frontmatter 必须包含: title, type: conference-summary, sources（值为上方给定的 sources 路径）, source_type: speech-recognition, date（值为上方给定的日期）, confidence: low, status: current, created（今日日期）, updated（今日日期）。
3. 正文结构: # 标题 → > 日期+参与者行（用 [[authors/路径|姓名]] 格式，复用上方人物映射）→ ## Navigation（2-4 句导航概述）→ ## Content（按议题分子段，- 列表项）。
4. 简写+纠错+去口语化，忠实于原文，不编造。
5. 三元组客体须为规范概念名/实体名：不含逗号、卷号页码、年份或描述性短语；核心词格式统一为「中文英文(缩写)」；无公认缩写则不写括号；无对应中文则只写英文，无对应英文则只写中文。
6. 会议纪要主要用于学术灵感与构思，三元组提取数量和密度宜低：只提取会议明确讨论的核心议题与学术判断，不提取顺带提及的背景知识。
7. 用 <<<WIKI>>> 和 <<<SLOTS>>> 两个分隔符分别包裹输出（先 wiki 后语义槽）。

语义槽格式：
参会者:
<参会者 entity 路径，每行一个，复用上方人物映射>
汇报者:
<人名 entity | 汇报议题，每行一条。议题用规范学术概念名（中文英文缩写），如 cnu-ren-shengquan | 树状采样tree search sampling>
决策:
<决策或学术判断，每行一条。含行政决定与学术判断（实验结论、方法选择、理论判断），如 树状采样表现略优>
待办:
<任务 | 负责人 entity，每行一条。任务名用规范概念名，如 补充Agent对比实验 | cnu-ren-shengquan>
三元组:
<主体|谓词|客体，每行一条>
主体用"本会议"代表这次会议；人物关系直接写人名/entity 路径作主体。
会议→议题 建议谓词: 讨论（核心议题，兜底）/涉及（顺带提及）/规划（行动项/计划）
议题→议题 建议谓词: 涉及（弱相关）/紧密相关于（强相关）
人物→会议 谓词: 参会
人物→人物 谓词: 指导/师从
只使用以上谓词；未列出的谓词不要使用。汇报者、决策、待办是独立 section，不要重复写进三元组。

[输出格式]
<<<META>>>
doc_date: <会议日期，从纪要内容提取；有什么提什么，如 2024-03 或 2024-03-15>
title: <会议标题>
doc_type: meeting
<<</META>>>
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）
<<<SLOTS>>>
（语义槽）"""

# ===== 3.3a write_wiki =====

def _format_resolved_entities(resolution_path: str) -> str:
    """从 entity-resolution.json 提取已 resolved 的人物映射，格式化为提示词片段。"""
    if not resolution_path:
        return "（无 entity-resolution.json，请从纪要正文识别人名）"
    p = REPO / resolution_path
    if not p.exists():
        return "（entity-resolution.json 不存在）"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return "（entity-resolution.json 解析失败）"
    lines = []
    for r in data.get("resolved", []):
        orig = r.get("original", "")
        norm = r.get("normalized", "")
        entity = r.get("entity", "")
        if entity and norm:
            lines.append(f"- {orig} → {entity}（{norm}）")
        elif norm:
            lines.append(f"- {orig} → {norm}")
    return "\n".join(lines) if lines else "（无 resolved 条目）"


def build_meeting_wiki_prompt(corrected_text: str, entity_info: str,
                               meeting_id: str, date_str: str,
                               sources_path: str, full_date: str, today: str,
                               errors: list[str] | None = None) -> str:
    error_section = ""
    if errors:
        error_section = "\n\n[上次输出的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    return f"""你是知识库摄入组件。基于以下会议纪要上下文，撰写会议 wiki 页面。

[会议纪要上下文]
{corrected_text}

[已解析人物映射（复用，不重复提取人名）]
{entity_info}
{error_section}

[会议 ID] {meeting_id}
[日期] {full_date}
[sources 路径] {sources_path}（frontmatter sources 字段必须精确使用此值，不得编造或用 memory:// 等占位）
[今日日期] {today}（created、updated 字段使用此值）

[要求]
1. 撰写 conference-summary 类型 wiki 页面，含 frontmatter 和正文。
2. frontmatter 必须包含: title, type: conference-summary, sources（值为上方给定的 sources 路径）, source_type: speech-recognition, date（值为上方给定的日期）, confidence: low, status: current, created（今日日期）, updated（今日日期）。
3. 正文结构: # 标题 → > 日期+参与者行（用 [[authors/路径|姓名]] 格式，复用上方人物映射）→ ## Navigation（2-4 句导航概述）→ ## Content（按议题分子段，- 列表项）。
4. 简写+纠错+去口语化，忠实于原文，不编造。
5. 输出完整 wiki markdown（含 frontmatter），用 <<<WIKI>>> 分隔符包裹。

[输出格式]
<<<WIKI>>>
（完整 wiki markdown，含 frontmatter）"""


def step_write_wiki(state: dict) -> tuple[bool, str]:
    """3.3a 第一阶段：调 LLM 撰写会议 wiki。"""
    extract_dir = REPO / state["extract_dir"]
    corrected_path = REPO / state["corrected_path"]
    corrected_text = corrected_path.read_text(encoding="utf-8")
    context_text = ic.build_source_context(
        "meeting", corrected_text, force_reduced=(ingest_mode() == "api"))
    entity_info = _format_resolved_entities(state.get("entity_resolution", ""))
    # 生成 meeting-id（仅首次）
    if "meeting_id" not in state:
        title = ""
        m = re.search(r"^#\s+(.+)", corrected_text, re.M)
        if m:
            title = m.group(1).strip()
        if not title:
            title = _fallback_meeting_title(corrected_text, state["source_filename"])
        base_id = generate_meeting_id(state["source_filename"], title)
        subproject = state.get("subproject", "academic")
        meeting_id = ensure_unique_meeting_id(base_id, subproject)
        state["meeting_id"] = meeting_id
        year = datetime.now().strftime("%Y")
        mp = meeting_paths(subproject, meeting_id, year)
        state["raw_dir"] = mp["raw_dir"]
        state["wiki_path"] = mp["wiki_path"]
        state["log_path"] = mp["log"]
        state["index_path"] = mp["index"]
    date_str = state.get("date_str", "")
    today = datetime.now().strftime("%Y-%m-%d")
    year = datetime.now().strftime("%Y")
    if len(date_str) == 4:
        full_date = f"{year}-{date_str[:2]}-{date_str[2:]}"
    else:
        full_date = date_str or today
    sources_path = f"{state['raw_dir']}/{state['source_filename']}"
    errors = state.get("wiki_errors", []) if state.get("wiki_retry", 0) > 0 else None
    is_agent = ingest_mode() == "agent"
    if is_agent:
        prompt = build_agent_meeting_wiki_slots_prompt(context_text, entity_info,
                                        state["meeting_id"], date_str,
                                        sources_path, full_date, today, errors)
    else:
        prompt = build_meeting_wiki_prompt(context_text, entity_info,
                                        state["meeting_id"], date_str,
                                        sources_path, full_date, today, errors)
    result = call_text(prompt, max_tokens=4096, retries=1, operation="ingest_wiki_write",
                       system="你是受程序约束的知识库摄入组件，基于会议纪要撰写 wiki 页面。")
    if result.get("status") == "agent_required":
        state["agent_required"] = True
        state["agent_prompt"] = result.get("prompt", "")
        return False, "需要 agent 接管"
    if not result.get("ok"):
        return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
    text = result.get("text", "")
    # META 交叉校验
    meta = parse_meta_block(text)
    if meta:
        expected_year = datetime.now().strftime("%Y")
        mismatches = validate_meta(meta, {"doc_type": "meeting", "year": expected_year})
        if has_type_mismatch(mismatches):
            state["type_mismatch"] = True
            state["meta_mismatches"] = mismatches
            state["meta_info"] = meta
            return False, f"doc_type 不一致（程序=meeting, LLM={meta.get('doc_type', '')}），跳过待 agent 判断"
        if has_year_mismatch(mismatches):
            # 年份不一致→用 LLM 的年份修正路径（会议可能在往年召开）
            llm_year = extract_year_from_meta(meta)
            if llm_year:
                old_year = expected_year
                mp = meeting_paths(subproject, state["meeting_id"], llm_year)
                state["raw_dir"] = mp["raw_dir"]
                state["wiki_path"] = mp["wiki_path"]
                state["log_path"] = mp["log"]
                state["index_path"] = mp["index"]
                state["meta_year_corrected"] = {"from": old_year, "to": llm_year}
    wiki_content = parse_delimited(text, WIKI_DELIMITER)
    if not wiki_content:
        return False, "LLM 输出缺少 <<<WIKI>>> 段"
    # sources 回填：年份/type 纠正后 raw_dir 已变，用最终路径覆盖（与 ingest_document 一致）
    correct_source = f"{state['raw_dir']}/{state['source_filename']}"
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
    if "conference-summary" not in fm:
        errors.append("type 应为 conference-summary")
    if "memory:" in fm:
        errors.append("sources 不得用 memory:// 占位路径，必须指向 raw/ 下的真实文件")
    if "## Navigation" not in wiki:
        errors.append("缺少 ## Navigation 段")
    if "## Content" not in wiki:
        errors.append("缺少 ## Content 段")
    return errors


# ===== 3.3b write_slots =====

def build_meeting_slots_prompt(wiki_content: str, entity_info: str,
                               errors: list[str] | None = None) -> str:
    error_section = ""
    if errors:
        error_section = "\n\n[上次语义槽的问题（请修正）]\n" + "\n".join(f"- {e}" for e in errors)
    return f"""基于你刚写好的 wiki 页面，为这次会议抽取语义槽。

[已写好的 wiki 页面]
<<<WIKI>>>
{wiki_content}

[已解析人物映射（参会者用这些 entity 路径）]
{entity_info}
{error_section}

[要求]
1. 三元组客体须为规范概念名/实体名：不含逗号、卷号页码、年份或描述性短语；核心词格式统一为「中文英文(缩写)」，如 检索增强生成retrieval-augmented generation(RAG)；无公认缩写则不写括号；无对应中文则只写英文，无对应英文则只写中文。
2. 会议纪要主要用于学术灵感与构思，三元组提取数量和密度宜低：只提取会议明确讨论的核心议题与学术判断，不提取顺带提及的背景知识。
3. 用 <<<SLOTS>>> 分隔符包裹输出语义槽，格式如下：

参会者:
<参会者 entity 路径，每行一个，如 cnu-wu-xi；复用上方人物映射>
汇报者:
<人名 entity | 汇报议题，每行一条。议题用规范学术概念名（中文英文缩写），如 cnu-ren-shengquan | 树状采样tree search sampling>
决策:
<决策或学术判断，每行一条。含行政决定（暂缓写文章）与学术判断（实验结论、方法选择、理论判断），如 树状采样表现略优>
待办:
<任务 | 负责人 entity，每行一条。任务名用规范概念名，如 补充Agent对比实验 | cnu-ren-shengquan>
三元组:
<主体|谓词|客体，每行一条>
主体用"本会议"代表这次会议；人物关系直接写人名/entity 路径作主体。
会议→议题 建议谓词: 讨论（核心议题，兜底）/涉及（顺带提及）/规划（行动项/计划）
议题→议题 建议谓词: 涉及（弱相关）/紧密相关于（强相关，如 Agent框架 | 紧密相关于 | 树状采样）
人物→会议 谓词: 参会
人物→人物 谓词: 指导/师从
只使用以上谓词；未列出的谓词不要使用。
汇报者、决策、待办是独立 section，不要重复写进三元组。

[语义槽填写示例（仅示格式，内容勿照搬）]
参会者:
cnu-wu-xi
cnu-ran-shiju
汇报者:
cnu-ren-shengquan | 对比实验进展
决策:
暂缓写文章优先敲定核心实验图
待办:
补充Agent对比实验 | cnu-ren-shengquan
三元组:
本会议 | 讨论 | 知识库选型knowledge base selection
本会议 | 规划 | 四层自动化方案
cnu-ran-shiju | 指导 | cnu-ren-shengquan

[输出格式]
<<<SLOTS>>>
（语义槽）"""


def step_write_slots(state: dict) -> tuple[bool, str]:
    """3.3b 第二阶段：续接对话，抽语义槽。"""
    # agent 合并模式已产出语义槽 → 跳过 LLM 调用
    if state.get("slots_content"):
        return True, ""
    wiki_content = state.get("wiki_content", "")
    if not wiki_content:
        return False, "无 wiki_content"
    entity_info = _format_resolved_entities(state.get("entity_resolution", ""))
    errors = state.get("slots_errors", []) if state.get("slots_retry", 0) > 0 else None
    prompt = build_meeting_slots_prompt(wiki_content, entity_info, errors)
    result = call_text(prompt, max_tokens=4096, retries=1, operation="ingest_wiki_write",
                       system="你是受程序约束的知识库摄入组件，基于 wiki 页面抽取语义槽。")
    if result.get("status") == "agent_required":
        state["agent_required"] = True
        state["agent_prompt"] = result.get("prompt", "")
        return False, "需要 agent 接管"
    if not result.get("ok"):
        return False, f"LLM 调用失败: {result.get('error', 'unknown')}"
    text = result.get("text", "")
    slots_content = parse_delimited(text, SLOTS_DELIMITER)
    if not slots_content:
        return False, "LLM 输出缺少 <<<SLOTS>>> 段"
    state["slots_content"] = slots_content
    return True, ""


# ===== 3.5 fill_semantics =====

def normalize_slots(text: str) -> str:
    """归一化语义槽格式。"""
    lines = text.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        result.append(stripped)
    return "\n".join(result) + "\n"


# step_fill_semantics → ic.step_fill_semantics(state, REPO, normalize_slots)


# ===== 3.6 validate_semantics =====

def is_clearly_descriptive(obj: str) -> bool:
    if len(obj) <= 8:
        return False
    return bool(re.search(r'[\u3002,\uff0c\uff1b;]', obj))


def _meeting_allowed_predicates() -> set[str]:
    """会议域合法谓词集（含 predicate_tiers.yaml 登记的）。"""
    allowed = {"参会", "讨论", "涉及", "汇报", "规划", "决策",
               "指导", "师从", "受指导于", "待办"}
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
    return ic.validate_semantics(state, REPO, _meeting_allowed_predicates())


def _build_repair_prompt(warnings: list[dict], is_second_pass: bool = False) -> str:
    items = "\n".join(f"- {w['line']} → {w['reason']}" for w in warnings)
    second_note = "\n[注意] 前次修复仍有残留，请更精准地修正。" if is_second_pass else ""
    return f"""请修正以下语义槽客体中的问题，只输出修正后的客体（每行一个，按问题顺序，不输出主体和谓词）：

[问题]
{items}
{second_note}

[要求]
- descriptive_phrase：客体改为规范概念名/实体名，去除逗号、句号等描述性短语；如需拆分长句为多条，每条单独一行
- bare_abbreviation：含英文缩写时写为「中文英文(缩写)」格式，如 直流电流(DC)
- duplicate_line：输出空行表示删除
只输出修正后的客体本身，每行一个，不解释、不带编号。"""


def step_repair_slots(state: dict, warnings: list[dict]) -> tuple[bool, str]:
    """3.6b 局部修复：两级降级链 → agent 兜底（委托 ingest_common）。"""
    return ic.repair_slots(state, REPO, warnings, _build_repair_prompt,
                          step_validate_semantics, non_blocking_issues=NON_BLOCKING_ISSUES)


# ===== 落位（委托 ingest_common）=====

FINALIZE_CONFIG = {
    "doc_id_key": "meeting_id",
    "manifest_files": lambda state: [state["source_filename"], "corrected.txt", "entity-resolution.json"],
    "copy_source": True,
}

FINALIZE_TAIL_CONFIG = {
    "doc_id_key": "meeting_id",
    "get_log_path": lambda state, REPO: REPO / state.get("log_path", "academic/wiki/log.md"),
    "get_index_path": lambda state, REPO: REPO / state.get("index_path", "academic/wiki/index.md"),
    "index_section": "## 会议",
    "entry_prefix": "conferences/",
    "build_log_entry": lambda ctx: (
        "\n## [" + ctx["today"] + "] ingest | ingest_meeting.py 摄入 " + ctx["doc_id"] + "\n"
        "- **来源与归档**：inbox 会议纪要经 speech_entity_resolver 纠错后落位至 `"
        + ctx["state"].get("raw_dir", "") + "/`。\n"
        "- **来源页**：新建 `conferences/" + ctx["page_name"] + ".md`（conference-summary），" + ctx["title"] + "。\n"
        "- **图谱巩固**：增量写入 " + str(ctx["edges"]) + " 条边"
        + ("，catch-all 关键词 " + str(ctx["report"].get("catch_all_keywords_added", 0)) + " 个"
           if ctx["report"].get("catch_all_keywords_added") else "") + "。\n"
        "- **验证**：`ingest_check --graph` PASS（ERROR=0）。\n"
    ),
}


def step_finalize(state: dict) -> tuple[bool, str]:
    return ic.step_finalize(state, REPO, FINALIZE_CONFIG)


def step_update_graph(state: dict) -> tuple[bool, str]:
    return ic.step_update_graph(state, REPO)


def step_validate_graph(state: dict) -> list[str]:
    return ic.step_validate_graph(state, REPO)


def step_finalize_tail(state: dict) -> tuple[bool, str]:
    return ic.step_finalize_tail(state, REPO, FINALIZE_TAIL_CONFIG)

# ===== 主编排循环 =====



MEETING_SPEC = {
    "script_name": "ingest_meeting.py",
    "preprocess_label": "speech_entity_resolver 实体纠错",
    "completion_label_key": "meeting_id",
    "repair_fail_strategy": "handoff",
    "cleanup_after": "validate_graph",
    "rollback_fn": None,
    "finalize_tail_failure": "warn",
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
    return ingest_pipeline.run_pipeline(state, MEETING_SPEC, progress)

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--txt", help="inbox/ 下的会议纪要 .txt 文件路径")
    parser.add_argument("--subproject", default="academic",
                        choices=["academic", "admin", "business"],
                        help="会议来源域（决定存储路径）：academic(默认)/admin/business")
    parser.add_argument("--resume", help="恢复已有事务 ID")
    parser.add_argument("--verbose", action="store_true", help="进度打印到 stdout")
    args = parser.parse_args()
    if args.resume:
        state = inbox_state.load(args.resume)
        if not state:
            raise SystemExit(f"ERROR: 事务不存在: {args.resume}")
    elif args.txt:
        txt_path = (REPO / args.txt).resolve()
        if not txt_path.is_file():
            raise SystemExit(f"ERROR: 文件不存在: {args.txt}")
        txn_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(txt_path.stem)[:20]
        state = {
            "transaction_id": txn_id,
            "status": "dedup_check",
            "source": str(txt_path.relative_to(REPO)),
            "source_filename": txt_path.name,
            "date_str": extract_meeting_date(txt_path.name),
            "subproject": args.subproject,
            "extract_dir": f"temp/inbox-extract/{txn_id}",
            "retry_count": 0,
            "errors": [],
        }
    else:
        parser.error("需要 --txt 或 --resume")
    if not args.verbose:
        import os
        os.makedirs("temp/inbox-state", exist_ok=True)
        log_path = f"temp/inbox-state/{state['transaction_id']}.log"
        set_progress_file(open(log_path, "a", encoding="utf-8"))
        set_progress_log_path(log_path)
        progress(f"ingest_meeting.py 日志: {log_path}")
    try:
        state = run_pipeline(state)
    except Exception as exc:
        state["status"] = "failed"
        state["errors"] = [f"未预期异常: {type(exc).__name__}: {exc}"]
        inbox_state.save(state["transaction_id"], state)
    if state["status"] == "completed":
        print(json.dumps({
            "status": "completed",
            "meeting_id": state.get("meeting_id"),
            "raw_dir": state.get("raw_dir"),
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
            "message": "INGEST_BACKEND=agent，需要 agent 接管 wiki 撰写",
            "prompt": state.get("agent_prompt", ""),
            "pipeline_plan": pipeline_plan_for(ingest_mode()),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps({
            "status": state["status"],
            "errors": state.get("errors", []),
            "transaction_id": state["transaction_id"],
        }, ensure_ascii=False, indent=2))
        log_path = ic.get_progress_log_path()
        if log_path:
            print(f"日志: {log_path}", file=sys.stderr)


if __name__ == "__main__":
    main()

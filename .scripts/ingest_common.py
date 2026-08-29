#!/usr/bin/env python3
"""摄入流程公共组件：三类文档摄入（论文/会议/通用文档）复用。

核心职责：
- 进度日志（progress）、子进程封装（run）、文本解析（parse_delimited / parse_check_errors）
- 语义槽校验+修复（validate_semantics / repair_slots / stop_for_semantic_errors）
- 交接与安全网（handoff_to_agent / validate_before_commit）
- 共享步骤（step_fill_semantics / step_update_graph / step_validate_graph /
  step_finalize / step_finalize_tail），各脚本通过 config 注入差异

差异点由各脚本传入 config（谓词集、修复 prompt builder、page_type、finalize 配置等）。"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from llm_structured import call_text


# ===== META 块解析与校验（LLM 读全文时的元信息交叉校验）=====

META_DELIMITER = "<<<META>>>"
META_END = "<<</META>>>"


def parse_meta_block(text: str) -> dict:
    """从 LLM 输出中提取 META 块，返回 meta_dict。

    META 格式（放在 <<<WIKI>>> 之前）：
    <<<META>>>
    doc_date: 2024-03
    title: Will We Run Out of Data?
    doc_type: paper
    <<</META>>>

    无闭合标签时截到下一个 <<<WIKI>>> / <<<SLOTS>>> 之前。
    """
    if META_DELIMITER not in text:
        return {}
    start = text.index(META_DELIMITER) + len(META_DELIMITER)
    rest = text[start:]
    end_markers = [META_END, "<<<WIKI>>>", "<<<SLOTS>>>"]
    end_idx = len(rest)
    for marker in end_markers:
        pos = rest.find(marker)
        if pos != -1 and pos < end_idx:
            end_idx = pos
    meta_text = rest[:end_idx].strip()
    meta = {}
    for line in meta_text.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def validate_meta(meta: dict, expected: dict) -> list[str]:
    """交叉校验 META 与程序推导值。返回不一致列表。

    expected 支持：
    - doc_type: 程序分类的文档类型（paper/meeting/document）
    - year: 程序从 ID/arxiv 推导的年份
    - date: 程序从文件名推导的日期（MMDD 或 YYYYMMDD）
    """
    mismatches = []
    # doc_type 校验
    expected_type = expected.get("doc_type", "")
    meta_type = meta.get("doc_type", "").lower().strip()
    if expected_type and meta_type and meta_type != expected_type:
        mismatches.append(f"doc_type: 程序={expected_type}, LLM={meta_type}")
    # 年份校验（从 doc_date 提取 4 位年份）
    expected_year = expected.get("year", "")
    meta_date = meta.get("doc_date", "")
    m = re.match(r"(\d{4})", meta_date)
    meta_year = m.group(1) if m else ""
    if expected_year and meta_year and meta_year != expected_year:
        mismatches.append(f"year: 程序={expected_year}, LLM={meta_year}")
    return mismatches


def extract_year_from_meta(meta: dict) -> str:
    """从 META 的 doc_date 提取 4 位年份。"""
    m = re.match(r"(\d{4})", meta.get("doc_date", ""))
    return m.group(1) if m else ""


def has_type_mismatch(mismatches: list[str]) -> bool:
    """检查不一致列表中是否包含 doc_type 不一致。"""
    return any(m.startswith("doc_type:") for m in mismatches)


def has_year_mismatch(mismatches: list[str]) -> bool:
    """检查不一致列表中是否包含 year 不一致。"""
    return any(m.startswith("year:") for m in mismatches)


# ===== 文档类型上下文注册表（统一内核 + 类型薄适配）=====

# 每种 source_kind 只声明自己的上下文预算与提取策略；
# LLM 调用、推理熔断、字段校验和输出清理仍在共享层统一执行。
CONTEXT_PROFILES = {
    "paper": {
        "full_text_max_chars": 40_000,
        "reduced_context_max_chars": 22_000,
        "section_char_cap": 6_000,
        "fallback_head_cap": 9_000,
        "fallback_tail_cap": 9_000,
        "sections": ("abstract", "introduction", "method", "results",
                     "discussion", "conclusions"),
        "extractor": "read_paper",
    },
    "document": {
        "full_text_max_chars": 60_000,
        "reduced_context_max_chars": 30_000,
        "section_char_cap": 8_000,
        "fallback_head_cap": 12_000,
        "fallback_tail_cap": 12_000,
        "sections": (
            "abstract", "摘要", "introduction", "简介", "background", "背景",
            "method", "方法", "approach", "方案", "results", "结果",
            "discussion", "讨论", "conclusion", "conclusions", "结论",
            "summary", "总结", "appendix", "附录",
        ),
        "extractor": "markdown_headings",
    },
    "meeting": {
        "full_text_max_chars": 20_000,
        "reduced_context_max_chars": 10_000,
        "section_char_cap": 4_000,
        "fallback_head_cap": 6_000,
        "fallback_tail_cap": 6_000,
        "sections": (),
        "extractor": "head_tail",
    },
}


def context_profile(kind: str) -> dict:
    """按 source_kind 返回上下文预算 profile；未知类型回退到通用文档。"""
    return CONTEXT_PROFILES.get(kind, CONTEXT_PROFILES["document"])


def _clip_context_section(content: str, cap: int) -> str:
    """超长 section 保留头部为主、尾部兜底，避免单段吞掉预算。"""
    content = content.strip()
    if len(content) <= cap:
        return content
    head = content[: int(cap * 0.8)]
    tail = content[-int(cap * 0.2):]
    return f"{head}\n\n[...中段省略...]\n\n{tail}"


def _assemble_reduced_context(profile: dict, sections: list[tuple[str, str]]) -> str:
    """把已选 section 组装成受预算约束的定向摘要。"""
    budget = profile["reduced_context_max_chars"]
    parts: list[str] = []
    for title, content in sections:
        cap = min(profile["section_char_cap"], max(1_500, budget))
        part = f"--- [{title}] ---\n{_clip_context_section(content, cap)}"
        parts.append(part)
        budget -= len(part)
        if budget <= 0:
            break
    return "\n\n".join(parts)


def _fallback_head_tail_context(profile: dict, text: str) -> str:
    """无可用结构提取时，程序确定性地截取头部与尾部。"""
    head_cap = min(profile["fallback_head_cap"], len(text) // 2)
    tail_cap = min(profile["fallback_tail_cap"], len(text) - head_cap)
    head = text[:head_cap].strip()
    tail = text[-tail_cap:].strip() if tail_cap > 0 else ""
    return f"--- [fallback: 头部截取] ---\n{head}\n\n--- [fallback: 尾部截取] ---\n{tail}"


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切分文档；标题前内容记为「前置」。"""
    sections: list[tuple[str, str]] = []
    current_title = "前置"
    current_lines: list[str] = []

    def flush() -> None:
        content = "\n".join(current_lines).strip()
        if content:
            sections.append((current_title, content))

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})[ \t]+(.+?)\s*$", line)
        if m:
            flush()
            current_title = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    flush()
    return sections


def _heading_matches(title: str, requested: tuple[str, ...]) -> bool:
    """忽略空白与大小写，按别名匹配标题。"""
    normalized = re.sub(r"\s+", "", title).lower()
    for alias in requested:
        alias_norm = re.sub(r"\s+", "", alias).lower()
        if alias_norm and alias_norm in normalized:
            return True
    return False


def _extract_markdown_sections(text: str, requested: tuple[str, ...]) -> list[tuple[str, str]]:
    """从 Markdown 文档中程序选择关键标题段；选不到则返回空列表。"""
    sections = _split_markdown_sections(text)
    if not sections:
        return []
    selected = [section for section in sections if _heading_matches(section[0], requested)]
    if not selected:
        return []
    return selected


def build_source_context(kind: str, text: str, *, source_path: Path | str | None = None,
                         force_reduced: bool = False) -> str:
    """统一构造喂给 LLM 的原文上下文。

    - 普通文本未超过类型阈值时保留全文，避免无谓改写。
    - 超长或 API/弱模型路径强制降为定向 section/头部尾部摘要。
    - 论文仍用 read_paper 的专业 section 匹配；普通文档用 Markdown 标题匹配；
      会议纪要用头部尾部截取。
    """
    profile = context_profile(kind)
    if not force_reduced and len(text) <= profile["full_text_max_chars"]:
        return text

    if kind == "paper" and source_path is not None:
        try:
            import read_paper
            paper_path = Path(source_path)
            hits, _misses, _total = read_paper.extract_sections(
                paper_path, list(profile["sections"]))
            if hits:
                sections = [(title, content) for _req, title, content in hits]
                return _assemble_reduced_context(profile, sections)
        except Exception:
            pass

    if kind == "document":
        sections = _extract_markdown_sections(text, profile["sections"])
        if sections:
            return _assemble_reduced_context(profile, sections)

    return _fallback_head_tail_context(profile, text)



def is_blocking_warning(w: dict, non_blocking_issues: tuple[str, ...] = ()) -> bool:
    """区分阻断型与非阻断型 warning。

    非阻断型由后置机制兜底（keyword_dedup / sync_keyword_aliases / resolve_bare_name），
    不进 3.6b LLM 修复；阻断型必须修复才能写图。"""
    if w.get("issue") in non_blocking_issues:
        return False
    return True


def is_malformed_predicate(pred: str) -> bool:
    """谓词格式非法：空串、含空白或标点，属于结构性硬错误。"""
    if not pred or pred != pred.strip():
        return True
    return bool(re.search(r'[\s,，。；;、]', pred))


def validate_semantics(state: dict, REPO: Path, allowed_predicates: set[str],
                        non_blocking_issues: tuple[str, ...] = ()) -> tuple[list[str], list[dict]]:
    """校验语义槽合法性。返回 (hard_errors, slot_warnings)。

    hard_errors: 结构性错误（谓词非法、解析失败、同行 header），需早停交接。
    slot_warnings: 客体内容问题（描述性短语、裸缩写、重复行），可走局部修复。
    """
    import graph_ingest
    from graph_ingest import is_bare_abbreviation, is_descriptive_phrase
    semantic_path = REPO / state["semantic_path"]
    sem_text = semantic_path.read_text(encoding="utf-8")
    # 裸缩写三段式第二步: alias 未命中时从 raw 全文查全称,自动补全为 full(ABBR) 格式
    # 论文管道(step_validate_semantics)已有等价逻辑;此处使文档/会议管道共享同一消解能力
    _raw_abbr_map = load_raw_abbr_map(state.get("wiki_path", ""))
    if _raw_abbr_map:
        _patched = autofix_bare_abbreviations(sem_text, _raw_abbr_map)
        if _patched != sem_text:
            semantic_path.write_text(_patched, encoding="utf-8")
            sem_text = _patched
            state["slots_content"] = _patched
    hard_errors: list[str] = []
    slot_warnings: list[dict] = []
    predicate_candidates: list[dict] = []
    # 格式检查（同行 header）→ 硬错误
    try:
        warns = graph_ingest.detect_inline_section_headers(sem_text)
        hard_errors.extend(warns)
    except Exception:
        pass
    try:
        page_path = state["wiki_path"]
        triples, keywords, *_ = graph_ingest.parse_semantic_text(sem_text, page_path)
        # 谓词校验
        for t in triples:
            pred = t.get("predicate", "")
            obj = t.get("object", "").strip()
            if pred:
                if is_malformed_predicate(pred):
                    hard_errors.append(f"谓词格式不合法: {pred} (客体={obj})")
                elif pred not in allowed_predicates:
                    # 格式正常但未登记：按候选谓词保留，不阻断摄入；由谓词治理或后续登记收敛
                    predicate_candidates.append({
                        "predicate": pred,
                        "subject": t.get("subject", ""),
                        "object": obj,
                    })
            if obj and is_descriptive_phrase(obj):
                slot_warnings.append({
                    "section": "三元组", "line": f"{t.get('subject','')}|{pred}|{obj}",
                    "issue": "descriptive_phrase",
                    "reason": "客体含逗号/句号等标点，应为规范概念名",
                })
            if is_bare_abbreviation(obj):
                slot_warnings.append({
                    "section": "三元组", "line": f"{t.get('subject','')}|{pred}|{obj}",
                    "issue": "bare_abbreviation",
                    "reason": "含英文缩写但未放入括号，应为「中文英文(缩写)」格式",
                })
        # 重复三元组检测
        seen = set()
        for t in triples:
            key = (t.get("subject", ""), t.get("predicate", ""), t.get("object", "").strip())
            if key in seen:
                slot_warnings.append({
                    "section": "三元组", "line": f"{key[0]} | {key[1]} | {key[2]}",
                    "issue": "duplicate_line", "reason": "重复三元组，应删除重复行",
                })
            else:
                seen.add(key)
    except Exception as exc:
        hard_errors.append(f"语义槽解析失败: {exc}")
    if predicate_candidates:
        state["predicate_candidates"] = predicate_candidates
    return hard_errors, slot_warnings


def patch_semantic_lines(sem_text: str, repaired_text: str, warnings: list[dict]) -> str | None:
    """用 LLM 修复输出替换语义槽中有问题的客体。

    旧实现按 warning 的 line 全串（subject|predicate|object）匹配语义槽行，
    但语义槽各 section 格式不同（决策=纯客体、待办=客体|主体、三元组用代词），
    永远匹配不上导致 patch 不生效。改为按客体文本定位并替换客体部分。
    """
    # 从 warning 的 line（subject|predicate|object）提取旧客体（最后一个 | 后的部分）
    old_objects = []
    for w in warnings:
        parts = w["line"].rsplit("|", 1)
        old_objects.append(parts[-1].strip() if len(parts) > 1 else w["line"].strip())
    # 从 LLM 输出提取新客体（取每行最后一个 | 后的部分，兼容 主体|谓词|客体 和纯客体格式）
    new_objects = []
    for line in repaired_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit("|", 1)
        new_objects.append(parts[-1].strip() if len(parts) > 1 else line)
    if not old_objects or len(old_objects) != len(new_objects):
        return None  # 数量不匹配，无法安全 patch
    obj_map = dict(zip(old_objects, new_objects))
    # 按长度降序匹配，避免短客体误匹配长客体子串
    sorted_old = sorted(obj_map.keys(), key=len, reverse=True)
    lines = sem_text.splitlines()
    result = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append(line)
            continue
        replaced = False
        for old_obj in sorted_old:
            if old_obj and old_obj in stripped:
                result.append(stripped.replace(old_obj, obj_map[old_obj]))
                replaced = True
                break
        if not replaced:
            result.append(line)
    return "\n".join(result) + "\n"



def repair_once(state: dict, REPO: Path, warnings: list[dict],
                build_repair_prompt, validate_fn, non_blocking_issues: tuple[str, ...] = (),
                operation: str = "ingest_semantic_fill",
                is_second_pass: bool = False) -> tuple[bool, list[dict]]:
    """单次修复 + 复验。返回 (ok, residual_warnings)。"""
    semantic_path = REPO / state["semantic_path"]
    sem_text = semantic_path.read_text(encoding="utf-8")
    prompt = build_repair_prompt(warnings, is_second_pass)
    result = call_text(prompt, max_tokens=600, retries=1, operation=operation,
                       system="你是语义槽修复组件，只输出修正后的行，不解释。")
    if result.get("status") == "agent_required":
        state["agent_required"] = True
        state["agent_prompt"] = result.get("prompt", "")
        return False, warnings
    if not result.get("ok"):
        return False, warnings
    repaired_text = result.get("text", "").strip()
    if not repaired_text:
        return False, warnings
    new_sem = patch_semantic_lines(sem_text, repaired_text, warnings)
    if new_sem is None:
        return False, warnings
    semantic_path.write_text(new_sem, encoding="utf-8")
    state["slots_content"] = new_sem
    _, residual = validate_fn(state)
    residual = [w for w in residual if is_blocking_warning(w, non_blocking_issues)]
    return (len(residual) == 0), residual


def repair_slots(state: dict, REPO: Path, warnings: list[dict],
                 build_repair_prompt, validate_fn, non_blocking_issues: tuple[str, ...] = ()
                 ) -> tuple[bool, str]:
    """两级局部修复：DeepSeek → GLM → agent 兜底。"""
    repaired, residual = repair_once(state, REPO, warnings, build_repair_prompt,
                                     validate_fn, non_blocking_issues,
                                     operation="ingest_semantic_fill")
    if repaired:
        return True, ""
    if residual:
        repaired2, residual2 = repair_once(state, REPO, residual, build_repair_prompt,
                                           validate_fn, non_blocking_issues,
                                           operation="ingest_semantic_repair",
                                           is_second_pass=True)
        if repaired2:
            return True, ""
        if residual2:
            state["agent_required"] = True
            return False, "局部修复失败，需 agent 兜底"
    return False, "修复失败"


def stop_for_semantic_errors(state: dict, errors: list[str], resume_cmd: str,
                             warnings: list[dict] | None = None) -> None:
    """结构错误不盲重试；保留可用 wiki/语义文件，交接受控修正后 --resume。

    warnings 为同时发现的阻断型 warning，一并写入 agent_prompt，避免修完硬错误后
    才在 resume 复验中暴露 warning，造成二次人工介入。"""
    state["status"] = "agent_required"
    state["errors"] = errors
    state["agent_required"] = True
    lines = (
        "语义槽存在无法自动修复的结构错误，已停止重复生成以保留已通过的 wiki。\n"
        + "\n".join(f"- {error}" for error in errors)
    )
    if warnings:
        lines += "\n\n同时存在以下阻断型 warning（请一并修正）：\n"
        lines += "\n".join(f"- [{w.get('issue', 'warning')}] {w.get('line', '')}（{w.get('reason', '')}）" for w in warnings)
    lines += f"\n\n请修正 `{state.get('semantic_path', '')}` 后运行 `{resume_cmd}`。"
    state["agent_prompt"] = lines


def handoff_to_agent(state: dict, context_msg: str, validate_fn,
                     resume_cmd: str, validate_cmd: str = "") -> None:
    """交接受控修正：跑全量语义校验，把当前所有 warning 写入 agent_prompt。"""
    # 记录 handoff 前阶段，供 resume 恢复（落位后 handoff 不应重跑落位）
    if state.get("status") != "agent_required":
        state["pre_handoff_status"] = state.get("status")
    state["status"] = "agent_required"
    state["agent_required"] = True
    state["errors"] = []
    lines: list[str] = []
    try:
        sem_hard, slot_warnings = validate_fn(state)
    except Exception as exc:
        sem_hard, slot_warnings = [f"语义槽校验失败: {exc}"], []
    if sem_hard:
        lines.extend(f"- [hard] {e}" for e in sem_hard)
    for w in slot_warnings:
        lines.append(f"- [{w['issue']}] {w['line']}（{w['reason']}）")
    tail = f"\n\n请手动修正 `{state.get('semantic_path', '')}` 后运行 `{resume_cmd}`。"
    if validate_cmd:
        tail += f" 修正后可先用 `{validate_cmd}` 自检。"
    state["agent_prompt"] = f"{context_msg}（当前共 {len(lines)} 项待修）。\n" + "\n".join(lines) + tail


def validate_before_commit(state: dict, validate_fn, non_blocking_issues: tuple[str, ...] = ()
                          ) -> list[str]:
    """落位前全量复验：resume 安全网，防止跳过校验直接写图。"""
    try:
        sem_hard, slot_warnings = validate_fn(state)
    except Exception as exc:
        return [f"恢复前语义槽校验失败: {exc}"]
    errors = list(sem_hard)
    blocking = [w for w in slot_warnings if is_blocking_warning(w, non_blocking_issues)]
    if blocking:
        errors.append(f"仍有 {len(blocking)} 个阻断型 warning")
    return errors


# ===== 进度日志（三类摄入共用）=====

_progress_state = threading.local()


def set_progress_file(f) -> None:
    _progress_state.file = f


def set_progress_log_path(p) -> None:
    _progress_state.log_path = p


def close_progress_file() -> None:
    progress_file = getattr(_progress_state, "file", None)
    if progress_file:
        progress_file.close()
        _progress_state.file = None


def get_progress_log_path() -> str | None:
    return getattr(_progress_state, "log_path", None)


def progress(*args, **kwargs) -> None:
    """进度输出：quiet 模式写日志文件，--verbose 时打印到 stdout（实时 flush）。"""
    progress_file = getattr(_progress_state, "file", None)
    if progress_file:
        end = kwargs.pop("end", "\n")
        kwargs.pop("flush", None)
        msg = " ".join(str(a) for a in args) + end
        progress_file.write(msg)
        progress_file.flush()
    else:
        kwargs["flush"] = True
        print(*args, **kwargs)


# ===== 子进程封装 =====

def run(command: list[str], REPO: Path) -> str:
    """运行子进程，失败抛 RuntimeError。stdout/stderr 实时打印，返回 stdout。"""
    return run_tracked(command, REPO)


def run_tracked(command: list[str], REPO: Path, state: dict | None = None,
                label: str | None = None) -> str:
    """运行子进程，失败抛 RuntimeError。stdout/stderr 实时打印，返回 stdout。

    P0 遥测：传入 state+label 时记录 returncode/duration，便于事后定位
    「子进程退出码被静默吞掉」的问题。"""
    start = time.monotonic()
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    duration_ms = int((time.monotonic() - start) * 1000)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if state is not None and label:
        record_subprocess(state, label, command, result.returncode, duration_ms)
    if result.returncode:
        raise RuntimeError(f"命令失败({result.returncode}): {' '.join(command)}")
    return result.stdout


def _ensure_telemetry(state: dict) -> dict:
    telemetry = state.setdefault("telemetry", {})
    telemetry.setdefault("llm_calls", {})
    telemetry.setdefault("subprocesses", {})
    return telemetry


def record_llm_call(state: dict, stage: str) -> None:
    """记录 LLM 调用次数（按 stage 累计），供复盘与成本对账。"""
    telemetry = _ensure_telemetry(state)
    calls = telemetry["llm_calls"]
    calls[stage] = calls.get(stage, 0) + 1
    telemetry["llm_calls_total"] = sum(calls.values())


def record_subprocess(state: dict, label: str, command: list[str],
                      returncode: int, duration_ms: int) -> None:
    """记录子进程调用结果（returncode/duration），不替代 run 的异常传播。"""
    telemetry = _ensure_telemetry(state)
    telemetry["subprocesses"][label] = {
        "returncode": returncode,
        "duration_ms": duration_ms,
        "command": " ".join(str(part) for part in command),
    }


def validate_completion(state: dict, REPO: Path) -> list[str]:
    """P0 完成判定：语义槽、graph_report、图产出、历史错误均须干净。

    返回阻断错误列表；空列表表示可标记 completed。
    - 历史错误未清空：防止 agent 修复成功但 errors 残留的假完成。
    - 语义槽缺失/空：防止缺少 SLOTS 段仍 completed。
    - graph_report 未解析/空跑：防止写图子进程输出异常却被静默吞掉。
    """
    errors: list[str] = []
    if state.get("errors"):
        errors.append("完成时仍有未清空错误: " + "; ".join(str(e) for e in state["errors"]))
    semantic_path = state.get("semantic_path")
    if not semantic_path:
        errors.append("语义槽路径缺失，不得标记 completed")
    else:
        sem_file = REPO / semantic_path
        if not sem_file.is_file() or not sem_file.read_text(encoding="utf-8").strip():
            errors.append("语义槽缺失或为空，不得标记 completed")
    report = state.get("graph_report")
    if not isinstance(report, dict) or "edges_added" not in report:
        errors.append("graph_report 缺失或未解析，不得标记 completed")
    else:
        if report.get("edges_added", 0) == 0 and report.get("dup_skipped", 0) == 0 \
                and report.get("nodes_created", 0) == 0:
            errors.append("图摄入未产生边或节点（graph_report 空跑），不得标记 completed")
    parse = (state.get("telemetry") or {}).get("graph_report_parse")
    if parse and parse.get("status") != "parsed":
        errors.append("graph_report 解析未成功，不得标记 completed")
    return errors


# ===== 文本解析 =====

WIKI_DELIMITER = "<<<WIKI>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"
CLOSING_DELIMITERS = {
    WIKI_DELIMITER: "<<</WIKI>>>",
    SLOTS_DELIMITER: "<<</SLOTS>>>",
}


def parse_delimited(text: str, delimiter: str) -> str:
    """从文本中提取分隔符包裹的内容。

    优先截到规范闭标记 `<<</WIKI>>>` / `<<</SLOTS>>>`；兼容旧的同标记闭合
    （agent 模式同一分隔符既作开标记也作闭标记）以及下一个其他开标记。
    """
    if delimiter not in text:
        return ""
    after = text.split(delimiter, 1)[1]
    closing = CLOSING_DELIMITERS.get(delimiter)
    if closing and closing in after:
        after = after.split(closing, 1)[0]
    elif delimiter in after:
        after = after.split(delimiter, 1)[0]
    else:
        for d in (WIKI_DELIMITER, SLOTS_DELIMITER):
            if d != delimiter and d in after:
                after = after.split(d, 1)[0]
    return after.strip()

def parse_check_errors(output: str) -> list[str]:
    """从 ingest_check.py 输出中提取 ERROR 行（跳过汇总行 ERROR=0）。"""
    errors = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ERROR") and not stripped.startswith("ERROR="):
            errors.append(stripped)
    return errors


# ===== 共享步骤 =====

NO_INFO_SLOT_VALUES = {
    "无明确期刊", "无明确信息", "无明确作者", "无明确日期",
    "无明确机构", "无明确研究对象", "无明确局限性",
}


def remove_no_info_slot_values(text: str) -> str:
    """删除语义槽中「（无明确...）」等占位值，只保留空 section 给后续校验兜底。"""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in NO_INFO_SLOT_VALUES:
            continue
        if (stripped.startswith("（") and stripped.endswith("）")
                and "无明确" in stripped):
            continue
        kept.append(line)
    return "\n".join(kept) + ("\n" if text.endswith("\n") and kept else "")


def step_fill_semantics(state: dict, REPO: Path, normalize_fn) -> tuple[bool, str]:
    """归一化语义槽格式，写 semantic 文件。normalize_fn 为各脚本的 normalize_slots。"""
    slots_content = state.get("slots_content", "")
    if not slots_content:
        return False, "无语义槽内容"
    normalized = normalize_fn(slots_content)
    normalized = remove_no_info_slot_values(normalized)
    semantic_path = REPO / "temp" / "inbox-state" / f"{state['transaction_id']}-semantic.txt"
    semantic_path.parent.mkdir(parents=True, exist_ok=True)
    semantic_path.write_text(normalized, encoding="utf-8")
    state["semantic_path"] = str(semantic_path.relative_to(REPO))
    return True, ""


def step_update_graph(state: dict, REPO: Path, clean: bool = False) -> tuple[bool, str]:
    """调 graph_ingest.py ingest --semantic 写图边。clean=True 时先清旧边（re-ingest）。"""
    cmd = [sys.executable, str(REPO / ".scripts/graph_ingest.py"), "ingest",
           "--page", state["wiki_path"], "--semantic", state["semantic_path"]]
    if clean:
        cmd.append("--clean")
    telemetry = _ensure_telemetry(state)
    try:
        output = run_tracked(cmd, REPO, state=state, label="graph_ingest")
    except RuntimeError as exc:
        telemetry["graph_report_parse"] = {"status": "failed", "error": str(exc)}
        state["graph_report"] = None
        return False, f"graph_ingest 子进程失败: {exc}"
    try:
        state["graph_report"] = json.loads(output)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}\s*$', output)
        if m:
            state["graph_report"] = json.loads(m.group(0))
    if not isinstance(state.get("graph_report"), dict) or "edges_added" not in state["graph_report"]:
        telemetry["graph_report_parse"] = {
            "status": "failed",
            "error": "graph_ingest 输出不含 JSON 报告或缺少 edges_added",
            "output_prefix": output[:500],
        }
        state["graph_report"] = None
        return False, "graph_ingest 输出不是 JSON 报告或缺少 edges_added"
    telemetry["graph_report_parse"] = {"status": "parsed"}
    # 持久化裸缩写 warning 到 abbreviation-todo.jsonl，供后置补全
    _record_abbreviation_warnings(state, REPO)
    return True, ""


def _record_abbreviation_warnings(state: dict, REPO: Path) -> None:
    """将 graph_report 中的 bare_abbreviation warning 追加到
    cross-domain/abbreviation-todo.jsonl，供后置 alias 补全参考。

    事务内幂等（state flag 防重复）。re-ingest（clean）时跳过——旧记录
    仍有效，新摄入的 warning 覆盖不了旧行，去重交消费者处理。
    """
    if state.get("abbreviation_warnings_recorded"):
        return
    report = state.get("graph_report")
    if not isinstance(report, dict):
        return
    warns = report.get("descriptive_warnings", [])
    bare = [w for w in warns if w.get("issue") == "bare_abbreviation"]
    if not bare:
        state["abbreviation_warnings_recorded"] = True
        return
    todo_path = REPO / "cross-domain" / "abbreviation-todo.jsonl"
    todo_path.parent.mkdir(parents=True, exist_ok=True)
    txn = state.get("transaction_id", "")
    page = state.get("wiki_path", "")
    doc_id = state.get("paper_id") or state.get("meeting_id") or ""
    with todo_path.open("a", encoding="utf-8") as handle:
        for w in bare:
            handle.write(json.dumps({
                "transaction_id": txn,
                "doc_id": doc_id,
                "page": page,
                "predicate": w.get("predicate", ""),
                "object": w.get("object", ""),
                "field": w.get("field", "object"),
            }, ensure_ascii=False) + "\n")
    state["abbreviation_warnings_recorded"] = True


def link_raw_relation(state: dict, REPO: Path, target_page: str, relation_type: str) -> None:
    """把本事务的 raw 节点关联到已有页面的 raw 节点（版本/补充材料关系）。

    relation_type: "version" | "supplementary" | "translation"
    只建 raw 间关系边；wiki 页面由管线 finalize 步骤创建，sources 追加由 append_source_to_page 完成。
    """
    import graph_lib as gl
    new_fm = gl.read_frontmatter(state["wiki_path"])
    new_sources = gl.parse_list_field(new_fm, "sources")
    new_raw = _derive_raw_path(new_sources[0], state["wiki_path"]) if new_sources else ""
    target_fm = gl.read_frontmatter(target_page)
    target_sources = gl.parse_list_field(target_fm, "sources")
    target_raw = _derive_raw_path(target_sources[0], target_page) if target_sources else ""
    if not new_raw or not target_raw:
        return
    if relation_type == "version":
        subj, pred, obj = target_raw, "后一版本", new_raw
    elif relation_type == "supplementary":
        subj, pred, obj = target_raw, "补充材料", new_raw
    elif relation_type == "translation":
        subj, pred, obj = new_raw, "译自", target_raw
    else:
        return
    conn = gl.connect()
    for raw_path in (subj, obj):
        if not gl.node_exists(conn, raw_path):
            gl.ensure_node(conn, raw_path, Path(raw_path).name or raw_path, "raw", "", "", "current", 0)
    existing = conn.execute(
        "SELECT id FROM edges WHERE subject=? AND predicate=? AND object=?",
        (subj, pred, obj),
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
            "VALUES (?,?,?,?,?,?)",
            (subj, pred, obj, "[可追溯]", "", 0),
        )
    conn.commit()
    conn.close()


def append_source_to_page(REPO: Path, page_path: str, new_source: str) -> bool:
    """在 wiki 页 frontmatter 的 sources 列表末尾追加一条来源（幂等）。

    用正则定位 sources: YAML 块，不依赖其他字段的固定位置。
    返回 True 表示已追加，False 表示已存在或页面缺失。
    """
    target_file = REPO / (page_path.removesuffix(".md") + ".md")
    if not target_file.exists():
        return False
    t_text = target_file.read_text(encoding="utf-8")
    source_line = f'  - "{new_source}"'
    if f'"{new_source}"' in t_text:
        return False
    fm_match = re.match(r'^(---\n)(.*?)(\n---\n)', t_text, re.S)
    if not fm_match:
        return False
    fm_body = fm_match.group(2)
    sources_match = re.search(r'(sources:\n)((?:  - .*\n)*)', fm_body)
    if sources_match:
        insert_at = sources_match.end()
        new_fm = fm_body[:insert_at] + source_line + '\n' + fm_body[insert_at:]
    else:
        new_fm = fm_body.rstrip('\n') + '\nsources:\n' + source_line + '\n'
    target_file.write_text(
        t_text[:fm_match.start(2)] + new_fm + t_text[fm_match.end(2):],
        encoding="utf-8")
    return True


def _derive_raw_path(source_field: str, page_path: str = "") -> str:
    """从 sources 字段推导 raw 节点路径。"""
    if not source_field:
        return ""
    import graph_lib as gl
    return gl.raw_node_path(source_field, page_path)


def load_raw_abbr_map(page_path: str) -> dict:
    """从 raw paper.md 提取缩写->全称映射（alias -> raw 查找的第二步）。

    两路合并:
    1. extract_abbreviations.extract_for_page（严格首字母验证,高精度）
    2. 宽松正则扫描（允许小写开头/含逗号连字符的全称,补严格模式遗漏）
    频率过滤: ABBR 在原文出现 >=2 次才纳入。返回 {ABBR: full_name}。
    """
    abbr_map = {}
    try:
        from extract_abbreviations import extract_for_page
        pairs, _ = extract_for_page(page_path)
        for abbr, full_info in pairs:
            full_val = full_info[0] if isinstance(full_info, (tuple, list)) else full_info
            if full_val:
                abbr_map[abbr] = full_val
    except Exception:
        pass
    try:
        from extract_abbreviations import find_raw
        raw_path = find_raw(page_path)
        if not raw_path:
            return abbr_map
        raw_text = Path(raw_path).read_text(encoding="utf-8", errors="ignore")
        # 宽松模式1: full name (ABBR)
        for m in re.finditer(r'([\w][\w\s,\-]{2,40}?)\s*[\uff08(]\s*([A-Z]{2,8}[A-Za-z0-9]*)\s*[\uff09)]', raw_text):
            full, abbr = m.group(1).strip(), m.group(2).upper()
            if len(abbr) >= 2 and abbr not in abbr_map and not re.search(r'\d', full) and len(full.split()) <= 6:
                abbr_map.setdefault(abbr, full)
        # 宽松模式2: ABBR (full name)
        for m in re.finditer(r'\b([A-Z]{2,8}[A-Za-z0-9]*)\s*[\uff08(]\s*([\w][\w\s,\-]{2,40}?)\s*[\uff09)]', raw_text):
            abbr, full = m.group(1).upper(), m.group(2).strip()
            if len(abbr) >= 2 and abbr not in abbr_map and not re.search(r'\d', full) and len(full.split()) <= 6:
                abbr_map.setdefault(abbr, full)
        # 停用词过滤: 全称含常见句子词(we/the/adopt 等)说明是句子碎片非缩写定义
        _STOP = {"we", "the", "a", "an", "is", "are", "was", "were", "in", "of", "for",
                 "with", "to", "by", "on", "at", "from", "that", "this", "our", "their",
                 "it", "as", "be", "been", "adopt", "use", "used", "using", "propose",
                 "proposed", "have", "has", "had", "do", "does", "did", "can", "could",
                 "should", "would", "will", "may", "might", "must", "which", "who", "phase"}
        abbr_map = {a: f for a, f in abbr_map.items()
                    if not any(w in _STOP for w in f.lower().split())}
        # 频率过滤
        filtered = {}
        for abbr in abbr_map:
            if len(re.findall(r'\b' + re.escape(abbr) + r'\b', raw_text)) >= 2:
                filtered[abbr] = abbr_map[abbr]
        abbr_map = filtered
    except Exception:
        pass
    return abbr_map


def autofix_bare_abbreviations(sem_text: str, abbr_map: dict) -> str:
    from graph_ingest import is_bare_abbreviation
    """用 raw 缩写映射自动补全语义槽裸缩写（三段式第二步）。

    仅当三元组字段精确等于裸缩写 token 时替换为 full(ABBR)。
    含中文前缀或复合词的字段不自动替换（交由 warning）。
    幂等: 已含括号的字段不会被 is_bare_abbreviation 判定。
    """
    if not abbr_map:
        return sem_text
    lines = sem_text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "|" not in stripped:
            continue
        parts = stripped.split("|")
        if len(parts) != 3:
            continue
        for j in range(len(parts)):
            field = parts[j].strip()
            if not is_bare_abbreviation(field):
                continue
            if field in abbr_map:
                parts[j] = f" {abbr_map[field]}({field}) "
                changed = True
            else:
                no_paren = re.sub(r"[\uff08(][^\uff09)]*[\uff09)]", "", field)
                tokens = re.findall(r"[A-Z]{2,}[A-Za-z0-9]*", no_paren)
                for tok in tokens:
                    if tok in abbr_map and field == tok:
                        parts[j] = f" {abbr_map[tok]}({tok}) "
                        changed = True
                        break
        if changed:
            lines[i] = "|".join(parts)
    if changed:
        return "\n".join(lines) + ("\n" if sem_text.endswith("\n") else "")
    return sem_text


def lightweight_abbr_resolve(REPO: Path) -> dict:
    """Query 后轻量裸缩写消解（只查图 alias，不扫 raw，零 LLM）。

    与 ingest 后的全量消解（_auto_resolve_abbreviations）不同：
    - 只做图层 alias 匹配（快，~10ms/条）
    - 不做 raw 正则扫描（query 场景无需）
    - 不跑命题层 --apply（写图操作留给 ingest 事务）

    返回 {"resolved": N, "remaining": M, "details": [...]}。
    """
    import graph_lib as gl
    todo_path = REPO / "cross-domain" / "abbreviation-todo.jsonl"
    if not todo_path.exists():
        return {"resolved": 0, "remaining": 0, "details": []}

    entries = []
    try:
        for line in todo_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        return {"resolved": 0, "remaining": 0, "details": []}

    remaining = []
    resolved_details = []
    try:
        conn = gl.connect()
        ti, ai, si = gl.build_name_index(conn)
    except Exception:
        return {"resolved": 0, "remaining": len(entries), "details": []}

    for entry in entries:
        abbr = entry.get("object", "").strip()
        if not abbr:
            remaining.append(entry)
            continue
        try:
            resolved_path, _ = gl.resolve_bare_name(abbr, ti, ai, si)
            if resolved_path:
                resolved_details.append({
                    "abbr": abbr,
                    "method": "graph_alias",
                    "resolved_to": resolved_path,
                    "page": entry.get("page", ""),
                })
                continue
        except Exception:
            pass
        remaining.append(entry)

    if resolved_details:
        with todo_path.open("w", encoding="utf-8") as handle:
            for entry in remaining:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return {
        "resolved": len(resolved_details),
        "remaining": len(remaining),
        "details": resolved_details,
    }





def detect_people_page_candidates(REPO: Path) -> dict:
    """检测达到 people page 建页标准的人物 entity（纯代码，零 LLM）。

    入选标准（满足任一）：
    - ≥6 篇论文的作者（作者边双向统计）
    - ≥4 篇论文的通讯作者
    - ≥3 次会议提及（参会边）
    - ≥2 种有意义关系类别（论文参与/会议参与/师生指导；所属/任职不计入）

    排除：
    - 已有 people page 的（path 含 wiki/authors/）
    - 占位符名（括号开头）
    - 机构名（含 Research/Institute/University 等关键词）

    结果追加到 cross-domain/people-pending.jsonl，幂等去重（按 path）。
    """
    import graph_lib as gl
    from datetime import datetime

    person_predicates = {"作者", "通讯作者", "参会", "指导", "师从", "受指导于", "所属", "任职于"}
    # 关系类别映射（排除所属/任职这类 trivial 关系）
    rel_categories = {
        "作者": "paper", "通讯作者": "paper",
        "参会": "meeting",
        "指导": "advisory", "师从": "advisory", "受指导于": "advisory",
    }
    org_keywords = ("research", "institute", "university", "laboratory",
                    "college", "school", "center", "academy", "corp",
                    "inc", "ltd", "qualcomm", "google", "microsoft", "ibm")

    conn = gl.connect()
    person_nodes = {
        row["path"]: {"title": row["title"] or "", "predicates": set(),
                      "categories": set(), "paper_count": 0,
                      "corresponding_count": 0, "meeting_count": 0}
        for row in conn.execute(
            "SELECT path, title FROM nodes WHERE entity_subtype='person'"
        )
    }

    # 过滤：已有 people page、占位符、机构
    def _is_valid_person(path: str, title: str) -> bool:
        if "/wiki/authors/" in path:
            return False  # 已有 people page
        if not title or title.startswith(("（", "(")):
            return False  # 占位符
        title_lower = title.lower()
        if any(kw in title_lower for kw in org_keywords):
            return False  # 机构名
        return True

    valid_persons = {p: info for p, info in person_nodes.items()
                     if _is_valid_person(p, info["title"])}
    if not valid_persons:
        _write_people_pending(REPO, [])
        return {"candidates": [], "pending_total": 0}

    # 统计关系
    for row in conn.execute("SELECT subject, predicate, object FROM edges"):
        pred = row["predicate"]
        if pred not in person_predicates:
            continue
        subj, obj = row["subject"], row["object"]
        for person_path, other_path in ((subj, obj), (obj, subj)):
            if person_path not in valid_persons:
                continue
            info = valid_persons[person_path]
            info["predicates"].add(pred)
            if pred in rel_categories:
                info["categories"].add(rel_categories[pred])
            if pred == "作者" and "/wiki/papers/" in other_path:
                info["paper_count"] += 1
            elif pred == "通讯作者" and "/wiki/papers/" in other_path:
                info["corresponding_count"] += 1
            elif pred == "参会" and ("/wiki/conferences/" in other_path or "/wiki/meetings/" in other_path):
                info["meeting_count"] += 1

    # 筛选达标者
    candidates = []
    for path, info in valid_persons.items():
        criteria = []
        if info["paper_count"] >= 6:
            criteria.append("multi_paper_author")
        if info["corresponding_count"] >= 4:
            criteria.append("multi_corresponding")
        if info["meeting_count"] >= 3:
            criteria.append("multi_meeting")
        if len(info["categories"]) >= 2:
            criteria.append("multi_relationship_type")
        if not criteria:
            continue
        candidates.append({
            "path": path,
            "name": info["title"],
            "criteria": criteria,
            "paper_count": info["paper_count"],
            "corresponding_count": info["corresponding_count"],
            "meeting_count": info["meeting_count"],
            "relationship_categories": sorted(info["categories"]),
            "detected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })

    candidates.sort(key=lambda c: (-c["paper_count"], -c["meeting_count"], c["name"]))
    pending_total = _write_people_pending(REPO, candidates)
    return {"candidates": candidates, "pending_total": pending_total}


def _write_people_pending(REPO: Path, candidates: list) -> int:
    """写入 people-pending.jsonl（全量覆盖，幂等）。"""
    pending_path = REPO / "cross-domain" / "people-pending.jsonl"
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    with pending_path.open("w", encoding="utf-8") as handle:
        for entry in candidates:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return len(candidates)


def step_validate_graph(state: dict, REPO: Path) -> list[str]:
    """运行 ingest_check.py --graph，返回 ERROR 列表。"""
    wiki_path = REPO / (state["wiki_path"] + ".md")
    cmd = [sys.executable, str(REPO / ".scripts/ingest_check.py"),
           "--graph", str(wiki_path.relative_to(REPO))]
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
    record_subprocess(state, "ingest_check", cmd, result.returncode,
                       int((time.monotonic() - start) * 1000))
    if result.returncode == 0:
        return []
    return parse_check_errors(result.stdout + result.stderr)


def step_finalize(state: dict, REPO: Path, config: dict) -> tuple[bool, str]:
    """调 inbox_finalize.py 原子落位 raw/wiki 到最终目录。

    config:
        doc_id_key: state 中的 ID 字段名（如 "paper_id"）
        manifest_files: list[str] | None — None 不建 manifest；
            非空则按存在性过滤后写入 manifest.json
        copy_source: bool — 是否复制源文件到 extract_dir
    """
    extract_dir = REPO / state["extract_dir"]
    raw_dir = REPO / state["raw_dir"]
    wiki_path = REPO / (state["wiki_path"] + ".md")
    if config.get("copy_source"):
        import shutil
        source_path = REPO / state["source"]
        raw_dest = extract_dir / source_path.name
        if not raw_dest.exists():
            shutil.copy2(source_path, raw_dest)
    manifest_files = config.get("manifest_files")
    if callable(manifest_files):
        manifest_files = manifest_files(state)
    if manifest_files is not None:
        raw_files = [name for name in manifest_files if (extract_dir / name).is_file()]
        if not raw_files:
            return False, "extract_dir 无可归档的 raw 文件"
        manifest = {"raw_files": raw_files, "wiki_file": "wiki.md"}
        (extract_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    cmd = [sys.executable, str(REPO / ".scripts/inbox_finalize.py"),
           "--paper-id", state[config["doc_id_key"]],
           "--raw-dir", str(raw_dir.relative_to(REPO)),
           "--wiki-path", str(wiki_path.relative_to(REPO)),
           "--extract-dir", str(extract_dir.relative_to(REPO))]
    if config.get("allow_existing_raw_dir"):
        cmd.append("--allow-existing-raw-dir")
    output = run(cmd, REPO)
    receipt_match = re.search(r"receipt:\s*(.+)", output)
    if receipt_match:
        state["receipt"] = receipt_match.group(1).strip()
    return True, ""


def step_finalize_tail(state: dict, REPO: Path, config: dict) -> tuple[bool, str]:
    """收尾三件：log.md 追加 + index.md 追加 + ingest_build 派生同步。

    config:
        doc_id_key: state 中的 ID 字段名
        get_log_path: (state, REPO) -> Path
        get_index_path: (state, REPO) -> Path
        index_section: index.md 中的 section header（如 "## 论文"），None 则追加到末尾
        entry_prefix: index 条目前缀（如 "papers/"），空串则无前缀
        build_log_entry: (ctx) -> str — ctx 含 today/doc_id/page_name/title/edges/report/state/fm
        build_entry: (ctx) -> str | None — 自定义 index 条目；None 则用默认格式
        skip_index: bool — True 时跳过 index.md 追加（re-ingest 等已有索引场景）
        frontier_capture: bool — 论文成功后限量捕获作者明示开放问题；失败仅 warning
        frontier_answer: bool — 捕获后在当前 WikiGraph 内非阻断尝试回答；默认 True
    """
    import graph_lib as gl
    today = datetime.now().strftime("%Y-%m-%d")
    doc_id = state.get(config["doc_id_key"], "")
    wiki_page = state.get("wiki_path", "")
    page_name = wiki_page.rsplit("/", 1)[-1] if wiki_page else doc_id
    report = state.get("graph_report") or {}
    edges = report.get("edges_added", 0)
    fm: dict = {}
    try:
        fm = gl.read_frontmatter(wiki_page)
    except Exception:
        pass
    title = fm.get("title", "") or ""
    ctx = {
        "today": today, "doc_id": doc_id, "page_name": page_name,
        "title": title, "edges": edges, "report": report,
        "state": state, "fm": fm,
    }
    # 1. log.md
    try:
        log_path = config["get_log_path"](state, REPO)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_entry = config["build_log_entry"](ctx)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as exc:
        return False, "log.md 追加失败: " + str(exc)
    # 2. index.md（skip_index=True 时跳过，如 re-ingest 已有索引）
    if not config.get("skip_index"):
        try:
            index_path = config["get_index_path"](state, REPO)
            index_path.parent.mkdir(parents=True, exist_ok=True)
            build_entry = config.get("build_entry")
            if build_entry:
                entry = build_entry(ctx)
            else:
                desc = title[:60] + ("…" if len(title) > 60 else "")
                entry = f"- [[{config.get('entry_prefix', '')}{page_name}]] — {desc}\n"
            if index_path.is_file():
                index_text = index_path.read_text(encoding="utf-8")
                section = config.get("index_section")
                if section and section in index_text:
                    m = re.search(rf'({re.escape(section)}\n)(.*?)(?=^## |\Z)', index_text, re.S | re.M)
                    if m:
                        insert_at = m.start() + len(m.group(1)) + len(m.group(2))
                        index_text = index_text[:insert_at] + entry + index_text[insert_at:]
                    else:
                        index_text += "\n" + section + "\n" + entry
                else:
                    index_text += entry
                index_path.write_text(index_text, encoding="utf-8")
            else:
                index_path.write_text(f"# 索引\n\n{entry}", encoding="utf-8")
        except Exception as exc:
            return False, "index.md 追加失败: " + str(exc)
    # 3. ingest_build
    try:
        run([sys.executable, str(REPO / ".scripts/ingest_build.py"), "--catalog"], REPO)
    except Exception as exc:
        return False, "ingest_build.py 失败: " + str(exc)
    # 4. Frontier 候选捕获：独立于 ingest 事务，只抓作者明示问题/局限/future work。
    # 失败不得让事实摄入回滚或失败。
    if config.get("frontier_capture") and wiki_page:
        cmd = [sys.executable, str(REPO / ".scripts/frontier.py"),
               "capture-paper", wiki_page, "--limit", str(config.get("frontier_capture_limit", 3))]
        if not config.get("frontier_answer", True):
            cmd.append("--no-answer")
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
        record_subprocess(state, "frontier_capture", cmd, result.returncode,
                          int((time.monotonic() - started) * 1000))
        if result.returncode == 0:
            try:
                state["frontier_capture"] = json.loads(result.stdout)
            except json.JSONDecodeError:
                state["frontier_capture"] = {"status": "degraded", "error": "非 JSON 输出"}
        else:
            warning = "Frontier 候选捕获失败（不影响 ingest）: " + (result.stderr or result.stdout).strip()[:300]
            state.setdefault("warnings", []).append(warning)
    return True, ""

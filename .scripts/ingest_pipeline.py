"""ingest_pipeline.py — 摄入流程共享驱动器（run_pipeline）。

把 ingest_meeting / ingest_document 两编排器中逐行同构的 run_pipeline
骨架抽出，差异点用 spec 参数化。借鉴 DSH capability seam 思想：
各编排器只声明 provider（step 函数）+ config（行为开关），驱动器统管
调度循环、状态机、修复循环、resume 安全网。

spec 字段：
  script_name            resume_cmd 里的脚本名（如 "ingest_meeting.py"）
  preprocess_label       [3.2] 进度文本
  completion_label_key   完成时打印的 state key（如 "meeting_id"）；None 则不打印
  repair_fail_strategy   "handoff"（repair 失败→交 agent）/ "retry"（增 slots_retry 续循环）
  cleanup_after          "validate_graph" / "finalize_tail"：清理 source/extract_dir 的时机
  rollback_fn            validate_graph 失败时的回滚函数；None 则不回滚
  finalize_tail_failure  "warn"（失败仍 completed）/ "hard"（失败→failed）
  steps                  dict：dedup_check/preprocess/write_wiki/validate_wiki/
                         write_slots/validate_semantics/repair_slots/finalize/
                         update_graph/validate_graph/finalize_tail

step 签名约定：
  dedup_check(state)->(bool,str); preprocess(state)->(bool,str)
  write_wiki(state)->(bool,str); validate_wiki(state)->list[str]
  write_slots(state)->(bool,str); validate_semantics(state)->(hard,warnings)
  repair_slots(state,warnings)->(bool,str); finalize(state)->(bool,str)
  update_graph(state)->(bool,str); validate_graph(state)->list[str]
  finalize_tail(state)->(bool,str)
"""
from __future__ import annotations

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import inbox_state
import ingest_common as ic
import trash_util


def _resume_cmd(spec, txn_id: str) -> str:
    return f"python3 .scripts/{spec['script_name']} --resume {txn_id}"


def _save(state: dict) -> None:
    inbox_state.save(state["transaction_id"], state)


def run_pipeline(state: dict, spec: dict, progress) -> dict:
    """运行全流程，处理修复循环。progress 为进度打印函数。"""
    steps = spec["steps"]
    txn = state["transaction_id"]

    # 手工修复 semantic.txt 后 resume：agent_required 状态需真正恢复推进
    if state["status"] == "agent_required":
        resume_cmd = _resume_cmd(spec, txn)
        semantic_path = REPO / state.get("semantic_path", "")
        if semantic_path.is_file():
            validation_errors = ic.validate_before_commit(
                state, steps["validate_semantics"], spec.get("non_blocking_issues", ()))
            if validation_errors:
                ic.handoff_to_agent(state, "恢复前语义槽校验未通过",
                                    steps["validate_semantics"], resume_cmd)
                _save(state)
                return state
            state["status"] = "finalize"
            state.pop("agent_required", None)
            state["errors"] = []
        else:
            state["status"] = "write_wiki"
            state.pop("agent_required", None)
            state["errors"] = []
        _save(state)

    # 恢复或手工修复后，落位前全量复验（resume 安全网）
    if state["status"] in {"finalize", "update_graph", "validate_graph", "finalize_tail", "graph_ready"}:
        validation_errors = ic.validate_before_commit(
            state, steps["validate_semantics"], spec.get("non_blocking_issues", ()))
        if validation_errors:
            ic.handoff_to_agent(state, "恢复前语义槽校验未通过",
                                steps["validate_semantics"], _resume_cmd(spec, txn))
            _save(state)
            return state

    # 3.1 dedup_check
    if state["status"] in ("init", "dedup_check"):
        progress(f"\n{'='*60}", flush=True)
        progress("[3.1] 去重检查：查 graph.db + raw 目录...", flush=True)
        is_dup, msg = steps["dedup_check"](state)
        if is_dup:
            progress(f"  ↳ 已摄入：{msg}", flush=True)
            state["status"] = "duplicate_found"
            _save(state)
            return state
        progress("  ↳ 未摄入，继续", flush=True)
        state["status"] = "preprocess"
        _save(state)

    # 3.2 preprocess
    if state["status"] == "preprocess":
        progress(f"[3.2] {spec['preprocess_label']}...", flush=True, end=" ")
        success, msg = steps["preprocess"](state)
        if not success:
            state["status"] = "failed"
            state["errors"] = [msg]
            _save(state)
            return state
        progress("完成", flush=True)
        state["status"] = "write_wiki"
        _save(state)

    # 3.3-3.6 两阶段循环
    if state["status"] == "write_wiki":
        state.setdefault("wiki_retry", 0)
        state.setdefault("slots_retry", 0)
    max_retries = spec.get("max_retries", 3)
    while state["status"] == "write_wiki" and \
            (state.get("wiki_retry", 0) + state.get("slots_retry", 0)) <= max_retries:
        # 第一阶段：写 wiki
        if state.get("wiki_retry", 0) == 0 and not state.get("wiki_content"):
            progress("\n[3.3a] 撰写 wiki（调用LLM）...", flush=True)
        elif state.get("wiki_retry", 0) > 0:
            progress(f"\n[3.3a] 撰写 wiki（重试第{state['wiki_retry']}/{max_retries}次）...", flush=True)
        ic.record_llm_call(state, "write_wiki")
        success, msg = steps["write_wiki"](state)
        if state.get("agent_required"):
            state["status"] = "agent_required"
            _save(state)
            return state
        if state.get("type_mismatch"):
            state["status"] = "type_mismatch"
            state["errors"] = [msg]
            _save(state)
            return state
        if not success:
            state["wiki_retry"] += 1
            state["errors"] = [msg]
            progress(f"  ↳ 失败: {msg}", flush=True)
            _save(state)
            continue
        progress("[3.4] wiki结构校验...", flush=True, end=" ")
        wiki_errors = steps["validate_wiki"](state)
        progress("通过" if not wiki_errors else f"{len(wiki_errors)}个错误", flush=True)
        if wiki_errors:
            state["wiki_errors"] = wiki_errors
            state["wiki_retry"] += 1
            state["wiki_content"] = ""
            _save(state)
            continue
        # 第二阶段：写语义槽
        if state.get("slots_retry", 0) == 0:
            progress("[3.3b] 抽取语义槽（续接对话）...", flush=True, end=" ")
        else:
            progress(f"[3.3b] 抽取语义槽（重试第{state['slots_retry']}/{max_retries}次）...", flush=True, end=" ")
        ic.record_llm_call(state, "write_slots")
        success, msg = steps["write_slots"](state)
        if state.get("agent_required"):
            state["status"] = "agent_required"
            _save(state)
            return state
        if not success:
            state["slots_retry"] += 1
            state["errors"] = [msg]
            progress(f"失败: {msg}", flush=True)
            _save(state)
            continue
        progress("完成", flush=True)
        progress("[3.5] 语义槽格式化...", flush=True, end=" ")
        try:
            ic.step_fill_semantics(state, REPO, spec["normalize_slots"])
            progress("完成", flush=True)
            progress("[3.6] 语义槽校验...", flush=True, end=" ")
            sem_hard, slot_warnings = steps["validate_semantics"](state)
            progress(("通过" if not sem_hard and not slot_warnings
                      else f"{len(sem_hard)}硬错误,{len(slot_warnings)}可修warning"), flush=True)
        except Exception as exc:
            sem_hard = [f"语义槽处理失败: {exc}"]
            slot_warnings = []
            progress(f"异常: {exc}", flush=True)
        _save(state)
        if not sem_hard and slot_warnings:
            ic.record_llm_call(state, "repair_slots")
            repaired, repair_msg = steps["repair_slots"](state, slot_warnings)
            if state.get("agent_required"):
                state["status"] = "agent_required"
                _save(state)
                return state
            if repaired:
                progress("  ↳ 修复完成", flush=True)
                slot_warnings = []
            else:
                progress(f"  ↳ 修复未完全: {repair_msg}", flush=True)
        _save(state)
        if not sem_hard and not slot_warnings:
            state["errors"] = []
            state["status"] = "finalize"
            break
        # 结构错误早停
        if sem_hard:
            progress(f"  ↳ 语义槽结构错误 {len(sem_hard)} 个，停止重复生成并交接修复", flush=True)
            ic.stop_for_semantic_errors(state, sem_hard,
                _resume_cmd(spec, txn), slot_warnings)
            _save(state)
            return state
        # repair_fail_strategy=retry：增 slots_retry 续循环
        if spec.get("repair_fail_strategy") == "retry":
            if state.get("slots_retry", 0) + state.get("wiki_retry", 0) >= max_retries:
                break
            state["slots_retry"] += 1
            _save(state)
        else:  # handoff（meeting 默认）：repair 未完全→交 agent
            state["status"] = "agent_required"
            _save(state)
            return state
    else:
        if state["status"] == "write_wiki":
            state["status"] = "failed"
            state["errors"] = ["修复循环超过最大重试次数", *state.get("errors", [])]
            _save(state)
            return state

    # 落位
    if state["status"] == "finalize":
        progress("\n[落位] 原子复制 raw/wiki 到最终目录...", flush=True, end=" ")
        success, msg = steps["finalize"](state)
        if not success:
            state["status"] = "failed"
            state["errors"] = [msg]
            _save(state)
            return state
        progress("完成", flush=True)
        state["status"] = "update_graph"
        _save(state)

    # 3.7 update_graph
    if state["status"] in ("update_graph", "graph_ready"):
        progress("[3.7] 写图边（graph_ingest）...", flush=True, end=" ")
        success, msg = steps["update_graph"](state)
        if not success:
            state["status"] = "failed"
            state["errors"] = [msg]
            _save(state)
            return state
        report = state.get("graph_report", {})
        progress(f"完成（{report.get('edges_added', '?')}条边）", flush=True)
        state["status"] = "validate_graph"
        _save(state)

    # 3.8 validate_graph
    if state["status"] == "validate_graph":
        progress("[3.8] 图校验（ingest_check --graph）...", flush=True, end=" ")
        graph_errors = steps["validate_graph"](state)
        if graph_errors:
            progress(f"{len(graph_errors)}个错误", flush=True)
            if spec.get("rollback_fn"):
                rolled = spec["rollback_fn"](state)
                state["status"] = "failed"
                state["errors"] = graph_errors + [f"已回滚落位: {', '.join(rolled)}"]
            else:
                state["status"] = "failed"
                state["errors"] = graph_errors
            _save(state)
            return state
        progress("PASS", flush=True)
        if spec.get("cleanup_after") == "validate_graph":
            _cleanup_sources(state, spec.get("skip_source_cleanup_if"))
        state["status"] = "finalize_tail"
        _save(state)

    # 3.9 finalize_tail
    if state["status"] == "finalize_tail":
        progress("[3.9] 收尾（log/index/派生同步）...", flush=True, end=" ")
        tail_ok, tail_msg = steps["finalize_tail"](state)
        if not tail_ok:
            if spec.get("finalize_tail_failure") == "hard":
                progress(f"失败: {tail_msg}", flush=True)
                state["status"] = "failed"
                state["errors"] = [tail_msg]
                _save(state)
                return state
            progress(f"WARN: {tail_msg}", flush=True)
            state.setdefault("warnings", []).append(tail_msg)
        else:
            progress("完成", flush=True)
        if spec.get("cleanup_after") == "finalize_tail":
            _cleanup_sources(state, spec.get("skip_source_cleanup_if"))
        completion_errors = ic.validate_completion(state, REPO)
        if completion_errors:
            progress(f"完成校验失败: {len(completion_errors)}个错误", flush=True)
            state["status"] = "failed"
            state["errors"] = completion_errors
            _save(state)
            return state
        state["status"] = "completed"
        _save(state)
        label = ""
        if spec.get("completion_label_key"):
            label = state.get(spec["completion_label_key"], "")
        progress(f"\n{'='*60}\n✅ 摄入完成: {label}", flush=True)

    return state


def _cleanup_sources(state: dict, skip_source_if: str | None = None) -> None:
    """清理 inbox 源 + 临时提取目录（移入废纸篓，可恢复）。

    skip_source_if：state key，truthy 时跳过 source 清理（paper from_raw 场景）。
    """
    if not (skip_source_if and state.get(skip_source_if)):
        source_path = REPO / state["source"]
        if source_path.is_file():
            trash_util.trash_path(source_path)
    extract_dir = REPO / state["extract_dir"]
    if extract_dir.exists():
        trash_util.trash_path(extract_dir)

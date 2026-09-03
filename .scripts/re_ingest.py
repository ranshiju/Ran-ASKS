#!/usr/bin/env python3
"""re_ingest.py — 重新摄入已入库的 raw 以对齐最新版本。

raw 不可变（红线），只重生 wiki + 清旧图边 + 重建。
复用 ingest_paper 的 wiki 生成步骤，自管 finalize（覆盖 wiki）和 commit（graph_ingest --clean）。

用法:
  python3 .scripts/re_ingest.py --raw academic/raw/works/papers/<paper-id>/paper.md   # 本人论文（works）
  python3 .scripts/re_ingest.py --raw academic/raw/references/<paper-id>/paper.md      # 外部论文
  python3 .scripts/re_ingest.py --manifest          # 全量 re-ingest（works + references）
  python3 .scripts/re_ingest.py --manifest --dry-run # 预览清单
  python3 .scripts/re_ingest.py --outdated          # 仅 re-ingest 管道版本落后的论文
  python3 .scripts/re_ingest.py --resume <txn-id>   # 恢复 agent_required 中断的事务
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import inbox_state
import ingest_common as ic
from ingest_common import (
    progress, set_progress_file, set_progress_log_path, close_progress_file,
)
import ingest_paper as ip
from ingest_paper import REPO

TEMP_REINGEST = REPO / "temp" / "reingest-extract"


def build_manifest() -> list[dict]:
    """扫描 academic/raw/{references,works/papers}/*/paper.md，构建 re-ingest 清单。"""
    items = []
    seen = set()
    for root_rel in ("academic/raw/references", "academic/raw/works/papers"):
        root = REPO / root_rel
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            paper_md = d / "paper.md"
            if not paper_md.is_file():
                continue
            paper_id = d.name
            if paper_id in seen:
                continue
            seen.add(paper_id)
            wiki_path = f"academic/wiki/papers/{paper_id}"
            wiki_file = REPO / (wiki_path + ".md")
            items.append({
                "paper_id": paper_id,
                "raw_md": str(paper_md.relative_to(REPO)),
                "wiki_path": wiki_path,
                "wiki_exists": wiki_file.exists(),
            })
    return items


def outdated_items() -> list[dict]:
    """查 graph.db 中 ingest_version < CURRENT_PIPELINE_VERSION 的 page 节点，
    映射回 academic/raw/{references,works/papers}/*/paper.md 清单。"""
    import graph_lib as gl
    conn = gl.connect()
    current = gl.CURRENT_PIPELINE_VERSION
    rows = conn.execute(
        "SELECT path FROM nodes WHERE type='page' AND "
        "(ingest_version IS NULL OR ingest_version < ?)",
        (current,)
    ).fetchall()
    conn.close()
    raw_roots = [REPO / "academic/raw/references", REPO / "academic/raw/works/papers"]
    items = []
    for row in rows:
        page_path = row[0]
        # page_path 形如 academic/wiki/papers/<paper-id>
        if "/papers/" not in page_path:
            continue
        paper_id = page_path.rsplit("/", 1)[-1]
        paper_md = None
        for root in raw_roots:
            cand = root / paper_id / "paper.md"
            if cand.is_file():
                paper_md = cand
                break
        if not paper_md:
            continue
        items.append({
            "paper_id": paper_id,
            "raw_md": str(paper_md.relative_to(REPO)),
            "wiki_path": page_path,
            "wiki_exists": (REPO / (page_path + ".md")).exists(),
        })
    return items


def page_ingest_version(paper_id: str) -> int | None:
    """Return the stored page pipeline version without mutating graph state."""
    import graph_lib as gl
    conn = gl.connect()
    try:
        row = conn.execute(
            "SELECT ingest_version FROM nodes WHERE path=? AND type='page'",
            (f"academic/wiki/papers/{paper_id}",),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def new_state_for_reingest(paper_id: str, raw_md_rel: str) -> dict:
    """为 re-ingest 构建状态：跳过 dedup + extract，从 write_wiki 开始。"""
    txn = "reingest-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + paper_id[:30]
    extract_dir = TEMP_REINGEST / txn
    extract_dir.mkdir(parents=True, exist_ok=True)
    # 复制 raw paper.md 到 extract_dir（step_write_wiki 从此读取）
    raw_md_path = REPO / raw_md_rel
    shutil.copy2(raw_md_path, extract_dir / "paper.md")
    # raw_dir 取 raw paper.md 的真实父目录（works/references 均可，不再硬编码 references）
    raw_dir = str((REPO / raw_md_rel).parent.relative_to(REPO))
    md_text = raw_md_path.read_text(encoding="utf-8")
    bibliography, corrections = ip.repair_archived_bibliography(
        ip.load_bibliographic_metadata(raw_md_path.parent), md_text)
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
        "reingest": True,
        "retry_count": 0,
        "errors": [],
        "wiki_retry": 0,
        "slots_retry": 0,
    }


def review_reingest_bibliography(state: dict) -> bool:
    """Run the same evidence-bound bibliography gate used by fresh ingestion."""
    extract_dir = REPO / state["extract_dir"]
    md_text = (extract_dir / "paper.md").read_text(encoding="utf-8")
    result = ip.review_bibliographic_metadata(
        state.get("bibliographic_meta"), md_text, state.get("transaction_id", ""))
    draft_rel = str(extract_dir.relative_to(REPO) / "bibliographic-review.json")

    if result.get("status") == "agent_required":
        state["status"] = "agent_required"
        state["agent_required"] = True
        state["pre_handoff_status"] = "write_wiki"
        state["agent_prompt"] = (
            result.get("agent_prompt", "")
            + f"\n\n请将符合 {ip.BIBLIOGRAPHIC_DECISION_PROTOCOL} schema 的书目裁决 JSON 写入 "
            + f"`{draft_rel}`，然后运行 "
            + f"`python3 .scripts/re_ingest.py --resume {state['transaction_id']}`。"
        )
        state["bibliographic_review"] = {
            "status": "agent_required",
            "review": result.get("review", {}),
            "decision": result.get("decision"),
            "candidates": result.get("candidates", {}),
            "catalog": result.get("catalog", {}),
            "input_hash": result.get("input_hash", ""),
            "worker": result.get("worker", {}),
            "draft_path": draft_rel,
        }
        return False

    if not result.get("ok"):
        if isinstance(result.get("review"), dict):
            (extract_dir / "bibliographic-review.json").write_text(
                json.dumps(result["review"], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        state["status"] = "bibliographic_review_required"
        state["bibliographic_review_required"] = True
        state["bibliographic_review"] = {
            "status": result.get("status", "bibliographic_review_required"),
            "error": result.get("error", ""),
            "review": result.get("review", {}),
            "compiled_review": result.get("compiled_review", {}),
            "candidates": result.get("candidates", {}),
            "catalog": result.get("catalog", {}),
            "input_hash": result.get("input_hash", ""),
            "worker": result.get("worker", {}),
            "draft_path": draft_rel,
        }
        state["retryable"] = False
        state["next_action"] = "repair_bibliographic_review_then_resume"
        state["errors"] = [result.get("error", "书目预审未通过，禁止 Wiki/Graph 提交")]
        return False

    state["bibliographic_meta"] = result.get("bibliographic") or {}
    ip._record_bibliographic_quality_warnings(state, md_text)
    state["bibliographic_review"] = {
        "status": "ok",
        "review": result.get("review"),
        "decision": result.get("decision"),
        "candidates": result.get("candidates"),
        "catalog": result.get("catalog"),
        "input_hash": result.get("input_hash"),
        "worker": result.get("worker"),
    }
    ip.persist_bibliographic_metadata(extract_dir, state["bibliographic_meta"])
    return True


REINGEST_TAIL_CONFIG = {
    "doc_id_key": "paper_id",
    "get_log_path": lambda state, REPO: REPO / "academic" / "wiki" / "log.md",
    "skip_index": True,
    "build_log_entry": lambda ctx: (
        "\n## [" + ctx["today"] + "] re-ingest | re_ingest.py 重新摄入 " + ctx["doc_id"] + "\n"
        "- **来源与归档**：raw 不变（红线），重生 wiki 对齐最新版本。\n"
        "- **来源页**：覆盖 `papers/" + ctx["doc_id"] + ".md`，" + ctx["title"] + "。\n"
        "- **图谱巩固**：清旧边" + str(ctx["state"].get("_cleaned_removed", 0))
        + "条后重建 " + str(ctx["edges"]) + " 条边"
        + ("，主方向「" + (ctx["report"].get("derived_directions") or {}).get("main", "") + "」"
           if (ctx["report"].get("derived_directions") or {}).get("main") else "") + "。\n"
        "- **验证**：`ingest_check --graph` PASS（ERROR=0）。\n"
    ),
}


def commit_wiki_and_graph(state: dict) -> dict:
    """覆盖 wiki.md + 清旧图边重建 + catalog + log。"""
    wiki_path = REPO / (state["wiki_path"] + ".md")
    extract_dir = REPO / state["extract_dir"]
    new_wiki = extract_dir / "wiki.md"

    # 1. 原子覆盖 wiki.md
    if not new_wiki.is_file():
        state["status"] = "failed"
        state["errors"] = ["extract_dir 无 wiki.md，无法覆盖"]
        inbox_state.save(state["transaction_id"], state)
        return state
    progress("[落位] 覆盖 wiki.md...", flush=True, end=" ")
    # 备份旧 wiki 到 extract_dir（安全回滚点）
    if wiki_path.exists():
        backup_path = REPO / "temp" / "inbox-state" / f"{state['transaction_id']}-wiki-old.md"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wiki_path, backup_path)
        state["wiki_backup"] = str(backup_path.relative_to(REPO))
    os.replace(new_wiki, wiki_path)
    progress("完成", flush=True)

    # 2. 清旧图边 + 重建（复用 ic.step_update_graph，clean=True）
    progress("[3.7] 写图边（graph_ingest --clean）...", flush=True, end=" ")
    graph_ok, graph_msg = ic.step_update_graph(state, REPO, clean=True)
    if not graph_ok:
        backup = REPO / str(state.get("wiki_backup") or "")
        if state.get("wiki_backup") and backup.is_file():
            shutil.copy2(backup, wiki_path)
            state["wiki_restored"] = True
        state["status"] = "failed"
        state["errors"] = [graph_msg or "graph update failed"]
        inbox_state.save(state["transaction_id"], state)
        return state
    ip._record_graph_quality_warnings(state)
    report = state.get("graph_report") or {}
    edges = report.get("edges_added", "?")
    removed = report.get("cleaned_page", "") and report.get("edges_removed", 0)
    progress(f"完成（{edges}条边，清旧{removed}条）", flush=True)

    # 3. 图校验
    progress("[3.8] 图校验（ingest_check --graph）...", flush=True, end=" ")
    graph_errors = ic.step_validate_graph(state, REPO)
    if graph_errors:
        progress(f"{len(graph_errors)}个错误", flush=True)
        state["status"] = "failed"
        state["errors"] = graph_errors
        inbox_state.save(state["transaction_id"], state)
        return state
    progress("PASS", flush=True)

    # 4+5. catalog 重建 + log.md（复用 ic.step_finalize_tail，skip_index=True）
    state["_cleaned_removed"] = removed
    progress("[3.9] 收尾（log/catalog/派生同步）...", flush=True, end=" ")
    tail_ok, tail_msg = ic.step_finalize_tail(state, REPO, REINGEST_TAIL_CONFIG)
    if not tail_ok:
        progress(f"WARN: {tail_msg}", flush=True)
        state.setdefault("warnings", []).append(tail_msg)
    else:
        progress("完成", flush=True)

    # 6. 清理 temp
    shutil.rmtree(extract_dir, ignore_errors=True)

    state["status"] = "completed"
    inbox_state.save(state["transaction_id"], state)
    progress(f"\n{'='*60}\n✅ re-ingest 完成: {state.get('paper_id', '')}", flush=True)
    return state


def run_one(paper_id: str, raw_md_rel: str, verbose: bool) -> dict:
    """单篇 re-ingest 全流程。"""
    state = new_state_for_reingest(paper_id, raw_md_rel)
    inbox_state.save(state["transaction_id"], state)

    # 隔离 quiet 模式进度日志
    set_progress_file(None)
    set_progress_log_path(None)
    if not verbose:
        log_path = REPO / "temp" / "inbox-state" / f"{state['transaction_id']}.log"
        set_progress_file(log_path.open("a", encoding="utf-8"))
        set_progress_log_path(log_path)
    try:
        progress(f"\n{'='*60}")
        progress(f"📄 re-ingest: {paper_id}")
        progress("[3.2b] 复核归档书目（证据约束 Worker）...", flush=True, end=" ")
        if not review_reingest_bibliography(state):
            progress(state["status"], flush=True)
            inbox_state.save(state["transaction_id"], state)
            return state
        progress("通过", flush=True)
        inbox_state.save(state["transaction_id"], state)
        state = ip.run_prepare(state)
        if state["status"] == "propositions_done":
            state = commit_wiki_and_graph(state)
    except Exception as exc:
        state["status"] = "failed"
        state["errors"] = [f"未预期异常: {type(exc).__name__}: {exc}"]
        inbox_state.save(state["transaction_id"], state)
    finally:
        close_progress_file()
    return state


def resume_reingest(txn_id: str, verbose: bool) -> int:
    """恢复 agent_required 中断的 re-ingest 事务。

    走 re-ingest 自有的 commit_wiki_and_graph（带 --clean 清旧边），
    而非 ingest_paper 的无 clean commit，避免旧边残留。
    """
    state = inbox_state.load(txn_id)
    if not state:
        print(json.dumps({"status": "error", "error": f"事务不存在: {txn_id}"}))
        return 1
    if state.get("status") != "agent_required":
        print(json.dumps({"status": "error",
                          "error": f"事务状态非 agent_required: {state.get('status')}",
                          "transaction_id": txn_id}))
        return 1
    # agent 已修正 semantic 文件，清标记，从命题抽取继续（跳过 wiki/slots 重写）
    state["agent_required"] = False
    state["agent_prompt"] = ""
    state["errors"] = []
    inbox_state.transition(state, "propositions", reason="resume_reingest_after_agent_fix")
    inbox_state.save(txn_id, state)

    set_progress_file(None)
    set_progress_log_path(None)
    if not verbose:
        log_path = REPO / "temp" / "inbox-state" / f"{txn_id}.log"
        set_progress_file(log_path.open("a", encoding="utf-8"))
        set_progress_log_path(log_path)
    try:
        progress(f"\n{'='*60}")
        progress(f"📄 re-ingest resume: {state.get('paper_id', '')}")
        state = ip.run_prepare(state)
        if state["status"] == "propositions_done":
            state = commit_wiki_and_graph(state)
    except Exception as exc:
        state["status"] = "failed"
        state["errors"] = [f"未预期异常: {type(exc).__name__}: {exc}"]
        inbox_state.save(txn_id, state)
    finally:
        close_progress_file()

    print(json.dumps({
        "status": state["status"],
        "paper_id": state.get("paper_id"),
        "transaction_id": txn_id,
        "errors": state.get("errors", []),
        "edges_added": (state.get("graph_report") or {}).get("edges_added"),
        "quality_status": "degraded" if state.get("quality_warnings") else "complete",
        "quality_warnings": state.get("quality_warnings", []),
        "bibliographic_corrections": state.get("bibliographic_corrections", []),
    }, ensure_ascii=False, indent=2))
    return 0 if state["status"] == "completed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--raw", help="raw paper.md 路径或 raw 目录路径（单篇）")
    src.add_argument("--manifest", action="store_true", help="扫描全量已入库论文")
    src.add_argument("--outdated", action="store_true", help="仅 re-ingest 管道版本落后的论文")
    src.add_argument("--resume", help="恢复 agent_required 中断的事务 ID（走 clean commit）")
    parser.add_argument("--dry-run", action="store_true", help="仅列出清单，不执行")
    parser.add_argument("--verbose", action="store_true", help="进度打印到 stdout")
    parser.add_argument("--force", action="store_true", help="即使页面已是当前管线版本仍重新生成")
    args = parser.parse_args()

    # resume：恢复 agent_required 中断的事务（走 re-ingest 自有 clean commit）
    if args.resume:
        return resume_reingest(args.resume, args.verbose)

    # 构建执行清单
    if args.outdated:
        items = outdated_items()
    elif args.manifest:
        items = build_manifest()
    else:
        raw_path = (REPO / args.raw).resolve()
        if raw_path.is_dir():
            raw_path = raw_path / "paper.md"
        if not raw_path.is_file():
            print(json.dumps({"status": "error", "error": f"raw 不存在: {args.raw}"}))
            return 1
        paper_id = raw_path.parent.name
        import graph_lib as gl
        stored_version = page_ingest_version(paper_id)
        if (not args.force and stored_version is not None
                and stored_version >= gl.CURRENT_PIPELINE_VERSION):
            print(json.dumps({
                "status": "completed",
                "items": [{
                    "paper_id": paper_id,
                    "status": "up_to_date",
                    "ingest_version": stored_version,
                    "current_pipeline_version": gl.CURRENT_PIPELINE_VERSION,
                    "api_called": False,
                }],
            }, ensure_ascii=False, indent=2))
            return 0
        items = [{
            "paper_id": paper_id,
            "raw_md": str(raw_path.relative_to(REPO)),
            "wiki_path": f"academic/wiki/papers/{paper_id}",
            "wiki_exists": (REPO / f"academic/wiki/papers/{paper_id}.md").exists(),
        }]

    if args.dry_run:
        print(json.dumps({"status": "dry-run", "count": len(items), "items": items},
                         ensure_ascii=False, indent=2))
        return 0

    if not items:
        print(json.dumps({"status": "completed", "items": []}))
        return 0

    # 逐篇 re-ingest
    results = []
    for item in items:
        state = run_one(item["paper_id"], item["raw_md"], args.verbose)
        results.append({
            "paper_id": item["paper_id"],
            "status": state["status"],
            "transaction_id": state["transaction_id"],
            "errors": state.get("errors", []),
            "edges_added": (state.get("graph_report") or {}).get("edges_added"),
            "quality_status": "degraded" if state.get("quality_warnings") else "complete",
            "quality_warnings": state.get("quality_warnings", []),
            "bibliographic_corrections": state.get("bibliographic_corrections", []),
        })
        if state["status"] == "agent_required":
            print(json.dumps({
                "status": "agent_required", "item": results[-1],
                "next": f"修正 semantic 文件后 --resume {state['transaction_id']}",
            }, ensure_ascii=False, indent=2))
            return 1

    print(json.dumps({
        "status": "completed" if all(r["status"] == "completed" for r in results) else "partial",
        "items": results,
    }, ensure_ascii=False, indent=2))
    return 0 if all(r["status"] == "completed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

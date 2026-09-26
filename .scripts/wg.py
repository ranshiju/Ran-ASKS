#!/usr/bin/env python3
"""wg.py — WikiGraph 横向能力面：跨功能、回合中可调的轻量统一入口。

设计目标（借鉴 DSH 能力面思想，本土化，非运行时插件）：
- 统一调用面：所有能力同一入口、同一 JSON envelope，agent 记一套约定
- 可组合：写作/研究中可任意组合能力，不必"切换任务"（解痛点：跨功能调用差）
- 自描述溯源：每个能力返回带 sources，可接 read-raw 核验事实
- 渐进披露：默认只返回导航/关联层，省 token；要深挖用 read-section / read-raw

底层全部复用现有脚本（query_graph.py / wiki_locator.py / cv.py /
workspace_state.py / research_memory.py / query_actions.py / source_locator.py），本文件只做薄包统一。

输出 envelope（stdout 一行 JSON）:
  {"ok": bool, "action": str, "result": ..., "sources": [...],
   "status": str, "error": ""}
  status ∈ ok | empty | error

用法:
  wg.py lookup <term>
  wg.py neighbors <page> [--depth N]
  wg.py relations <page> [--predicate P]
  wg.py hub-of <page>
  wg.py read-section <page> <section>
  wg.py read-raw <locator>              # 精确 locator：path#标题 / #L5-L8 / #page-2-3
  wg.py recall <project>
  wg.py remember <project> --title "..." --intent <intent> [--content "..." | --stdin] [--tags a,b]
  wg.py workspace recall <workspace>
  wg.py workspace item add <workspace> --title "..." --state active --next-action "..."
  wg.py cv status --workspace <workspace>
  wg.py cv render <edition> --workspace <workspace>
  wg.py ingest <file> --subproject admin [--allow-remote-ocr]
  wg.py ingest --resume <transaction-id>
  wg.py task inspect <transaction-id>
  wg.py task advance <transaction-id>
  wg.py task run <transaction-id> --task-command check|commit|resume|refresh|read
  wg.py abbr <term>
  wg.py frontier ask "<academic question>"
  wg.py frontier list
  wg.py frontier show <ID>
  wg.py frontier answer <ID>
  wg.py functions list
  wg.py functions show knowledge.query
  wg.py functions resolve knowledge.query --caller main_agent --backend agent
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

import source_locator as sl
import wiki_locator as wl
import graph_lib as gl
import query_actions as qa
import function_registry as fr
import agent_task
import inbox_state

RAW_PREVIEW_CHARS = 6000


def envelope(action, result, sources=None, status="ok", error="", ok=None):
    if ok is None:
        ok = status != "error"
    out = {
        "ok": ok,
        "action": action,
        "result": result,
        "sources": sources or [],
        "status": status,
        "error": error,
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def run_script(args: list[str], **kw) -> tuple[int, str, str]:
    binary_input = isinstance(kw.get("input"), bytes)
    p = subprocess.run(args, capture_output=True, text=not binary_input, **kw)
    if binary_input:
        return p.returncode, p.stdout.decode("utf-8", errors="replace"), p.stderr.decode("utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def extract_last_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    best = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[index + end:].strip():
            best = value
    return best or {}


def query_graph_json(cmd: str, pos_args: list[str], opts: list[str] | None = None) -> dict:
    args = ["python3", str(SCRIPTS / "query_graph.py"), cmd, *pos_args, "--json",
            "--db", str(qa.query_db_path())]
    if opts:
        args.extend(opts)
    rc, out, err = run_script(args)
    if rc != 0:
        return {"_error": (err or out).strip()[:500]}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"_error": f"非 JSON 输出: {out[:300]}",
                "_raw": out[:500]}
    if isinstance(data, dict) and data.get("error"):
        return {"_error": str(data["error"])[:500]}
    return data


def resolve_page_path(page: str) -> Path | None:
    return sl.resolve_path(page)


def collect_sources_from_edges(obj: dict) -> list[str]:
    srcs = []
    for edge in obj.get("edges", []) if isinstance(obj, dict) else []:
        s = edge.get("source") if isinstance(edge, dict) else None
        if s and s not in srcs:
            srcs.append(s)
    return srcs


def cmd_lookup(args):
    res = query_graph_json("search", [args.term])
    if "_error" in res:
        return envelope("lookup", None, status="error", error=res["_error"])
    hits = res.get("nodes") or res.get("results") or []
    if not hits:
        return envelope("lookup", [], status="empty", error="无命中")
    return envelope("lookup", hits, sources=[], status="ok")


def cmd_neighbors(args):
    res = query_graph_json("neighbors", [args.page], ["--depth", str(args.depth)])
    if "_error" in res:
        return envelope("neighbors", None, status="error", error=res["_error"])
    srcs = collect_sources_from_edges(res)
    return envelope("neighbors", res, sources=srcs, status="ok")


def cmd_relations(args):
    opts = ["--predicate", args.predicate] if args.predicate else []
    res = query_graph_json("relations", [args.page], opts)
    if "_error" in res:
        return envelope("relations", None, status="error", error=res["_error"])
    srcs = collect_sources_from_edges(res)
    return envelope("relations", res, sources=srcs, status="ok")


def cmd_hub_of(args):
    res = query_graph_json("hub_of", [args.page])
    if "_error" in res:
        return envelope("hub-of", None, status="error", error=res["_error"])
    return envelope("hub-of", res, sources=[], status="ok")


def cmd_abbr(args):
    res = query_graph_json("search", [args.term])
    if "_error" in res:
        return envelope("abbr", None, status="error", error=res["_error"])
    nodes = res.get("nodes", [])
    if not nodes:
        return envelope("abbr", {"term": args.term, "matches": []},
                        status="empty", ok=False,
                        error=f"未找到缩写: {args.term}")
    return envelope("abbr", {"term": args.term, "matches": nodes},
                    sources=[node.get("path", "") for node in nodes if node.get("path")],
                    status="ok")


def cmd_read_section(args):
    try:
        if args.with_context:
            result = qa.wiki_context_data(
                args.page, args.section or "", args.profile, args.topk
            )
            sources = result["wiki"]["raw_citations"]
        else:
            result = qa.read_wiki_data(args.page, args.section or "")
            sources = result["raw_citations"]
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return envelope("read-section", None, status="error", error=str(exc))
    return envelope("read-section", result,
                    sources=sources, status="ok")


def cmd_hybrid_recall(args):
    text, _tokens = qa.hybrid_recall(args.query, args.intent, args.domain, str(args.topk))
    if text.startswith("[ERROR"):
        return envelope("hybrid-recall", None, status="error", error=text)
    return envelope("hybrid-recall", json.loads(text), status="ok")


def cmd_read_raw(args):
    raw = args.locator
    path_part, loc = sl.split_locator(raw)
    if not path_part:
        path_part = raw
    target = sl.resolve_path(path_part)
    if target is None:
        return envelope("read-raw", {"locator": raw}, status="error",
                        error=f"raw 路径未解析: {path_part}")
    qa.check_path_scope(target)
    requested_rel = str(target.resolve().relative_to(REPO)) if target.is_absolute() else str(target)
    if not loc or loc == "全篇":
        return envelope("read-raw", {"locator": raw, "path": requested_rel}, status="error",
                        error="read-raw 需要精确 locator（标题、Lx-Ly 或 page-x-y）；不向 LLM 返回全文")
    companion_binding = sl.companion_binding_for_target(target)
    if companion_binding and companion_binding["status"] == "invalid":
        return envelope(
            "read-raw", {"locator": raw, "path": requested_rel,
                         "companion_binding": companion_binding}, status="error", ok=False,
            error="companion 与原件绑定失效，须重新生成后再查询",
        )
    source_target, read_target = sl.evidence_targets(target, loc)
    qa.check_path_scope(source_target)
    qa.check_path_scope(read_target)
    source_rel = str(source_target.resolve().relative_to(REPO))
    read_rel = str(read_target.resolve().relative_to(REPO))
    status = sl.locator_status(loc, read_target)
    result = {"locator": raw, "path": source_rel, "source_path": source_rel,
              "read_path": read_rel, "evidence_locator": f"{read_rel}#{loc}", "section": loc,
              "locator_status": status,
              "companion_binding": companion_binding,
              "is_binary": source_target.suffix.lower() in sl.BINARY_SUFFIXES}
    if status == "missing":
        return envelope("read-raw", result, sources=[source_rel], status="empty", ok=False,
                        error=f"locator '{loc}' 在 {read_rel} 中未找到")
    seg = sl.read_locator_text(read_target, loc)
    if seg is None:
        return envelope("read-raw", result, sources=[source_rel], status="empty", ok=False,
                        error=f"locator '{loc}' 已验证但无法精确截取；未返回全文")
    if len(seg) > RAW_PREVIEW_CHARS:
        return envelope("read-raw", result, sources=[source_rel], status="error", ok=False,
                        error=f"locator '{loc}' 命中 {len(seg)} 字符，范围过大；请细化 locator，未返回半截内容")
    result["text"] = seg
    return envelope("read-raw", result, sources=[source_rel], status="ok")


def cmd_recall(args):
    rc, out, err = run_script(["python3", str(SCRIPTS / "research_memory.py"),
                               "recall", args.project])
    if rc != 0:
        return envelope("recall", None, status="error",
                        error=(err or out).strip()[:300])
    return envelope("recall", {"project": args.project, "text": out.strip()},
                    sources=[], status="ok")


def cmd_ingest(args):
    pasted = getattr(args, "stdin", False)
    keep_source = getattr(args, "keep_source", False)
    if sum(bool(value) for value in (args.file, args.resume, pasted)) != 1:
        return envelope("ingest", None, status="error",
                        error="必须且只能提供一个 file、--stdin 或 --resume")
    if keep_source and not args.file:
        return envelope("ingest", None, status="error", error="--keep-source 只能用于文件附件")
    command = ["python3", str(SCRIPTS / "ingest_inbox.py"), "--run"]
    input_options = {}
    if args.resume:
        command.extend(["--resume", args.resume])
    elif pasted:
        command.extend(["--stdin", "--subproject", args.subproject])
        if args.name:
            command.extend(["--import-name", args.name])
        if args.document_type:
            command.extend(["--document-type", args.document_type])
        input_options["input"] = sys.stdin.buffer.read()
    else:
        supplied = Path(args.file).expanduser()
        source = supplied.absolute() if keep_source else supplied.resolve()
        try:
            rel = source.relative_to(REPO)
        except ValueError:
            rel = None
        if not keep_source and rel is not None and rel.parts and rel.parts[0] == "inbox":
            command.extend(["--file", str(rel)])
        else:
            command.extend(["--import-file", str(source)])
            if keep_source:
                command.append("--keep-source")
            if args.name:
                command.extend(["--import-name", args.name])
        command.extend(["--subproject", args.subproject])
        if args.document_type:
            command.extend(["--document-type", args.document_type])
    if args.ocr_result:
        command.extend(["--ocr-result", args.ocr_result])
    if getattr(args, "allow_remote_ppt", False):
        command.append("--allow-remote-ppt")
    if args.allow_remote_ocr:
        command.append("--allow-remote-ocr")
    rc, out, err = run_script(command, cwd=REPO, **input_options)
    result = extract_last_json(out)
    if not result:
        return envelope("ingest", None, status="error",
                        error=(err or out or "摄入入口未返回 JSON").strip()[:500])
    sources = []
    for item in result.get("files", []) if isinstance(result.get("files"), list) else []:
        for key in ("raw_dir", "wiki_path"):
            if item.get(key) and item[key] not in sources:
                sources.append(item[key])
    for key in ("raw_dir", "wiki_path"):
        if result.get(key) and result[key] not in sources:
            sources.append(result[key])
    failed = result.get("status") in {"failed", "validation_error", "backend_mismatch"}
    if rc != 0 and failed:
        return envelope("ingest", result, sources=sources, status="error",
                        error=str(result.get("errors") or result.get("error") or "摄入失败")[:500])
    return envelope("ingest", result, sources=sources, status="ok")


def _task_execution_payload(execution: dict) -> dict:
    parsed = extract_last_json(execution.get("stdout", ""))
    return {
        "action": execution["action"],
        "returncode": execution["returncode"],
        "command": execution["command"],
        "environment_overrides": execution["environment_overrides"],
        "result": parsed,
        "error": "" if parsed else (execution.get("stderr") or execution.get("stdout") or "受管 action 未返回 JSON").strip()[:500],
    }


def _run_task_action(state: dict, action: str) -> dict:
    return _task_execution_payload(agent_task.run_action(state, action, REPO))


def cmd_task(args):
    """Expose one content-agnostic host loop for persisted Agent tasks."""
    state = inbox_state.load(args.transaction_id)
    if not state:
        return envelope(
            f"task.{args.task_action}", None, status="error",
            error=f"事务不存在: {args.transaction_id}",
        )
    try:
        view = agent_task.control_view(state, REPO)
    except ValueError as exc:
        return envelope(f"task.{args.task_action}", None, status="error", error=str(exc))
    if args.task_action == "inspect":
        return envelope("task.inspect", view)
    if view["workflow_status"] in {"completed", "failed"}:
        return envelope(f"task.{args.task_action}", {"control": view, "executions": []})
    reading_only = args.task_action == "run" and args.task_command == "read"
    if view["missing_outputs"] and not reading_only:
        return envelope(
            f"task.{args.task_action}", {"control": view, "executions": []},
            status="empty", ok=False,
            error="请先写完 task.outputs 中的 required 暂存产物",
        )

    commands = (view["task"].get("commands") or {})
    executions = []
    try:
        if args.task_action == "advance":
            if "check" in commands:
                checked = _run_task_action(state, "check")
                executions.append(checked)
                checked_result = checked.get("result") or {}
                ready = (
                    checked_result.get("workflow_status") == "ready_to_commit"
                    or checked_result.get("status") == "ready_to_commit"
                )
                if not ready or checked["returncode"] != 0:
                    return envelope("task.advance", {
                        "control": view, "executions": executions,
                        "next_action": "repair_outputs" if checked_result else "inspect_error",
                    }, status="ok" if checked_result else "error",
                    error=checked.get("error", ""))
                if "commit" in commands:
                    executions.append(_run_task_action(state, "commit"))
            elif "resume" in commands:
                executions.append(_run_task_action(state, "resume"))
            elif "commit" in commands:
                executions.append(_run_task_action(state, "commit"))
            else:
                return envelope("task.advance", {"control": view, "executions": []},
                                status="error", error="任务未声明可推进的受管 action")
        else:
            action = args.task_command
            if action == "read":
                executions.append(_run_task_action(state, "read"))
            else:
                executions.append(_run_task_action(state, action))
    except ValueError as exc:
        return envelope(f"task.{args.task_action}", {
            "control": view, "executions": executions,
        }, status="error", error=str(exc))

    refreshed = inbox_state.load(args.transaction_id) or state
    refreshed_view = agent_task.control_view(refreshed, REPO)
    last = executions[-1] if executions else {}
    failed = last.get("returncode", 0) != 0 or bool(last.get("error"))
    return envelope(
        f"task.{args.task_action}",
        {"control": refreshed_view, "executions": executions},
        status="error" if failed else "ok",
        error=last.get("error", "") if failed else "",
    )


def cmd_remember(args):
    if args.stdin:
        content = sys.stdin.read()
    else:
        content = args.content or ""
    cmd = ["python3", str(SCRIPTS / "research_memory.py"), "add",
           args.project, "--title", args.title, "--intent", args.intent]
    if content:
        cmd += ["--content", content]
    if args.tags:
        cmd += ["--tags", args.tags]
    rc, out, err = run_script(cmd)
    if rc != 0:
        return envelope("remember", None, status="error",
                        error=(err or out).strip()[:300])
    return envelope("remember", {"project": args.project, "output": out.strip()},
                    sources=[], status="ok")


def cmd_workspace(args):
    """Generic workspace-state thin wrapper; the target script owns its schema."""
    command = ["python3", str(SCRIPTS / "workspace_state.py"), *args.workspace_args]
    rc, out, err = run_script(command)
    if rc != 0:
        try:
            diagnostic = json.loads(out)
        except json.JSONDecodeError:
            diagnostic = None
        if isinstance(diagnostic, dict) and diagnostic.get("ok") is False:
            return envelope("workspace", {"command": args.workspace_args, "output": diagnostic},
                            sources=[], status="error", ok=False,
                            error="workspace doctor reported errors")
        return envelope("workspace", None, status="error",
                        error=(err or out).strip()[:500])
    output = out.strip()
    try:
        result = json.loads(output)
    except json.JSONDecodeError:
        result = {"text": output}
    ok = result.get("ok") if isinstance(result, dict) and "ok" in result else True
    return envelope("workspace", {"command": args.workspace_args, "output": result},
                    sources=[], status="ok" if ok else "error", ok=ok,
                    error="" if ok else "workspace doctor reported errors")


def cmd_cv(args):
    """Thin wrapper around the deterministic CV workspace function."""
    command = [sys.executable, str(SCRIPTS / "cv.py"), *args.cv_args]
    rc, out, err = run_script(command)
    try:
        result = json.loads(out.strip())
    except json.JSONDecodeError:
        return envelope("cv", None, status="error",
                        error=(err or out or "cv.py returned no JSON").strip()[:500])
    sources = [value for key in ("source", "manifest", "artifact")
               if isinstance((value := result.get(key)), str) and value]
    failed = rc != 0 or result.get("status") in {"error", "blocked"}
    return envelope("cv", {"command": args.cv_args, "output": result},
                    sources=sources, status="error" if failed else "ok", ok=not failed,
                    error=(result.get("error") or err.strip())[:500] if failed else "")


def cmd_frontier(args):
    """Frontier 薄包；主逻辑和准入契约只定义在 frontier.py。"""
    cmd = ["python3", str(SCRIPTS / "frontier.py"), args.frontier_cmd]
    if args.frontier_cmd == "ask":
        cmd += ["--question", args.question, "--topk", str(args.topk)]
        if args.no_ai:
            cmd.append("--no-ai")
    elif args.frontier_cmd in {"show", "answer", "refresh", "review", "add-entry"}:
        cmd.append(args.record_id)
        if args.frontier_cmd in {"answer", "refresh"} and args.no_ai:
            cmd.append("--no-ai")
        elif args.frontier_cmd == "review":
            cmd += ["--status", args.status, "--reviewer", args.reviewer]
        elif args.frontier_cmd == "add-entry":
            cmd += ["--kind", args.kind, "--content", args.content,
                    "--origin", args.origin, "--epistemic", args.epistemic]
            for evidence in args.evidence or []:
                cmd += ["--evidence", evidence]
    elif args.frontier_cmd == "list":
        if args.kind:
            cmd += ["--kind", args.kind]
        if args.status:
            cmd += ["--status", args.status]
        if args.all:
            cmd.append("--all")
    elif args.frontier_cmd == "search":
        cmd.append(args.query)
    elif args.frontier_cmd == "capture-paper":
        cmd += [args.page, "--limit", str(args.limit)]
        if args.no_answer:
            cmd.append("--no-answer")
    rc, out, err = run_script(cmd)
    if rc != 0:
        return envelope("frontier", None, status="error", error=(err or out).strip()[:500])
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return envelope("frontier", None, status="error", error=f"Frontier 非 JSON 输出: {out[:300]}")
    sources = result.get("raw_evidence", []) if isinstance(result, dict) else []
    return envelope("frontier", result, sources=sources,
                    status="empty" if isinstance(result, dict) and result.get("count") == 0 else "ok")


def cmd_functions(args):
    """Expose catalog discovery and policy resolution, never generic execution."""
    if args.functions_cmd == "list":
        result = fr.catalog(
            audience=args.audience,
            kind=args.kind,
            include_internal=args.include_internal,
        )
    elif args.functions_cmd == "show":
        result = fr.show(args.function_id)
    elif args.functions_cmd == "resolve":
        result = fr.resolve(
            args.function_id,
            caller=args.caller,
            backend=args.backend,
            state=args.state,
        )
    else:
        errors = fr.validate_runtime() if args.runtime else fr.validate_registry()
        result = {
            "schema": "function-registry-validation-v1",
            "status": "error" if errors else "ok",
            "errors": errors,
        }
        if errors:
            return envelope(
                "functions", result,
                sources=[str(fr.REGISTRY_PATH.relative_to(REPO))],
                status="error", error="; ".join(errors),
            )
    return envelope(
        "functions", result,
        sources=[str(fr.REGISTRY_PATH.relative_to(REPO))],
    )


def build_parser():
    ap = argparse.ArgumentParser(prog="wg.py", description="WikiGraph 能力面")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lookup", help="关键词查节点（导航层）")
    p.add_argument("term"); p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("neighbors", help="节点关联召回 BFS")
    p.add_argument("page"); p.add_argument("--depth", type=int, default=2)
    p.set_defaults(func=cmd_neighbors)

    p = sub.add_parser("relations", help="节点关系边（可按谓词）")
    p.add_argument("page"); p.add_argument("--predicate", default="")
    p.set_defaults(func=cmd_relations)

    p = sub.add_parser("hub-of", help="页所属 Hub")
    p.add_argument("page"); p.set_defaults(func=cmd_hub_of)

    p = sub.add_parser("abbr", help="缩写解析（导航层）")
    p.add_argument("term"); p.set_defaults(func=cmd_abbr)

    p = sub.add_parser("read-section", help="按 heading slug 读 wiki 页及该节 Raw 引用")
    p.add_argument("page", help="wiki 路径，亦可直接写 page.md#heading-slug")
    p.add_argument("section", nargs="?", default="", help="heading 标题或 slug")
    p.add_argument("--with-context", action="store_true", help="动态附加受限 Graph 上下文")
    p.add_argument("--profile", default="explanation",
                   choices=("fact", "relation", "explanation", "exploration", "lineage"))
    p.add_argument("--topk", type=int, default=12)
    p.set_defaults(func=cmd_read_section)

    p = sub.add_parser("hybrid-recall", help="按意图融合 Wiki 语义与 Graph 结构召回")
    p.add_argument("query")
    p.add_argument("--intent", default="exploration",
                   choices=("fact", "relation", "explanation", "exploration", "lineage"))
    p.add_argument("--domain", default="", choices=("", "academic", "admin", "teaching", "business", "private"))
    p.add_argument("--topk", type=int, default=8)
    p.set_defaults(func=cmd_hybrid_recall)

    p = sub.add_parser("read-raw", help="按 locator 读 raw 片段溯源核验")
    p.add_argument("locator"); p.set_defaults(func=cmd_read_raw)

    p = sub.add_parser("recall", help="研究记忆恢复")
    p.add_argument("project"); p.set_defaults(func=cmd_recall)

    p = sub.add_parser("ingest", help="统一 inbox 摄入入口；外部附件受管暂存后进入同一管线")
    p.add_argument("file", nargs="?", help="inbox 文件或仓库外附件路径")
    p.add_argument("--stdin", action="store_true", help="从 stdin 原样摄入粘贴正文")
    p.add_argument("--keep-source", action="store_true", help="对话附件复制摄入，原件不移动、不修改、不删除")
    p.add_argument("--resume", default="", help="恢复既有摄入事务")
    p.add_argument("--name", default="", help="外部附件进入 inbox 时使用的文件名")
    p.add_argument("--subproject", default="academic",
                   choices=("academic", "admin", "teaching", "business"))
    p.add_argument("--document-type", default="",
                   choices=("", "editorial", "academic-reference", "conference-summary"))
    p.add_argument("--ocr-result", default="", help="已有源绑定 OCR JSON 回执")
    p.add_argument("--allow-remote-ppt", action="store_true", help="显式授权所选PPTX的API内容识读")
    p.add_argument("--allow-remote-ocr", action="store_true",
                   help="显式授权单张图片上传 GLM OCR API")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("task", help="跨 Agent 的受管任务 inspect/advance/action 入口")
    p.add_argument("task_action", choices=("inspect", "advance", "run"))
    p.add_argument("transaction_id")
    p.add_argument("--task-command", default="read",
                   choices=("read", "check", "commit", "resume", "refresh"),
                   help="task run 时执行任务显式声明的受管 action")
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("remember", help="研究记忆沉淀")
    p.add_argument("project"); p.add_argument("--title", required=True)
    p.add_argument("--intent", required=True)
    p.add_argument("--content", default="")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--tags", default="")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("workspace", help="通用持续工作区状态、事项、记忆与投影")
    p.add_argument("workspace_args", nargs=argparse.REMAINDER,
                   help="传给 workspace_state.py 的子命令与参数")
    p.set_defaults(func=cmd_workspace)

    p = sub.add_parser("cv", help="简历状态、校验、日期版本生成与差异比较")
    p.add_argument("cv_args", nargs=argparse.REMAINDER,
                   help="传给 cv.py 的子命令与参数")
    p.set_defaults(func=cmd_cv)

    p = sub.add_parser("frontier", help="研究前沿 Question/Trajectory")
    frontier_sub = p.add_subparsers(dest="frontier_cmd", required=True)

    fp = frontier_sub.add_parser("ask", help="用户学术问题先查库再进入 Frontier")
    fp.add_argument("question"); fp.add_argument("--topk", type=int, default=6)
    fp.add_argument("--no-ai", action="store_true"); fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("list", help="列出 triaged/active Frontier 对象")
    fp.add_argument("--kind", choices=("question", "trajectory"), default="")
    fp.add_argument("--status", default=""); fp.add_argument("--all", action="store_true")
    fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("search", help="检索 Frontier")
    fp.add_argument("query"); fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("show", help="读取 Frontier 对象")
    fp.add_argument("record_id"); fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("answer", help="在当前知识库内尝试回答 Question Page")
    fp.add_argument("record_id"); fp.add_argument("--no-ai", action="store_true")
    fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("review", help="人工确认 Frontier 状态")
    fp.add_argument("record_id"); fp.add_argument("--status", required=True)
    fp.add_argument("--reviewer", default="user"); fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("refresh", help="基于当前知识库刷新 Question Page 回答")
    fp.add_argument("record_id"); fp.add_argument("--no-ai", action="store_true")
    fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("add-entry", help="追加思路、答案或验证条目")
    fp.add_argument("record_id"); fp.add_argument("--kind", required=True)
    fp.add_argument("--content", required=True); fp.add_argument("--origin", default="user_proposed")
    fp.add_argument("--epistemic", default="untested"); fp.add_argument("--evidence", action="append")
    fp.set_defaults(func=cmd_frontier)

    fp = frontier_sub.add_parser("capture-paper", help="从论文 Raw 捕获作者明示问题")
    fp.add_argument("page"); fp.add_argument("--limit", type=int, default=3)
    fp.add_argument("--no-answer", action="store_true")
    fp.set_defaults(func=cmd_frontier)

    p = sub.add_parser("functions", help="统一功能、状态和调用策略目录")
    functions_sub = p.add_subparsers(dest="functions_cmd", required=True)

    fp = functions_sub.add_parser("list", help="列出功能")
    fp.add_argument("--audience", choices=sorted(fr.VALID_AUDIENCE), default="")
    fp.add_argument("--kind", choices=sorted(fr.VALID_KINDS), default="")
    fp.add_argument("--include-internal", action="store_true")
    fp.set_defaults(func=cmd_functions)

    fp = functions_sub.add_parser("show", help="查看一个 canonical 功能")
    fp.add_argument("function_id")
    fp.set_defaults(func=cmd_functions)

    fp = functions_sub.add_parser("resolve", help="按调用者、后端和状态解析可用入口")
    fp.add_argument("function_id")
    fp.add_argument("--caller", required=True, choices=sorted(fr.VALID_CALLERS))
    fp.add_argument("--backend", required=True, choices=sorted(fr.VALID_BACKENDS))
    fp.add_argument("--state", default="")
    fp.set_defaults(func=cmd_functions)

    fp = functions_sub.add_parser("validate", help="校验功能注册表")
    fp.add_argument("--runtime", action="store_true")
    fp.set_defaults(func=cmd_functions)

    for name in ("lookup", "neighbors", "relations", "hub-of", "abbr", "read-section", "hybrid-recall", "read-raw"):
        sub.choices[name].add_argument("--subproject", default="",
            choices=("public", "academic", "admin", "teaching", "business", "private"),
            help="查询存储范围；private 仅访问隔离子库，空值按明确路径推断，否则使用公共库")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        if hasattr(args, "subproject") and args.cmd in {
                "lookup", "neighbors", "relations", "hub-of", "abbr", "read-section", "hybrid-recall", "read-raw"}:
            paths = [getattr(args, key, "") for key in ("page", "locator")]
            scope = qa.scope_for(args.subproject or getattr(args, "domain", ""), *paths)
            with qa.query_scope(scope):
                for path in paths:
                    if path:
                        qa.check_path_scope(path)
                return args.func(args)
        return args.func(args)
    except Exception as e:
        return envelope(args.cmd, None, status="error",
                        error=f"{type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    sys.exit(main())

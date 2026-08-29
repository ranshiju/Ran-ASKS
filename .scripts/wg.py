#!/usr/bin/env python3
"""wg.py — WikiGraph 横向能力面：跨功能、回合中可调的轻量统一入口。

设计目标（借鉴 DSH 能力面思想，本土化，非运行时插件）：
- 统一调用面：所有能力同一入口、同一 JSON envelope，agent 记一套约定
- 可组合：写作/研究中可任意组合能力，不必"切换任务"（解痛点：跨功能调用差）
- 自描述溯源：每个能力返回带 sources，可接 read-raw 核验事实
- 渐进披露：默认只返回导航/关联层，省 token；要深挖用 read-section / read-raw

底层全部复用现有脚本（query_graph.py / wiki_locator.py /
research_memory.py / query_actions.py / source_locator.py），本文件只做薄包统一。

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
  wg.py abbr <term>
  wg.py frontier ask "<academic question>"
  wg.py frontier list
  wg.py frontier show <ID>
  wg.py frontier answer <ID>
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
    p = subprocess.run(args, capture_output=True, text=True, **kw)
    return p.returncode, p.stdout, p.stderr


def query_graph_json(cmd: str, pos_args: list[str], opts: list[str] | None = None) -> dict:
    args = ["python3", str(SCRIPTS / "query_graph.py"), cmd, *pos_args, "--json"]
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
        result = wl.read_wiki_locator(args.page, args.section or "")
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return envelope("read-section", None, status="error", error=str(exc))
    return envelope("read-section", result,
                    sources=result["raw_citations"], status="ok")


def cmd_read_raw(args):
    raw = args.locator
    path_part, loc = sl.split_locator(raw)
    if not path_part:
        path_part = raw
    target = sl.resolve_path(path_part)
    if target is None:
        return envelope("read-raw", {"locator": raw}, status="error",
                        error=f"raw 路径未解析: {path_part}")
    rel = str(target.resolve().relative_to(REPO)) if target.is_absolute() else str(target)
    if not loc or loc == "全篇":
        return envelope("read-raw", {"locator": raw, "path": rel}, status="error",
                        error="read-raw 需要精确 locator（标题、Lx-Ly 或 page-x-y）；不向 LLM 返回全文")
    status = sl.locator_status(loc, target)
    result = {"locator": raw, "path": rel, "section": loc,
              "locator_status": status,
              "is_binary": target.suffix.lower() in sl.BINARY_SUFFIXES}
    if status == "missing":
        return envelope("read-raw", result, sources=[rel], status="empty", ok=False,
                        error=f"locator '{loc}' 在 {rel} 中未找到")
    seg = sl.read_locator_text(target, loc)
    if seg is None and result["is_binary"]:
        result["note"] = "原始材料没有可机械返回的 locator 文本；请读取同目录 Markdown companion。"
        return envelope("read-raw", result, sources=[rel], status="ok")
    if seg is None:
        return envelope("read-raw", result, sources=[rel], status="empty", ok=False,
                        error=f"locator '{loc}' 已验证但无法精确截取；未返回全文")
    if len(seg) > RAW_PREVIEW_CHARS:
        return envelope("read-raw", result, sources=[rel], status="error", ok=False,
                        error=f"locator '{loc}' 命中 {len(seg)} 字符，范围过大；请细化 locator，未返回半截内容")
    result["text"] = seg
    return envelope("read-raw", result, sources=[rel], status="ok")


def cmd_recall(args):
    rc, out, err = run_script(["python3", str(SCRIPTS / "research_memory.py"),
                               "recall", args.project])
    if rc != 0:
        return envelope("recall", None, status="error",
                        error=(err or out).strip()[:300])
    return envelope("recall", {"project": args.project, "text": out.strip()},
                    sources=[], status="ok")


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
    p.set_defaults(func=cmd_read_section)

    p = sub.add_parser("read-raw", help="按 locator 读 raw 片段溯源核验")
    p.add_argument("locator"); p.set_defaults(func=cmd_read_raw)

    p = sub.add_parser("recall", help="研究记忆恢复")
    p.add_argument("project"); p.set_defaults(func=cmd_recall)

    p = sub.add_parser("remember", help="研究记忆沉淀")
    p.add_argument("project"); p.add_argument("--title", required=True)
    p.add_argument("--intent", required=True)
    p.add_argument("--content", default="")
    p.add_argument("--stdin", action="store_true")
    p.add_argument("--tags", default="")
    p.set_defaults(func=cmd_remember)

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

    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except Exception as e:
        return envelope(args.cmd, None, status="error",
                        error=f"{type(e).__name__}: {str(e)[:300]}")


if __name__ == "__main__":
    sys.exit(main())

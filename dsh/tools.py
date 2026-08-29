"""tools.py — 将 WikiGraph 能力包装为 DSH 注册工具。

DSH capability seam：Service Definition（schema + execute_fn）→ Consumer（agent loop）。
查询类工具（graph_search/wiki_recall 等）直接调 query_actions 函数；
CLI 类工具（recall/remember 等）经 wg.py 子进程调用。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

from dsh.harness import ToolDefinition, ToolExecutionResult


def _wg_call(action: str, args: list[str]) -> str:
    """调用 wg.py，返回解析后的 content 或 error 文本。"""
    cmd = ["python3", str(SCRIPTS / "wg.py"), action, *args]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        return f"[ERROR wg.py 返回码 {p.returncode}: {p.stderr[:200]}]"
    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError:
        return f"[ERROR 非 JSON 输出: {p.stdout[:200]}]"
    if not env.get("ok"):
        return env.get("error", "[ERROR unknown]") or "[ERROR empty]"
    result = env.get("result", "")
    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False)
    return str(result)


def _qa_call(action: str, input_: dict) -> str:
    """直接调 query_actions 函数，返回 content 或 error 文本。

    查询类工具（graph_search/wiki_recall/admin_recall）不经 wg.py 子进程，
    避免子进程开销和 JSON envelope 二次解析。
    """
    import query_actions as qa
    result = qa.execute(action, input_)
    if not result.get("ok"):
        return result.get("error", "[ERROR unknown]") or "[ERROR empty]"
    return result.get("text", "")


# ============ 工具定义 ============

def build_tools() -> list[ToolDefinition]:
    """构建所有 WikiGraph 能力工具。"""
    return [
        ToolDefinition(
            name="graph_search",
            description="在知识图谱中搜索术语（覆盖缩写/别名/标题三路匹配），返回匹配节点列表",
            input_schema={"type": "object", "properties": {
                "term": {"type": "string", "description": "搜索术语"}},
                "required": ["term"]},
            execute_fn=lambda args: _qa_call("graph_search", {"term": args.get("term", "")}),
        ),
        ToolDefinition(
            name="graph_neighbors",
            description="获取某节点的图邻居（关联节点），depth 控制深度",
            input_schema={"type": "object", "properties": {
                "node": {"type": "string", "description": "节点标识"},
                "depth": {"type": "integer", "description": "搜索深度", "default": 2}},
                "required": ["node"]},
            execute_fn=lambda args: _qa_call("graph_neighbors",
                {"node": args.get("node", ""), "depth": str(args.get("depth", 2))}),
        ),
        ToolDefinition(
            name="graph_relations",
            description="获取某节点的关系边（按谓词过滤）",
            input_schema={"type": "object", "properties": {
                "node": {"type": "string"},
                "predicate": {"type": "string", "default": ""}},
                "required": ["node"]},
            execute_fn=lambda args: _qa_call("graph_relations",
                {"node": args.get("node", ""), "predicate": args.get("predicate", "")}),
        ),
        ToolDefinition(
            name="graph_hub_of",
            description="查询某页面所属的 hub（主题中心）",
            input_schema={"type": "object", "properties": {
                "page": {"type": "string"}},
                "required": ["page"]},
            execute_fn=lambda args: _qa_call("graph_hub_of", {"page": args.get("page", "")}),
        ),
        ToolDefinition(
            name="node_resolve",
            description=(
                "把名称或缩写解析为 canonical node_id。内部按 path/alias/label+semantic identity gate；"
                "只读且不执行 merge，ambiguous 时必须保留多候选或补上下文"
            ),
            input_schema={"type": "object", "properties": {
                "name": {"type": "string", "description": "待解析名称或缩写"},
                "context": {"type": "string", "description": "可选局部语境，用于同名消歧", "default": ""},
                "node_types": {"type": "string", "description": "可选逗号分隔类型", "default": ""},
                "topk": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}},
                "required": ["name"]},
            execute_fn=lambda args: _qa_call("node_resolve", {
                "name": args.get("name", ""), "context": args.get("context", ""),
                "node_types": args.get("node_types", ""), "topk": str(args.get("topk", 5)),
            }),
        ),
        ToolDefinition(
            name="semantic_search",
            description=(
                "按含义召回相关节点或 Hub。结果仅是 semantic candidates，不能据此认定同一节点或执行 merge"
            ),
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "概念性查询描述"},
                "scope": {"type": "string", "enum": ["node", "hub"], "default": "node"},
                "topk": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
                "required": ["query"]},
            execute_fn=lambda args: _qa_call("semantic_search", {
                "query": args.get("query", ""), "scope": args.get("scope", "node"),
                "topk": str(args.get("topk", 8)),
            }),
        ),
        ToolDefinition(
            name="hub_route",
            description=(
                "读取论文 Wiki 的可定位‘研究方向定位’句，与 canonical Hub Scope 路由；"
                "只返回候选或唯一计划，不创建 Hub、不写边"
            ),
            input_schema={"type": "object", "properties": {
                "page": {"type": "string", "description": "论文 Wiki path"},
                "topk": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5}},
                "required": ["page"]},
            execute_fn=lambda args: _qa_call("hub_route", {
                "page": args.get("page", ""), "topk": str(args.get("topk", 5)),
            }),
        ),
        ToolDefinition(
            name="hub_inspect",
            description=(
                "读取 Hub Scope、keyword/proposition/People 类型化成员及代码 split candidate，"
                "供主 Agent 判断 canonical 生命周期语义；只读"
            ),
            input_schema={"type": "object", "properties": {
                "hub": {"type": "string", "description": "canonical Hub path"}},
                "required": ["hub"]},
            execute_fn=lambda args: _qa_call("hub_inspect", {"hub": args.get("hub", "")}),
        ),
        ToolDefinition(
            name="read_section",
            description="按 heading slug 读取一个 wiki section，并返回该节引用的 Raw locators；page 也可写成 path#slug",
            input_schema={"type": "object", "properties": {
                "page": {"type": "string"},
                "section": {"type": "string", "default": ""}},
                "required": ["page"]},
            execute_fn=lambda args: _qa_call("read_section",
                {"page": args.get("page", ""), "section": args.get("section", "")}),
        ),
        ToolDefinition(
            name="read_raw",
            description="按精确 locator 读取 raw 片段（标题、Lx-Ly 或 page-x-y）；拒绝裸路径和 #全篇",
            input_schema={"type": "object", "properties": {
                "locator": {"type": "string", "description": "raw 路径#标题、#Lx-Ly 或 #page-x-y"}},
                "required": ["locator"]},
            execute_fn=lambda args: _qa_call("read_raw", {"locator": args.get("locator", "")}),
        ),
        ToolDefinition(
            name="admin_recall",
            description="行政文档直接召回（标题/别名/Navigation 三路匹配 + 图回退），只返回候选 Navigation",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "查询文本"},
                "topk": {"type": "string", "default": "8"}},
                "required": ["query"]},
            execute_fn=lambda args: _qa_call("admin_recall",
                {"query": args.get("query", ""), "topk": args.get("topk", "8")}),
        ),
        ToolDefinition(
            name="wiki_recall",
            description="跨域统一召回（标题/Navigation/论文方向定位 + 图回退），只返回候选 Navigation",
            input_schema={"type": "object", "properties": {
                "query": {"type": "string"},
                "domain": {"type": "string", "description": "academic/admin/teaching/business，空=跨域"},
                "topk": {"type": "string", "default": "8"}},
                "required": ["query"]},
            execute_fn=lambda args: _qa_call("wiki_recall",
                {"query": args.get("query", ""), "domain": args.get("domain", ""), "topk": args.get("topk", "8")}),
        ),
        ToolDefinition(
            name="recall",
            description="恢复研究项目上下文（profile + 近期记忆 + status）",
            input_schema={"type": "object", "properties": {
                "project": {"type": "string"}},
                "required": ["project"]},
            execute_fn=lambda args: _wg_call("recall", [args["project"]]),
        ),
        ToolDefinition(
            name="remember",
            description="向研究项目添加记忆条目（decision/insight/literature_judgment 等）",
            input_schema={"type": "object", "properties": {
                "project": {"type": "string"},
                "title": {"type": "string"},
                "intent": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "string", "description": "逗号分隔"}},
                "required": ["project", "title", "intent"]},
            execute_fn=lambda args: _wg_call("remember",
                [args["project"], "--title", args["title"], "--intent", args["intent"]]
                + (["--content", args["content"]] if args.get("content") else [])
                + (["--tags", args["tags"]] if args.get("tags") else [])),
        ),
    ]

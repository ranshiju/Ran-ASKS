"""build_tools.py — 建设任务专用 DSH capability seam。

借用 DSH 的 ToolDefinition/ToolRegistry/guard chain：
- 建设任务只暴露 impact、locator read、filtered list 三个能力。
- 工具体只经子进程调用 engineering_graph.py / engineering_locator.py。
- BuildLocatorGuard 在 impact 完成前拒绝 read/list，且 list 必须带 prefix。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dsh.guards.build_locator_guard import BuildLocatorGuard, TOOL_BUILD_IMPACT, TOOL_BUILD_LIST, TOOL_BUILD_READ
from dsh.harness import SessionLog, ToolDefinition, ToolExecution, ToolExecutionResult, ToolRegistry

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"


def _run_json(cmd: list[str], timeout: int = 90) -> str:
    """执行只读 CLI 并返回 JSON 结构化结果，不让裸 stdout 直接进入执行管道。"""
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "error": "工具调用超时"}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    output = proc.stdout or ""
    error = proc.stderr or ""
    payload = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": output,
        "error": error,
    }
    return json.dumps(payload, ensure_ascii=False)


def _impact_tool() -> ToolDefinition:
    def execute(args: dict) -> str:
        target = str(args.get("target") or "").strip()
        cmd = [sys.executable, str(SCRIPTS / "engineering_graph.py"), "impact", target]
        if args.get("verify", False):
            cmd.append("--verify")
        return _run_json(cmd, timeout=120)

    return ToolDefinition(
        name=TOOL_BUILD_IMPACT,
        description="建设任务影响面入口：impact <target> --verify。必须先调用本工具，记录 node/contract/capability、推荐 locator 与最小验证命令。",
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "graph.yaml 节点名或建设目标，如 ingest_paper"},
                "verify": {"type": "boolean", "description": "是否输出最小验证命令，建设任务默认 true", "default": True},
            },
            "required": ["target"],
        },
        execute_fn=execute,
    )


def _read_tool() -> ToolDefinition:
    def execute(args: dict) -> str:
        locator = str(args.get("locator") or "").strip()
        cmd = [sys.executable, str(SCRIPTS / "engineering_locator.py"), "read", locator]
        if args.get("max_chars"):
            cmd += ["--max-chars", str(args["max_chars"])]
        return _run_json(cmd)

    return ToolDefinition(
        name=TOOL_BUILD_READ,
        description="精确读取一条 engineering locator：md:/yaml:/py:/Lx-Ly。返回片段、canonical locator、起止行、字符数与 SHA-256；失败不回退全文。",
        input_schema={
            "type": "object",
            "properties": {
                "locator": {"type": "string", "description": "精确 locator，如 operations/engineering/graph.yaml#yaml:/script_contracts/ingest_paper"},
                "max_chars": {"type": "integer", "description": "片段预算上限，默认 6000"},
            },
            "required": ["locator"],
        },
        execute_fn=execute,
    )


def _list_tool() -> ToolDefinition:
    def execute(args: dict) -> str:
        path = str(args.get("path") or "").strip()
        prefix = str(args.get("prefix") or "").strip()
        cmd = [sys.executable, str(SCRIPTS / "engineering_locator.py"), "list", path]
        if prefix:
            cmd += ["--prefix", prefix]
        return _run_json(cmd)

    return ToolDefinition(
        name=TOOL_BUILD_LIST,
        description="按 prefix 枚举 engineering locator 元数据，不回传正文。impact 推荐不足时才使用；必须传 prefix。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "仓内文本/YAML/Python 文件相对路径"},
                "prefix": {"type": "string", "description": "过滤前缀，如 py:step_extract / yaml:/nodes/ingest_paper / md:"},
            },
            "required": ["path", "prefix"],
        },
        execute_fn=execute,
    )


def build_build_tools() -> list[ToolDefinition]:
    return [_impact_tool(), _read_tool(), _list_tool()]


class BuildLocatorCockpit:
    """建设任务读取门的最小 cockpit。

    组合 ToolRegistry + BuildLocatorGuard + SessionLog，供具备建设定位能力的
    驱动循环复用；不写 raw/wiki/graph，也不进入查询型 DSH 工具面。
    """

    def __init__(self):
        self.guard = BuildLocatorGuard()
        self.registry = ToolRegistry()
        for tool in build_build_tools():
            self.registry.register(tool)
        self.registry.on_pre_execute(self.guard.on_pre_execute)
        self.registry.on_post_execute(self.guard.on_post_execute)

    def schemas(self) -> list[dict]:
        return self.registry.schemas()

    def execute(self, name: str, args: dict) -> ToolExecutionResult:
        session_log = SessionLog()
        return self.registry.execute(ToolExecution(name=name, arguments=args), session_log)

    def start_prompt(self, target: str) -> str:
        lines = [
            "建设任务已进入工程精确读取门。",
            f"第一步必须调用 {TOOL_BUILD_IMPACT}(target={target!r}, verify=true)。",
            "impact 给出推荐 locator 后，用 build_locator_read 精确读取。",
            "只有推荐不足时，才调用 build_locator_list 且必须传 prefix。",
            "不得直接使用裸 cat/sed 读取工程 YAML 或代码全文。",
        ]
        return "\n".join(lines)

    def audit(self) -> dict:
        return self.guard.snapshot()

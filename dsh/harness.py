"""harness.py — DSH cockpit 核心：Hook 系统 + 工具注册 + 执行管道。

借鉴 DSH 的 tool-execution-pipeline：
  tool/call → tools/pre-execute(瀑布) → tools/execute(瀑布, around-dispatch)
  → tool body → tools/post-execute(瀑布) → tool/result

Hook 是事件瀑布（waterfall）：每个 listener 调 next() 委托给下一个。
Guard 是自包含插件：监听事件，转换行为（观察/否决/替换/追加上下文）。
"""
from __future__ import annotations

import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent


# ============ Session Event Log ============

@dataclass
class SessionEvent:
    """持久化事件——到达模型的一切必须可从日志重建。"""
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    type: str = ""           # turn/start, step/start, tool/call, tool/result, user/message, assistant/message, turn/end
    data: dict = field(default_factory=dict)


class SessionLog:
    """append-only 事件日志。DSH 不变量：model-visible means logged。"""

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._events: list[SessionEvent] = []

    def append(self, event_type: str, data: dict | None = None) -> SessionEvent:
        ev = SessionEvent(type=event_type, data=data or {})
        self._events.append(ev)
        return ev

    def events(self) -> list[SessionEvent]:
        return list(self._events)

    def derive_messages(self) -> list[dict]:
        """从日志投影模型可见的消息序列（DSH deriveMessages 简化版）。"""
        msgs = []
        for ev in self._events:
            if ev.type in ("user/message", "assistant/message"):
                msgs.append({"role": ev.data.get("role", "user"),
                             "content": ev.data.get("content", "")})
            elif ev.type == "tool/result":
                msgs.append({"role": "tool", "content": ev.data.get("content", ""),
                             "tool_name": ev.data.get("name", "")})
            elif ev.type == "plugin/message":
                msgs.append({"role": "user", "content": ev.data.get("content", ""),
                             "source": "plugin"})
        return msgs

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps({"ts": e.ts, "type": e.type, "data": e.data},
                                    ensure_ascii=False) for e in self._events)


# ============ Data Types ============

@dataclass
class ToolDefinition:
    """工具定义（DSH capability seam 的 Service Definition 角色）。"""
    name: str
    description: str
    input_schema: dict
    execute_fn: Callable[[dict], str]
    timeout_ms: int | None = None


@dataclass
class ToolExecution:
    """一次工具调用的执行上下文。"""
    name: str
    arguments: dict
    agent_id: str = ""
    signal_cancelled: bool = False


@dataclass
class PreToolDecision:
    kind: str = "allow"          # allow | deny
    reason: str = ""


@dataclass
class ToolExecutionResult:
    content: str
    is_error: bool = False
    error_code: str = ""
    tokens: int = 0


@dataclass
class PostToolDecision:
    kind: str = "accept"         # accept | block | replace
    feedback: str = ""
    additional_contexts: list[dict] = field(default_factory=list)
    replacement: ToolExecutionResult | None = None


@dataclass
class PreStepDecision:
    kind: str = "enter"          # enter | reject
    messages: list[dict] = field(default_factory=list)


# ============ Tool Registry + Guard Pipeline ============

class ToolRegistry:
    """工具注册表 + 受守卫的执行管道。

    借鉴 DSH 的 ctx.tools：注册工具、组装 schema、通过 hook 管道执行。
    Guard（pre-execute / execute / post-execute 监听器）可组合，不耦合工具。

    Hook 签名：
    - pre_execute(exec_ctx) -> PreToolDecision | None（可否决）
    - execute(exec_ctx, next_fn) -> ToolExecutionResult（around-dispatch：timeout/retry）
    - post_execute(exec_ctx, result) -> PostToolDecision | None（可 block/replace/追加）
    - pre_step(messages) -> PreStepDecision | None（可 reject 或 rewrite）
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._pre_execute: list[Callable] = []
        self._execute_wrappers: list[Callable] = []
        self._post_execute: list[Callable] = []
        self._pre_step: list[Callable] = []

    def register(self, tool: ToolDefinition):
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def schemas(self) -> list[dict]:
        """组装所有工具的 schema（供 prompt assembly）。"""
        return [{"name": t.name, "description": t.description,
                 "input_schema": t.input_schema}
                for t in self._tools.values()]

    def on_pre_execute(self, fn):
        self._pre_execute.append(fn)

    def on_execute(self, fn):
        """注册 around-dispatch wrapper：fn(exec_ctx, next_fn) -> result。"""
        self._execute_wrappers.append(fn)

    def on_post_execute(self, fn):
        self._post_execute.append(fn)

    def on_pre_step(self, fn):
        self._pre_step.append(fn)

    def execute(self, exec_ctx: ToolExecution, session_log: SessionLog) -> ToolExecutionResult:
        """通过完整管道执行一次工具调用。

        管道顺序（对应 DSH tool-execution-pipeline）：
        1. tool/call 事件记入 session log
        2. tools/pre-execute 瀑布（hook/permission）
        3. tools/execute 瀑布（around-dispatch：timeout/retry）
        4. tool body 执行
        5. tools/post-execute 瀑布（accept/block/replace/add context）
        6. tool/result 事件记入 session log
        """
        session_log.append("tool/call", {"name": exec_ctx.name,
                                          "arguments": exec_ctx.arguments})
        tool = self._tools.get(exec_ctx.name)
        if tool is None:
            result = ToolExecutionResult(content=f"[ERROR 未知工具: {exec_ctx.name}]",
                                         is_error=True, error_code="UNKNOWN_TOOL")
            session_log.append("tool/result", {"name": exec_ctx.name,
                                                "content": result.content, "is_error": True})
            return result

        # 1. pre-execute 瀑布
        denied = None
        for hook in self._pre_execute:
            decision = hook(exec_ctx)
            if decision and decision.kind == "deny":
                denied = decision
                break

        if denied:
            result = ToolExecutionResult(content=f"[DENIED] {denied.reason}",
                                          is_error=True, error_code="DENIED")
            self._run_post_execute(exec_ctx, result, session_log)
            session_log.append("tool/result", {"name": exec_ctx.name, "content": result.content,
                                                "is_error": True, "denied": denied.reason})
            return result

        # 2. execute 瀑布（around-dispatch）
        def base_execute(ctx: ToolExecution) -> ToolExecutionResult:
            try:
                content = tool.execute_fn(ctx.arguments)
                return ToolExecutionResult(content=content)
            except Exception as e:
                return ToolExecutionResult(
                    content=f"[ERROR] {type(e).__name__}: {str(e)[:200]}",
                    is_error=True, error_code="EXECUTION_ERROR")

        chain = base_execute
        for wrapper in reversed(self._execute_wrappers):
            chain = _wrap(wrapper, chain)
        result = chain(exec_ctx)

        # 3. post-execute 瀑布
        self._run_post_execute(exec_ctx, result, session_log)

        # 4. tool/result 事件
        session_log.append("tool/result", {"name": exec_ctx.name, "content": result.content[:500],
                                           "is_error": result.is_error, "tokens": result.tokens})
        return result

    def _run_post_execute(self, exec_ctx: ToolExecution, result: ToolExecutionResult,
                          session_log: SessionLog):
        """执行 post-execute 瀑布（denied 调用也流过）。"""
        for hook in self._post_execute:
            decision = hook(exec_ctx, result)
            if decision is None:
                continue
            if decision.kind == "block":
                result.content = f"[BLOCKED] {decision.feedback}"
                result.is_error = True
                result.error_code = "BLOCKED"
            elif decision.kind == "replace" and decision.replacement:
                result.content = decision.replacement.content
                result.is_error = decision.replacement.is_error
                result.error_code = decision.replacement.error_code
            if decision.additional_contexts:
                for ctx_msg in decision.additional_contexts:
                    session_log.append("plugin/message", ctx_msg)

    def run_pre_step(self, messages: list[dict]) -> tuple[str, list[dict]]:
        """执行 pre-step 瀑布，返回 (decision_kind, messages)。"""
        for hook in self._pre_step:
            decision = hook(messages)
            if decision and decision.kind == "reject":
                return "reject", []
            if decision and decision.kind == "enter" and decision.messages:
                messages = decision.messages
        return "enter", messages


def _wrap(wrapper: Callable, next_fn: Callable) -> Callable:
    """构造 execute 瀑布链：wrapper(exec_ctx, next_fn) -> result。

    wrapper 调 next_fn(exec_ctx) 委托给下一层（DSH 的 next() 模式）。
    """
    def chained(exec_ctx: ToolExecution) -> ToolExecutionResult:
        return wrapper(exec_ctx, next_fn)
    return chained

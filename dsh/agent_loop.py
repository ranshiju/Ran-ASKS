"""agent_loop.py — DSH cockpit 的 turn/step 驱动循环。

借鉴 DSH 的 turn flow：
  turn/start → claim input → assemble prompt+schemas
  → agent/pre-step(reject|enter) → step/start
  → model request → tool/call* → tools/pre-execute → tools/execute
    → tools/post-execute → tool/result*
  → step/end → agent/turn-stopping → turn/end

API 模式：LLM 输出决策 JSON(discover/read/answer)，程序通过 hook 管道执行。
Agent 模式：返回 agent_required 交接，由外部 agent 决定下一步。
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
sys.path.insert(0, str(SCRIPTS))

from dsh.harness import (
    ToolRegistry, SessionLog, ToolExecution, ToolExecutionResult,
    PreToolDecision, PostToolDecision,
)
from dsh.tools import build_tools
from dsh.ingest_tools import build_ingest_tools
from dsh.guards.repeat_tool_reminder import RepeatToolReminder
from dsh.guards.timeout_policy import TimeoutPolicy
from dsh.guards.citation_guard import CitationGuard
from dsh.guards.ingest_guard import IngestGuard


@dataclass
class TurnResult:
    session_id: str
    answer: str = ""
    citations: list[str] = field(default_factory=list)
    citation_check: dict = field(default_factory=dict)
    rounds: int = 0
    handoff: dict | None = None
    snapshot: dict = field(default_factory=dict)


@dataclass
class IngestTurnResult:
    session_id: str
    status: str = "completed"
    items: list = field(default_factory=list)
    handoff: dict | None = None
    snapshot: dict = field(default_factory=dict)


class AgentLoop:
    """DSH cockpit 的 agent 循环。

    组装工具注册表 + guard 管道 + session log，驱动 turn/step 循环。
    """

    def __init__(self, mode: str = "agent"):
        self.mode = mode
        self.registry = ToolRegistry()
        self.session_log = SessionLog()
        self.repeat_guard = RepeatToolReminder()
        self.timeout_guard = TimeoutPolicy()
        self.citation_guard = CitationGuard()
        self.max_rounds = 5
        self._setup_registry()

    def _setup_registry(self):
        """注册工具 + 挂载 guard。"""
        for tool in build_tools():
            self.registry.register(tool)
        # pre-execute: (此处可加 permission/sandbox guard)
        # execute: timeout wrapper
        self.registry.on_execute(self.timeout_guard.on_execute)
        # post-execute: repeat-reminder + citation tracking
        self.registry.on_post_execute(self.repeat_guard.on_post_execute)
        self.registry.on_post_execute(self.citation_guard.on_post_execute)

    def _build_prompt(self, query: str, round_num: int, last_results: list) -> str:
        """组装 LLM 提示（含工具 schema + 状态上下文）。"""
        schemas = self.registry.schemas()
        parts = [
            f"查询: {query}",
            f"轮次: {round_num}/{self.max_rounds}",
            f"已读来源: {self.citation_guard.read_sources}",
            "",
            "可用工具:",
        ]
        for s in schemas:
            properties = (s.get("input_schema") or {}).get("properties", {})
            required = set((s.get("input_schema") or {}).get("required", []))
            args = ", ".join(
                f"{name}{'*' if name in required else ''}" for name in properties
            )
            parts.append(f"  - {s['name']}({args}): {s['description']}")
        if last_results:
            parts.append("\n上轮结果:")
            for r in last_results:
                parts.append(f"  - {r.get('name', '?')}({r.get('arguments', {})}): {r.get('content', '')[:200]}")
        parts.append("")
        parts.append("输出 JSON:")
        parts.append('  {"decision": "discover|read|answer",')
        parts.append('   "plan": [{"action": "工具名", "input": {...}, "reason": "..."}],')
        parts.append('   "answer": "回答文本",')
        parts.append('   "citations": ["raw路径或locator"]}')
        parts.append("discover=发现候选, read=核验(read_raw的locator会记入已读来源), answer=回答")
        parts.append("answer的citations必须全部来自已读来源")
        return "\n".join(parts)

    def _decision_schema(self, obj) -> bool:
        """校验 LLM 返回的决策 JSON。"""
        if not isinstance(obj, dict):
            return False
        decision = obj.get("decision")
        if decision not in ("discover", "read", "answer"):
            return False
        if decision == "answer":
            return isinstance(obj.get("answer", ""), str)
        plan = obj.get("plan")
        return isinstance(plan, list) and len(plan) > 0

    def run(self, query: str, llm_call_fn: Callable | None = None) -> TurnResult:
        """执行完整 turn 循环。

        llm_call_fn(prompt) -> dict（含 'parsed' 和 'status'）。
        如果 llm_call_fn 为 None 或返回 agent_required，返回 handoff。
        """
        self.session_log.append("turn/start", {"query": query})
        self.session_log.append("user/message", {"role": "user", "content": query})

        if self.mode != "api" or llm_call_fn is None:
            self.session_log.append("turn/end", {"reason": "agent_required"})
            return TurnResult(session_id=self.session_log.session_id,
                              handoff={"status": "agent_required", "mode": "agent"})

        last_results = []
        for round_num in range(1, self.max_rounds + 1):
            # pre-step（仅首轮检查新用户消息，续轮不重置重复链）
            if round_num == 1:
                messages = [{"role": "user", "content": query}]
                decision_kind, messages = self.registry.run_pre_step(messages)
                if decision_kind == "reject":
                    break
                self.repeat_guard.on_pre_step(messages)

            self.session_log.append("step/start", {"round": round_num})
            prompt = self._build_prompt(query, round_num, last_results)

            result = llm_call_fn(prompt)
            if result.get("status") == "agent_required":
                self.session_log.append("turn/end", {"reason": "agent_required"})
                return TurnResult(session_id=self.session_log.session_id,
                                  handoff=result, rounds=round_num)

            parsed = result.get("parsed") or {}
            self.session_log.append("assistant/message",
                                    {"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})

            decision = parsed.get("decision", "answer")
            plan = parsed.get("plan", [])
            answer = parsed.get("answer", "")
            citations = parsed.get("citations", [])

            if decision == "answer":
                citation_check = self.citation_guard.check(citations)
                self.session_log.append("turn/end", {"reason": "answered",
                                          "citation_check": citation_check.__dict__})
                return TurnResult(
                    session_id=self.session_log.session_id,
                    answer=answer, citations=citations,
                    citation_check=citation_check.__dict__,
                    rounds=round_num,
                    snapshot=self._snapshot())

            # 执行 plan 中的工具调用
            round_results = []
            for step in plan:
                tool_name = step.get("action", "")
                tool_input = step.get("input", {})
                exec_ctx = ToolExecution(name=tool_name, arguments=tool_input,
                                         agent_id=self.session_log.session_id)
                tool_result = self.registry.execute(exec_ctx, self.session_log)
                round_results.append({"name": tool_name, "arguments": tool_input,
                                      "content": tool_result.content[:500],
                                      "is_error": tool_result.is_error})
            last_results = round_results
            self.session_log.append("step/end", {"round": round_num, "executed": len(round_results)})

        self.session_log.append("turn/end", {"reason": "loop_exhausted"})
        return TurnResult(session_id=self.session_log.session_id,
                          snapshot=self._snapshot(), rounds=self.max_rounds)

    def _snapshot(self) -> dict:
        return {"read_sources": self.citation_guard.read_sources,
                "events": len(self.session_log.events()),
                "tool_names": self.registry.names()}


class IngestAgentLoop:
    """DSH 摄入专用 loop：不做 LLM 决策，只按确定性计划调用 ingest 工具。

    摄入状态机仍归底层 ingest_* 脚本所有；这里只负责 guard + session log +
    工具执行，保证 DSH 层不重写摄入状态、不直接写 raw/wiki/graph.db。
    """

    def __init__(self, mode: str = "agent"):
        self.mode = mode
        self.registry = ToolRegistry()
        self.session_log = SessionLog()
        self.ingest_guard = IngestGuard()
        self.last_structured: dict | None = None
        self._setup_registry()

    def _setup_registry(self):
        for tool in build_ingest_tools():
            self.registry.register(tool)
        self.registry.on_pre_execute(self.ingest_guard.on_pre_execute)

    def _parse_structured(self, content: str) -> dict | None:
        """从底层脚本输出中提取最后一个工作流结果 JSON。

        用 raw_decode 而非正则：graph_report 含多层嵌套，
        一层正则无法完整匹配，导致 structured 为 null。领域对象也可能带
        ``status: active/current``，不能把它们误当成摄入终态。
        """
        if not content:
            return None
        decoder = json.JSONDecoder()
        fallback = None
        terminal = None
        saw_status = False
        workflow_statuses = {
            "completed", "duplicate_found", "agent_required", "failed",
            "type_mismatch", "classification_required", "bibliographic_review_required",
            "validation_error", "partial", "error",
        }
        cursor = 0
        while cursor < len(content):
            idx = content.find("{", cursor)
            if idx < 0:
                break
            try:
                obj, end = decoder.raw_decode(content[idx:])
            except json.JSONDecodeError:
                cursor = idx + 1
                continue
            cursor = idx + end
            if not isinstance(obj, dict):
                continue
            if fallback is None:
                fallback = obj
            if "status" not in obj:
                continue
            saw_status = True
            if str(obj.get("status", "")).lower() in workflow_statuses:
                terminal = obj
        if terminal is not None:
            return terminal
        return None if saw_status else fallback

    def _execute(self, name: str, arguments: dict) -> str:
        self.session_log.append("turn/start", {"ingest_tool": name, "mode": self.mode})
        result = self.registry.execute(
            ToolExecution(name=name, arguments=arguments,
                          agent_id=self.session_log.session_id),
            self.session_log,
        )
        structured = self._parse_structured(result.content)
        self.last_structured = structured
        self.session_log.append(
            "ingest/result",
            {"tool": name, "is_error": result.is_error, "structured": structured},
        )
        self.session_log.append("turn/end", {"reason": "tool_completed" if not result.is_error else "tool_error"})
        return result.content

    def execute(self, name: str, arguments: dict | None = None) -> str:
        """按工具名执行任意已注册 ingest 工具。"""
        if name not in self.registry.names():
            self.last_structured = None
            return f"[ERROR 未注册 ingest 工具: {name}]"
        return self._execute(name, arguments or {})

    def _status_from_last(self) -> tuple[str, dict | None]:
        """从最近一次执行结果推导 DSH 工作流状态。"""
        if self.last_structured:
            status = str(self.last_structured.get("status", "")).lower()
            if status == "agent_required":
                return "agent_required", {
                    "status": "agent_required",
                    "transaction_id": self.last_structured.get("transaction_id", ""),
                    "message": self.last_structured.get("message", ""),
                    "prompt": self.last_structured.get("prompt", ""),
                    "write_to": self.last_structured.get("write_to", ""),
                    "pipeline_plan": self.last_structured.get("pipeline_plan", []),
                }
            if status == "failed":
                return "failed", dict(self.last_structured)
            if status in {
                "bibliographic_review_required", "validation_error",
                "classification_required", "type_mismatch", "partial",
            }:
                return status, dict(self.last_structured)
            if status == "duplicate_found":
                return "duplicate_found", dict(self.last_structured)
            if status == "completed":
                return "completed", dict(self.last_structured)
        return "completed", None

    def _make_turn_result(self, fallback: str, reason: str) -> IngestTurnResult:
        status, handoff = self._status_from_last()
        if status in {
            "agent_required", "failed", "duplicate_found", "bibliographic_review_required",
            "validation_error", "classification_required", "type_mismatch", "partial",
        }:
            self.session_log.append("ingest/handoff", {"status": status, "reason": reason, "handoff": handoff})
            self.session_log.append("turn/end", {"reason": status})
            return IngestTurnResult(
                session_id=self.session_log.session_id,
                status=status,
                handoff=handoff,
                snapshot=self._snapshot(),
            )
        maintenance = (handoff or {}).get("maintenance") if handoff else None
        if status == "completed" and isinstance(maintenance, dict) and (
                maintenance.get("status") in {"agent_required", "error"}):
            maintenance_handoff = {
                "status": "completed",
                "file_status": "completed",
                "maintenance": maintenance,
            }
            self.session_log.append("ingest/handoff", {
                "status": status, "reason": "post_ingest_maintenance",
                "handoff": maintenance_handoff,
            })
            self.session_log.append("turn/end", {"reason": "post_ingest_maintenance"})
            return IngestTurnResult(
                session_id=self.session_log.session_id,
                status=status,
                handoff=maintenance_handoff,
                snapshot=self._snapshot(),
            )
        self.session_log.append("turn/end", {"reason": reason})
        return IngestTurnResult(
            session_id=self.session_log.session_id,
            status=status,
            snapshot=self._snapshot(),
        )

    def run_inbox(self, file: str | None = None) -> IngestTurnResult:
        """执行 inbox 摄入工作流：dry-run → 单文件或全量执行 → 状态推导。"""
        self.session_log.append("turn/start", {"workflow": "ingest_inbox", "file": file, "mode": self.mode})
        self.session_log.append("ingest/plan", {"phase": "dry_run"})
        self.dry_run()
        self.session_log.append("ingest/plan", {"phase": "run", "file": file or "all"})
        if file:
            content = self.run_file(file)
        else:
            content = self.run_all()
        if content.startswith("[ERROR") or content.startswith("[DENIED]"):
            self.session_log.append("ingest/handoff", {"status": "failed", "reason": content[:200]})
            self.session_log.append("turn/end", {"reason": "tool_error"})
            return IngestTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={"status": "failed", "errors": [content[:500]]},
                snapshot=self._snapshot(),
            )
        return self._make_turn_result(content, "workflow_completed")

    def run_paper_pdf(self, pdf: str) -> IngestTurnResult:
        """执行单篇论文摄入工作流并推导 agent_required/failed/duplicate/completed。"""
        self.session_log.append("turn/start", {"workflow": "ingest_paper_pdf", "pdf": pdf, "mode": self.mode})
        content = self.ingest_paper_pdf(pdf)
        if content.startswith("[ERROR") or content.startswith("[DENIED]"):
            self.session_log.append("ingest/handoff", {"status": "failed", "reason": content[:200]})
            self.session_log.append("turn/end", {"reason": "tool_error"})
            return IngestTurnResult(
                session_id=self.session_log.session_id,
                status="failed",
                handoff={"status": "failed", "errors": [content[:500]]},
                snapshot=self._snapshot(),
            )
        return self._make_turn_result(content, "workflow_completed")

    def _snapshot(self) -> dict:
        return {"events": len(self.session_log.events()), "tool_names": self.registry.names()}

    def dry_run(self) -> str:
        return self._execute("ingest_inbox_dry_run", {})

    def run_file(self, file: str) -> str:
        return self._execute("ingest_inbox_run_file", {"file": file})

    def run_all(self) -> str:
        return self._execute("ingest_inbox_run", {})

    def ingest_paper_inbox(self) -> str:
        return self._execute("ingest_paper_inbox", {})

    def ingest_paper_pdf(self, pdf: str) -> str:
        return self._execute("ingest_paper_pdf", {"pdf": pdf})

    def resume_paper(self, txn: str) -> str:
        return self._execute("ingest_paper_resume", {"txn": txn})

    def ingest_meeting(self, file: str, subproject: str | None = None) -> str:
        args = {"file": file}
        if subproject:
            args["subproject"] = subproject
        return self._execute("ingest_meeting_txt", args)

    def resume_meeting(self, txn: str) -> str:
        return self._execute("ingest_meeting_resume", {"txn": txn})

    def ingest_document(self, file: str, subproject: str | None = None,
                        document_type: str | None = None) -> str:
        args = {"file": file}
        if subproject:
            args["subproject"] = subproject
        if document_type:
            args["document_type"] = document_type
        return self._execute("ingest_document_file", args)

    def re_ingest_raw(self, raw: str) -> str:
        return self._execute("re_ingest_raw", {"raw": raw})

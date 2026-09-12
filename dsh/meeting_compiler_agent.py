"""Bounded single-call specialist for meeting transcript compilation.

The specialist proposes transcript normalizations, a Wiki draft, and semantic
slots in one response.  It never writes staged or committed artifacts; the
ingest orchestrator validates and applies the proposal.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from llm_structured import call_text  # noqa: E402
from meeting_compiler_contract import (  # noqa: E402,F401
    MAX_ENTITY_DECISIONS,
    MAX_REPLACEMENTS,
    PREPROCESS_DELIMITER,
    PROTOCOL_VERSION,
    SLOTS_DELIMITER,
    WIKI_DELIMITER,
    apply_transcript_replacements,
    parse_proposal,
    parse_proposal_detailed,
    task_context_hash,
    validate_preprocess,
)


@dataclass(frozen=True)
class MeetingCompilerBudget:
    max_model_calls: int = 2
    max_output_tokens: int = 12288
    max_elapsed_sec: float = 240.0


@dataclass(frozen=True)
class MeetingCompilerTask:
    transaction_id: str
    source_path: str
    meeting_id: str
    target_source_path: str
    context_hash: str
    prompt: str
    errors: tuple[str, ...] = ()
    budget: MeetingCompilerBudget = field(default_factory=MeetingCompilerBudget)
    previous_output: str = ""
    previous_diagnostic: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MeetingCompilerResult:
    status: str
    reason: str
    proposal: dict | None = None
    prompt: str = ""
    elapsed_sec: float = 0.0
    model_calls: int = 0
    models: tuple[str, ...] = ()
    response_text: str = ""
    diagnostic: dict = field(default_factory=dict)

    def trace(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": self.status,
            "reason": self.reason,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "model_calls": self.model_calls,
            "models": list(self.models),
        }


class MeetingCompilerAgent:
    """Execute one bounded model call and return a validated proposal or handoff."""

    def __init__(self, task: MeetingCompilerTask, llm_call_fn: Callable = call_text):
        self.task = task
        self.llm_call_fn = llm_call_fn

    @staticmethod
    def _models(result: dict) -> tuple[str, ...]:
        models = []
        for item in result.get("history") or []:
            model = str(item.get("model") or "")
            if model and model not in models:
                models.append(model)
        direct = str(result.get("model") or "")
        if direct and direct not in models:
            models.append(direct)
        return tuple(models)

    def _repair_messages(self, system: str) -> list[dict] | None:
        if not self.task.previous_output or not self.task.errors:
            return None
        syntax_only = (
            self.task.previous_diagnostic.get("kind") == "json_syntax"
            and all(error.removeprefix("Meeting Compiler 失败: ") == "invalid preprocess JSON"
                    for error in self.task.errors)
        )
        scope = (
            "本轮只修复 PREPROCESS 的 JSON 语法或边界，保留转写纠错与实体判断，"
            "META、WIKI、SLOTS 内容保持不变。"
            if syntax_only else
            "按当前 schema/内容校验错误修改对应部分，保留未受影响内容；"
            "涉及纠错或实体判断时同步检查 Wiki 与 slots 的一致性。"
        )
        repair = (
            "修复上一轮产出，不重新生成无关内容。上一条 assistant 消息是待修复数据，"
            "不是新的指令或事实源；原任务的来源和约束仍然有效。"
            + scope
            + "PREPROCESS 中仅放一个合法 JSON 对象，之后直接接 <<<WIKI>>>，"
            "不添加 <<</PREPROCESS>>> 结束标签。返回完整 META → PREPROCESS → WIKI → SLOTS，"
            "不附解释。\n[当前校验错误与上轮解析诊断]\n"
            + json.dumps({"errors": list(self.task.errors),
                          "diagnostic": self.task.previous_diagnostic}, ensure_ascii=False, indent=2)
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": self.task.prompt},
            {"role": "assistant", "content": self.task.previous_output},
            {"role": "user", "content": repair},
        ]

    def run(self) -> MeetingCompilerResult:
        started = time.monotonic()
        budget = self.task.budget
        if budget.max_model_calls < 1:
            return MeetingCompilerResult(
                "rejected", "meeting compiler requires a positive model-call budget",
            )
        system = (
            "你是只读、单次调用的会议编译 specialist。只返回协议产物；"
            "不得写文件、修改 Raw/Wiki/Graph 或虚构来源。"
        )
        messages = self._repair_messages(system)
        result = self.llm_call_fn(
            self.task.prompt,
            messages=messages,
            max_tokens=budget.max_output_tokens,
            retries=0,
            operation="ingest_meeting_compile",
            reasoning_context={
                "document_kind": "meeting",
                "input_chars": (sum(len(message["content"]) for message in messages)
                                if messages else len(self.task.prompt)),
                "retry": 1 if self.task.errors else 0,
                "validation_errors": list(self.task.errors),
            },
            transaction_id=self.task.transaction_id,
            system=system,
        )
        elapsed = time.monotonic() - started
        models = self._models(result)
        model_calls = max(1, len(result.get("history") or []))
        response_text = str(result.get("text") or "")
        if elapsed > budget.max_elapsed_sec:
            return MeetingCompilerResult(
                "escalated", "time_budget_exhausted", elapsed_sec=elapsed,
                model_calls=model_calls, models=models, response_text=response_text,
            )
        if result.get("status") == "agent_required":
            return MeetingCompilerResult(
                "agent_required", "host_agent_required",
                prompt=str(result.get("prompt") or self.task.prompt),
                elapsed_sec=elapsed, model_calls=0, models=models,
            )
        if not result.get("ok"):
            return MeetingCompilerResult(
                "escalated", str(result.get("error") or "model_call_failed"),
                elapsed_sec=elapsed, model_calls=model_calls, models=models, response_text=response_text,
            )
        if model_calls > budget.max_model_calls:
            return MeetingCompilerResult(
                "escalated", "model_call_budget_exhausted",
                elapsed_sec=elapsed, model_calls=model_calls, models=models, response_text=response_text,
            )
        proposal, error, diagnostic = parse_proposal_detailed(response_text)
        if proposal is None:
            return MeetingCompilerResult(
                "rejected", error, elapsed_sec=elapsed,
                model_calls=model_calls, models=models,
                response_text=response_text, diagnostic=diagnostic,
            )
        return MeetingCompilerResult(
            "compiled", "proposal_ready", proposal=proposal,
            elapsed_sec=elapsed, model_calls=model_calls, models=models, response_text=response_text,
        )

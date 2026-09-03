"""Bounded specialist agent for unresolved staged semantic-slot issues.

The agent can inspect only caller-provided staged text and returns a typed
proposal. It never receives a write or commit tool.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dsh.harness import SessionLog, ToolDefinition, ToolExecution, ToolRegistry
from llm_structured import call_json


PROTOCOL_VERSION = "semantic-patch-v1"


@dataclass(frozen=True)
class AgentBudget:
    max_turns: int = 3
    max_tool_calls: int = 2
    max_elapsed_sec: float = 120.0
    max_total_tokens: int = 12000
    max_output_tokens_per_turn: int = 1200
    max_model_calls: int = 6
    request_timeout_sec: float = 30.0


@dataclass(frozen=True)
class AgentTaskEnvelope:
    task_id: str
    transaction_id: str
    semantic_path: str
    wiki_path: str
    extract_dir: str
    context_hash: str
    issues: tuple[dict, ...]
    budget: AgentBudget = field(default_factory=AgentBudget)


@dataclass
class AgentResult:
    status: str
    task_id: str
    proposal: dict | None = None
    reason: str = ""
    turns: int = 0
    tool_calls: int = 0
    total_tokens: int = 0
    elapsed_sec: float = 0.0
    repeated_actions: int = 0
    api_calls: int = 0
    models: list[str] = field(default_factory=list)

    def trace(self) -> dict:
        data = asdict(self)
        data.pop("proposal", None)
        return data


def proposal_schema(value) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "protocol_version", "review_status", "patches", "review_notes",
    }:
        return False
    if value.get("protocol_version") != PROTOCOL_VERSION:
        return False
    if value.get("review_status") not in {"patched", "manual_required"}:
        return False
    if not isinstance(value.get("review_notes"), list) or not all(
            isinstance(note, str) for note in value["review_notes"]):
        return False
    patches = value.get("patches")
    if not isinstance(patches, list) or len(patches) > 32:
        return False
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {
            "issue_id", "action", "replacement_lines",
        }:
            return False
        if not isinstance(patch.get("issue_id"), str):
            return False
        if patch.get("action") not in {"replace", "abstain"}:
            return False
        lines = patch.get("replacement_lines")
        if not isinstance(lines, list) or len(lines) > 1 or not all(
                isinstance(line, str) and line.strip() for line in lines):
            return False
        if patch["action"] == "replace" and len(lines) != 1:
            return False
        if patch["action"] == "abstain" and lines:
            return False
    return True


def decision_schema(value) -> bool:
    if not isinstance(value, dict) or value.get("decision") not in {"tool", "propose"}:
        return False
    if value["decision"] == "tool":
        return (
            set(value) == {"decision", "tool", "arguments", "reason"}
            and isinstance(value.get("tool"), str)
            and isinstance(value.get("arguments"), dict)
            and isinstance(value.get("reason"), str)
        )
    return set(value) == {"decision", "proposal"} and proposal_schema(value.get("proposal"))


def make_task_envelope(state: dict, issues: list[dict], semantic_text: str,
                       wiki_text: str, source_text: str,
                       budget: AgentBudget | None = None) -> AgentTaskEnvelope:
    transaction_id = str(state.get("transaction_id") or "")
    context_payload = {
        "transaction_id": transaction_id,
        "semantic_sha256": hashlib.sha256(semantic_text.encode()).hexdigest(),
        "wiki_sha256": hashlib.sha256(wiki_text.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
        "issues": issues,
    }
    context_hash = hashlib.sha256(
        json.dumps(context_payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    task_id = hashlib.sha256(f"{transaction_id}:{context_hash}".encode()).hexdigest()[:20]
    return AgentTaskEnvelope(
        task_id=task_id,
        transaction_id=transaction_id,
        semantic_path=str(state.get("semantic_path") or ""),
        wiki_path=str(state.get("wiki_path") or ""),
        extract_dir=str(state.get("extract_dir") or ""),
        context_hash=context_hash,
        issues=tuple(issues),
        budget=budget or AgentBudget(),
    )


def _bounded_excerpt(text: str, query: str, max_chars: int = 4000) -> str:
    if not text:
        return "[EMPTY]"
    query = query.strip().lower()
    if not query:
        return text[:max_chars]
    lower = text.lower()
    pos = lower.find(query)
    if pos < 0:
        return "[NO MATCH]"
    start = max(0, pos - max_chars // 2)
    end = min(len(text), start + max_chars)
    return text[start:end]


class SemanticRecoveryAgent:
    """Small observe/act loop with a read-only, task-local tool registry."""

    def __init__(self, envelope: AgentTaskEnvelope, semantic_text: str,
                 wiki_text: str, source_text: str,
                 llm_call_fn: Callable = call_json):
        self.envelope = envelope
        self.semantic_text = semantic_text
        self.wiki_text = wiki_text
        self.source_text = source_text
        self.llm_call_fn = llm_call_fn
        self.registry = ToolRegistry()
        self.session_log = SessionLog(session_id=envelope.task_id)
        self._issues = {issue["id"]: issue for issue in envelope.issues}
        self._setup_registry()

    def _setup_registry(self) -> None:
        self.registry.register(ToolDefinition(
            name="inspect_issue_context",
            description="一次读取一个或多个 issue 的语义行及 staged Wiki/source 有界证据。",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_ids": {
                        "type": "array", "items": {"type": "string"},
                        "minItems": 1, "maxItems": 8,
                    }
                },
                "required": ["issue_ids"],
            },
            execute_fn=self._inspect_issue_context,
        ))

    @staticmethod
    def _needles(issue: dict) -> list[str]:
        values = [str(issue.get("observed") or ""), str(issue.get("line") or "")]
        line_parts = [part.strip() for part in str(issue.get("line") or "").split("|")]
        values.extend(reversed(line_parts))
        tokens = []
        for value in values:
            tokens.extend(part for part in re.split(r"[\s,，。；;、|/()（）]+", value) if len(part) >= 3)
        return [value for value in [*values, *tokens] if value]

    @staticmethod
    def _best_excerpt(text: str, needles: list[str], max_chars: int) -> str:
        for needle in needles:
            excerpt = _bounded_excerpt(text, needle, max_chars)
            if excerpt != "[NO MATCH]":
                return excerpt
        return "[NO MATCH]"

    def _inspect_issue_context(self, args: dict) -> str:
        issue_ids = args.get("issue_ids")
        if not isinstance(issue_ids, list) or not issue_ids or len(issue_ids) > 8:
            return "[ERROR issue_ids must contain 1-8 IDs]"
        if len(issue_ids) != len(set(str(item) for item in issue_ids)):
            return "[ERROR duplicate issue_ids]"
        per_issue = max(500, 6000 // len(issue_ids))
        contexts = []
        for raw_issue_id in issue_ids:
            issue_id = str(raw_issue_id)
            issue = self._issues.get(issue_id)
            if issue is None:
                return f"[ERROR unknown issue_id: {issue_id}]"
            needle = str(issue.get("line") or issue.get("observed") or "").strip()
            matching_lines = [line for line in self.semantic_text.splitlines()
                              if needle and (needle in line or line.strip() in needle)]
            needles = self._needles(issue)
            contexts.append({
                "issue": issue,
                "matching_semantic_lines": matching_lines[:8],
                "wiki_excerpt": self._best_excerpt(self.wiki_text, needles, per_issue),
                "source_excerpt": self._best_excerpt(self.source_text, needles, per_issue),
            })
        return json.dumps({"contexts": contexts}, ensure_ascii=False)

    def _prompt(self) -> str:
        schemas = self.registry.schemas()
        public_task = {
            "protocol_version": PROTOCOL_VERSION,
            "task_id": self.envelope.task_id,
            "context_hash": self.envelope.context_hash,
            "issues": list(self.envelope.issues),
            "tools": schemas,
        }
        return f"""你是摄入流程中的受限语义恢复 specialist。你的唯一目标是为当前 issue 提出最小局部修补。

任务：
{json.dumps(public_task, ensure_ascii=False)}

每轮只输出以下两种 JSON 之一：
1. 调查：{{"decision":"tool","tool":"工具名","arguments":{{...}},"reason":"为什么需要"}}
2. 提案：{{"decision":"propose","proposal":{{"protocol_version":"{PROTOCOL_VERSION}","review_status":"patched|manual_required","patches":[{{"issue_id":"issue-01","action":"replace|abstain","replacement_lines":["一行替换文本"]}}],"review_notes":[]}}}}

规则：每个 issue 必须且只能出现一次；每个 replace 只能返回一行；只修 issue 定位的行；证据不足必须 abstain。若需调查，优先一次 inspect 全部相关 issue；获得足够上下文后下一轮必须 propose，不得逐个读取同一上下文。不得请求写文件、修改 Raw/Wiki/Graph、生成 locator 或改变事务状态。"""

    @staticmethod
    def _token_count(result: dict) -> int:
        usage = result.get("usage") or {}
        return int(usage.get("total_tokens") or (
            int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
        ))

    def _result(self, status: str, reason: str, started: float, turns: int,
                tool_calls: int, total_tokens: int, repeated_actions: int,
                proposal: dict | None = None, api_calls: int = 0,
                models: list[str] | None = None) -> AgentResult:
        return AgentResult(
            status=status,
            task_id=self.envelope.task_id,
            proposal=proposal,
            reason=reason,
            turns=turns,
            tool_calls=tool_calls,
            total_tokens=total_tokens,
            elapsed_sec=round(time.monotonic() - started, 3),
            repeated_actions=repeated_actions,
            api_calls=api_calls,
            models=models or [],
        )

    def run(self) -> AgentResult:
        budget = self.envelope.budget
        started = time.monotonic()
        messages = [
            {"role": "system", "content": "你是只读调查、只产 typed proposal 的 bounded specialist。"},
            {"role": "user", "content": self._prompt()},
        ]
        seen_actions: set[str] = set()
        tool_calls = 0
        total_tokens = 0
        repeated_actions = 0
        api_calls = 0
        models: list[str] = []
        self.session_log.append("turn/start", {
            "task_id": self.envelope.task_id,
            "context_hash": self.envelope.context_hash,
        })

        for turn in range(1, budget.max_turns + 1):
            if time.monotonic() - started >= budget.max_elapsed_sec:
                return self._result("escalated", "time_budget_exhausted", started, turn - 1,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            if total_tokens >= budget.max_total_tokens:
                return self._result("escalated", "token_budget_exhausted", started, turn - 1,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            result = self.llm_call_fn(
                "semantic recovery",
                decision_schema,
                messages=messages,
                max_tokens=budget.max_output_tokens_per_turn,
                retries=0,
                operation="ingest_semantic_recovery",
                reasoning="standard",
                transaction_id=self.envelope.transaction_id,
                timeout_sec=budget.request_timeout_sec,
            )
            total_tokens += self._token_count(result)
            history = result.get("history") or []
            if history:
                api_calls += len(history)
                attempted_models = [str(item.get("model") or "") for item in history]
            else:
                attempted_models = [str(result.get("model") or "")]
            for model in attempted_models:
                if model and model not in models:
                    models.append(model)
            if total_tokens > budget.max_total_tokens:
                return self._result("escalated", "token_budget_exhausted", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            if api_calls > budget.max_model_calls:
                return self._result("escalated", "model_call_budget_exhausted", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            if time.monotonic() - started >= budget.max_elapsed_sec:
                return self._result("escalated", "time_budget_exhausted", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            if result.get("status") == "agent_required":
                return self._result("escalated", "api_agent_mode", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            if not result.get("ok"):
                return self._result("escalated", "model_call_failed", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            decision = result["parsed"]
            messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
            if decision["decision"] == "propose":
                proposal = decision["proposal"]
                expected = set(self._issues)
                actual = [patch["issue_id"] for patch in proposal["patches"]]
                if len(actual) != len(set(actual)) or set(actual) != expected:
                    return self._result("escalated", "proposal_issue_coverage", started, turn,
                                        tool_calls, total_tokens, repeated_actions,
                                        api_calls=api_calls, models=models)
                abstained = proposal["review_status"] == "manual_required" or any(
                    patch["action"] == "abstain" for patch in proposal["patches"]
                )
                return self._result("abstained" if abstained else "resolved",
                                    "evidence_insufficient" if abstained else "proposal_ready",
                                    started, turn, tool_calls, total_tokens,
                                    repeated_actions, proposal, api_calls, models)

            if tool_calls >= budget.max_tool_calls:
                return self._result("escalated", "tool_budget_exhausted", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            tool_name = decision["tool"]
            arguments = decision["arguments"]
            action_key = json.dumps([tool_name, arguments], ensure_ascii=False, sort_keys=True)
            if action_key in seen_actions:
                repeated_actions += 1
                return self._result("escalated", "repeated_action", started, turn,
                                    tool_calls, total_tokens, repeated_actions,
                                    api_calls=api_calls, models=models)
            seen_actions.add(action_key)
            execution = ToolExecution(
                name=tool_name,
                arguments=arguments,
                agent_id=self.envelope.task_id,
            )
            tool_result = self.registry.execute(execution, self.session_log)
            tool_calls += 1
            messages.append({
                "role": "tool",
                "tool_name": tool_name,
                "content": tool_result.content,
            })

        return self._result("escalated", "turn_budget_exhausted", started,
                            budget.max_turns, tool_calls, total_tokens, repeated_actions,
                            api_calls=api_calls, models=models)

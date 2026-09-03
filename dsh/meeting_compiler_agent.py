"""Bounded single-call specialist for meeting transcript compilation.

The specialist proposes transcript normalizations, a Wiki draft, and semantic
slots in one response.  It never writes staged or committed artifacts; the
ingest orchestrator validates and applies the proposal.
"""
from __future__ import annotations

import hashlib
import json
import re
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


PROTOCOL_VERSION = "meeting-compiler-v1"
PREPROCESS_DELIMITER = "<<<PREPROCESS>>>"
WIKI_DELIMITER = "<<<WIKI>>>"
SLOTS_DELIMITER = "<<<SLOTS>>>"
MAX_REPLACEMENTS = 64
MAX_ENTITY_DECISIONS = 128


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


@dataclass(frozen=True)
class MeetingCompilerResult:
    status: str
    reason: str
    proposal: dict | None = None
    prompt: str = ""
    elapsed_sec: float = 0.0
    model_calls: int = 0
    models: tuple[str, ...] = ()

    def trace(self) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "status": self.status,
            "reason": self.reason,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "model_calls": self.model_calls,
            "models": list(self.models),
        }


def task_context_hash(source_text: str, entity_candidates: dict, *, meeting_id: str,
                      target_source_path: str, errors: list[str] | None = None) -> str:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "entity_candidates": entity_candidates,
        "meeting_id": meeting_id,
        "target_source_path": target_source_path,
        "errors": errors or [],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _section(text: str, delimiter: str, next_delimiter: str | None) -> str:
    start = text.find(delimiter)
    if start < 0:
        return ""
    start += len(delimiter)
    end = text.find(next_delimiter, start) if next_delimiter else len(text)
    if end < 0:
        end = len(text)
    return text[start:end].strip()


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def _parse_meta(text: str) -> dict[str, str]:
    match = re.search(r"<<<META>>>\s*(.*?)\s*<<</META>>>", text, re.S)
    if not match:
        return {}
    values = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def validate_preprocess(value) -> bool:
    if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
        return False
    replacements = value.get("transcript_replacements")
    decisions = value.get("entity_resolutions")
    if not isinstance(replacements, list) or len(replacements) > MAX_REPLACEMENTS:
        return False
    if not isinstance(decisions, list) or len(decisions) > MAX_ENTITY_DECISIONS:
        return False
    seen = set()
    for item in replacements:
        if not isinstance(item, dict):
            return False
        original = item.get("original")
        replacement = item.get("replacement")
        reason = item.get("reason")
        if not all(isinstance(part, str) for part in (original, replacement, reason)):
            return False
        if not original.strip() or not replacement.strip() or original == replacement:
            return False
        if "\n" in original or "\n" in replacement or len(original) > 80 or len(replacement) > 160:
            return False
        if original in seen:
            return False
        seen.add(original)
    for item in decisions:
        if not isinstance(item, dict):
            return False
        if set(item) != {"mention", "canonical", "status", "reason"}:
            return False
        if item.get("status") not in {"resolved", "unchanged", "unresolved"}:
            return False
        if not all(isinstance(item.get(key), str) for key in item):
            return False
        if not item["mention"].strip() or not item["reason"].strip():
            return False
        if item["status"] == "resolved" and not item["canonical"].strip():
            return False
    return True


def parse_proposal(text: str) -> tuple[dict | None, str]:
    preprocess_text = _section(text, PREPROCESS_DELIMITER, WIKI_DELIMITER)
    wiki = _section(text, WIKI_DELIMITER, SLOTS_DELIMITER)
    slots = _section(text, SLOTS_DELIMITER, None)
    if not preprocess_text:
        return None, f"missing {PREPROCESS_DELIMITER} section"
    try:
        preprocess = json.loads(_strip_json_fence(preprocess_text))
    except json.JSONDecodeError:
        return None, "invalid preprocess JSON"
    if not validate_preprocess(preprocess):
        return None, "invalid meeting-compiler-v1 preprocess proposal"
    if not wiki:
        return None, f"missing {WIKI_DELIMITER} section"
    if not slots:
        return None, f"missing {SLOTS_DELIMITER} section"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "preprocess": preprocess,
        "meta": _parse_meta(text),
        "wiki_markdown": wiki,
        "semantic_slots": slots,
    }, ""


def apply_transcript_replacements(source_text: str, replacements: list[dict]) -> str:
    """Apply exact replacements against the original text in one non-cascading pass."""
    if not replacements:
        return source_text
    preprocess = {
        "protocol_version": PROTOCOL_VERSION,
        "transcript_replacements": replacements,
        "entity_resolutions": [],
    }
    if not validate_preprocess(preprocess):
        raise ValueError("invalid transcript replacements")
    mapping = {item["original"]: item["replacement"] for item in replacements}
    missing = [original for original in mapping if original not in source_text]
    if missing:
        raise ValueError("replacement source text not found: " + ", ".join(missing))
    pattern = re.compile("|".join(re.escape(value) for value in sorted(mapping, key=len, reverse=True)))
    matched = set(pattern.findall(source_text))
    if matched != set(mapping):
        raise ValueError("overlapping transcript replacements are not allowed")
    return pattern.sub(lambda match: mapping[match.group(0)], source_text)


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

    def run(self) -> MeetingCompilerResult:
        started = time.monotonic()
        budget = self.task.budget
        if budget.max_model_calls < 1:
            return MeetingCompilerResult(
                "rejected", "meeting compiler requires a positive model-call budget",
            )
        result = self.llm_call_fn(
            self.task.prompt,
            max_tokens=budget.max_output_tokens,
            retries=0,
            operation="ingest_meeting_compile",
            reasoning_context={
                "document_kind": "meeting",
                "input_chars": len(self.task.prompt),
                "retry": 1 if self.task.errors else 0,
                "validation_errors": list(self.task.errors),
            },
            transaction_id=self.task.transaction_id,
            system=(
                "你是只读、单次调用的会议编译 specialist。只返回协议产物；"
                "不得写文件、修改 Raw/Wiki/Graph 或虚构来源。"
            ),
        )
        elapsed = time.monotonic() - started
        models = self._models(result)
        model_calls = max(1, len(result.get("history") or []))
        if elapsed > budget.max_elapsed_sec:
            return MeetingCompilerResult(
                "escalated", "time_budget_exhausted", elapsed_sec=elapsed,
                model_calls=model_calls, models=models,
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
                elapsed_sec=elapsed, model_calls=model_calls, models=models,
            )
        if model_calls > budget.max_model_calls:
            return MeetingCompilerResult(
                "escalated", "model_call_budget_exhausted",
                elapsed_sec=elapsed, model_calls=model_calls, models=models,
            )
        proposal, error = parse_proposal(str(result.get("text") or ""))
        if proposal is None:
            return MeetingCompilerResult(
                "rejected", error, elapsed_sec=elapsed,
                model_calls=model_calls, models=models,
            )
        return MeetingCompilerResult(
            "compiled", "proposal_ready", proposal=proposal,
            elapsed_sec=elapsed, model_calls=model_calls, models=models,
        )

#!/usr/bin/env python3
"""Persist resumable state for one inbox intake transaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_PROTOCOL_VERSION = "ingest-state-v1"
RUNTIME_SUMMARY_VERSION = "ingest-runtime-summary-v1"
KNOWN_STATUSES = frozenset({
    "init", "dedup_check", "preprocess", "extract", "write_wiki", "write_slots",
    "bibliographic_review_required", "agent_required", "type_mismatch",
    "classification_required", "finalize", "propositions", "propositions_done",
    "prepared", "finalized", "graph_ready", "update_graph", "validate_graph",
    "finalize_tail", "completed", "duplicate_found", "failed", "validation_error",
})
RESUME_TRANSITIONS = {
    "failed": frozenset({"finalize", "graph_ready"}),
    "agent_required": frozenset({
        "write_wiki", "write_slots", "finalize", "propositions", "graph_ready",
        "update_graph", "validate_graph", "finalize_tail", "bibliographic_review_required",
    }),
    "bibliographic_review_required": frozenset({"write_wiki", "agent_required"}),
}


def validate_status(state: dict) -> str:
    status = str(state.get("status") or "")
    if status not in KNOWN_STATUSES:
        raise ValueError(f"unknown ingest status: {status or '(empty)'}")
    return status


def transition(state: dict, target: str, *, reason: str,
               allowed_targets: set[str] | frozenset[str] | None = None,
               allowed_from: set[str] | frozenset[str] | None = None) -> None:
    """Apply a guarded non-linear recovery transition.

    Ordinary forward stages remain owned by their pipeline. This helper protects
    persisted resume pointers and handoff returns, where a bad string could skip
    validation or commit stages.
    """
    source = validate_status(state)
    target = str(target or "")
    if target not in KNOWN_STATUSES:
        raise ValueError(f"unknown ingest transition target: {target or '(empty)'}")
    if allowed_targets is not None and allowed_from is not None:
        raise ValueError("pass allowed_targets, not both recovery override names")
    # allowed_from was the original, misleading name. Keep it for old callers while
    # making the checked value explicit: this is a set of permitted target stages.
    override = allowed_targets if allowed_targets is not None else allowed_from
    permitted = frozenset(override) if override is not None else RESUME_TRANSITIONS.get(source, frozenset())
    if source != target and target not in permitted:
        raise ValueError(f"illegal ingest recovery transition: {source} -> {target}")
    state["status"] = target
    state["_pending_transition"] = {
        "protocol_version": STATE_PROTOCOL_VERSION,
        "from": source,
        "to": target,
        "reason": str(reason or "")[:160],
    }


def classify_failure(state: dict) -> dict | None:
    """Classify the current terminal/problem state for routing and metrics."""
    status = str(state.get("status") or "")
    errors = [str(item) for item in (state.get("errors") or [])]
    text = "\n".join(errors).lower()
    category = ""
    domain = "unknown"
    disposition = "stop_and_inspect"
    next_action = "inspect_transaction"
    retryable = False
    owner = "program"
    if status == "bibliographic_review_required":
        category, domain, disposition, owner, next_action = (
            "human_policy_decision", "policy", "human_decision", "human",
            "complete_bibliographic_review",
        )
    elif "403" in text or "forbidden" in text or "authentication" in text:
        category, domain, disposition, owner, next_action = (
            "api_auth_or_permission", "api", "configuration_fix", "configuration",
            "repair_api_configuration",
        )
    elif "429" in text or "too many requests" in text:
        category, domain, disposition, retryable, next_action = (
            "api_rate_limit", "api", "bounded_client_retry", True,
            "resume_after_provider_recovers",
        )
    elif any(marker in text for marker in (
            "timeout", "timed out", "urlerror", "name or service not known",
            "nodename nor servname", "connection reset")):
        category, domain, disposition, retryable, next_action = (
            "api_network_transient", "api", "bounded_client_retry", True,
            "resume_after_network_recovers",
        )
    elif any(marker in text for marker in (
            "空输出", "schema 校验", "缺少 <<<", "missing <<<",
            "invalid preprocess json", "invalid meeting-compiler-v1 preprocess proposal",
    )):
        category = "worker_output_invalid"
        domain = "worker"
        disposition = "revise_output"
        next_action = "bounded_output_revision"
        retryable = "修复循环超过" not in text
    elif any(marker in text for marker in (
            "nameerror", "brokenpipeerror", "未预期异常", "traceback")):
        category, domain, disposition, owner, next_action = (
            "code_defect", "code", "engineering_fix", "engineering",
            "inspect_code_failure",
        )
    elif any(marker in text for marker in ("graph:", "sqlite", "graph_ingest", "图校验")):
        category, domain, disposition, owner, next_action = (
            "deterministic_validation", "graph", "engineering_fix", "engineering",
            "repair_graph_then_resume",
        )
    elif "frontmatter" in text:
        category, domain, disposition, owner, next_action = (
            "deterministic_validation", "validation", "engineering_fix", "engineering",
            "repair_staged_artifact",
        )
    elif any(marker in text for marker in ("extractor", "提取失败", "ocr")):
        category, domain, disposition, owner, next_action = (
            "extraction_failure", "extraction", "inspect_extraction", "program",
            "inspect_extracted_artifact",
        )
    elif status in {"agent_required", "type_mismatch", "classification_required"}:
        category, domain, disposition, owner, next_action = (
            "semantic_decision", "semantic", "specialist_review", "specialist_agent",
            str(state.get("next_action") or "review_handoff_then_resume"),
        )
    elif status in {"failed", "validation_error"} or errors:
        category = "unknown_failure"
    if not category:
        return None
    fingerprints = []
    for error in errors:
        normalized = re.sub(r"\s+", " ", error.strip().lower())
        fingerprints.append(hashlib.sha256(normalized.encode()).hexdigest()[:20])
    return {
        "category": category,
        "domain": domain,
        "disposition": disposition,
        "retryable": retryable,
        "owner": owner,
        "next_action": next_action,
        "fingerprints": fingerprints,
    }


def output_payload(state: dict, payload: dict) -> dict:
    """Attach the canonical failure disposition without mutating caller output."""
    result = dict(payload)
    failure = ((state.get("telemetry") or {}).get("current_failure")
               or classify_failure(state))
    if failure:
        result["failure_disposition"] = failure
    return result


def state_path(transaction_id: str) -> Path:
    return REPO / "temp" / "inbox-state" / f"{transaction_id}.json"


def _source_hash(state: dict) -> str | None:
    """对 state['source'] 文件内容做 sha256，用于跨重试关联同一来源。"""
    source = state.get("source")
    if not source:
        return None
    path = REPO / source
    if not path.is_file():
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _record_telemetry(state: dict) -> None:
    """Record workflow state changes; API calls live only in ExecutionEvent."""
    now = datetime.now(timezone.utc)
    telemetry = state.setdefault("telemetry", {})
    telemetry.setdefault("started_at", now.isoformat())
    telemetry["updated_at"] = now.isoformat()
    if "source_hash" not in telemetry:
        telemetry["source_hash"] = _source_hash(state)
    if "llm_calls" in telemetry or "llm_calls_total" in telemetry:
        legacy = telemetry.setdefault("legacy_stage_call_estimates", {})
        if "llm_calls" in telemetry:
            legacy.setdefault("llm_calls", telemetry.pop("llm_calls"))
        if "llm_calls_total" in telemetry:
            legacy.setdefault("llm_calls_total", telemetry.pop("llm_calls_total"))
    telemetry["execution_events"] = {
        "event_version": "execution-event-v1",
        "directory": "temp/llm-events",
        "transaction_id": str(state.get("transaction_id") or ""),
        "canonical_for_api_calls": True,
    }
    status = state.get("status")
    retry = state.get("retry_count")
    recovery_attempts = sum(
        int(value or 0)
        for value in ((state.get("recovery") or {}).get("attempts") or {}).values()
    )
    errors_count = len(state.get("errors") or [])
    last_status = telemetry.get("last_status")
    last_retry = telemetry.get("last_retry")
    last_recovery_attempts = telemetry.get("last_recovery_attempts")
    last_errors = telemetry.get("last_errors")
    pending = state.pop("_pending_transition", None)
    if (status != last_status or retry != last_retry or
            recovery_attempts != last_recovery_attempts or errors_count != last_errors):
        event = {
            "at": now.isoformat(),
            "from": last_status,
            "to": status,
            "retry_count": retry,
            "recovery_attempts": recovery_attempts,
            "errors_count": errors_count,
        }
        if isinstance(pending, dict) and pending.get("to") == status:
            event["transition"] = pending
        failure = classify_failure(state)
        if failure:
            event["failure"] = failure
            telemetry["current_failure"] = failure
        else:
            telemetry.pop("current_failure", None)
        telemetry.setdefault("events", []).append(event)
        telemetry["last_status"] = status
        telemetry["last_retry"] = retry
        telemetry["last_recovery_attempts"] = recovery_attempts
        telemetry.pop("last_attempts", None)
        telemetry["last_errors"] = errors_count


def load(transaction_id: str) -> dict | None:
    path = state_path(transaction_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(transaction_id: str, state: dict) -> Path:
    validate_status(state)
    state["transaction_id"] = transaction_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _record_telemetry(state)
    path = state_path(transaction_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
    return path


def summarize_runtime(state_dir: Path | None = None, events_dir: Path | None = None) -> dict:
    """Read-only aggregate of resumable transactions and canonical API events."""
    states_root = Path(state_dir or (REPO / "temp" / "inbox-state"))
    events_root = Path(events_dir or (REPO / "temp" / "llm-events"))
    statuses: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    recovery: Counter[str] = Counter()
    transactions: set[str] = set()
    degraded = 0
    invalid_state_files = 0
    for path in sorted(states_root.glob("*.json")) if states_root.is_dir() else []:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            invalid_state_files += 1
            continue
        transaction_id = str(state.get("transaction_id") or "") if isinstance(state, dict) else ""
        status = str(state.get("status") or "") if isinstance(state, dict) else ""
        if not transaction_id or status not in KNOWN_STATUSES:
            continue
        transactions.add(transaction_id)
        statuses[status] += 1
        if state.get("quality_warnings"):
            degraded += 1
        failure = ((state.get("telemetry") or {}).get("current_failure")
                   or classify_failure(state))
        if failure:
            failures[str(failure.get("category") or "unknown_failure")] += 1
        for category, count in (((state.get("recovery") or {}).get("attempts") or {}).items()):
            recovery[str(category)] += int(count or 0)

    api_calls = 0
    total_tokens = 0
    latency_sec = 0.0
    operations: Counter[str] = Counter()
    api_statuses: Counter[str] = Counter()
    invalid_event_lines = 0
    if events_root.is_dir():
        for path in sorted(events_root.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except (TypeError, json.JSONDecodeError):
                    invalid_event_lines += 1
                    continue
                if event.get("event_version") != "execution-event-v1" or event.get("event_kind") != "llm_api_call":
                    continue
                transaction_id = str(event.get("transaction_id") or "")
                if transaction_id not in transactions:
                    continue
                api_calls += 1
                latency_sec += float(event.get("latency_sec") or 0)
                total_tokens += int((event.get("usage") or {}).get("total_tokens") or 0)
                operations[str(event.get("operation") or "unknown")] += 1
                api_statuses[str(event.get("status") or "unknown")] += 1
    return {
        "summary_version": RUNTIME_SUMMARY_VERSION,
        "transactions": len(transactions),
        "by_status": dict(sorted(statuses.items())),
        "degraded": degraded,
        "failures_by_category": dict(sorted(failures.items())),
        "recovery_attempts": dict(sorted(recovery.items())),
        "api": {
            "event_version": "execution-event-v1",
            "calls": api_calls,
            "total_tokens": total_tokens,
            "latency_sec": round(latency_sec, 3),
            "by_operation": dict(sorted(operations.items())),
            "by_status": dict(sorted(api_statuses.items())),
        },
        "invalid_state_files": invalid_state_files,
        "invalid_event_lines": invalid_event_lines,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", action="store_true", help="只读汇总摄入事务与 API 事件")
    parser.add_argument("--state-dir")
    parser.add_argument("--events-dir")
    args = parser.parse_args()
    if not args.summary:
        parser.error("需要 --summary")
    report = summarize_runtime(
        Path(args.state_dir) if args.state_dir else None,
        Path(args.events_dir) if args.events_dir else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

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
RESULT_PROTOCOL_VERSION = "ingest-result-v1"
OPERATION_VIEW_VERSION = "ingest-operation-view-v1"
VERIFICATION_RECEIPT_VERSION = "verification-receipt-v1"
PUBLIC_WORKFLOW_STATUSES = frozenset({
    "awaiting_agent", "ready_to_commit", "completed", "failed",
})
KNOWN_STATUSES = frozenset({
    "init", "dedup_check", "preprocess", "extract", "write_wiki", "write_slots",
    "bibliographic_review_required", "agent_required", "type_mismatch",
    "classification_required", "finalize", "propositions", "propositions_done",
    "prepared", "finalized", "graph_ready", "update_graph", "validate_graph",
    "post_commit_projection", "finalize_tail", "completed", "duplicate_found", "failed", "validation_error",
    "superseded",
})
RESUME_TRANSITIONS = {
    "failed": frozenset({"finalize", "graph_ready", "post_commit_projection"}),
    "agent_required": frozenset({
        "write_wiki", "write_slots", "finalize", "propositions", "graph_ready",
        "update_graph", "validate_graph", "post_commit_projection", "finalize_tail", "bibliographic_review_required",
    }),
    "prepared": frozenset({
        "preprocess", "write_wiki", "write_slots", "finalize", "propositions", "graph_ready",
        "update_graph", "validate_graph", "post_commit_projection", "finalize_tail", "bibliographic_review_required",
    }),
    "bibliographic_review_required": frozenset({"write_wiki", "agent_required"}),
}
FORWARD_TRANSITIONS = {
    "init": frozenset({"dedup_check", "preprocess", "extract", "prepared",
                       "agent_required", "duplicate_found", "failed"}),
    "dedup_check": frozenset({"preprocess", "extract", "prepared", "agent_required",
                              "duplicate_found", "failed"}),
    "preprocess": frozenset({"extract", "write_wiki", "prepared", "agent_required",
                             "duplicate_found", "failed"}),
    "extract": frozenset({"write_wiki", "bibliographic_review_required", "prepared",
                          "agent_required", "duplicate_found", "failed", "superseded"}),
    "write_wiki": frozenset({"write_slots", "finalize", "prepared", "agent_required",
                              "type_mismatch", "duplicate_found", "failed"}),
    "write_slots": frozenset({"write_wiki", "finalize", "prepared", "agent_required",
                               "failed"}),
    "bibliographic_review_required": frozenset({"write_wiki", "prepared",
                                                 "agent_required", "failed"}),
    "agent_required": RESUME_TRANSITIONS["agent_required"] | frozenset({
        "prepared", "failed", "superseded",
    }),
    "classification_required": frozenset({"preprocess", "prepared", "agent_required", "failed"}),
    "finalize": frozenset({"finalized", "propositions", "graph_ready", "update_graph", "failed"}),
    "propositions": frozenset({"propositions_done", "prepared", "agent_required", "failed"}),
    "propositions_done": frozenset({"graph_ready", "failed"}),
    "prepared": RESUME_TRANSITIONS["prepared"] | frozenset({
        "agent_required", "failed", "superseded",
    }),
    "finalized": frozenset({"graph_ready", "update_graph", "failed"}),
    "graph_ready": frozenset({"update_graph", "validate_graph", "completed", "failed"}),
    "update_graph": frozenset({"validate_graph", "failed"}),
    "validate_graph": frozenset({"post_commit_projection", "finalize_tail", "completed", "failed"}),
    "post_commit_projection": frozenset({"finalize_tail", "failed"}),
    "finalize_tail": frozenset({"completed", "failed"}),
    "failed": RESUME_TRANSITIONS["failed"] | frozenset({"prepared", "agent_required"}),
    "validation_error": frozenset({
        "prepared", "agent_required", "failed", "superseded",
    }),
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


def advance(state: dict, target: str, *, reason: str) -> None:
    """Advance one declared forward stage without granting recovery overrides."""
    source = validate_status(state)
    transition(
        state, target, reason=reason,
        allowed_targets=FORWARD_TRANSITIONS.get(source, frozenset()),
    )


def _transition_allowed(source: str, target: str) -> bool:
    if source == target:
        return True
    return (
        target in FORWARD_TRANSITIONS.get(source, frozenset())
        or target in RESUME_TRANSITIONS.get(source, frozenset())
    )


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_digest(repo: Path, value: str) -> dict:
    relative = Path(str(value or ""))
    if not value or relative.is_absolute() or ".." in relative.parts:
        return {"path": str(value or ""), "exists": False, "sha256": "", "kind": "invalid"}
    path = (repo / relative).resolve()
    try:
        path.relative_to(repo.resolve())
    except ValueError:
        return {"path": relative.as_posix(), "exists": False, "sha256": "", "kind": "invalid"}
    if path.is_file():
        return {
            "path": relative.as_posix(), "exists": True,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "kind": "file",
        }
    if path.is_dir():
        rows = []
        for child in sorted(item for item in path.rglob("*") if item.is_file()):
            rows.append({
                "path": child.relative_to(path).as_posix(),
                "sha256": hashlib.sha256(child.read_bytes()).hexdigest(),
            })
        return {
            "path": relative.as_posix(), "exists": True,
            "sha256": _canonical_hash(rows), "kind": "directory", "files": len(rows),
        }
    return {"path": relative.as_posix(), "exists": False, "sha256": "", "kind": "missing"}


def _artifact_digest(repo: Path, key: str, value: str) -> dict:
    relative = Path(str(value or ""))
    if key == "wiki_path" and not relative.suffix and not (repo / relative).exists():
        markdown_path = relative.with_suffix(".md")
        if (repo / markdown_path).is_file():
            value = markdown_path.as_posix()
    return _path_digest(repo, value)


def build_verification_receipt(state: dict, repo: Path | None = None) -> dict:
    """Bind one terminal outcome to its stable state and declared final artifacts."""
    repo = Path(repo or REPO)
    internal_status = validate_status(state)
    workflow_status = public_workflow_status(state)
    artifact_keys = ("raw_dir", "wiki_path", "receipt_path", "report_path")
    artifacts = {
        key: _artifact_digest(repo, key, str(state.get(key) or ""))
        for key in artifact_keys if state.get(key)
    }
    receipt_values = {
        key: value for key, value in state.items()
        if "receipt" in key and key != "verification_receipt" and value
    }
    workspace_receipt = (state.get("agent_workspace") or {}).get("validation_receipt")
    if workspace_receipt:
        receipt_values["agent_workspace.validation_receipt"] = workspace_receipt
    terminal = internal_status in {"completed", "duplicate_found"}
    transition_trace = [
        {
            "from": event.get("from"),
            "to": event.get("to"),
            "transition": event.get("transition"),
        }
        for event in ((state.get("telemetry") or {}).get("events") or [])
        if isinstance(event, dict)
    ]
    checks = {
        "known_status": internal_status in KNOWN_STATUSES,
        "terminal_outcome": terminal,
        "no_errors": not bool(state.get("errors")),
        "artifact_integrity": all(item["exists"] for item in artifacts.values()),
    }
    body = {
        "schema": VERIFICATION_RECEIPT_VERSION,
        "status": "PASS" if all(checks.values()) else (
            "FAILED" if workflow_status == "failed" else "INCOMPLETE"
        ),
        "transaction_id": str(state.get("transaction_id") or ""),
        "state_protocol": STATE_PROTOCOL_VERSION,
        "workflow_status": workflow_status,
        "internal_status": internal_status,
        "outcome": "committed" if internal_status == "completed" else (
            "duplicate" if internal_status == "duplicate_found" else "uncommitted"
        ),
        "committed": internal_status == "completed",
        "checks": checks,
        "state_sha256": _canonical_hash({
            "transaction_id": state.get("transaction_id"),
            "status": internal_status,
            "source": state.get("source"),
            "source_hash": (state.get("telemetry") or {}).get("source_hash"),
            "raw_dir": state.get("raw_dir"),
            "wiki_path": state.get("wiki_path"),
            "pipeline_script": state.get("pipeline_script"),
            "semantic_backend": state.get("semantic_backend"),
        }),
        "transitions_sha256": _canonical_hash(transition_trace),
        "artifacts": artifacts,
        "validator_receipts_sha256": _canonical_hash(receipt_values),
    }
    body["receipt_hash"] = _canonical_hash(body)
    return body


def verify_receipt(state: dict, repo: Path | None = None) -> dict:
    """Recompute the final receipt; stale or tampered receipts fail closed."""
    current = build_verification_receipt(state, repo)
    stored = state.get("verification_receipt")
    if current["status"] != "PASS":
        return current
    if not isinstance(stored, dict) or stored.get("schema") != VERIFICATION_RECEIPT_VERSION:
        return {**current, "status": "INCOMPLETE", "error": "verification receipt missing"}
    if stored.get("receipt_hash") != current.get("receipt_hash"):
        return {**current, "status": "FAILED", "error": "verification receipt mismatch"}
    return current


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
            "invalid preprocess json", "invalid meeting compiler preprocess proposal",
            "invalid meeting-compiler-v1 preprocess proposal",
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
    elif status in {"agent_required", "prepared", "type_mismatch", "classification_required"}:
        category, domain, disposition, owner, next_action = (
            "semantic_decision", "semantic", "host_agent_review", "host_agent",
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


def public_workflow_status(state: dict, payload: dict | None = None) -> str:
    """Project internal ingest stages onto the shared four-state result contract."""
    payload = payload or {}
    explicit = str(payload.get("workflow_status") or "")
    if explicit in PUBLIC_WORKFLOW_STATUSES:
        return explicit
    status = str(payload.get("status") or state.get("status") or "")
    if status in {"completed", "duplicate_found"}:
        return "completed"
    if status in {"graph_ready", "finalized", "ready_to_commit"}:
        return "ready_to_commit"
    if status in {"failed", "validation_error", "type_mismatch", "superseded"}:
        return "failed"
    return "awaiting_agent"


def _artifact_refs(state: dict, payload: dict) -> dict:
    refs: dict[str, object] = {}
    supplied = payload.get("artifacts")
    if isinstance(supplied, dict):
        refs.update({str(key): value for key, value in supplied.items() if value})
    for key in (
        "raw_dir", "wiki_path", "report_path", "receipt_path", "review_path",
        "write_to", "validation_receipt",
    ):
        value = payload.get(key) or state.get(key)
        if value:
            refs.setdefault(key, value)
    task = payload.get("agent_task") or state.get("agent_task")
    if isinstance(task, dict) and task.get("schema") == "agent-task-v1":
        refs.setdefault("agent_task", {
            "schema": task["schema"],
            "outputs": [
                item.get("path") for item in task.get("outputs") or []
                if isinstance(item, dict) and item.get("path")
            ],
        })
    return refs


def output_payload(state: dict, payload: dict) -> dict:
    """Return one typed invocation result without mutating caller output.

    A successful invocation may still leave the workflow awaiting semantic work.
    Callers must inspect workflow_status/terminal/committed instead of inferring
    knowledge commit from the process exit code.
    """
    result = dict(payload)
    workflow_status = public_workflow_status(state, result)
    internal_status = str(
        result.get("internal_status") or state.get("status") or result.get("status") or ""
    )
    transaction_id = str(result.get("transaction_id") or state.get("transaction_id") or "")
    result.update({
        "schema": RESULT_PROTOCOL_VERSION,
        "invocation_status": "ok",
        "workflow_status": workflow_status,
        "terminal": workflow_status in {"completed", "failed"},
        "committed": internal_status == "completed",
        "internal_status": internal_status,
        "artifact_refs": _artifact_refs(state, result),
    })
    if transaction_id:
        result["transaction_id"] = transaction_id
        result["transaction_ids"] = [transaction_id]
        result["state_ref"] = f"temp/inbox-state/{transaction_id}.json"
        result["runtime_status_ref"] = f"temp/inbox-state/{transaction_id}.status.md"
    next_actions = result.get("next_actions")
    if not next_actions and result.get("next_action"):
        next_actions = {"resume": result["next_action"]}
    if not next_actions and isinstance(result.get("agent_task"), dict):
        next_actions = result["agent_task"].get("commands")
    if next_actions:
        result["next_actions"] = next_actions
    if state.get("verification_receipt"):
        result["verification_receipt"] = state["verification_receipt"]
    failure = ((state.get("telemetry") or {}).get("current_failure")
               or classify_failure(state))
    if failure:
        result["failure_disposition"] = failure
    return result


def operation_view(state: dict, repo: Path | None = None) -> dict:
    """Return a read-only, content-agnostic view of one persisted ingest run."""
    status = validate_status(state)
    workflow_status = public_workflow_status(state)
    task = state.get("agent_task") if isinstance(state.get("agent_task"), dict) else {}
    commands = dict(task.get("commands") or {})
    if workflow_status in {"completed", "failed"}:
        next_action = "none"
    elif task.get("schema") == "agent-task-v1":
        next_action = "write_outputs"
        if all(
            not item.get("required", True)
            or ((Path(repo or REPO) / str(item.get("path") or "")).is_file()
                and (Path(repo or REPO) / str(item.get("path") or "")).stat().st_size > 0)
            for item in task.get("outputs") or []
        ):
            next_action = "advance"
    elif state.get("next_action"):
        next_action = str(state["next_action"])
    elif status == "graph_ready":
        next_action = "commit"
    else:
        next_action = "resume"
    view = {
        "schema": OPERATION_VIEW_VERSION,
        "transaction_id": str(state.get("transaction_id") or ""),
        "workflow_status": workflow_status,
        "internal_status": status,
        "terminal": workflow_status in {"completed", "failed"},
        "committed": status == "completed",
        "state_ref": f"temp/inbox-state/{state.get('transaction_id', '')}.json",
        "runtime_status_ref": (
            f"temp/inbox-state/{state.get('transaction_id', '')}.status.md"
        ),
        "next_action": next_action,
        "allowed_actions": sorted(commands),
        "artifact_refs": _artifact_refs(state, {}),
    }
    failure = ((state.get("telemetry") or {}).get("current_failure")
               or classify_failure(state))
    if failure:
        view["failure_disposition"] = failure
    if state.get("verification_receipt"):
        view["verification_receipt"] = state["verification_receipt"]
    return view


def batch_output_payload(*, items: list[dict], status: str, phase: str,
                         next_action: str = "") -> dict:
    """Derive a batch result exclusively from canonical item envelopes."""
    normalized = []
    for item in items:
        if item.get("schema") != RESULT_PROTOCOL_VERSION:
            raise ValueError("batch item 缺少 ingest-result-v1")
        workflow_status = str(item.get("workflow_status") or "")
        if workflow_status not in PUBLIC_WORKFLOW_STATUSES:
            raise ValueError(f"batch item workflow_status 非法: {workflow_status or '(empty)'}")
        normalized.append(item)
    statuses = Counter(item["workflow_status"] for item in normalized)
    internal = Counter(str(item.get("internal_status") or "") for item in normalized)
    if statuses.get("failed"):
        workflow_status = "failed"
    elif normalized and statuses.get("completed", 0) == len(normalized):
        workflow_status = "completed"
    elif normalized and (
        statuses.get("ready_to_commit", 0) + statuses.get("completed", 0)
        == len(normalized)
    ):
        workflow_status = "ready_to_commit"
    elif normalized:
        workflow_status = "awaiting_agent"
    else:
        workflow_status = "completed"
    counts = {
        "total": len(normalized),
        **{key: statuses.get(key, 0) for key in sorted(PUBLIC_WORKFLOW_STATUSES)},
        "duplicates": internal.get("duplicate_found", 0),
        "committed": sum(bool(item.get("committed")) for item in normalized),
    }
    if sum(counts[key] for key in PUBLIC_WORKFLOW_STATUSES) != counts["total"]:
        raise ValueError("batch workflow counts 不守恒")
    transaction_ids = [
        str(item.get("transaction_id")) for item in normalized
        if item.get("transaction_id")
    ]
    result = {
        "schema": RESULT_PROTOCOL_VERSION,
        "invocation_status": "ok",
        "status": status,
        "phase": phase,
        "workflow_status": workflow_status,
        "terminal": workflow_status in {"completed", "failed"},
        "committed": bool(normalized) and all(
            bool(item.get("committed")) for item in normalized
        ),
        "transaction_ids": transaction_ids,
        "artifact_refs": {},
        "counts": counts,
        "items": normalized,
    }
    if next_action:
        result["next_actions"] = {"resume": next_action}
    return result


def state_path(transaction_id: str) -> Path:
    return REPO / "temp" / "inbox-state" / f"{transaction_id}.json"


def runtime_status_path(transaction_id: str) -> Path:
    return REPO / "temp" / "inbox-state" / f"{transaction_id}.status.md"


def _runtime_status_document(state: dict) -> str:
    """Render a bounded, overwrite-only progress view for low-frequency inspection."""
    status = str(state.get("status") or "unknown")
    errors = state.get("errors") or []
    if isinstance(errors, str):
        errors = [errors]
    if status in {"completed", "duplicate_found", "superseded"}:
        signal, next_check = "done", 0
    elif errors or status in {
            "failed", "validation_error", "type_mismatch", "classification_required",
            "bibliographic_review_required", "agent_required", "prepared"}:
        signal, next_check = "attention", 0
    else:
        signal, next_check = "running", 60
    stage_events = {
        "extract": "PDF extraction running (MinerU when configured)",
        "write_wiki": "Wiki drafting or validation running",
        "write_slots": "Semantic extraction or validation running",
        "update_graph": "Graph update running",
        "validate_graph": "Graph validation running",
        "completed": "Ingest completed",
        "duplicate_found": "Exact duplicate closed without a new commit",
        "superseded": "Transaction superseded by a completed same-source transaction",
    }
    event = str(
        errors[-1] if errors else state.get("next_action")
        or stage_events.get(status) or status
    )
    event = " ".join(event.split())[:120]
    transaction_id = str(state.get("transaction_id") or "")
    log_ref = f"temp/inbox-state/{transaction_id}.log"
    state_ref = f"temp/inbox-state/{transaction_id}.json"
    return (
        "# Ingest runtime status\n\n"
        f"- transaction: `{transaction_id}`\n"
        f"- signal: `{signal}`\n"
        f"- stage: `{status}`\n"
        f"- updated_at: `{state.get('updated_at', '')}`\n"
        f"- event: {event}\n"
        f"- next_check_after_seconds: `{next_check}`\n"
        f"- details: `{state_ref}`; log: `{log_ref}`\n"
    )


def _write_runtime_status(state: dict) -> Path:
    path = runtime_status_path(str(state.get("transaction_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _runtime_status_document(state)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
    return path


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
    path = state_path(transaction_id)
    previous_status = ""
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot validate previous ingest state: {exc}") from exc
        previous_status = validate_status(previous)
    current_status = str(state["status"])
    pending = state.get("_pending_transition")
    if previous_status and previous_status != current_status:
        if not _transition_allowed(previous_status, current_status):
            raise ValueError(
                f"illegal persisted ingest transition: {previous_status} -> {current_status}"
            )
        if isinstance(pending, dict):
            if pending.get("from") != previous_status or pending.get("to") != current_status:
                raise ValueError(
                    f"ingest transition receipt mismatch: {previous_status} -> {current_status}"
                )
        else:
            state["_pending_transition"] = {
                "protocol_version": STATE_PROTOCOL_VERSION,
                "from": previous_status,
                "to": current_status,
                "reason": "persisted_forward_transition",
            }
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _record_telemetry(state)
    if current_status in {"completed", "duplicate_found"}:
        state["verification_receipt"] = build_verification_receipt(state)
    else:
        state.pop("verification_receipt", None)
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
    try:
        _write_runtime_status(state)
    except OSError:
        # Observability must not invalidate an already durable ingest state.
        pass
    return path


def find_resumable_transaction(source: str, pipeline_script: str, *,
                               semantic_backend: str = "") -> dict | None:
    """Find the only compatible uncommitted transaction for an inbox source.

    Source path alone is not an identity: the live file hash, pipeline owner and
    (once selected) semantic backend must agree. Multiple matches fail closed so
    the caller cannot choose an arbitrary branch of transaction history.
    """
    relative = Path(str(source or ""))
    if (not source or relative.is_absolute() or ".." in relative.parts
            or not pipeline_script):
        raise ValueError("invalid source transaction lookup")
    source_path = (REPO / relative).resolve()
    try:
        source_path.relative_to(REPO.resolve())
    except ValueError as exc:
        raise ValueError("source transaction lookup escapes repository") from exc
    if not source_path.is_file():
        return None
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    states_root = REPO / "temp" / "inbox-state"
    matches = []
    terminal = {"completed", "duplicate_found", "superseded"}
    for path in sorted(states_root.glob("*.json")) if states_root.is_dir() else []:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            status = validate_status(state)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if status in terminal:
            continue
        if state.get("source") != relative.as_posix():
            continue
        if state.get("pipeline_script") != pipeline_script:
            continue
        recorded_backend = str(state.get("semantic_backend") or "")
        if semantic_backend and recorded_backend and recorded_backend != semantic_backend:
            continue
        recorded_hash = str((state.get("telemetry") or {}).get("source_hash") or "")
        if not recorded_hash or recorded_hash != source_hash:
            continue
        matches.append(state)
    if len(matches) > 1:
        ids = ", ".join(str(item.get("transaction_id") or "") for item in matches)
        raise ValueError(f"ambiguous same-source transactions: {ids}")
    return matches[0] if matches else None


def supersede_transaction(transaction_id: str, completed_by: str) -> dict:
    """Close one stale, uncommitted transaction using a completed same-source txn."""
    if not re.fullmatch(r"[\w.-]+", transaction_id or ""):
        raise ValueError("invalid superseded transaction id")
    if not re.fullmatch(r"[\w.-]+", completed_by or ""):
        raise ValueError("invalid replacement transaction id")
    if transaction_id == completed_by:
        raise ValueError("transaction cannot supersede itself")
    stale = load(transaction_id)
    replacement = load(completed_by)
    if stale is None or replacement is None:
        raise ValueError("supersede requires two existing transactions")
    if stale.get("status") not in {
            "extract", "prepared", "agent_required", "validation_error"}:
        raise ValueError("only an uncommitted review transaction may be superseded")
    if replacement.get("status") not in {"completed", "duplicate_found"}:
        raise ValueError("replacement transaction is not complete")
    if stale.get("source") != replacement.get("source"):
        raise ValueError("supersede transactions do not reference the same source")
    stale_hash = str((stale.get("telemetry") or {}).get("source_hash") or "")
    replacement_hash = str((replacement.get("telemetry") or {}).get("source_hash") or "")
    if not stale_hash or not replacement_hash:
        raise ValueError("supersede requires non-empty source hashes")
    if stale_hash != replacement_hash:
        raise ValueError("supersede transactions have different source hashes")
    if (stale.get("verification_receipt") or {}).get("committed"):
        raise ValueError("committed transaction cannot be superseded")
    transition(
        stale, "superseded", reason=f"completed_by:{completed_by}",
        allowed_targets={"superseded"},
    )
    stale["superseded_by"] = completed_by
    stale["superseded_reason"] = "same_source_completed_transaction"
    stale["errors"] = []
    path = save(transaction_id, stale)
    return {
        "status": "superseded",
        "transaction_id": transaction_id,
        "superseded_by": completed_by,
        "state_path": str(path.relative_to(REPO)),
    }


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
    unattributed_calls = 0
    unattributed_tokens = 0
    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    token_breakdown_missing = 0
    operation_usage = {}
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
                if not transaction_id:
                    unattributed_calls += 1
                    unattributed_tokens += int((event.get("usage") or {}).get("total_tokens") or 0)
                if transaction_id not in transactions:
                    continue
                api_calls += 1
                latency_sec += float(event.get("latency_sec") or 0)
                usage = event.get("usage") or {}
                total_tokens += int(usage.get("total_tokens") or 0)
                prompt_tokens += int(usage.get("prompt_tokens") or 0)
                completion_tokens += int(usage.get("completion_tokens") or 0)
                token_breakdown_missing += int(
                    "prompt_tokens" not in usage or "completion_tokens" not in usage
                )
                operation = str(event.get("operation") or "unknown")
                operations[operation] += 1
                breakdown = operation_usage.setdefault(operation, {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                    "latency_sec": 0.0,
                })
                for token_field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    breakdown[token_field] += int(usage.get(token_field) or 0)
                breakdown["latency_sec"] = round(
                    breakdown["latency_sec"] + float(event.get("latency_sec") or 0), 3,
                )
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
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "token_breakdown_missing_calls": token_breakdown_missing,
            "usage_by_operation": dict(sorted(operation_usage.items())),
            "latency_sec": round(latency_sec, 3),
            "by_operation": dict(sorted(operations.items())),
            "by_status": dict(sorted(api_statuses.items())),
        },
        "invalid_state_files": invalid_state_files,
        "invalid_event_lines": invalid_event_lines,
        "unattributed_api_events": {
            "calls": unattributed_calls, "total_tokens": unattributed_tokens,
            "scope": "all_scanned_event_files_without_transaction_id",
        },
        "cost_coverage": {
            "scope": "transaction_tagged_llm_api_events_only",
            "monetary_cost": None,
            "unmetered_components": ["host_agent", "embedding", "pdf_extraction"],
        },
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--summary", action="store_true", help="只读汇总摄入事务与 API 事件")
    action.add_argument("--supersede", help="关闭同源、未提交的旧事务 ID")
    parser.add_argument("--by", help="已完成的替代事务 ID")
    parser.add_argument("--state-dir")
    parser.add_argument("--events-dir")
    args = parser.parse_args()
    if args.supersede:
        if not args.by:
            parser.error("--supersede 需要 --by")
        print(json.dumps(
            supersede_transaction(args.supersede, args.by),
            ensure_ascii=False, indent=2,
        ))
        return
    report = summarize_runtime(
        Path(args.state_dir) if args.state_dir else None,
        Path(args.events_dir) if args.events_dir else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

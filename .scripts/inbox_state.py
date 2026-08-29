#!/usr/bin/env python3
"""Persist resumable state for one inbox intake transaction."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


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
    """记录阶段时间线与来源哈希；仅追加状态/重试变化事件，不覆盖事实字段。"""
    now = datetime.now(timezone.utc)
    telemetry = state.setdefault("telemetry", {})
    telemetry.setdefault("started_at", now.isoformat())
    telemetry["updated_at"] = now.isoformat()
    if "source_hash" not in telemetry:
        telemetry["source_hash"] = _source_hash(state)
    status = state.get("status")
    retry = state.get("retry_count")
    attempts = state.get("wiki_retry", 0) + state.get("slots_retry", 0)
    errors_count = len(state.get("errors") or [])
    last_status = telemetry.get("last_status")
    last_retry = telemetry.get("last_retry")
    last_attempts = telemetry.get("last_attempts")
    last_errors = telemetry.get("last_errors")
    if (status != last_status or retry != last_retry or
            attempts != last_attempts or errors_count != last_errors):
        telemetry.setdefault("events", []).append({
            "at": now.isoformat(),
            "from": last_status,
            "to": status,
            "retry_count": retry,
            "attempts": attempts,
            "errors_count": errors_count,
        })
        telemetry["last_status"] = status
        telemetry["last_retry"] = retry
        telemetry["last_attempts"] = attempts
        telemetry["last_errors"] = errors_count


def load(transaction_id: str) -> dict | None:
    path = state_path(transaction_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(transaction_id: str, state: dict) -> Path:
    state["transaction_id"] = transaction_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _record_telemetry(state)
    path = state_path(transaction_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path

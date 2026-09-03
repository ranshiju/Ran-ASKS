#!/usr/bin/env python3
"""Versioned recovery budgets shared by ingest and LLM execution layers."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


POLICY_VERSION = "typed-recovery-v1"
RECOVERY_CLASSES = (
    "infrastructure",
    "output_transport",
    "wiki_revision",
    "semantic_revision",
    "deterministic_repair",
    "subagent",
)
LLM_DEFAULT_LIMITS = {
    "infrastructure": 1,
    "output_transport": 1,
}
DEFAULT_LIMITS = {
    **LLM_DEFAULT_LIMITS,
    "wiki_revision": 1,
    "semantic_revision": 1,
    "deterministic_repair": 1,
    "subagent": 1,
}


def normalize_limits(overrides: dict | None = None) -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    for category, value in (overrides or {}).items():
        if category not in RECOVERY_CLASSES:
            raise ValueError(f"unknown recovery class: {category}")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid recovery limit for {category}: {value}") from exc
        if parsed < 0:
            raise ValueError(f"negative recovery limit for {category}: {parsed}")
        limits[category] = parsed
    return limits


def limits_from_spec(spec: dict) -> dict[str, int]:
    """Read the typed policy, with a conservative bridge for old specs."""
    configured = spec.get("recovery_limits")
    if configured is not None:
        return normalize_limits(configured)
    legacy_max = max(0, int(spec.get("max_retries", 1)))
    return normalize_limits({
        "wiki_revision": max(0, int(spec.get("max_wiki_validation_retries", legacy_max))),
        "semantic_revision": max(0, int(spec.get("max_semantic_hard_retries", legacy_max))),
    })


def llm_limits(retries: int = 1, overrides: dict | None = None) -> dict[str, int]:
    """Compile client-owned retry limits without exposing pipeline classes."""
    legacy = max(0, int(retries))
    limits = {
        "infrastructure": legacy,
        "output_transport": legacy,
    }
    for category, value in (overrides or {}).items():
        if category not in {"infrastructure", "output_transport"}:
            raise ValueError(f"LLM client cannot own recovery class: {category}")
        parsed = int(value)
        if parsed < 0:
            raise ValueError(f"negative recovery limit for {category}: {parsed}")
        limits[category] = parsed
    return limits


def ensure_state(state: dict, limits: dict | None = None) -> dict:
    """Initialize typed transaction recovery state and bridge old counters once."""
    compiled = normalize_limits(limits)
    recovery = state.get("recovery")
    if not isinstance(recovery, dict) or recovery.get("policy_version") != POLICY_VERSION:
        if isinstance(recovery, dict) and recovery:
            state.setdefault("legacy_recovery", []).append(deepcopy(recovery))
        recovery = {
            "policy_version": POLICY_VERSION,
            "limits": compiled,
            "attempts": {},
        }
        state["recovery"] = recovery
    else:
        recovery["limits"] = compiled
        recovery.setdefault("attempts", {})

    if not recovery.get("legacy_counters_migrated"):
        wiki_retry = max(0, int(state.get("wiki_retry") or 0))
        slots_retry = max(
            max(0, int(state.get("slots_retry") or 0)),
            max(0, int(state.get("semantic_hard_retry") or 0)),
            max(0, int(state.get("sparse_slots_retry") or 0)),
        )
        if wiki_retry:
            recovery["attempts"].setdefault("wiki_revision", wiki_retry)
        if slots_retry:
            recovery["attempts"].setdefault("semantic_revision", slots_retry)
        recovery["legacy_counters_migrated"] = True
    return recovery


def consume(state: dict, category: str, limits: dict | None = None,
            detail: str = "") -> bool:
    """Consume one recovery action. False means the typed budget is exhausted."""
    if category not in RECOVERY_CLASSES:
        raise ValueError(f"unknown recovery class: {category}")
    recovery = ensure_state(state, limits)
    attempts = recovery["attempts"]
    used = max(0, int(attempts.get(category) or 0))
    limit = recovery["limits"][category]
    allowed = used < limit
    if allowed:
        used += 1
        attempts[category] = used
        if category == "wiki_revision":
            state["wiki_retry"] = max(int(state.get("wiki_retry") or 0), used)
        elif category == "semantic_revision":
            state["slots_retry"] = max(int(state.get("slots_retry") or 0), used)
    recovery["last_action"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "attempt": used,
        "limit": limit,
        "outcome": "consumed" if allowed else "exhausted",
        "detail": str(detail or "")[:240],
    }
    return allowed


def remaining(state: dict, category: str, limits: dict | None = None) -> int:
    recovery = ensure_state(state, limits)
    return max(
        0,
        recovery["limits"][category] - int(recovery["attempts"].get(category) or 0),
    )

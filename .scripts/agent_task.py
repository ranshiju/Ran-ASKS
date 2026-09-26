#!/usr/bin/env python3
"""Prompt-free task manifests and a portable host control contract."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath

import env_config


SCHEMA_VERSION = "agent-task-v1"
CONTROL_VERSION = "agent-task-control-v1"
CONTEXT_INLINE_BYTES = 4096
CONTEXT_TOTAL_BYTES = 16384
VALID_BACKENDS = frozenset({"agent", "api"})
ALLOWED_COMMAND_ENV = frozenset({
    "INGEST_BACKEND", "QUERY_BACKEND", "RESEARCH_BACKEND",
})
REPO = Path(__file__).resolve().parent.parent


def _configured_value(variable: str, default: str) -> str:
    """Read one non-secret setting with process environment precedence."""
    return env_config.load_env(
        REPO / ".env", keys={variable}
    ).get(variable, default)


def backend(variable: str, default: str = "agent") -> str:
    """Read one explicit execution backend without importing a model client."""
    value = _configured_value(variable, default).strip().lower() or default
    if value not in VALID_BACKENDS:
        raise RuntimeError(f"{variable} 只能是 agent 或 api")
    return value


def ingest_backend() -> str:
    return backend("INGEST_BACKEND")


def query_backend() -> str:
    return backend("QUERY_BACKEND")


def research_backend() -> str:
    return backend("RESEARCH_BACKEND")


def resolve_temp_artifact(repo: Path, value: str | Path, namespace: str, *,
                          must_exist: bool = True) -> Path:
    """Resolve an Agent artifact inside one owned temp namespace."""
    namespace_path = PurePosixPath(str(namespace or ""))
    if (not namespace or namespace_path.is_absolute() or ".." in namespace_path.parts):
        raise ValueError(f"非法 Agent temp namespace: {namespace!r}")
    repo = repo.resolve()
    base = (repo / "temp" / namespace_path).resolve()
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Agent 产物必须位于 temp/{namespace_path.as_posix()}/") from exc
    if must_exist and (not resolved.is_file() or resolved.stat().st_size == 0):
        raise ValueError(f"Agent 产物不存在或为空: {resolved}")
    return resolved


def _relative_path(value: str) -> str:
    path = PurePosixPath(str(value or ""))
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"agent task path 必须是仓库内相对路径: {value!r}")
    return path.as_posix()


def _artifacts(values: list[dict], *, output: bool) -> list[dict]:
    if not isinstance(values, list) or not values:
        raise ValueError("agent task inputs/outputs 必须是非空列表")
    normalized = []
    for value in values:
        if not isinstance(value, dict) or not str(value.get("name") or "").strip():
            raise ValueError("agent task artifact 缺少 name")
        item = dict(value)
        item["name"] = str(item["name"]).strip()
        item["path"] = _relative_path(str(item.get("path") or ""))
        if output:
            if PurePosixPath(item["path"]).parts[0] != "temp":
                raise ValueError("agent task output 只能位于 temp/ 暂存区")
            item["required"] = bool(item.get("required", True))
        normalized.append(item)
    return normalized


def _control_contract(commands: dict[str, str]) -> dict:
    """Describe the host loop without duplicating content-specific instructions."""
    return {
        "version": CONTROL_VERSION,
        "loop": ["inspect", "write_outputs", "advance"],
        "allowed_actions": sorted(commands),
        "write_scope": "declared_outputs_only",
        "validation_authority": "declared_managed_commands",
        "transaction_policy": "same_transaction_until_terminal",
        "terminal_workflow_statuses": ["completed", "failed"],
    }


def make_task(*, kind: str, transaction_id: str, inputs: list[dict],
              outputs: list[dict], protocol: dict, issues: list | None = None,
              commands: dict | None = None, context: dict | None = None) -> dict:
    """Build and validate an agent-task-v1 manifest."""
    if not str(kind or "").strip() or not str(transaction_id or "").strip():
        raise ValueError("agent task 缺少 kind 或 transaction_id")
    if not isinstance(protocol, dict) or not str(protocol.get("name") or "").strip():
        raise ValueError("agent task protocol 缺少 name")
    command_map = {}
    for name, command in (commands or {}).items():
        if name not in {"read", "check", "commit", "resume", "refresh"}:
            raise ValueError(f"不支持的 agent task command: {name}")
        if command:
            command_map[name] = str(command)
    return {
        "schema": SCHEMA_VERSION,
        "status": "prepared",
        "kind": str(kind).strip(),
        "transaction_id": str(transaction_id).strip(),
        "inputs": _artifacts(inputs, output=False),
        "outputs": _artifacts(outputs, output=True),
        "protocol": dict(protocol),
        "issues": list(issues or []),
        "commands": command_map,
        "control": _control_contract(command_map),
        "context": dict(context or {}),
    }


def prepare(state: dict, **task_fields) -> dict:
    """Attach a task to persisted state without invoking or impersonating an Agent."""
    task = make_task(**task_fields)
    state["agent_task"] = task
    state["status"] = "prepared"
    state.pop("agent_required", None)
    state.pop("agent_prompt", None)
    state.pop("agent_write_to", None)
    return task


def is_prepared(state: dict) -> bool:
    task = state.get("agent_task") or {}
    return state.get("status") == "prepared" and task.get("schema") == SCHEMA_VERSION


def missing_outputs(state: dict, repo: Path) -> list[str]:
    task = state.get("agent_task") or {}
    missing = []
    for artifact in task.get("outputs") or []:
        if not artifact.get("required", True):
            continue
        path = repo / str(artifact.get("path") or "")
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(str(artifact.get("path") or ""))
    return missing


def reopen(state: dict, issues: list) -> dict:
    """Return an invalid staged result to the same task and preserve diagnostics."""
    task = dict(state.get("agent_task") or {})
    if task.get("schema") != SCHEMA_VERSION:
        raise ValueError("state 缺少 agent-task-v1")
    task["status"] = "prepared"
    task["issues"] = list(issues or [])
    state["agent_task"] = task
    state["status"] = "prepared"
    state["errors"] = [str(item) for item in issues or []]
    state.pop("agent_required", None)
    state.pop("agent_prompt", None)
    state.pop("agent_write_to", None)
    return task


def mark_consumed(state: dict) -> None:
    task = state.get("agent_task") or {}
    if task.get("schema") == SCHEMA_VERSION:
        task["status"] = "consumed"
        state["agent_task"] = task


def payload(state: dict) -> dict:
    task = state.get("agent_task") or {}
    if task.get("schema") != SCHEMA_VERSION:
        raise ValueError("state 缺少 agent-task-v1")
    result = dict(task)
    result.setdefault("control", _control_contract(result.get("commands") or {}))
    return result


def workflow_status(state: dict) -> str:
    """Project content-specific internal stages onto the portable host states."""
    status = str(state.get("status") or "")
    if status in {"completed", "duplicate_found"}:
        return "completed"
    if status in {"failed", "validation_error", "type_mismatch", "superseded"}:
        return "failed"
    if status in {"ready_to_commit", "graph_ready", "finalized"}:
        return "ready_to_commit"
    return "awaiting_agent"


def _compact_marker(value, encoded: bytes) -> dict:
    return {
        "compacted": True,
        "type": type(value).__name__,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _compact_context(context: dict) -> dict:
    """Bound inspect output while leaving the persisted task untouched."""
    compacted = {}
    for key, value in context.items():
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        compacted[key] = (
            value if len(encoded) <= CONTEXT_INLINE_BYTES
            else _compact_marker(value, encoded)
        )
    encoded = json.dumps(
        compacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= CONTEXT_TOTAL_BYTES:
        return compacted
    original = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    marker = _compact_marker(context, original)
    marker["key_count"] = len(context)
    return marker


def control_view(state: dict, repo: Path = REPO) -> dict:
    """Return the stable host-facing view for one persisted Agent task."""
    task = payload(state)
    status = workflow_status(state)
    missing = [] if status in {"completed", "failed"} else missing_outputs(state, repo)
    task["context"] = _compact_context(task.get("context") or {})
    commands = task.get("commands") or {}
    if status in {"completed", "failed"}:
        next_action = "none"
    elif missing:
        next_action = "write_outputs"
    elif status == "ready_to_commit" and "commit" in commands:
        next_action = "commit"
    elif any(name in commands for name in ("check", "resume", "commit")):
        next_action = "advance"
    elif "read" in commands:
        next_action = "read"
    else:
        next_action = "unsupported"
    return {
        "schema": SCHEMA_VERSION,
        "control_version": CONTROL_VERSION,
        "transaction_id": task["transaction_id"],
        "kind": task["kind"],
        "workflow_status": status,
        "internal_status": str(state.get("status") or ""),
        "next_action": next_action,
        "missing_outputs": missing,
        "task": task,
    }


def _managed_command(command: str, repo: Path) -> tuple[list[str], dict[str, str]]:
    """Parse one declared action without invoking a shell or accepting arbitrary code."""
    try:
        parts = shlex.split(str(command or ""))
    except ValueError as exc:
        raise ValueError(f"Agent task command 无法解析: {exc}") from None
    overrides: dict[str, str] = {}
    while parts and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", parts[0]):
        name, value = parts.pop(0).split("=", 1)
        if name not in ALLOWED_COMMAND_ENV or value not in VALID_BACKENDS:
            raise ValueError(f"Agent task command 不允许环境覆盖: {name}")
        overrides[name] = value
    if len(parts) < 2 or Path(parts[0]).name not in {"python", "python3"}:
        raise ValueError("Agent task command 必须调用 Python 受管脚本")
    script = Path(parts[1])
    resolved = script.resolve() if script.is_absolute() else (repo / script).resolve()
    scripts_root = (repo / ".scripts").resolve()
    try:
        resolved.relative_to(scripts_root)
    except ValueError as exc:
        raise ValueError("Agent task command 只能调用仓库 .scripts 下入口") from exc
    if resolved.suffix != ".py" or not resolved.is_file():
        raise ValueError(f"Agent task command 入口不存在: {resolved}")
    return [sys.executable, str(resolved), *parts[2:]], overrides


def run_action(state: dict, action: str, repo: Path = REPO) -> dict:
    """Execute one declared managed action and capture its machine-readable output."""
    task = payload(state)
    commands = task.get("commands") or {}
    if action not in commands:
        raise ValueError(f"Agent task 未声明 action: {action}")
    argv, overrides = _managed_command(commands[action], repo)
    env = os.environ.copy()
    env.update(overrides)
    result = subprocess.run(
        argv, cwd=repo, env=env, text=True, capture_output=True,
    )
    return {
        "action": action,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "command": [str(Path(argv[1]).relative_to(repo.resolve())), *argv[2:]],
        "environment_overrides": sorted(overrides),
    }

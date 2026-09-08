#!/usr/bin/env python3
"""Prompt-free task manifests for work performed by the current host Agent."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


SCHEMA_VERSION = "agent-task-v1"
VALID_BACKENDS = frozenset({"agent", "api"})
REPO = Path(__file__).resolve().parent.parent


def _configured_value(variable: str, default: str) -> str:
    """Read one non-secret setting with process environment precedence."""
    if variable in os.environ:
        return os.environ[variable]
    env_file = REPO / ".env"
    value = default
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, configured = line.split("=", 1)
            if key.strip() == variable:
                value = configured.strip()
    return value


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
    return dict(task)

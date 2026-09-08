#!/usr/bin/env python3
"""Generic persistent workspace state for projects/ directories.

Markdown records are authoritative. status.md, profile.json and index.sqlite
are projections and may be rebuilt without changing workspace facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import agent_task

PROJECTS_DIR = Path(os.environ.get("WIKIGRAPH_PROJECTS_DIR", REPO / "projects"))
PROFILE_CONFIG_PATH = REPO / "operations" / "config" / "workspace-profiles.yaml"
STORE_NAME = ".workspace"
CONFIG_NAME = "workspace.yaml"
STATUS_MARKER = "<!-- generated-by: workspace_state -->"
WORKSPACE_SCHEMA = "workspace-v1"
ITEM_SCHEMA = "workspace-item-v1"
MEMORY_SCHEMA = "workspace-memory-v1"
PROFILE_SCHEMA = "workspace-profile-v1"
INDEX_SCHEMA_VERSION = "1"
MEMORY_STATES = frozenset({"active", "superseded", "expired"})
PROPOSAL_STATUSES = {
    "pending_submission": {"label": "待提", "state": "active", "next_action": "提交提案"},
    "pending_discussion": {"label": "待讨论", "state": "active", "next_action": "等待班子会讨论"},
    "discussed": {"label": "已讨论", "state": "done", "outcome": "已讨论（未记录结论）"},
}


class WorkspaceError(ValueError):
    """Workspace contract violation."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _utc_id_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def new_id(prefix: str) -> str:
    """Create an ID that does not depend on a central counter."""
    return f"{prefix}-{_utc_id_time()}-{secrets.token_hex(4).upper()}"


def _inside(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def resolve_project(value: str | Path, *, require_exists: bool = True) -> Path:
    """Resolve a project reference to a path physically contained in projects/."""
    base = PROJECTS_DIR.resolve()
    raw = Path(value)
    if raw.is_absolute():
        candidate = raw.resolve()
    else:
        parts = PurePosixPath(str(value)).parts
        if parts and parts[0] == "projects":
            parts = parts[1:]
        if not parts or ".." in parts:
            raise WorkspaceError(f"非法工作区路径: {value}")
        candidate = (base.joinpath(*parts)).resolve()
    if candidate == base or not _inside(candidate, base):
        raise WorkspaceError(f"工作区必须位于 projects/ 内: {value}")
    if require_exists and not candidate.is_dir():
        raise WorkspaceError(f"项目目录不存在: {candidate}")
    return candidate


def workspace_ref(root: Path) -> str:
    return root.resolve().relative_to(PROJECTS_DIR.resolve()).as_posix()


def repo_relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO.resolve()).as_posix()
    except ValueError:
        try:
            return "projects/" + resolved.relative_to(PROJECTS_DIR.resolve()).as_posix()
        except ValueError as exc:
            raise WorkspaceError(f"路径不在仓库或 projects/ 根内: {path}") from exc


def require_workspace(value: str | Path) -> Path:
    root = resolve_project(value)
    if not (root / CONFIG_NAME).is_file():
        raise WorkspaceError(f"尚未初始化工作区: projects/{workspace_ref(root)}")
    load_workspace_config(root)
    return root


def store_dir(root: Path) -> Path:
    return root / STORE_NAME


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl_atomic(path: Path, values: list[dict]) -> None:
    text = "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values)
    _atomic_write_text(path, text)


def read_jsonl_strict(path: Path) -> list[dict]:
    if not path.exists():
        return []
    values: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkspaceError(f"{path}:{number}: JSONL 无法解析: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise WorkspaceError(f"{path}:{number}: JSONL 条目必须是 object")
        values.append(value)
    return values


def append_event(root: Path, action: str, *, entity_type: str = "workspace",
                 entity_id: str = "", details: dict | None = None) -> dict:
    event = {
        "schema": "workspace-event-v1",
        "event_id": new_id("EVT"),
        "at": now_iso(),
        "workspace": workspace_ref(root),
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": details or {},
    }
    path = store_dir(root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    return event


def load_profile_config() -> dict:
    try:
        value = yaml.safe_load(PROFILE_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceError(f"无法读取 workspace profile 配置: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise WorkspaceError("workspace profile 配置必须是 version: 1 的 object")
    profiles = value.get("profiles")
    states = value.get("states")
    if not isinstance(profiles, dict) or not profiles:
        raise WorkspaceError("workspace profile 配置缺少 profiles")
    if not isinstance(states, list) or not all(isinstance(item, str) for item in states):
        raise WorkspaceError("workspace profile 配置缺少 states")
    return value


def profile_spec(name: str) -> dict:
    profiles = load_profile_config()["profiles"]
    if name not in profiles:
        raise WorkspaceError(f"未知 workspace profile: {name}; 可选 {sorted(profiles)}")
    spec = profiles[name]
    if not isinstance(spec, dict):
        raise WorkspaceError(f"workspace profile {name} 必须是 object")
    return spec


def default_profile(name: str) -> dict:
    values: dict[str, Any] = {}
    for field, definition in profile_spec(name)["profile_schema"]["fields"].items():
        field_type = definition.get("type")
        if field_type == "string":
            values[field] = ""
        elif field_type == "string_list":
            values[field] = []
        elif field_type == "enum":
            allowed = definition.get("values", [])
            values[field] = "unknown" if "unknown" in allowed else allowed[0]
    values.update({"updated_at": now_iso(), "schema": PROFILE_SCHEMA, "profile": name})
    return values


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkspaceError(f"{field} 必须是字符串列表")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _date_text(value: Any, field: str) -> str:
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"{field} 必须是 ISO 日期或时间字符串")
    text = value.strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise WorkspaceError(f"{field} 不是合法 ISO 日期或时间: {text}") from exc
    return text


def _optional_text(metadata: dict, field: str) -> str:
    value = metadata.get(field)
    return value.strip() if isinstance(value, str) else ""


def load_workspace_config(root: Path) -> dict:
    path = root / CONFIG_NAME
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceError(f"无法解析 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"{path} 必须是 YAML object")
    required = {"schema", "id", "name", "profile", "domain", "created_at"}
    missing = sorted(required - set(value))
    if missing:
        raise WorkspaceError(f"{path} 缺少字段: {', '.join(missing)}")
    if value.get("schema") != WORKSPACE_SCHEMA:
        raise WorkspaceError(f"{path} schema 应为 {WORKSPACE_SCHEMA}")
    if value.get("id") != workspace_ref(root):
        raise WorkspaceError(f"{path} id 应为 {workspace_ref(root)!r}")
    for field in ("name", "profile", "domain", "created_at"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise WorkspaceError(f"{path} {field} 必须是非空字符串")
    profile_spec(value["profile"])
    parent = value.get("parent")
    if parent is not None and (not isinstance(parent, str) or not parent.strip()):
        raise WorkspaceError(f"{path} parent 必须是路径字符串或 null")
    return value


def _nearest_parent_workspace(root: Path) -> Path | None:
    projects = PROJECTS_DIR.resolve()
    cursor = root.resolve().parent
    while cursor != projects and _inside(cursor, projects):
        if (cursor / CONFIG_NAME).is_file():
            return cursor
        cursor = cursor.parent
    return None


def _frontmatter_text(metadata: dict, body: str) -> str:
    dumped = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False,
                            default_flow_style=False).strip()
    return f"---\n{dumped}\n---\n\n{body.strip()}\n"


def read_markdown_record(path: Path) -> tuple[dict, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise WorkspaceError(f"无法读取 {path}: {exc}") from exc
    if not text.startswith("---\n"):
        raise WorkspaceError(f"{path}: 缺少 YAML frontmatter")
    parts = text.split("---\n", 2)
    if len(parts) != 3:
        raise WorkspaceError(f"{path}: YAML frontmatter 未闭合")
    try:
        metadata = yaml.safe_load(parts[1])
    except Exception as exc:
        raise WorkspaceError(f"{path}: YAML frontmatter 无法解析: {exc}") from exc
    if not isinstance(metadata, dict):
        raise WorkspaceError(f"{path}: YAML frontmatter 必须是 object")
    return metadata, parts[2].lstrip("\n").rstrip() + "\n"


def _record_paths(root: Path, kind: str) -> list[Path]:
    directory = store_dir(root) / ("items" if kind == "item" else "memories")
    if not directory.exists():
        return []
    return sorted(directory.glob("*.md"))


def validate_item(metadata: dict, profile_name: str, *, path: Path | None = None) -> dict:
    required = {"schema", "id", "title", "type", "state", "created_at", "updated_at", "tags", "links"}
    missing = sorted(required - set(metadata))
    if missing:
        raise WorkspaceError("事项缺少字段: " + ", ".join(missing))
    if metadata.get("schema") != ITEM_SCHEMA:
        raise WorkspaceError(f"事项 schema 应为 {ITEM_SCHEMA}")
    if not isinstance(metadata.get("id"), str) or not metadata["id"].startswith("ITEM-"):
        raise WorkspaceError("事项 id 必须以 ITEM- 开头")
    if path is not None and path.stem != metadata["id"]:
        raise WorkspaceError(f"事项文件名与 id 不一致: {path}")
    if not _optional_text(metadata, "title"):
        raise WorkspaceError("事项 title 必须是非空字符串")
    spec = profile_spec(profile_name)
    if metadata.get("type") not in spec.get("item_types", []):
        raise WorkspaceError(f"事项 type 不适用于 {profile_name}: {metadata.get('type')}")
    states = load_profile_config()["states"]
    state = metadata.get("state")
    if state not in states:
        raise WorkspaceError(f"事项 state 不在允许集合: {state}")
    _date_text(metadata["created_at"], "created_at")
    _date_text(metadata["updated_at"], "updated_at")
    metadata = dict(metadata)
    metadata["tags"] = _string_list(metadata["tags"], "tags")
    metadata["links"] = _string_list(metadata["links"], "links")
    requirements = {
        "active": ("next_action",),
        "waiting": ("waiting_on", "review_at"),
        "blocked": ("blocked_by",),
        "done": ("outcome",),
    }
    missing_state = [field for field in requirements.get(state, ()) if not _optional_text(metadata, field)]
    if missing_state:
        raise WorkspaceError(f"事项状态 {state} 缺少字段: {', '.join(missing_state)}")
    if metadata.get("review_at"):
        metadata["review_at"] = _date_text(metadata["review_at"], "review_at")
    if metadata.get("type") == "proposal":
        proposal_status = metadata.get("proposal_status")
        if proposal_status not in PROPOSAL_STATUSES:
            raise WorkspaceError("提案 proposal_status 必须是 pending_submission、pending_discussion 或 discussed")
        expected_state = PROPOSAL_STATUSES[proposal_status]["state"]
        if state != expected_state:
            raise WorkspaceError(f"提案状态 {proposal_status} 必须映射为事项 state={expected_state}")
        if metadata.get("discussed_at"):
            metadata["discussed_at"] = _date_text(metadata["discussed_at"], "discussed_at")
            if proposal_status != "discussed":
                raise WorkspaceError("只有已讨论提案可以设置 discussed_at")
    elif metadata.get("proposal_status") or metadata.get("discussed_at"):
        raise WorkspaceError("proposal_status 和 discussed_at 只适用于 proposal 类型事项")
    return metadata


def validate_memory(metadata: dict, profile_name: str, *, path: Path | None = None) -> dict:
    required = {"schema", "id", "title", "kind", "state", "created_at", "updated_at",
                "tags", "applies_to", "evidence"}
    missing = sorted(required - set(metadata))
    if missing:
        raise WorkspaceError("记忆缺少字段: " + ", ".join(missing))
    if metadata.get("schema") != MEMORY_SCHEMA:
        raise WorkspaceError(f"记忆 schema 应为 {MEMORY_SCHEMA}")
    if not isinstance(metadata.get("id"), str) or not metadata["id"].startswith("MEM-"):
        raise WorkspaceError("记忆 id 必须以 MEM- 开头")
    if path is not None and path.stem != metadata["id"]:
        raise WorkspaceError(f"记忆文件名与 id 不一致: {path}")
    if not _optional_text(metadata, "title"):
        raise WorkspaceError("记忆 title 必须是非空字符串")
    spec = profile_spec(profile_name)
    if metadata.get("kind") not in spec.get("memory_kinds", []):
        raise WorkspaceError(f"记忆 kind 不适用于 {profile_name}: {metadata.get('kind')}")
    if metadata.get("state") not in MEMORY_STATES:
        raise WorkspaceError(f"记忆 state 不在允许集合: {metadata.get('state')}")
    _date_text(metadata["created_at"], "created_at")
    _date_text(metadata["updated_at"], "updated_at")
    metadata = dict(metadata)
    for field in ("tags", "applies_to", "evidence"):
        metadata[field] = _string_list(metadata[field], field)
    for field in ("valid_from", "valid_to"):
        if metadata.get(field):
            metadata[field] = _date_text(metadata[field], field)
    if metadata.get("valid_from") and metadata.get("valid_to"):
        if str(metadata["valid_from"])[:10] > str(metadata["valid_to"])[:10]:
            raise WorkspaceError("valid_from 不得晚于 valid_to")
    if metadata.get("state") == "superseded" and not _optional_text(metadata, "superseded_by"):
        raise WorkspaceError("superseded 记忆必须给出 superseded_by")
    return metadata


def load_items(root: Path) -> list[tuple[dict, str, Path]]:
    profile_name = load_workspace_config(root)["profile"]
    records = []
    seen: set[str] = set()
    for path in _record_paths(root, "item"):
        metadata, body = read_markdown_record(path)
        metadata = validate_item(metadata, profile_name, path=path)
        if metadata["id"] in seen:
            raise WorkspaceError(f"重复事项 id: {metadata['id']}")
        seen.add(metadata["id"])
        records.append((metadata, body, path))
    return records


def load_memories(root: Path) -> list[tuple[dict, str, Path]]:
    profile_name = load_workspace_config(root)["profile"]
    records = []
    seen: set[str] = set()
    for path in _record_paths(root, "memory"):
        metadata, body = read_markdown_record(path)
        metadata = validate_memory(metadata, profile_name, path=path)
        if metadata["id"] in seen:
            raise WorkspaceError(f"重复记忆 id: {metadata['id']}")
        seen.add(metadata["id"])
        records.append((metadata, body, path))
    return records


def _hash_record(metadata: dict, body: str) -> str:
    value = json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\0" + body
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_record(path: Path, metadata: dict, body: str) -> None:
    _atomic_write_text(path, _frontmatter_text(metadata, body))


def add_item(value: str | Path, *, title: str, item_type: str | None = None,
             state: str = "planned", content: str = "", tags: list[str] | None = None,
             links: list[str] | None = None, next_action: str = "", waiting_on: str = "",
             review_at: str = "", blocked_by: str = "", outcome: str = "",
             proposal_status: str = "", discussed_at: str = "") -> dict:
    root = require_workspace(value)
    config = load_workspace_config(root)
    item_types = profile_spec(config["profile"])["item_types"]
    timestamp = now_iso()
    item_id = new_id("ITEM")
    metadata = {
        "schema": ITEM_SCHEMA,
        "id": item_id,
        "title": title.strip(),
        "type": item_type or item_types[0],
        "state": state,
        "created_at": timestamp,
        "updated_at": timestamp,
        "tags": tags or [],
        "links": links or [],
    }
    optional = {
        "next_action": next_action,
        "waiting_on": waiting_on,
        "review_at": review_at,
        "blocked_by": blocked_by,
        "outcome": outcome,
        "proposal_status": proposal_status,
        "discussed_at": discussed_at,
    }
    metadata.update({key: val.strip() for key, val in optional.items() if val and val.strip()})
    metadata = validate_item(metadata, config["profile"])
    path = store_dir(root) / "items" / f"{item_id}.md"
    if path.exists():
        raise WorkspaceError(f"事项 ID 碰撞，未覆盖现有记录: {item_id}")
    _write_record(path, metadata, content)
    append_event(root, "item.add", entity_type="item", entity_id=item_id,
                 details={"state": state, "record_hash": _hash_record(metadata, content)})
    refresh_projections(root)
    return {"status": "saved", "workspace": workspace_ref(root), "id": item_id,
            "state": state, "path": repo_relative(path)}


def _find_record(root: Path, kind: str, record_id: str) -> tuple[dict, str, Path]:
    directory = "items" if kind == "item" else "memories"
    path = store_dir(root) / directory / f"{record_id}.md"
    if not path.is_file():
        raise WorkspaceError(f"{kind} 不存在: {record_id}")
    metadata, body = read_markdown_record(path)
    profile_name = load_workspace_config(root)["profile"]
    metadata = (validate_item(metadata, profile_name, path=path) if kind == "item"
                else validate_memory(metadata, profile_name, path=path))
    return metadata, body, path


def update_item(value: str | Path, item_id: str, changes: dict[str, Any],
                *, clear: list[str] | None = None) -> dict:
    root = require_workspace(value)
    changes = dict(changes)
    metadata, body, path = _find_record(root, "item", item_id)
    before_hash = _hash_record(metadata, body)
    allowed = {"title", "type", "state", "next_action", "waiting_on", "review_at",
               "blocked_by", "outcome", "proposal_status", "discussed_at",
               "tags", "links", "content"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise WorkspaceError("不允许更新字段: " + ", ".join(unknown))
    new_body = str(changes.pop("content")) if "content" in changes else body
    for field in clear or []:
        metadata.pop(field, None)
    metadata.update({key: value for key, value in changes.items() if value is not None})
    metadata["updated_at"] = now_iso()
    metadata = validate_item(metadata, load_workspace_config(root)["profile"])
    _write_record(path, metadata, new_body)
    after_hash = _hash_record(metadata, new_body)
    append_event(root, "item.update", entity_type="item", entity_id=item_id,
                 details={"before_hash": before_hash, "after_hash": after_hash,
                          "state": metadata["state"]})
    refresh_projections(root)
    return {"status": "updated", "workspace": workspace_ref(root), "id": item_id,
            "state": metadata["state"]}


def _proposal_state_fields(proposal_status: str, *, outcome: str = "",
                           discussed_at: str = "") -> dict[str, str]:
    if proposal_status not in PROPOSAL_STATUSES:
        raise WorkspaceError("提案状态必须是 pending_submission、pending_discussion 或 discussed")
    spec = PROPOSAL_STATUSES[proposal_status]
    fields = {"proposal_status": proposal_status, "state": spec["state"]}
    if proposal_status == "discussed":
        fields["outcome"] = outcome.strip() or spec["outcome"]
        if discussed_at.strip():
            fields["discussed_at"] = discussed_at.strip()
    else:
        fields["next_action"] = spec["next_action"]
    return fields


def add_proposal(value: str | Path, *, title: str,
                 proposal_status: str = "pending_submission", content: str = "",
                 tags: list[str] | None = None, links: list[str] | None = None,
                 outcome: str = "", discussed_at: str = "") -> dict:
    if proposal_status != "discussed" and (outcome.strip() or discussed_at.strip()):
        raise WorkspaceError("只有已讨论提案可以记录 outcome 或 discussed_at")
    fields = _proposal_state_fields(proposal_status, outcome=outcome,
                                    discussed_at=discussed_at)
    return add_item(value, title=title, item_type="proposal", content=content,
                    tags=tags, links=links, **fields)


def update_proposal(value: str | Path, item_id: str, changes: dict[str, Any],
                    *, proposal_status: str | None = None,
                    outcome: str | None = None, discussed_at: str | None = None) -> dict:
    root = require_workspace(value)
    metadata, _body, _path = _find_record(root, "item", item_id)
    if metadata.get("type") != "proposal":
        raise WorkspaceError(f"事项不是提案: {item_id}")
    target_status = proposal_status or metadata["proposal_status"]
    target_outcome = metadata.get("outcome", "") if outcome is None else outcome
    target_discussed_at = metadata.get("discussed_at", "") if discussed_at is None else discussed_at
    if target_status != "discussed" and (outcome is not None or discussed_at is not None):
        raise WorkspaceError("只有已讨论提案可以记录 outcome 或 discussed_at")
    mapped = _proposal_state_fields(target_status, outcome=target_outcome,
                                    discussed_at=target_discussed_at)
    changes = dict(changes)
    changes.update(mapped)
    clear = ["waiting_on", "review_at", "blocked_by"]
    if target_status == "discussed":
        clear.append("next_action")
        if not target_discussed_at:
            clear.append("discussed_at")
    else:
        clear.extend(["outcome", "discussed_at"])
    return update_item(root, item_id, changes, clear=clear)


def list_proposals(value: str | Path, *, proposal_status: str = "") -> dict:
    root = require_workspace(value)
    if proposal_status and proposal_status not in PROPOSAL_STATUSES:
        raise WorkspaceError("提案状态必须是 pending_submission、pending_discussion 或 discussed")
    entries = [metadata for metadata, _body, _path in load_items(root)
               if metadata.get("type") == "proposal"
               and (not proposal_status or metadata.get("proposal_status") == proposal_status)]
    entries.sort(key=lambda item: (list(PROPOSAL_STATUSES).index(item["proposal_status"]),
                                   item.get("updated_at", ""), item["id"]))
    return {"workspace": workspace_ref(root), "count": len(entries), "proposals": entries}


def add_memory(value: str | Path, *, title: str, kind: str, content: str,
               tags: list[str] | None = None, applies_to: list[str] | None = None,
               evidence: list[str] | None = None, valid_from: str = "", valid_to: str = "",
               supersedes: str = "") -> dict:
    root = require_workspace(value)
    if not content.strip():
        raise WorkspaceError("记忆 content 不能为空")
    timestamp = now_iso()
    memory_id = new_id("MEM")
    metadata = {
        "schema": MEMORY_SCHEMA,
        "id": memory_id,
        "title": title.strip(),
        "kind": kind,
        "state": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
        "tags": tags or [],
        "applies_to": applies_to or [],
        "evidence": evidence or [],
    }
    for field, val in (("valid_from", valid_from), ("valid_to", valid_to),
                       ("supersedes", supersedes)):
        if val and val.strip():
            metadata[field] = val.strip()
    profile_name = load_workspace_config(root)["profile"]
    metadata = validate_memory(metadata, profile_name)
    previous: tuple[dict, str, Path] | None = None
    if supersedes:
        previous = _find_record(root, "memory", supersedes)
        if previous[0]["state"] != "active":
            raise WorkspaceError(f"只能替代 active 记忆: {supersedes}")
    path = store_dir(root) / "memories" / f"{memory_id}.md"
    if path.exists():
        raise WorkspaceError(f"记忆 ID 碰撞，未覆盖现有记录: {memory_id}")
    _write_record(path, metadata, content)
    if previous:
        old_meta, old_body, old_path = previous
        old_meta["state"] = "superseded"
        old_meta["superseded_by"] = memory_id
        old_meta["updated_at"] = timestamp
        old_meta = validate_memory(old_meta, profile_name)
        try:
            _write_record(old_path, old_meta, old_body)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    append_event(root, "memory.add", entity_type="memory", entity_id=memory_id,
                 details={"kind": kind, "supersedes": supersedes or None,
                          "record_hash": _hash_record(metadata, content)})
    refresh_projections(root)
    return {"status": "saved", "workspace": workspace_ref(root), "id": memory_id,
            "state": "active", "supersedes": supersedes or None,
            "path": repo_relative(path)}


def expire_memory(value: str | Path, memory_id: str, *, valid_to: str = "") -> dict:
    root = require_workspace(value)
    metadata, body, path = _find_record(root, "memory", memory_id)
    if metadata["state"] == "superseded":
        raise WorkspaceError("已被替代的记忆不能改为 expired")
    metadata["state"] = "expired"
    metadata["updated_at"] = now_iso()
    if valid_to:
        metadata["valid_to"] = valid_to
    metadata = validate_memory(metadata, load_workspace_config(root)["profile"])
    _write_record(path, metadata, body)
    append_event(root, "memory.expire", entity_type="memory", entity_id=memory_id,
                 details={"valid_to": metadata.get("valid_to")})
    refresh_projections(root)
    return {"status": "expired", "workspace": workspace_ref(root), "id": memory_id}


def _record_row(metadata: dict, body: str, path: Path, root: Path) -> tuple:
    return (
        metadata["id"], metadata.get("title", ""), metadata.get("type") or metadata.get("kind", ""),
        metadata["state"], metadata.get("created_at", ""), metadata.get("updated_at", ""),
        json.dumps(metadata.get("tags", []), ensure_ascii=False),
        path.relative_to(root).as_posix(), body,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )


def rebuild_index(root: Path) -> dict:
    root = require_workspace(root)
    items = load_items(root)
    memories = load_memories(root)
    directory = store_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".index.", suffix=".sqlite", dir=directory)
    os.close(fd)
    try:
        with sqlite3.connect(temporary) as conn:
            conn.executescript("""
                PRAGMA journal_mode=DELETE;
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE items (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, type TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    tags_json TEXT NOT NULL, source_path TEXT NOT NULL, body TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    tags_json TEXT NOT NULL, source_path TEXT NOT NULL, body TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE INDEX items_state_idx ON items(state, updated_at);
                CREATE INDEX memories_state_idx ON memories(state, updated_at);
            """)
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         ("schema_version", INDEX_SCHEMA_VERSION))
            conn.execute("INSERT INTO metadata(key, value) VALUES (?, ?)",
                         ("workspace", workspace_ref(root)))
            conn.executemany("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [_record_row(*record, root) for record in items])
            conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [_record_row(*record, root) for record in memories])
            conn.commit()
        os.replace(temporary, directory / "index.sqlite")
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return {"items": len(items), "memories": len(memories)}


def _direct_child_workspaces(root: Path) -> list[Path]:
    children: list[Path] = []
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        directories[:] = [name for name in directories if not name.startswith(".") and name not in {"archive", "node_modules", "__pycache__"}]
        if current_path != root and CONFIG_NAME in files:
            children.append(current_path)
            directories[:] = []
    return sorted(children)


def workspace_summary(root: Path) -> dict:
    config = load_workspace_config(root)
    items = [metadata for metadata, _body, _path in load_items(root)]
    counts = {state: 0 for state in load_profile_config()["states"]}
    for item in items:
        counts[item["state"]] += 1
    proposal_counts = {
        status: sum(item.get("type") == "proposal" and item.get("proposal_status") == status
                    for item in items)
        for status in PROPOSAL_STATUSES
    }
    return {"id": config["id"], "name": config["name"], "profile": config["profile"],
            "domain": config["domain"], "counts": counts,
            "proposal_counts": proposal_counts, "total": len(items)}


def render_status(root: Path) -> str:
    config = load_workspace_config(root)
    items = [metadata for metadata, _body, _path in load_items(root)]
    memories = [metadata for metadata, _body, _path in load_memories(root)]
    order = {name: number for number, name in enumerate(load_profile_config()["states"])}
    items.sort(key=lambda item: (order.get(item["state"], 99), item.get("updated_at", ""), item["id"]))
    counts = {state: 0 for state in load_profile_config()["states"]}
    for item in items:
        counts[item["state"]] += 1
    lines = [STATUS_MARKER, f"# {config['name']}状态", "",
             f"- 工作区：`projects/{config['id']}`",
             f"- Profile：`{config['profile']}`",
             f"- Domain：`{config['domain']}`",
             f"- 更新时间：{now_iso()}", "", "## 概览", ""]
    lines.append("- " + "；".join(f"{state}: {counts[state]}" for state in counts))
    lines.append(f"- 有效记忆：{sum(1 for memory in memories if memory['state'] == 'active')} 条")
    proposals = [item for item in items if item.get("type") == "proposal"]
    if config["profile"] == "role_work":
        proposal_counts = {status: sum(item["proposal_status"] == status for item in proposals)
                           for status in PROPOSAL_STATUSES}
        lines.append("- 提案：" + "；".join(
            f"{spec['label']} {proposal_counts[status]}"
            for status, spec in PROPOSAL_STATUSES.items()))
        lines.extend(["", "## 提案", ""])
        for proposal_status, spec in PROPOSAL_STATUSES.items():
            selected = [item for item in proposals if item["proposal_status"] == proposal_status]
            lines.extend([f"### {spec['label']}", ""])
            if selected:
                for item in selected:
                    lines.append(f"- `{item['id']}` {item['title']}")
                    for field, label in (("discussed_at", "讨论日期"), ("outcome", "结果")):
                        if _optional_text(item, field):
                            lines.append(f"  - {label}：{item[field]}")
            else:
                lines.append("- 无")
            lines.append("")
        lines.pop()
    for state in ("active", "waiting", "blocked", "planned", "done", "cancelled"):
        selected = [item for item in items if item["state"] == state
                    and item.get("type") != "proposal"]
        if not selected:
            continue
        lines.extend(["", f"## {state}", ""])
        for item in selected:
            lines.append(f"- `{item['id']}` {item['title']}")
            for field, label in (("next_action", "下一步"), ("waiting_on", "等待"),
                                 ("review_at", "复核时间"), ("blocked_by", "阻塞"),
                                 ("outcome", "结果")):
                if _optional_text(item, field):
                    lines.append(f"  - {label}：{item[field]}")
    children = _direct_child_workspaces(root)
    if children:
        lines.extend(["", "## 子工作区摘要", ""])
        for child in children:
            summary = workspace_summary(child)
            active = summary["counts"]["active"]
            waiting = summary["counts"]["waiting"]
            blocked = summary["counts"]["blocked"]
            lines.append(f"- `projects/{summary['id']}`：active {active}，waiting {waiting}，blocked {blocked}，总计 {summary['total']}")
    lines.extend(["", "> 本页由 `.scripts/workspace_state.py rebuild` 从权威 Markdown 记录生成。", ""])
    return "\n".join(lines)


def refresh_status(root: Path) -> None:
    _atomic_write_text(root / "notes" / "status.md", render_status(root))


def refresh_ancestor_statuses(root: Path) -> None:
    parent = _nearest_parent_workspace(root)
    while parent is not None:
        refresh_status(parent)
        parent = _nearest_parent_workspace(parent)


def refresh_projections(root: Path) -> dict:
    counts = rebuild_index(root)
    profile_path = store_dir(root) / "profile.json"
    if not profile_path.exists():
        save_profile(root, default_profile(load_workspace_config(root)["profile"]))
    refresh_status(root)
    refresh_ancestor_statuses(root)
    return counts


def init_workspace(value: str | Path, *, name: str = "", profile: str = "generic",
                   domain: str = "general") -> dict:
    root = resolve_project(value, require_exists=False)
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_NAME
    if config_path.exists():
        raise WorkspaceError(f"工作区已经初始化: projects/{workspace_ref(root)}")
    profile_value = profile_spec(profile)
    if not isinstance(domain, str) or not domain.strip():
        raise WorkspaceError("domain 必须是非空字符串")
    previous_status = root / "notes" / "status.md"
    previous_text = previous_status.read_text(encoding="utf-8") if previous_status.is_file() else ""
    parent = _nearest_parent_workspace(root)
    config = {
        "schema": WORKSPACE_SCHEMA,
        "id": workspace_ref(root),
        "name": (name or root.name).strip(),
        "profile": profile,
        "domain": domain.strip(),
        "parent": workspace_ref(parent) if parent else None,
        "created_at": now_iso(),
    }
    _atomic_write_text(config_path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    (store_dir(root) / "items").mkdir(parents=True, exist_ok=True)
    (store_dir(root) / "memories").mkdir(parents=True, exist_ok=True)
    append_event(root, "workspace.init", details={"profile": profile, "domain": domain})
    imported_id = ""
    if previous_text.strip() and STATUS_MARKER not in previous_text:
        imported_id = new_id("MEM")
        timestamp = now_iso()
        kinds = profile_value["memory_kinds"]
        kind = "context" if "context" in kinds else kinds[0]
        metadata = validate_memory({
            "schema": MEMORY_SCHEMA,
            "id": imported_id,
            "title": "初始化前工作状态",
            "kind": kind,
            "state": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
            "tags": ["imported-status"],
            "applies_to": [workspace_ref(root)],
            "evidence": [f"projects/{workspace_ref(root)}/notes/status.md@before-workspace-init"],
        }, profile)
        imported_path = store_dir(root) / "memories" / f"{imported_id}.md"
        if imported_path.exists():
            raise WorkspaceError(f"导入记忆 ID 碰撞: {imported_id}")
        _write_record(imported_path, metadata, previous_text)
        append_event(root, "memory.import_status", entity_type="memory", entity_id=imported_id)
    counts = refresh_projections(root)
    return {"status": "initialized", "workspace": workspace_ref(root), "profile": profile,
            "domain": domain, "parent": config["parent"], "imported_status_memory": imported_id or None,
            **counts}


def _memory_effective(metadata: dict, today: str | None = None) -> bool:
    if metadata.get("state") != "active":
        return False
    current = today or date.today().isoformat()
    valid_from = str(metadata.get("valid_from") or "")[:10]
    valid_to = str(metadata.get("valid_to") or "")[:10]
    return (not valid_from or valid_from <= current) and (not valid_to or current <= valid_to)


def recall_text(value: str | Path, *, memory_limit: int = 10) -> str:
    root = require_workspace(value)
    config = load_workspace_config(root)
    items = [metadata for metadata, _body, _path in load_items(root)]
    memories = [metadata for metadata, _body, _path in load_memories(root)]
    proposals = [item for item in items if item.get("type") == "proposal"]
    current_items = [item for item in items
                     if item["state"] in {"active", "waiting", "blocked"}
                     and item.get("type") != "proposal"]
    current_items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    effective = [memory for memory in memories if _memory_effective(memory)]
    effective.sort(key=lambda memory: memory.get("updated_at", ""), reverse=True)
    lines = [f"[工作区] projects/{config['id']}", f"- name: {config['name']}",
             f"- profile: {config['profile']}", f"- domain: {config['domain']}"]
    profile = load_profile(root)
    if profile:
        lines.extend(["", "[画像投影]"])
        for field in profile_spec(config["profile"])["profile_schema"]["required"]:
            value = profile.get(field)
            if isinstance(value, list):
                lines.append(f"- {field}: {', '.join(str(item) for item in value[:8]) or '-'}")
            else:
                lines.append(f"- {field}: {value or '-'}")
    lines.extend(["", f"[当前事项]（共 {len(items)} 条，显示 active/waiting/blocked）"])
    if current_items:
        for item in current_items:
            detail = item.get("next_action") or item.get("waiting_on") or item.get("blocked_by") or ""
            suffix = f"；{detail}" if detail else ""
            lines.append(f"- {item['id']} [{item['state']}] {item['title']}{suffix}")
    else:
        lines.append("- 无")
    if config["profile"] == "role_work":
        lines.extend(["", f"[提案]（共 {len(proposals)} 条）"])
        for proposal_status, spec in PROPOSAL_STATUSES.items():
            selected = [item for item in proposals if item["proposal_status"] == proposal_status]
            lines.append(f"- {spec['label']}（{len(selected)}）")
            for item in selected:
                detail = item.get("outcome") or item.get("discussed_at") or ""
                suffix = f"；{detail}" if detail else ""
                lines.append(f"  - {item['id']} {item['title']}{suffix}")
    lines.extend(["", f"[有效记忆]（共 {len(effective)} 条，显示最近 {min(memory_limit, len(effective))} 条）"])
    for memory in effective[:memory_limit]:
        lines.append(f"- {memory['id']} [{memory['kind']}] {memory['title']}")
    if not effective:
        lines.append("- 无")
    children = _direct_child_workspaces(root)
    if children:
        lines.extend(["", "[直接子工作区摘要]"])
        for child in children:
            summary = workspace_summary(child)
            counts = summary["counts"]
            lines.append(f"- projects/{summary['id']}: active={counts['active']}, waiting={counts['waiting']}, blocked={counts['blocked']}, total={summary['total']}")
        lines.append("- 子工作区记忆未召回；进入子工作区后单独 recall。")
    return "\n".join(lines) + "\n"


def validate_profile_value(value: dict, profile_name: str) -> list[str]:
    if not isinstance(value, dict):
        return ["profile 必须是 JSON object"]
    schema = profile_spec(profile_name)["profile_schema"]
    errors: list[str] = []
    missing = [field for field in schema["required"] if field not in value]
    if missing:
        errors.append("缺少字段: " + ", ".join(missing))
    for field, definition in schema["fields"].items():
        field_value = value.get(field)
        field_type = definition.get("type")
        if field_type == "string" and not isinstance(field_value, str):
            errors.append(f"{field} 必须是字符串")
        elif field_type == "string_list" and (not isinstance(field_value, list) or
                                                not all(isinstance(item, str) for item in field_value)):
            errors.append(f"{field} 必须是字符串列表")
        elif field_type == "enum" and field_value not in definition.get("values", []):
            errors.append(f"{field} 不在允许集合")
    return errors


def normalize_profile(value: dict, profile_name: str) -> dict:
    errors = validate_profile_value(value, profile_name)
    if errors:
        raise WorkspaceError("; ".join(errors))
    fields = profile_spec(profile_name)["profile_schema"]["fields"]
    normalized: dict[str, Any] = {}
    for field in fields:
        current = value[field]
        if isinstance(current, list):
            normalized[field] = list(dict.fromkeys(item.strip() for item in current if item.strip()))
        elif isinstance(current, str):
            normalized[field] = current.strip()
        else:
            normalized[field] = current
    normalized["updated_at"] = now_iso()
    normalized["schema"] = PROFILE_SCHEMA
    normalized["profile"] = profile_name
    return normalized


def load_profile(root_or_value: str | Path) -> dict:
    root = root_or_value if isinstance(root_or_value, Path) and root_or_value.is_dir() else require_workspace(root_or_value)
    path = store_dir(root) / "profile.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceError(f"无法解析 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkspaceError(f"{path} 必须是 JSON object")
    return value


def save_profile(root: Path, value: dict) -> None:
    atomic_write_json(store_dir(root) / "profile.json", value)


def collect_bounded_text(root: Path, *, excluded_managed: set[str] | None = None,
                         max_chars: int = 20_000, per_file: int = 3_000) -> str:
    """Collect a bounded snapshot without crossing nested workspace boundaries."""
    excluded = {STORE_NAME, ".research-memory", ".git", "archive", "node_modules", "__pycache__"}
    excluded.update(excluded_managed or set())
    binary = {".pdf", ".doc", ".docx", ".zip", ".gz", ".png", ".jpg", ".jpeg",
              ".sqlite", ".db", ".ppt", ".pptx", ".xls", ".xlsx", ".dvi"}
    parts: list[str] = []
    size = 0
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        if current_path != root and (current_path / CONFIG_NAME).is_file():
            directories[:] = []
            continue
        directories[:] = [name for name in directories if name not in excluded and not name.startswith(".")]
        for filename in sorted(files):
            path = current_path / filename
            if path.suffix.lower() in binary or path.name == "status.md" and path.parent.name == "notes":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if not text.strip():
                continue
            rel = path.relative_to(root).as_posix()
            chunk = f"=== {rel} ===\n{text[:per_file]}"
            remaining = max_chars - size
            if remaining <= 0:
                break
            parts.append(chunk[:remaining])
            size += len(parts[-1]) + 2
        if size >= max_chars:
            break
    return "\n\n".join(parts)


def collect_workspace_text(value: str | Path) -> str:
    root = require_workspace(value)
    artifact_text = collect_bounded_text(root, max_chars=14_000)
    records: list[str] = []
    for metadata, body, _path in load_items(root):
        records.append(f"ITEM {metadata['id']} {metadata['state']} {metadata['title']}\n{body[:800]}")
    for metadata, body, _path in load_memories(root):
        if _memory_effective(metadata):
            records.append(f"MEMORY {metadata['id']} {metadata['kind']} {metadata['title']}\n{body[:800]}")
    return (artifact_text + "\n\n" + "\n\n".join(records))[:20_000].strip()


def prepare_profile_task(root: Path, text: str, profile_name: str, *,
                         namespace: str = "workspace-state", kind: str = "workspace_profile",
                         output_format: str = "workspace-profile-v1",
                         validator: str = "workspace_state.validate_profile_value",
                         command: str | None = None) -> dict:
    reference = workspace_ref(root)
    digest = hashlib.sha256((reference + "\0" + profile_name + "\0" + text).encode("utf-8")).hexdigest()[:16]
    directory = REPO / "temp" / namespace
    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / f"{digest}-workspace.txt"
    output_path = directory / f"{digest}-profile.json"
    _atomic_write_text(input_path, text[:15_000])
    commit = command or (
        "python3 .scripts/workspace_state.py profile "
        f"{json.dumps(reference, ensure_ascii=False)} --apply-profile "
        f"{repo_relative(output_path)}"
    )
    schema = profile_spec(profile_name)["profile_schema"]
    return agent_task.make_task(
        kind=kind,
        transaction_id=f"workspace-profile-{digest}",
        inputs=[{"name": "workspace_snapshot", "path": repo_relative(input_path),
                 "role": "bounded_workspace_evidence", "read": "full"}],
        outputs=[{"name": "profile", "path": repo_relative(output_path),
                  "format": output_format}],
        protocol={"name": output_format, "fields": schema["fields"], "validator": validator},
        commands={"commit": commit},
        context={"workspace": reference, "profile": profile_name},
    )


def apply_profile(value: str | Path, path: str | Path) -> dict:
    root = require_workspace(value)
    resolved = agent_task.resolve_temp_artifact(REPO, path, "workspace-state")
    try:
        candidate = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceError(f"画像 JSON 无法解析: {exc}") from exc
    profile_name = load_workspace_config(root)["profile"]
    normalized = normalize_profile(candidate, profile_name)
    save_profile(root, normalized)
    append_event(root, "profile.apply", entity_type="profile",
                 details={"profile": profile_name, "source": repo_relative(resolved)})
    return {"status": "completed", "workspace": workspace_ref(root), "profile": normalized}


def _profile_prompt(profile_name: str, text: str) -> str:
    schema = profile_spec(profile_name)["profile_schema"]
    output = {}
    requirements = []
    for field, definition in schema["fields"].items():
        field_type = definition["type"]
        if field_type == "string":
            output[field] = "..."
        elif field_type == "string_list":
            output[field] = ["..."]
        else:
            output[field] = definition["values"][0]
        requirements.append(f"- {field}: {json.dumps(definition, ensure_ascii=False)}")
    return (f"分析以下 {profile_name} 工作区的有界材料，生成接续工作所需的结构化画像。"
            "只输出严格 JSON，不使用 Markdown 包裹。\n\n字段规则：\n"
            + "\n".join(requirements) + "\n\n[工作区材料]\n" + text[:15_000]
            + "\n\n[输出示例]\n" + json.dumps(output, ensure_ascii=False))


def refresh_profile(value: str | Path, *, backend_getter: Callable[[], str] | None = None) -> dict:
    root = require_workspace(value)
    text = collect_workspace_text(root)
    profile_name = load_workspace_config(root)["profile"]
    if not text:
        return {"status": "empty", "workspace": workspace_ref(root), "profile": load_profile(root)}
    backend = backend_getter() if backend_getter else agent_task.backend("WORKSPACE_BACKEND")
    if backend == "agent":
        return prepare_profile_task(root, text, profile_name)
    try:
        from llm_structured import call_text
        result = call_text(_profile_prompt(profile_name, text), max_tokens=2048, retries=1,
                           operation="workspace_profile",
                           system="你从有界工作区材料中提取结构化接续画像，不补造材料外事实。")
        if not result.get("ok"):
            return {"status": "failed", "error": result.get("error") or "API 调用失败",
                    "profile": load_profile(root)}
        raw = str(result.get("text") or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            return {"status": "failed", "error": "API 未返回 JSON object", "profile": load_profile(root)}
        candidate = json.loads(raw[start:end + 1])
        normalized = normalize_profile(candidate, profile_name)
        save_profile(root, normalized)
        append_event(root, "profile.refresh", entity_type="profile",
                     details={"profile": profile_name, "backend": "api"})
        return normalized
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "profile": load_profile(root)}


def doctor(value: str | Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        root = require_workspace(value)
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)], "warnings": [], "counts": {}}
    try:
        config = load_workspace_config(root)
    except Exception as exc:
        config = {}
        errors.append(str(exc))
    try:
        items = load_items(root)
    except Exception as exc:
        items = []
        errors.append(str(exc))
    try:
        memories = load_memories(root)
    except Exception as exc:
        memories = []
        errors.append(str(exc))
    try:
        events = read_jsonl_strict(store_dir(root) / "events.jsonl")
    except Exception as exc:
        events = []
        errors.append(str(exc))
    memory_map = {metadata["id"]: metadata for metadata, _body, _path in memories}
    for metadata, _body, path in memories:
        target = metadata.get("supersedes")
        if target and target not in memory_map:
            errors.append(f"{path}: supersedes 指向不存在的记忆 {target}")
        elif target:
            previous = memory_map[target]
            if previous.get("state") != "superseded" or previous.get("superseded_by") != metadata["id"]:
                errors.append(f"{path}: supersedes 与旧记忆状态不是双向一致关系")
        successor = metadata.get("superseded_by")
        if successor and successor not in memory_map:
            errors.append(f"{path}: superseded_by 指向不存在的记忆 {successor}")
        elif successor and memory_map[successor].get("supersedes") != metadata["id"]:
            errors.append(f"{path}: superseded_by 与新记忆状态不是双向一致关系")
    parent = _nearest_parent_workspace(root)
    expected_parent = workspace_ref(parent) if parent else None
    if config and config.get("parent") != expected_parent:
        errors.append(f"workspace.yaml parent 应为 {expected_parent!r}")
    try:
        projected_profile = load_profile(root)
        if not projected_profile:
            warnings.append("缺少 profile.json；运行 rebuild")
        elif config:
            profile_errors = validate_profile_value(projected_profile, config["profile"])
            if projected_profile.get("schema") != PROFILE_SCHEMA:
                profile_errors.append(f"schema 应为 {PROFILE_SCHEMA}")
            if projected_profile.get("profile") != config["profile"]:
                profile_errors.append("profile 与 workspace.yaml 不一致")
            errors.extend(f"profile.json: {message}" for message in profile_errors)
    except Exception as exc:
        errors.append(str(exc))
    index = store_dir(root) / "index.sqlite"
    if not index.is_file():
        warnings.append("缺少 index.sqlite；运行 rebuild")
    else:
        try:
            with sqlite3.connect(f"file:{index}?mode=ro", uri=True) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
                indexed_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                indexed_memories = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if check != "ok":
                errors.append(f"index.sqlite quick_check: {check}")
            if indexed_items != len(items) or indexed_memories != len(memories):
                warnings.append("index.sqlite 与 Markdown 条目数量不一致；运行 rebuild")
        except Exception as exc:
            errors.append(f"index.sqlite 无法读取: {exc}")
    status_path = root / "notes" / "status.md"
    if not status_path.is_file() or STATUS_MARKER not in status_path.read_text(encoding="utf-8"):
        warnings.append("缺少受管 status.md 投影；运行 rebuild")
    return {"ok": not errors, "workspace": workspace_ref(root), "errors": errors,
            "warnings": warnings, "counts": {"items": len(items), "memories": len(memories),
                                                   "events": len(events)}}


def rebuild(value: str | Path) -> dict:
    root = require_workspace(value)
    counts = refresh_projections(root)
    append_event(root, "workspace.rebuild", details=counts)
    return {"status": "rebuilt", "workspace": workspace_ref(root), **counts}


def _csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _content(args: argparse.Namespace, *, required: bool = False) -> str:
    value = sys.stdin.read() if getattr(args, "stdin", False) else getattr(args, "content", "")
    if required and not value.strip():
        raise WorkspaceError("需用 --content 或 --stdin 提供内容")
    return value


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="初始化通用工作区")
    init.add_argument("workspace"); init.add_argument("--name", default="")
    init.add_argument("--profile", default="generic", choices=sorted(load_profile_config()["profiles"]))
    init.add_argument("--domain", default="general")
    init.set_defaults(run=lambda args: _json(init_workspace(args.workspace, name=args.name,
                                                             profile=args.profile, domain=args.domain)))

    recall = sub.add_parser("recall", help="恢复当前工作区上下文")
    recall.add_argument("workspace")
    recall.set_defaults(run=lambda args: print(recall_text(args.workspace), end=""))

    status = sub.add_parser("status", help="返回当前工作区摘要")
    status.add_argument("workspace")
    status.set_defaults(run=lambda args: _json(workspace_summary(require_workspace(args.workspace))))

    rebuild_parser = sub.add_parser("rebuild", help="从 Markdown 重建投影")
    rebuild_parser.add_argument("workspace")
    rebuild_parser.set_defaults(run=lambda args: _json(rebuild(args.workspace)))

    doctor_parser = sub.add_parser("doctor", help="检查权威记录和投影一致性")
    doctor_parser.add_argument("workspace")
    def run_doctor(args: argparse.Namespace) -> int:
        report = doctor(args.workspace)
        _json(report)
        return 0 if report["ok"] else 1
    doctor_parser.set_defaults(run=run_doctor)

    profile_parser = sub.add_parser("profile", help="读取或刷新工作区画像")
    profile_parser.add_argument("workspace"); profile_parser.add_argument("--refresh", action="store_true")
    profile_parser.add_argument("--apply-profile", default="")
    def run_profile(args: argparse.Namespace) -> None:
        if args.apply_profile:
            _json(apply_profile(args.workspace, args.apply_profile))
        elif args.refresh:
            _json(refresh_profile(args.workspace))
        else:
            _json(load_profile(args.workspace))
    profile_parser.set_defaults(run=run_profile)

    item = sub.add_parser("item", help="管理事项")
    item_sub = item.add_subparsers(dest="item_command", required=True)
    item_add = item_sub.add_parser("add")
    item_add.add_argument("workspace"); item_add.add_argument("--title", required=True)
    item_add.add_argument("--type", default=""); item_add.add_argument("--state", default="planned")
    item_add.add_argument("--content", default=""); item_add.add_argument("--stdin", action="store_true")
    item_add.add_argument("--tags", default=""); item_add.add_argument("--links", default="")
    item_add.add_argument("--next-action", default=""); item_add.add_argument("--waiting-on", default="")
    item_add.add_argument("--review-at", default=""); item_add.add_argument("--blocked-by", default="")
    item_add.add_argument("--outcome", default=""); item_add.add_argument("--proposal-status", default="")
    item_add.add_argument("--discussed-at", default="")
    item_add.set_defaults(run=lambda args: _json(add_item(
        args.workspace, title=args.title, item_type=args.type or None, state=args.state,
        content=_content(args), tags=_csv(args.tags), links=_csv(args.links),
        next_action=args.next_action, waiting_on=args.waiting_on, review_at=args.review_at,
        blocked_by=args.blocked_by, outcome=args.outcome,
        proposal_status=args.proposal_status, discussed_at=args.discussed_at)))

    item_update = item_sub.add_parser("update")
    item_update.add_argument("workspace"); item_update.add_argument("item_id")
    for option in ("title", "type", "state", "next-action", "waiting-on", "review-at",
                   "blocked-by", "outcome", "proposal-status", "discussed-at",
                   "tags", "links", "content"):
        item_update.add_argument(f"--{option}", default=None)
    item_update.add_argument("--stdin", action="store_true")
    item_update.add_argument("--clear", action="append", default=[],
                             choices=["next_action", "waiting_on", "review_at", "blocked_by",
                                      "outcome", "proposal_status", "discussed_at"])
    def run_item_update(args: argparse.Namespace) -> None:
        changes = {name: getattr(args, name) for name in ("title", "type", "state", "next_action",
                                                          "waiting_on", "review_at", "blocked_by", "outcome",
                                                          "proposal_status", "discussed_at")
                   if getattr(args, name) is not None}
        if args.tags is not None:
            changes["tags"] = _csv(args.tags)
        if args.links is not None:
            changes["links"] = _csv(args.links)
        if args.stdin:
            changes["content"] = sys.stdin.read()
        elif args.content is not None:
            changes["content"] = args.content
        _json(update_item(args.workspace, args.item_id, changes, clear=args.clear))
    item_update.set_defaults(run=run_item_update)

    item_list = item_sub.add_parser("list")
    item_list.add_argument("workspace"); item_list.add_argument("--state", default="")
    def run_item_list(args: argparse.Namespace) -> None:
        root = require_workspace(args.workspace)
        entries = [metadata for metadata, _body, _path in load_items(root)
                   if not args.state or metadata["state"] == args.state]
        _json({"workspace": workspace_ref(root), "count": len(entries), "items": entries})
    item_list.set_defaults(run=run_item_list)

    item_get = item_sub.add_parser("get")
    item_get.add_argument("workspace"); item_get.add_argument("item_id")
    item_get.set_defaults(run=lambda args: print(_find_record(require_workspace(args.workspace), "item", args.item_id)[2].read_text(encoding="utf-8"), end=""))

    proposal = sub.add_parser("proposal", help="简洁记录提案")
    proposal_sub = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_add = proposal_sub.add_parser("add")
    proposal_add.add_argument("workspace"); proposal_add.add_argument("--title", required=True)
    proposal_add.add_argument("--status", default="pending_submission", choices=PROPOSAL_STATUSES)
    proposal_add.add_argument("--content", default=""); proposal_add.add_argument("--stdin", action="store_true")
    proposal_add.add_argument("--tags", default=""); proposal_add.add_argument("--links", default="")
    proposal_add.add_argument("--discussed-at", default=""); proposal_add.add_argument("--outcome", default="")
    proposal_add.set_defaults(run=lambda args: _json(add_proposal(
        args.workspace, title=args.title, proposal_status=args.status,
        content=_content(args), tags=_csv(args.tags), links=_csv(args.links),
        discussed_at=args.discussed_at, outcome=args.outcome)))

    proposal_update = proposal_sub.add_parser("update")
    proposal_update.add_argument("workspace"); proposal_update.add_argument("item_id")
    proposal_update.add_argument("--status", default=None, choices=PROPOSAL_STATUSES)
    proposal_update.add_argument("--title", default=None); proposal_update.add_argument("--content", default=None)
    proposal_update.add_argument("--stdin", action="store_true")
    proposal_update.add_argument("--tags", default=None); proposal_update.add_argument("--links", default=None)
    proposal_update.add_argument("--discussed-at", default=None); proposal_update.add_argument("--outcome", default=None)
    def run_proposal_update(args: argparse.Namespace) -> None:
        changes = {name: getattr(args, name) for name in ("title",) if getattr(args, name) is not None}
        if args.tags is not None:
            changes["tags"] = _csv(args.tags)
        if args.links is not None:
            changes["links"] = _csv(args.links)
        if args.stdin:
            changes["content"] = sys.stdin.read()
        elif args.content is not None:
            changes["content"] = args.content
        _json(update_proposal(args.workspace, args.item_id, changes,
                              proposal_status=args.status, outcome=args.outcome,
                              discussed_at=args.discussed_at))
    proposal_update.set_defaults(run=run_proposal_update)

    proposal_list = proposal_sub.add_parser("list")
    proposal_list.add_argument("workspace")
    proposal_list.add_argument("--status", default="", choices=["", *PROPOSAL_STATUSES])
    proposal_list.set_defaults(run=lambda args: _json(list_proposals(
        args.workspace, proposal_status=args.status)))

    memory = sub.add_parser("memory", help="管理长期记忆")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_sub.add_parser("add")
    memory_add.add_argument("workspace"); memory_add.add_argument("--title", required=True)
    memory_add.add_argument("--kind", required=True); memory_add.add_argument("--content", default="")
    memory_add.add_argument("--stdin", action="store_true"); memory_add.add_argument("--tags", default="")
    memory_add.add_argument("--applies-to", default=""); memory_add.add_argument("--evidence", default="")
    memory_add.add_argument("--valid-from", default=""); memory_add.add_argument("--valid-to", default="")
    memory_add.add_argument("--supersedes", default="")
    memory_add.set_defaults(run=lambda args: _json(add_memory(
        args.workspace, title=args.title, kind=args.kind, content=_content(args, required=True),
        tags=_csv(args.tags), applies_to=_csv(args.applies_to), evidence=_csv(args.evidence),
        valid_from=args.valid_from, valid_to=args.valid_to, supersedes=args.supersedes)))

    memory_expire = memory_sub.add_parser("expire")
    memory_expire.add_argument("workspace"); memory_expire.add_argument("memory_id")
    memory_expire.add_argument("--valid-to", default="")
    memory_expire.set_defaults(run=lambda args: _json(expire_memory(args.workspace, args.memory_id,
                                                                     valid_to=args.valid_to)))

    memory_list = memory_sub.add_parser("list")
    memory_list.add_argument("workspace"); memory_list.add_argument("--kind", default="")
    memory_list.add_argument("--state", default="")
    def run_memory_list(args: argparse.Namespace) -> None:
        root = require_workspace(args.workspace)
        entries = [metadata for metadata, _body, _path in load_memories(root)
                   if (not args.kind or metadata["kind"] == args.kind)
                   and (not args.state or metadata["state"] == args.state)]
        _json({"workspace": workspace_ref(root), "count": len(entries), "memories": entries})
    memory_list.set_defaults(run=run_memory_list)

    memory_get = memory_sub.add_parser("get")
    memory_get.add_argument("workspace"); memory_get.add_argument("memory_id")
    memory_get.set_defaults(run=lambda args: print(_find_record(require_workspace(args.workspace), "memory", args.memory_id)[2].read_text(encoding="utf-8"), end=""))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.run(args)
    except WorkspaceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())

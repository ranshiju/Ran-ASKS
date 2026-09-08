#!/usr/bin/env python3
"""Build a project-wide prompt map and an execution-mode-aware review brief.

The program locates prompt surfaces. The current Agent, or a controlled API
worker, reviews those locations unless the explicitly selected trusted
Agent-build profile requests deterministic structure checks only.
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
ENGINEERING_GRAPH = REPO / "operations/engineering/graph.yaml"
CODE_ROOTS = (REPO / ".scripts", REPO / "dsh", REPO / ".codex/skills", REPO / "projects")
CACHE_SCHEMA = "prompt-map-cache-v1"
DEFAULT_CACHE_PATH = REPO / "temp/prompt-audit/map-cache-v1.json"
EXCLUDED_PARTS = {
    ".git", ".research-memory", "__pycache__", "node_modules",
    "temp", "archive", "outputs", "raw", "wiki", "state",
}
PROMPT_NAME = re.compile(r"prompt|instruction|messages|system", re.IGNORECASE)
INSTRUCTION_CUE = re.compile(
    r"必须|不得|只(?:能|需|负责|输出|使用|保留)|请|负责|执行|调用|读取|写入|"
    r"must|should|only|return|output|use|read|write",
    re.IGNORECASE,
)

REVIEW_CRITERIA = [
    {
        "id": "placement",
        "title": "出现位置",
        "question": "提示词是否位于合适的全局、任务、能力、模式或执行单元层级？",
    },
    {
        "id": "necessity",
        "title": "必要性",
        "question": "该位置是否需要提示词介入；可由程序、契约、Schema、校验器或上层共享提示稳定承担的要求，是否已交给相应机制？",
    },
    {
        "id": "consistency",
        "title": "矛盾或重复",
        "question": "不同位置的提示词是否相互矛盾，或在同一运行上下文中重复表达同一要求？",
    },
    {
        "id": "currency",
        "title": "版本一致性",
        "question": "提示词引用的命令、参数、路径、角色、流程和责任边界，是否仍符合当前上位 capability/task/script contract 及实际 CLI、Schema/validator 与实现？是否残留已退役约定？",
    },
    {
        "id": "conciseness",
        "title": "表述冗余",
        "question": "每句话是否影响判断、行动或验收；是否可简化解释性重复，以及目标 Agent 已稳定具备且不影响本项目判断与执行的通用常识？",
    },
    {
        "id": "ambiguity",
        "title": "表述歧义",
        "question": "控制主体、适用范围、触发条件、行动要求和验收含义是否清楚？",
    },
    {
        "id": "negation",
        "title": "否定式必要性",
        "question": "否定式是否用于红线、风险边界或已知易错点，其他要求是否更适合正向表达？",
    },
]

LAYER_PRIORITY = {
    "global-instruction": 0,
    "shared-instruction": 1,
    "memory-instruction": 2,
    "capability-guardrail": 3,
    "task-instruction": 4,
    "domain-contract": 5,
    "scoped-instruction": 6,
    "skill-instruction": 7,
    "build-instruction": 8,
    "build-contract": 9,
    "prompt-resource": 10,
    "runtime-prompt": 11,
}

ARTIFACT_DESCRIPTIONS = {
    "instruction": "面向 Agent 或受控执行单元的行为指令。",
    "memory": "按任务触发、进入 LLM 上下文的偏好、playbook 或经验。",
    "contract": "独立约束源；该位置因明确供 LLM 读取而构成提示面。",
    "prompt-resource": "Agent 按需读取的模板或检查清单。",
    "runtime-prompt": "程序在运行时构造或传给模型的提示。",
}

FULL_REVIEW_PROFILE = "full-semantic-review"
TRUSTED_AGENT_BUILD_PROFILE = "trusted-agent-build"
RUNTIME_PROMPT_MODES = {"agent", "api", "shared"}

_ENGINEERING_GRAPH_CACHE: dict | None = None
_SOURCE_FILE_INDEX: list[Path] | None = None


def _relative(path: Path) -> str:
    return path.resolve().relative_to(REPO.resolve()).as_posix()


def _excluded(path: Path) -> bool:
    rel_parts = path.resolve().relative_to(REPO.resolve()).parts
    return any(part in EXCLUDED_PARTS or part.startswith(".venv") for part in rel_parts)


def _location(*, location_id: str, path: str, layer: str, selector: str,
              mode: str = "shared", symbol: str = "",
              start_line: int = 0, end_line: int = 0,
              artifact_type: str = "instruction") -> dict:
    item = {
        "id": location_id,
        "path": path,
        "layer": layer,
        "mode": mode,
        "selector": selector,
        "artifact_type": artifact_type,
        "llm_visible": True,
    }
    if symbol:
        item["symbol"] = symbol
    if start_line:
        item["start_line"] = start_line
        item["end_line"] = end_line or start_line
    return item


def discover_instruction_documents() -> list[dict]:
    """Find graph-registered specs plus Agent, memory, and skill instructions."""
    data = _load_engineering_graph()
    prompt_kinds = {
        "constitution", "spec", "spec-family", "skill",
        "private-profile", "private-reference",
    }
    paths = {
        REPO / node["path"]
        for node in data["nodes"].values()
        if node.get("kind") in prompt_kinds
        and "<domain>" not in str(node.get("path") or "")
        and str(node.get("path") or "").endswith(".md")
    }
    capability_members = {
        node_id
        for capability in data["capabilities"].values()
        for node_id in (
            list(capability.get("required") or ())
            + list(capability.get("optional") or ())
            + ([capability["entry"]] if capability.get("entry") else [])
        )
    }
    paths.update(
        REPO / data["nodes"][node_id]["path"]
        for node_id in capability_members
        if node_id in data["nodes"]
        and str(data["nodes"][node_id].get("path") or "").endswith(".md")
        and "<domain>" not in str(data["nodes"][node_id].get("path") or "")
    )
    operations = REPO / "operations"
    if operations.exists():
        paths.update(operations.glob("*.md"))
        templates = operations / "templates"
        if templates.exists():
            paths.update(_walk_files((templates,), suffixes={".md"}))
    paths.update(path for path in _repository_source_files() if path.name == "AGENTS.md")
    paths.update(REPO.glob("*/SCHEMA.md"))
    agents = REPO / "agents"
    if agents.exists():
        paths.update(_walk_files((agents,), suffixes={".md"}))
    memory = REPO / "memory"
    if memory.exists():
        paths.update(path for path in (
            memory / "MEMORY.md",
            memory / "playbooks" / "index.md",
        ) if path.is_file())
        experiences = memory / "experiences"
        if experiences.exists():
            paths.update(_walk_files((experiences,), suffixes={".md"}))
    skills = REPO / ".codex/skills"
    if skills.exists():
        paths.update(_walk_files((skills,), names={"SKILL.md"}))

    locations = []
    for path in sorted(paths):
        if not path.is_file() or _excluded(path):
            continue
        rel = _relative(path)
        if rel == "AGENTS.md":
            layer, mode, artifact_type = "global-instruction", "agent", "instruction"
        elif rel == "operations/shared-conventions.md":
            layer, mode, artifact_type = "shared-instruction", "shared", "instruction"
        elif rel == "memory/MEMORY.md":
            layer, mode, artifact_type = "memory-instruction", "agent", "memory"
        elif rel.startswith("memory/"):
            layer, mode, artifact_type = "task-instruction", "agent", "memory"
        elif path.name == "AGENTS.md":
            layer, mode, artifact_type = "scoped-instruction", "agent", "instruction"
        elif rel == "agents/writer/AGENT.md":
            layer, mode, artifact_type = "scoped-instruction", "agent", "instruction"
        elif rel.startswith("agents/"):
            layer, mode, artifact_type = "prompt-resource", "agent", "prompt-resource"
        elif rel == "operations/RECOVERY.md" or rel.startswith("operations/templates/"):
            layer, mode, artifact_type = "prompt-resource", "agent", "prompt-resource"
        elif path.name == "SKILL.md":
            layer, mode, artifact_type = "skill-instruction", "agent", "instruction"
        elif path.name == "SCHEMA.md":
            layer, mode, artifact_type = "domain-contract", "shared", "contract"
        elif rel.startswith("operations/engineering/"):
            layer, mode, artifact_type = "build-instruction", "agent", "instruction"
        else:
            layer, mode, artifact_type = "task-instruction", "shared", "instruction"
        locations.append(_location(
            location_id=f"doc:{rel}", path=rel, layer=layer, mode=mode,
            selector="document", artifact_type=artifact_type,
        ))
    return locations


def _load_engineering_graph() -> dict:
    global _ENGINEERING_GRAPH_CACHE
    if _ENGINEERING_GRAPH_CACHE is not None:
        return _ENGINEERING_GRAPH_CACHE
    module_path = REPO / ".scripts/engineering_graph.py"
    spec = importlib.util.spec_from_file_location("prompt_audit_engineering_graph", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 engineering_graph.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    nodes, edges, capabilities, _, _, script_contracts, _ = module.load()
    _ENGINEERING_GRAPH_CACHE = {
        "nodes": nodes,
        "edges": edges,
        "capabilities": capabilities,
        "script_contracts": script_contracts,
    }
    return _ENGINEERING_GRAPH_CACHE


def discover_engineering_prompt_locations() -> list[dict]:
    """Expose graph-backed instructions that are injected or read on demand."""
    data = _load_engineering_graph()
    locations = []
    for name, capability in sorted(data["capabilities"].items()):
        if not capability.get("guardrails"):
            continue
        selector = f"yaml:/capabilities/{name}/guardrails"
        locations.append(_location(
            location_id=f"graph:capability:{name}",
            path=_relative(ENGINEERING_GRAPH),
            layer="capability-guardrail", mode="shared", selector=selector,
        ))
    for name, contract in sorted(data["script_contracts"].items()):
        if not contract:
            continue
        selector = f"yaml:/script_contracts/{name}"
        locations.append(_location(
            location_id=f"graph:contract:{name}",
            path=_relative(ENGINEERING_GRAPH),
            layer="build-contract", mode="agent", selector=selector,
            artifact_type="contract",
        ))
    return locations


def _literal_text(node: ast.AST) -> str:
    return "".join(
        child.value for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _function_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return "".join(_literal_text(item) for item in body)


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    names = []
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.append(child.id)
            elif isinstance(child, ast.Attribute):
                names.append(child.attr)
    return names


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _runtime_prompt_mode(rel: str, symbol: str, kind: str) -> str:
    """Classify a concrete runtime prompt surface by its execution site."""
    normalized = symbol.lower()
    if kind == "tool-definition":
        return "api"
    if "build_agent_" in normalized or normalized.startswith("agent_"):
        return "agent"
    if "build_api_" in normalized or normalized.startswith("api_") or "_api_prompt" in normalized:
        return "api"
    if kind == "call-argument" and (
        normalized.startswith(("call_json:", "call_text:"))
        or normalized.endswith(":system")
    ):
        return "api"

    data = _load_engineering_graph()
    roles = {
        str(data["nodes"][node_id].get("role") or "").lower()
        for node_id in _matching_graph_nodes(rel, data)
    }
    if any(role.startswith(("api-only-", "api-backend-")) for role in roles):
        return "api"
    if any(role.startswith("agent-only-") for role in roles):
        return "agent"
    if rel.startswith("dsh/"):
        return "api"
    return "shared"


class PromptNodeVisitor(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.candidates: list[dict] = []
        self.depth = 0
        self.referenced_names: set[str] = set()
        self.referenced_functions: set[str] = set()

    def _add(self, node: ast.AST, kind: str, symbol: str = "") -> None:
        start = int(getattr(node, "lineno", 0) or 0)
        end = int(getattr(node, "end_lineno", start) or start)
        mode = _runtime_prompt_mode(self.rel, symbol, kind)
        self.candidates.append(_location(
            location_id=f"code:{self.rel}:{start}:{kind}", path=self.rel,
            layer="runtime-prompt", mode=mode, selector=f"L{start}-L{end}",
            symbol=symbol, start_line=start, end_line=end,
            artifact_type="runtime-prompt",
        ))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        text = _function_text(node)
        if PROMPT_NAME.search(node.name) and len(text.strip()) >= 20:
            self._add(node, "builder", node.name)
            self.referenced_names.update(
                child.id for child in ast.walk(node)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            )
            self.referenced_functions.update(
                _call_name(child) for child in ast.walk(node)
                if isinstance(child, ast.Call) and _call_name(child)
            )

        positional = list(node.args.posonlyargs) + list(node.args.args)
        defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
        for arg, default in zip(positional, defaults):
            if default is not None and PROMPT_NAME.search(arg.arg):
                if len(_literal_text(default).strip()) >= 20:
                    self._add(default, "default", f"{node.name}:{arg.arg}")
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if default is not None and PROMPT_NAME.search(arg.arg):
                if len(_literal_text(default).strip()) >= 20:
                    self._add(default, "default", f"{node.name}:{arg.arg}")

        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._visit_assignment(node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_assignment(node)
        self.generic_visit(node)

    def _visit_assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        value = node.value
        if value is None:
            return
        text = _literal_text(value).strip()
        if len(text) < 20:
            return
        names = _target_names(node)
        named_prompt = any(
            PROMPT_NAME.search(name) and not name.upper().endswith("_VERSION")
            for name in names
        )
        module_instruction = (
            self.rel == ".scripts/route.py"
            and self.depth == 0
            and len(text) >= 80
            and INSTRUCTION_CUE.search(text)
        )
        embedded_prompt = re.search(
            r"(?m)^\s*(?:default_prompt|system_prompt|user_prompt|prompt|instructions?)\s*:",
            text,
        )
        if named_prompt or module_instruction or embedded_prompt:
            kind = "embedded" if embedded_prompt and not (named_prompt or module_instruction) else "assignment"
            self._add(node, kind, ",".join(names))
            self.referenced_names.update(
                child.id for child in ast.walk(value)
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
            )

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node)
        if call_name == "ToolDefinition":
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            prompt_text = "".join(
                _literal_text(keywords[name])
                for name in ("description", "input_schema")
                if name in keywords
            )
            if prompt_text.strip():
                tool_name = _literal_text(keywords.get("name", ast.Constant(value=""))).strip()
                self._add(node, "tool-definition", f"ToolDefinition:{tool_name or '?'}")
        for keyword in node.keywords:
            if keyword.arg and PROMPT_NAME.search(keyword.arg):
                if len(_literal_text(keyword.value).strip()) >= 20:
                    self._add(keyword.value, "call-argument", f"{call_name}:{keyword.arg}")
        if call_name in {"call_json", "call_text"} and node.args:
            if len(_literal_text(node.args[0]).strip()) >= 20:
                self._add(node.args[0], "call-argument", call_name)
        self.generic_visit(node)


def _walk_files(roots: tuple[Path, ...], *, suffixes: set[str] | None = None,
                names: set[str] | None = None) -> list[Path]:
    paths = []
    for root in roots:
        if not root.exists():
            continue
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in EXCLUDED_PARTS and not name.startswith(".venv")
            )
            paths.extend(
                Path(directory) / name
                for name in filenames
                if (suffixes is not None and Path(name).suffix.lower() in suffixes)
                or (names is not None and name in names)
            )
    return sorted(set(paths))


def _repository_source_files() -> list[Path]:
    global _SOURCE_FILE_INDEX
    if _SOURCE_FILE_INDEX is not None:
        return _SOURCE_FILE_INDEX
    paths = _walk_files((REPO,), suffixes={".py", ".yaml", ".yml"}, names={"AGENTS.md"})
    _SOURCE_FILE_INDEX = sorted(paths)
    return _SOURCE_FILE_INDEX


def _requested_files(wanted: set[str], suffixes: set[str]) -> list[Path]:
    paths = []
    for rel in sorted(wanted):
        path = REPO / rel
        if path.suffix.lower() in suffixes and path.is_file() and not _excluded(path):
            paths.append(path)
    return paths


def _python_files(wanted: set[str] | None = None) -> list[Path]:
    paths = (
        _requested_files(wanted, {".py"})
        if wanted is not None
        else [path for path in _repository_source_files() if path.suffix.lower() == ".py"]
    )
    roots = tuple(root.resolve() for root in CODE_ROOTS)
    return [
        path for path in paths
        if any(path.resolve().is_relative_to(root) for root in roots)
        and not path.name.startswith("test_")
        and not path.stem.endswith("_test")
        and path.resolve() != Path(__file__).resolve()
    ]


def _file_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
    }


def _display_path(path: Path) -> str:
    try:
        return _relative(path)
    except ValueError:
        return str(path.resolve())


class PromptMapCache:
    def __init__(self, path: Path, *, enabled: bool = True, refresh: bool = False):
        self.path = path
        self.enabled = enabled
        self.refresh = refresh
        self.scanner_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.files: dict[str, dict] = {}
        self.dirty = False
        self.stats = {
            "enabled": enabled,
            "path": _display_path(path),
            "hits": 0,
            "hash_hits": 0,
            "misses": 0,
            "parsed": 0,
            "removed": 0,
            "invalidated": False,
            "entries": 0,
            "written": False,
        }
        if enabled:
            self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            self.stats["invalidated"] = True
            self.dirty = True
            return
        if (
            payload.get("schema") != CACHE_SCHEMA
            or payload.get("scanner_sha256") != self.scanner_sha256
            or not isinstance(payload.get("files"), dict)
        ):
            self.stats["invalidated"] = True
            self.dirty = True
            return
        self.files = payload["files"]

    def scan(self, path: Path, kind: str, parser) -> list[dict]:
        rel = _relative(path)
        try:
            signature = _file_signature(path)
        except OSError as exc:
            raise RuntimeError(f"无法读取提示词候选 {rel}: {exc}") from exc
        entry = self.files.get(rel)
        reusable = (
            self.enabled
            and not self.refresh
            and isinstance(entry, dict)
            and entry.get("kind") == kind
            and isinstance(entry.get("locations"), list)
        )
        if reusable and entry.get("signature") == signature:
            self.stats["hits"] += 1
            return [dict(item) for item in entry["locations"]]

        if self.enabled:
            self.stats["misses"] += 1
        try:
            source_bytes = path.read_bytes()
            source = source_bytes.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"无法读取提示词候选 {rel}: {exc}") from exc
        digest = hashlib.sha256(source_bytes).hexdigest()
        if reusable and entry.get("sha256") == digest:
            self.stats["hash_hits"] += 1
            entry["signature"] = _file_signature(path)
            self.dirty = True
            return [dict(item) for item in entry["locations"]]

        locations = parser(source, rel)
        self.stats["parsed"] += 1
        if self.enabled:
            self.files[rel] = {
                "kind": kind,
                "signature": _file_signature(path),
                "sha256": digest,
                "locations": [dict(item) for item in locations],
            }
            self.dirty = True
        return locations

    def prune(self, active_paths: set[str], wanted: set[str] | None) -> None:
        if not self.enabled:
            return
        if wanted is None:
            stale = set(self.files) - active_paths
        else:
            stale = {rel for rel in self.files if rel in wanted and rel not in active_paths}
        for rel in stale:
            del self.files[rel]
        self.stats["removed"] += len(stale)
        self.dirty = self.dirty or bool(stale)

    def save(self) -> dict:
        self.stats["entries"] = len(self.files)
        if not self.enabled or not self.dirty:
            return dict(self.stats)
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                json.dump({
                    "schema": CACHE_SCHEMA,
                    "scanner_sha256": self.scanner_sha256,
                    "files": dict(sorted(self.files.items())),
                }, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                temporary = Path(stream.name)
            os.replace(temporary, self.path)
            self.stats["written"] = True
        except OSError as exc:
            self.stats["write_error"] = str(exc)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return dict(self.stats)


def _scan_python_source(source: str, rel: str) -> list[dict]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as exc:
        raise RuntimeError(f"无法解析提示词候选 {rel}: {exc}") from exc
    visitor = PromptNodeVisitor(rel)
    visitor.visit(tree)
    module_assignments = {}
    module_functions = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _target_names(node):
                module_assignments[name] = node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_functions[node.name] = node
    for name in sorted(visitor.referenced_names):
        node = module_assignments.get(name)
        if node is None or len(_literal_text(node).strip()) < 20:
            continue
        visitor._add(node, "dependency", name)
    for name in sorted(visitor.referenced_functions):
        node = module_functions.get(name)
        text = _function_text(node) if node is not None else ""
        if len(text.strip()) < 20 or not INSTRUCTION_CUE.search(text):
            continue
        visitor._add(node, "dependency", name)
    builders = [item for item in visitor.candidates if item["id"].endswith(":builder")]
    return [
        item for item in visitor.candidates
        if not any(
            item is not builder
            and builder["start_line"] <= item["start_line"]
            and builder["end_line"] >= item["end_line"]
            for builder in builders
        )
    ]


def discover_runtime_prompts(cache: PromptMapCache,
                             wanted: set[str] | None = None) -> tuple[list[dict], set[str]]:
    paths = _python_files(wanted)
    locations = [
        item
        for path in paths
        for item in cache.scan(path, "python", _scan_python_source)
    ]
    return locations, {_relative(path) for path in paths}


YAML_PROMPT_KEYS = {
    "default_prompt", "system_prompt", "user_prompt", "prompt",
    "instruction", "instructions",
}


def _yaml_pointer(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _collect_yaml_prompt_nodes(node: yaml.Node | None, rel: str,
                               locations: list[dict],
                               parts: tuple[str, ...] = ()) -> None:
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = str(key_node.value)
            child_parts = (*parts, key)
            if key.lower() in YAML_PROMPT_KEYS:
                start = key_node.start_mark.line + 1
                end = max(start, value_node.end_mark.line + 1)
                pointer = _yaml_pointer(child_parts)
                mode = "agent" if rel.startswith(".codex/skills/") else "api"
                locations.append(_location(
                    location_id=f"yaml:{rel}:{pointer}", path=rel,
                    layer="runtime-prompt", mode=mode,
                    selector=f"yaml:{pointer}", symbol=key,
                    start_line=start, end_line=end,
                    artifact_type="runtime-prompt",
                ))
            _collect_yaml_prompt_nodes(value_node, rel, locations, child_parts)
    elif isinstance(node, yaml.SequenceNode):
        for index, child in enumerate(node.value):
            _collect_yaml_prompt_nodes(child, rel, locations, (*parts, str(index)))


def _yaml_files(wanted: set[str] | None = None) -> list[Path]:
    return (
        _requested_files(wanted, {".yaml", ".yml"})
        if wanted is not None
        else [
            path for path in _repository_source_files()
            if path.suffix.lower() in {".yaml", ".yml"}
        ]
    )


def _scan_yaml_source(source: str, rel: str) -> list[dict]:
    locations = []
    key_re = re.compile(r"^\s*(default_prompt|system_prompt|user_prompt|prompt|instructions?)\s*:", re.M)
    if not key_re.search(source):
        return locations
    try:
        documents = list(yaml.compose_all(source))
    except yaml.YAMLError as exc:
        raise RuntimeError(f"无法解析 YAML 提示词候选 {rel}: {exc}") from exc
    for document in documents:
        _collect_yaml_prompt_nodes(document, rel, locations)
    return locations


def discover_yaml_prompts(cache: PromptMapCache,
                          wanted: set[str] | None = None) -> tuple[list[dict], set[str]]:
    paths = _yaml_files(wanted)
    locations = [
        item
        for path in paths
        for item in cache.scan(path, "yaml", _scan_yaml_source)
    ]
    return locations, {_relative(path) for path in paths}


GLOBAL_PROMPT_ID = "doc:AGENTS.md"


def _base_order(item: dict) -> tuple:
    return (
        LAYER_PRIORITY.get(item["layer"], 99),
        item["path"], item.get("start_line", 0), item["id"],
    )


def _matching_graph_nodes(rel: str, data: dict) -> set[str]:
    matches = set()
    for node_id, node in data["nodes"].items():
        template = str(node.get("path") or "")
        if not template:
            continue
        pattern = re.escape(template)
        pattern = re.sub(r"<[^>]+>", r"[^/]+", pattern)
        if re.fullmatch(pattern, rel):
            matches.add(node_id)
    return matches


def _capability_governors(item: dict, data: dict, known_ids: set[str]) -> list[str]:
    node_ids = _matching_graph_nodes(item["path"], data)
    if item["id"].startswith("graph:contract:"):
        node_ids.add(item["id"].removeprefix("graph:contract:"))
    parents = []
    for name, capability in sorted(data["capabilities"].items()):
        prompt_id = f"graph:capability:{name}"
        if prompt_id not in known_ids:
            continue
        members = set(capability.get("required") or ()) | set(capability.get("optional") or ())
        entry = capability.get("entry")
        if entry:
            members.add(entry)
        if node_ids & members:
            parents.append(prompt_id)
    return parents


def _script_contract_governors(item: dict, data: dict, known_ids: set[str]) -> list[str]:
    if item["artifact_type"] != "runtime-prompt":
        return []
    return sorted(
        contract_id
        for node_id in _matching_graph_nodes(item["path"], data)
        if node_id in data["script_contracts"]
        if (contract_id := f"graph:contract:{node_id}") in known_ids
    )


def _nearest_instruction_parent(rel: str, known_ids: set[str]) -> str:
    path = Path(rel)
    for directory in (path.parent, *path.parents):
        for filename in ("SKILL.md", "AGENT.md", "AGENTS.md"):
            candidate = (directory / filename).as_posix()
            prompt_id = f"doc:{candidate}"
            if candidate != rel and prompt_id in known_ids:
                return prompt_id
    return GLOBAL_PROMPT_ID if GLOBAL_PROMPT_ID in known_ids else ""


def _direct_governors(item: dict, data: dict, known_ids: set[str]) -> list[str]:
    item_id = item["id"]
    rel = item["path"]
    if item_id == GLOBAL_PROMPT_ID:
        return []
    if item_id.startswith("graph:capability:"):
        return [GLOBAL_PROMPT_ID]
    explicit = {
        "doc:operations/shared-conventions.md": [GLOBAL_PROMPT_ID],
        "doc:operations/RECOVERY.md": ["doc:operations/QUERY.md"],
        "doc:operations/templates/admin-templates.md": [
            "doc:operations/INGEST.md", "doc:admin/SCHEMA.md",
        ],
        "doc:operations/templates/paper-summary.md": [
            "doc:operations/INGEST.md", "doc:academic/SCHEMA.md",
        ],
        "doc:memory/MEMORY.md": [GLOBAL_PROMPT_ID],
        "doc:memory/playbooks/index.md": [GLOBAL_PROMPT_ID],
        "doc:agents/writer/AGENT.md": ["doc:operations/WRITE.md"],
    }
    if item_id in explicit:
        return [parent for parent in explicit[item_id] if parent in known_ids]
    if rel.startswith("agents/writer/"):
        parent = "doc:agents/writer/AGENT.md"
        if parent in known_ids:
            return [parent]
    if rel.startswith("operations/templates/"):
        parent = "doc:operations/INGEST.md"
        if parent in known_ids:
            return [parent]
    if rel.startswith("memory/experiences/"):
        parent = "doc:operations/EXPERIENCES.md"
        if parent in known_ids:
            return [parent]

    contract_parents = _script_contract_governors(item, data, known_ids)
    if contract_parents:
        return contract_parents
    capability_parents = _capability_governors(item, data, known_ids)
    if capability_parents:
        return capability_parents
    parent = _nearest_instruction_parent(rel, known_ids)
    return [parent] if parent else []


def _attach_governors_and_sort(locations: list[dict]) -> list[dict]:
    data = _load_engineering_graph()
    by_id = {item["id"]: item for item in locations}
    if len(by_id) != len(locations):
        raise RuntimeError("提示词位置 ID 不唯一")
    known_ids = set(by_id)
    for item in locations:
        item["governed_by"] = _direct_governors(item, data, known_ids)
        missing = set(item["governed_by"]) - known_ids
        if missing:
            raise RuntimeError(f"提示词上位关系悬空 {item['id']}: {sorted(missing)}")

    children = {item_id: [] for item_id in known_ids}
    indegree = {item_id: 0 for item_id in known_ids}
    for item in locations:
        for parent in item["governed_by"]:
            children[parent].append(item["id"])
            indegree[item["id"]] += 1
    ready = sorted(
        (by_id[item_id] for item_id, degree in indegree.items() if degree == 0),
        key=_base_order,
    )
    ordered = []
    while ready:
        item = ready.pop(0)
        ordered.append(item)
        for child_id in children[item["id"]]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(by_id[child_id])
        ready.sort(key=_base_order)
    if len(ordered) != len(locations):
        cycle_ids = sorted(item_id for item_id, degree in indegree.items() if degree)
        raise RuntimeError(f"提示词上位关系存在环: {cycle_ids}")
    return ordered


def _normalize_requested_paths(paths: list[str] | None) -> set[str] | None:
    if not paths:
        return None
    wanted = set()
    for value in paths:
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (REPO / candidate).resolve()
        try:
            wanted.add(resolved.relative_to(REPO.resolve()).as_posix())
        except ValueError as exc:
            raise RuntimeError(f"审计路径必须位于仓库内: {value}") from exc
    return wanted


def collect_prompt_map(paths: list[str] | None = None, *, cache_path: Path = DEFAULT_CACHE_PATH,
                       cache_enabled: bool = True,
                       refresh_cache: bool = False) -> tuple[list[dict], dict]:
    wanted = _normalize_requested_paths(paths)
    cache = PromptMapCache(cache_path, enabled=cache_enabled, refresh=refresh_cache)
    runtime_locations, runtime_paths = discover_runtime_prompts(cache, wanted)
    yaml_locations, yaml_paths = discover_yaml_prompts(cache, wanted)
    locations = (
        discover_instruction_documents()
        + discover_engineering_prompt_locations()
        + runtime_locations
        + yaml_locations
    )
    priority = {
        "tool-definition": 0, "builder": 1, "assignment": 2, "embedded": 3,
        "default": 4, "call-argument": 5, "dependency": 6,
    }
    unique = {}
    for item in locations:
        key = (
            item["path"], item.get("start_line", 0), item.get("end_line", 0),
            item.get("symbol", ""), item["selector"],
        )
        current = unique.get(key)
        kind = item["id"].rsplit(":", 1)[-1]
        current_kind = current["id"].rsplit(":", 1)[-1] if current else ""
        if current is None or priority.get(kind, 9) < priority.get(current_kind, 9):
            unique[key] = item
    selected = _attach_governors_and_sort(list(unique.values()))
    if wanted is not None:
        by_id = {item["id"]: item for item in selected}
        included = {
            item["id"] for item in selected if item["path"] in wanted
        }
        pending = list(included)
        while pending:
            item = by_id[pending.pop()]
            for parent in item["governed_by"]:
                if parent not in included:
                    included.add(parent)
                    pending.append(parent)
        selected = [item for item in selected if item["id"] in included]
    cache.prune(runtime_paths | yaml_paths, wanted)
    return selected, cache.save()


def collect_prompt_locations(paths: list[str] | None = None) -> list[dict]:
    locations, _ = collect_prompt_map(paths)
    return locations


def _review_profile(task: str, backend: str) -> str:
    if task == "build" and backend == "agent":
        return TRUSTED_AGENT_BUILD_PROFILE
    return FULL_REVIEW_PROFILE


def _structural_checks(locations: list[dict]) -> list[dict]:
    """Recompute structural facts from the emitted map; do not infer coverage."""
    repo_root = REPO.resolve()
    path_errors = []
    for item in locations:
        rel = item.get("path")
        if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
            path_errors.append(item.get("id", "<missing-id>"))
            continue
        resolved = (REPO / rel).resolve()
        if not resolved.is_relative_to(repo_root) or _excluded(resolved):
            path_errors.append(item.get("id", "<missing-id>"))

    known_ids = {
        item.get("id") for item in locations
        if isinstance(item.get("id"), str) and item.get("id")
    }
    missing_governors = sorted({
        parent
        for item in locations
        for parent in item.get("governed_by", [])
        if parent not in known_ids
    })
    positions = {item.get("id"): index for index, item in enumerate(locations)}
    order_errors = [
        f"{parent}->{item.get('id', '<missing-id>')}"
        for item in locations
        for parent in item.get("governed_by", [])
        if parent in positions and positions[parent] >= positions.get(item.get("id"), -1)
    ]
    field_errors = [
        item.get("id", "<missing-id>")
        for item in locations
        if not all(item.get(field) for field in (
            "id", "path", "layer", "mode", "selector", "artifact_type",
        ))
        or item.get("mode") not in RUNTIME_PROMPT_MODES
        or item.get("artifact_type") not in ARTIFACT_DESCRIPTIONS
        or item.get("llm_visible") is not True
    ]
    runtime_count = sum(
        item.get("artifact_type") == "runtime-prompt" for item in locations
    )

    return [
        {
            "id": "repository_path_scope",
            "status": "failed" if path_errors else "passed",
            "detail": (
                f"发现 {len(path_errors)} 个越界、绝对或排除目录位置: {path_errors[:5]}"
                if path_errors else f"已验证 {len(locations)} 个位置均为仓库内允许路径"
            ),
        },
        {
            "id": "governor_references_resolved",
            "status": "failed" if missing_governors else "passed",
            "detail": (
                f"发现悬空 governed_by: {missing_governors[:5]}"
                if missing_governors else "所有 governed_by 均指向本次位置地图中的已知上位位置"
            ),
        },
        {
            "id": "governor_graph_acyclic",
            "status": "failed" if order_errors else "passed",
            "detail": (
                f"发现 {len(order_errors)} 个拓扑顺序错误: {order_errors[:5]}"
                if order_errors else "所有 governed_by 均先于下位位置，拓扑顺序有效"
            ),
        },
        {
            "id": "location_schema_and_mode",
            "status": "failed" if field_errors else "passed",
            "detail": (
                f"发现 {len(field_errors)} 个字段或 mode 分类错误: {field_errors[:5]}"
                if field_errors else f"已验证 {len(locations)} 个位置的必填字段、类型与 mode 枚举"
            ),
        },
        {
            "id": "llm_visible_surface_only",
            "status": "unknown",
            "detail": (
                f"已分类 {runtime_count} 个运行时提示位置；llm_visible 是扫描分类结果，"
                "不能独立证明所有模型可见提示面均已覆盖或没有误收，完整性留待语义审查"
            ),
        },
    ]


def build_review_task(locations: list[dict], cache_stats: dict | None = None,
                      *, task: str = "", backend: str = "") -> dict:
    layers = Counter(item["layer"] for item in locations)
    artifacts = Counter(item["artifact_type"] for item in locations)
    relations = [
        {"upper": parent, "lower": item["id"], "type": "governs"}
        for item in locations for parent in item["governed_by"]
    ]
    summary = {
        "locations": len(locations),
        "files": len({item["path"] for item in locations}),
        "relations": len(relations),
        "layers": dict(sorted(layers.items())),
        "artifacts": dict(sorted(artifacts.items())),
    }
    if cache_stats is not None:
        summary["cache"] = cache_stats
    profile = _review_profile(task, backend)
    trusted_agent_build = profile == TRUSTED_AGENT_BUILD_PROFILE
    return {
        "schema": "prompt-audit-task-v4",
        "status": "structural_only" if trusted_agent_build else "prepared",
        "scope": (
            "trusted Agent-build prompt structure"
            if trusted_agent_build
            else "project LLM-visible prompt surfaces and governing sources"
        ),
        "execution_context": {
            "task": task or None,
            "backend": backend or None,
        },
        "review_profile": profile,
        "semantic_review": {
            "status": "skipped" if trusted_agent_build else "required",
            "reason": (
                "explicit task=build and backend=agent trusts the host Agent's "
                "semantic judgment; no audit prompt is generated"
                if trusted_agent_build
                else "full prompt semantic review remains enabled"
            ),
        },
        "structural_checks": _structural_checks(locations),
        "criteria": [] if trusted_agent_build else REVIEW_CRITERIA,
        "version_basis": {
            "governing_sources": "当次位置地图中的 governed_by capability、task 与 LLM 可见 script contract",
            "implementation_evidence": "当前 CLI 参数、Schema/validator、组件责任边界与实际代码路径",
            "freshness_limit": "文件签名、mtime、缓存命中与 SHA-256 只证明扫描内容新鲜，不证明提示语义符合当前版本",
        },
        "artifact_types": ARTIFACT_DESCRIPTIONS,
        "locations": locations,
        "relations": relations,
        "summary": summary,
        "review_rules": [] if trusted_agent_build else [
            "沿 governed_by 关系按总到分审查：先确立上位共享约束，再审查任务、能力、领域与局部提示，最后审查运行时执行单元。",
            "契约是独立约束源；仅审查地图中明确标记为 LLM 可见的契约提示面，不把机器 Schema、类型、校验器或测试视为提示词。",
            "逐项覆盖位置地图，只报告会影响理解或执行的实质问题。",
            "跨位置的矛盾或重复同时引用相关位置，并结合实际运行上下文判断。",
            "版本一致性须核对 version_basis：以上位规范确定目标，以当前接口、校验器和责任边界验证可执行性；不得只凭文件日期或哈希判定未过时。",
            "保留不同独立运行上下文为自包含所需的必要重复。",
            "每项发现使用：位置｜问题类型｜判断依据｜最小修改建议。",
            "没有实质问题时明确报告审计通过。",
        ],
    }


def render_text(task: dict) -> str:
    summary = task["summary"]
    structural_only = task["review_profile"] == TRUSTED_AGENT_BUILD_PROFILE
    lines = [
        "# 项目提示词结构检查" if structural_only else "# 项目提示词审查任务",
        "",
        f"提示词地图：{summary['locations']} 个位置，{summary['files']} 个文件，{summary['relations']} 条上位关系。",
    ]
    if "cache" in summary:
        cache = summary["cache"]
        lines.append(
            "增量缓存："
            f"命中 {cache['hits']}，哈希复用 {cache['hash_hits']}，"
            f"重解析 {cache['parsed']}，删除 {cache['removed']}。"
        )
    if structural_only:
        lines.extend([
            "",
            "审查配置：trusted-agent-build；语义审查已跳过，不生成审计提示或改写建议。",
            "",
            "## 确定性检查",
            "",
        ])
        lines.extend(
            f"- {item['id']}: {item['status']} - {item['detail']}"
            for item in task["structural_checks"]
        )
    else:
        lines.extend(["", "## 审查标准", ""])
        for index, criterion in enumerate(task["criteria"], 1):
            lines.append(f"{index}. {criterion['title']}：{criterion['question']}")
        version_basis = task["version_basis"]
        lines.extend([
            "",
            "## 当前版本依据",
            "",
            f"- 上位规范：{version_basis['governing_sources']}",
            f"- 实现证据：{version_basis['implementation_evidence']}",
            f"- 新鲜度边界：{version_basis['freshness_limit']}",
        ])
        lines.extend(["", "## 审查要求", ""])
        lines.extend(f"- {rule}" for rule in task["review_rules"])
    lines.extend(["", "## 提示词位置地图", ""])
    for item in task["locations"]:
        position = item["selector"]
        symbol = f" ({item['symbol']})" if item.get("symbol") else ""
        parents = ", ".join(item["governed_by"]) or "ROOT"
        lines.append(
            f"- [{item['layer']}/{item['mode']}/{item['artifact_type']}] "
            f"{item['path']}#{position}{symbol} <- {parents}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成项目级提示词地图与执行模式感知的审查任务")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--paths", nargs="+", help="仅生成指定文件的审查任务；默认覆盖全项目")
    parser.add_argument("--task", help="显式执行任务；与 --backend 同时提供")
    parser.add_argument("--backend", choices=("agent", "api"),
                        help="显式执行后端；与 --task 同时提供")
    parser.add_argument("--cache-path", default=str(DEFAULT_CACHE_PATH),
                        help="增量缓存路径；相对路径从仓库根目录解析")
    cache_mode = parser.add_mutually_exclusive_group()
    cache_mode.add_argument("--refresh-cache", action="store_true", help="强制重新解析本次范围")
    cache_mode.add_argument("--no-cache", action="store_true", help="本次禁用缓存读写")
    args = parser.parse_args()
    if bool(args.task) != bool(args.backend):
        parser.error("--task 与 --backend 必须同时提供")

    cache_path = Path(args.cache_path).expanduser()
    if not cache_path.is_absolute():
        cache_path = REPO / cache_path

    try:
        locations, cache_stats = collect_prompt_map(
            args.paths, cache_path=cache_path,
            cache_enabled=not args.no_cache, refresh_cache=args.refresh_cache,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not locations:
        print("ERROR: 未发现提示词位置", file=sys.stderr)
        raise SystemExit(2)

    task = build_review_task(
        locations, cache_stats, task=args.task or "", backend=args.backend or "",
    )
    if args.format == "json":
        print(json.dumps(task, ensure_ascii=False, indent=2))
    else:
        print(render_text(task), end="")


if __name__ == "__main__":
    main()

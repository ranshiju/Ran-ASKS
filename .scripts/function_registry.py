#!/usr/bin/env python3
"""Canonical runtime function catalog, policy resolver, and drift validator."""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
REGISTRY_PATH = REPO / "operations/config/function-registry.yaml"

VALID_KINDS = {"function", "tool", "pipeline", "state_operation", "worker"}
VALID_AUDIENCE = {"user", "agent"}
VALID_OWNERS = {"host_agent", "api_controller", "code"}
VALID_CALLERS = {"main_agent", "sub_agent", "api_controller", "pipeline", "worker"}
VALID_BACKENDS = {"agent", "api"}
VALID_EFFECTS = {"read", "temp", "project", "raw", "wiki", "graph", "remote", "state"}
VALID_MATURITY = {"stable", "preview", "internal", "compat", "deprecated"}

class RegistryError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode,
                              deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise RegistryError(
                f"function registry 含重复 YAML key {key!r}"
                f"（line {key_node.start_mark.line + 1}）"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader) or {}
    except RegistryError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise RegistryError(f"无法读取 function registry: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError("function registry 顶层必须是 mapping")
    return data


def _string_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise RegistryError(f"{label} 必须是{'非空' if not allow_empty else ''}字符串列表")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise RegistryError(f"{label} 含非法值")
    if len(set(value)) != len(value):
        raise RegistryError(f"{label} 含重复值")
    return list(value)


def _check_enum_list(record: dict, key: str, allowed: set[str], label: str,
                     *, required: bool = True) -> list[str]:
    if key not in record and not required:
        return []
    values = _string_list(record.get(key), f"{label}.{key}", allow_empty=False)
    invalid = sorted(set(values) - allowed)
    if invalid:
        raise RegistryError(f"{label}.{key} 含未知值: {invalid}")
    return values


def _binding_owners(data: dict, binding: str) -> dict[str, str]:
    owners: dict[str, str] = {}
    for function_id, record in data["functions"].items():
        values = (record.get("bindings") or {}).get(binding, [])
        if isinstance(values, dict):
            values = list(values)
        for value in values or []:
            if value in owners:
                raise RegistryError(
                    f"{binding} binding 重复归属: {value} -> {owners[value]}, {function_id}"
                )
            owners[value] = function_id
    return owners


def _route_profiles(data: dict) -> dict[str, set[str]]:
    profiles: dict[str, set[str]] = {}
    for function_id, record in data["functions"].items():
        mapping = (record.get("bindings") or {}).get("route_capabilities", {})
        if not isinstance(mapping, dict):
            raise RegistryError(f"{function_id}.bindings.route_capabilities 必须是 mapping")
        for name, values in mapping.items():
            if name in profiles:
                raise RegistryError(f"route capability 重复归属: {name}")
            profiles[name] = set(_string_list(values, f"{function_id}.route_capabilities.{name}", allow_empty=False))
    return profiles


def dsh_providers(data: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    data = data if data is not None else load_registry()
    adapters = data.get("adapters")
    if not isinstance(adapters, dict):
        raise RegistryError("adapters 必须是 mapping")
    dsh = adapters.get("dsh")
    if not isinstance(dsh, dict):
        raise RegistryError("adapters.dsh 必须是 mapping")
    providers = dsh.get("providers")
    if not isinstance(providers, list) or not providers:
        raise RegistryError("adapters.dsh.providers 必须是非空列表")
    result: list[tuple[str, str]] = []
    for index, provider in enumerate(providers):
        label = f"adapters.dsh.providers[{index}]"
        if not isinstance(provider, dict) or set(provider) != {"module", "factory"}:
            raise RegistryError(f"{label} 必须只含 module/factory")
        module = provider.get("module")
        factory = provider.get("factory")
        if not isinstance(module, str) or not module.startswith("dsh."):
            raise RegistryError(f"{label}.module 必须位于 dsh 包")
        if not isinstance(factory, str) or not factory.startswith("build_"):
            raise RegistryError(f"{label}.factory 必须是 build_* factory")
        module_path = (REPO / module.replace(".", "/")).with_suffix(".py")
        if not module_path.is_file():
            raise RegistryError(f"{label}.module 不存在: {module}")
        result.append((module, factory))
    if len(result) != len(set(result)):
        raise RegistryError("adapters.dsh.providers 含重复 provider")
    return result


def validate_registry(data: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    try:
        data = data if data is not None else load_registry()
        if data.get("schema") != "function-registry-v1" or data.get("version") != 1:
            raise RegistryError("function registry schema/version 不受支持")
        dsh_providers(data)
        states = data.get("states")
        functions = data.get("functions")
        if not isinstance(states, dict) or not states:
            raise RegistryError("states 必须是非空 mapping")
        if not isinstance(functions, dict) or not functions:
            raise RegistryError("functions 必须是非空 mapping")

        state_route_tasks: dict[str, str] = {}
        for state_id, state in states.items():
            if not isinstance(state, dict) or not state.get("title") or not state.get("authority"):
                raise RegistryError(f"state 缺少 title/authority: {state_id}")
            for task in _string_list(state.get("route_tasks", []), f"states.{state_id}.route_tasks"):
                if task in state_route_tasks:
                    raise RegistryError(f"state route task 重复: {task}")
                state_route_tasks[task] = state_id

        for function_id, record in functions.items():
            label = f"functions.{function_id}"
            if not isinstance(record, dict) or not record.get("title") or not record.get("summary"):
                raise RegistryError(f"{label} 缺少 title/summary")
            if record.get("kind") not in VALID_KINDS:
                raise RegistryError(f"{label}.kind 非法: {record.get('kind')}")
            audience = _check_enum_list(record, "audience", VALID_AUDIENCE, label)
            _check_enum_list(record, "control_owners", VALID_OWNERS, label)
            _check_enum_list(record, "allowed_callers", VALID_CALLERS, label)
            backends = _check_enum_list(record, "backends", VALID_BACKENDS, label)
            _check_enum_list(record, "effects", VALID_EFFECTS, label)
            if record.get("maturity") not in VALID_MATURITY:
                raise RegistryError(f"{label}.maturity 非法: {record.get('maturity')}")
            for state_id in _string_list(record.get("states", []), f"{label}.states"):
                if state_id not in states:
                    raise RegistryError(f"{label}.states 引用未知状态: {state_id}")
            if record["kind"] == "worker":
                if "user" in audience or backends != ["api"]:
                    raise RegistryError(f"{label} worker 必须是 agent audience 且仅 api backend")
                if "main_agent" in record["allowed_callers"] or "sub_agent" in record["allowed_callers"]:
                    raise RegistryError(f"{label} worker 不能由 main/sub-agent 直接调用")

            bindings = record.get("bindings") or {}
            if not isinstance(bindings, dict) or not bindings:
                raise RegistryError(f"{label}.bindings 必须是非空 mapping")
            for key in ("route_tasks", "wg", "dsh", "cli", "pipelines", "implementations"):
                if key in bindings:
                    values = _string_list(bindings[key], f"{label}.bindings.{key}", allow_empty=False)
                    if key in {"cli", "pipelines", "implementations"}:
                        missing = [value for value in values if not (REPO / value).exists()]
                        if missing:
                            raise RegistryError(f"{label}.bindings.{key} 路径不存在: {missing}")
            aliases = bindings.get("route_task_aliases", {})
            if not isinstance(aliases, dict) or not all(
                isinstance(key, str) and key and isinstance(value, str) and value
                for key, value in aliases.items()
            ):
                raise RegistryError(f"{label}.bindings.route_task_aliases 必须是字符串 mapping")

        route_tasks = _binding_owners(data, "route_tasks")
        route_aliases = _binding_owners(data, "route_task_aliases")
        overlap = sorted((set(route_tasks) | set(route_aliases)) & set(state_route_tasks))
        if overlap:
            raise RegistryError(f"route task 同时归属状态与功能: {overlap}")
        _binding_owners(data, "wg")
        dsh_owners = _binding_owners(data, "dsh")
        _route_profiles(data)
        for tool_name, function_id in dsh_owners.items():
            if "api" not in functions[function_id]["backends"]:
                raise RegistryError(f"DSH 工具 {tool_name} 归属的 {function_id} 不支持 api backend")
    except RegistryError as exc:
        errors.append(str(exc))
    return errors


def _literal_mapping(path: Path, name: str) -> ast.Dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            if not isinstance(statement.value, ast.Dict):
                raise RegistryError(f"{path.name}:{name} 必须是显式字典")
            return statement.value
    raise RegistryError(f"{path.name} 未找到 {name}")


def _literal_keys(mapping: ast.Dict, label: str) -> set[str]:
    values = []
    for key in mapping.keys:
        value = ast.literal_eval(key)
        if not isinstance(value, str):
            raise RegistryError(f"{label} 含非字符串 key")
        values.append(value)
    if len(values) != len(set(values)):
        raise RegistryError(f"{label} 含重复 key")
    return set(values)


def observed_route() -> tuple[set[str], dict[str, set[str]]]:
    path = SCRIPTS / "route.py"
    tasks = _literal_keys(_literal_mapping(path, "ROUTES"), "ROUTES")
    capability_ast = _literal_mapping(path, "CAPABILITY_ROUTES")
    profiles: dict[str, set[str]] = {}
    for key, value in zip(capability_ast.keys, capability_ast.values):
        name = ast.literal_eval(key)
        if not isinstance(name, str) or not isinstance(value, ast.Dict):
            raise RegistryError("CAPABILITY_ROUTES 必须是显式两层字典")
        profiles[name] = _literal_keys(value, f"CAPABILITY_ROUTES.{name}")
    return tasks, profiles


def observed_wg_commands() -> set[str]:
    tree = ast.parse((SCRIPTS / "wg.py").read_text(encoding="utf-8"), filename="wg.py")
    commands = set()
    for node in ast.walk(tree):
        call = node if isinstance(node, ast.Call) else None
        if not call or not isinstance(call.func, ast.Attribute) or call.func.attr != "add_parser":
            continue
        if not isinstance(call.func.value, ast.Name) or call.func.value.id != "sub" or not call.args:
            continue
        name = ast.literal_eval(call.args[0])
        if isinstance(name, str):
            commands.add(name)
    return commands


def load_dsh_tools(data: dict[str, Any] | None = None) -> list[Any]:
    data = data if data is not None else load_registry()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    tools: list[Any] = []
    for module_name, factory_name in dsh_providers(data):
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        tools.extend(factory())
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise RegistryError(f"DSH provider 间工具名重复: {duplicates}")
    return tools


def observed_dsh_tools(data: dict[str, Any] | None = None) -> set[str]:
    return {tool.name for tool in load_dsh_tools(data)}


def _registered_route_tasks(data: dict) -> set[str]:
    tasks = set(_binding_owners(data, "route_tasks")) | set(_binding_owners(data, "route_task_aliases"))
    for state in data["states"].values():
        tasks.update(state.get("route_tasks") or [])
    return tasks


def registered_route_surface(data: dict[str, Any] | None = None) -> tuple[set[str], set[str]]:
    """Return managed Route task labels and on-demand capability labels."""
    data = data if data is not None else load_registry()
    return _registered_route_tasks(data), set(_route_profiles(data))


def validate_runtime(data: dict[str, Any] | None = None) -> list[str]:
    if data is None:
        try:
            data = load_registry()
        except RegistryError as exc:
            return [str(exc)]
    errors = validate_registry(data)
    if errors:
        return errors
    try:
        observed_tasks, observed_profiles = observed_route()
        registered_tasks = _registered_route_tasks(data)
        if observed_tasks != registered_tasks:
            errors.append(
                f"Route task 漂移: missing={sorted(observed_tasks - registered_tasks)} "
                f"stale={sorted(registered_tasks - observed_tasks)}"
            )
        registered_profiles = _route_profiles(data)
        if observed_profiles != registered_profiles:
            errors.append(
                f"Route capability 漂移: observed={_sorted_sets(observed_profiles)} "
                f"registered={_sorted_sets(registered_profiles)}"
            )
        observed_wg = observed_wg_commands()
        registered_wg = set(_binding_owners(data, "wg"))
        if observed_wg != registered_wg:
            errors.append(
                f"wg command 漂移: missing={sorted(observed_wg - registered_wg)} "
                f"stale={sorted(registered_wg - observed_wg)}"
            )
        observed_dsh = observed_dsh_tools(data)
        registered_dsh = set(_binding_owners(data, "dsh"))
        if observed_dsh != registered_dsh:
            errors.append(
                f"DSH tool 漂移: missing={sorted(observed_dsh - registered_dsh)} "
                f"stale={sorted(registered_dsh - observed_dsh)}"
            )
    except (RegistryError, ImportError, AttributeError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return errors


def _sorted_sets(mapping: dict[str, set[str]]) -> dict[str, list[str]]:
    return {key: sorted(value) for key, value in sorted(mapping.items())}


def function_for_dsh_tool(tool_name: str, data: dict[str, Any] | None = None) -> str:
    data = data or load_registry()
    return _binding_owners(data, "dsh").get(tool_name, "")


def dsh_tool_metadata(tool_name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_registry()
    function_id = function_for_dsh_tool(tool_name, data)
    if not function_id:
        raise RegistryError(f"DSH 工具未登记: {tool_name}")
    record = data["functions"][function_id]
    return {
        "function_id": function_id,
        "audience": list(record["audience"]),
        "allowed_callers": list(record["allowed_callers"]),
        "effects": list(record["effects"]),
        "maturity": record["maturity"],
    }


def catalog(*, audience: str = "", kind: str = "", include_internal: bool = False,
            data: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = data or load_registry()
    result = []
    for function_id, record in data["functions"].items():
        if audience and audience not in record["audience"]:
            continue
        if kind and kind != record["kind"]:
            continue
        if not include_internal and record["maturity"] == "internal":
            continue
        result.append({
            "id": function_id,
            "title": record["title"],
            "kind": record["kind"],
            "audience": list(record["audience"]),
            "backends": list(record["backends"]),
            "maturity": record["maturity"],
            "summary": record["summary"],
        })
    return result


def show(function_id: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_registry()
    if function_id not in data["functions"]:
        raise RegistryError(f"未知功能: {function_id}")
    result = deepcopy(data["functions"][function_id])
    result["id"] = function_id
    return result


def resolve(function_id: str, *, caller: str, backend: str, state: str = "",
            data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load_registry()
    record = show(function_id, data)
    if caller not in VALID_CALLERS:
        raise RegistryError(f"未知调用者: {caller}")
    if backend not in VALID_BACKENDS:
        raise RegistryError(f"未知 backend: {backend}")
    if caller not in record["allowed_callers"]:
        raise RegistryError(f"{caller} 无权调用 {function_id}")
    if backend not in record["backends"]:
        raise RegistryError(f"{function_id} 不支持 {backend} backend")
    allowed_states = record.get("states") or []
    if state and state not in data["states"]:
        raise RegistryError(f"未知状态: {state}")
    if state and allowed_states and state not in allowed_states:
        raise RegistryError(f"{function_id} 不允许在 {state} 状态调用")
    bindings = deepcopy(record["bindings"])
    if backend == "agent":
        bindings.pop("dsh", None)
    return {
        "schema": "function-resolution-v1",
        "function_id": function_id,
        "caller": caller,
        "backend": backend,
        "state": state or None,
        "control_owners": record["control_owners"],
        "effects": record["effects"],
        "bindings": bindings,
    }


def render_route_listing(data: dict[str, Any] | None = None) -> str:
    data = data or load_registry()
    lines = ["持续状态:"]
    for state_id, state in data["states"].items():
        tasks = state.get("route_tasks") or []
        suffix = f" route={','.join(tasks)}" if tasks else ""
        lines.append(f"  {state_id}: {state['title']}{suffix}")
    lines.append("可路由任务:")
    for function_id, record in data["functions"].items():
        for task in (record.get("bindings") or {}).get("route_tasks", []):
            lines.append(f"  {task}: {function_id}")
    lines.append("按需能力:")
    for function_id, record in data["functions"].items():
        for name, profiles in (record.get("bindings") or {}).get("route_capabilities", {}).items():
            lines.append(f"  {name}: {function_id} profiles={','.join(sorted(profiles))}")
    aliases = []
    for function_id, record in data["functions"].items():
        for name, target in (record.get("bindings") or {}).get("route_task_aliases", {}).items():
            aliases.append((name, function_id, target))
    if aliases:
        lines.append("兼容 task 别名:")
        for name, function_id, target in aliases:
            lines.append(f"  {name}: {function_id} -> {target}")
    return "\n".join(lines)


def _emit(value: Any, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    if isinstance(value, list):
        for item in value:
            print(f"{item['id']}\t{item['title']}\t{item['kind']}\t{item['maturity']}")
        return
    print(yaml.safe_dump(value, allow_unicode=True, sort_keys=False).rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WikiGraph 统一功能注册表")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--audience", choices=sorted(VALID_AUDIENCE), default="")
    list_parser.add_argument("--kind", choices=sorted(VALID_KINDS), default="")
    list_parser.add_argument("--include-internal", action="store_true")
    list_parser.add_argument("--format", choices=("text", "json"), default="text")

    show_parser = sub.add_parser("show")
    show_parser.add_argument("function_id")
    show_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")

    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("function_id")
    resolve_parser.add_argument("--caller", required=True, choices=sorted(VALID_CALLERS))
    resolve_parser.add_argument("--backend", required=True, choices=sorted(VALID_BACKENDS))
    resolve_parser.add_argument("--state", default="")
    resolve_parser.add_argument("--format", choices=("yaml", "json"), default="yaml")

    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--runtime", action="store_true")
    validate_parser.add_argument("--format", choices=("text", "json"), default="text")

    args = parser.parse_args(argv)
    try:
        data = load_registry()
        if args.command == "list":
            _emit(catalog(audience=args.audience, kind=args.kind,
                          include_internal=args.include_internal, data=data), args.format)
        elif args.command == "show":
            _emit(show(args.function_id, data), args.format)
        elif args.command == "resolve":
            _emit(resolve(args.function_id, caller=args.caller, backend=args.backend,
                          state=args.state, data=data), args.format)
        else:
            errors = validate_runtime(data) if args.runtime else validate_registry(data)
            payload = {"schema": "function-registry-validation-v1",
                       "status": "error" if errors else "ok", "errors": errors}
            if args.format == "json":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif errors:
                print("功能注册表无效:")
                for error in errors:
                    print(f"- {error}")
            else:
                print(f"功能注册表有效: {len(data['functions'])} 功能, {len(data['states'])} 状态")
            return 1 if errors else 0
    except (OSError, RegistryError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

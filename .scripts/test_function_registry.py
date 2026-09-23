#!/usr/bin/env python3
"""Runtime function registry coverage and policy regression."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / ".scripts"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import function_registry as registry


def test_registry_and_runtime_have_exact_coverage():
    data = registry.load_registry()
    assert registry.validate_registry(data) == []
    assert registry.validate_runtime(data) == []
    assert len(data["functions"]) == 24
    assert set(data["states"]) == {"workspace", "research", "frontier"}
    assert len(registry.observed_dsh_tools()) == 38


def test_dsh_tools_receive_canonical_policy_metadata():
    tools = registry.load_dsh_tools()
    assert len(tools) == 38
    assert all(tool.function_id for tool in tools)
    assert all("api_controller" in tool.allowed_callers for tool in tools)
    assert next(tool for tool in tools if tool.name == "read_raw").function_id == "knowledge.query"
    assert next(tool for tool in tools if tool.name == "hub_route").function_id == "knowledge.hub"
    assert next(tool for tool in tools if tool.name == "comic_generate").function_id == "artifact.comic"


def test_registry_rejects_duplicate_yaml_keys():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "registry.yaml"
        path.write_text("schema: first\nschema: second\n", encoding="utf-8")
        try:
            registry.load_registry(path)
        except registry.RegistryError as exc:
            assert "重复 YAML key" in str(exc)
        else:
            raise AssertionError("duplicate YAML key was silently accepted")


def test_registry_reports_invalid_yaml():
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "registry.yaml"
        path.write_text("functions: [\n", encoding="utf-8")
        try:
            registry.load_registry(path)
        except registry.RegistryError as exc:
            assert "无法读取 function registry" in str(exc)
        else:
            raise AssertionError("invalid YAML was silently accepted")


def test_resolution_enforces_caller_backend_and_state():
    resolved = registry.resolve(
        "knowledge.query", caller="sub_agent", backend="agent", state="research",
    )
    assert resolved["function_id"] == "knowledge.query"
    assert "dsh" not in resolved["bindings"]

    api = registry.resolve("knowledge.query", caller="api_controller", backend="api")
    assert "graph_search" in api["bindings"]["dsh"]

    for function_id, caller, backend in (
        ("knowledge.ingest", "sub_agent", "agent"),
        ("worker.ingest.paper_workspace", "main_agent", "api"),
        ("artifact.write", "main_agent", "api"),
    ):
        try:
            registry.resolve(function_id, caller=caller, backend=backend)
        except registry.RegistryError:
            pass
        else:
            raise AssertionError(f"expected policy rejection: {function_id}/{caller}/{backend}")


def test_fixed_pipelines_remain_code_owned():
    data = registry.load_registry()
    ingest = data["functions"]["knowledge.ingest"]
    presentation = data["functions"]["artifact.presentation"]
    assert ingest["kind"] == "pipeline" and "code" in ingest["control_owners"]
    assert ".scripts/ingest_pipeline.py" in ingest["bindings"]["pipelines"]
    assert presentation["kind"] == "pipeline" and "code" in presentation["control_owners"]
    assert "dsh" not in presentation["bindings"]


def test_runtime_drift_is_reported():
    data = deepcopy(registry.load_registry())
    data["functions"]["knowledge.query"]["bindings"]["dsh"].remove("graph_search")
    errors = registry.validate_runtime(data)
    assert any("DSH tool 漂移" in error and "graph_search" in error for error in errors), errors


def test_route_and_wg_use_registry_discovery():
    route = subprocess.run(
        [sys.executable, str(SCRIPTS / "route.py"), "--list"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout
    assert "持续状态:" in route and "可路由任务:" in route and "按需能力:" in route
    assert "task/state" not in route

    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "wg.py"), "functions", "show", "knowledge.query"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["result"]["id"] == "knowledge.query"
    assert payload["sources"] == ["operations/config/function-registry.yaml"]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"function registry regression: {len(tests)}/{len(tests)} PASS")

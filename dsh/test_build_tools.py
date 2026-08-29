#!/usr/bin/env python3
"""build_tools.py DSH 建设能力 seam 回归测试。"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dsh.build_tools import BuildLocatorCockpit, build_build_tools
from dsh.guards.build_locator_guard import (
    TOOL_BUILD_IMPACT, TOOL_BUILD_READ, TOOL_BUILD_LIST, BuildLocatorGuard,
)
from dsh.harness import ToolExecution, ToolExecutionResult


def test_build_tools_expose_three_capabilities():
    names = [tool.name for tool in build_build_tools()]
    assert names == [TOOL_BUILD_IMPACT, TOOL_BUILD_READ, TOOL_BUILD_LIST], names


def test_guard_denies_read_before_impact():
    guard = BuildLocatorGuard()
    decision = guard.on_pre_execute(ToolExecution(
        name=TOOL_BUILD_READ,
        arguments={"locator": "operations/engineering/graph.yaml#yaml:/nodes/ingest_paper"},
    ))
    assert decision is not None and decision.kind == "deny"
    assert "impact" in decision.reason


def test_guard_denies_unprefixed_list_before_impact():
    guard = BuildLocatorGuard()
    decision = guard.on_pre_execute(ToolExecution(
        name=TOOL_BUILD_LIST,
        arguments={"path": "operations/engineering/graph.yaml", "prefix": "yaml:/"},
    ))
    assert decision is not None and decision.kind == "deny"


def test_impact_success_marks_impact_seen():
    guard = BuildLocatorGuard()
    result = ToolExecutionResult(content=json.dumps({"ok": True}))
    guard.on_post_execute(ToolExecution(
        name=TOOL_BUILD_IMPACT, arguments={"target": "ingest_paper"}), result)
    assert guard.audit.impact_seen is True
    assert guard.audit.impact_target == "ingest_paper"


def test_guard_accepts_canonical_locator_after_impact():
    guard = BuildLocatorGuard()
    guard.on_post_execute(ToolExecution(name=TOOL_BUILD_IMPACT, arguments={"target": "ingest_paper"}),
                          ToolExecutionResult(content=json.dumps({"ok": True})))
    decision = guard.on_pre_execute(ToolExecution(
        name=TOOL_BUILD_READ,
        arguments={"locator": "operations/engineering/graph.yaml#yaml:/script_contracts/ingest_paper"},
    ))
    assert decision is None
    assert guard.audit.read_locators == [
        "operations/engineering/graph.yaml#yaml:/script_contracts/ingest_paper"
    ]


def test_guard_rejects_raw_or_wiki_paths():
    guard = BuildLocatorGuard()
    guard.on_post_execute(ToolExecution(name=TOOL_BUILD_IMPACT, arguments={"target": "ingest_paper"}),
                          ToolExecutionResult(content=json.dumps({"ok": True})))
    for bad in ("academic/raw/references/x/paper.md#md:title",
                "academic/wiki/papers/x.md#md:title",
                "cross-domain/graph.db#L1-L2"):
        decision = guard.on_pre_execute(ToolExecution(name=TOOL_BUILD_READ, arguments={"locator": bad}))
        assert decision is not None and decision.kind == "deny", bad


def test_cockpit_has_audit_state():
    cockpit = BuildLocatorCockpit()
    assert cockpit.audit()["compliant"] is False
    assert any(schema["name"] == TOOL_BUILD_IMPACT for schema in cockpit.schemas())
    assert "impact" in cockpit.start_prompt("ingest_paper")


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            return 1
    print(f"build_tools regression: {passed}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

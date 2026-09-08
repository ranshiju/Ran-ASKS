#!/usr/bin/env python3
"""Regression tests for independent Agent/API research-profile adapters."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import research_memory as memory


def _project(root: Path) -> None:
    project = root / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("研究量子采样并验证复杂度。\n", encoding="utf-8")


def test_agent_refresh_prepares_bounded_task_and_apply_validates():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _project(root)
        originals = memory.REPO, memory.PROJECTS_DIR, memory.agent_task.research_backend
        try:
            memory.REPO = root
            memory.PROJECTS_DIR = root / "projects"
            memory.agent_task.research_backend = lambda: "agent"
            task = memory.refresh_profile("demo")
            assert task["schema"] == "agent-task-v1"
            assert task["status"] == "prepared"
            assert task["kind"] == "research_profile"
            output = root / task["outputs"][0]["path"]
            output.write_text(json.dumps({
                "topic": "量子采样复杂度",
                "keywords": ["量子采样", "复杂度"],
                "stage": "experiment",
                "active_questions": ["优势是否稳健？"],
            }, ensure_ascii=False), encoding="utf-8")
            applied = memory.apply_profile("demo", output)
            assert applied["status"] == "completed"
            assert memory.load_profile("demo")["stage"] == "experiment"
        finally:
            memory.REPO, memory.PROJECTS_DIR, memory.agent_task.research_backend = originals


def test_invalid_agent_profile_preserves_previous_profile():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _project(root)
        originals = memory.REPO, memory.PROJECTS_DIR
        try:
            memory.REPO = root
            memory.PROJECTS_DIR = root / "projects"
            previous = {
                "topic": "old", "keywords": [], "stage": "unknown",
                "active_questions": [], "updated_at": "before",
            }
            memory.save_profile("demo", previous)
            output = root / "temp" / "research-memory" / "bad.json"
            output.parent.mkdir(parents=True)
            output.write_text('{"topic":"bad"}', encoding="utf-8")
            try:
                memory.apply_profile("demo", output)
                raise AssertionError("invalid profile must fail")
            except ValueError:
                pass
            assert memory.load_profile("demo") == previous
        finally:
            memory.REPO, memory.PROJECTS_DIR = originals


def test_api_refresh_uses_model_adapter_and_shared_schema():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _project(root)
        originals = memory.REPO, memory.PROJECTS_DIR, memory.agent_task.research_backend
        old_module = sys.modules.get("llm_structured")
        calls = []
        try:
            memory.REPO = root
            memory.PROJECTS_DIR = root / "projects"
            memory.agent_task.research_backend = lambda: "api"
            sys.modules["llm_structured"] = SimpleNamespace(call_text=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {
                    "ok": True,
                    "text": json.dumps({
                        "topic": "API profile", "keywords": ["sampling"],
                        "stage": "writing", "active_questions": ["What remains?"],
                    }),
                }
            ))
            profile = memory.refresh_profile("demo")
            assert calls and profile["topic"] == "API profile"
            assert memory.load_profile("demo")["stage"] == "writing"
        finally:
            memory.REPO, memory.PROJECTS_DIR, memory.agent_task.research_backend = originals
            if old_module is None:
                sys.modules.pop("llm_structured", None)
            else:
                sys.modules["llm_structured"] = old_module


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"research memory regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

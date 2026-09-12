#!/usr/bin/env python3
"""Regression tests for the host-Agent task envelope and temp boundary."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import agent_task


def test_backends_are_explicit_and_independent():
    original_repo = agent_task.REPO
    original = {name: os.environ.get(name) for name in (
        "INGEST_BACKEND", "QUERY_BACKEND", "RESEARCH_BACKEND",
    )}
    try:
        with tempfile.TemporaryDirectory() as directory:
            agent_task.REPO = Path(directory)
            for name in original:
                os.environ.pop(name, None)
            assert agent_task.ingest_backend() == "agent"
            assert agent_task.query_backend() == "agent"
            assert agent_task.research_backend() == "agent"
            os.environ["QUERY_BACKEND"] = "api"
            assert agent_task.query_backend() == "api"
            assert agent_task.research_backend() == "agent"
            os.environ["RESEARCH_BACKEND"] = "api"
            assert agent_task.research_backend() == "api"
            os.environ["INGEST_BACKEND"] = "hybrid"
            try:
                agent_task.ingest_backend()
                raise AssertionError("invalid backend must fail")
            except RuntimeError:
                pass
    finally:
        agent_task.REPO = original_repo
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_backends_preserve_dotenv_configuration_with_environment_precedence():
    original_repo = agent_task.REPO
    original = {name: os.environ.get(name) for name in (
        "INGEST_BACKEND", "QUERY_BACKEND", "RESEARCH_BACKEND",
    )}
    try:
        with tempfile.TemporaryDirectory() as directory:
            agent_task.REPO = Path(directory)
            (agent_task.REPO / ".env").write_text(
                "INGEST_BACKEND=api\nQUERY_BACKEND=api\nRESEARCH_BACKEND=api\n",
                encoding="utf-8",
            )
            for name in original:
                os.environ.pop(name, None)
            assert agent_task.ingest_backend() == "api"
            assert agent_task.query_backend() == "api"
            assert agent_task.research_backend() == "api"
            os.environ["QUERY_BACKEND"] = "agent"
            assert agent_task.query_backend() == "agent"
    finally:
        agent_task.REPO = original_repo
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_task_lifecycle_and_required_outputs():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        source = repo / "input.txt"
        source.write_text("evidence", encoding="utf-8")
        output = repo / "temp" / "demo" / "result.json"
        state = {}
        task = agent_task.prepare(
            state,
            kind="demo",
            transaction_id="txn",
            inputs=[{"name": "source", "path": "input.txt"}],
            outputs=[{"name": "result", "path": "temp/demo/result.json"}],
            protocol={"name": "demo-v1"},
            commands={"commit": "python3 demo.py --commit"},
        )
        assert task["schema"] == "agent-task-v1"
        assert agent_task.is_prepared(state)
        assert agent_task.missing_outputs(state, repo) == ["temp/demo/result.json"]
        output.parent.mkdir(parents=True)
        output.write_text("{}\n", encoding="utf-8")
        assert agent_task.missing_outputs(state, repo) == []
        agent_task.reopen(state, ["schema mismatch"])
        assert state["agent_task"]["issues"] == ["schema mismatch"]
        agent_task.mark_consumed(state)
        assert state["agent_task"]["status"] == "consumed"


def test_task_rejects_non_temp_output_and_escaping_artifact():
    try:
        agent_task.make_task(
            kind="bad", transaction_id="txn",
            inputs=[{"name": "source", "path": "input.txt"}],
            outputs=[{"name": "result", "path": "wiki/result.md"}],
            protocol={"name": "bad-v1"},
        )
        raise AssertionError("managed Agent output must remain in temp")
    except ValueError:
        pass
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        outside = repo / "temp" / "other" / "result.json"
        outside.parent.mkdir(parents=True)
        outside.write_text("{}", encoding="utf-8")
        try:
            agent_task.resolve_temp_artifact(repo, outside, "owned")
            raise AssertionError("cross-namespace artifact must fail")
        except ValueError:
            pass


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"agent task regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

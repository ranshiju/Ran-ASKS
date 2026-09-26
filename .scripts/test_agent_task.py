#!/usr/bin/env python3
"""Regression tests for the host-Agent task envelope and temp boundary."""
from __future__ import annotations

import json
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
        assert task["control"] == {
            "version": "agent-task-control-v1",
            "loop": ["inspect", "write_outputs", "advance"],
            "allowed_actions": ["commit"],
            "write_scope": "declared_outputs_only",
            "validation_authority": "declared_managed_commands",
            "transaction_policy": "same_transaction_until_terminal",
            "terminal_workflow_statuses": ["completed", "failed"],
        }
        assert agent_task.is_prepared(state)
        assert agent_task.missing_outputs(state, repo) == ["temp/demo/result.json"]
        view = agent_task.control_view(state, repo)
        assert view["next_action"] == "write_outputs"
        assert view["workflow_status"] == "awaiting_agent"
        output.parent.mkdir(parents=True)
        output.write_text("{}\n", encoding="utf-8")
        assert agent_task.missing_outputs(state, repo) == []
        assert agent_task.control_view(state, repo)["next_action"] == "advance"
        agent_task.reopen(state, ["schema mismatch"])
        assert state["agent_task"]["issues"] == ["schema mismatch"]
        agent_task.mark_consumed(state)
        assert state["agent_task"]["status"] == "consumed"


def test_control_view_compacts_large_context_without_mutating_state():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        large_review = "review line\n" * 1000
        state = {}
        agent_task.prepare(
            state, kind="demo", transaction_id="txn",
            inputs=[{"name": "source", "path": "input.txt"}],
            outputs=[{
                "name": "result", "path": "temp/demo/result.json",
                "required": False,
            }],
            protocol={"name": "demo-v1"},
            commands={"resume": "python3 .scripts/demo.py --resume txn"},
            context={"presentation_review": large_review, "page": 3},
        )
        view = agent_task.control_view(state, repo)
        marker = view["task"]["context"]["presentation_review"]
        assert marker["compacted"] is True
        assert marker["type"] == "str"
        assert marker["bytes"] == len(json.dumps(
            large_review, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"))
        assert len(marker["sha256"]) == 64
        assert view["task"]["context"]["page"] == 3
        assert state["agent_task"]["context"]["presentation_review"] == large_review


def test_completed_control_view_does_not_request_cleaned_temp_outputs():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        state = {}
        agent_task.prepare(
            state, kind="demo", transaction_id="txn",
            inputs=[{"name": "source", "path": "input.txt"}],
            outputs=[{"name": "result", "path": "temp/demo/result.json"}],
            protocol={"name": "demo-v1"},
            commands={"resume": "python3 .scripts/demo.py --resume txn"},
        )
        state["status"] = "completed"
        view = agent_task.control_view(state, repo)
        assert view["workflow_status"] == "completed"
        assert view["next_action"] == "none"
        assert view["missing_outputs"] == []


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


def test_managed_action_runs_without_shell_and_rejects_unmanaged_commands():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        scripts = repo / ".scripts"
        scripts.mkdir()
        runner = scripts / "runner.py"
        runner.write_text(
            "import json, os\n"
            "print(json.dumps({'status': 'ok', 'backend': os.environ.get('INGEST_BACKEND')}))\n",
            encoding="utf-8",
        )
        state = {}
        agent_task.prepare(
            state, kind="demo", transaction_id="txn",
            inputs=[{"name": "source", "path": "input.txt"}],
            outputs=[{"name": "result", "path": "temp/demo/result.json", "required": False}],
            protocol={"name": "demo-v1"},
            commands={"resume": "INGEST_BACKEND=agent python3 .scripts/runner.py"},
        )
        result = agent_task.run_action(state, "resume", repo)
        assert result["returncode"] == 0
        assert json.loads(result["stdout"])["backend"] == "agent"
        assert result["environment_overrides"] == ["INGEST_BACKEND"]

        state["agent_task"]["commands"]["resume"] = "python3 -c 'print(1)'"
        try:
            agent_task.run_action(state, "resume", repo)
            raise AssertionError("python -c must not be accepted")
        except ValueError as exc:
            assert ".scripts" in str(exc) or "入口" in str(exc)

        state["agent_task"]["commands"]["resume"] = (
            "SECRET=value python3 .scripts/runner.py"
        )
        try:
            agent_task.run_action(state, "resume", repo)
            raise AssertionError("arbitrary environment overrides must not be accepted")
        except ValueError as exc:
            assert "环境覆盖" in str(exc)


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"agent task regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

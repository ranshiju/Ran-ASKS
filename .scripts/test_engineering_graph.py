#!/usr/bin/env python3
"""Engineering graph change-set and completeness regressions."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import engineering_graph as graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git", "-c", "user.email=test@example.invalid",
            "-c", "user.name=Engineering Graph Test", *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_changed_path_resolution_is_exact_and_honest() -> None:
    nodes = {
        "tool": {"path": ".scripts/tool.py", "role": "tool"},
        "schemas": {"path": "<domain>/SCHEMA.md", "role": "schema-family"},
        "workspace": {"path": "projects/example/", "role": "workspace"},
    }
    resolved, unresolved = graph.resolve_changed_files(nodes, [
        ".scripts/tool.py",
        "academic/SCHEMA.md",
        "projects/example/notes/status.md",
        "unknown.py",
    ])
    assert {item["node_id"] for item in resolved} == {"tool", "schemas", "workspace"}
    assert unresolved == [{"path": "unknown.py", "reason": "unregistered_path"}]


def test_change_classification_separates_state_from_graph_gaps() -> None:
    nodes = {
        "tool": {"path": ".scripts/tool.py", "kind": "implementation", "role": "tool"},
        "graph_db": {"path": "cross-domain/graph.db", "kind": "data", "role": "state"},
        "experiment": {
            "path": "projects/example/run.py", "kind": "implementation", "role": "experiment",
        },
    }
    resolved, unresolved, out_of_scope = graph.classify_changed_files(nodes, [
        ".scripts/tool.py",
        "cross-domain/graph.db",
        "academic/wiki/index.md",
        "projects/example/run.py",
        "projects/example/notes.md",
        "operations/new-policy.md",
    ])
    assert {item["node_id"] for item in resolved} == {"tool", "experiment"}
    assert unresolved == [{
        "path": "operations/new-policy.md", "reason": "unregistered_path",
    }]
    assert {item["path"] for item in out_of_scope} == {
        "cross-domain/graph.db", "academic/wiki/index.md", "projects/example/notes.md",
    }


def test_impact_result_bounds_and_verification_classes() -> None:
    nodes = {
        "seed": {"path": "seed.py", "role": "seed"},
        "near": {"path": "near.py", "role": "near"},
        "far": {"path": "far.py", "role": "far"},
    }
    edges = [["seed", "uses", "near"], ["near", "uses", "far"]]
    verification = {
        "seed": ["python3 test_seed.py", "python3 test_shared.py"],
        "near": ["python3 test_near.py", "python3 test_shared.py"],
    }
    result = graph.build_impact_result(
        nodes,
        edges,
        {},
        ["seed"],
        source={"kind": "files"},
        changed_files=["seed.py", "unknown.py"],
        resolved_files=[{"path": "seed.py", "node_id": "seed"}],
        unresolved_files=[{"path": "unknown.py", "reason": "unregistered_path"}],
        verification=verification,
        max_results=2,
    )
    assert result["schema"] == graph.IMPACT_SCHEMA
    assert result["status"] == "partial"
    assert result["reason"] == "changed_files_only_partially_registered"
    assert result["completeness"]["affected_nodes_total"] == 3
    assert result["completeness"]["affected_nodes_returned"] == 2
    assert result["completeness"]["affected_nodes_omitted"] == 1
    assert result["completeness"]["truncated"] is True
    assert [entry["command"] for entry in result["verification"]["direct"]] == [
        "python3 test_seed.py", "python3 test_shared.py",
    ]
    assert [entry["command"] for entry in result["verification"]["related"]] == [
        "python3 test_near.py",
    ]


def test_git_working_tree_includes_staged_unstaged_and_untracked() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo / "staged.py", "value = 1\n")
        _write(repo / "unstaged.py", "value = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "initial")

        _write(repo / "staged.py", "value = 2\n")
        _git(repo, "add", "staged.py")
        _write(repo / "unstaged.py", "value = 2\n")
        _write(repo / "untracked.py", "value = 1\n")

        paths, source = graph.discover_git_changes("working-tree", repo=repo)
        assert source == {"kind": "working-tree"}
        assert paths == ["staged.py", "unstaged.py", "untracked.py"]


def test_git_base_uses_merge_base() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        _git(repo, "init", "-q", "-b", "main")
        _write(repo / "shared.py", "value = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "initial")
        _git(repo, "switch", "-qc", "feature")
        _write(repo / "feature.py", "value = 1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "feature")

        paths, source = graph.discover_git_changes("base", "main", repo=repo)
        assert paths == ["feature.py"]
        assert source["kind"] == "base"
        assert source["base"] == "main"
        assert source["merge_base"] == _git(repo, "merge-base", "HEAD", "main")


def test_git_timeout_is_not_reported_as_empty() -> None:
    timeout = subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=0.01)
    with patch.object(graph.subprocess, "run", side_effect=timeout):
        try:
            graph.discover_git_changes("staged", timeout=0.01)
        except graph.ChangeDiscoveryError as exc:
            assert "未把超时解释为无变化" in str(exc)
        else:
            raise AssertionError("Git timeout unexpectedly became an empty change set")


if __name__ == "__main__":
    test_changed_path_resolution_is_exact_and_honest()
    test_change_classification_separates_state_from_graph_gaps()
    test_impact_result_bounds_and_verification_classes()
    test_git_working_tree_includes_staged_unstaged_and_untracked()
    test_git_base_uses_merge_base()
    test_git_timeout_is_not_reported_as_empty()
    print("engineering graph regression: PASS")

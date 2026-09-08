#!/usr/bin/env python3
"""Regression tests for the managed user-assertion ingestion transaction."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("ingest_user_assertions.py")
spec = importlib.util.spec_from_file_location("ingest_user_assertions", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


FACT_ID = "fact-alice-advises-bob-20260905"
FACT_LINE = f"- [2026-09-05] **Alice advises Bob.** {{: #{FACT_ID}}}"
GRAPH_SCRIPT_FILES = {
    "direction_matcher.py",
    "embed_helper.py",
    "graph_delta.py",
    "graph_ingest.py",
    "graph_lib.py",
    "hub_semantics.py",
    "ingest_check.py",
    "inbox_state.py",
    "knowledge_ir.py",
    "node_semantics.py",
    "predicate_tiers.yaml",
    "source_locator.py",
    "sync_keyword_aliases.py",
    "wiki_locator.py",
}


def _setup_repo(root: Path) -> tuple[Path, Path, Path, Path]:
    pending = root / "inbox" / "facts-pending.md"
    raw = root / "cross-domain" / "raw" / "facts" / "user-assertions.md"
    graph = root / "cross-domain" / "graph.db"
    wiki = root / "academic" / "wiki" / "authors" / "alice.md"
    pending.parent.mkdir(parents=True)
    raw.parent.mkdir(parents=True)
    wiki.parent.mkdir(parents=True)
    pending.write_text(f"# Pending\n\n{FACT_LINE}\n", encoding="utf-8")
    raw.write_text("# Assertions\n", encoding="utf-8")
    graph.write_bytes(b"graph-before")
    wiki.write_text("old wiki\n", encoding="utf-8")
    return pending, raw, graph, wiki


def _write_proposal(root: Path, prepared: dict, wiki: Path, *, valid_wiki: bool = False) -> None:
    locator = f"cross-domain/raw/facts/user-assertions.md#{FACT_ID}"
    if valid_wiki:
        content = (
            "---\ntitle: Alice\ntype: people\nsources:\n"
            f"  - {locator}\nsource_type: user-assertion\n"
            "date: 2026-09-05\nconfidence: medium\nstatus: current\n"
            "created: 2026-09-05\nupdated: 2026-09-05\n---\n"
            "# Alice\n\n## Navigation\n\nAlice advises Bob.[^f1]\n\n"
            "## Content\n\nAlice advises Bob.\n\n"
            f"[^f1]: {locator}\n"
        )
    else:
        content = (
            "---\ntitle: Alice\ntype: people\nsources:\n"
            f"  - {locator}\nsource_type: user-assertion\n---\n"
            "# Alice\n\n## Core Triples\n\nAlice | advises | Bob.[^f1]\n\n"
            f"[^f1]: {locator}\n"
        )
    proposal = {
        "schema": module.PROPOSAL_SCHEMA,
        "transaction_id": prepared["transaction_id"],
        "handled_fact_ids": [FACT_ID],
        "wiki_updates": [{
            "page": "academic/wiki/authors/alice.md",
            "before_sha256": hashlib.sha256(wiki.read_bytes()).hexdigest(),
            "content": content,
        }],
        "relations": [{
            "page": "academic/wiki/authors/alice.md",
            "fact_id": FACT_ID,
            "subject": "Alice",
            "predicate": "advises",
            "object": "Bob",
            "confidence": "[可追溯]",
        }],
    }
    (root / prepared["write_to"]).write_text(
        json.dumps(proposal, ensure_ascii=False), encoding="utf-8",
    )


def _install_graph_runtime(root: Path) -> None:
    scripts = root / ".scripts"
    scripts.mkdir()
    for name in GRAPH_SCRIPT_FILES:
        shutil.copy2(SCRIPT.with_name(name), scripts / name)
    shutil.copytree(SCRIPT.parent.parent / "operations" / "config", root / "operations" / "config")


def _initialize_graph(root: Path, graph: Path) -> None:
    graph.write_bytes(b"")
    initialized = subprocess.run(
        [sys.executable, ".scripts/graph_ingest.py", "--db", str(graph), "init"],
        cwd=root, text=True, capture_output=True,
    )
    assert initialized.returncode == 0, initialized.stdout + initialized.stderr


def test_prepare_is_idempotent_and_does_not_modify_fact_sources():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pending, raw, _graph, _wiki = _setup_repo(root)
        before_pending, before_raw = pending.read_bytes(), raw.read_bytes()
        first = module.prepare_transaction(repo=root)
        second = module.prepare_transaction(repo=root)
        assert first["status"] == "prepared"
        assert first["transaction_id"] == second["transaction_id"]
        assert first["next_action"] == "complete_agent_task"
        assert first["agent_task"]["schema"] == "agent-task-v1"
        assert (root / first["manifest_path"]).is_file()
        assert pending.read_bytes() == before_pending
        assert raw.read_bytes() == before_raw


def test_apply_commits_raw_wiki_graph_validation_and_pending_together():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pending, raw, graph, wiki = _setup_repo(root)
        prepared = module.prepare_transaction(repo=root)
        _write_proposal(root, prepared, wiki)
        original_run = module._run_command
        try:
            module._run_command = lambda command, _repo: subprocess.CompletedProcess(
                command, 0, stdout='{"status":"completed","graph_report":{}}\n', stderr="",
            )
            result = module.apply_transaction(prepared["transaction_id"], repo=root)
        finally:
            module._run_command = original_run
        assert result["status"] == "completed"
        assert FACT_LINE in raw.read_text(encoding="utf-8")
        assert FACT_ID in wiki.read_text(encoding="utf-8")
        assert FACT_LINE not in pending.read_text(encoding="utf-8")
        assert graph.read_bytes() == b"graph-before"
        receipt = json.loads((root / result["receipt_path"]).read_text(encoding="utf-8"))
        assert receipt["status"] == "completed"


def test_apply_rolls_back_every_layer_when_final_validation_fails():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pending, raw, graph, wiki = _setup_repo(root)
        originals = [pending.read_bytes(), raw.read_bytes(), graph.read_bytes(), wiki.read_bytes()]
        prepared = module.prepare_transaction(repo=root)
        _write_proposal(root, prepared, wiki)
        original_run = module._run_command

        def fail_validation(command, _repo):
            if "graph_ingest.py" in command:
                return subprocess.CompletedProcess(command, 0, stdout='{"status":"completed"}\n', stderr="")
            return subprocess.CompletedProcess(command, 1, stdout="ERROR=1", stderr="")

        try:
            module._run_command = fail_validation
            result = module.apply_transaction(prepared["transaction_id"], repo=root)
        finally:
            module._run_command = original_run
        assert result["status"] == "failed"
        assert result["rolled_back"] is True
        assert [pending.read_bytes(), raw.read_bytes(), graph.read_bytes(), wiki.read_bytes()] == originals


def test_apply_runs_real_graph_ingest_and_ingest_check_in_isolated_repo():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        pending, raw, graph, wiki = _setup_repo(root)
        _install_graph_runtime(root)
        _initialize_graph(root, graph)
        graph_before = graph.read_bytes()
        prepared = module.prepare_transaction(repo=root)
        _write_proposal(root, prepared, wiki, valid_wiki=True)

        result = module.apply_transaction(prepared["transaction_id"], repo=root)

        assert result["status"] == "completed", result
        assert result["validations"][0]["returncode"] == 0
        assert result["graph_reports"][0]["page"] == "academic/wiki/authors/alice"
        assert "hub_dynamics" in result["graph_reports"][0]
        assert graph.read_bytes() != graph_before
        assert FACT_LINE in raw.read_text(encoding="utf-8")
        assert FACT_ID in wiki.read_text(encoding="utf-8")
        assert FACT_LINE not in pending.read_text(encoding="utf-8")


def main() -> None:
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"user assertion transaction regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

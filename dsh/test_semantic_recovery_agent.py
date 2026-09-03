#!/usr/bin/env python3
"""Regression tests for the bounded semantic recovery specialist."""
from __future__ import annotations

import tempfile
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / ".scripts") not in sys.path:
    sys.path.insert(0, str(REPO / ".scripts"))

from dsh import semantic_recovery_agent as sra
from dsh import semantic_recovery_eval as sre
import ingest_common as ic
from dsh.semantic_recovery_agent import (
    AgentResult,
    AgentBudget,
    SemanticRecoveryAgent,
    make_task_envelope,
)


def _envelope(budget: AgentBudget | None = None):
    state = {
        "transaction_id": "txn-1",
        "semantic_path": "temp/inbox-state/txn-1-semantic.txt",
        "wiki_path": "academic/wiki/papers/test",
        "extract_dir": "temp/inbox-extract/txn-1",
    }
    issues = [{
        "id": "issue-01",
        "issue_code": "malformed_predicate",
        "section": "三元组",
        "line": "A | 实验/结果 | B",
        "field": "predicate",
        "observed": "实验/结果",
        "expected": "单一谓词",
        "reason": "谓词含标点",
        "suggested_actions": ["replace"],
        "retryable": True,
        "fingerprint": "abc",
    }]
    return make_task_envelope(
        state, issues, "三元组:\nA | 实验/结果 | B\n",
        "## 方法\nA 产生 B。", "实验表明 A 产生 B。", budget,
    )


def test_proposal_is_returned_without_write_tools():
    calls = []

    def fake_call(*_args, **kwargs):
        calls.append(kwargs)
        return {
            "ok": True,
            "status": "ok",
            "model": "GLM-5.3-Flash",
            "history": [{
                "model": "GLM-5.3-Flash",
                "status": "ok",
                "usage": {"total_tokens": 42},
            }],
            "usage": {"total_tokens": 42},
            "parsed": {
                "decision": "propose",
                "proposal": {
                    "protocol_version": "semantic-patch-v1",
                    "review_status": "patched",
                    "patches": [{
                        "issue_id": "issue-01",
                        "action": "replace",
                        "replacement_lines": ["A | 得到 | B"],
                    }],
                    "review_notes": [],
                },
            },
        }

    agent = SemanticRecoveryAgent(
        _envelope(), "三元组:\nA | 实验/结果 | B\n",
        "## 方法\nA 产生 B。", "实验表明 A 产生 B。", fake_call,
    )
    result = agent.run()
    assert result.status == "resolved"
    assert result.total_tokens == 42
    assert result.models == ["GLM-5.3-Flash"]
    assert result.api_calls == 1
    assert result.tool_calls == 0
    assert all("write" not in name and "commit" not in name for name in agent.registry.names())
    assert calls[0]["retries"] == 0
    assert calls[0]["operation"] == "ingest_semantic_recovery"


def test_duplicate_tool_action_stops_immediately():
    decisions = [
        {"decision": "tool", "tool": "inspect_issue_context",
         "arguments": {"issue_ids": ["issue-01"]}, "reason": "inspect"},
        {"decision": "tool", "tool": "inspect_issue_context",
         "arguments": {"issue_ids": ["issue-01"]}, "reason": "inspect again"},
    ]

    def fake_call(*_args, **_kwargs):
        return {"ok": True, "status": "ok", "parsed": decisions.pop(0), "usage": {}}

    text = "三元组:\nA | 实验/结果 | B\n"
    result = SemanticRecoveryAgent(
        _envelope(), text, "wiki", "source", fake_call,
    ).run()
    assert result.status == "escalated"
    assert result.reason == "repeated_action"
    assert result.tool_calls == 1
    assert result.repeated_actions == 1


def test_attempted_fallback_models_are_traced():
    def fake_call(*_args, **_kwargs):
        return {
            "ok": True,
            "status": "ok",
            "model": "GLM-5.3-Flash",
            "history": [
                {"model": "candidate-model", "status": "api_error", "usage": {}},
                {"model": "GLM-5.3-Flash", "status": "ok", "usage": {}},
            ],
            "usage": {},
            "parsed": {
                "decision": "propose",
                "proposal": {
                    "protocol_version": "semantic-patch-v1",
                    "review_status": "patched",
                    "patches": [{
                        "issue_id": "issue-01",
                        "action": "replace",
                        "replacement_lines": ["A | 得到 | B"],
                    }],
                    "review_notes": [],
                },
            },
        }

    result = SemanticRecoveryAgent(
        _envelope(), "三元组:\nA | 实验/结果 | B\n",
        "## 方法\nA 产生 B。", "实验表明 A 产生 B。", fake_call,
    ).run()
    assert result.status == "resolved"
    assert result.models == ["candidate-model", "GLM-5.3-Flash"]
    assert result.api_calls == 2


def test_turn_budget_is_hard_limit():
    counter = {"calls": 0}

    def fake_call(*_args, **_kwargs):
        counter["calls"] += 1
        return {
            "ok": True,
            "status": "ok",
            "parsed": {
                "decision": "tool",
                "tool": "inspect_issue_context",
                "arguments": {"issue_ids": ["issue-01", f"unknown-{counter['calls']}"]},
                "reason": "inspect",
            },
            "usage": {},
        }

    budget = AgentBudget(max_turns=2, max_tool_calls=6)
    result = SemanticRecoveryAgent(
        _envelope(budget), "semantic", "wiki", "source", fake_call,
    ).run()
    assert counter["calls"] == 2
    assert result.status == "escalated"
    assert result.reason == "turn_budget_exhausted"


def test_ingest_accepts_only_validator_passed_proposal():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "temp" / "inbox-state" / "semantic.txt"
        semantic.parent.mkdir(parents=True)
        original = "三元组:\n本文 | 实验/结果 | 纠缠熵增长\n"
        semantic.write_text(original, encoding="utf-8")
        state = {
            "transaction_id": "txn-integration",
            "semantic_path": "temp/inbox-state/semantic.txt",
            "extract_dir": "extract",
            "wiki_content": "## 结果\n实验验证纠缠熵增长。",
        }
        hard = ["谓词格式不合法: 实验/结果 (主体=本文, 客体=纠缠熵增长)"]

        class FakeAgent:
            def __init__(self, envelope, *_args):
                self.envelope = envelope

            def run(self):
                return AgentResult(
                    status="resolved", task_id=self.envelope.task_id,
                    proposal={
                        "protocol_version": "semantic-patch-v1",
                        "review_status": "patched",
                        "patches": [{
                            "issue_id": "issue-01", "action": "replace",
                            "replacement_lines": ["本文 | 验证 | 纠缠熵增长"],
                        }],
                        "review_notes": [],
                    },
                )

        old_agent = sra.SemanticRecoveryAgent
        sra.SemanticRecoveryAgent = FakeAgent
        try:
            def validate(_state):
                return (hard, []) if "实验/结果" in semantic.read_text(encoding="utf-8") else ([], [])

            ok, message = ic.try_semantic_recovery(state, root, hard, [], validate)
        finally:
            sra.SemanticRecoveryAgent = old_agent
        assert ok, message
        assert "本文 | 验证 | 纠缠熵增长" in semantic.read_text(encoding="utf-8")
        assert state["semantic_recovery_agent"]["status"] == "accepted"
        assert state["semantic_recovery_agent"]["issue_delta"] == 1


def test_ingest_restores_rejected_candidate():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "temp" / "inbox-state" / "semantic.txt"
        semantic.parent.mkdir(parents=True)
        original = "三元组:\n本文 | 实验/结果 | 纠缠熵增长\n"
        semantic.write_text(original, encoding="utf-8")
        state = {
            "transaction_id": "txn-rollback",
            "semantic_path": "temp/inbox-state/semantic.txt",
            "extract_dir": "extract",
            "wiki_content": "wiki",
        }
        hard = ["谓词格式不合法: 实验/结果 (主体=本文, 客体=纠缠熵增长)"]

        class FakeAgent:
            def __init__(self, envelope, *_args):
                self.envelope = envelope

            def run(self):
                return AgentResult(
                    status="resolved", task_id=self.envelope.task_id,
                    proposal={
                        "protocol_version": "semantic-patch-v1",
                        "review_status": "patched",
                        "patches": [{
                            "issue_id": "issue-01", "action": "replace",
                            "replacement_lines": ["本文 | 仍/非法 | 纠缠熵增长"],
                        }],
                        "review_notes": [],
                    },
                )

        old_agent = sra.SemanticRecoveryAgent
        sra.SemanticRecoveryAgent = FakeAgent
        try:
            ok, _message = ic.try_semantic_recovery(
                state, root, hard, [], lambda _state: (["仍非法"], []),
            )
        finally:
            sra.SemanticRecoveryAgent = old_agent
        assert not ok
        assert semantic.read_text(encoding="utf-8") == original
        assert state["semantic_recovery_agent"]["status"] == "rejected"
        assert state["semantic_recovery_agent"]["reason"] == "validator_rejected"


def test_ingest_rejects_non_staged_semantic_path_without_model_call():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = root / "raw" / "source.txt"
        raw.parent.mkdir(parents=True)
        raw.write_text("三元组:\n本文 | 实验/结果 | B\n", encoding="utf-8")
        state = {
            "transaction_id": "txn-redline",
            "semantic_path": "raw/source.txt",
        }
        ok, message = ic.try_semantic_recovery(
            state, root,
            ["谓词格式不合法: 实验/结果 (主体=本文, 客体=B)"],
            [], lambda _state: ([], []),
        )
        assert not ok
        assert "staged temp" in message
        assert raw.read_text(encoding="utf-8") == "三元组:\n本文 | 实验/结果 | B\n"


def test_eval_routing_policy_allows_no_fallback_and_excludes_gpt():
    with tempfile.TemporaryDirectory() as directory:
        catalog_path = Path(directory) / "llm-models.yaml"
        catalog_path.write_text(
            "models:\n"
            "  - GLM-5.3-Flash\n"
            "  - AWS-GPT-5.6-Terra\n"
            "sub_agent_policy:\n"
            "  text_model: GLM-5.3-Flash\n"
            "  status: fixed_pending_evaluation\n"
            "  excluded_families: [GPT]\n"
            "  runtime_model_selection: disabled\n"
            "  automatic_promotion: disabled\n"
            "preferred:\n"
            "  semantic_recovery_agent: GLM-5.3-Flash\n"
            "fallback: {}\n"
            "agent_profiles:\n"
            "  semantic_recovery_agent:\n"
            "    excluded_families: [GPT]\n",
            encoding="utf-8",
        )
        original_catalog = sre.MODEL_CATALOG
        try:
            sre.MODEL_CATALOG = catalog_path
            policy = sre.validate_routing_policy()
            assert policy["selected"] == {
                "preferred": "GLM-5.3-Flash",
                "fallback": None,
            }

            catalog_path.write_text(
                catalog_path.read_text(encoding="utf-8").replace(
                    "semantic_recovery_agent: GLM-5.3-Flash",
                    "semantic_recovery_agent: AWS-GPT-5.6-Terra",
                ).replace(
                    "text_model: GLM-5.3-Flash",
                    "text_model: AWS-GPT-5.6-Terra",
                ),
                encoding="utf-8",
            )
            try:
                sre.validate_routing_policy()
            except ValueError as exc:
                assert "excluded family" in str(exc)
            else:
                raise AssertionError("GPT-family preferred model must be rejected")
        finally:
            sre.MODEL_CATALOG = original_catalog


if __name__ == "__main__":
    test_proposal_is_returned_without_write_tools()
    test_duplicate_tool_action_stops_immediately()
    test_attempted_fallback_models_are_traced()
    test_turn_budget_is_hard_limit()
    test_ingest_accepts_only_validator_passed_proposal()
    test_ingest_restores_rejected_candidate()
    test_ingest_rejects_non_staged_semantic_path_without_model_call()
    test_eval_routing_policy_allows_no_fallback_and_excludes_gpt()
    print("semantic recovery agent tests: OK")

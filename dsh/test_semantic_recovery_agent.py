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
    assert calls[0]["allow_profile_fallback"] is False


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
                {"model": "candidate-model", "status": "validation_error", "usage": {"total_tokens": 30}},
                {"model": "GLM-5.3-Flash", "status": "ok", "usage": {"total_tokens": 40}},
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
    assert result.total_tokens == 70


def test_remaining_time_and_request_budget_bound_next_call():
    calls = []

    def fake_call(*_args, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "model": "candidate", "usage": {"total_tokens": 5},
                "parsed": {"decision": "tool", "tool": "inspect_issue_context",
                           "arguments": {"issue_ids": ["issue-01"]}, "reason": "inspect"}}

    budget = AgentBudget(max_model_calls=1, max_elapsed_sec=5, request_timeout_sec=30)
    result = SemanticRecoveryAgent(_envelope(budget), "semantic", "wiki", "source", fake_call).run()
    assert len(calls) == 1 and 0 < calls[0]["timeout_sec"] <= 5
    assert result.reason == "model_call_budget_exhausted"
    assert result.api_calls == 1


def test_failed_attempt_tokens_can_exhaust_budget():
    def fake_call(*_args, **_kwargs):
        return {"ok": True, "usage": {"total_tokens": 2},
                "history": [{"model": "candidate", "usage": {"total_tokens": 90}},
                            {"model": "candidate", "usage": {"total_tokens": 2}}]}

    result = SemanticRecoveryAgent(
        _envelope(AgentBudget(max_total_tokens=80)), "semantic", "wiki", "source", fake_call).run()
    assert result.reason == "token_budget_exhausted" and result.total_tokens == 92


def test_exam_distinguishes_repair_abstention_and_wrong_proposal():
    cases = sre.load_exam(sre.DEFAULT_EXAM)["cases"]
    case = cases[0]
    result = AgentResult(status="resolved", task_id="exam", api_calls=2, proposal={
        "patches": [{"issue_id": "issue-01", "action": "replace", "replacement_lines": ["wrong"]}]})
    score = sre.score_case(case, result)
    assert not score["case_passed"] and not score["autonomously_resolved"]
    assert score["api_calls"] == 2
    result.proposal["patches"][0]["replacement_lines"] = case["expected"]["accepted_replacements"]["issue-01"][:1]
    assert sre.score_case(case, result)["autonomously_resolved"]
    result.status = "abstained"
    result.proposal["patches"][0].update(action="abstain", replacement_lines=[])
    score = sre.score_case(cases[-1], result)
    assert score["case_passed"] and score["correct_abstention"]
    assert not score["autonomously_resolved"]


def test_small_and_cross_language_context_is_delivered_within_budget():
    text = "The experiment verifies entanglement entropy growth."
    assert SemanticRecoveryAgent._best_excerpt(text, ["纠缠熵增长"], 500) == text
    long_text = "irrelevant " * 1000 + "epochs should be scaled slightly faster" + " tail" * 1000
    needles = SemanticRecoveryAgent._needles({"line": "本文 | 发现 | epoch增长快于参数增长"})
    excerpt = SemanticRecoveryAgent._best_excerpt(long_text, needles, 500)
    assert "epochs should be scaled slightly faster" in excerpt and len(excerpt) <= 500
    assert SemanticRecoveryAgent._best_excerpt(long_text, ["missing"], 500) == "[NO MATCH]"


def test_responsibility_issue_targets_only_unique_predicate():
    error = "负责人关系缺少原文同一行的明确职责措辞: 李某"
    text = "三元组:\n项目甲 | 负责人 | 李某\n项目甲 | 涉及 | 联合培养\n"
    issues = ic.build_structured_semantic_issues([error], [], text)
    assert issues[0]["retryable"] and issues[0]["field"] == "predicate"
    assert issues[0]["issue_code"] == "unsupported_responsibility"
    assert "保持主体与客体不变" in issues[0]["expected"]
    proposal = {"patches": [{"issue_id": "issue-01", "action": "replace",
                            "replacement_lines": ["任意主体 | 联系人 | 任意对象"]}]}
    candidate = ic.apply_semantic_recovery_proposal(text, proposal, issues)
    assert candidate == text.replace(" | 负责人 | ", " | 联系人 | ")
    for ambiguous in [text + "项目乙 | 负责人 | 李某\n", "三元组:\n项目甲 | 负责人 | 王某\n"]:
        issue = ic.build_structured_semantic_issues([error], [], ambiguous)[0]
        assert not issue["retryable"] and issue["line"] == ""


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


def test_specialist_model_override_does_not_change_global_default():
    from unittest.mock import patch
    catalog = {"models": ["GLM-5.3-Flash", "GLM-5.3-EV"],
               "sub_agent_policy": {"text_model": "GLM-5.3-Flash", "excluded_families": ["GPT"]},
               "agent_profiles": {"semantic_recovery_agent": {"text_model": "GLM-5.3-EV"}},
               "preferred": {"semantic_recovery_agent": "GLM-5.3-EV"}}
    with patch.object(sre, "model_catalog", return_value=catalog):
        assert sre.validate_routing_policy()["text_model"] == "GLM-5.3-EV"
    assert catalog["sub_agent_policy"]["text_model"] == "GLM-5.3-Flash"



def test_ignored_paper_relation_targets_unique_concept_role_only():
    prefix = "论文级方向/标签谓词会在图编译时被忽略: "
    suffix = "；请依据 Wiki 证据改为研究基础/核心方法/对比方法，或删除该关系"
    error = prefix + "本论文 | 主要研究 | 蒙特卡洛模拟" + suffix
    text = "三元组:\n本文 | 主要研究 | 蒙特卡洛模拟\n算法甲 | 应用于 | 自旋模型\n"
    issues = ic.build_structured_semantic_issues([error], [], text)
    issue = issues[0]
    assert issue["retryable"] and issue["field"] == "predicate"
    assert issue["issue_code"] == "ignored_paper_relation"
    assert issue["allowed_values"] == ["研究基础", "核心方法", "对比方法"]
    proposal = {"patches": [{"issue_id": "issue-01", "action": "replace",
                            "replacement_lines": ["changed subject | 研究基础 | changed object"]}]}
    assert ic.apply_semantic_recovery_proposal(text, proposal, issues) == text.replace(" | 主要研究 | ", " | 研究基础 | ")
    for replacement in ("本文 | 核心创新点 | 蒙特卡洛模拟", "本文 | 涉及 | 蒙特卡洛模拟", "invalid line"):
        proposal["patches"][0]["replacement_lines"] = [replacement]
        assert ic.apply_semantic_recovery_proposal(text, proposal, issues) is None
    for ambiguous in (text + "本论文 | 主要研究 | 蒙特卡洛模拟\n", text.replace("本文 |", "其他论文 |"), "三元组:\n"):
        assert not ic.build_structured_semantic_issues([error], [], ambiguous)[0]["retryable"]
    # A concept-to-concept relation is never selected as the paper relation.
    unrelated = "三元组:\n算法甲 | 主要研究 | 蒙特卡洛模拟\n"
    assert not ic.build_structured_semantic_issues([error], [], unrelated)[0]["retryable"]

if __name__ == "__main__":
    test_proposal_is_returned_without_write_tools()
    test_duplicate_tool_action_stops_immediately()
    test_attempted_fallback_models_are_traced()
    test_remaining_time_and_request_budget_bound_next_call()
    test_failed_attempt_tokens_can_exhaust_budget()
    test_exam_distinguishes_repair_abstention_and_wrong_proposal()
    test_small_and_cross_language_context_is_delivered_within_budget()
    test_responsibility_issue_targets_only_unique_predicate()
    test_ignored_paper_relation_targets_unique_concept_role_only()
    test_turn_budget_is_hard_limit()
    test_ingest_accepts_only_validator_passed_proposal()
    test_ingest_restores_rejected_candidate()
    test_ingest_rejects_non_staged_semantic_path_without_model_call()
    test_eval_routing_policy_allows_no_fallback_and_excludes_gpt()
    test_specialist_model_override_does_not_change_global_default()
    print("semantic recovery agent tests: OK")

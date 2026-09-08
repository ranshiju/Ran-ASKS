#!/usr/bin/env python3
"""Regression tests for the generic persistent workspace-state kernel."""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import workspace_state as state
import research_project


@contextmanager
def isolated_repo():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        projects = root / "projects"
        projects.mkdir()
        originals = state.REPO, state.PROJECTS_DIR
        try:
            state.REPO = root
            state.PROJECTS_DIR = projects
            yield root
        finally:
            state.REPO, state.PROJECTS_DIR = originals


def test_init_imports_existing_status_and_rebuilds_projections():
    with isolated_repo() as root:
        project = root / "projects" / "demo"
        (project / "notes").mkdir(parents=True)
        (project / "notes" / "status.md").write_text("# 旧状态\n\n关键历史。\n", encoding="utf-8")
        result = state.init_workspace("demo", profile="role_work",
                                      domain="academic_administration")
        assert result["imported_status_memory"].startswith("MEM-")
        assert (project / "workspace.yaml").is_file()
        assert state.load_profile(project)["profile"] == "role_work"
        assert state.STATUS_MARKER in (project / "notes" / "status.md").read_text(encoding="utf-8")
        memories = state.load_memories(project)
        assert len(memories) == 1 and "关键历史" in memories[0][1]
        with sqlite3.connect(project / ".workspace" / "index.sqlite") as conn:
            assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert state.doctor("demo")["ok"] is True


def test_item_state_requirements_reject_invalid_source_write():
    with isolated_repo() as root:
        state.init_workspace("demo")
        try:
            state.add_item("demo", title="缺下一步", state="active")
            raise AssertionError("active without next_action must fail")
        except state.WorkspaceError as exc:
            assert "next_action" in str(exc)
        assert not list((root / "projects" / "demo" / ".workspace" / "items").glob("*.md"))
        saved = state.add_item("demo", title="可执行事项", state="active",
                               next_action="完成第一步")
        assert saved["id"].startswith("ITEM-") and len(saved["id"].split("-")) == 3
        state.update_item("demo", saved["id"], {
            "state": "waiting", "waiting_on": "外部回复", "review_at": "2026-09-15",
        })
        item = state.load_items(root / "projects" / "demo")[0][0]
        assert item["state"] == "waiting" and item["waiting_on"] == "外部回复"


def test_role_work_proposals_use_three_simple_statuses_and_derived_item_state():
    with isolated_repo() as root:
        state.init_workspace("role", profile="role_work", domain="management")
        initial_status = (root / "projects" / "role" / "notes" / "status.md").read_text(encoding="utf-8")
        assert all(heading in initial_status for heading in ("### 待提", "### 待讨论", "### 已讨论"))
        proposal = state.add_proposal("role", title="讨论实验室空间安排")
        item_id = proposal["id"]
        item = state.load_items(root / "projects" / "role")[0][0]
        assert item["type"] == "proposal"
        assert item["proposal_status"] == "pending_submission"
        assert item["state"] == "active" and item["next_action"] == "提交提案"

        state.update_proposal("role", item_id, {}, proposal_status="pending_discussion")
        item = state.load_items(root / "projects" / "role")[0][0]
        assert item["proposal_status"] == "pending_discussion"
        assert item["state"] == "active" and item["next_action"] == "等待班子会讨论"

        state.update_proposal("role", item_id, {}, proposal_status="discussed",
                              discussed_at="2026-09-15", outcome="原则同意")
        item = state.load_items(root / "projects" / "role")[0][0]
        assert item["state"] == "done" and item["outcome"] == "原则同意"
        assert item["discussed_at"] == "2026-09-15"
        listed = state.list_proposals("role", proposal_status="discussed")
        assert listed["count"] == 1 and listed["proposals"][0]["id"] == item_id
        assert state.workspace_summary(root / "projects" / "role")["proposal_counts"]["discussed"] == 1
        status = (root / "projects" / "role" / "notes" / "status.md").read_text(encoding="utf-8")
        assert "## 提案" in status and "### 已讨论" in status and "原则同意" in status
        recalled = state.recall_text("role")
        assert "[提案]" in recalled and "已讨论（1）" in recalled

        try:
            state.add_item("role", title="不一致提案", item_type="proposal", state="done",
                           outcome="完成", proposal_status="pending_submission")
            raise AssertionError("proposal status/item state mismatch must fail")
        except state.WorkspaceError as exc:
            assert "必须映射" in str(exc)
        try:
            state.add_proposal("role", title="未讨论却带结果", outcome="不应接受")
            raise AssertionError("pending proposal outcome must fail")
        except state.WorkspaceError as exc:
            assert "只有已讨论" in str(exc)

        args = state.build_parser().parse_args([
            "proposal", "update", "role", item_id,
            "--status", "discussed", "--outcome", "通过",
        ])
        assert args.proposal_command == "update" and args.status == "discussed"


def test_parent_recall_aggregates_child_summary_without_child_memory():
    with isolated_repo():
        state.init_workspace("parent", profile="role_work", domain="management")
        state.init_workspace("parent/child", profile="role_work", domain="management")
        state.add_item("parent/child", title="子事项", state="active", next_action="子步骤")
        state.add_memory("parent/child", title="子项目秘密决策", kind="decision",
                         content="只应在子工作区召回。")
        parent_text = state.recall_text("parent")
        assert "parent/child" in parent_text
        assert "active=1" in parent_text
        assert "子项目秘密决策" not in parent_text
        assert "只应在子工作区召回" not in parent_text
        parent_status = Path(state.PROJECTS_DIR) / "parent" / "notes" / "status.md"
        assert "active 1" in parent_status.read_text(encoding="utf-8")
        child_text = state.recall_text("parent/child")
        assert "子项目秘密决策" in child_text


def test_memory_supersession_preserves_history_and_effective_recall():
    with isolated_repo() as root:
        state.init_workspace("demo", profile="research", domain="academic")
        old = state.add_memory("demo", title="旧判断", kind="decision", content="采用 A。")
        new = state.add_memory("demo", title="新判断", kind="decision", content="改用 B。",
                               supersedes=old["id"], valid_from="2026-09-01")
        records = {metadata["id"]: metadata for metadata, _body, _path
                   in state.load_memories(root / "projects" / "demo")}
        assert records[old["id"]]["state"] == "superseded"
        assert records[old["id"]]["superseded_by"] == new["id"]
        assert records[new["id"]]["supersedes"] == old["id"]
        recalled = state.recall_text("demo")
        assert "新判断" in recalled and "旧判断" not in recalled


def test_new_research_project_uses_shared_workspace_kernel():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        projects = root / "projects"
        template = projects / "_templates" / "research"
        template.parent.mkdir(parents=True)
        shutil.copytree(research_project.REPO / "projects" / "_templates" / "research", template)
        originals = (
            research_project.PROJECTS_DIR, research_project.TEMPLATE_DIR,
            state.REPO, state.PROJECTS_DIR,
        )
        try:
            research_project.PROJECTS_DIR = projects
            research_project.TEMPLATE_DIR = template
            args = SimpleNamespace(project="demo", name="Demo", topic="共享内核验证",
                                   stage="experiment")
            assert research_project.init_project(args) == 0
            project = projects / "demo"
            config = state.load_workspace_config(project)
            assert config["profile"] == "research"
            assert state.load_profile(project)["topic"] == "共享内核验证"
            items = state.load_items(project)
            assert len(items) == 1 and items[0][0]["state"] == "active"
            assert research_project.validate_project(SimpleNamespace(
                project="demo", strict=False,
            )) == 0
        finally:
            research_project.PROJECTS_DIR, research_project.TEMPLATE_DIR = originals[:2]
            state.REPO, state.PROJECTS_DIR = originals[2:]


def test_rebuild_uses_markdown_and_doctor_reports_corrupt_events():
    with isolated_repo() as root:
        state.init_workspace("demo")
        state.add_item("demo", title="重建事项", state="done", outcome="已经完成")
        project = root / "projects" / "demo"
        (project / ".workspace" / "index.sqlite").unlink()
        (project / "notes" / "status.md").unlink()
        rebuilt = state.rebuild("demo")
        assert rebuilt["items"] == 1
        assert "重建事项" in (project / "notes" / "status.md").read_text(encoding="utf-8")
        with (project / ".workspace" / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        report = state.doctor("demo")
        assert report["ok"] is False
        assert any("JSONL 无法解析" in error for error in report["errors"])


def test_agent_and_api_profiles_share_schema_without_crossing_adapters():
    with isolated_repo() as root:
        state.init_workspace("demo")
        project = root / "projects" / "demo"
        (project / "brief.md").write_text("推进一个通用工作项目。\n", encoding="utf-8")
        original_backend = state.agent_task.backend
        old_module = sys.modules.get("llm_structured")
        calls = []
        try:
            state.agent_task.backend = lambda _variable: "agent"
            task = state.refresh_profile("demo")
            assert task["schema"] == "agent-task-v1"
            assert task["kind"] == "workspace_profile"
            output = root / task["outputs"][0]["path"]
            output.write_text(json.dumps({
                "summary": "通用项目", "keywords": ["项目"], "stage": "execution",
                "active_questions": ["下一步是什么？"],
            }, ensure_ascii=False), encoding="utf-8")
            applied = state.apply_profile("demo", output)
            assert applied["profile"]["stage"] == "execution"

            state.agent_task.backend = lambda _variable: "api"
            sys.modules["llm_structured"] = SimpleNamespace(call_text=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or {"ok": True, "text": json.dumps({
                    "summary": "API 项目", "keywords": ["API"], "stage": "review",
                    "active_questions": [],
                }, ensure_ascii=False)}
            ))
            profile = state.refresh_profile("demo")
            assert calls and profile["stage"] == "review"
        finally:
            state.agent_task.backend = original_backend
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
    print(f"workspace state regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

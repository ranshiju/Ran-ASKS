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
            args = SimpleNamespace(project="research/demo", name="Demo", topic="共享内核验证",
                                   stage="experiment")
            assert research_project.init_project(args) == 0
            project = projects / "research/demo"
            config = state.load_workspace_config(project)
            assert config["profile"] == "research"
            assert state.load_profile(project)["topic"] == "共享内核验证"
            items = state.load_items(project)
            assert len(items) == 1 and items[0][0]["state"] == "active"
            assert research_project.validate_project(SimpleNamespace(
                project="research/demo", strict=False,
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


def test_engineering_context_scopes_effective_memories_and_exact_alias():
    with isolated_repo() as root:
        state.init_workspace("eng", profile="engineering")
        saved = state.add_item("eng", title="坐标修复", item_type="repair", state="done",
                               outcome="回归通过；净收益未测", content="当前结论与证据", tags=["FIX-demo", "coords"])
        state.add_memory("eng", title="匹配", kind="lesson", content="往返验证", applies_to=["coords"], evidence=["report.md"])
        state.add_memory("eng", title="不适用", kind="lesson", content="不应读取", applies_to=["other"])
        state.add_memory("eng", title="未来", kind="lesson", content="未来经验", valid_from="2999-01-01")
        state.add_memory("eng", title="过去", kind="lesson", content="过期经验", valid_to="2000-01-01")
        old = state.add_memory("eng", title="旧记忆", kind="decision", content="旧结论")
        state.add_memory("eng", title="替代", kind="decision", content="新结论", supersedes=old["id"])
        expired = state.add_memory("eng", title="撤销", kind="lesson", content="撤销结论")
        state.expire_memory("eng", expired["id"])
        state.init_workspace("eng/child", profile="engineering")
        state.add_item("eng/child", title="坐标子项", item_type="repair")
        state.add_memory("eng/child", title="子秘密", kind="lesson", content="隔离")
        state.init_workspace("other", profile="engineering")
        state.add_memory("other", title="外部秘密", kind="lesson", content="隔离")
        result = state.workspace_context("eng", item="FIX-demo")
        assert result["items"][0]["metadata"]["id"] == saved["id"]
        assert result["items"][0]["metadata"]["outcome"].endswith("净收益未测")
        assert {r["metadata"]["title"] for r in result["memories"]} == {"匹配", "替代"}
        queried = state.workspace_context("eng", query="坐标")
        assert len(queried["items"]) == 1
        assert state.workspace_context("eng", query="%_")["items"] == []
        state.add_item("eng", title="重名", item_type="observation", tags=["FIX-demo"])
        try:
            state.workspace_context("eng", item="FIX-demo")
            raise AssertionError("ambiguous alias accepted")
        except state.WorkspaceError as exc:
            assert "不唯一" in str(exc)


def test_context_stale_missing_and_injected_index_fail_without_writes():
    with isolated_repo() as root:
        state.init_workspace("eng", profile="engineering")
        saved = state.add_item("eng", title="坐标", item_type="repair", content="原始正文")
        project = root / "projects/eng"
        path = project / ".workspace/items" / (saved["id"] + ".md")
        path.write_text(path.read_text() + "\n新证据\n", encoding="utf-8")
        index = project / ".workspace/index.sqlite"
        before = index.read_bytes()
        try:
            state.workspace_context("eng", item=saved["id"])
            raise AssertionError("stale index accepted")
        except state.WorkspaceError as exc:
            assert "rebuild" in str(exc)
        assert index.read_bytes() == before
        state.rebuild("eng")
        with sqlite3.connect(index) as conn:
            conn.execute("UPDATE items SET source_path='../outside.md'")
        try:
            state.workspace_context("eng", item=saved["id"])
            raise AssertionError("injected path accepted")
        except state.WorkspaceError as exc:
            assert "rebuild" in str(exc)
        state.rebuild("eng")
        with sqlite3.connect(index) as conn:
            conn.execute("UPDATE items SET id='ITEM-../../outside'")
        try:
            state.workspace_context("eng", query="坐标")
            raise AssertionError("injected id accepted")
        except state.WorkspaceError as exc:
            assert "非法记录" in str(exc)
        index.unlink()
        try:
            state.workspace_context("eng", item=saved["id"])
            raise AssertionError("missing index accepted")
        except state.WorkspaceError as exc:
            assert "rebuild" in str(exc)
        assert not index.exists()


def test_context_json_budget_reports_omissions_without_truncating_action():
    with isolated_repo():
        state.init_workspace("eng", profile="engineering")
        for i in range(5):
            state.add_item("eng", title=f"预算{i}", item_type="optimization", state="active",
                           next_action="保留完整行动", content="首段" + 'x"\\\n' * 5000 + "尾部关键历史")
        for i in range(5):
            state.add_memory("eng", title=f"经验{i}", kind="lesson", content="长内容" * 5000)
        result = state.workspace_context("eng", query="预算", max_chars=2500)
        assert len(json.dumps(result, ensure_ascii=False, indent=2)) + 1 <= 2500
        assert result["more_items"] and result["more_memories"]
        assert result["omitted_for_budget"]["memories"] > 0
        assert result["items"][0]["body_truncated"]
        assert result["items"][0]["metadata"]["next_action"] == "保留完整行动"
        full = state.workspace_context("eng", query="预算", max_chars=50000, item_limit=1, memory_limit=0)
        assert "尾部关键历史" in full["items"][0]["body"]


def test_context_reuses_budget_after_omitting_records():
    with isolated_repo():
        state.init_workspace("eng", profile="engineering")
        for i in range(3):
            state.add_item("eng", title=f"预算检查{i}", item_type="repair",
                           content="当前结论：根因与边界已经核验。\n" * 40)
            state.add_memory("eng", title=f"经验{i}", kind="lesson", content="经验及不适用范围。" * 100)
        result = state.workspace_context("eng", query="预算检查", max_chars=2500)
        assert result["omitted_for_budget"]["memories"] > 0
        assert all("当前结论" in record["body"] and "边界" in record["body"]
                   for record in result["items"])
        assert len(json.dumps(result, ensure_ascii=False, indent=2)) + 1 <= 2500
        # A complete short body should fit without the truncation marker overhead.
        short = state.add_item("eng", title="短正文", item_type="repair", content="简短结论")
        result = state.workspace_context("eng", item=short["id"], memory_limit=0, max_chars=2000)
        assert result["items"][0]["body"].strip() == "简短结论"
        assert not result["items"][0]["body_truncated"]


def test_engineering_cli_cross_process_resume():
    import os
    import subprocess
    scripts = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as directory:
        projects = Path(directory) / "projects"
        projects.mkdir()
        env = dict(os.environ, WIKIGRAPH_PROJECTS_DIR=str(projects))
        def call(script, *args, content=None):
            result = subprocess.run([sys.executable, str(scripts / script), *args],
                                    input=content, capture_output=True, text=True, env=env, check=True)
            return json.loads(result.stdout)
        call("workspace_state.py", "init", "engineering", "--profile", "engineering")
        saved = call("workspace_state.py", "item", "add", "engineering", "--type", "repair",
                     "--title", "跨进程接续", "--tags", "FIX-portable", "--state", "active",
                     "--next-action", "验证恢复", "--stdin", content="## 关键历史\n\n原始依据\n")
        call("workspace_state.py", "memory", "add", "engineering", "--title", "局部经验",
             "--kind", "lesson", "--applies-to", "FIX-portable", "--content", "适用范围及理由")
        call("workspace_state.py", "item", "log", "engineering", saved["id"], "--stdin", content="新进程追加验证")
        envelope = call("wg.py", "workspace", "context", "engineering", "--item", "FIX-portable")
        assert envelope["ok"]
        context = envelope["result"]["output"]
        assert context["items"][0]["metadata"]["next_action"] == "验证恢复"
        assert "新进程追加验证" in context["items"][0]["body"]
        assert context["memories"][0]["metadata"]["title"] == "局部经验"
        assert call("workspace_state.py", "doctor", "engineering")["ok"]


def test_item_log_retains_state_and_appends_audited_key_history():
    with isolated_repo() as root:
        state.init_workspace("eng", profile="engineering")
        saved = state.add_item("eng", title="修复", item_type="repair", state="done", outcome="已实现，效果未测",
                               content="## 关键历史\n\n旧尝试\n\n## 验证\n\n原验证\n")
        state.item_log("eng", saved["id"], "复现不同入口；不撤销原结论。")
        result = state.workspace_context("eng", item=saved["id"])["items"][0]
        assert result["metadata"]["state"] == "done"
        assert result["metadata"]["outcome"] == "已实现，效果未测"
        assert result["body"].index("旧尝试") < result["body"].index("复现不同入口") < result["body"].index("## 验证")
        event = json.loads((root / "projects/eng/.workspace/events.jsonl").read_text().splitlines()[-1])
        assert event["action"] == "item.update"
        assert state.doctor("eng")["ok"]
        for bad_id in ("../secret", "ITEM-../../secret"):
            try:
                state.item_log("eng", bad_id, "禁止越界")
                raise AssertionError("unsafe id accepted")
            except state.WorkspaceError:
                pass


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"workspace state regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

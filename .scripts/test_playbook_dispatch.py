#!/usr/bin/env python3
"""playbook_dispatch.py regression tests."""
import contextlib
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("playbook_dispatch.py")
spec = importlib.util.spec_from_file_location("playbook_dispatch", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


@contextlib.contextmanager
def playbook_fixture(extra=""):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.md"
        path.write_text(
            "# fixture\n\n"
            "---\n"
            "## DSH 摄入\n\n"
            "**触发词**：「用 DSH 摄入 inbox」\n\n"
            "使用 API DSH。\n\n"
            "---\n"
            "## 摄入\n\n"
            "**触发词**：「摄入 inbox」\n\n"
            "使用 ingest_paper.py。\n\n"
            "---\n"
            "## 更新工程文档\n\n"
            "**触发词**：「更新工程文档」\n\n"
            "先判断改动类型。\n" + extra,
            encoding="utf-8",
        )
        original = module.PLAYBOOK
        try:
            module.PLAYBOOK = path
            yield
        finally:
            module.PLAYBOOK = original


def test_dispatch_hit_ingest():
    with playbook_fixture():
        result = module.dispatch("摄入 inbox")
    assert result is not None
    assert "ingest_paper.py" in result
    assert "触发词" in result
    assert "API DSH" not in result


def test_dispatch_prefers_explicit_specialized_trigger():
    with playbook_fixture():
        result = module.dispatch("请用 DSH 摄入 inbox")
    assert result is not None
    assert "API DSH" in result
    assert "使用 ingest_paper.py" not in result


def test_dispatch_rejects_overly_short_reverse_substring():
    with playbook_fixture():
        result = module.dispatch("摄入")
    assert result is None


def test_dispatch_hit_update_docs():
    with playbook_fixture():
        result = module.dispatch("更新工程文档")
    assert result is not None
    assert "改动类型" in result


def test_dispatch_no_match():
    with playbook_fixture():
        result = module.dispatch("查询论文")
    assert result is None


WORKSPACE_ENTRIES = """
---
## 班子会提案

**触发词**：「班子会提案」

projects/example-team 使用 workspace proposal；不触发会议纪要摄入。

---
## 系会汇报

**触发词**：「系会」「系会汇报」

projects/example-team/reports 使用 workspace_state.py item，区分待汇报与已汇报。
不使用班子会 `proposal` 状态；不触发会议纪要摄入。
"""


def test_workspace_proposal_dispatch_with_portable_fixture():
    with playbook_fixture(WORKSPACE_ENTRIES):
        result = module.dispatch("班子会提案：新增实验室空间安排")
    assert result is not None
    assert "projects/example-team" in result
    assert "workspace proposal" in result
    assert "不触发会议纪要摄入" in result


def test_workspace_report_aliases_with_portable_fixture():
    for query in ("系会", "系会汇报", "系会：新增科研宣传汇报", "系会汇报：标记已汇报"):
        with playbook_fixture(WORKSPACE_ENTRIES):
            result = module.dispatch(query)
        assert result is not None
        assert result.startswith("## 系会汇报\n")
        assert "projects/example-team/reports" in result
        assert "待汇报" in result and "已汇报" in result
        assert "workspace_state.py item" in result
        assert "不使用班子会 `proposal` 状态" in result
        assert "不触发会议纪要摄入" in result
        assert "## 班子会提案" not in result


def test_real_playbook_dispatches_ingest_backend_model_aliases():
    if not module.PLAYBOOK.is_file():
        print("SKIP local ingest aliases: private playbook index is not published")
        return
    for query in (
        "将ingest的后端模型改为agent",
        "把 ingest 后端模型切换为 api",
        "将摄入的后端模型设为 agent",
        "修改摄入后端",
    ):
        result = module.dispatch(query)
        assert result is not None
        assert result.startswith("## 修改摄入后端\n")
        assert "普通运行参数设置，不是建设任务" in result
        assert "`.env` 的 `INGEST_BACKEND`" in result


def test_real_playbook_dispatches_query_backend_model_aliases():
    if not module.PLAYBOOK.is_file():
        print("SKIP local query aliases: private playbook index is not published")
        return
    for query in (
        "query backend是否也做了同样的处理",
        "把 query 后端模型切换为 agent",
        "将查询的后端模型设为 api",
        "修改查询后端",
    ):
        result = module.dispatch(query)
        assert result is not None
        assert result.startswith("## 修改查询后端\n")
        assert "普通运行参数设置，不是建设任务" in result
        assert "`.env` 的 `QUERY_BACKEND`" in result
        assert "private 查询" in result


def test_dispatch_short_query():
    with playbook_fixture():
        result = module.dispatch("x")
    assert result is None


def test_real_oral_entry_links_specialized_fast_playbook():
    queries = ("/oral help", "/oral start 矩阵乘积态理解程度", "继续口试")
    if not module.PLAYBOOK.is_file():
        print("SKIP local oral aliases; still validate the public playbook and route")
        queries = ()
    for query in queries:
        result = module.dispatch(query)
        assert result is not None
        assert result.startswith("## 智能口试指令\n")
        assert "operations/ORAL_EXAM_AGENT.md" in result
        assert "oral receive" in result
        assert "最终回复" in result
        assert len(result) < 1000
    playbook = (module.REPO / "operations/ORAL_EXAM_AGENT.md").read_text(encoding="utf-8")
    for required in ("教授", "物理图像", "--displayed-message-id", "task_error", "task.commands.commit", "最终回复（final）"):
        assert required in playbook
    routed = subprocess.run(
        [sys.executable, ".scripts/route.py", "--task", "oral_exam"],
        cwd=module.REPO, capture_output=True, text=True, check=True)
    assert "智能口试快路径" in routed.stdout
    assert "原子确认" in routed.stdout
    assert "最终回复（final）" in routed.stdout


def test_list_entries():
    import io

    buf = io.StringIO()
    with playbook_fixture(), contextlib.redirect_stdout(buf):
        module.list_entries()
    out = buf.getvalue()
    assert "摄入" in out
    assert "更新工程文档" in out


def main():
    test_dispatch_hit_ingest()
    test_dispatch_prefers_explicit_specialized_trigger()
    test_dispatch_rejects_overly_short_reverse_substring()
    test_dispatch_hit_update_docs()
    test_dispatch_no_match()
    test_workspace_proposal_dispatch_with_portable_fixture()
    test_workspace_report_aliases_with_portable_fixture()
    test_real_playbook_dispatches_ingest_backend_model_aliases()
    test_real_playbook_dispatches_query_backend_model_aliases()
    test_dispatch_short_query()
    test_real_oral_entry_links_specialized_fast_playbook()
    test_list_entries()
    print("playbook dispatch regression: PASS")


if __name__ == "__main__":
    main()

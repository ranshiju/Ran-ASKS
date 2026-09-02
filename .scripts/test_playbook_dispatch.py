#!/usr/bin/env python3
"""playbook_dispatch.py regression tests."""
import contextlib
import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("playbook_dispatch.py")
spec = importlib.util.spec_from_file_location("playbook_dispatch", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


@contextlib.contextmanager
def playbook_fixture():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "index.md"
        path.write_text(
            "# fixture\n\n"
            "---\n"
            "## 摄入\n\n"
            "**触发词**：「摄入 inbox」\n\n"
            "使用 ingest_paper.py。\n\n"
            "---\n"
            "## 更新工程文档\n\n"
            "**触发词**：「更新工程文档」\n\n"
            "先判断改动类型。\n",
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


def test_dispatch_hit_update_docs():
    with playbook_fixture():
        result = module.dispatch("更新工程文档")
    assert result is not None
    assert "改动类型" in result


def test_dispatch_no_match():
    with playbook_fixture():
        result = module.dispatch("查询论文")
    assert result is None


def test_dispatch_short_query():
    with playbook_fixture():
        result = module.dispatch("x")
    assert result is None


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
    test_dispatch_hit_update_docs()
    test_dispatch_no_match()
    test_dispatch_short_query()
    test_list_entries()
    print("playbook dispatch regression: PASS")


if __name__ == "__main__":
    main()

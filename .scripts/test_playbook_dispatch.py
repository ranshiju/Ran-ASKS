#!/usr/bin/env python3
"""playbook_dispatch.py regression tests."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).with_name("playbook_dispatch.py")
spec = importlib.util.spec_from_file_location("playbook_dispatch", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_dispatch_hit_ingest():
    result = module.dispatch("摄入 inbox")
    assert result is not None
    assert "ingest_paper.py" in result
    assert "触发词" in result


def test_dispatch_hit_update_docs():
    result = module.dispatch("更新工程文档")
    assert result is not None
    assert "改动类型" in result


def test_dispatch_no_match():
    result = module.dispatch("查询论文")
    assert result is None


def test_dispatch_short_query():
    result = module.dispatch("x")
    assert result is None


def test_list_entries():
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
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

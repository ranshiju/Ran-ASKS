#!/usr/bin/env python3
"""Paper and role-neutral People skeleton contract regression."""
import importlib.util
import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).with_name("wiki_skeleton.py")
spec = importlib.util.spec_from_file_location("wiki_skeleton", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_temporary_raw_writes_final_source_path():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        raw = repo / "temp/inbox-extract/demo/paper.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("# A Temporary Paper\n\nAda Lovelace and Grace Hopper\n\nAbstract text.", encoding="utf-8")
        old_repo = module.REPO
        module.REPO = repo
        try:
            output = io.StringIO()
            with redirect_stdout(output):
                module.gen_skeleton(
                    "academic/wiki/papers/demo", None, str(raw),
                    "raw/references/demo/paper.md",
                )
        finally:
            module.REPO = old_repo
        text = output.getvalue()
        assert 'title: "A Temporary Paper"' in text
        assert "raw/references/demo/paper.md" in text
        assert "temp/inbox-extract" not in text
        assert "## 研究方向定位" in text


def test_people_skeleton_has_role_neutral_locatable_portrait():
    output = io.StringIO()
    with redirect_stdout(output):
        module.gen_skeleton("academic/wiki/authors/demo", None)
    text = output.getvalue()
    assert "type: people" in text
    assert "## 人物画像" in text
    assert "行政人员" in text and "学生" in text and "精确 Raw locator" in text


def main():
    test_temporary_raw_writes_final_source_path()
    test_people_skeleton_has_role_neutral_locatable_portrait()
    print("wiki skeleton regression: PASS")


if __name__ == "__main__":
    main()

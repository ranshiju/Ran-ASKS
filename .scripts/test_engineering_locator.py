#!/usr/bin/env python3
"""engineering_locator.py precise-read regression tests."""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path

import engineering_locator as el


@contextmanager
def fixture_repo():
    old_repo = el.REPO
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "repo"
        root.mkdir()
        el.REPO = root
        try:
            yield root
        finally:
            el.REPO = old_repo


def write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_markdown_hierarchy_duplicates_and_fences():
    with fixture_repo() as root:
        write(root, "operations/demo.md", (
            "# Demo\n\n"
            "## Build\nintro\n\n"
            "### Contract\nfirst\n\n"
            "```md\n### Fake\n```\n\n"
            "### Contract\nsecond\n\n"
            "## Other\nout\n"
        ))
        listed = el.list_engineering_locators("operations/demo.md")
        locators = [item["locator"] for item in listed["locators"]]
        assert "md:demo/build" in locators
        assert "md:demo/build/contract@1" in locators
        assert "md:demo/build/contract@2" in locators
        assert all("fake" not in item for item in locators)

        first = el.read_engineering_locator(
            "operations/demo.md#md:demo/build/contract@1"
        )
        second = el.read_engineering_locator(
            "operations/demo.md#md:demo/build/contract@2"
        )
        assert first["ok"] and "first" in first["content"]
        assert "second" not in first["content"]
        assert second["ok"] and "second" in second["content"]

        ambiguous = el.read_engineering_locator(
            "operations/demo.md#md:demo/build/contract"
        )
        assert not ambiguous["ok"]
        assert ambiguous["candidates"] == [
            "md:demo/build/contract@1", "md:demo/build/contract@2"
        ]


def test_markdown_miss_suggests_candidates_and_never_returns_whole_file():
    with fixture_repo() as root:
        write(root, "operations/demo.md", "# Demo\n\n## Contract\nbody\n")
        miss = el.read_engineering_locator("operations/demo.md#md:contrat")
        assert not miss["ok"]
        assert "md:demo/contract" in miss["candidates"]
        assert "content" not in miss

        bare = el.read_engineering_locator("operations/demo.md")
        assert not bare["ok"]
        assert "全文" in bare["error"]
        assert "content" not in bare


def test_yaml_mapping_sequence_and_pointer_escaping():
    with fixture_repo() as root:
        write(root, "operations/config/demo.yaml", (
            "root:\n"
            "  child: value\n"
            "  seq:\n"
            "    - one\n"
            "    - two\n"
            "  a/b: slash\n"
            "next: z\n"
        ))
        mapping = el.read_engineering_locator(
            "operations/config/demo.yaml#yaml:/root/seq"
        )
        assert mapping["ok"]
        assert mapping["start_line"] == 3 and mapping["end_line"] == 5
        assert mapping["content"] == "  seq:\n    - one\n    - two"

        item = el.read_engineering_locator(
            "operations/config/demo.yaml#yaml:/root/seq/1"
        )
        assert item["ok"] and item["content"] == "    - two"
        escaped = el.read_engineering_locator(
            "operations/config/demo.yaml#yaml:/root/a~1b"
        )
        assert escaped["ok"] and "a/b: slash" in escaped["content"]

        root_pointer = el.read_engineering_locator(
            "operations/config/demo.yaml#yaml:/"
        )
        assert not root_pointer["ok"] and "非根" in root_pointer["error"]

        filtered = el.list_engineering_locators(
            "operations/config/demo.yaml", prefix="yaml:/root/seq"
        )
        assert filtered["ok"] and filtered["count"] == 3
        assert filtered["filter_prefix"] == "yaml:/root/seq"
        assert [item["locator"] for item in filtered["locators"]] == [
            "yaml:/root/seq", "yaml:/root/seq/0", "yaml:/root/seq/1",
        ]
        assert all(item["kind"] == "yaml-node" for item in filtered["locators"])


def test_python_qualified_symbols_include_decorators():
    with fixture_repo() as root:
        write(root, ".scripts/demo.py", (
            "def top():\n"
            "    return 1\n\n"
            "class Worker:\n"
            "    @staticmethod\n"
            "    def run():\n"
            "        return 2\n\n"
            "def test_behavior():\n"
            "    assert top() == 1\n"
        ))
        method = el.read_engineering_locator(
            ".scripts/demo.py#py:Worker.run"
        )
        assert method["ok"]
        assert method["start_line"] == 5
        assert method["content"].startswith("    @staticmethod")
        assert "def run" in method["content"]

        test_symbol = el.read_engineering_locator(
            ".scripts/demo.py#py:test_behavior"
        )
        assert test_symbol["ok"] and "assert top()" in test_symbol["content"]
        listed = el.list_engineering_locators(".scripts/demo.py")
        assert {item["locator"] for item in listed["locators"]} >= {
            "py:top", "py:Worker", "py:Worker.run", "py:test_behavior"
        }
        filtered = el.list_engineering_locators(
            ".scripts/demo.py", prefix="py:Worker."
        )
        assert filtered["count"] == 1
        assert filtered["locators"][0]["locator"] == "py:Worker.run"


def test_line_range_and_over_budget_refusal():
    with fixture_repo() as root:
        write(root, "operations/plain.txt", "one\ntwo\nthree\nfour\n")
        excerpt = el.read_engineering_locator(
            "operations/plain.txt#L2-L3"
        )
        assert excerpt["ok"]
        assert excerpt["content"] == "two\nthree"
        assert excerpt["start_line"] == 2 and excerpt["end_line"] == 3
        assert len(excerpt["content_sha256"]) == 64

        oversized = el.read_engineering_locator(
            "operations/plain.txt#L1-L4", max_chars=3
        )
        assert not oversized["ok"]
        assert oversized["content_chars"] > 3
        assert "content" not in oversized


def test_path_and_file_boundaries():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        root = base / "repo"
        root.mkdir()
        old_repo = el.REPO
        el.REPO = root
        try:
            write(root, "raw/secret.md", "# Secret\n")
            write(root, "wiki/page.md", "# Page\n")
            write(root, "cross-domain/graph.db", "not a database")
            write(root, "operations/binary.txt", b"hello\x00world")
            outsider = base / "outside.md"
            outsider.write_text("# Outside\n", encoding="utf-8")

            for locator in (
                "raw/secret.md#L1-L1",
                "wiki/page.md#L1-L1",
                "cross-domain/graph.db#L1-L1",
                "operations/binary.txt#L1-L1",
                f"{outsider}#L1-L1",
            ):
                result = el.read_engineering_locator(locator)
                assert not result["ok"], locator
                assert "content" not in result
        finally:
            el.REPO = old_repo


def main():
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS: {len(tests)} engineering locator tests")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only inbox routing-plan regression."""
import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("inbox_plan.py")
spec = importlib.util.spec_from_file_location("inbox_plan", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_one_pdf_is_create_not_batch():
    with tempfile.TemporaryDirectory() as directory:
        inbox = Path(directory) / "inbox"
        inbox.mkdir()
        (inbox / "Paper Title.pdf").write_bytes(b"pdf")
        old_repo = module.REPO
        module.REPO = Path(directory)
        try:
            plan = module.build_plan(inbox)
        finally:
            module.REPO = old_repo
        assert plan["routing"] == {
            "batch_eligible": False,
            "batch_reason": "不满足至少三份同类 academic paper 文件；逐项使用 create。",
            "default_mode": "create",
        }
        assert plan["items"][0]["subproject"] == "academic"
        assert plan["items"][0]["proposed_id"] == "paper-title"


def test_three_pdfs_are_batch_eligible():
    with tempfile.TemporaryDirectory() as directory:
        inbox = Path(directory) / "inbox"
        inbox.mkdir()
        for number in range(3):
            (inbox / f"paper-{number}.pdf").write_bytes(b"pdf")
        old_repo = module.REPO
        module.REPO = Path(directory)
        try:
            plan = module.build_plan(inbox)
        finally:
            module.REPO = old_repo
        assert plan["routing"]["batch_eligible"]
        assert plan["routing"]["default_mode"] == "batch"


def test_empty_facts_pending_is_not_a_fact_ingest():
    with tempfile.TemporaryDirectory() as directory:
        inbox = Path(directory) / "inbox"
        inbox.mkdir()
        facts = inbox / "facts-pending.md"
        facts.write_text("# pending\n", encoding="utf-8")
        old_repo = module.REPO
        module.REPO = Path(directory)
        try:
            plan = module.build_plan(inbox)
        finally:
            module.REPO = old_repo
        assert plan["items"][0]["fact_entries"] == 0


def main():
    test_one_pdf_is_create_not_batch()
    test_three_pdfs_are_batch_eligible()
    test_empty_facts_pending_is_not_a_fact_ingest()
    print("inbox plan regression: PASS")


if __name__ == "__main__":
    main()

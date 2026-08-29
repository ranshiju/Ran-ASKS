#!/usr/bin/env python3
"""Inbox PDF transaction orchestration regression."""
import importlib.util
from argparse import Namespace
from pathlib import Path

SCRIPT = Path(__file__).with_name("inbox_ingest.py")
spec = importlib.util.spec_from_file_location("inbox_ingest", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_complete_batch_preserves_requested_order():
    completed = []
    original = module.cmd_complete
    module.cmd_complete = lambda args: completed.append(args.transaction_id)
    try:
        module.cmd_complete_batch(Namespace(transaction_id=["first", "second"], candidate=["known"]))
    finally:
        module.cmd_complete = original
    assert completed == ["first", "second"]


def main():
    test_complete_batch_preserves_requested_order()
    print("inbox ingest regression: PASS")


if __name__ == "__main__":
    main()

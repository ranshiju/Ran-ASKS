#!/usr/bin/env python3
"""Offline demo provenance and isolation regression."""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("foundation_demo", REPO / ".scripts" / "foundation_demo.py")
demo = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(demo)


def test_demo_returns_raw_bound_answer_without_production_writes() -> None:
    graph = REPO / "cross-domain" / "graph.db"
    before = graph.read_bytes() if graph.is_file() else None
    result = demo.run_demo(REPO)
    after = graph.read_bytes() if graph.is_file() else None
    assert before == after
    assert result["status"] == "completed"
    assert result["authority"] == "Raw source excerpt"
    assert result["network_calls"] == 0 and result["production_writes"] == 0
    assert "cooling fan cable had been disconnected" in result["evidence"]["text"]


def test_demo_rejects_fixture_drift() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "examples" / "demo"
        shutil.copytree(REPO / "examples" / "demo", target)
        (target / "sources" / "experiment-notes.md").write_text("tampered\n", encoding="utf-8")
        try:
            demo.load_scenario(root)
        except ValueError as error:
            assert "hash mismatch" in str(error)
        else:
            raise AssertionError("tampered demo source was accepted")


def test_demo_json_cli() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / ".scripts" / "foundation_demo.py"), "--json"],
        cwd=REPO, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout)["schema"] == "ran-asks-demo-result-v1"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

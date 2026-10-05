#!/usr/bin/env python3
"""Installed CLI workspace discovery and dispatch regression."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ran_asks import cli


def test_workspace_discovery_from_nested_path() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "workspace"
        nested = root / "a" / "b"
        (root / ".scripts").mkdir(parents=True)
        nested.mkdir(parents=True)
        (root / "AGENTS.md").write_text("test\n", encoding="utf-8")
        (root / ".scripts" / "wg.py").write_text("pass\n", encoding="utf-8")
        assert cli.find_workspace(cwd=nested) == root.resolve()


def test_cli_reports_canonical_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ran_asks", "--version"], cwd=REPO,
        text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == (REPO / "VERSION").read_text(encoding="utf-8").strip()


def test_foundation_dispatch_uses_current_interpreter() -> None:
    completed = subprocess.CompletedProcess([], 0)
    with patch.object(cli.subprocess, "run", return_value=completed) as mocked:
        assert cli.main(["--workspace", str(REPO), "doctor", "--json"]) == 0
    command = mocked.call_args.args[0]
    assert command[0] == sys.executable
    assert command[1].endswith(".scripts/workspace_doctor.py")
    assert command[-1] == "--json"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

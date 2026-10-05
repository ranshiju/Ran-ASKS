#!/usr/bin/env python3
"""Workspace doctor contract regression."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("workspace_doctor", REPO / ".scripts" / "workspace_doctor.py")
doctor = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(doctor)


def test_current_workspace_core_contract() -> None:
    report = doctor.diagnose(REPO, "core")
    assert report["schema"] == "ran-asks-doctor-v1"
    assert report["network_calls"] == 0
    assert report["summary"]["errors"] == 0, report


def test_missing_workspace_markers_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        report = doctor.diagnose(Path(temporary), "core")
    assert report["status"] == "error"
    assert any(check["name"] == "workspace" and check["status"] == "error" for check in report["checks"])


def test_json_cli_contract() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO / ".scripts" / "workspace_doctor.py"), "--json"],
        cwd=REPO, text=True, capture_output=True, check=True,
    )
    assert json.loads(result.stdout)["schema"] == "ran-asks-doctor-v1"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

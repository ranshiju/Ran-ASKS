#!/usr/bin/env python3
"""Offline weak-model governance exam regression."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "dsh/weak_model_governance_eval.py"
spec = importlib.util.spec_from_file_location("weak_model_governance_eval", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_exam_passes_without_network_or_persistent_writes() -> None:
    report = module.run_exam(module.load_exam())
    assert report["schema"] == "weak-model-governance-eval-v1"
    assert report["status"] == "passed"
    assert report["network_calls"] == 0
    assert report["persistent_writes"] == 0
    assert len(report["cases"]) >= 7
    assert all(case["status"] == "passed" for case in report["cases"])


def test_injection_is_blocked_and_fallback_trace_is_complete() -> None:
    report = module.run_exam(module.load_exam())
    cases = {case["id"]: case for case in report["cases"]}
    injection = cases["source-prompt-injection"]
    assert injection["disposition"] == "blocked"
    assert injection["observed"]["mutation_tools_registered"] == 0
    assert injection["observed"]["mutation_executions"] == 0
    fallback = cases["fallback-trace-completeness"]
    assert fallback["disposition"] == "accepted_with_trace"
    assert fallback["observed"]["models"] == ["synthetic-weak", "synthetic-fallback"]
    assert fallback["observed"]["statuses"] == ["validation_error", "ok"]


def test_cli_json_and_validate_modes() -> None:
    json_run = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"], cwd=REPO,
        text=True, capture_output=True, check=False,
    )
    assert json_run.returncode == 0, json_run.stderr
    assert json.loads(json_run.stdout)["status"] == "passed"
    validate = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate"], cwd=REPO,
        text=True, capture_output=True, check=False,
    )
    assert validate.returncode == 0, validate.stderr
    assert "PASS" in validate.stdout


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

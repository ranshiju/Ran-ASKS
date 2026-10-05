#!/usr/bin/env python3
"""Profiled test runner regression."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("test_suite_runner", REPO / ".scripts" / "test_suite.py")
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner)


def test_profiles_are_nonempty_and_release_extends_foundation() -> None:
    foundation = runner.profile_commands(REPO, "foundation")
    release = runner.profile_commands(REPO, "release")
    assert foundation
    assert set(foundation) < set(release)
    assert ".scripts/test_workspace_doctor.py" in foundation
    assert "dsh/test_weak_model_governance_eval.py" in foundation


def test_runner_propagates_failure() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "pass.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (root / "fail.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
        report = runner.run_specs(root, ["pass.py", "fail.py"], stream=False)
    assert report["status"] == "failed"
    assert report["summary"] == {"passed": 1, "failed": 1, "planned": 2}


def test_all_discovers_declared_regressions() -> None:
    discovered = runner.profile_commands(REPO, "all")
    assert ".scripts/test_test_suite.py" in discovered
    assert "dsh/test_dsh_harness.py" in discovered
    assert all("--render" not in command for command in discovered)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

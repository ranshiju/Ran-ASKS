#!/usr/bin/env python3
"""Run stable Ran-ASKS regression profiles with one command."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
FOUNDATION = [
    ".scripts/test_cli_package.py",
    ".scripts/test_workspace_doctor.py",
    ".scripts/test_foundation_demo.py",
    ".scripts/test_test_suite.py",
    ".scripts/test_security_policy.py",
    ".scripts/test_env_config.py",
    ".scripts/test_function_registry.py",
    ".scripts/test_engineering_graph.py",
    ".scripts/test_engineering_locator.py",
    ".scripts/test_wg.py",
    "dsh/test_weak_model_governance_eval.py",
]
RELEASE = FOUNDATION + [
    ".scripts/test_open_source_release.py",
    ".scripts/test_open_source_publish.py",
    ".scripts/test_paper_artifact.py",
    ".scripts/paper_artifact.py verify paper-artifacts/v0.2.0",
    "paper-artifacts/v0.2.1/verify.py",
]


def command_for(spec: str) -> list[str]:
    return [sys.executable, *spec.split()]


def discover_all(workspace: Path) -> list[str]:
    paths = sorted((workspace / ".scripts").glob("test_*.py"))
    paths += sorted((workspace / "dsh").glob("test_*.py"))
    skill = workspace / ".codex" / "skills" / "manuscript-diagnosis" / "scripts" / "test_diagnosis_preflight.py"
    if skill.is_file():
        paths.append(skill)
    return [path.relative_to(workspace).as_posix() for path in paths]


def profile_commands(workspace: Path, profile: str) -> list[str]:
    if profile == "foundation":
        return list(FOUNDATION)
    if profile == "release":
        return list(RELEASE)
    if profile == "all":
        return discover_all(workspace)
    raise ValueError(f"unknown profile: {profile}")


def run_specs(workspace: Path, specs: list[str], *, fail_fast: bool = False,
              stream: bool = True) -> dict:
    results = []
    started = time.monotonic()
    for spec in specs:
        item_started = time.monotonic()
        completed = subprocess.run(
            command_for(spec), cwd=workspace, text=True,
            capture_output=not stream,
        )
        result = {
            "command": spec,
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "duration_seconds": round(time.monotonic() - item_started, 3),
        }
        if not stream:
            result["stdout"] = completed.stdout[-4000:]
            result["stderr"] = completed.stderr[-4000:]
        results.append(result)
        if completed.returncode and fail_fast:
            break
    failed = sum(result["status"] == "failed" for result in results)
    return {
        "schema": "ran-asks-test-suite-v1",
        "status": "passed" if not failed and len(results) == len(specs) else "failed",
        "commands": results,
        "summary": {"passed": len(results) - failed, "failed": failed, "planned": len(specs)},
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("foundation", "release", "all"), nargs="?", default="foundation")
    parser.add_argument("--workspace", type=Path, default=REPO)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)
    specs = profile_commands(args.workspace, args.profile)
    if args.list:
        print("\n".join(specs))
        return 0
    report = run_specs(args.workspace, specs, fail_fast=args.fail_fast, stream=not args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"Ran-ASKS {args.profile}: {summary['passed']} passed, {summary['failed']} failed")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

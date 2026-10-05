#!/usr/bin/env python3
"""Read-only, offline readiness checks for a Ran-ASKS workspace."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
PROFILES = {
    "core": {"yaml": "PyYAML"},
    "release": {
        "yaml": "PyYAML", "numpy": "NumPy", "PIL": "Pillow",
        "fitz": "PyMuPDF", "pptx": "python-pptx",
    },
    "pdf": {"yaml": "PyYAML", "fitz": "PyMuPDF", "requests": "requests"},
    "office": {
        "yaml": "PyYAML", "PIL": "Pillow", "pptx": "python-pptx",
        "docx": "python-docx", "openpyxl": "openpyxl", "xlrd": "xlrd",
    },
    "analysis": {
        "yaml": "PyYAML", "numpy": "NumPy", "scipy": "SciPy",
        "tiktoken": "tiktoken", "rank_bm25": "rank-bm25",
    },
    "visual": {
        "yaml": "PyYAML", "numpy": "NumPy", "scipy": "SciPy", "PIL": "Pillow",
        "fitz": "PyMuPDF", "pptx": "python-pptx", "matplotlib": "matplotlib",
        "networkx": "networkx",
    },
}


def item(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def configured_keys(workspace: Path) -> set[str]:
    env_path = workspace / ".env"
    if not env_path.is_file() or env_path.is_symlink():
        return set()
    keys = set()
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            if key.strip() and value.strip():
                keys.add(key.strip())
    return keys


def diagnose(workspace: Path, profile: str = "core") -> dict:
    root = workspace.resolve()
    checks: list[dict[str, str]] = []
    if profile not in PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    markers = ["AGENTS.md", "VERSION", "pyproject.toml", "uv.lock", ".scripts/wg.py"]
    missing = [name for name in markers if not (root / name).is_file()]
    checks.append(item("workspace", "error" if missing else "pass",
                       "missing: " + ", ".join(missing) if missing else str(root)))

    version_path = root / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
    checks.append(item("version", "pass" if SEMVER.fullmatch(version) else "error",
                       version or "missing"))

    pyproject_path = root / "pyproject.toml"
    try:
        metadata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        dynamic = metadata.get("project", {}).get("dynamic", [])
        valid_metadata = metadata.get("project", {}).get("name") == "ran-asks" and "version" in dynamic
    except (OSError, tomllib.TOMLDecodeError):
        valid_metadata = False
    checks.append(item("project metadata", "pass" if valid_metadata else "error",
                       "dynamic VERSION" if valid_metadata else "invalid pyproject.toml"))

    missing_dependencies = [
        package for module, package in PROFILES[profile].items()
        if importlib.util.find_spec(module) is None
    ]
    checks.append(item(
        f"dependencies:{profile}", "error" if missing_dependencies else "pass",
        "missing: " + ", ".join(missing_dependencies) if missing_dependencies else "available",
    ))

    in_venv = bool(os.environ.get("VIRTUAL_ENV")) or sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    checks.append(item("virtual environment", "pass" if in_venv else "warn",
                       sys.prefix if in_venv else "not detected; use uv run or activate .venv"))

    env_keys = configured_keys(root)
    checks.append(item("model configuration", "pass" if env_keys else "warn",
                       f"{len(env_keys)} non-empty settings" if env_keys else "optional .env not configured"))

    public_db = root / "cross-domain" / "graph.db"
    private_db = root / "private" / "graph.db"
    isolated = not (public_db.exists() and private_db.exists() and public_db.resolve() == private_db.resolve())
    checks.append(item("graph isolation", "pass" if isolated else "error",
                       "public/private targets are distinct" if isolated else "targets resolve to the same file"))

    validator = root / ".scripts" / "engineering_graph.py"
    if validator.is_file() and importlib.util.find_spec("yaml") is not None:
        result = subprocess.run(
            [sys.executable, str(validator), "validate"], cwd=root,
            text=True, capture_output=True,
        )
        detail = (result.stdout or result.stderr).strip().splitlines()
        checks.append(item("engineering graph", "pass" if result.returncode == 0 else "error",
                           detail[-1] if detail else f"exit {result.returncode}"))
    else:
        checks.append(item("engineering graph", "error", "validator or PyYAML missing"))

    errors = sum(check["status"] == "error" for check in checks)
    warnings = sum(check["status"] == "warn" for check in checks)
    return {
        "schema": "ran-asks-doctor-v1",
        "status": "error" if errors else "ready_with_warnings" if warnings else "ready",
        "profile": profile,
        "version": version,
        "network_calls": 0,
        "checks": checks,
        "summary": {"passed": len(checks) - errors - warnings, "warnings": warnings, "errors": errors},
    }


def render_text(report: dict) -> str:
    marks = {"pass": "OK", "warn": "WARN", "error": "ERROR"}
    lines = [f"Ran-ASKS {report['version'] or 'unknown'}", f"Profile: {report['profile']}", ""]
    for check in report["checks"]:
        lines.append(f"[{marks[check['status']]}] {check['name']}: {check['detail']}")
    summary = report["summary"]
    lines.extend(["", f"Status: {report['status']} ({summary['warnings']} warnings, {summary['errors']} errors)"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=REPO)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="core")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = diagnose(args.workspace, args.profile)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_text(report))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

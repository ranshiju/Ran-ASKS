"""Workspace-aware installed CLI; domain logic remains in repository scripts."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


FOUNDATION_COMMANDS = {
    "doctor": "workspace_doctor.py",
    "demo": "foundation_demo.py",
    "test": "test_suite.py",
}


def is_workspace(path: Path) -> bool:
    return (path / "AGENTS.md").is_file() and (path / ".scripts" / "wg.py").is_file()


def find_workspace(explicit: str | Path | None = None, *, cwd: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    elif os.environ.get("RAN_ASKS_WORKSPACE"):
        candidates.append(Path(os.environ["RAN_ASKS_WORKSPACE"]).expanduser())
    else:
        start = (cwd or Path.cwd()).resolve()
        candidates.extend([start, *start.parents])
        candidates.append(Path(__file__).resolve().parent.parent)
    for candidate in candidates:
        resolved = candidate.resolve()
        if is_workspace(resolved):
            return resolved
    target = str(candidates[0]) if candidates else str(cwd or Path.cwd())
    raise ValueError(f"not a Ran-ASKS workspace: {target}")


def parse_global_args(argv: list[str]) -> tuple[str | None, list[str]]:
    remaining = list(argv)
    workspace = None
    if remaining[:1] == ["--workspace"]:
        if len(remaining) < 2:
            raise ValueError("--workspace requires a path")
        workspace = remaining[1]
        remaining = remaining[2:]
    return workspace, remaining


def help_text() -> str:
    return """usage: ran-asks [--workspace PATH] <command> [args]

Foundation commands:
  doctor   Check the local environment without network calls or writes
  demo     Run the offline source -> Wiki -> Graph -> Raw evidence tour
  test     Run a named deterministic regression profile

All other commands are forwarded to the workspace's .scripts/wg.py.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        explicit, args = parse_global_args(args)
        workspace = find_workspace(explicit)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if args in (["--version"], ["version"]):
        print((workspace / "VERSION").read_text(encoding="utf-8").strip())
        return 0
    if not args or args[0] in {"-h", "--help"}:
        print(help_text())
        return 0
    command = args[0]
    script_name = FOUNDATION_COMMANDS.get(command, "wg.py")
    forwarded = args[1:] if command in FOUNDATION_COMMANDS else args
    completed = subprocess.run(
        [sys.executable, str(workspace / ".scripts" / script_name), *forwarded],
        cwd=workspace,
    )
    return completed.returncode

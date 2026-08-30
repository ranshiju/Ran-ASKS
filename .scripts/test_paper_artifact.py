#!/usr/bin/env python3
"""Regression checks for the sanitized paper artifact."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts/paper_artifact.py"
ARTIFACT = REPO / "paper-artifacts/v0.2.0"


def run(path: Path, expected: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "verify", str(path)],
        cwd=REPO,
        text=True,
        capture_output=True,
    )
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def main() -> None:
    verified = run(ARTIFACT)
    assert "no Raw files" in verified.stdout
    assert len(list((ARTIFACT / "wiki/papers").rglob("*.md"))) == 56
    assert len(list((ARTIFACT / "wiki/hubs").rglob("*.md"))) == 18
    assert not any(path.suffix == ".db" for path in ARTIFACT.rglob("*"))
    compatibility = json.loads(
        (ARTIFACT / "config/code-compatibility.json").read_text(encoding="utf-8")
    )
    assert compatibility["files_recorded_by_frozen_run"] == 16
    assert compatibility["exact_matches"] == 15
    assert compatibility["post_run_drift"] == 1
    assert {
        path
        for path, item in compatibility["files"].items()
        if not item["exact_match"]
    } == {".scripts/graph_ingest.py"}
    trajectory = (ARTIFACT / "metrics/trajectory.csv").read_text(encoding="utf-8")
    assert "RESEARCH ARTICLE" not in trajectory
    assert all(b"\r\n" not in path.read_bytes() for path in ARTIFACT.rglob("*.csv"))

    with tempfile.TemporaryDirectory() as temporary:
        copied = Path(temporary) / "artifact"
        shutil.copytree(ARTIFACT, copied)
        with (copied / "metrics/trajectory.csv").open("a", encoding="utf-8") as handle:
            handle.write("tampered\n")
        failed = run(copied, expected=1)
        assert "checksum mismatch" in failed.stderr

    print("paper artifact regression: PASS")


if __name__ == "__main__":
    main()

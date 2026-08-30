#!/usr/bin/env python3
"""Regression checks for manifest-governed public release construction."""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts/open_source_release.py"


def run(*args: str, expected: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=REPO, text=True,
                            capture_output=True)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def main() -> None:
    expected_version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "release"
        run("build", str(destination))
        verified = run("verify", str(destination))
        assert "Public release verified" in verified.stdout
        assert (destination / "README.md").is_file()
        assert (destination / "README.zh-CN.md").is_file()
        assert (destination / "THIRD_PARTY_NOTICES.md").is_file()
        assert (destination / "VERSION").is_file()
        assert (destination / "VERSION").read_text(encoding="utf-8").strip() == expected_version
        assert f"> Current release: v{expected_version}" in (destination / "README.md").read_text(encoding="utf-8")
        assert f"> 当前发布版本: v{expected_version}" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert "[简体中文](README.zh-CN.md)" in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert "[English](README.md)" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert "[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)" in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert (destination / ".scripts/route.py").is_file()
        assert (destination / ".scripts/e1_experiment.py").is_file()
        assert (destination / ".scripts/test_e1_experiment.py").is_file()
        assert (destination / "operations/QUERY.md").is_file()
        assert (destination / "agents/writer/AGENT.md").is_file()
        assert (destination / "dsh/agent_loop.py").is_file()
        assert (destination / "paper-artifacts/v0.2.0/metadata.json").is_file()
        assert (destination / "paper-artifacts/v0.2.0/wiki/papers").is_dir()
        assert (destination / "paper-artifacts/v0.2.0/wiki/hubs").is_dir()
        assert (destination / "paper-artifacts/v0.2.0/graph/final-graph.jsonl").is_file()
        artifact_check = subprocess.run(
            [
                sys.executable,
                ".scripts/paper_artifact.py",
                "verify",
                "paper-artifacts/v0.2.0",
            ],
            cwd=destination, text=True, capture_output=True,
        )
        assert artifact_check.returncode == 0, artifact_check.stdout + artifact_check.stderr
        assert not any((destination / "paper-artifacts").rglob("*.db"))
        assert not any((destination / "paper-artifacts").rglob("*.pdf"))
        assert (destination / "academic/raw/.gitkeep").is_file()
        assert (destination / "academic/frontier/.gitkeep").is_file()
        assert not (destination / "academic/raw/example.pdf").exists()
        assert not any(destination.rglob(".DS_Store"))
        assert not (destination / ".scripts/speech_entity_index.json").exists()
        assert (destination / "operations/engineering/open-source-assets/README.md").is_file()
        graph_text = (destination / "operations/engineering/graph.yaml").read_text(encoding="utf-8")
        assert "frontier_store:" in graph_text
        assert "e1_experiment_workspace:" not in graph_text
        assert "e1_experiment_plan:" not in graph_text
        assert "e1_analysis:" not in graph_text
        assert "path: projects/ForBetterScience" not in graph_text
        graph_check = subprocess.run(
            [sys.executable, ".scripts/engineering_graph.py", "validate"],
            cwd=destination, text=True, capture_output=True,
        )
        assert graph_check.returncode == 0, graph_check.stdout + graph_check.stderr

        (destination / "academic/raw/leak.txt").write_text("private", encoding="utf-8")
        failed = run("verify", str(destination), expected=1)
        assert "unexpected file" in failed.stderr

    print("open source release regression: PASS")


if __name__ == "__main__":
    main()

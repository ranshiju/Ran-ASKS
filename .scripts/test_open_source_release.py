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
        subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
        verified = run("verify", str(destination))
        assert "Public release verified" in verified.stdout
        assert (destination / "README.md").is_file()
        assert (destination / "README.zh-CN.md").is_file()
        assert (destination / "CHANGELOG.md").is_file()
        changelog = (destination / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{expected_version}]" in changelog
        assert "[CHANGELOG.md](CHANGELOG.md)" in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert "[CHANGELOG.md](CHANGELOG.md)" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert "MINERU_API_TOKEN=" in (destination / ".env.example").read_text(encoding="utf-8")
        assert "MINERU_API_TOKEN" in (destination / "README.md").read_text(encoding="utf-8")
        assert "MINERU_API_TOKEN" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        introduction_relative = "docs/introduction/ASKS-Chinese-Introduction-2026-09-03.pdf"
        introduction = destination / introduction_relative
        assert introduction.is_file()
        assert introduction.stat().st_size > 100_000
        assert b"GPL Ghostscript" in introduction.read_bytes()
        assert (destination / "docs/introduction/README.md").is_file()
        assert introduction_relative in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert introduction_relative in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert "sjran@cnu.edu.cn" in (destination / "README.md").read_text(encoding="utf-8")
        assert "sjran@cnu.edu.cn" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")

        original_introduction = introduction.read_bytes()
        introduction.write_bytes(original_introduction.replace(b"GPL Ghostscript", b"Word PDF export"))
        unnormalized = run("verify", str(destination), expected=1)
        assert "public introduction PDF is not browser-normalized" in unnormalized.stderr
        introduction.write_bytes(original_introduction)
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
        assert not (destination / ".scripts/e1_experiment.py").exists()
        assert not (destination / ".scripts/e1_order_robustness.py").exists()
        assert not (destination / ".scripts/test_e1_experiment.py").exists()
        assert not (destination / ".scripts/test_e1_order_robustness.py").exists()
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
        assert (destination / "paper-artifacts/v0.2.1/metadata.json").is_file()
        audit_artifact_check = subprocess.run(
            [sys.executable, "verify.py"],
            cwd=destination / "paper-artifacts/v0.2.1",
            text=True,
            capture_output=True,
        )
        assert audit_artifact_check.returncode == 0, (
            audit_artifact_check.stdout + audit_artifact_check.stderr
        )
        assert not any((destination / "paper-artifacts").rglob("*.db"))
        artifact_pdfs = {
            path.relative_to(destination).as_posix()
            for path in (destination / "paper-artifacts").rglob("*.pdf")
        }
        assert artifact_pdfs <= {
            "paper-artifacts/v0.2.1/figures/figure5-external-audit-evidence.pdf"
        }
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
        assert "path: projects/ASKS" not in graph_text
        assert "path: projects/ForBetterScience" not in graph_text
        graph_check = subprocess.run(
            [sys.executable, ".scripts/engineering_graph.py", "validate"],
            cwd=destination, text=True, capture_output=True,
        )
        assert graph_check.returncode == 0, graph_check.stdout + graph_check.stderr

        subprocess.run(["git", "config", "user.name", "release-test"], cwd=destination, check=True)
        subprocess.run(
            ["git", "config", "user.email", "release-test@example.invalid"],
            cwd=destination,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=destination, check=True)

        changed_path = destination / "AGENTS.md"
        original_changed = changed_path.read_bytes()
        changed_path.write_bytes(original_changed + b"\n")
        unsynchronized = run("verify", str(destination), expected=1)
        assert "public update must synchronize" in unsynchronized.stderr

        synchronized_paths = [
            destination / "README.md",
            destination / "README.zh-CN.md",
            destination / "docs/introduction/README.md",
            introduction,
        ]
        synchronized_originals = {path: path.read_bytes() for path in synchronized_paths}
        for path in synchronized_paths:
            path.write_bytes(path.read_bytes() + b"\n")
        synchronized = run("verify", str(destination))
        assert "Public release verified" in synchronized.stdout
        changed_path.write_bytes(original_changed)
        for path, content in synchronized_originals.items():
            path.write_bytes(content)

        gitignore = destination / ".gitignore"
        original_gitignore = gitignore.read_text(encoding="utf-8")
        gitignore.write_text(original_gitignore + "\npaper-artifacts/**\n", encoding="utf-8")
        ignored = run("verify", str(destination), expected=1)
        assert "release file ignored by destination .gitignore" in ignored.stderr
        gitignore.write_text(original_gitignore, encoding="utf-8")

        changelog_path = destination / "CHANGELOG.md"
        original_changelog = changelog_path.read_text(encoding="utf-8")
        changelog_path.write_text(
            original_changelog.replace(f"## [{expected_version}]", "## [9.9.9]", 1),
            encoding="utf-8",
        )
        stale_changelog = run("verify", str(destination), expected=1)
        assert "CHANGELOG.md missing current release heading" in stale_changelog.stderr
        changelog_path.write_text(original_changelog, encoding="utf-8")

        (destination / "academic/raw/leak.txt").write_text("private", encoding="utf-8")
        failed = run("verify", str(destination), expected=1)
        assert "unexpected file" in failed.stderr

    print("open source release regression: PASS")


if __name__ == "__main__":
    main()

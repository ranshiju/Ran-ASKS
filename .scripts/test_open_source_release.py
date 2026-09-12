#!/usr/bin/env python3
"""Regression checks for manifest-governed public release construction."""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import open_source_release as release


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".scripts/open_source_release.py"


def run(*args: str, expected: int = 0) -> subprocess.CompletedProcess:
    result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=REPO, text=True,
                            capture_output=True)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def check_version_preparation() -> None:
    assert release.next_version("0.4.9", "patch") == "0.4.10"
    assert release.next_version("0.4.9", "minor") == "0.5.0"
    assert release.next_version("0.4.9", "major") == "1.0.0"
    for version, level in [("01.2.3", "patch"), ("1.2", "minor"), ("1.2.3", "auto")]:
        try:
            release.next_version(version, level)
        except ValueError:
            pass
        else:
            raise AssertionError((version, level))
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary)
        version_path = repository / "VERSION"
        changelog = repository / "public-CHANGELOG.md"
        mirror = repository / "CHANGELOG.md"
        original = "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- Image OCR.\n\n## [0.4.0] - 2026-09-04\n\n- Earlier work.\n"
        version_path.write_text("0.4.0\n", encoding="utf-8")
        changelog.write_text(original, encoding="utf-8")
        mirror.write_text("old mirror\n", encoding="utf-8")
        manifest = {"public_assets": {"CHANGELOG.md": changelog.name}}
        with patch.object(release, "REPO", repository), patch.object(release, "VERSION_PATH", version_path), patch.object(release, "load_manifest", return_value=manifest):
            arguments = {"level": "minor", "reason": "Compatible image ingestion capability", "from_version": "0.4.0", "release_date": "2026-09-08"}
            snapshot = {item: item.read_bytes() for item in (version_path, changelog, mirror)}
            planned = release.prepare_version(**arguments)
            assert planned["status"] == "planned" and planned["to"] == "0.5.0"
            assert all(item.read_bytes() == content for item, content in snapshot.items())
            for changes in [{"reason": " "}, {"reason": "two\nlines"}, {"release_date": "invalid"}]:
                try:
                    release.prepare_version(**(arguments | changes), apply=True)
                except ValueError:
                    pass
                else:
                    raise AssertionError(changes)
                assert all(item.read_bytes() == content for item, content in snapshot.items())
            for invalid in ["# Changelog\n", "## [Unreleased]\n\n### Added\n", original + "\n## [0.5.0]\n"]:
                changelog.write_text(invalid, encoding="utf-8")
                try:
                    release.prepare_version(**arguments, apply=True)
                except ValueError:
                    pass
                else:
                    raise AssertionError(invalid)
                assert version_path.read_bytes() == snapshot[version_path]
                assert mirror.read_bytes() == snapshot[mirror]
            changelog.write_text(original, encoding="utf-8")
            original_write = Path.write_text

            def fail_mirror(target, *args, **kwargs):
                if target == mirror:
                    raise OSError("injected write failure")
                return original_write(target, *args, **kwargs)

            with patch.object(Path, "write_text", fail_mirror):
                try:
                    release.prepare_version(**arguments, apply=True)
                except OSError:
                    pass
                else:
                    raise AssertionError("expected rollback")
            assert all(item.read_bytes() == content for item, content in snapshot.items())
            applied = release.prepare_version(**arguments, apply=True)
            assert applied["status"] == "applied"
            assert version_path.read_text() == "0.5.0\n"
            assert mirror.read_bytes() == changelog.read_bytes()
            updated = changelog.read_text()
            assert "## [Unreleased]\n\n## [0.5.0] - 2026-09-08" in updated
            assert "- MINOR: Compatible image ingestion capability" in updated
            assert "- Image OCR." in updated and "- Earlier work." in updated
            try:
                release.prepare_version(**arguments, apply=True)
            except ValueError as error:
                assert "do not bump twice" in str(error)
            else:
                raise AssertionError("duplicate bump accepted")


def check_version_progression() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary)

        def git(*arguments):
            return subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True, text=True)

        assert release.git_publication_diff(repository) == (set(), None)
        git("init", "-q")
        git("config", "user.name", "release-test")
        git("config", "user.email", "release-test@example.invalid")
        assert release.git_publication_diff(repository) == (set(), None)
        version_path = repository / "VERSION"
        version_path.write_text("0.4.0\n")
        git("add", "-A")
        git("commit", "-qm", "baseline")
        assert release.git_publication_diff(repository) == (set(), None)
        (repository / "README.md").write_text("New capability\n")
        changes, base = release.git_publication_diff(repository)
        assert changes == {"README.md"} and base == "HEAD"
        for candidate in ["0.4.0", "0.3.9"]:
            assert release.version_progression_errors(repository, candidate, changes, base)
        assert not release.version_progression_errors(repository, "0.5.0", changes, base)
        version_path.write_text("0.5.0\n")
        git("add", "-A")
        changes, base = release.git_publication_diff(repository)
        assert changes == {"VERSION", "README.md"} and base == "HEAD"
        git("commit", "-qm", "new capability")
        changes, base = release.git_publication_diff(repository)
        assert changes == {"VERSION", "README.md"} and base == "HEAD^"
        assert not release.version_progression_errors(repository, "0.5.0", changes, base)
        for candidate in ["0.4.0", "0.3.9"]:
            assert release.version_progression_errors(repository, candidate, changes, base)
        (repository / "README.md").write_text("Same-version edit\n")
        git("add", "-A")
        git("commit", "-qm", "unversioned update")
        changes, base = release.git_publication_diff(repository)
        assert release.version_progression_errors(repository, "0.5.0", changes, base)


def check_documentation_omissions() -> None:
    manifest = release.load_manifest()
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary)
        for path, term in [
            ("README.md", "稿件诊断"),
            ("README.zh-CN.md", "manuscript-diagnosis"),
            ("CHANGELOG.md", "Manuscript Diagnosis"),
            ("docs/introduction/example.md", "MANUSCRIPT ASSESSMENT"),
            ("operations/engineering/open-source-assets/README.md", "manuscript_diagnosis"),
        ]:
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(term, encoding="utf-8")
            errors = release.documentation_omission_errors(destination, manifest, {path})
            assert len(errors) == 1 and path in errors[0], errors
        skill_path = ".codex/skills/manuscript-diagnosis/SKILL.md"
        target = destination / skill_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("稿件诊断 manuscript-diagnosis", encoding="utf-8")
        assert not release.documentation_omission_errors(destination, manifest, {skill_path})


def main() -> None:
    check_documentation_omissions()
    check_version_preparation()
    check_version_progression()
    expected_version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "release"
        run("build", str(destination))
        subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
        verified = run("verify", str(destination))
        assert "Public release verified" in verified.stdout
        skill = destination / ".codex/skills/manuscript-diagnosis"
        assert {p.relative_to(skill).as_posix() for p in skill.rglob("*") if p.is_file()} == {
            "SKILL.md", "agents/openai.yaml", "scripts/diagnosis_preflight.py",
            "scripts/test_diagnosis_preflight.py",
        }
        assert not (destination / "projects").exists()
        private_ignored = subprocess.run(
            ["git", "check-ignore", "projects/local-input/manuscript.pdf"],
            cwd=destination, text=True, capture_output=True,
        )
        assert private_ignored.returncode == 0, private_ignored.stderr
        skill_check = subprocess.run(
            [sys.executable, "-B", str(skill / "scripts/test_diagnosis_preflight.py")],
            cwd=destination, text=True, capture_output=True,
        )
        assert skill_check.returncode == 0, skill_check.stdout + skill_check.stderr
        readme_path = destination / "README.md"
        original_readme = readme_path.read_text(encoding="utf-8")
        readme_path.write_text(original_readme + "\n稿件诊断\n", encoding="utf-8")
        omitted = run("verify", str(destination), expected=1)
        assert "unlisted feature in public documentation: README.md" in omitted.stderr
        readme_path.write_text(original_readme, encoding="utf-8")
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
        introduction_markdown_relative = (
            "docs/introduction/ASKS-Chinese-Introduction-2026-09-03.md"
        )
        introduction_pdf_relative = (
            "docs/introduction/ASKS-Chinese-Introduction-2026-09-03.pdf"
        )
        introduction_markdown = destination / introduction_markdown_relative
        introduction_pdf = destination / introduction_pdf_relative
        assert introduction_markdown.is_file()
        assert introduction_markdown.stat().st_size > 10_000
        introduction_text = introduction_markdown.read_text(encoding="utf-8")
        assert introduction_text.startswith("# ASKS 智能知识系统\n")
        assert "assets/knowledge-compilation-zh.png" in introduction_text
        assert "assets/author-research-portrait.png" in introduction_text
        assert introduction_pdf_relative.rsplit("/", 1)[-1] in introduction_text
        assert "/tmp/" not in introduction_text
        assert (destination / "docs/introduction/assets/knowledge-compilation-zh.png").is_file()
        assert (destination / "docs/introduction/assets/author-research-portrait.png").is_file()
        assert introduction_pdf.is_file()
        assert introduction_pdf.stat().st_size > 100_000
        assert b"GPL Ghostscript" in introduction_pdf.read_bytes()
        assert (destination / "docs/introduction/README.md").is_file()
        assert introduction_markdown_relative in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert introduction_markdown_relative in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert introduction_pdf_relative in (
            destination / "README.md"
        ).read_text(encoding="utf-8")
        assert introduction_pdf_relative in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")
        assert "sjran@cnu.edu.cn" in (destination / "README.md").read_text(encoding="utf-8")
        assert "sjran@cnu.edu.cn" in (
            destination / "README.zh-CN.md"
        ).read_text(encoding="utf-8")

        original_introduction = introduction_pdf.read_bytes()
        introduction_pdf.write_bytes(
            original_introduction.replace(b"GPL Ghostscript", b"Word PDF export")
        )
        unnormalized = run("verify", str(destination), expected=1)
        assert "public introduction PDF is not normalized" in unnormalized.stderr
        introduction_pdf.write_bytes(original_introduction)
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
        assert "manuscript_diagnosis_skill:" in graph_text
        assert "manuscript_diagnosis_preflight:" in graph_text
        assert "manuscript_diagnosis_style:" not in graph_text
        assert "manuscript_diagnosis_style_library:" not in graph_text
        assert "0730 PRL" not in graph_text
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
        (destination / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=destination, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=destination, check=True)
        (destination / "VERSION").write_text(expected_version + "\n", encoding="utf-8")

        changed_path = destination / "AGENTS.md"
        original_changed = changed_path.read_bytes()
        changed_path.write_bytes(original_changed + b"\n")
        unsynchronized = run("verify", str(destination), expected=1)
        assert "public update must synchronize" in unsynchronized.stderr

        synchronized_paths = [
            destination / "README.md",
            destination / "README.zh-CN.md",
            destination / "docs/introduction/README.md",
            introduction_markdown,
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

        changelog_path.write_text(
            original_changelog.replace("## [Unreleased]", "## [Unreleased]\n\n## [9.9.9]", 1),
            encoding="utf-8",
        )
        misordered = run("verify", str(destination), expected=1)
        assert "newest release heading must match VERSION" in misordered.stderr
        changelog_path.write_text(original_changelog, encoding="utf-8")

        (destination / "academic/raw/leak.txt").write_text("private", encoding="utf-8")
        failed = run("verify", str(destination), expected=1)
        assert "unexpected file" in failed.stderr

    print("open source release regression: PASS")


if __name__ == "__main__":
    main()

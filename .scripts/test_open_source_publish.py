#!/usr/bin/env python3
"""Regression checks for committed-snapshot public publication orchestration."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import open_source_publish as publish
import open_source_release as release


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=repository, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def init_repository(path: Path) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    git(path, "config", "user.name", "release-test")
    git(path, "config", "user.email", "release-test@example.invalid")
    (path / "public.txt").write_text("public\n", encoding="utf-8")
    (path / "private.txt").write_text("private\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "baseline")
    return git(path, "rev-parse", "HEAD")


def test_dirty_release_input_boundary() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repository = Path(temporary) / "source"
        commit = init_repository(repository)
        (repository / "private.txt").write_text("private work\n", encoding="utf-8")
        dirty = publish.dirty_release_inputs(repository, commit, {"public.txt"})
        assert dirty == set(), dirty
        (repository / "public.txt").write_text("public work\n", encoding="utf-8")
        dirty = publish.dirty_release_inputs(repository, commit, {"public.txt"})
        assert dirty == {"public.txt"}, dirty
        (repository / "new-public.txt").write_text("new\n", encoding="utf-8")
        dirty = publish.dirty_release_inputs(
            repository, commit, {"public.txt", "new-public.txt"}
        )
        assert dirty == {"public.txt", "new-public.txt"}, dirty


def test_destination_remote_and_divergence_guards() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        repository = root / "public"
        init_repository(repository)
        git(repository, "branch", "-M", "main")
        git(repository, "remote", "add", "origin", str(remote))
        git(repository, "push", "-qu", "origin", "main")
        publish.assert_destination(
            repository, remote="origin", branch="main",
            expected_remote_url=str(remote), fetch=True,
        )
        try:
            publish.assert_destination(
                repository, remote="origin", branch="main",
                expected_remote_url="wrong", fetch=False,
            )
        except publish.PublishError as error:
            assert "remote URL mismatch" in str(error)
        else:
            raise AssertionError("wrong remote URL accepted")
        (repository / "public.txt").write_text("dirty\n", encoding="utf-8")
        try:
            publish.assert_destination(
                repository, remote="origin", branch="main",
                expected_remote_url=str(remote), fetch=False,
            )
        except publish.PublishError as error:
            assert "must be clean" in str(error)
        else:
            raise AssertionError("dirty public worktree accepted")


def test_transactional_install_rolls_back() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination = root / "destination"
        staging = root / "staging"
        (destination / ".git").mkdir(parents=True)
        (destination / "old.txt").write_text("old\n", encoding="utf-8")
        staging.mkdir()
        (staging / "a.txt").write_text("a\n", encoding="utf-8")
        (staging / "b.txt").write_text("b\n", encoding="utf-8")
        original_replace = release.os.replace

        def fail_second_stage_move(source, target):
            if Path(source) == staging / "b.txt":
                raise OSError("injected install failure")
            return original_replace(source, target)

        with patch.object(release.os, "replace", fail_second_stage_move):
            try:
                release.install_release_tree(staging, destination)
            except OSError as error:
                assert "injected" in str(error)
            else:
                raise AssertionError("injected install failure was ignored")
        assert (destination / ".git").is_dir()
        assert (destination / "old.txt").read_text(encoding="utf-8") == "old\n"
        assert not (destination / "a.txt").exists()
        assert not (destination / "b.txt").exists()


def test_tree_diff_paths() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        left, right = root / "left", root / "right"
        left.mkdir(); right.mkdir()
        (left / "same.txt").write_text("same\n", encoding="utf-8")
        (right / "same.txt").write_text("same\n", encoding="utf-8")
        (left / "changed.txt").write_text("old\n", encoding="utf-8")
        (right / "changed.txt").write_text("new\n", encoding="utf-8")
        (right / "added.txt").write_text("added\n", encoding="utf-8")
        assert publish.tree_diff_paths(left, right) == {"changed.txt", "added.txt"}


def main() -> None:
    tests = [
        test_dirty_release_input_boundary,
        test_destination_remote_and_divergence_guards,
        test_transactional_install_rolls_back,
        test_tree_diff_paths,
    ]
    for test in tests:
        test()
        print(f"  {test.__name__}: PASS")
    print(f"open source publish regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

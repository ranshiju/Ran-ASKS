#!/usr/bin/env python3
"""Publish a committed WikiGraph source snapshot into the public Git repository."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import open_source_release as release


REPO = Path(__file__).resolve().parent.parent
MANIFEST_RELATIVE = "operations/engineering/open-source-manifest.yaml"


class PublishError(ValueError):
    pass


def run(command: list[str], *, cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise PublishError(f"command failed ({' '.join(command)}): {detail}")
    return result


def git(repository: Path, *arguments: str) -> str:
    return run(["git", *arguments], cwd=repository).stdout.strip()


def source_identity(repository: Path, source_ref: str) -> tuple[str, str]:
    commit = git(repository, "rev-parse", f"{source_ref}^{{commit}}")
    tree = git(repository, "rev-parse", f"{commit}^{{tree}}")
    if not release.GIT_OBJECT_PATTERN.fullmatch(commit) or not release.GIT_OBJECT_PATTERN.fullmatch(tree):
        raise PublishError(f"invalid source ref: {source_ref}")
    return commit, tree


def release_input_paths() -> set[str]:
    manifest = release.load_manifest()
    paths = release.release_preflight(manifest)
    paths.update(manifest.get("public_assets", {}).values())
    paths.update({"VERSION", MANIFEST_RELATIVE, ".scripts/open_source_release.py",
                  ".scripts/open_source_publish.py"})
    return paths


def dirty_release_inputs(repository: Path, source_commit: str, selected: set[str]) -> set[str]:
    tracked = set(git(repository, "diff", "--name-only", source_commit, "--").splitlines())
    untracked = set(git(repository, "ls-files", "--others", "--exclude-standard").splitlines())
    return {path for path in tracked | untracked if path in selected}


def assert_committed_source(repository: Path, source_ref: str) -> tuple[str, str]:
    commit, tree = source_identity(repository, source_ref)
    dirty = dirty_release_inputs(repository, commit, release_input_paths())
    if dirty:
        raise PublishError(
            "manifest-selected source files differ from the requested commit: "
            + ", ".join(sorted(dirty))
        )
    return commit, tree


def assert_destination(repository: Path, *, remote: str, branch: str,
                       expected_remote_url: str, fetch: bool = True) -> None:
    if not (repository / ".git").exists():
        raise PublishError(f"destination is not a Git worktree: {repository}")
    current_branch = git(repository, "branch", "--show-current")
    if current_branch != branch:
        raise PublishError(f"destination branch is {current_branch!r}, expected {branch!r}")
    if git(repository, "status", "--porcelain"):
        raise PublishError("destination worktree must be clean before publication")
    actual_remote_url = git(repository, "remote", "get-url", remote)
    if actual_remote_url != expected_remote_url:
        raise PublishError(
            f"remote URL mismatch for {remote}: {actual_remote_url!r} != {expected_remote_url!r}"
        )
    if fetch:
        run(["git", "fetch", remote, branch], cwd=repository)
    remote_ref = f"refs/remotes/{remote}/{branch}"
    git(repository, "rev-parse", "--verify", remote_ref)
    counts = git(repository, "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}").split()
    if counts != ["0", "0"]:
        raise PublishError(f"destination and {remote}/{branch} diverged: ahead={counts[0]} behind={counts[1]}")


def tree_diff_paths(left: Path, right: Path) -> set[str]:
    left_files = release.actual_files(left)
    right_files = release.actual_files(right)
    changed = left_files ^ right_files
    for relative in left_files & right_files:
        if release.sha256_file(left / relative) != release.sha256_file(right / relative):
            changed.add(relative)
    return changed - {release.MARKER}


def assert_release_diff(destination: Path, staging: Path) -> set[str]:
    changed = tree_diff_paths(destination, staging)
    if not changed:
        raise PublishError("generated public tree has no changes to publish")
    old_version = (destination / "VERSION").read_text(encoding="utf-8").strip()
    new_version = (staging / "VERSION").read_text(encoding="utf-8").strip()
    if not release.VERSION_PATTERN.fullmatch(old_version) or not release.VERSION_PATTERN.fullmatch(new_version):
        raise PublishError("source or destination VERSION is invalid")
    if tuple(map(int, new_version.split("."))) <= tuple(map(int, old_version.split("."))):
        raise PublishError(f"public VERSION must advance beyond {old_version}, found {new_version}")
    required = release.documentation_sync_paths(release.load_manifest())
    missing = required - changed
    if missing:
        raise PublishError("reader documentation is not synchronized: " + ", ".join(sorted(missing)))
    ignored = release.git_ignored_files(destination, release.actual_files(staging))
    if ignored:
        raise PublishError("destination .gitignore hides release files: " + ", ".join(ignored))
    return changed


def verify_public_tree(destination: Path, *, include_regressions: bool) -> None:
    commands = [
        [sys.executable, ".scripts/open_source_release.py", "verify", "."],
        [sys.executable, ".scripts/engineering_graph.py", "validate"],
    ]
    if include_regressions:
        commands.extend([
            [sys.executable, ".scripts/test_open_source_release.py"],
            [sys.executable, ".scripts/test_open_source_publish.py"],
            [sys.executable, ".scripts/paper_artifact.py", "verify", "paper-artifacts/v0.2.0"],
            [sys.executable, "paper-artifacts/v0.2.1/verify.py"],
        ])
    for command in commands:
        run(command, cwd=destination, capture=False)


def build_committed_release(source_repository: Path, source_commit: str, source_tree: str,
                            staging: Path) -> None:
    worktree = staging.parent / "source"
    run(["git", "worktree", "add", "--detach", str(worktree), source_commit], cwd=source_repository)
    try:
        run([
            sys.executable, str(worktree / ".scripts/open_source_release.py"), "build", str(staging),
            "--source-commit", source_commit, "--source-tree", source_tree,
            "--source-mode", "committed",
        ], cwd=worktree, capture=False)
        verify_public_tree(staging, include_regressions=False)
        provenance = json.loads((staging / release.PROVENANCE_PATH).read_text(encoding="utf-8"))
        if provenance.get("source", {}).get("commit") != source_commit:
            raise PublishError("generated release provenance does not match the source commit")
    finally:
        run(["git", "worktree", "remove", "--force", str(worktree)], cwd=source_repository)


def remote_commit(repository: Path, remote: str, branch: str) -> str:
    output = git(repository, "ls-remote", remote, f"refs/heads/{branch}")
    fields = output.split()
    if len(fields) != 2:
        raise PublishError(f"unable to resolve remote branch {remote}/{branch}")
    return fields[0]


def publish(*, destination: Path, source_ref: str, remote: str, branch: str,
            expected_remote_url: str, message: str, push: bool, dry_run: bool) -> dict:
    destination = destination.resolve()
    source_commit, source_tree = assert_committed_source(REPO, source_ref)
    assert_destination(
        destination, remote=remote, branch=branch,
        expected_remote_url=expected_remote_url, fetch=True,
    )
    with tempfile.TemporaryDirectory(prefix="wikigraph-publish-") as temporary:
        staging = Path(temporary) / "release"
        build_committed_release(REPO, source_commit, source_tree, staging)
        changed = assert_release_diff(destination, staging)
        plan = {
            "status": "planned" if dry_run else "committed",
            "source_commit": source_commit,
            "source_tree": source_tree,
            "destination": str(destination),
            "remote": remote,
            "branch": branch,
            "version": (staging / "VERSION").read_text(encoding="utf-8").strip(),
            "changed_paths": sorted(changed),
        }
        if dry_run:
            return plan
        release.install_release_tree(staging, destination)
    verify_public_tree(destination, include_regressions=True)
    run(["git", "add", "-A"], cwd=destination)
    if not git(destination, "diff", "--cached", "--name-only"):
        raise PublishError("publication produced no staged changes")
    run(["git", "diff", "--cached", "--check"], cwd=destination)
    verify_public_tree(destination, include_regressions=False)
    run(["git", "commit", "-m", message], cwd=destination, capture=False)
    verify_public_tree(destination, include_regressions=False)
    commit = git(destination, "rev-parse", "HEAD")
    plan["commit"] = commit
    if push:
        run(["git", "push", remote, f"HEAD:{branch}"], cwd=destination, capture=False)
        confirmed = remote_commit(destination, remote, branch)
        if confirmed != commit:
            raise PublishError(f"remote confirmation mismatch: local={commit} remote={confirmed}")
        plan["status"] = "pushed"
        plan["remote_commit"] = confirmed
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source-ref", default="HEAD")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--expected-remote-url", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = publish(
            destination=args.destination, source_ref=args.source_ref, remote=args.remote,
            branch=args.branch, expected_remote_url=args.expected_remote_url,
            message=args.message, push=args.push, dry_run=args.dry_run,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

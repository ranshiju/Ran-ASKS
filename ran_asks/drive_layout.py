"""Install machine-local compatibility links for visible Drive workspace data."""
import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT_NAMES = {".scripts": "system-tools", ".project": "project-config",
              ".codex": "agent-resources", ".github": "github-config",
              ".gitignore": "git-ignore.txt", ".gitmodules": "git-modules.txt",
              ".env.example": "environment-example.txt"}
GIT_CONFIG_FILES = {".gitignore", ".gitmodules"}
PROJECT_NAMES = {".workspace": "workspace-state", ".research-memory": "research-memory",
                 ".organization": "project-organization", ".path-migrations.json": "path-migrations.json",
                 ".mineru": "mineru-artifacts"}


def pairs(root):
    for old, new in ROOT_NAMES.items():
        yield root / old, root / new
    yield root / "inbox/.source-retention", root / "inbox/source-retention"
    for base, dirs, files in os.walk(root / "projects", followlinks=False):
        names = set(dirs + files)
        dirs[:] = [d for d in dirs if d not in {"raw", ".git", "node_modules", "__pycache__"}
                   and d not in PROJECT_NAMES and d not in PROJECT_NAMES.values()
                   and not (Path(base) / d).is_symlink()]
        for old, new in PROJECT_NAMES.items():
            if old in names or new in names:
                yield Path(base) / old, Path(base) / new


def install(root, migrate):
    if not (root / "AGENTS.md").is_file() or not (root / "ran_asks").is_dir():
        raise ValueError("Run from the workspace root")
    actions = []
    for old, new in pairs(root):
        if new.is_symlink():
            raise ValueError(f"Unexpected visible symlink: {new}")
        if old.name in GIT_CONFIG_FILES and old.is_file() and new.is_file() and not old.is_symlink():
            if old.read_bytes() != new.read_bytes():
                raise ValueError(f"Reconcile the local Git control file with {new}")
            continue
        if old.is_symlink():
            if os.readlink(old) != new.name or not new.exists():
                raise ValueError(f"Unexpected compatibility link: {old}")
            if old.name in GIT_CONFIG_FILES:
                actions.append((old, new, False))
        elif old.exists():
            if new.exists() or not migrate:
                raise ValueError(f"Existing local data requires reconciliation: {old}")
            actions.append((old, new, True))
        elif new.exists():
            actions.append((old, new, False))
    for old, new, move in actions:
        if move:
            old.rename(new)
        if old.name in GIT_CONFIG_FILES:
            if old.is_symlink():
                old.unlink()
            os.link(new, old)
        else:
            old.symlink_to(new.name, target_is_directory=new.is_dir())
    return len(actions)


def changed_pair(relative):
    """Find a Drive mapping enclosing a path changed by the public upstream."""
    parts = relative.parts
    if parts[0] in ROOT_NAMES.values():
        raise ValueError(f"Upstream also uses the visible Drive path {relative}; reconcile the layout first")
    if parts[0] in ROOT_NAMES:
        return Path(parts[0]), Path(ROOT_NAMES[parts[0]])
    if parts[:2] == ("inbox", ".source-retention"):
        return Path(*parts[:2]), Path("inbox/source-retention")
    if parts[:2] == ("inbox", "source-retention"):
        raise ValueError(f"Upstream also uses the visible Drive path {relative}")
    if parts[0] == "projects":
        for i, part in enumerate(parts[1:], start=1):
            if part in PROJECT_NAMES.values():
                raise ValueError(f"Upstream also uses the visible Drive path {relative}")
            if part in PROJECT_NAMES:
                old = Path(*parts[:i+1])
                return old, old.with_name(PROJECT_NAMES[part])
    return None


def check_pair(old, new):
    if old.parent.resolve() != old.parent or new.is_symlink():
        raise ValueError(f"Unexpected symlink in Drive paths {old} / {new}")
    if old.is_symlink():
        if os.readlink(old) != new.name or not new.exists():
            raise ValueError(f"Unexpected compatibility link: {old}")
    elif old.exists() and new.exists():
        if old.name in GIT_CONFIG_FILES and old.is_file() and new.is_file():
            if old.read_bytes() == new.read_bytes():
                return
        raise ValueError(f"Reconcile both copies before updating: {old} / {new}")


def restore_pair(old, new):
    check_pair(old, new)
    if old.is_symlink():
        return
    if old.exists() and not new.exists():
        old.rename(new)
    if new.exists() and not old.exists():
        if old.name in GIT_CONFIG_FILES:
            os.link(new, old)
        else:
            old.symlink_to(new.name, target_is_directory=new.is_dir())


def upgrade(root):
    """Fast-forward the configured upstream using Git's tracked paths during checkout."""
    import fcntl  # Direct Drive is supported on macOS.

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root)

    root = root.resolve()
    if any(os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE")):
        raise ValueError("Run the update without Git path overrides")
    if Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("Run from the root of the Git workspace")
    git_dir = Path(os.fsdecode(git("rev-parse", "--absolute-git-dir")).strip())
    with (git_dir / "ran-asks-drive-upgrade.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (root / "system-tools").is_dir():
            raise ValueError("A migrated Drive workspace is required. For interrupted migration run --migrate")
        for old_name, new_name in ROOT_NAMES.items():
            check_pair(root / old_name, root / new_name)
        # Resolve tracking before fetching, so an unconfigured receiver never pulls
        # from an arbitrary remote. Drive distributes the owner's completed upgrade.
        git("rev-parse", "--verify", "@{upstream}")
        git("fetch")
        head = git("rev-parse", "HEAD").decode().strip()
        target = git("rev-parse", "@{upstream}").decode().strip()
        if head == target:
            return head
        git("merge-base", "--is-ancestor", head, target)
        changed = git("diff", "--no-renames", "--name-only", "-z", head, target)
        affected = set()
        for name in changed.split(b"\0"):
            if name and (pair := changed_pair(Path(os.fsdecode(name)))):
                affected.add((root / pair[0], root / pair[1]))
        # Validate the whole batch before moving even one directory.
        for old, new in affected:
            check_pair(old, new)
        try:
            for old, new in sorted(affected):
                if new.exists():
                    if old.exists() or old.is_symlink():
                        old.unlink()
                    new.rename(old)
            git("merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", target)
        finally:
            for old, new in sorted(affected):
                restore_pair(old, new)
        return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--migrate", action="store_true", help="Move legacy paths in a quiescent local copy")
    action.add_argument("--upgrade", action="store_true", help="Fast-forward a direct Drive workspace from its upstream")
    args = parser.parse_args()
    try:
        if args.upgrade:
            print(f"Drive workspace up to date at {upgrade(Path.cwd())}")
        else:
            print(f"Installed {install(Path.cwd(), args.migrate)} local entries")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))

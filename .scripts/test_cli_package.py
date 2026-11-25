#!/usr/bin/env python3
"""Installed CLI workspace discovery and dispatch regression."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from ran_asks import cli


def test_workspace_discovery_from_nested_path() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "workspace"
        nested = root / "a" / "b"
        (root / ".scripts").mkdir(parents=True)
        nested.mkdir(parents=True)
        (root / "AGENTS.md").write_text("test\n", encoding="utf-8")
        (root / ".scripts" / "wg.py").write_text("pass\n", encoding="utf-8")
        assert cli.find_workspace(cwd=nested) == root.resolve()


def test_cli_reports_canonical_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ran_asks", "--version"], cwd=REPO,
        text=True, capture_output=True, check=True,
    )
    assert result.stdout.strip() == (REPO / "VERSION").read_text(encoding="utf-8").strip()


def test_foundation_dispatch_uses_current_interpreter() -> None:
    completed = subprocess.CompletedProcess([], 0)
    with patch.object(cli.subprocess, "run", return_value=completed) as mocked:
        assert cli.main(["--workspace", str(REPO), "doctor", "--json"]) == 0
    command = mocked.call_args.args[0]
    assert command[0] == sys.executable
    assert Path(command[1]) == cli.tools_directory(REPO) / "workspace_doctor.py"
    assert command[-1] == "--json"


def test_storage_setup_reuses_confirmed_paths():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        drive = base / "Drive"
        old = drive / "old"
        old.mkdir(parents=True)
        work = base / "work"
        work.mkdir()
        config = base / "config"
        env = {**os.environ, "XDG_CONFIG_HOME": str(config)}

        def setup(cwd, *args, check=True):
            return subprocess.run(
                [sys.executable, str(REPO / ".scripts/workspace_setup.py"), *args],
                cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=check,
            )

        assert json.loads(setup(old, "--status").stdout)["missing"] == "mode"
        assert not config.exists()
        assert json.loads(setup(old, "--mode", "synology").stdout)["missing"] == "drive_root"
        assert json.loads(setup(old, "--drive-root", str(drive)).stdout)["missing"] == "work_dir"
        alias = base / "alias"
        alias.symlink_to(old)
        assert setup(old, "--work-dir", str(alias), check=False).returncode != 0
        assert json.loads(setup(old, "--work-dir", str(work)).stdout)["status"] == "open_configured_workspace"
        assert json.loads(setup(work).stdout)["status"] == "configured"
        assert json.loads(setup(work, "--status").stdout)["transfer_dir"] == str(drive / "WikiGraph-Checkpoints")
        assert not (drive / "WikiGraph-Checkpoints").exists()
        assert json.loads(setup(work, "--mode", "local").stdout)["mode"] == "local"
        assert json.loads(setup(old, "--status").stdout)["work_dir"] == str(work)
        assert json.loads(setup(work).stdout) == {
            "status": "configured", "missing": None, "mode": "local", "work_dir": str(work),
        }
        fresh = base / "fresh"
        fresh.mkdir()
        pending = json.loads(setup(fresh, "--drive-root", str(drive)).stdout)
        assert pending["missing"] == "work_dir"
        assert json.loads(setup(fresh, "--work-dir", str(fresh)).stdout)["status"] == "configured"
        default = json.loads(setup(work, "--drive-root", str(drive)).stdout)
        assert default["work_dir"] == str(work) and default["mode"] == "synology"


def test_scratch_placement_uses_workspace_preference():
    sys.path.insert(0, str(REPO / ".scripts"))
    from workspace_setup import scratch_root
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp).resolve()
        work = base / "Drive/work"
        (work / "system-tools").mkdir(parents=True)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "config")}):
            config = base / "config/ran-asks/storage.json"
            config.parent.mkdir(parents=True)
            for mode in ["local", "synology", "synology-direct"]:
                entry = {"mode": mode, "work_dir": str(work)}
                if mode == "synology-direct":
                    entry["scratch_dir"] = str(base / "scratch")
                config.write_text(json.dumps({str(work): entry}))
                expected = base / "scratch" if mode == "synology-direct" else work / "temp"
                assert scratch_root(work, "review") == expected / "review"


def test_direct_drive_rebuild_links_and_external_git():
    from ran_asks.drive_layout import install
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        drive = base / "Drive"
        root = drive / "work"
        (root / "ran_asks").mkdir(parents=True)
        (root / "AGENTS.md").touch()
        (root / ".scripts").mkdir()
        (root / ".scripts" / "wg.py").write_text("old")
        project = root / "projects" / "project"
        (project / ".workspace").mkdir(parents=True)
        (project / ".workspace" / "record").write_text("known")
        assert install(root, True) == 2
        (root / ".scripts").unlink()
        (project / ".workspace").unlink()
        assert install(root, False) == 2 and install(root, False) == 0
        assert (project / ".workspace/record").read_text() == "known"
        env = dict(os.environ, HOME=str(base), XDG_CONFIG_HOME=str(base / "settings"))
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "AGENTS.md"], cwd=root, check=True)
        command = [sys.executable, str(REPO / ".scripts/workspace_setup.py")]
        options = ["--mode", "synology-direct", "--drive-root", str(drive), "--externalize-git"]
        result = subprocess.check_output(command + options, env=env, cwd=root, text=True)
        profile = json.loads(result)
        assert (root / ".git").is_file() and not Path(profile["git_dir"]).is_relative_to(drive)
        assert subprocess.check_output(["git", "ls-files"], cwd=root).strip() == b"AGENTS.md"
        subprocess.run(command + ["--externalize-git"], env=env, cwd=root, check=True, capture_output=True)
        assert subprocess.run(command + ["--work-dir", str(base)], env=env, cwd=root,
                              capture_output=True).returncode != 0


def test_public_clone_upgrade_in_drive_and_local_layouts():
    import shutil
    from ran_asks.drive_layout import install

    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        origin = base / "origin"
        origin.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   PYTHONDONTWRITEBYTECODE="1", XDG_CONFIG_HOME=str(base / "config"))

        def run(root, *args, check=True):
            return subprocess.run(args, cwd=root, env=env, check=check, text=True,
                                  capture_output=True)

        run(origin, "git", "init", "-q")
        run(origin, "git", "config", "user.name", "Test")
        run(origin, "git", "config", "user.email", "test@example.invalid")
        shutil.copytree(REPO / "ran_asks", origin / "ran_asks", ignore=shutil.ignore_patterns("__pycache__"))
        scripts = origin / ".scripts"
        scripts.mkdir()
        shutil.copy(REPO / ".scripts/platform_compat.py", scripts)
        (scripts / "wg.py").write_text("print('first')")
        (scripts / "remove.py").touch()
        (origin / ".gitignore").write_text("*.cache\n")
        (origin / "AGENTS.md").touch()
        run(origin, "git", "add", ".")
        run(origin, "git", "commit", "-qm", "base")
        direct, local = base / "Drive/work", base / "local"
        for checkout in (direct, local):
            run(base, "git", "clone", "-q", str(origin), str(checkout))
        install(direct, True)
        notes = direct / "projects/personal/notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("personal")
        (scripts / "wg.py").write_text("print('updated')")
        (scripts / "remove.py").unlink()
        (scripts / "added.py").touch()
        (origin / ".gitignore").write_text("*.cache\n*.tmp\n")
        run(origin, "git", "add", "-A")
        run(origin, "git", "commit", "-qm", "upgrade scripts")
        upgrade = [sys.executable, "-m", "ran_asks.drive_layout", "--upgrade"]
        active = direct / "system-tools/wg.py"
        active.write_text("print('user edit')")
        old = run(direct, "git", "rev-parse", "HEAD").stdout
        assert run(direct, *upgrade, check=False).returncode
        assert active.read_text() == "print('user edit')" and (direct / ".scripts").is_symlink()
        assert run(direct, "git", "rev-parse", "HEAD").stdout == old
        active.write_text("print('first')")
        run(direct, *upgrade)
        assert (direct / ".scripts").samefile(direct / "system-tools")
        assert (direct / ".gitignore").samefile(direct / "git-ignore.txt")
        assert (direct / "git-ignore.txt").read_text() == (origin / ".gitignore").read_text()
        assert not (direct / "system-tools/remove.py").exists()
        assert (direct / "system-tools/added.py").is_file()
        assert notes.read_text() == "personal"
        run(direct, *upgrade)
        run(local, "git", "pull", "--ff-only")
        assert not (local / "system-tools").exists()
        for checkout in (local, direct):
            assert run(checkout, sys.executable, "-m", "ran_asks", "probe").stdout.strip() == "updated"

        # A legacy plain pull can strand two independent script directories.
        (direct / ".scripts").unlink()
        (direct / ".scripts").mkdir()
        (direct / ".scripts/wg.py").write_text("print('other copy')")
        assert run(direct, *upgrade, check=False).returncode
        assert run(direct, sys.executable, "-m", "ran_asks", "probe", check=False).returncode
        assert active.read_text() == "print('updated')"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")

"""First-use storage preferences. All paths belong to the current machine."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from platform_compat import fcntl


def directory(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("请提供绝对目录路径")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"路径不是目录：{path}")
    return path


def overlaps(left, right):
    return left.is_relative_to(right) or right.is_relative_to(left)


def local_store(work):
    key = hashlib.sha256(os.fsencode(work.resolve())).hexdigest()[:16]
    return (Path.home() / "WikiGraphData" / "workspaces" / key).resolve()


def config_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ran-asks/storage.json"


def workspace_profile(records, root):
    entry = records.get(str(root.resolve()), {})
    return records[entry["configured_at"]] if "configured_at" in entry else entry


def scratch_root(work, category):
    config = config_path()
    records = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    profile = workspace_profile(records, work)
    if (profile.get("mode") == "synology-direct" and profile.get("scratch_dir")
            and profile.get("work_dir") == str(work.resolve())):
        return Path(profile["scratch_dir"]) / category
    return work / "temp" / category


def configure(root, args, saved):
    mode = args.mode or saved.get("mode")
    if (args.drive_root or args.work_dir) and mode in (None, "local") and not args.mode:
        mode = "synology"
    if args.status and any((args.mode, args.drive_root, args.work_dir, args.externalize_git)):
        raise ValueError("--status 只用于查看设置")
    ask = not args.status and sys.stdin.isatty()
    if not mode and ask:
        answer = input("工作区位置：单目录 [回车/local]、云盘内直同步 [direct]、本地工作区＋云盘交接包 [pack]：").strip().lower()
        choices = {"": "local", "local": "local", "n": "local", "no": "local", "否": "local",
                   "direct": "synology-direct", "y": "synology-direct", "yes": "synology-direct",
                   "是": "synology-direct", "pack": "synology"}
        if answer not in choices:
            raise ValueError("请选择 local、direct 或 pack")
        mode = choices[answer]
    if not mode:
        return {}, "mode"

    if mode == "local":
        if args.drive_root or args.work_dir:
            raise ValueError("本机模式使用当前工作区，只需 --mode local")
        work = root if args.mode else saved.get("work_dir", root)
        return {"mode": mode, "work_dir": str(work)}, None

    profile = {"mode": mode}
    drive_value = args.drive_root or saved.get("drive_root")
    if not drive_value and ask:
        drive_value = input("云盘在本机的同步根目录：").strip()
    if not drive_value:
        if args.work_dir or saved.get("work_dir"):
            profile["work_dir"] = str(directory(args.work_dir or saved["work_dir"]))
        return profile, "drive_root"
    drive = directory(drive_value)
    if not drive.is_dir():
        raise ValueError(f"请检查群晖同步根目录是否可用：{drive}")
    if mode == "synology-direct":
        profile["drive_root"] = str(drive)
        value = args.work_dir or saved.get("work_dir")
        if not value or not directory(value).is_relative_to(drive):
            if args.work_dir:
                raise ValueError("直同步工作区须在群晖同步根内")
            value = root if root.is_relative_to(drive) and root != drive else None
        if not value and ask:
            value = input("群晖内工作目录：").strip()
        if not value:
            return profile, "work_dir"
        work = directory(value)
        if work == drive or not work.is_relative_to(drive):
            raise ValueError("请指定群晖根内独立的工作目录")
        local = local_store(work)
        if overlaps(local, drive):
            raise ValueError("本机 Git 与临时目录必须在同步范围外")
        profile.update(work_dir=str(work), git_dir=str(local / "git"),
                       scratch_dir=str(local / "scratch"))
        return profile, None
    transfer = drive / "WikiGraph-Checkpoints"
    if transfer.resolve() != transfer or (transfer.exists() and not transfer.is_dir()):
        raise ValueError(f"交接目录须为同步根下的真实目录：{transfer}")
    profile.update(drive_root=str(drive), transfer_dir=str(transfer))
    work_value = args.work_dir or saved.get("work_dir")
    if ask and not args.work_dir and (not work_value or overlaps(directory(work_value), drive)):
        if overlaps(root, drive):
            work_value = input("请指定群晖同步范围外的工作目录：").strip()
        else:
            work_value = input(f"群晖外工作目录（回车使用 {root}）：").strip() or root
    if not work_value:
        return profile, "work_dir"
    work = directory(work_value)
    if overlaps(work, drive):
        if args.work_dir:
            raise ValueError("工作目录必须位于群晖同步范围外，两个目录也不能相互包含")
        return profile, "work_dir"
    profile["work_dir"] = str(work)
    return profile, None


def externalize_git(root, profile):
    """Bind only this workspace; Git preserves the independent index and refs."""
    if profile.get("mode") != "synology-direct" or str(root) != profile.get("work_dir"):
        raise ValueError("请在已登记的群晖直同步工作区中运行 Git 外置")
    if any(os.environ.get(k) for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR",
                                       "GIT_OBJECT_DIRECTORY", "GIT_INDEX_FILE")):
        raise ValueError("请先取消当前 shell 的 Git 路径覆盖")
    entry = root / ".git"
    target = Path(profile["git_dir"])
    if entry.is_symlink() or target.resolve() != target:
        raise ValueError("Git 路径包含符号链接")
    if entry.is_file():
        bound = subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=root, text=True).strip()
        if Path(bound) != target:
            raise ValueError("已有 Git 指针属于其他仓库或本机位置")
        return
    if target.exists():
        raise ValueError("外置 Git 目标已存在，请核对已有仓库")
    if entry.exists() and (not (entry / "HEAD").is_file() or
                           any((entry / k).exists() for k in
                               ("worktrees", "commondir", "objects/info/alternates")) or
                           any(entry.rglob("*.lock"))):
        raise ValueError("Git 仓库正在写入、共享历史或不是独立仓库")
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if root.stat().st_dev != target.parent.stat().st_dev:
        raise ValueError("Git 外置要求工作区和本机目标在同一卷")
    subprocess.run(["git", "init", "--quiet", "--separate-git-dir", str(target), str(root)],
                   check=True, cwd=root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--externalize-git", action="store_true")
    parser.add_argument("--mode", choices=("local", "synology", "synology-direct"))
    parser.add_argument("--drive-root")
    parser.add_argument("--work-dir")
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    path = config_path()

    def load():
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    try:
        if args.status:
            profile, missing = configure(root, args, workspace_profile(load(), root))
        else:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (path.parent / "storage.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                records = load()
                saved = workspace_profile(records, root)
                profile, missing = configure(root, args, saved)
                if args.externalize_git:
                    if missing:
                        raise ValueError(f"缺少目录设置：{missing}")
                    externalize_git(root, profile)
                if profile and profile != saved:
                    target = profile.get("work_dir")
                    if target and target != str(root) and records.get(target) not in (None, profile, saved):
                        raise ValueError("目标工作区已有独立设置，请从该工作区运行 setup")
                    records[str(root)] = profile
                    if target and target != str(root) and not missing:
                        for key, entry in records.items():
                            if entry.get("configured_at") == str(root):
                                records[key] = {"configured_at": target}
                        records[str(root)] = {"configured_at": target}
                        records[target] = profile
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                                     dir=path.parent, delete=False) as out:
                        json.dump(records, out, ensure_ascii=False, indent=2)
                        out.write("\n")
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(out.name, path)
        status = "needs_input" if missing else "configured"
        if not missing and profile["work_dir"] != str(root):
            status = "open_configured_workspace"
        print(json.dumps({"status": status, "missing": missing, **profile}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, EOFError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

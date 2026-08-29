#!/usr/bin/env python3
"""把文件/目录移到废纸篓，确保可恢复。

inbox 摄入成功后清理源文件与临时提取目录时使用：即使后续流程异常，
源文件仍可从废纸篓找回，避免永久丢失。

策略：优先用 /usr/bin/trash（有独立 TCC 权限，移入系统废纸篓）；
trash 后验证源已消失，若仍存在则回退到项目内 temp/trash/ 保底。"""
from __future__ import annotations
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_TRASH_BIN = shutil.which("trash")


def trash_path(path) -> None:
    """移动文件或目录到废纸篓。trash 不可用或失败时回退到项目内 temp/trash/。"""
    p = Path(path)
    if not p.exists() and not p.is_symlink():
        return
    # 优先：/usr/bin/trash（NSWorkspace.recycleURLs，移入系统废纸篓）
    if _TRASH_BIN:
        result = subprocess.run([_TRASH_BIN, str(p)], capture_output=True, text=True)
        if result.returncode == 0 and not p.exists():
            return  # 成功移入系统废纸篓
    # 回退：项目内 temp/trash/（可见、可恢复，避免永久丢失）
    _move_to_project_trash(p)


def _move_to_project_trash(p: Path) -> None:
    """回退方案：移到项目内 temp/trash/，按时间戳归档。"""
    dest_dir = REPO / "temp" / "trash"
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = p.suffix if p.is_file() else ""
    name = f"{p.stem}-{ts}{suffix}" if p.is_file() else f"{p.name}-{ts}"
    dest = dest_dir / name
    shutil.move(str(p), str(dest))

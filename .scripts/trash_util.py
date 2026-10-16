#!/usr/bin/env python3
"""把文件/目录移到废纸篓，确保可恢复。

inbox 摄入成功后清理源文件与临时提取目录时使用：即使后续流程异常，
源文件仍可从废纸篓找回，避免永久丢失。

策略：优先使用宿主可用的系统废纸篓命令；命令后验证源已消失，
若仍存在则回退到项目内 temp/trash/ 保底。"""
from __future__ import annotations
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import platform_compat

REPO = Path(__file__).resolve().parent.parent


def trash_path(path) -> None:
    """移动文件或目录到废纸篓。trash 不可用或失败时回退到项目内 temp/trash/。"""
    # Absolute operands cannot become CLI options; keep symlinks unexpanded.
    p = Path(path).absolute()
    if not p.exists() and not p.is_symlink():
        return
    for command in platform_compat.system_trash_commands(p):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and not p.exists() and not p.is_symlink():
            return
    # 回退：项目内 temp/trash/（可见、可恢复，避免永久丢失）
    _move_to_project_trash(p)


def _move_to_project_trash(p: Path) -> None:
    """Move into an exclusively created bucket, preserving the original name."""
    dest_dir = REPO / "temp" / "trash"
    dest_dir.mkdir(parents=True, exist_ok=True)
    bucket = Path(tempfile.mkdtemp(
        prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=dest_dir,
    ))
    try:
        shutil.move(str(p), str(bucket / p.name))
    except Exception:
        # Remove only an empty reservation. Never delete the source or a
        # partially copied recovery artifact when a cross-device move fails.
        try:
            bucket.rmdir()
        except OSError:
            pass
        raise

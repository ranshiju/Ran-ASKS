#!/usr/bin/env python3
"""platform_compat.py — 跨平台兼容层(macOS / Linux / Windows)。

本项目原先假定 POSIX 运行环境。本模块收拢三类平台差异，供所有脚本复用：

1. 文件锁：POSIX 用 fcntl.flock，Windows 用 msvcrt.locking。
2. 子进程：统一用 sys.executable 而非 "python3"，并强制 UTF-8 编解码。
   中文 Windows 的 locale 编码是 cp936，subprocess(text=True) 默认按 cp936
   解码子进程输出，会把本项目的 UTF-8 中文输出解成乱码或直接抛
   UnicodeDecodeError。因此这里显式指定 encoding 并给子进程设 PYTHONUTF8。
3. 桌面集成：打开文件、外部工具查找(LibreOffice 等)的平台路径差异。

被 graph_lib、ingest_*、dsh/* 等复用。
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# 本项目所有文本产物(Wiki、Raw、JSON、日志)一律 UTF-8，不随系统 locale 变化。
ENCODING = "utf-8"

# 子进程一律用当前解释器，避免 Windows 上没有 python3 命令。
PYTHON = sys.executable or "python"


# --------------------------------------------------------------------------
# 1. 文件锁
# --------------------------------------------------------------------------

@contextmanager
def exclusive_lock(handle):
    """对已打开的文件句柄加独占锁，退出时释放。

    POSIX 用 fcntl.flock；Windows 用 msvcrt.locking(LK_LOCK)，它会阻塞重试
    约 10 秒后抛 OSError，故在此循环等待以获得与 flock 一致的阻塞语义。
    """
    if IS_WINDOWS:
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                continue  # 10 秒超时后重试，等价于 flock 的无限阻塞
        try:
            yield
        finally:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# --------------------------------------------------------------------------
# 2. 子进程
# --------------------------------------------------------------------------

def utf8_env(extra: dict | None = None) -> dict:
    """返回强制子进程以 UTF-8 读写标准流的环境变量副本。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run_text(cmd, **kwargs):
    """subprocess.run 的跨平台文本包装：强制 UTF-8 编解码。

    默认 text=True、encoding=utf-8、errors=replace，并把 PYTHONUTF8 传给子进程。
    调用方可以照常传 cwd/timeout/capture_output/check 等参数。
    """
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", ENCODING)
    kwargs.setdefault("errors", "replace")
    kwargs["env"] = utf8_env(kwargs.get("env"))
    return subprocess.run(cmd, **kwargs)


def python_cmd(script, *args) -> list[str]:
    """构造调用本项目脚本的命令行，使用当前解释器。"""
    return [PYTHON, str(script), *[str(a) for a in args]]


# --------------------------------------------------------------------------
# 3. 桌面集成与外部工具
# --------------------------------------------------------------------------

def open_in_desktop(path) -> None:
    """用系统默认程序打开文件，失败时静默(仅为便利功能)。"""
    target = str(path)
    try:
        if IS_WINDOWS:
            os.startfile(target)  # noqa: S606 — Windows 专用
        elif IS_MACOS:
            subprocess.run(["open", target], check=False)
        else:
            subprocess.run(["xdg-open", target], check=False)
    except OSError:
        pass


def soffice_candidates() -> list[Path]:
    """返回各平台 LibreOffice 可执行文件的常见安装路径。"""
    if IS_WINDOWS:
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
        return [Path(root) / "LibreOffice" / "program" / "soffice.exe"
                for root in roots if root]
    if IS_MACOS:
        return [Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")]
    return [Path("/usr/bin/soffice"), Path("/usr/local/bin/soffice"),
            Path("/opt/libreoffice/program/soffice")]


def soffice_names() -> list[str]:
    """PATH 查找用的 LibreOffice 命令名。"""
    return ["soffice.exe", "soffice", "libreoffice"] if IS_WINDOWS \
        else ["soffice", "libreoffice"]


def symlinks_available() -> bool:
    """当前进程能否创建符号链接。

    Windows 需要管理员权限或已开启“开发者模式”，否则创建符号链接抛
    OSError(WinError 1314)。依赖符号链接的测试据此跳过，而不是误报失败。
    """
    global _SYMLINK_OK
    if _SYMLINK_OK is None:
        import tempfile

        try:
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "target"
                target.write_text("", encoding=ENCODING, newline="\n")
                (Path(tmp) / "link").symlink_to(target)
            _SYMLINK_OK = True
        except (OSError, NotImplementedError):
            _SYMLINK_OK = False
    return _SYMLINK_OK


_SYMLINK_OK: bool | None = None

SYMLINK_SKIP_REASON = (
    "需要创建符号链接的权限；Windows 请开启开发者模式"
    "(设置 → 系统 → 开发者选项)或以管理员运行"
)


# --------------------------------------------------------------------------
# 4. Word 文档取文本(替代 macOS 专用的 textutil)
# --------------------------------------------------------------------------

def _docx_text(path: Path) -> str:
    """纯 Python 解析 .docx。docx 是 zip + OOXML，无需外部依赖。

    按 <w:p> 分段、<w:tab> 转制表符、<w:br>/<w:cr> 转换行，保持可读的纯文本。
    """
    import xml.etree.ElementTree as ET
    import zipfile

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""

    paragraphs = []
    for para in root.iter(f"{ns}p"):
        parts = []
        for node in para.iter():
            if node.tag == f"{ns}t":
                parts.append(node.text or "")
            elif node.tag == f"{ns}tab":
                parts.append("\t")
            elif node.tag in (f"{ns}br", f"{ns}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _convert_via_soffice(path: Path) -> str:
    """用 LibreOffice 无头模式把文档转成 txt，用于 .doc 等旧格式。"""
    import shutil
    import tempfile

    binary = None
    for name in soffice_names():
        binary = shutil.which(name)
        if binary:
            break
    if not binary:
        for candidate in soffice_candidates():
            if candidate.is_file():
                binary = str(candidate)
                break
    if not binary:
        return ""
    with tempfile.TemporaryDirectory() as tmp:
        result = run_text(
            [binary, "--headless", "--convert-to", "txt:Text (encoded):UTF8",
             "--outdir", tmp, str(path)],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            return ""
        produced = Path(tmp) / (path.stem + ".txt")
        if produced.is_file():
            return produced.read_text(encoding=ENCODING, errors="replace")
    return ""


def word_document_text(path) -> str:
    """跨平台读取 .doc/.docx 正文文本，取不到时返回空字符串。

    macOS 保留原有的 textutil 路径(对两种格式都可靠)；其他平台 .docx 走内置
    OOXML 解析，.doc 依次尝试 LibreOffice 与 pandoc。
    """
    import shutil

    target = Path(path)
    if IS_MACOS and shutil.which("textutil"):
        result = run_text(["textutil", "-convert", "txt", "-stdout", str(target)],
                          capture_output=True, timeout=30)
        if result.returncode == 0:
            return result.stdout
        return ""

    if target.suffix.lower() == ".docx":
        text = _docx_text(target)
        if text.strip():
            return text

    text = _convert_via_soffice(target)
    if text.strip():
        return text

    if shutil.which("pandoc"):
        result = run_text(["pandoc", "-t", "plain", str(target)],
                          capture_output=True, timeout=60)
        if result.returncode == 0:
            return result.stdout
    return ""

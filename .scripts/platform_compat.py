"""Shared OS primitives; callers retain authorization, transactions and semantic control."""
from __future__ import annotations

import errno
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"


def is_link(path):
    path = Path(path)
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def reject_link_components(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if is_link(part):
            raise ValueError(f"Symbolic links/reparse paths are not permitted: {part}")
    return path


class _WindowsAPI:
    """Loaded only on Windows; Win32 handles are never narrowed to 32-bit integers."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ctypes = ctypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD), ("hEvent", wt.HANDLE)]

        class AttributeTag(ctypes.Structure):
            _fields_ = [("FileAttributes", wt.DWORD), ("ReparseTag", wt.DWORD)]

        self.Overlapped, self.AttributeTag = Overlapped, AttributeTag
        definitions = {
            "LockFileEx": ([wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(Overlapped)], wt.BOOL),
            "UnlockFileEx": ([wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.POINTER(Overlapped)], wt.BOOL),
            "GetOverlappedResult": ([wt.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wt.DWORD), wt.BOOL], wt.BOOL),
            "CreateFileW": ([wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, wt.HANDLE], wt.HANDLE),
            "GetFileInformationByHandleEx": ([wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD], wt.BOOL),
            "GetFileType": ([wt.HANDLE], wt.DWORD),
            "GetFinalPathNameByHandleW": ([wt.HANDLE, wt.LPWSTR, wt.DWORD, wt.DWORD], wt.DWORD),
            "CloseHandle": ([wt.HANDLE], wt.BOOL),
        }
        for name, (args, result) in definitions.items():
            fn = getattr(self.kernel, name); fn.argtypes = args; fn.restype = result

    def lock(self, fd, *, exclusive, nonblocking, unlock=False):
        import msvcrt
        from ctypes import wintypes as wt
        c, kernel = self.ctypes, self.kernel
        handle = msvcrt.get_osfhandle(fd)
        overlapped = self.Overlapped()
        if unlock:
            if not kernel.UnlockFileEx(handle, 0, 1, 0, c.byref(overlapped)):
                raise c.WinError(c.get_last_error())
            return
        flags = (2 if exclusive else 0) | (1 if nonblocking else 0)
        if kernel.LockFileEx(handle, flags, 0, 1, 0, c.byref(overlapped)):
            return
        error = c.get_last_error()
        if error == 33 and nonblocking:  # ERROR_LOCK_VIOLATION, not access denied / bad descriptor.
            raise BlockingIOError(errno.EAGAIN, "file lock is held")
        if error == 997 and not nonblocking:  # Asynchronous handle supplied by a caller.
            transferred = wt.DWORD()
            if kernel.GetOverlappedResult(handle, c.byref(overlapped), c.byref(transferred), True):
                return
            error = c.get_last_error()
        raise c.WinError(error)

    def open_regular(self, path, flags):
        import msvcrt
        c, kernel = self.ctypes, self.kernel
        allowed = os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        if flags & ~allowed:
            raise ValueError("safe_open supports readonly/readwrite/create/exclusive, not truncation or append")
        access = 0xC0000000 if flags & os.O_RDWR else 0x40000000 if flags & os.O_WRONLY else 0x80000000
        disposition = 1 if flags & os.O_EXCL and flags & os.O_CREAT else 4 if flags & os.O_CREAT else 3
        # Share read/write/delete; OPEN_REPARSE_POINT prevents following a final link.
        handle = kernel.CreateFileW(str(path), access, 7, None, disposition, 0x00200000, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        owned = True
        try:
            info = self.AttributeTag()
            if not kernel.GetFileInformationByHandleEx(handle, 9, c.byref(info), c.sizeof(info)):
                raise c.WinError(c.get_last_error())
            if info.FileAttributes & (0x400 | 0x10) or kernel.GetFileType(handle) != 1:
                raise ValueError("safe_open requires a regular file, not a directory/reparse point/device")
            size = kernel.GetFinalPathNameByHandleW(handle, None, 0, 0)
            if not size:
                raise c.WinError(c.get_last_error())
            name = c.create_unicode_buffer(size + 1)
            returned = kernel.GetFinalPathNameByHandleW(handle, name, size + 1, 0)
            if not returned or returned > size:
                raise OSError("could not resolve opened file path")
            final = name.value
            if final.startswith("\\\\?\\UNC\\"):
                final = "\\\\" + final[8:]
            elif final.startswith("\\\\?\\"):
                final = final[4:]
            reject_link_components(path)
            if os.path.normcase(os.path.normpath(final)) != os.path.normcase(os.path.normpath(str(path))):
                raise ValueError("opened handle resolves outside the requested path")
            fd = msvcrt.open_osfhandle(handle, (flags & (os.O_RDWR | os.O_WRONLY)) | os.O_BINARY)
            owned = False
            return fd
        finally:
            if owned:
                kernel.CloseHandle(handle)


_windows = None


def _windows_api():
    global _windows
    if _windows is None:
        _windows = _WindowsAPI()
    return _windows


class _WindowsFcntl:
    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8

    @staticmethod
    def flock(target, operation):
        fd = target if isinstance(target, int) else target.fileno()
        base = operation & ~_WindowsFcntl.LOCK_NB
        if base not in (1, 2, 8) or operation & ~15:
            raise ValueError("unsupported lock operation")
        _windows_api().lock(fd, exclusive=base == 2, nonblocking=bool(operation & 4), unlock=base == 8)


if IS_WINDOWS:
    fcntl = _WindowsFcntl()
else:
    import fcntl


def safe_open(path, flags=os.O_RDONLY, mode=0o600):
    """Open a regular file without following symlinks or Windows reparse points."""
    path = Path(path).absolute()
    if IS_WINDOWS:
        raw = str(path)
        if raw.startswith(("\\\\?\\", "\\\\.\\")) or any(":" in part for part in path.parts[1:]):
            raise ValueError("Device namespaces and alternate data streams are not permitted")
        reject_link_components(path)
        return _windows_api().open_regular(path, flags)
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Expected a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def fsync_path(path):
    """Sync file or POSIX directory. False explicitly means Windows directory sync unsupported."""
    path = Path(path)
    if IS_WINDOWS and path.is_dir():
        return False
    descriptor = os.open(path, os.O_RDWR if IS_WINDOWS else os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


def relative_protocol_path(value):
    raw = str(value or "").replace("\\", "/")
    posix, windows = PurePosixPath(raw), PureWindowsPath(raw)
    if (not raw or raw == "." or posix.is_absolute() or windows.drive or windows.root
            or ".." in posix.parts or any(":" in part for part in posix.parts)):
        raise ValueError(f"expected a repository-relative path: {value!r}")
    return posix.as_posix()


def python_env(env=None):
    result = dict(os.environ if env is None else env)
    result["PYTHONIOENCODING"] = "utf-8:strict"
    result["PYTHONUTF8"] = "1"
    return result


def configure_stdio():
    """Explicit CLI boundary only; importing this module never changes global streams."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


def run_python(args, **kwargs):
    """Run a managed Python child with strict UTF-8 text results, preserving byte input."""
    kwargs = dict(kwargs)
    kwargs["env"] = python_env(kwargs.get("env"))
    for name in ("text", "encoding", "errors", "universal_newlines"):
        kwargs.pop(name, None)
    if isinstance(kwargs.get("input"), str):
        kwargs["input"] = kwargs["input"].encode("utf-8")
    check = kwargs.pop("check", False)
    # Windows subprocess readers run in threads: text-mode decode failures there
    # need not reach the caller. Capture bytes and decode on the calling thread.
    result = subprocess.run(args, **kwargs)
    for name in ("stdout", "stderr"):
        value = getattr(result, name)
        if isinstance(value, bytes):
            setattr(result, name, value.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
    if check:
        result.check_returncode()
    return result

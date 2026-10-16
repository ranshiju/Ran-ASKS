#!/usr/bin/env python3
"""Portable adapters for host commands used by shared workflows."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping


Which = Callable[[str], str | None]


def _executable(path: str | Path | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        # Keep launcher symlinks intact. Snap dispatches from the invoked name,
        # so resolving /snap/bin/libreoffice to /usr/bin/snap breaks argv[0].
        return candidate.absolute()
    return None


def find_executable(*names: str, which: Which = shutil.which) -> Path | None:
    """Return the first executable resolved from ``PATH``."""
    for name in names:
        resolved = _executable(which(name))
        if resolved is not None:
            return resolved
    return None


def find_soffice(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Which = shutil.which,
    home: Path | None = None,
) -> Path | None:
    """Resolve LibreOffice on Ubuntu, packaged runtimes, and macOS."""
    environment = os.environ if environ is None else environ
    candidates: list[Path] = []
    for configured in (explicit, environment.get("SOFFICE_BIN")):
        if configured:
            candidates.append(Path(configured).expanduser())

    for name in ("soffice", "libreoffice"):
        found = which(name)
        if found:
            candidates.append(Path(found))

    candidates.extend([
        Path("/usr/lib/libreoffice/program/soffice"),
        Path("/snap/bin/libreoffice"),
        Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ])
    runtime_root = (home or Path.home()) / ".cache" / "codex-runtimes"
    if runtime_root.exists():
        candidates.extend(sorted(
            runtime_root.glob("*/dependencies/bin/override/soffice"),
            reverse=True,
        ))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.suffix == ".app" and candidate.is_dir():
            candidate = candidate / "Contents/MacOS/soffice"
        resolved = _executable(candidate)
        if resolved is not None:
            return resolved
    return None


def soffice_environment(
    soffice: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a child environment without mutating the caller's environment."""
    env = dict(os.environ if environ is None else environ)
    if env.get("FONTCONFIG_FILE") or env.get("FONTCONFIG_PATH"):
        return env

    binary = Path(soffice)
    binaries = [binary]
    try:
        resolved_binary = binary.resolve(strict=True)
    except OSError:
        resolved_binary = binary
    if resolved_binary != binary:
        binaries.append(resolved_binary)

    candidates: list[Path] = []
    for candidate_binary in binaries:
        candidates.append(
            candidate_binary.parent.parent / "Resources/fontconfig/fonts.conf"
        )
        if (
            candidate_binary.parent.name == "override"
            and candidate_binary.parent.parent.name == "bin"
        ):
            candidates.append(
                candidate_binary.parent.parent.parent
                / "native/libreoffice-headless/libreoffice/LibreOfficeDev.app"
                / "Contents/Resources/fontconfig/fonts.conf"
            )
    for config in candidates:
        if config.is_file():
            env["FONTCONFIG_FILE"] = str(config)
            break
    return env


def default_open_commands(
    path: str | Path,
    *,
    platform: str | None = None,
    which: Which = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[list[str]]:
    """Return host opener commands in fallback order."""
    target = str(Path(path).absolute())
    host = platform or sys.platform
    if host == "darwin":
        opener = find_executable("open", which=which)
        return [[str(opener), target]] if opener else []
    commands: list[list[str]] = []
    if host.startswith("linux"):
        opener = find_executable("xdg-open", which=which)
        if opener:
            commands.append([str(opener), target])
        gio = find_executable("gio", which=which)
        if gio:
            commands.append([str(gio), "open", target])
        wslview = find_executable("wslview", which=which)
        if wslview:
            commands.append([str(wslview), target])
        explorer = find_executable("explorer.exe", which=which)
        if explorer:
            explorer_target = target
            wslpath = find_executable("wslpath", which=which)
            if wslpath:
                try:
                    converted = run(
                        [str(wslpath), "-w", target],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except (OSError, subprocess.SubprocessError):
                    converted = None
                if converted is not None and converted.returncode == 0:
                    windows_path = converted.stdout.strip()
                    if windows_path:
                        explorer_target = windows_path
            commands.append([str(explorer), explorer_target])
    return commands


def default_open_command(
    path: str | Path,
    *,
    platform: str | None = None,
    which: Which = shutil.which,
) -> list[str] | None:
    """Return the preferred host opener command, if available."""
    commands = default_open_commands(path, platform=platform, which=which)
    return commands[0] if commands else None


def open_with_default_app(
    path: str | Path,
    *,
    platform: str | None = None,
    which: Which = shutil.which,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Open a generated file without making a missing desktop tool fatal."""
    host = platform or sys.platform
    if host.startswith("win"):
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            return False
        try:
            startfile(str(Path(path).absolute()))
            return True
        except OSError:
            return False

    wsl_bridge = (
        host.startswith("linux")
        and find_executable("wslpath", which=which) is not None
        and find_executable("explorer.exe", which=which) is not None
    )
    for command in default_open_commands(
        path, platform=host, which=which, run=run
    ):
        try:
            result = run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        launcher = Path(command[0]).name.casefold()
        wsl_launch_accepted = (
            wsl_bridge
            and Path(path).exists()
            and launcher == "explorer.exe"
            and result.returncode == 1
        )
        if result.returncode == 0 or wsl_launch_accepted:
            return True
    return False


def system_trash_commands(
    path: str | Path,
    *,
    platform: str | None = None,
    which: Which = shutil.which,
) -> list[list[str]]:
    """Return recoverable system-trash commands in host preference order."""
    target = str(Path(path).absolute())
    host = platform or sys.platform
    commands: list[list[str]] = []

    if host.startswith("linux"):
        gio = find_executable("gio", which=which)
        if gio:
            commands.append([str(gio), "trash", target])
        trash_put = find_executable("trash-put", which=which)
        if trash_put:
            commands.append([str(trash_put), target])

    trash = find_executable("trash", which=which)
    if trash:
        commands.append([str(trash), target])
    return commands

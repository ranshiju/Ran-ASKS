#!/usr/bin/env python3
"""Dependency-free project environment parsing and API endpoint helpers."""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REFERENCE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _parse_value(raw: str, *, line_number: int) -> str:
    value = raw.lstrip()
    output: list[str] = []
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            else:
                output.append(char)
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and index > 0 and value[index - 1].isspace():
            break
        output.append(char)
    if escaped:
        output.append("\\")
    if quote:
        raise ValueError(f".env 第 {line_number} 行引号未闭合")
    return "".join(output).rstrip()


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """Parse a small, deterministic dotenv subset without importing a package."""
    path = Path(path)
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f".env 第 {line_number} 行缺少 '='")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f".env 第 {line_number} 行变量名非法: {key!r}")
        values[key] = _parse_value(raw_value, line_number=line_number)
    return values


def expand_references(values: dict[str, str]) -> dict[str, str]:
    """Expand `${NAME}` references and leave missing or cyclic references visible."""
    expanded = dict(values)
    for _ in range(len(expanded) + 1):
        updated = {
            key: _REFERENCE_RE.sub(
                lambda match: expanded.get(match.group(1), match.group(0)), value
            )
            for key, value in expanded.items()
        }
        if updated == expanded:
            break
        expanded = updated
    return expanded


def load_env(
    path: str | Path,
    *,
    keys: set[str] | frozenset[str] | None = None,
    prefixes: tuple[str, ...] = (),
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> dict[str, str]:
    """Load dotenv values with process-environment precedence, then expand refs."""
    values = parse_dotenv(path)
    environment = os.environ if environ is None else environ
    selected = set(values)
    selected.update(keys or ())
    for value in values.values():
        selected.update(_REFERENCE_RE.findall(value))
    for key, value in environment.items():
        if key in selected or any(key.startswith(prefix) for prefix in prefixes):
            values[key] = value
    return expand_references(values)


def join_api_url(base: str, path: str) -> str:
    """Join an absolute API base and origin-relative path without duplicate segments."""
    base = str(base or "").strip().rstrip("/")
    path = str(path or "").strip()
    parsed_base = urlsplit(base)
    parsed_path = urlsplit(path)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError("API base 必须是 http(s) 绝对 URL")
    if parsed_base.query or parsed_base.fragment:
        raise ValueError("API base 不得包含 query 或 fragment")
    if (not path.startswith("/") or parsed_path.scheme or parsed_path.netloc
            or parsed_path.query or parsed_path.fragment):
        raise ValueError("API path 必须是无 query/fragment 的站内绝对路径")
    base_parts = [part for part in parsed_base.path.split("/") if part]
    path_parts = [part for part in parsed_path.path.split("/") if part]
    if any(part in {".", ".."} for part in base_parts + path_parts):
        raise ValueError("API endpoint 不得包含路径穿越片段")
    overlap = 0
    for count in range(1, min(len(base_parts), len(path_parts)) + 1):
        if base_parts[-count:] == path_parts[:count]:
            overlap = count
    joined_path = "/" + "/".join(base_parts + path_parts[overlap:])
    return urlunsplit((parsed_base.scheme, parsed_base.netloc, joined_path, "", ""))

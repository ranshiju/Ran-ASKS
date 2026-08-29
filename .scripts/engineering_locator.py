#!/usr/bin/env python3
"""On-demand precise locators for engineering Markdown, YAML and Python.

This reader is deliberately separate from Raw/Wiki locator semantics.  It only
reads repository engineering text and never falls back to a whole document.
"""
from __future__ import annotations

import argparse
import ast
import codecs
import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import source_locator as source
import wiki_locator as wiki


REPO = Path(__file__).resolve().parent.parent
DEFAULT_MAX_CHARS = 12_000
MAX_LIST_ITEMS = 500
DENIED_PARTS = {"raw", "wiki"}
DENIED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz", ".tar", ".pyc",
}
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.+?)[ \t]*$", re.M)


@dataclass(frozen=True)
class LocatedBlock:
    locator: str
    kind: str
    start_line: int
    end_line: int
    content: str
    title: str = ""


def _error(path: str, locator: str, message: str, candidates: list[str] | None = None,
           **extra: Any) -> dict:
    result = {
        "ok": False,
        "path": path,
        "locator": locator,
        "error": message,
        "candidates": list(candidates or []),
    }
    result.update(extra)
    return result


def _repo_relative(target: Path) -> str:
    return target.resolve().relative_to(REPO.resolve()).as_posix()


def resolve_engineering_path(value: str | Path) -> tuple[Path | None, str]:
    """Resolve one repository engineering text path and enforce tool boundaries."""
    raw = str(value or "").strip()
    if not raw:
        return None, "缺少工程文件路径"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = REPO / candidate
    try:
        target = candidate.resolve()
        relative = target.relative_to(REPO.resolve())
    except (OSError, ValueError):
        return None, "路径不在仓库内"
    if any(part in DENIED_PARTS for part in relative.parts):
        return None, "Raw/Wiki 必须使用各自专用 locator"
    if target.suffix.lower() in DENIED_SUFFIXES:
        return None, "不支持数据库或二进制工程文件"
    if not target.is_file():
        return None, "工程文件不存在"
    try:
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        with target.open("rb") as handle:
            while chunk := handle.read(65_536):
                if b"\x00" in chunk:
                    return None, "目标不是可定位文本文件"
                decoder.decode(chunk, final=False)
        decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError):
        return None, "目标不是 UTF-8 工程文本"
    return target, ""


def _read_text(target: Path) -> tuple[str, list[str]]:
    text = target.read_text(encoding="utf-8")
    return text, text.splitlines()


def _heading_rows(text: str) -> list[dict]:
    """Parse Markdown headings outside fenced code and build stable hierarchy paths."""
    lines = text.splitlines()
    raw_rows: list[dict] = []
    stack: list[tuple[int, str]] = []
    fence: str | None = None
    for index, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        fence_match = re.match(r"^(```+|~~~+)", stripped)
        if fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        match = re.match(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        slug = wiki.heading_slug(title)
        while stack and stack[-1][0] >= level:
            stack.pop()
        path_parts = [item[1] for item in stack] + [slug]
        base = "/".join(part for part in path_parts if part)
        raw_rows.append({
            "level": level, "title": title, "slug": slug, "base": base,
            "start_line": index,
        })
        stack.append((level, slug))
    base_totals: dict[str, int] = {}
    for row in raw_rows:
        base_totals[row["base"]] = base_totals.get(row["base"], 0) + 1
    base_seen: dict[str, int] = {}
    for position, row in enumerate(raw_rows):
        base = row["base"]
        base_seen[base] = base_seen.get(base, 0) + 1
        suffix = f"@{base_seen[base]}" if base_totals[base] > 1 else ""
        row["locator"] = f"md:{base}{suffix}"
        end_line = len(lines)
        for later in raw_rows[position + 1:]:
            if later["level"] <= row["level"]:
                end_line = later["start_line"] - 1
                break
        row["end_line"] = end_line
    return raw_rows


def markdown_blocks(target: Path) -> list[LocatedBlock]:
    text, lines = _read_text(target)
    return [
        LocatedBlock(
            locator=row["locator"], kind="markdown-section",
            start_line=row["start_line"], end_line=row["end_line"],
            content="\n".join(lines[row["start_line"] - 1:row["end_line"]]),
            title=row["title"],
        )
        for row in _heading_rows(text)
    ]


def _match_markdown(target: Path, locator: str, *, prefix: bool = False) -> tuple[LocatedBlock | None, list[str]]:
    blocks = markdown_blocks(target)
    wanted = locator[3:] if locator.startswith("md:") else locator
    wanted_slug = wiki.heading_slug(re.sub(r"@\d+$", "", wanted.split("/")[-1]))
    exact = [block for block in blocks if block.locator == f"md:{wanted}"]
    if not exact:
        canonical_base = f"md:{wanted}"
        exact = [block for block in blocks
                 if re.sub(r"@\d+$", "", block.locator) == canonical_base]
    if not exact:
        exact = [block for block in blocks if block.title == wanted]
    if not exact:
        exact = [block for block in blocks
                 if block.locator.removeprefix("md:").split("/")[-1] == wanted_slug]
    if prefix and not exact:
        exact = [block for block in blocks if wanted.lower() in block.title.lower()]
    if len(exact) == 1:
        return exact[0], []
    choices = [block.locator for block in blocks]
    if len(exact) > 1:
        return None, [block.locator for block in exact[:10]]
    close = difflib.get_close_matches(f"md:{wanted}", choices, n=8, cutoff=0.25)
    return None, close or choices[:8]


def read_markdown_query(target: Path | str, query: str) -> dict:
    """Route adapter: resolve one existing section name/prefix without whole-file fallback."""
    path, error = resolve_engineering_path(target)
    if error:
        return _error(str(target), f"md:{query}", error)
    block, candidates = _match_markdown(path, query, prefix=True)
    if block is None:
        return _error(_repo_relative(path), f"md:{query}", "Markdown section 未唯一命中", candidates)
    return _success(path, block)


def _json_pointer_parts(pointer: str) -> list[str] | None:
    if not pointer.startswith("/"):
        return None
    if pointer == "/":
        return []
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _escape_pointer(part: str) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


def _yaml_pointers(node: yaml.Node, prefix: str = "", wanted: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            name = str(getattr(key, "value", ""))
            pointer = f"{prefix}/{_escape_pointer(name)}"
            relevant = not wanted or pointer.startswith(wanted) or wanted.startswith(pointer + "/")
            if not relevant:
                continue
            if not wanted or pointer.startswith(wanted):
                result.append(pointer)
            result.extend(_yaml_pointers(value, pointer, wanted))
    elif isinstance(node, yaml.SequenceNode):
        for index, value in enumerate(node.value):
            pointer = f"{prefix}/{index}"
            relevant = not wanted or pointer.startswith(wanted) or wanted.startswith(pointer + "/")
            if not relevant:
                continue
            if not wanted or pointer.startswith(wanted):
                result.append(pointer)
            result.extend(_yaml_pointers(value, pointer, wanted))
    return result


def _yaml_block(target: Path, locator: str) -> tuple[LocatedBlock | None, list[str], str]:
    text, lines = _read_text(target)
    try:
        root = yaml.compose(text)
    except yaml.YAMLError as exc:
        return None, [], f"YAML 无法解析: {exc}"
    if root is None:
        return None, [], "YAML 为空"
    pointer = locator.removeprefix("yaml:")
    parts = _json_pointer_parts(pointer)
    candidates = [f"yaml:{item}" for item in _yaml_pointers(root)[:MAX_LIST_ITEMS]]
    if parts is None or not parts:
        return None, candidates[:8], "YAML locator 必须是非根 JSON Pointer"
    node = root
    selected_start = node.start_mark.line
    for part in parts:
        if isinstance(node, yaml.MappingNode):
            match = next(((key, value) for key, value in node.value
                          if str(getattr(key, "value", "")) == part), None)
            if match is None:
                close = difflib.get_close_matches(locator, candidates, n=8, cutoff=0.25)
                return None, close or candidates[:8], "YAML key 不存在"
            key, node = match
            selected_start = key.start_mark.line
        elif isinstance(node, yaml.SequenceNode) and part.isdigit():
            index = int(part)
            if index >= len(node.value):
                return None, candidates[:8], "YAML sequence index 越界"
            node = node.value[index]
            selected_start = node.start_mark.line
        else:
            return None, candidates[:8], "YAML pointer 无法继续下钻"
    start_line = selected_start + 1
    end_line = max(start_line, node.end_mark.line)
    content = "\n".join(lines[start_line - 1:end_line])
    return LocatedBlock(locator=f"yaml:{pointer}", kind="yaml-node",
                        start_line=start_line, end_line=end_line, content=content), [], ""


def _python_rows(text: str) -> tuple[list[dict], str]:
    try:
        root = ast.parse(text)
    except SyntaxError as exc:
        return [], f"Python 无法解析: {exc.msg} (line {exc.lineno})"
    rows: list[dict] = []

    def visit(body: list[ast.stmt], parents: list[str]) -> None:
        for node in body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualified = ".".join([*parents, node.name])
            decorators = [item.lineno for item in getattr(node, "decorator_list", [])]
            start = min([node.lineno, *decorators])
            rows.append({
                "name": qualified,
                "locator": f"py:{qualified}",
                "start_line": start,
                "end_line": int(getattr(node, "end_lineno", node.lineno)),
                "kind": "python-class" if isinstance(node, ast.ClassDef) else "python-symbol",
            })
            visit(node.body, [*parents, node.name])

    visit(root.body, [])
    return rows, ""


def _python_block(target: Path, locator: str) -> tuple[LocatedBlock | None, list[str], str]:
    text, lines = _read_text(target)
    rows, error = _python_rows(text)
    if error:
        return None, [], error
    wanted = locator.removeprefix("py:")
    exact = [row for row in rows if row["name"] == wanted]
    choices = [row["locator"] for row in rows]
    if len(exact) != 1:
        close = difflib.get_close_matches(f"py:{wanted}", choices, n=8, cutoff=0.25)
        return None, close or choices[:8], "Python symbol 未唯一命中"
    row = exact[0]
    content = "\n".join(lines[row["start_line"] - 1:row["end_line"]])
    return LocatedBlock(locator=row["locator"], kind=row["kind"],
                        start_line=row["start_line"], end_line=row["end_line"],
                        content=content, title=row["name"]), [], ""


def _line_block(target: Path, locator: str) -> tuple[LocatedBlock | None, str]:
    requested = source.line_range(locator)
    if not requested:
        return None, "不支持的 engineering locator"
    _text, lines = _read_text(target)
    start, end = requested
    if end > len(lines):
        return None, "行范围越界"
    return LocatedBlock(locator=f"L{start}-L{end}", kind="line-range",
                        start_line=start, end_line=end,
                        content="\n".join(lines[start - 1:end])), ""


def _success(target: Path, block: LocatedBlock) -> dict:
    return {
        "ok": True,
        "path": _repo_relative(target),
        "locator": block.locator,
        "kind": block.kind,
        "start_line": block.start_line,
        "end_line": block.end_line,
        "content": block.content,
        "content_chars": len(block.content),
        "content_sha256": hashlib.sha256(block.content.encode("utf-8")).hexdigest(),
        "candidates": [],
    }


def read_engineering_locator(value: str, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Read exactly one engineering locator and return structured metadata."""
    path_part, locator = source.split_locator(value)
    if not path_part or not locator:
        return _error(path_part or str(value or ""), locator,
                      "需要精确 engineering locator；不会返回全文")
    target, error = resolve_engineering_path(path_part)
    if error:
        return _error(path_part, locator, error)
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return _error(_repo_relative(target), locator, "max_chars 必须是正整数")
    if max_chars < 1:
        return _error(_repo_relative(target), locator, "max_chars 必须是正整数")

    block: LocatedBlock | None
    candidates: list[str] = []
    detail = ""
    suffix = target.suffix.lower()
    if locator.startswith("md:"):
        if suffix not in {".md", ".markdown"}:
            return _error(_repo_relative(target), locator, "md locator 只适用于 Markdown")
        block, candidates = _match_markdown(target, locator)
        detail = "Markdown section 未唯一命中" if block is None else ""
    elif locator.startswith("yaml:"):
        if suffix not in {".yaml", ".yml"}:
            return _error(_repo_relative(target), locator, "yaml locator 只适用于 YAML")
        block, candidates, detail = _yaml_block(target, locator)
    elif locator.startswith("py:"):
        if suffix != ".py":
            return _error(_repo_relative(target), locator, "py locator 只适用于 Python")
        block, candidates, detail = _python_block(target, locator)
    else:
        block, detail = _line_block(target, locator)
    if block is None:
        return _error(_repo_relative(target), locator, detail, candidates)
    if len(block.content) > max_chars:
        return _error(
            _repo_relative(target), block.locator,
            f"locator 命中 {len(block.content)} 字符，超过预算 {max_chars}；请细化 locator",
            [], start_line=block.start_line, end_line=block.end_line,
            content_chars=len(block.content),
        )
    return _success(target, block)


def list_engineering_locators(value: str, prefix: str = "") -> dict:
    """List logical locators without returning document content."""
    target, error = resolve_engineering_path(value)
    if error:
        return _error(str(value), "", error)
    suffix = target.suffix.lower()
    items: list[dict] = []
    detail = ""
    if suffix in {".md", ".markdown"}:
        items = [{"locator": block.locator, "start_line": block.start_line,
                  "end_line": block.end_line, "title": block.title,
                  "kind": "markdown-section"}
                 for block in markdown_blocks(target)]
    elif suffix in {".yaml", ".yml"}:
        text, _lines = _read_text(target)
        try:
            root = yaml.compose(text)
            yaml_prefix = prefix.removeprefix("yaml:") if prefix.startswith("yaml:") else ""
            pointers = _yaml_pointers(root, wanted=yaml_prefix) if root else []
            items = [{"locator": f"yaml:{pointer}", "kind": "yaml-node"}
                     for pointer in pointers]
        except yaml.YAMLError as exc:
            detail = f"YAML 无法解析: {exc}"
    elif suffix == ".py":
        text, _lines = _read_text(target)
        rows, detail = _python_rows(text)
        items = [{key: row[key] for key in ("locator", "start_line", "end_line", "kind")}
                 for row in rows]
    else:
        detail = "该文本只支持显式 Lx-Ly locator"
    if detail:
        return _error(_repo_relative(target), "", detail)
    if prefix:
        items = [item for item in items if item["locator"].startswith(prefix)]
    truncated = len(items) > MAX_LIST_ITEMS
    return {
        "ok": True,
        "path": _repo_relative(target),
        "locators": items[:MAX_LIST_ITEMS],
        "count": len(items),
        "truncated": truncated,
        "filter_prefix": prefix,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="精确读取仓库工程文档、YAML、Python 符号或行范围")
    sub = parser.add_subparsers(dest="command", required=True)
    read_parser = sub.add_parser("read")
    read_parser.add_argument("locator")
    read_parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    list_parser = sub.add_parser("list")
    list_parser.add_argument("path")
    list_parser.add_argument("--prefix", default="",
                             help="只返回 canonical locator 以此前缀开头的条目")
    args = parser.parse_args()
    result = (read_engineering_locator(args.locator, args.max_chars)
              if args.command == "read" else list_engineering_locators(args.path, args.prefix))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

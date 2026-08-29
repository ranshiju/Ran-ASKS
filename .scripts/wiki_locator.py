#!/usr/bin/env python3
"""Wiki section locator and Raw-footnote bridge.

Wiki locators are rebuildable navigation addresses (``page.md#heading-slug``).
Raw locators remain the stable evidence addresses.  This module only performs
deterministic Markdown parsing; it never asks an LLM to find or slice text.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

import source_locator as raw_locator


REPO = Path(__file__).resolve().parent.parent
HEADING_RE = re.compile(r"^(?P<marks>#{1,6})[ \t]+(?P<title>.+?)\s*$")
FOOTNOTE_REF_RE = re.compile(r"\[\^(?P<id>[A-Za-z0-9_-]+)\]")
FOOTNOTE_DEF_RE = re.compile(
    r"^\[\^(?P<id>[A-Za-z0-9_-]+)\]:[ \t]+(?P<locator>.+?)[ \t]*$"
)
RAW_PLACEHOLDER_RE = re.compile(
    r"^(?P<prefix>\[\^[A-Za-z0-9_-]+\]:[ \t]+)RAW(?P<fragment>#\S+)[ \t]*$",
    re.M,
)
MAX_RAW_CITATION_CHARS = 8000


@dataclass(frozen=True)
class WikiSection:
    title: str
    slug: str
    level: int
    start_line: int
    end_line: int
    text: str
    footnote_ids: tuple[str, ...]
    raw_citations: tuple[str, ...]


def heading_slug(title: str) -> str:
    """Return a small, deterministic GitHub-style heading slug.

    CJK letters are preserved, Latin text is case-folded, whitespace becomes
    ``-`` and punctuation is removed.  Duplicate slugs are rejected instead of
    silently receiving unstable ``-1``/``-2`` suffixes.
    """
    text = re.sub(r"<[^>]+>", "", str(title or ""))
    text = re.sub(r"!?(?:\[([^]]*)\])\([^)]*\)", r"\1", text)
    text = text.replace("`", "").replace("*", "").replace("_", "")
    out: list[str] = []
    pending_dash = False
    for char in unicodedata.normalize("NFKC", text).casefold().strip():
        category = unicodedata.category(char)
        if char.isspace() or char == "-":
            pending_dash = bool(out)
            continue
        if category[0] in {"L", "N"}:
            if pending_dash and out and out[-1] != "-":
                out.append("-")
            out.append(char)
            pending_dash = False
    return "".join(out).strip("-")


def _frontmatter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return 0


def _footnote_definitions(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    definitions: dict[str, str] = {}
    duplicates: list[str] = []
    for line in lines:
        match = FOOTNOTE_DEF_RE.fullmatch(line.strip())
        if not match:
            continue
        footnote_id = match.group("id")
        if footnote_id in definitions:
            duplicates.append(footnote_id)
        else:
            locator = match.group("locator").strip()
            if locator.startswith("<") and locator.endswith(">"):
                locator = locator[1:-1].strip()
            definitions[footnote_id] = locator
    return definitions, duplicates


def parse_wiki_text(text: str) -> tuple[list[WikiSection], dict[str, str]]:
    """Parse headings, section bounds, footnote uses and Raw locators."""
    lines = text.splitlines()
    definitions, _duplicates = _footnote_definitions(lines)
    headings: list[tuple[int, int, str, str]] = []
    for index, line in enumerate(lines[_frontmatter_end(lines):], start=_frontmatter_end(lines)):
        match = HEADING_RE.fullmatch(line)
        if not match:
            continue
        title = match.group("title").strip()
        headings.append((index, len(match.group("marks")), title, heading_slug(title)))

    sections: list[WikiSection] = []
    for position, (start, level, title, slug) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _next_title, _next_slug in headings[position + 1:]:
            if next_level <= level:
                end = next_start
                break
        section_lines = lines[start:end]
        section_text = "\n".join(section_lines).strip()
        ids = tuple(dict.fromkeys(
            match.group("id")
            for line in section_lines
            if not FOOTNOTE_DEF_RE.fullmatch(line.strip())
            for match in FOOTNOTE_REF_RE.finditer(line)
        ))
        citations = tuple(definitions[footnote_id] for footnote_id in ids if footnote_id in definitions)
        sections.append(WikiSection(
            title=title,
            slug=slug,
            level=level,
            start_line=start + 1,
            end_line=end,
            text=section_text,
            footnote_ids=ids,
            raw_citations=citations,
        ))
    return sections, definitions


def parse_wiki_page(path: Path | str) -> tuple[list[WikiSection], dict[str, str]]:
    target = Path(path)
    return parse_wiki_text(target.read_text(encoding="utf-8", errors="replace"))


def resolve_wiki_path(value: str, base_path: str | None = None) -> Path | None:
    path_part, _fragment = raw_locator.split_locator(value)
    return raw_locator.resolve_path(path_part or value, base_path)


def get_wiki_section(path: Path | str, section: str) -> WikiSection | None:
    wanted = heading_slug(section)
    sections, _definitions = parse_wiki_page(path)
    return next((item for item in sections if item.slug == wanted), None)


def read_wiki_locator(value: str, section: str = "") -> dict:
    """Return only one Wiki section and the Raw locators cited by that section."""
    path_part, fragment = raw_locator.split_locator(value)
    target = resolve_wiki_path(path_part or value)
    if target is None:
        raise FileNotFoundError(f"Wiki 页面未找到: {path_part or value}")
    wanted = section or fragment
    if not wanted:
        raise ValueError("Wiki locator 需要 heading slug，例如 page.md#retrieval-control")
    item = get_wiki_section(target, wanted)
    if item is None:
        available = [entry.slug for entry in parse_wiki_page(target)[0]]
        raise KeyError(f"section '{wanted}' 不存在；可用: {', '.join(available)}")
    rel = str(target.resolve().relative_to(REPO.resolve()))
    return {
        "page": rel,
        "section": item.slug,
        "heading": item.title,
        "text": item.text,
        "raw_citations": list(item.raw_citations),
    }


def validate_wiki_page(path: Path | str, *, require_citations: bool = False,
                       raw_overrides: dict[str, Path | str] | None = None) -> list[str]:
    """Run only the three checks required by the Wiki→Raw locator contract."""
    target = Path(path)
    text = target.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    sections, definitions = parse_wiki_text(text)
    _defs, duplicate_defs = _footnote_definitions(lines)
    errors: list[str] = []

    seen_slugs: dict[str, str] = {}
    for item in sections:
        if not item.slug:
            errors.append(f"标题无法生成 locator slug: {item.title}")
        elif item.slug in seen_slugs:
            errors.append(f"heading slug 重复: {item.slug}")
        else:
            seen_slugs[item.slug] = item.title
    for footnote_id in duplicate_defs:
        errors.append(f"脚注定义重复: [^{footnote_id}]")

    used_ids = tuple(dict.fromkeys(
        match.group("id")
        for line in lines
        if not FOOTNOTE_DEF_RE.fullmatch(line.strip())
        for match in FOOTNOTE_REF_RE.finditer(line)
    ))
    for footnote_id in used_ids:
        if footnote_id not in definitions:
            errors.append(f"脚注未定义: [^{footnote_id}]")
    if require_citations and not used_ids:
        errors.append("页面没有 Raw locator 脚注")

    overrides = {str(key): Path(value) for key, value in (raw_overrides or {}).items()}
    for footnote_id in used_ids:
        locator = definitions.get(footnote_id, "")
        raw_path, fragment = raw_locator.split_locator(locator)
        if not raw_path or "/raw/" not in f"/{raw_path}" or not fragment or fragment == "全篇":
            errors.append(f"脚注 [^{footnote_id}] 不是精确 Raw locator: {locator or '<empty>'}")
            continue
        resolved = overrides.get(raw_path) or raw_locator.resolve_path(raw_path, target)
        if resolved is None or not Path(resolved).is_file():
            errors.append(f"脚注 [^{footnote_id}] Raw 路径不存在: {raw_path}")
            continue
        resolved = Path(resolved)
        if raw_locator.locator_status(fragment, resolved) != "present":
            errors.append(f"脚注 [^{footnote_id}] locator 不存在: {locator}")
            continue
        excerpt = raw_locator.read_locator_text(resolved, fragment)
        if not excerpt or not excerpt.strip():
            errors.append(f"脚注 [^{footnote_id}] locator 读取为空: {locator}")
        elif len(excerpt) > MAX_RAW_CITATION_CHARS:
            errors.append(f"脚注 [^{footnote_id}] locator 范围过大({len(excerpt)}字符): {locator}")
    return errors


def best_cited_section(path: Path | str, *terms: str) -> WikiSection | None:
    """Choose a cited section for Graph navigation using deterministic text overlap."""
    sections, _definitions = parse_wiki_page(path)
    candidates = [item for item in sections
                  if item.raw_citations and item.slug not in {"sources", "来源"}]
    if not candidates:
        return None
    body_candidates = [item for item in candidates if item.level >= 2]
    if body_candidates:
        candidates = body_candidates
    needles = [str(term).casefold().strip() for term in terms if str(term).strip()]

    def score(item: WikiSection) -> tuple[int, int, int]:
        haystack = item.text.casefold()
        exact = sum(3 for needle in needles if needle and needle in haystack)
        tokens = []
        for needle in needles:
            tokens.extend(re.findall(r"[a-z0-9]{3,}|[\u3400-\u9fff]{2,}", needle))
        overlap = sum(1 for token in set(tokens) if token in haystack)
        return exact + overlap, item.level, -item.start_line

    return max(candidates, key=score)


def graph_wiki_source(path: Path | str, *terms: str) -> tuple[str, list[str]] | tuple[str, list]:
    """Return ``wiki/page#section`` plus that section's Raw citations."""
    target = Path(path)
    if not target.is_absolute():
        target = REPO / target
    if not target.suffix and target.with_suffix(".md").is_file():
        target = target.with_suffix(".md")
    item = best_cited_section(target, *terms)
    if item is None:
        return "", []
    rel = str(target.resolve().relative_to(REPO.resolve())).removesuffix(".md")
    return f"{rel}#{item.slug}", list(item.raw_citations)


def annotate_raw_lines(text: str, raw_path: str = "RAW") -> str:
    """Expose deterministic single-line handles to a writer without changing Raw."""
    return "\n".join(
        f"<{raw_path}#L{index}> {line}" if line.strip() else line
        for index, line in enumerate(text.splitlines(), start=1)
    )


def annotate_context_lines(context: str, raw_text: str, raw_path: str = "RAW") -> str:
    """Attach true Raw line handles to a reduced context when exact lines survive."""
    positions: dict[str, list[int]] = {}
    for index, line in enumerate(raw_text.splitlines(), start=1):
        key = line.strip()
        if key:
            positions.setdefault(key, []).append(index)
    used: dict[str, int] = {}
    out: list[str] = []
    for line in context.splitlines():
        key = line.strip()
        choices = positions.get(key, [])
        offset = used.get(key, 0)
        if key and offset < len(choices):
            out.append(f"<{raw_path}#L{choices[offset]}> {line}")
            used[key] = offset + 1
        else:
            out.append(line)
    return "\n".join(out)


def replace_raw_placeholder(text: str, raw_path: str) -> str:
    """Replace only footnote-definition placeholders emitted by document prompts."""
    return RAW_PLACEHOLDER_RE.sub(
        lambda match: f"{match.group('prefix')}{raw_path}{match.group('fragment')}", text)

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
RAW_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9_])<?RAW#L\d+>?")
MALFORMED_RAW_FOOTNOTE_RE = re.compile(r"\[\^r\d+\]\d+>")
RAW_PLACEHOLDER_RE = re.compile(
    r"^(?P<prefix>\[\^[A-Za-z0-9_-]+\]:[ \t]+)RAW(?P<fragment>#\S+)[ \t]*$",
    re.M,
)
MAX_RAW_CITATION_CHARS = 8000
PLACEHOLDER_CLAIM_RE = re.compile(
    r"(?:结论|内容|信息|证据|详情)?(?:仍)?待(?:巩固|补充)"
    r"|暂无(?:结论|内容|信息|证据)",
)
PLACEHOLDER_TOKEN_RE = re.compile(r"(?:tbd|todo|unknown)", re.I)


_DRAFT_MARKER_RE = re.compile(
    r"[（(]\s*此处(?:仅)?(?:为|是)(?:示例|占位)(?:文本|内容)?\s*[）)]"
    r"|\[\s*待(?:填写|补充|替换)(?:[：:][^\]\n]{1,80})?\s*\]"
    r"|\[\s*(?:insert|write|add)\s+[^\]\n]{1,80}?\s+here\s*\]",
    re.IGNORECASE,
)


def _visible_prose(body):
    """Mask fenced/inline code and comments while retaining body line positions."""
    def mask(match):
        return re.sub(r"[^\n]", " ", match.group())

    # Keep newlines so diagnostics refer to the original body, not a compacted
    # rendering. Fences may use tildes or longer runs around Markdown examples.
    visible = []
    fence = None
    for line in body.splitlines(keepends=True):
        if fence:
            visible.append(re.sub(r"[^\n]", " ", line))
            if re.fullmatch(r"[ \t]{0,3}" + re.escape(fence[0])
                            + "{" + str(fence[1]) + r",}[ \t]*", line.rstrip("\r\n")):
                fence = None
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if opening:
            run = opening.group(1)
            fence = (run[0], len(run))
            visible.append(re.sub(r"[^\n]", " ", line))
        else:
            visible.append(line)
    prose = "".join(visible)
    prose = re.sub(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", mask, prose)
    return re.sub(r"<!--[\s\S]*?-->", mask, prose)


def _draft_markers_in_line(line):
    for match in _DRAFT_MARKER_RE.finditer(line):
        # A link named [待补充] is a reference, not an unfinished prose slot.
        if match.group().startswith("[") and re.match(r"[ \t]*[\[(]", line[match.end():]):
            continue
        yield match.group()


def check_drafting_markers(body):
    """Reject explicit authoring placeholders in prose, not literal code examples."""
    errors = []
    for number, line in enumerate(_visible_prose(body).splitlines(), 1):
        for marker in _draft_markers_in_line(line):
            errors.append(
                f"draft-marker: 正文第 {number} 行残留写作占位文本 {marker!r}；"
                "请依据来源完成该段并复验"
            )
    return errors


def semantic_overlap_tokens(value: str) -> set[str]:
    """Return stable Latin words and CJK bigrams for locator ranking."""
    text = str(value or "").casefold()
    tokens = set(re.findall(r"[a-z0-9]{3,}", text))
    for chunk in re.findall(r"[\u3400-\u9fff]+", text):
        if len(chunk) <= 2:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return tokens


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


def normalize_raw_footnote_locator(value: str) -> str:
    """Unwrap one unambiguous Markdown wrapper without changing the address.

    Aliases, nested wrappers, inline commentary and non-Raw links stay untouched
    so the ordinary evidence validator can reject them. This is not resolution.
    """
    locator = value.strip()
    for opening, closing in (("[[", "]]"), ("`", "`"), ("<", ">")):
        if not (locator.startswith(opening) and locator.endswith(closing)):
            continue
        inner = locator[len(opening):-len(closing)].strip()
        path, fragment = raw_locator.split_locator(inner)
        if (path and "/raw/" in f"/{path}" and fragment
                and not any(char in inner for char in "`[]<>|\r\n")):
            return inner
        break
    return locator


def normalize_raw_footnotes(text: str, *, reuse_inline: bool = False,
                            supplied_locators: tuple[str, ...] = ()) -> str:
    """Canonicalize definitions and optionally reuse exact inline Raw citations."""
    def replace(match):
        value = match.group("locator")
        normalized = normalize_raw_footnote_locator(value)
        if normalized == value.strip():
            return match.group(0)
        return f"[^{match.group('id')}]: {normalized}"

    normalized = re.sub(FOOTNOTE_DEF_RE.pattern, replace, text, flags=re.M)
    if not reuse_inline:
        return normalized
    lines = normalized.splitlines(keepends=True)
    definitions, duplicates = _footnote_definitions(lines)
    if duplicates:
        return normalized
    citations = {}
    for footnote_id, locator in definitions.items():
        raw_path, fragment = raw_locator.split_locator(locator)
        if "/raw/" in f"/{raw_path}" and re.fullmatch(r"L[1-9]\d*(?:-L[1-9]\d*)?", fragment):
            citations.setdefault(locator, footnote_id)
    frontmatter_end = _frontmatter_end([line.rstrip("\r\n") for line in lines])
    fence_marker = ""
    body_lines = []
    source_sections = []
    top_level_headings = []
    for index, line in enumerate(lines):
        if index < frontmatter_end:
            continue
        fence = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not fence_marker:
                fence_marker = marker
            elif (marker[0] == fence_marker[0] and len(marker) >= len(fence_marker)
                  and not line[fence.end():].strip()):
                fence_marker = ""
            continue
        if not fence_marker and line.strip() == "## Sources":
            source_sections.append(index)
        if not fence_marker and re.match(r"^#{1,2}\s+", line):
            top_level_headings.append(index)
        if (fence_marker or re.match(r"^(?: {4}|\t)", line)
                or re.match(r"^[ \t]{0,3}\[[^\]\n]+\]:", line)
                or HEADING_RE.fullmatch(line.strip()) or "`" in line or "[[" in line or "](" in line):
            continue
        body_lines.append(index)
    allowed = set()
    if len(source_sections) == 1 and not fence_marker:
        for locator in supplied_locators:
            path, fragment = raw_locator.split_locator(locator)
            if ("/raw/" in f"/{path}" and not any(char in locator for char in "`[]<>|\r\n ")
                    and re.fullmatch(r"L[1-9]\d*(?:-L[1-9]\d*)?", fragment)):
                allowed.add(locator)
    if not citations and not allowed:
        return normalized
    addresses = "|".join(re.escape(locator) for locator in sorted(set(citations) | allowed, key=len, reverse=True))
    inline_pattern = re.compile(
        r"(?<![/`\[\]\(<\"'\\!])\[(?P<bracket>" + addresses + r")\](?![\[\](])"
        r"|(?<![\w/`\[\(<\"'])(?P<bare>" + addresses + r")(?![\w/#-]|\.[\w])"
    )
    # A shortcut reference with a definition is a real Markdown link, even when
    # its label happens to be an exact Raw locator. Preserve that interpretation.
    visible_lines = _visible_prose(normalized).splitlines(keepends=True)
    reference_labels = {
        " ".join(match.group(1).split()).casefold()
        for line in visible_lines
        if (match := re.match(r"^[ \t]{0,3}\[([^\]^\n]+)\]:", line))
    }
    reserved = set(FOOTNOTE_REF_RE.findall(normalized))
    created = []

    def replace_inline(match):
        locator = match.group("bracket") or match.group("bare")
        if (visible_lines[index][match.start():match.end()] != match.group(0)
                or (match.group("bracket") and " ".join(locator.split()).casefold() in reference_labels)):
            return match.group(0)
        if locator not in citations:
            number = 1
            while f"r{number}" in reserved:
                number += 1
            footnote_id = f"r{number}"
            reserved.add(footnote_id)
            citations[locator] = footnote_id
            created.append(f"[^{footnote_id}]: {locator}\n")
        return f"[^{citations[locator]}]"

    for index in body_lines:
        lines[index] = inline_pattern.sub(replace_inline, lines[index])
    if created:
        end = next((index for index in top_level_headings
                    if index > source_sections[0]), len(lines))
        if end and not lines[end - 1].endswith("\n"):
            lines[end - 1] += "\n"
        lines[end:end] = ["\n", *created, "\n"]
    return "".join(lines)


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
            definitions[footnote_id] = normalize_raw_footnote_locator(match.group("locator"))
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
    rel = target.resolve().relative_to(REPO.resolve()).as_posix()
    return {
        "page": rel,
        "section": item.slug,
        "heading": item.title,
        "text": item.text,
        "raw_citations": list(item.raw_citations),
    }


def validate_wiki_page(path: Path | str, *, require_citations: bool = False,
                       raw_overrides: dict[str, Path | str] | None = None,
                       base_path: Path | str | None = None) -> list[str]:
    """Run the closed-loop checks required by the Wiki→Raw locator contract."""
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

    for handle in sorted(set(RAW_HANDLE_RE.findall(text))):
        errors.append(f"Wiki 残留未编译 RAW handle: {handle}")
    for fragment in sorted(set(MALFORMED_RAW_FOOTNOTE_RE.findall(text))):
        errors.append(f"Wiki 含残缺 RAW 脚注引用: {fragment}")

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
        resolved = overrides.get(raw_path) or raw_locator.resolve_path(raw_path, base_path or target)
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


def placeholder_claim_line_numbers(body: str) -> list[int]:
    """Locate empty placeholders without treating substantive uncertainty as one."""
    found = []
    for number, line in enumerate(_visible_prose(body).splitlines(), 1):
        without_citations = FOOTNOTE_REF_RE.sub("", line)
        statement = re.sub(r"^\s*(?:[-+*>]\s*|\d+[.)、]\s*)+", "", without_citations).strip()
        token = re.sub(r"[\s`*_>#\[\](){}:;,.!?，。；：！？、\-]+", "", statement)
        if (PLACEHOLDER_CLAIM_RE.fullmatch(token) or PLACEHOLDER_TOKEN_RE.fullmatch(token)
                or re.fullmatch(r"[（(]\s*待(?:填写|补充|替换)[^（）()\n]*[）)][。.!！]?", statement)
                or any(_draft_markers_in_line(without_citations))):
            found.append(number)
    return found


def validate_claim_sections(path: Path | str, section_names: tuple[str, ...]) -> list[str]:
    """Reject mechanically empty or explicit placeholder claims.

    This is deliberately not an entailment check. It only prevents a citation or
    known placeholder token from masquerading as substantive Wiki prose.
    """
    target = Path(path)
    sections, _definitions = parse_wiki_page(target)
    by_slug = {item.slug: item for item in sections}
    errors: list[str] = []
    for name in section_names:
        section = by_slug.get(heading_slug(name))
        if section is None:
            continue
        body_lines = []
        for line in section.text.splitlines()[1:]:
            stripped = line.strip()
            if not stripped or HEADING_RE.fullmatch(stripped) or FOOTNOTE_DEF_RE.fullmatch(stripped):
                continue
            body_lines.append(stripped)
        body = "\n".join(body_lines)
        without_citations = FOOTNOTE_REF_RE.sub("", body)
        visible = re.sub(r"[\s`*_>#\[\](){}:;,.!?，。；：！？、\-]+", "", without_citations)
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", visible):
            errors.append(f"{section.title} 只有脚注或格式标记，没有实质陈述")
        if placeholder_claim_line_numbers("\n".join(section.text.splitlines()[1:])):
            errors.append(f"{section.title} 含明确占位陈述")
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
        tokens = set().union(*(semantic_overlap_tokens(needle) for needle in needles))
        overlap = sum(1 for token in tokens if token in haystack)
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
    rel = target.resolve().relative_to(REPO.resolve()).as_posix().removesuffix(".md")
    return f"{rel}#{item.slug}", list(item.raw_citations)


def best_raw_citation(citations, *terms: str) -> str:
    """在已选 Wiki section 的脚注中，以文本重合选最贴近概念说明的一条。"""
    needles = [str(term).casefold().strip() for term in terms if str(term).strip()]
    tokens = set().union(*(semantic_overlap_tokens(needle) for needle in needles))

    def score(locator: str) -> tuple[int, int]:
        path, fragment = raw_locator.split_locator(locator)
        target = raw_locator.resolve_path(path)
        if target is None or raw_locator.locator_status(fragment, target) != "present":
            return -1, 0
        excerpt = raw_locator.read_locator_text(target, fragment).casefold()
        exact = sum(4 for needle in needles if needle and needle in excerpt)
        overlap = sum(1 for token in tokens if token in excerpt)
        return exact + overlap, -len(excerpt)

    valid = [str(locator).strip() for locator in citations if str(locator).strip()]
    return max(valid, key=score) if valid else ""


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

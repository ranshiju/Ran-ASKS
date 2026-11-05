#!/usr/bin/env python3
"""Read-only structure discovery and complete, source-addressed reading windows."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CHAR_THRESHOLD = 8_000
DEFAULT_SECTION_THRESHOLD = 4
MIN_SECTION_CHARS = 400
WINDOW_CHARS = 12_000
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CHAPTER_RE = re.compile(r"^(?:第[一二三四五六七八九十百零〇两\d]+[章节篇部]|chapter\s+\w+\b)", re.I)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def pdf_profile(path: Path) -> dict:
    """Bounded local probe; chapter structure is a format signal, not a domain guess."""
    if path.suffix.lower() != '.pdf':
        return {}
    try:
        import fitz
        with fitz.open(path) as doc:
            toc = doc.get_toc()
            chapters = [dict(level=level, title=title, page=page)
                        for level, title, page in toc
                        if CHAPTER_RE.match(title.strip()) and 1 <= page <= len(doc)]
            return {'pages': len(doc), 'chapters': chapters,
                    'book_structure': len({c['page'] for c in chapters}) >= 2}
    except (ImportError, RuntimeError, ValueError, OSError):
        return {}


def extract_native_pdf(path: Path) -> tuple[str, dict]:
    """Retain every page, expose bookmarks as headings only when found in its text.

    Reject image-only pages and corrupt text instead of silently dropping them.
    Formulas/figures still require checking the original; text extraction is not OCR.
    """
    import fitz
    lines, pages, issues, warnings = [], [], [], []
    with fitz.open(path) as doc:
        toc = doc.get_toc()
        for index, page in enumerate(doc, 1):
            text = page.get_text().strip()
            if not text and (page.get_images() or page.get_drawings()):
                issues.append(f'PDF page {index}: no text layer for visible content')
            bad = text.count('\ufffd')
            controls = sum(ord(c) < 32 and c not in '\n\r\t' for c in text)
            if controls:
                warnings.append({'page': index, 'unmapped_controls': controls})
                text = ''.join(f'[unmapped U+{ord(c):04X}]' if ord(c) < 32 and c not in '\n\r\t' else c for c in text)
            if text and bad / len(text) > .02:
                issues.append(f'PDF page {index}: corrupt text layer')
            start = len(lines) + 1
            lines.extend([f'<!-- PDF page {index} -->', ''])
            page_lines = [re.sub(r'^(#{1,6})(?=\s)', r'\\\1', line) for line in text.splitlines()]
            # Preserve the source words and physical order; only add Markdown heading syntax.
            for level, title, target in toc:
                if target != index:
                    continue
                normalized = re.sub(r'\s+', '', title)
                for n, line in enumerate(page_lines):
                    if re.sub(r'\s+', '', line) == normalized:
                        page_lines[n] = '#' * min(level + 1, 6) + ' ' + line
                        break
            lines.extend(page_lines)
            lines.append('')
            pages.append({'page': index, 'line': start, 'end_line': len(lines),
                          'text_chars': len(text)})
    if not pages or issues:
        raise ValueError('; '.join(issues) or 'empty PDF')
    text = '\n'.join(lines) + '\n'
    return text, {'schema': 'document-pdf-map-v1', 'parser': 'pymupdf',
                  'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'text_sha256': digest(text), 'pages': pages, 'warnings': warnings,
                  'limitations': ['Native text preserves reading evidence, not formula or figure layout; verify those against the PDF.']}


def parse_sections(text: str) -> list[dict]:
    lines = text.splitlines()
    headings, plain_headings = [], []
    fenced = False
    for number, line in enumerate(lines, 1):
        if re.match(r'^\s*(```|~~~)', line):
            fenced = not fenced
        match = None if fenced else HEADING_RE.match(line)
        if match:
            headings.append((number, len(match[1]), match[2]))
        elif (not fenced and len(line.strip()) < 160 and CHAPTER_RE.match(line.strip())
              and not re.search(r'(?:\.{2,}|\s{3,}).*\d+\s*$', line)):
            plain_headings.append((number, 2, line.strip()))
    if len(headings) < 2:
        headings = sorted(headings + plain_headings)
    # Choose structural peers. A single H1 is the document title, multiple H1s are chapters.
    levels = sorted({h[1] for h in headings})
    level = next((x for x in levels if sum(h[1] == x for h in headings) >= 2), None)
    if level is None:
        level = 2
    chapter_roots = [h for h in headings if CHAPTER_RE.match(h[2])]
    roots = chapter_roots if len(chapter_roots) >= 2 else [h for h in headings if h[1] == level]
    sections = []
    for index, (start, depth, title) in enumerate(roots):
        end = roots[index + 1][0] - 1 if index + 1 < len(roots) else len(lines)
        sections.append({'id': f'section-{index + 1:03d}', 'title': title, 'level': depth,
                         'line': start, 'end_line': end,
                         'char_count': len('\n'.join(lines[start:end]).strip())})
    return sections


def reading_windows(text: str, start: int = 1, end: int | None = None,
                    budget: int = WINDOW_CHARS) -> list[dict]:
    """Cover every character, including overlong individual lines, without renumbering.

    A split line retains its physical RAW line and offsets into that line.
    """
    if budget < 128:
        raise ValueError('reading budget must be >= 128')
    lines = text.splitlines()
    end = len(lines) if end is None else end
    windows, pieces, used = [], [], 0
    def flush():
        nonlocal pieces, used
        if pieces:
            windows.append({'line': pieces[0]['line'], 'end_line': pieces[-1]['line'],
                            'pieces': pieces, 'chars': used})
            pieces, used = [], 0
    for number in range(start, end + 1):
        line = lines[number - 1]
        offsets = range(0, max(1, len(line)), budget - 64)
        for offset in offsets:
            part = line[offset:offset + budget - 64]
            cost = len(part) + len(f'<RAW#L{number}> ') + 1
            if used + cost > budget:
                flush()
            pieces.append({'line': number, 'offset': offset, 'end_offset': offset + len(part),
                           'text': part})
            used += cost
    flush()
    return windows


def render_window(window: dict) -> str:
    return '\n'.join(f"<RAW#L{p['line']}> {p['text']}" for p in window['pieces'])


def keyword_budget(unit_count: int) -> dict:
    return {'overview': {'min': 3, 'max': min(8, 3 + unit_count)},
            'section': {'min': 2, 'max': 5},
            'selection_rule': '密度只作候选信号；依据独立问题、证据与导航价值确认。'}


def plan_text(text: str, source: str = '', *, char_threshold=DEFAULT_CHAR_THRESHOLD,
              section_threshold=DEFAULT_SECTION_THRESHOLD) -> dict:
    sections = parse_sections(text)
    substantive = [s for s in sections if s['char_count'] >= MIN_SECTION_CHARS]
    trigger = len(text) >= char_threshold or len(sections) >= section_threshold
    # Heading density alone does not justify splitting a short, fully readable source.
    review = len(text) >= char_threshold and len(substantive) >= 2
    # Include front matter and short sections as reading units even when not page candidates.
    units = []
    if sections and sections[0]['line'] > 1:
        units.append({'id': 'front-matter', 'title': '文档前置材料', 'line': 1,
                      'end_line': sections[0]['line'] - 1, 'page_candidate': False})
    for section in sections:
        units.append({**section, 'page_candidate': review and section in substantive})
    if not units:
        units = [{'id': 'document', 'title': '全文', 'line': 1,
                  'end_line': len(text.splitlines()), 'page_candidate': False}]
    for unit in units:
        unit['windows'] = reading_windows(text, unit['line'], unit['end_line'])
    return {'version': 2, 'raw': source, 'read_only': True, 'source_sha256': digest(text),
            'char_count': len(text), 'line_count': len(text.splitlines()),
            'heading_count': len(sections), 'substantive_section_count': len(substantive),
            'thresholds': {'char_count': char_threshold, 'heading_count': section_threshold,
                           'min_section_chars': MIN_SECTION_CHARS},
            'candidate_trigger': trigger, 'semantic_review_required': review,
            'recommended_structure': 'overview-plus-sections' if review else 'single-source-page',
            'split_candidates': substantive, 'units': units,
            'keyword_budget': keyword_budget(len(substantive) if review else 1),
            'next_step': '完整读取所有窗口；语义确认独立问题与证据后按需发布最多一层章节页。'}


def make_plan(path: Path, *, char_threshold=DEFAULT_CHAR_THRESHOLD,
              section_threshold=DEFAULT_SECTION_THRESHOLD) -> dict:
    try:
        source = path.resolve().relative_to(REPO).as_posix()
    except ValueError:
        source = str(path.resolve())
    return plan_text(path.read_text(encoding='utf-8'), source,
                     char_threshold=char_threshold, section_threshold=section_threshold)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw')
    parser.add_argument('--char-threshold', type=int, default=DEFAULT_CHAR_THRESHOLD)
    parser.add_argument('--section-threshold', type=int, default=DEFAULT_SECTION_THRESHOLD)
    args = parser.parse_args()
    path = (REPO / args.raw).resolve()
    if not path.is_file() or REPO not in path.parents:
        raise SystemExit('ERROR: source must be a repository file')
    if min(args.char_threshold, args.section_threshold) < 1:
        raise SystemExit('ERROR: thresholds must be positive')
    plan = make_plan(path, char_threshold=args.char_threshold, section_threshold=args.section_threshold)
    # CLI is a structure report; reading text is accessed separately via the task's windows.
    for unit in plan['units']:
        unit['windows'] = [{k: v for k, v in w.items() if k != 'pieces'} for w in unit['windows']]
    print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

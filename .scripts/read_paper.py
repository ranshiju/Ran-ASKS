#!/usr/bin/env python3
"""read_paper.py — paper.md 定向截取(替代 LLM 手动 grep+sed)

一次调用截取多段关键内容(Abstract/Method/Results/Conclusions 等),
省工具调用(3-4次→1次)、防漏读(报告命中/未命中)、模糊匹配标题写法。

用法:
  read_paper.py <paper.md路径> [sections...]
  read_paper.py academic/raw/references/orus-2008-itebd-beyond-unitary/paper.md
  read_paper.py .../paper.md Abstract Method Results Conclusions

  不传 sections 时默认截取: Abstract Introduction Method Results Conclusions Discussion
  section 名支持模糊匹配(大小写/编号前缀/中英文等价)。
"""
import argparse
import re
import sys
from pathlib import Path

# ===== 模糊匹配:section 名 → (正则, 权重) =====
# 权重用于在同名多个标题中选最优：主结果 > 实验段，正文段 > 附录段。
SECTION_ALIASES = {
    "abstract":     ((r"^(?:abstract|摘要)\b", 100), (r"\babstract\b|摘要", 80)),
    "introduction": ((r"^(?:introduction|引言|绪论|intro)\b", 100),
                     (r"\bintroduction\b|\bintro\b|引言|绪论", 80)),
    "method":       ((r"\b(?:method|methodology|framework|approach|formalism|canonical|algorithm|derivation|methods)\b|方法|推导", 100),
                     (r"\b(?:problem formalization|training|inference|self-rag)\b", 60)),
    "results":      ((r"\b(?:results?)\b|结果", 100),
                     (r"\banalysis\b|分析", 80),
                     (r"\b(?:experiments?|experimental|simulation|benchmark|numerical)\b|实验", 60)),
    "conclusions":  ((r"\bconclusions?\b|结论|总结", 100),
                     (r"\bsummary\b|summaries\b", 80)),
    "discussion":   ((r"\bdiscussion\b|讨论", 100),
                     (r"\banalysis\b|分析", 60)),
}

# 默认截取的关键段(按 INGEST 编码阶段最小读取)
DEFAULT_SECTIONS = ["abstract", "introduction", "method", "results", "discussion", "conclusions"]
SPACED_SECTION_NAMES = {
    "abstract", "introduction", "method", "methods", "methodology",
    "results", "discussion", "conclusion", "conclusions", "summary",
}


def normalize_heading_for_match(title):
    """Collapse all-caps spaced letters only when they form a known section name."""
    text = str(title or "").strip()
    for pattern in (
        r"(?P<letters>[A-Za-z](?:\s+[A-Za-z]){3,})",
        r"(?P<prefix>(?:(?:\d+(?:\.\d+)*)|(?:[IVX]+))[.:-]\s*)"
        r"(?P<letters>[A-Za-z](?:\s+[A-Za-z]){3,})",
    ):
        match = re.fullmatch(pattern, text, re.I)
        if not match:
            continue
        collapsed = re.sub(r"\s+", "", match.group("letters"))
        if collapsed.casefold() in SPACED_SECTION_NAMES:
            return f"{match.groupdict().get('prefix') or ''}{collapsed}"
    return text


def parse_sections(paper_path):
    """解析 paper.md 的二至六级 section，返回原始行号范围。"""
    text = Path(paper_path).read_text(encoding="utf-8")
    lines = text.split('\n')
    sections = []
    current_title = None
    current_start = None

    for i, line in enumerate(lines):
        # ## 标题(含编号,如 "## I. INTRODUCTION" / "## 1 Introduction")
        m = re.match(r'^#{2,6}\s+(.+)', line)
        if m:
            if current_title is not None:
                sections.append((current_title, current_start, i - 1))
            current_title = m.group(1).strip()
            current_start = i
    if current_title is not None:
        sections.append((current_title, current_start, len(lines) - 1))

    # ## 前的内容(论文标题+作者行+摘要,若无 ## Abstract)
    if not sections or sections[0][1] > 0:
        pre_end = sections[0][1] - 1 if sections else len(lines) - 1
        sections.insert(0, ("__preamble__", 0, pre_end))

    return sections, lines


def section_match_score(requested, title):
    """返回 requested 与 title 的匹配权重；0 表示不匹配。"""
    req = requested.lower().strip()
    title_lower = normalize_heading_for_match(title).lower()
    scores = []
    for alias_key, patterns in SECTION_ALIASES.items():
        if alias_key != req:
            continue
        for pattern, weight in patterns:
            if re.search(pattern, title_lower, re.I):
                scores.append(weight)
    if re.search(re.escape(req), title_lower, re.I):
        scores.append(70)
    return max(scores) if scores else 0


def _section_ranges(sections, lines, idx, req, max_chars=8000):
    """Select original inclusive ranges before rendering or adding handles."""
    ranges = [(sections[idx][1], sections[idx][2])]
    content = '\n'.join(lines[ranges[0][0]:ranges[0][1] + 1])
    deep = req.lower() == "method"
    if not deep and len(content.strip()) >= 200:
        return ranges
    title = sections[idx][0]
    numbered = re.match(r'^(\d+)(?:[ .:])', title)
    roman = re.match(r'^[IVXLCDM]+\.\s+', title)
    heading = re.match(r'^(#{2,6})\s+', lines[sections[idx][1]])
    level = len(heading.group(1)) if heading else 2
    prefix = numbered.group(1) + "." if numbered else None
    total = len(content)
    next_letter = "A"
    for j in range(idx + 1, len(sections)):
        next_title = sections[j][0]
        next_heading = re.match(r'^(#{2,6})\s+', lines[sections[j][1]])
        nested = next_heading is not None and len(next_heading.group(1)) > level
        letter = re.match(r'^([A-Z])\.\s+', next_title)
        roman_child = roman is not None and letter is not None
        if re.match(r'^[IVXLCDM]+\.\s+', next_title) and (
                letter is None or letter.group(1) != next_letter):
            roman_child = False
        if not (nested or roman_child or (prefix and next_title.startswith(prefix))):
            break
        if not (nested or roman_child) and req and section_match_score(req, next_title) < (60 if deep else 100):
            if deep:
                continue
            break
        part = '\n'.join(lines[sections[j][1]:sections[j][2] + 1])
        ranges.append((sections[j][1], sections[j][2]))
        if roman_child:
            next_letter = chr(ord(letter.group(1)) + 1)
        total += len(part) + 2
        if total >= max_chars:
            break
    return ranges


def _render_ranges(lines, ranges, source_path=None):
    return "\n\n".join(
        "\n".join(
            f"<{source_path}#L{number + 1}> {lines[number]}"
            if source_path else lines[number]
            for number in range(start, end + 1)
        )
        for start, end in ranges
    )


def is_appendix_section(title):
    """识别附录标题：APPENDIX 或 A./A.1/B./C./D. 这类字母编号段。"""
    stripped = title.strip()
    if re.match(r"^appendix\b", stripped, re.I):
        return True
    return bool(re.match(r"^[A-D](?:\d+(?:\.\d+)*)?[\.\s]", stripped))


def match_section(requested, title):
    """检查 requested section 名是否匹配 title(模糊,大小写不敏感)。"""
    return section_match_score(requested, title) > 0


def _abstract_range(lines):
    """从 preamble(无 ## Abstract)中收窄提取摘要。
    边界:received/published 行之后 → DOI 行或正文开头。
    找不到边界标记则返回 None；调用方不得把 preamble 全量冒充摘要。"""
    abs_start = None
    abs_end = None
    for i, line in enumerate(lines):
        ls = line.strip()
        # 开始:received/published/accepted 行之后
        if abs_start is None and re.search(r'received|accepted|published', ls, re.I):
            abs_start = i + 1
            # 跳过空行
            while abs_start < len(lines) and not lines[abs_start].strip():
                abs_start += 1
            continue
        # 结束:DOI 行
        if abs_start is not None and re.match(r'^doi[:\s]', ls, re.I):
            abs_end = i
            break
    if abs_start is not None:
        end = abs_end if abs_end is not None else min(abs_start + 40, len(lines))
        while end > abs_start and not lines[end - 1].strip():
            end -= 1
        if end == abs_start:
            return None
        return abs_start, end - 1
    return None


def _narrow_abstract_from_preamble(lines):
    selected = _abstract_range(lines)
    return _render_ranges(lines, [selected]).strip() if selected else None


def _reference_indices(lines):
    """无 ## References 段时,按 [N] 编号格式提取引文(从第一个 [N] 到文末或下一个非引文段)。"""
    indices = []
    in_refs = False
    for number, line in enumerate(lines):
        if re.match(r'^\[\d+\]\s', line) or re.match(r'^\d+\.\s+', line):
            in_refs = True
            indices.append(number)
        elif in_refs:
            # 续行(非空、非新编号)
            if line.strip() and not re.match(r'^\[\d+\]', line) and not re.match(r'^\d+\.\s', line):
                indices.append(number)
            elif not line.strip():
                indices.append(number)
            else:
                break  # 下一段开始
    return indices


def _extract_references_fallback(lines):
    indices = _reference_indices(lines)
    return '\n'.join(lines[number] if lines[number].strip() else "" for number in indices) if indices else None


def extract_sections(paper_path, requested_sections, *, source_lines=False, source_path=None):
    """截取请求的 sections,返回 (hits, misses, total_chars)。"""
    sections, lines = parse_sections(paper_path)
    source = str(source_path or paper_path) if source_lines else None

    hits = []   # [(requested, title, content)]
    misses = []  # [requested]
    used_indices = set()

    for req in requested_sections:
        found = False
        candidates = []
        for idx, (title, start, end) in enumerate(sections):
            if idx in used_indices:
                continue
            score = section_match_score(req, title)
            if score:
                candidates.append((score, 0 if not is_appendix_section(title) else 1,
                                   idx, title, start, end))
        if candidates:
            # 优先非附录；同类中权重高者优先；同权取先出现者。
            candidates.sort(key=lambda item: (item[1], -item[0], item[2]))
            _, _, idx, title, start, end = candidates[0]
            content = _render_ranges(lines, _section_ranges(sections, lines, idx, req), source)
            hits.append((req, title, content))
            used_indices.add(idx)
            found = True
        # abstract 未命中 ## 时只接受可机械定位的 preamble 摘要边界。
        if not found and req.lower() == "abstract":
            for idx, (title, start, end) in enumerate(sections):
                if idx in used_indices:
                    continue
                if title == "__preamble__":
                    preamble_lines = lines[start:end + 1]
                    # 尝试收窄:从 preamble 提取摘要段
                    narrowed = _narrow_abstract_from_preamble(preamble_lines)
                    if narrowed:
                        if source:
                            first, last = _abstract_range(preamble_lines)
                            narrowed = _render_ranges(lines, [(start + first, start + last)], source)
                        hits.append((req, "(narrowed: abstract after received line)", narrowed))
                        used_indices.add(idx)
                        found = True
                        break
        if not found:
            # References fallback: 无 ## References 段时,按编号式 [N] 提取
            if req.lower() in ("references", "reference", "bibliography", "引文", "参考文献"):
                ref_content = _extract_references_fallback(lines)
                if ref_content:
                    if source:
                        ref_content = '\n'.join(
                            f"<{source}#L{number + 1}> {lines[number]}"
                            for number in _reference_indices(lines)
                        )
                    hits.append((req, "(fallback: [N] numbered references)", ref_content))
                    found = True
            if not found:
                misses.append(req)

    total_chars = sum(len(c) for _, _, c in hits)
    return hits, misses, total_chars


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paper_path")
    parser.add_argument("sections", nargs="*", default=DEFAULT_SECTIONS)
    parser.add_argument("--source-lines", action="store_true")
    parser.add_argument("--source-path", help="Raw handle 基址；不改变实际读取路径")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--start-line", type=int)
    parser.add_argument("--end-line", type=int)
    args = parser.parse_intermixed_args()
    if not args.full and (args.start_line is not None or args.end_line is not None):
        parser.error("--start-line/--end-line require --full")
    paper_path = args.paper_path
    if not Path(paper_path).exists():
        print(f"ERROR: 文件不存在: {paper_path}", file=sys.stderr)
        sys.exit(1)

    if args.full:
        lines = Path(paper_path).read_text(encoding="utf-8").split('\n')
        start = args.start_line if args.start_line is not None else 1
        end = args.end_line if args.end_line is not None else len(lines)
        if not 1 <= start <= end <= len(lines):
            parser.error("line range must be inside the original file")
        print(_render_ranges(lines, [(start - 1, end - 1)],
                             str(args.source_path or paper_path) if args.source_lines else None))
        return
    requested = args.sections or DEFAULT_SECTIONS

    hits, misses, total_chars = extract_sections(
        paper_path, requested, source_lines=args.source_lines, source_path=args.source_path,
    )
    approx_tokens = total_chars // 3  # 中文+英文混合粗估

    # stderr: 诊断报告(命中哪些/未命中哪些/合计 token)
    print(f"[read_paper] {paper_path}", file=sys.stderr)
    print(f"[read_paper] 请求: {requested}", file=sys.stderr)
    print(f"[read_paper] 命中 {len(hits)}/{len(requested)} ({total_chars} chars, ≈{approx_tokens} tokens):", file=sys.stderr)
    for req, title, content in hits:
        print(f"  ✓ {req:12} → {title[:50]:50} ({len(content)} chars)", file=sys.stderr)

    # stdout: section 正文(供 LLM 上下文)。约定(见 INGEST.md 编码阶段):
    # 每段以 "--- [req] title ---" 标记起始,空行分隔,便于解析。
    if hits:
        for req, title, content in hits:
            print(f"--- [{req}] {title} ---")
            print(content)
            print()
    elif misses:
        # 无命中:尝试无标题论文的头部截取(PRL/Letter 流式,上限 ~3k tokens ≈ 9000 chars)
        has_headers = any(line.lstrip().startswith("## ") for line in Path(paper_path).read_text(encoding="utf-8").splitlines())
        if not has_headers:
            full = Path(paper_path).read_text(encoding="utf-8")
            cap = 9000
            output = full[:cap] if len(full) > cap else full
            tag = f"头部截取({cap}/{len(full)})" if len(full) > cap else f"全文({len(output)})"
            print(f"[read_paper] 未命中 {len(misses)}, 本文无 section 标题, {tag}, ≈{len(output)//3} tokens", file=sys.stderr)
            if args.source_lines:
                print(_render_ranges(output.split('\n'), [(0, len(output.split('\n')) - 1)],
                                     str(args.source_path or paper_path)))
            else:
                print(output)
        else:
            print(f"[read_paper] 未命中 {len(misses)}:", file=sys.stderr)
            for m in misses:
                print(f"  ✗ {m}", file=sys.stderr)
if __name__ == "__main__":
    main()

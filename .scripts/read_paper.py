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


def parse_sections(paper_path):
    """解析 paper.md 的所有 ## section,返回 [(title, start_line, end_line)]。"""
    text = Path(paper_path).read_text(encoding="utf-8")
    lines = text.split('\n')
    sections = []
    current_title = None
    current_start = None

    for i, line in enumerate(lines):
        # ## 标题(含编号,如 "## I. INTRODUCTION" / "## 1 Introduction")
        m = re.match(r'^##\s+(.+)', line)
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
    title_lower = title.lower()
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


def _expand_shallow_numbered_section(sections, lines, idx, content, req,
                                          min_chars=200, max_chars=8000):
    """父标题无正文时，向下吸收同号子节，避免命中空 results/method 段。"""
    if len(content.strip()) >= min_chars:
        return content
    title = sections[idx][0]
    m = re.match(r'^(\d+)(?:[ .:])', title)
    if not m:
        return content
    prefix = m.group(1) + "."
    parts = [content]
    total = len(content)
    for j in range(idx + 1, len(sections)):
        next_title = sections[j][0]
        if not next_title.startswith(prefix):
            break
        if req and section_match_score(req, next_title) < 100:
            break
        part = '\n'.join(lines[sections[j][1]:sections[j][2] + 1])
        parts.append(part)
        total += len(part) + 2
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def _expand_deep_method_section(sections, lines, idx, content, req,
                                        min_child_score=60, max_chars=8000):
    """method 命中父标题后，向下吸收同号相关子节。

    例如命中了 `## 3 SELF-RAG`，应把 `## 3.1 Problem Formulation`、
    `## 3.2 Training`、`## 3.3 Inference` 一并纳入，而不是只读概览段。
    """
    title = sections[idx][0]
    m = re.match(r'^(\d+)(?:[ .:])', title)
    if not m:
        return content
    prefix = m.group(1) + "."
    parts = [content]
    total = len(content)
    for j in range(idx + 1, len(sections)):
        next_title = sections[j][0]
        if not next_title.startswith(prefix):
            break
        if section_match_score(req, next_title) < min_child_score:
            continue
        part = '\n'.join(lines[sections[j][1]:sections[j][2] + 1])
        parts.append(part)
        total += len(part) + 2
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def is_appendix_section(title):
    """识别附录标题：APPENDIX 或 A./A.1/B./C./D. 这类字母编号段。"""
    stripped = title.strip()
    if re.match(r"^appendix\b", stripped, re.I):
        return True
    return bool(re.match(r"^[A-D](?:\d+(?:\.\d+)*)?[\.\s]", stripped))


def match_section(requested, title):
    """检查 requested section 名是否匹配 title(模糊,大小写不敏感)。"""
    return section_match_score(requested, title) > 0


def _narrow_abstract_from_preamble(lines):
    """从 preamble(无 ## Abstract)中收窄提取摘要。
    边界:received/published 行之后 → DOI 行或正文开头。
    找不到边界标记则返回 None(保持 preamble 全量)。"""
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
        content = '\n'.join(lines[abs_start:end])
        return content.strip()
    return None


def _extract_references_fallback(lines):
    """无 ## References 段时,按 [N] 编号格式提取引文(从第一个 [N] 到文末或下一个非引文段)。"""
    ref_lines = []
    in_refs = False
    for line in lines:
        if re.match(r'^\[\d+\]\s', line) or re.match(r'^\d+\.\s+', line):
            in_refs = True
            ref_lines.append(line)
        elif in_refs:
            # 续行(非空、非新编号)
            if line.strip() and not re.match(r'^\[\d+\]', line) and not re.match(r'^\d+\.\s', line):
                ref_lines.append(line)
            elif not line.strip():
                ref_lines.append("")  # 空行保留
            else:
                break  # 下一段开始
    return '\n'.join(ref_lines) if ref_lines else None


def extract_sections(paper_path, requested_sections):
    """截取请求的 sections,返回 (hits, misses, total_chars)。"""
    sections, lines = parse_sections(paper_path)

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
            content = '\n'.join(lines[start:end + 1])
            if req.lower() == "method":
                content = _expand_deep_method_section(sections, lines, idx, content, req)
            else:
                content = _expand_shallow_numbered_section(sections, lines, idx, content, req)
            hits.append((req, title, content))
            used_indices.add(idx)
            found = True
        # abstract 未命中 ## → 从 preamble 提取(标题+作者+摘要常在 ## 之前)
        if not found and req.lower() == "abstract":
            for idx, (title, start, end) in enumerate(sections):
                if idx in used_indices:
                    continue
                if title == "__preamble__":
                    preamble_lines = lines[start:end + 1]
                    # 尝试收窄:从 preamble 提取摘要段
                    narrowed = _narrow_abstract_from_preamble(preamble_lines)
                    if narrowed:
                        hits.append((req, "(narrowed: abstract after received line)", narrowed))
                    else:
                        content = '\n'.join(preamble_lines)
                        hits.append((req, "(preamble: title/authors/abstract)", content))
                    used_indices.add(idx)
                    found = True
                    break
        if not found:
            # References fallback: 无 ## References 段时,按编号式 [N] 提取
            if req.lower() in ("references", "reference", "bibliography", "引文", "参考文献"):
                ref_content = _extract_references_fallback(lines)
                if ref_content:
                    hits.append((req, "(fallback: [N] numbered references)", ref_content))
                    found = True
            if not found:
                misses.append(req)

    total_chars = sum(len(c) for _, _, c in hits)
    return hits, misses, total_chars


def main():
    if len(sys.argv) < 2:
        print("用法: read_paper.py <paper.md路径> [sections...]")
        print("  sections: abstract introduction method results conclusions discussion")
        print("  默认: 全部 6 段")
        sys.exit(1)

    paper_path = sys.argv[1]
    if not Path(paper_path).exists():
        print(f"ERROR: 文件不存在: {paper_path}", file=sys.stderr)
        sys.exit(1)

    requested = sys.argv[2:] if len(sys.argv) > 2 else DEFAULT_SECTIONS

    hits, misses, total_chars = extract_sections(paper_path, requested)
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
            print(output)
        else:
            print(f"[read_paper] 未命中 {len(misses)}:", file=sys.stderr)
            for m in misses:
                print(f"  ✗ {m}", file=sys.stderr)
if __name__ == "__main__":
    main()

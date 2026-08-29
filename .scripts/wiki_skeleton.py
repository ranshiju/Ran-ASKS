#!/usr/bin/env python3
"""wiki_skeleton.py — 生成 wiki md 骨架(防格式手写错误)

仿照 graph_ingest prefill 的思路:程序填确定性骨架,LLM 只填语义内容。
- frontmatter: type/sources/source_type/date/confidence/status/created/updated 程序填
- title/authors: 从 paper.md 机械提取(论文类)
- section 标题: Navigation/Content/Core Triples 独立成行(防手写混入 bug)
- 语义槽: venue/related/Navigation正文/Content正文 留空,标注 <-- LLM 填 -->

用法:
  wiki_skeleton.py --page academic/wiki/papers/orus-2008-itebd-beyond-unitary
  wiki_skeleton.py --page academic/wiki/authors/zhang-san  (非论文类,只生成空骨架)

输出到 stdout,LLM 复制后填语义槽写入文件。
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# ===== 路径推断 =====
# academic/wiki/papers/<id> → subproject=academic, dir=papers, type=paper-summary
DIR_TO_TYPE = {
    "papers": "paper-summary",
    "authors": "people",
    "concepts": "concept",
    "comparisons": "comparison",
    "reviews": "review",
    "review-guides": "review-guide",
    "web-references": "web-reference",
    "conferences": "conference-summary",
    "discussions": "discussion",
    # admin
    "policies": "policy",
    "procedures": "procedure",
    "decisions": "decision",
    "meetings": "meeting-summary",
    "speeches": "speech",
    "activities": "activity",
    "applications": "application",
    "profile": "profile",
    "references": "reference",
    "outputs": "output",
}

# 论文 raw 路径推断:references vs works/papers
def find_paper_md(subproject, paper_id):
    """找 paper.md 的 raw 路径(自己论文 vs 他人参考)。"""
    candidates = [
        f"{subproject}/raw/works/papers/{paper_id}/paper.md",   # 自己论文
        f"{subproject}/raw/references/{paper_id}/paper.md",     # 他人参考
    ]
    for c in candidates:
        if (REPO / c).exists():
            return c
    return None


def find_conference_raw(subproject, page_id):
    """找会议原文；历史 corrected.md 仅作兼容回退。"""
    base = REPO / subproject / "raw" / "conferences"
    if not base.exists():
        return None
    candidates = []
    for year_dir in sorted((path for path in base.iterdir() if path.is_dir()), reverse=True):
        candidates.extend([
            year_dir / page_id / f"{page_id}.txt",
            year_dir / f"{page_id}.txt",
            year_dir / f"{page_id}.md",
            year_dir / page_id / "corrected.md",
        ])
    for candidate in candidates:
        if candidate.exists():
            return candidate.relative_to(REPO).as_posix()
    return None


# ===== admin raw 路径推断 =====
def find_admin_raw(subproject, wiki_dir, page_id):
    """admin文档的raw路径推断。返回 (raw_path, source_type, confidence) 或 (None, ..., ...)"""
    # 按目录映射raw子目录
    dir_to_raw = {
        "policies": "policies",
        "procedures": "procedures",
        "decisions": "decisions",
        "meetings": "meetings",
        "speeches": "speeches",
        "activities": "activities",
        "applications": "applications",
        "profile": "profile",
        "references": "references",
        "outputs": "outputs",
    }
    raw_subdir = dir_to_raw.get(wiki_dir)
    if not raw_subdir:
        return None, "official-doc", "high"
    # 尝试多种匹配:精确文件/子目录内/年份子目录
    candidates = []
    # 精确同名(带各种扩展名)
    for ext in [".md", ".pdf", ".txt", ".docx"]:
        candidates.append(f"{subproject}/raw/{raw_subdir}/{page_id}{ext}")
    # 子目录形式(如policies/20260723-xxx/paper.md)
    candidates.append(f"{subproject}/raw/{raw_subdir}/{page_id}/paper.md")
    # meetings按年份子目录
    if raw_subdir == "meetings":
        # page_id 可能是 0714-xxx 或 2026-07-14-xxx
        for y in ["2026", "2025", "2024"]:
            candidates.append(f"{subproject}/raw/{raw_subdir}/{y}/{page_id}.md")
            candidates.append(f"{subproject}/raw/{raw_subdir}/{y}/{page_id}.txt")
    # speeches: 主题-YYYYMMDD格式
    if raw_subdir == "speeches":
        candidates.append(f"{subproject}/raw/{raw_subdir}/{page_id}.md")
        candidates.append(f"{subproject}/raw/{raw_subdir}/{page_id}.txt")
    for c in candidates:
        if (REPO / c).exists():
            st = "speech-recognition" if c.endswith(".txt") else "official-doc"
            conf = "medium" if st == "speech-recognition" else "high"
            return c, st, conf
    return None, "official-doc", "high"


# ===== 从 paper.md 机械提取 =====
_JOURNAL_HEADER_RE = re.compile(r"(?i)\b(?:VOLUME|VOL\.?)\s*\d")

def extract_title_from_papermd(paper_path):
    """从 paper.md 第一个非期刊页眉的 # 行提取标题。"""
    text = (REPO / paper_path).read_text(encoding="utf-8")
    for m in re.finditer(r'^# (.+)$', text, re.M):
        title = m.group(1).strip()
        if not _JOURNAL_HEADER_RE.search(title):
            return title
    return ""


def _normalize_author(name):
    """全大写作者名规范化为 Title Case，其余保持原样。"""
    if name and name.isupper():
        return name.title()
    return name


def extract_authors_from_text(text):
    """从论文标题下的作者块提取作者，兼容 MinerU 的空格和 HTML 上标噪声。"""
    lines = text.split('\n')
    # 找标题行（跳过期刊页眉）
    title_idx = None
    for i, line in enumerate(lines):
        if line.startswith('# '):
            if _JOURNAL_HEADER_RE.search(line[2:]):
                continue
            title_idx = i
            break
    if title_idx is None:
        return []
    # 标题后收集作者块；MinerU 可能把多位作者与机构压在同一行。
    collected = []
    institution_cues = (
        "DeepMind", "Perimeter", "Joint Quantum Institute", "National Quantum Laboratory",
        "Department", "Institute", "University", "School",
        "Laboratory", "College", "Faculty", "Centre", "Center", "Hospital",
    )
    name_token = r"[A-Z][A-Za-zÀ-ÿ\'’]+(?:-[A-Za-z][A-Za-zÀ-ÿ\'’]+)*"
    name_pattern = re.compile(
        rf"(?:{name_token}(?:\s+(?:[A-Z]\.?\s*){{1,4}})?\s+{name_token}|"
        rf"(?:[A-Z]\.?\s*){{1,4}}{name_token}(?:\s+{name_token})*|"
        r"(?:[A-ZÀ-ÿ]{2,}(?:-[A-ZÀ-ÿ]{2,})*(?:\s+[A-ZÀ-ÿ]{2,}(?:-[A-ZÀ-ÿ]{2,})*){1,4}))"
    )
    for i in range(title_idx + 1, min(title_idx + 40, len(lines))):
        line = lines[i].replace("\u00a0", " ").strip()
        if not line:
            if collected:
                lookahead = next((candidate.strip() for candidate in lines[i + 1:min(i + 3, len(lines))] if candidate.strip()), "")
                lookahead = re.sub(r'<sup\b[^>]*>.*?</sup>', '', lookahead, flags=re.I)
                if not name_pattern.match(lookahead):
                    break
            continue
        if re.search(r'\b(Abstract|摘要)\b', line, re.I):
            break
        line = re.sub(r'<sup\b[^>]*>.*?</sup>', '', line, flags=re.I)
        if collected and re.match(r"^(?:" + "|".join(re.escape(cue) for cue in institution_cues) + r")\b", line, re.I):
            break
        line = re.sub(r'\b([A-Z])\s+([a-zÀ-ÿ]{2,})\b', r'\1\2', line)
        line = re.split(
            r'\s+(?=(?:' + "|".join(re.escape(cue) for cue in institution_cues) + r')\b)',
            line, maxsplit=1, flags=re.I,
        )[0]
        # 逐行“姓名 + 机构 + 邮箱”只取行首姓名，避免把 UC San/Google 等机构片段当作者。
        # 无邮箱的压缩作者行仍按 AND/全行匹配保留多人。
        if "@" in line:
            leading = name_pattern.match(line)
            candidates = [leading.group(0).strip().rstrip("-–—")] if leading else []
        else:
            segments = re.split(r'\s+AND\s+', line)
            candidates = []
            for segment in segments:
                candidates.extend(match.group(0).strip().rstrip("-–—") for match in name_pattern.finditer(segment))
        if candidates:
            collected.extend(candidates)
        elif collected:
            break
    # 全大写作者名规范化为 Title Case
    collected = [_normalize_author(name) for name in collected]
    return list(dict.fromkeys(collected))


def extract_authors_from_papermd(paper_path):
    """从 paper.md 标题下方提取完整作者列表。"""
    return extract_authors_from_text((REPO / paper_path).read_text(encoding="utf-8"))


def parse_author_line(line):
    """解析作者行,如 'R. Orús and G. Vidal' → ['R. Orús', 'G. Vidal']。"""
    # 用 and/逗号分隔
    parts = re.split(r'\s+and\s+|,\s*', line)
    authors = [p.strip() for p in parts if p.strip()]
    return authors


# ===== 生成骨架 =====
def gen_skeleton(page_path, args, raw_override=None, source_override=None):
    """生成 wiki md 骨架。page_path 无 .md 后缀。"""
    page_path = page_path.removesuffix(".md")
    parts = page_path.split("/")
    if len(parts) < 4 or parts[1] != "wiki":
        print(f"ERROR: 路径格式应为 <subproject>/wiki/<dir>/<id>, got: {page_path}", file=sys.stderr)
        sys.exit(1)

    subproject = parts[0]       # academic
    wiki_dir = parts[2]         # papers
    page_id = parts[-1]         # orus-2008-itebd-beyond-unitary

    page_type = DIR_TO_TYPE.get(wiki_dir, "concept")
    today = date.today().isoformat()

    is_paper = (page_type == "paper-summary")

    # 论文类:从 paper.md 提取 title + authors + sources
    title = page_id
    authors = []
    sources = []
    source_type = "official-doc"
    confidence = "high"

    if is_paper:
        raw_path = raw_override or find_paper_md(subproject, page_id)
        if raw_path:
            sources = [source_override or raw_path]
            paper_path = Path(raw_path) if Path(raw_path).is_absolute() else REPO / raw_path
            paper_text = paper_path.read_text(encoding="utf-8")
            ext_title = ""
            for m in re.finditer(r'^# (.+)$', paper_text, re.M):
                cand = m.group(1).strip()
                if not _JOURNAL_HEADER_RE.search(cand):
                    ext_title = cand
                    break
            if ext_title:
                title = ext_title
            authors = extract_authors_from_text(paper_text)
        else:
            print(f"提示: 未找到 paper.md (尝试 works/papers 和 references)", file=sys.stderr)
    elif page_type == "conference-summary":
        raw_path = find_conference_raw(subproject, page_id)
        if raw_path:
            sources = [raw_path]
            source_type = "speech-recognition"
            confidence = "medium"
        else:
            print(f"提示: 未找到 conference raw (尝试 {subproject}/raw/conferences/<year>/)", file=sys.stderr)
    elif page_type in ("policy", "procedure", "decision", "meeting-summary", "activity", "speech", "application", "profile", "reference", "output"):
        # admin文档:推断raw路径
        raw_path, admin_st, admin_conf = find_admin_raw(subproject, wiki_dir, page_id)
        if raw_path:
            sources = [raw_path]
            source_type = admin_st
            confidence = admin_conf
        else:
            print(f"提示: 未找到 admin raw (尝试 {subproject}/raw/{wiki_dir}/)", file=sys.stderr)
    elif page_type == "people":
        source_type = "official-doc"
        # people 页 sources 由 LLM 填

    # 生成 frontmatter
    fm_lines = ["---"]
    fm_lines.append(f'title: "{title}"')
    fm_lines.append(f'type: {page_type}')
    if sources:
        fm_lines.append("sources:")
        for s in sources:
            fm_lines.append(f'  - {s}')
    else:
        fm_lines.append("sources: []  # <-- LLM 填(raw 路径)")
    fm_lines.append(f'source_type: {source_type}')
    if is_paper:
        fm_lines.append('date:  # <-- LLM 从 raw 提取发表日期(YYYY-MM-DD 或 YYYY)')
    else:
        fm_lines.append(f'date: {today}  # <-- LLM 确认(文档日期)')
    if is_paper:
        fm_lines.append(f'venue: ""  # <-- LLM 填(如 "Phys. Rev. B 78, 155117 (2008)")')
    # 规范型字段
    if page_type in ("policy", "procedure", "decision"):
        fm_lines.append(f'department: ""  # <-- LLM 填(发布部门)')
        fm_lines.append(f'effective_from: ""  # <-- LLM 填(生效日期)')
        fm_lines.append(f'effective_to: ""  # <-- LLM 填(失效日期,无则空)')
        fm_lines.append(f'applicable_to: ""  # <-- LLM 填(适用对象)')
    elif page_type in ("meeting-summary", "conference-summary", "activity"):
        fm_lines.append(f'department: ""  # <-- LLM 填(主办部门)')
    elif page_type == "speech":
        fm_lines.append(f'speaker: ""  # <-- LLM 填(发言人)')
        fm_lines.append(f'occasion: ""  # <-- LLM 填(场合)')
    if authors:
        fm_lines.append(f'authors: {authors}')
    fm_lines.append(f'confidence: {confidence}')
    default_status = "current" if page_type == "conference-summary" else ("confirmed" if page_type == "meeting-summary" else ("final" if page_type == "speech" else ("completed" if page_type == "activity" else "current")))
    fm_lines.append(f'status: {default_status}')
    fm_lines.append(f'created: {today}')
    fm_lines.append(f'updated: {today}')
    fm_lines.append('related: []  # <-- LLM 填(巩固阶段补)')
    fm_lines.append("---")

    frontmatter = '\n'.join(fm_lines)

    # 生成正文骨架
    if is_paper:
        body = f"""# {title}

> **作者**：{', '.join(authors) if authors else '<-- LLM 填 -->'} | **发表**：<-- LLM 填 -->
> **核心贡献**：<-- LLM 填(一句话概括) -->

## Navigation

<-- LLM 填:2-4 句导航概述(80-200 tokens)。注意:本段文本末尾不得接 ## Content 标题,必须分行。 -->

## 研究方向定位

<-- LLM 填:一句话说明研究对象、核心问题及方法或场景，并引用精确 Raw locator。此句专供程序与 Hub Scope 匹配。 -->

## Content

<-- LLM 填:按本论文主题选择 2-6 个自然小标题；不为凑模板重复摘要 -->

## Sources

<-- LLM 填:仅写正文实际使用的 Raw locator 脚注定义 -->"""
    elif page_type == "people":
        body = f"""# {title}

## Navigation

<-- LLM 填:一句话定位(身份/单位/研究方向) -->

## 人物画像

<-- LLM 填:一句可定位的导航描述。研究人员写研究对象/问题/方法；行政人员写职责/服务范围；学生写阶段/关注方向；其他角色按实际活动范围。引用精确 Raw locator。 -->

## Content

### 基本信息

<-- LLM 填:name/institution/papers 等 -->"""
    elif page_type in ("policy", "procedure", "decision"):
        # 规范型模板
        body = f"""# {title}

> **发布者**：<-- LLM 填 --> | **适用对象**：<-- LLM 填 --> | **生效**：<-- LLM 填 --> | **状态**：<-- LLM 填 -->

## Navigation

<-- LLM 填:2-3 句导航(什么文件/谁发布/管什么/是否有效) -->

## 适用对象与范围

<-- LLM 填:适用对象/适用范围 -->

## 核心规则

### 权利/义务
<-- LLM 填:可以/必须做什么 -->

### 禁止/例外
<-- LLM 填:不得做什么/例外情形 -->

## 关键节点

<-- LLM 填:生效时间/修订记录/衔接文件 -->"""
    elif page_type == "conference-summary":
        body = f"""# {title}

> **日期**：<-- LLM 填 -->
> **会议**：<-- LLM 填 -->
> **参与者**：<-- LLM 填；有 people 页时使用 wikilink -->

## Navigation

<-- LLM 填:2-3 句导航（会议主题、核心进展/结论、行动项）；不得将会议助手的解释性评述当事实 -->

## Content

### 一、讨论与进展

<-- LLM 填:只保留 raw 明确支持的议题、方法或结果；仅历史页可参考 corrected.md -->

### 二、资源、限制或待核实项

<-- LLM 填:没有则删除本小节；口述内容按 medium 置信度表述 -->

### 三、行动项

<-- LLM 填:任务、负责人、截止时间；未知信息不编造 -->"""
    elif page_type in ("meeting-summary", "activity"):
        # 事件型模板
        body = f"""# {title}

> **时间**：<-- LLM 填 --> | **地点/形式**：<-- LLM 填 --> | **主办**：<-- LLM 填 -->

## Navigation

<-- LLM 填:2-3 句导航(什么事件/何时/谁参与/核心议题) -->

## 参与者

<-- LLM 填(人名+角色,独立行) -->

## 讨论记录

<-- LLM 填:议题要点 -->

## 决策记录

<-- LLM 填:决定内容(决定≠讨论) -->

## 行动项

| 任务 | 负责人 | 截止时间 |
|------|--------|----------|
| <-- LLM 填 --> | | |

## 结果/产出

<-- LLM 填:主要结果 -->"""
    else:
        # 通用/实体型
        body = f"""# {title}

> **核心信息**：<-- LLM 填(一句话) -->

## Navigation

<-- LLM 填:2-3 句导航 -->

## Content

<-- LLM 填:正文 -->"""

    # 输出
    print(f"[wiki_skeleton] type={page_type}  subproject={subproject}  page_id={page_id}", file=sys.stderr)
    if is_paper and sources:
        print(f"[wiki_skeleton] 从 {sources[0]} 提取: title='{title}'  authors={authors}", file=sys.stderr)
    print(file=sys.stderr)

    print(frontmatter)
    print()
    print(body)


def main():
    ap = argparse.ArgumentParser(description="生成 wiki md 骨架")
    ap.add_argument("--page", required=True, help="目标 wiki 路径(无 .md 后缀亦可)")
    ap.add_argument("--raw", help="临时或正式 paper.md；仅 paper-summary 使用")
    ap.add_argument("--source", help="写入 frontmatter 的最终 sources 路径；需与 --raw 配合")
    ap.add_argument("--output", help="将骨架写入仓库内文件；默认 stdout")
    args = ap.parse_args()
    if bool(args.raw) != bool(args.source):
        ap.error("--raw 与 --source 必须同时提供")
    if args.raw:
        raw = (REPO / args.raw).resolve()
        if not raw.is_file() or REPO not in raw.parents:
            ap.error("--raw 必须是仓库内已提取的 paper.md")
        args.raw = str(raw)
    if args.output:
        output = (REPO / args.output).resolve()
        if REPO not in output.parents:
            ap.error("--output 必须位于仓库内")
        from contextlib import redirect_stdout
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as handle, redirect_stdout(handle):
            gen_skeleton(args.page, args, args.raw, args.source)
    else:
        gen_skeleton(args.page, args, args.raw, args.source)


if __name__ == "__main__":
    main()

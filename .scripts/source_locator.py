#!/usr/bin/env python3
"""来源定位解析与校验工具函数。"""
from pathlib import Path
import re

REPO = Path(__file__).resolve().parent.parent
LOCATOR_RE = re.compile(r"^(?P<path>[^#]+)(?:#(?P<locator>.+))?$")
LINE_LOCATOR_RE = re.compile(r"^L(?P<start>\d+)(?:-L?(?P<end>\d+))?$", re.I)
PAGE_LOCATOR_RE = re.compile(r"^(?:page-?)?(?P<start>\d+)(?:-(?P<end>\d+))?$", re.I)
TEXT_LOCATOR_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".csv"}
BINARY_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".jpg", ".jpeg", ".png"}
FACT_PREDICATES = {
    "作者", "通讯作者", "发表于", "引用", "参会", "就读", "所属", "主讲",
    "指导", "师从", "受指导于", "任职于", "研究关键词", "研究基础",
    "核心方法", "核心创新点", "局限性", "未来展望",
    "子方向",  # 父hub→子hub 层次边(hub_split 产生)
    "前一版本", "后一版本",  # raw 节点版本关系
    "补充材料",  # raw 节点补充材料关系
    "译自", "翻译为",  # raw 节点翻译关系
    "讨论", "汇报", "规划", "决策",  # 会议 keyword 谓词（涉及已在 NAV_PREDICATE_PREFIXES）
    "形成决策", "推动", "申请事项", "适用对象",  # 行政 keyword 谓词
    "依据", "替代", "发布者", "负责人", "承办部门",  # 行政关系谓词
    "涵盖", "考核", "前置", "后续", "适用", "开课单位", "主讲人",  # 教学谓词
    "分析", "规划", "竞争", "合作",  # 商业 keyword 谓词
}
NAV_PREDICATE_PREFIXES = ("主要研究", "紧密相关于", "涉及", "应用于", "基于", "贡献于", "探索")

def split_locator(value):
    match = LOCATOR_RE.match(str(value or "").strip())
    if not match:
        return "", ""
    return match.group("path").strip(), (match.group("locator") or "").strip()

def candidate_paths(path, base_path=None):
    path = str(path).strip()
    if path.startswith(("http://", "https://", "synology://")):
        return []
    candidates = [REPO / path]
    if not Path(path).suffix:
        candidates.append(REPO / f"{path}.md")
    if base_path:
        domain = next((p for p in Path(base_path).parts if p in {"academic", "admin", "teaching", "business", "cross-domain"}), None)
        if domain:
            candidates.append(REPO / domain / path)
            if not Path(path).suffix:
                candidates.append(REPO / domain / f"{path}.md")
    return list(dict.fromkeys(candidates))

def resolve_path(path, base_path=None):
    for candidate in candidate_paths(path, base_path):
        if candidate.exists():
            return candidate
    return None

def line_range(locator):
    """Parse L12/L12-18/L12-L18 into an inclusive 1-based range."""
    match = LINE_LOCATOR_RE.fullmatch(str(locator or "").strip())
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return None
    return start, end

def page_range(locator):
    """Parse page-3/page3/page-3-5 into an inclusive 1-based range."""
    match = PAGE_LOCATOR_RE.fullmatch(str(locator or "").strip())
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    if start < 1 or end < start:
        return None
    return start, end

def is_locator_compatible(path):
    """Whether the raw file can be deterministically sliced as text."""
    return Path(str(path)).suffix.lower() in TEXT_LOCATOR_SUFFIXES

def pdf_has_text(path):
    """Whether a PDF has a usable native text layer for page locators."""
    try:
        import fitz
        document = fitz.open(str(path))
        try:
            return any(page.get_text().strip() for page in document)
        finally:
            document.close()
    except Exception:
        return False

def native_locator_kind(path):
    """Return section-line/page when the original raw is directly locatable."""
    target = Path(str(path))
    if is_locator_compatible(target):
        return "section-line"
    if target.suffix.lower() == ".pdf" and target.is_file() and pdf_has_text(target):
        return "page"
    return None

def needs_locator_companion(path):
    return native_locator_kind(path) is None

def locator_companion_name(source_name):
    """Return the raw filename used for locators, preserving native text files."""
    source = Path(str(source_name))
    if is_locator_compatible(source):
        return source.name
    return f"{source.stem}.md"

def valid_locator(locator, target):
    if not locator:
        return False
    if target.suffix.lower() == ".pdf":
        return locator == "全篇" or bool(page_range(locator))
    if target.suffix.lower() in BINARY_SUFFIXES:
        return False
    if is_locator_compatible(target):
        return locator == "全篇" or bool(line_range(locator)) or bool(locator.strip())
    return bool(locator)

def locator_status(locator, target):
    """返回 present/missing/unverifiable，区分不存在与二进制材料不可机械核验。"""
    if not locator:
        return "present"
    if target.suffix.lower() == ".pdf":
        if locator == "全篇":
            return "present" if pdf_has_text(target) else "unverifiable"
        requested = page_range(locator)
        if not requested:
            return "missing"
        try:
            import fitz
            document = fitz.open(str(target))
            try:
                start, end = requested
                return "present" if end <= len(document) else "missing"
            finally:
                document.close()
        except Exception:
            return "unverifiable"
    if target.suffix.lower() in BINARY_SUFFIXES:
        return "unverifiable" if valid_locator(locator, target) else "missing"
    text = target.read_text(encoding="utf-8", errors="replace")
    if locator == "全篇":
        return "present"
    lines = text.splitlines()
    requested = line_range(locator)
    if requested:
        start, end = requested
        return "present" if end <= len(lines) else "missing"
    if locator in {"authors", "participants", "references"}:
        patterns = {
            "authors": (r"^# .+$", r"作者", r"\band\b"),
            "participants": (r"参与者", r"参会人", r"participants"),
            "references": (r"^#+\s*(References|参考文献)", r"References", r"参考文献"),
        }
        return "present" if any(re.search(pattern, text, re.M | re.I) for pattern in patterns[locator]) else "missing"
    headings = {m.group(1).strip().lower() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.M)}
    return "present" if locator.lower() in headings else "missing"

def read_locator_text(target, locator):
    """Return exactly the requested text slice, or None when it cannot be read."""
    if target.suffix.lower() == ".pdf":
        requested = page_range(locator)
        if locator != "全篇" and not requested:
            return None
        try:
            import fitz
            document = fitz.open(str(target))
            try:
                start, end = requested or (1, len(document))
                if end > len(document):
                    return None
                pages = [document[index - 1].get_text().strip() for index in range(start, end + 1)]
                text = "\n\n".join(page for page in pages if page)
                return text or None
            finally:
                document.close()
        except Exception:
            return None
    if target.suffix.lower() in BINARY_SUFFIXES:
        return None
    text = target.read_text(encoding="utf-8", errors="replace")
    if not locator or locator == "全篇":
        return text
    requested = line_range(locator)
    if requested:
        start, end = requested
        lines = text.splitlines()
        if end > len(lines):
            return None
        return "\n".join(lines[start - 1:end])
    locator_lower = locator.lower()
    if locator_lower == "authors":
        title = re.search(r"^#\s+.+$", text, re.M)
        if not title:
            return None
        next_heading = re.search(r"^#{1,6}\s+", text[title.end():], re.M)
        end = title.end() + (next_heading.start() if next_heading else len(text[title.end():]))
        return text[title.end():end].strip()
    heading_pattern = re.escape(locator)
    if locator_lower == "references":
        heading_pattern = r"(?:References|参考文献)"
    elif locator_lower == "participants":
        heading_pattern = r"(?:Participants|参与者|参会人(?:员)?)"
    match = re.search(rf"^(?P<marks>#{{1,6}})\s+{heading_pattern}\s*$", text, re.M | re.I)
    if not match and locator_lower == "participants":
        line = re.search(r"^.*(?:参与者|参会人(?:员)?|Participants)\s*[:：].*$", text, re.M | re.I)
        return line.group(0).strip() if line else None
    if not match:
        return None
    start = match.end()
    level = len(match.group("marks"))
    next_heading = re.search(rf"^#{{1,{level}}}\s+", text[start:], re.M)
    end = start + (next_heading.start() if next_heading else len(text[start:]))
    return text[start:end]

def classify_predicate(predicate):
    if predicate in FACT_PREDICATES:
        return "fact"
    if predicate.startswith(NAV_PREDICATE_PREFIXES):
        return "navigation"
    return "unknown"

#!/usr/bin/env python3
"""来源定位解析与校验工具函数。"""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent
LOCATOR_RE = re.compile(r"^(?P<path>[^#]+)(?:#(?P<locator>.+))?$")
LINE_LOCATOR_RE = re.compile(r"^L(?P<start>\d+)(?:-L?(?P<end>\d+))?$", re.I)
EXPLICIT_ANCHOR_RE = re.compile(
    r"\{:\s*#(?P<anchor>[A-Za-z][A-Za-z0-9_.:-]*)[^}]*\}"
)
PAGE_LOCATOR_RE = re.compile(r"^(?:page-?)?(?P<start>\d+)(?:-(?P<end>\d+))?$", re.I)
TABLE_LOCATOR_RE = re.compile(r"^table:([^:]+):([A-Z]+(?:,[A-Z]+)*):R([1-9]\d*)(?:-R?([1-9]\d*))?$")
TEXT_LOCATOR_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".jsonl", ".csv"}
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
BINARY_SUFFIXES = {".pdf", ".docx", ".doc", ".pptx", ".xls", ".xlsx"} | IMAGE_SUFFIXES
COMPANION_SCHEMA = "raw-companion-v1"
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
    """Whether managed ingest must preserve a Markdown reading projection."""
    return Path(str(path)).suffix.lower() in BINARY_SUFFIXES

def locator_companion_name(source_name):
    """Return the raw filename used for locators, preserving native text files."""
    source = Path(str(source_name))
    if is_locator_compatible(source):
        return source.name
    return f"{source.stem}.md"


def companion_sidecar_name(source_name):
    return Path(str(source_name)).name + ".source.json"


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_companion_record_data(source, companion_name, companion_data, *, generator,
                               generated_at, locator_scheme, method,
                               limitations=None, review_status=None):
    """Build binding metadata before a companion is committed to Raw."""
    source = Path(source)
    companion_name = Path(str(companion_name)).name
    if not source.is_file():
        raise ValueError("companion binding requires an existing source")
    if source.stem != Path(companion_name).stem or Path(companion_name).suffix.lower() != ".md":
        raise ValueError("companion must be same-stem Markdown")
    if not isinstance(companion_data, bytes):
        raise ValueError("companion_data must be bytes")
    if not isinstance(generator, dict) or not generator.get("name") or not generator.get("version"):
        raise ValueError("companion generator name and version are required")
    if not str(generated_at or "").strip():
        raise ValueError("companion generated_at is required")
    record = {
        "schema": COMPANION_SCHEMA,
        "original": source.name,
        "companion": companion_name,
        "source_sha256": _sha256_file(source),
        "companion_sha256": hashlib.sha256(companion_data).hexdigest(),
        "generator": generator,
        "generated_at": generated_at,
        "locator_scheme": locator_scheme,
        "fidelity": {
            "method": method,
            "limitations": list(limitations or []),
        },
    }
    if review_status:
        record["fidelity"]["review_status"] = review_status
    return record


def make_companion_record(source, companion, **kwargs):
    """Build binding metadata for an existing original/companion pair."""
    companion = Path(companion)
    if not companion.is_file():
        raise ValueError("companion binding requires an existing companion")
    return make_companion_record_data(
        source, companion.name, companion.read_bytes(), **kwargs)


def _binding_hash_status(source, companion, record):
    required = ("original", "companion", "source_sha256", "companion_sha256")
    if any(not record.get(field) for field in required):
        return {"status": "invalid", "reason": "binding_fields_missing"}
    if record["original"] != source.name or record["companion"] != companion.name:
        return {"status": "invalid", "reason": "binding_names_mismatch"}
    try:
        if _sha256_file(source) != record["source_sha256"]:
            return {"status": "invalid", "reason": "source_hash_mismatch"}
        if _sha256_file(companion) != record["companion_sha256"]:
            return {"status": "invalid", "reason": "companion_hash_mismatch"}
    except OSError:
        return {"status": "invalid", "reason": "binding_file_missing"}
    return {"status": "valid", "reason": "hash_bound"}


def companion_record_status(source, companion, record):
    source = Path(source)
    companion = Path(companion)
    if not isinstance(record, dict) or record.get("schema") != COMPANION_SCHEMA:
        return {"status": "invalid", "reason": "binding_schema_invalid"}
    return _binding_hash_status(source, companion, record)


def companion_binding_status(source, companion):
    """Return valid/legacy/invalid without exposing companion content."""
    source = Path(source)
    companion = Path(companion)
    sidecar = source.with_name(companion_sidecar_name(source.name))
    if not sidecar.is_file():
        return {"status": "legacy", "reason": "sidecar_missing"}
    try:
        context = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "invalid", "reason": "sidecar_unreadable"}
    record = context.get("companion") if isinstance(context, dict) else None
    if record is None:
        # Older reviewed image/PPTX sidecars already carry equivalent hashes.
        legacy_record = context.get("ocr") or context.get("presentation") if isinstance(context, dict) else None
        if not isinstance(legacy_record, dict) or not legacy_record.get("source_sha256"):
            return {"status": "legacy", "reason": "binding_metadata_missing"}
        record = {
            "original": legacy_record.get("original"),
            "companion": legacy_record.get("companion"),
            "source_sha256": legacy_record.get("source_sha256"),
            "companion_sha256": legacy_record.get("text_sha256"),
        }
    else:
        return companion_record_status(source, companion, record)
    return _binding_hash_status(source, companion, record)


def companion_binding_for_target(target):
    """Return binding status when the requested Raw path has a companion."""
    target = Path(target)
    original = original_for_companion(target)
    if original is not None:
        return companion_binding_status(original, target)
    if target.suffix.lower() in BINARY_SUFFIXES:
        companion = target.with_name(locator_companion_name(target.name))
        if companion.is_file():
            return companion_binding_status(target, companion)
    return None


def original_for_companion(path):
    """Return the unique same-stem binary original for a Markdown companion."""
    target = Path(path)
    if target.suffix.lower() != ".md":
        return None
    originals = [
        target.with_suffix(suffix)
        for suffix in sorted(BINARY_SUFFIXES)
        if target.with_suffix(suffix).is_file()
    ]
    return originals[0] if len(originals) == 1 else None


def pdf_companion_page_text(text, locator):
    """Read PDF page locators from a `## Page N` Markdown projection."""
    requested = page_range(locator)
    if not requested:
        return None
    matches = list(re.finditer(r"^#{1,6}\s+Page\s+(\d+)\s*$", text, re.M | re.I))
    pages = {}
    for index, match in enumerate(matches):
        page_number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages[page_number] = text[match.end():end].strip()
    start, end = requested
    if any(page_number not in pages for page_number in range(start, end + 1)):
        return None
    excerpt = "\n\n".join(pages[page_number] for page_number in range(start, end + 1)).strip()
    return excerpt or None


def explicit_anchor_block(text, locator):
    """Return the Markdown block bound to ``{: #locator}``, if present."""
    locator = str(locator or "").strip()
    if not locator:
        return None
    for match in EXPLICIT_ANCHOR_RE.finditer(text):
        if match.group("anchor") != locator:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        same_line = text[line_start:match.start()].strip()
        if same_line:
            return same_line
        before = text[:line_start].rstrip()
        if not before:
            return None
        block_start = before.rfind("\n\n") + 2
        block = before[block_start:].strip()
        return block or None
    return None

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

def table_locator_text(text, locator):
    match = TABLE_LOCATOR_RE.fullmatch(locator)
    if not match:
        return None
    sheet_name, column_names, first_row, last_row = match.groups()
    sheet_name = unquote(sheet_name)
    start, end = int(first_row), int(last_row or first_row)
    if end < start:
        return None
    columns = []
    for name in column_names.split(","):
        number = 0
        for character in name:
            number = number * 26 + ord(character) - ord("A") + 1
        columns.append(number - 1)
    current_sheet = None
    values_by_row = {}
    try:
        for line in text.splitlines():
            if line.startswith("## 工作表 "):
                current_sheet = json.loads(line.removeprefix("## 工作表 "))
            if current_sheet != sheet_name:
                continue
            row_match = re.fullmatch(r"R(\d+): (.*)", line)
            if not row_match:
                continue
            row_number = int(row_match.group(1))
            if row_number != 1 and not start <= row_number <= end:
                continue
            values = json.loads(row_match.group(2))
            if not isinstance(values, list) or max(columns) >= len(values) or row_number in values_by_row:
                return None
            values_by_row[row_number] = [values[column] for column in columns]
    except (ValueError, TypeError):
        return None
    if 1 not in values_by_row or end - start + 1 > len(values_by_row):
        return None
    if any(row_number not in values_by_row for row_number in range(start, end + 1)):
        return None
    header = json.dumps(values_by_row[1], ensure_ascii=False)
    lines = [f"工作表 {json.dumps(sheet_name, ensure_ascii=False)}；原始列 {column_names}；R1: {header}"]
    lines.extend(f"R{row_number}: {json.dumps(values_by_row[row_number], ensure_ascii=False)}"
                 for row_number in range(start, end + 1))
    return "\n".join(lines)


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
    original = original_for_companion(target)
    if original is not None and original.suffix.lower() == ".pdf" and page_range(locator):
        return "present" if pdf_companion_page_text(text, locator) is not None else "missing"
    if locator.startswith("table:"):
        return "present" if table_locator_text(text, locator) is not None else "missing"
    if locator == "全篇":
        return "present"
    lines = text.splitlines()
    requested = line_range(locator)
    if requested:
        start, end = requested
        return "present" if end <= len(lines) else "missing"
    if explicit_anchor_block(text, locator) is not None:
        return "present"
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
    original = original_for_companion(target)
    if original is not None and original.suffix.lower() == ".pdf" and page_range(locator):
        return pdf_companion_page_text(text, locator)
    if locator and locator.startswith("table:"):
        return table_locator_text(text, locator)
    if not locator or locator == "全篇":
        return text
    requested = line_range(locator)
    if requested:
        start, end = requested
        lines = text.splitlines()
        if end > len(lines):
            return None
        return "\n".join(lines[start - 1:end])
    anchored = explicit_anchor_block(text, locator)
    if anchored is not None:
        return anchored
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


def evidence_targets(target, locator):
    """Return ``(citation source, read target)`` for one Raw locator.

    A managed Markdown companion is a reading projection, not a replacement
    fact source. Managed binary companions are preferred for reading while
    citations retain the unique original path. Native PDF page extraction is
    only a compatibility fallback when no matching companion is available.
    """
    target = Path(target)
    original = original_for_companion(target)
    if original is not None:
        binding = companion_binding_status(original, target)
        return (original, target) if binding["status"] != "invalid" else (original, original)
    if target.suffix.lower() not in BINARY_SUFFIXES:
        return target, target
    companion = target.with_name(locator_companion_name(target.name))
    binding = companion_binding_status(target, companion) if companion.is_file() else None
    if (companion.is_file() and binding["status"] != "invalid"
            and locator_status(locator, companion) == "present"
            and read_locator_text(companion, locator) is not None):
        return target, companion
    if locator_status(locator, target) == "present" and read_locator_text(target, locator) is not None:
        return target, target
    return target, target

def classify_predicate(predicate):
    if predicate in FACT_PREDICATES:
        return "fact"
    if predicate.startswith(NAV_PREDICATE_PREFIXES):
        return "navigation"
    return "unknown"

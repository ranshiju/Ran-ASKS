#!/usr/bin/env python3
"""ingest_inbox.py — inbox 统一摄入入口：程序分流 + 边界样本 API 复核。

扫描 inbox/ 下的文件，按扩展名+内容关键词分类，分发到对应摄入脚本：
  - PDF + 学术特征（Abstract/References/arXiv/DOI）→ ingest_paper.py
  - PDF 非学术 → ingest_document.py
  - .txt + 会议特征（会议/参会/元宝会议助手/时间戳）→ ingest_meeting.py
  - .txt 非会议 / .docx / .doc / .pptx / .md → ingest_document.py

分类先由 Python 完成（pymupdf 前2页 + 关键词评分）；仅边界分数在 --run 时
调用一次受限 API 分类器复核。高置信度文件和 dry-run 不增加 LLM 调用。

用法：
  python3 .scripts/ingest_inbox.py                    # 扫描+分类，打印分流表（dry run）
  python3 .scripts/ingest_inbox.py --run              # 扫描+分类+逐个分发执行
  python3 .scripts/ingest_inbox.py --file inbox/x.pdf # 单文件分类
  python3 .scripts/ingest_inbox.py --run --file inbox/x.pdf  # 单文件执行
  python3 .scripts/ingest_inbox.py --download URL1 URL2      # 下载 PDF 到 inbox/ 再处理
  python3 .scripts/ingest_inbox.py --download URL --run       # 下载并摄入
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

ACADEMIC_EDITORIAL_MARKERS = (
    "专题导言", "特邀编辑", "本期专题", "编者按", "guest editorial", "guest editor",
)

ACADEMIC_PDF_PATTERNS = (
    ("abstract", r"\bAbstract\b|摘要"),
    ("references", r"\bReferences\b|参考文献"),
    ("arxiv", r"arxiv[:\s]?\d{4}\.\d{4,5}|\b\d{4}\.\d{4,5}\b"),
    ("doi", r"\bDOI\b|doi\.org"),
    ("keywords", r"\bKeywords\b|关键词"),
    ("introduction", r"\bIntroduction\b|引言|\d+\.\s+Introduction"),
    ("affiliation", r"[Uu]niversity|[Ii]nstitute|[Dd]epartment|[Ll]aboratory"),
    ("citation", r"\[\d+\]|\([A-Z]\w+\s+et\s+al\."),
    ("latex", r"\$.*?\$"),
    ("journal", r"PACS|Phys\.\s*Rev|Rev\.\s*Mod\.\s*Phys"),
)

MEETING_TXT_PATTERNS = (
    ("meeting", r"会议"), ("attendee", r"参会"), ("minutes", r"纪要"),
    ("assistant", r"元宝会议助手|腾讯会议|飞书"),
    ("timestamp", r"\(\d{2}:\d{2}\)"), ("report", r"汇报"),
    ("discussion", r"讨论"),
)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ingest_common as ic
from llm_structured import call_json
INBOX = REPO / "inbox"
SKIP_FILES = {".gitkeep", ".DS_Store", "facts-pending.md"}


def read_pdf_text(path: Path, max_pages: int = 2) -> str:
    """用 pymupdf 读 PDF 纯文本（不调 LLM）。

    短篇 PDF（≤6 页）自动读全部页，避免末页参考文献漏读导致误分类。
    """
    try:
        import fitz
        doc = fitz.open(str(path))
        total = len(doc)
        pages = total if total <= 6 else min(max_pages, total)
        text = ""
        for i in range(pages):
            text += doc[i].get_text()
        doc.close()
        return text
    except Exception:
        return ""


def _text_quality(text: str) -> float:
    """检测文本提取质量：可打印字母/数字/空格占比。乱码文本该值极低。"""
    if not text:
        return 0.0
    good = sum(1 for c in text if c.isprintable() and (c.isalnum() or c.isspace()))
    return good / len(text)


def _is_academic_by_metadata(path: Path) -> bool:
    """文本提取失败或乱码时，回退查 PDF 元数据判断是否学术论文。

    dvips/TeX/LaTeX 等生成器是强学术信号（几乎只用于论文/书籍排版）。
    """
    try:
        import fitz
        doc = fitz.open(str(path))
        creator = (doc.metadata.get("creator") or "").lower()
        producer = (doc.metadata.get("producer") or "").lower()
        doc.close()
        markers = ("dvips", "tex", "latex", "pdftex", "xetex", "luatex", "miktex")
        return any(marker in creator or marker in producer for marker in markers)
    except Exception:
        return False


def _matched_markers(text: str, patterns: tuple[tuple[str, str], ...]) -> list[str]:
    return [name for name, pattern in patterns if re.search(pattern, text, re.IGNORECASE)]


def classify_file_details(path: Path) -> dict:
    """返回确定性类型、得分、命中标记及是否需要 API 边界复核。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = read_pdf_text(path, max_pages=2)
        quality = _text_quality(text)
        if not text or len(text) < 200 or quality < 0.5:
            metadata_hit = _is_academic_by_metadata(path)
            return {
                "file_type": "paper" if metadata_hit else "document",
                "score": 3 if metadata_hit else 0,
                "threshold": 3,
                "markers": ["academic_pdf_metadata"] if metadata_hit else [],
                "confidence": "high" if metadata_hit else "unreviewable",
                "needs_api_review": False,
                "review_text": "",
            }
        markers = _matched_markers(text, ACADEMIC_PDF_PATTERNS)
        score = len(markers)
        return {
            "file_type": "paper" if score >= 3 else "document",
            "score": score,
            "threshold": 3,
            "markers": markers,
            "confidence": "low" if score in {2, 3} else "high",
            "needs_api_review": score in {2, 3},
            "review_text": text[:8000],
        }
    if suffix == ".txt":
        try:
            text = path.read_text(encoding="utf-8")[:8000]
        except (OSError, UnicodeError):
            text = ""
        markers = _matched_markers(text, MEETING_TXT_PATTERNS)
        score = len(markers)
        return {
            "file_type": "meeting" if score >= 2 else "document",
            "score": score,
            "threshold": 2,
            "markers": markers,
            "confidence": "low" if score in {1, 2} else "high",
            "needs_api_review": bool(text) and score in {1, 2},
            "review_text": text,
        }
    return {
        "file_type": "document", "score": None, "threshold": None,
        "markers": [f"extension:{suffix or 'none'}"], "confidence": "high",
        "needs_api_review": False, "review_text": "",
    }


def is_academic_pdf(path: Path) -> bool:
    return classify_file_details(path)["file_type"] == "paper"


def is_meeting_txt(path: Path) -> bool:
    return classify_file_details(path)["file_type"] == "meeting"


def classify_file(path: Path) -> str:
    """兼容入口：返回 'paper' / 'meeting' / 'document'。"""
    return classify_file_details(path)["file_type"]


def classification_review_schema(value) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("doc_type") not in {"paper", "meeting", "document", "ambiguous"}:
        return False
    if value.get("confidence") not in {"high", "medium", "low"}:
        return False
    reasons = value.get("reasons")
    evidence = value.get("evidence_quotes")
    return (isinstance(reasons, list) and 1 <= len(reasons) <= 4
            and all(isinstance(item, str) and item.strip() for item in reasons)
            and isinstance(evidence, list) and len(evidence) <= 4
            and all(isinstance(item, str) for item in evidence))


def review_low_confidence_classification(path: Path, decision: dict) -> dict:
    """用受限 API 分类器复核边界样本；不读知识库、不写任何持久层。"""
    allowed = "paper|document" if path.suffix.lower() == ".pdf" else "meeting|document"
    prompt = f"""你是受程序约束的文档类型复核组件。只判断来源类型，不生成 Wiki 或三元组。

[来源格式]
{path.suffix.lower()}

[程序判断]
type={decision['file_type']}
score={decision['score']}
threshold={decision['threshold']}
markers={json.dumps(decision['markers'], ensure_ascii=False)}

[有限正文]
{decision.get('review_text', '')}

[要求]
1. doc_type 只能为 {allowed}|ambiguous。
2. 只依据有限正文中的明确结构信号；不得用文件名或常识臆测。
3. 证据不足就返回 ambiguous，不要勉强同意程序。
4. evidence_quotes 只复制正文中的短原句，最多 4 条。
5. 只输出 JSON：
{{"doc_type":"...","confidence":"high|medium|low","reasons":["..."],"evidence_quotes":["..."]}}"""
    review_id = "classify-" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    result = call_json(
        prompt, classification_review_schema, max_tokens=600, retries=1,
        operation="ingest_type_review", reasoning="fast", transaction_id=review_id,
        system="你是文档类型复核组件，只输出符合契约的 JSON。",
    )
    if not result.get("ok"):
        return {"status": "review_error", "error": result.get("error", "API 分类复核失败"),
                "classification_transaction_id": review_id}
    review = dict(result.get("parsed") or {})
    review["status"] = "ok"
    review["classification_transaction_id"] = review_id
    return review


def reconcile_classification(decision: dict, review: dict) -> tuple[bool, str]:
    """只有 API 中高置信度同意程序类型时放行。"""
    if review.get("status") != "ok":
        return False, f"API 分类复核失败: {review.get('error', 'unknown')}"
    api_type = review.get("doc_type")
    if api_type == "ambiguous":
        return False, "API 分类复核认为证据不足（ambiguous）"
    if api_type != decision.get("file_type"):
        return False, f"程序/API 类型不一致: {decision.get('file_type')} != {api_type}"
    if review.get("confidence") == "low":
        return False, "API 分类复核置信度仍为 low"
    return True, ""


def classify_academic_document(path: Path) -> str | None:
    """只用强信号自动识别 academic 非论文文档；不确定时要求人工分类。"""
    text = path.stem
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text += "\n" + read_pdf_text(path, max_pages=3)
    elif suffix in {".txt", ".md"}:
        try:
            text += "\n" + path.read_text(encoding="utf-8")[:20000]
        except (OSError, UnicodeError):
            pass
    lowered = text.lower()
    if any(marker in lowered for marker in ACADEMIC_EDITORIAL_MARKERS):
        return "editorial"
    if re.search(r"\beditorial\b", lowered):
        return "editorial"
    return None


def dispatch_command(file_type: str, rel_path: str, subproject: str,
                     document_type: str | None = None) -> list[str]:
    """返回对应类型的分发命令；academic 文档必须已有显式分类。"""
    if file_type == "paper":
        return [sys.executable, str(REPO / ".scripts/ingest_paper.py"), "--pdf", rel_path]
    if file_type == "meeting":
        return [sys.executable, str(REPO / ".scripts/ingest_meeting.py"),
                "--txt", rel_path, "--subproject", subproject]
    if subproject == "academic" and not document_type:
        raise ValueError("classification_required: academic 文档缺少 document_type")
    command = [sys.executable, str(REPO / ".scripts/ingest_document.py"),
               "--file", rel_path, "--subproject", subproject]
    if document_type:
        command.extend(["--document-type", document_type])
    return command


def dsi_tool(file_type: str, rel_path: str, subproject: str,
             document_type: str | None = None) -> tuple[str, dict]:
    """返回 DSH ingest tool 名与参数，替代直接 subprocess dispatch。"""
    if file_type == "paper":
        return "ingest_paper_pdf", {"pdf": rel_path}
    if file_type == "meeting":
        return "ingest_meeting_txt", {"file": rel_path, "subproject": subproject}
    if subproject == "academic" and not document_type:
        raise ValueError("classification_required: academic 文档缺少 document_type")
    args = {"file": rel_path, "subproject": subproject}
    if document_type:
        args["document_type"] = document_type
    return "ingest_document_file", args


def scan_inbox() -> list[Path]:
    """扫描 inbox/ 下的待摄入文件（排除 .gitkeep/.DS_Store/facts-pending.md）。"""
    if not INBOX.is_dir():
        return []
    files = []
    for p in sorted(INBOX.iterdir()):
        if p.is_file() and p.name not in SKIP_FILES and not p.name.startswith("."):
            files.append(p)
    return files


def download_pdfs(urls: list[str], verbose: bool = True) -> list[Path]:
    """下载 URL 列表到 inbox/，返回已下载文件路径。"""
    INBOX.mkdir(parents=True, exist_ok=True)
    downloaded = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        name = Path(parsed.path).name
        if not name or not name.lower().endswith(".pdf"):
            # arxiv URL: https://arxiv.org/pdf/2401.12345 → 2401.12345.pdf
            m = re.search(r"(\d{4}\.\d{4,5})", url)
            name = f"{m.group(1)}.pdf" if m else f"downloaded-{len(downloaded)+1}.pdf"
        dest = INBOX / name
        # 下载前查重：arxiv ID 查图 + raw 目录，已存在则跳过
        arxiv_id = re.search(r"(\d{4}\.\d{4,5})", name)
        if arxiv_id and _already_ingested(arxiv_id.group(1)):
            if verbose:
                print(f"  ⊘ 跳过 {url}（arXiv:{arxiv_id.group(1)} 已在知识库）", flush=True)
            continue
        if verbose:
            print(f"  下载 {url} → inbox/{name}...", flush=True)
        result = subprocess.run(
            ["curl", "-sL", "-o", str(dest), url],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and dest.stat().st_size > 1000:
            downloaded.append(dest)
            if verbose:
                print(f"  ✓ {dest.stat().st_size} bytes")
        elif verbose:
            print(f"  ✗ 下载失败: {result.stderr[:200]}", file=sys.stderr)
    return downloaded


def _already_ingested(arxiv_id: str) -> bool:
    """查 arxiv ID 是否已在知识库（图 alias + raw source.yaml）。"""
    # 1. 查图：alias 或 node path 含该 arxiv ID
    try:
        import graph_lib as gl
        conn = gl.connect(gl.graph_db_for("academic/wiki/papers/x"))
        hit = conn.execute(
            "SELECT 1 FROM aliases WHERE alias LIKE ? LIMIT 1",
            (f"%{arxiv_id}%",),
        ).fetchone()
        if hit:
            return True
        hit = conn.execute(
            "SELECT 1 FROM nodes WHERE path LIKE ? OR title LIKE ? LIMIT 1",
            (f"%{arxiv_id}%", f"%{arxiv_id}%"),
        ).fetchone()
        conn.close()
        if hit:
            return True
    except Exception:
        pass
    # 2. 查 raw 目录：source.yaml external_path 含该 arxiv ID
    raw_refs = REPO / "academic" / "raw" / "references"
    if raw_refs.exists():
        for d in raw_refs.iterdir():
            sy = d / "source.yaml"
            if sy.is_file() and arxiv_id in sy.read_text(encoding="utf-8"):
                return True
    return False


def _extract_last_json(stdout: str) -> dict:
    """从子进程 stdout 中提取最后一个工作流结果 JSON。

    graph_report 内的 Hub/page 对象也可能带 active/current 等 status；这里只
    接受摄入工作流状态，避免把领域对象误当作最终返回体。
    """
    if not stdout:
        return {}
    fallback = {}
    terminal = {}
    saw_status = False
    workflow_statuses = {
        "completed", "duplicate_found", "agent_required", "failed",
        "type_mismatch", "partial", "error",
    }
    for idx in range(len(stdout)):
        if stdout[idx] != "{":
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(stdout[idx:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if fallback == {}:
            fallback = obj
        if "status" not in obj:
            continue
        saw_status = True
        if str(obj.get("status", "")).lower() in workflow_statuses:
            terminal = obj
    if terminal:
        return terminal
    return {} if saw_status else fallback


def _relation_hints(classified):
    """分析分类后的文件列表，输出可能的版本/补充关系提示。

    启发式：同类(document/meeting) + 文件名相似或含版本关键词(盖章/扫描/补充/v2/版本)
    → 提示 agent 可能是同一材料的版本/补充，考虑用 --related-to 关联摄入。
    纯提示，不自动判定关系——最终决策由 agent 基于语义判断。
    """
    if len(classified) < 2:
        return []
    hints = []
    # 同类型文件两两比较
    by_type = {}
    for f, ftype, rel in classified:
        by_type.setdefault(ftype, []).append((f, ftype, rel))
    for ftype, items in by_type.items():
        if len(items) < 2:
            continue
        for i, (f_a, _, rel_a) in enumerate(items):
            name_a = f_a.stem.lower()
            for f_b, _, rel_b in items[i+1:]:
                name_b = f_b.stem.lower()
                # 文件名相似度（共同词比例）
                words_a = set(name_a.replace("_", " ").replace("-", " ").split())
                words_b = set(name_b.replace("_", " ").replace("-", " ").split())
                common = words_a & words_b
                if not common:
                    continue
                similarity = len(common) / max(len(words_a), len(words_b))
                # 版本关键词
                has_version_a = any(kw in name_a for kw in VERSION_KEYWORDS)
                has_version_b = any(kw in name_b for kw in VERSION_KEYWORDS)
                if similarity >= 0.5 or (has_version_a or has_version_b):
                    primary = f_b.name if has_version_a else f_a.name
                    secondary = f_a.name if has_version_a else f_b.name
                    hints.append(
                        f"  ⚡ '{primary}' 与 '{secondary}' 文件名相似({similarity:.0%})，"
                        f"可能是同一材料的版本/补充。"
                        f"建议先摄入主文档，再用 --related-to 关联摄入次文档。"
                    )
    return hints


VERSION_KEYWORDS = ("盖章", "扫描", "补充", "v2", "版本", "修订", "签字", "正式版")


def _is_version_file(path: Path) -> bool:
    """判断文件名是否含版本/补充关键词，此类文件应排在其主文档之后。"""
    name = path.stem.lower()
    return any(kw in name for kw in VERSION_KEYWORDS)


def _plan_ingest_order(classified):
    """为多文档摄入制定执行顺序：主文档优先，版本/补充靠后。

    规则：
    1. 含版本关键词（盖章/扫描/补充/v2 等）的文件优先级降为 1，其余为 0
    2. 同优先级内保持原有排序（字母序/时间序）
    3. 版本文件紧随其相似的主文档之后（通过文件名共同词匹配）
    """
    if len(classified) < 2:
        return classified, []
    scored = [(0 if not _is_version_file(item[0]) else 1, i, item)
              for i, item in enumerate(classified)]
    scored.sort(key=lambda t: (t[0], t[1]))
    ordered = [item for _, _, item in scored]
    # 记录哪些文件被重排了
    original_names = [f.name for f, _, _ in classified]
    ordered_names = [f.name for f, _, _ in ordered]
    reorder_notes = []
    if original_names != ordered_names:
        version_files = [f.name for f, _, _ in ordered if _is_version_file(f)]
        if version_files:
            reorder_notes.append(f"版本/补充文件排后: {', '.join(version_files)}")
    return ordered, reorder_notes


def _map_paper_batch_results(parsed: dict) -> list[dict]:
    """把 ingest_paper.py --inbox 的 items 映射为统一结果条目。"""
    results = []
    if not isinstance(parsed, dict):
        return results
    items = parsed.get("items")
    if not isinstance(items, list):
        return results
    for item in items:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", ""))
        fname = source.rsplit("/", 1)[-1]
        status = item.get("status", "")
        entry = {"file": fname, "type": "paper", "ok": False, "status": status or "unknown"}
        if status == "completed":
            entry["ok"] = True
            for key in ("paper_id", "raw_dir", "wiki_path", "engine", "graph_report",
                        "transaction_id", "proposition_status", "proposition_details",
                        "quality_status", "quality_warnings"):
                if item.get(key) is not None:
                    entry[key] = item[key]
        elif status == "duplicate_found":
            entry["ok"] = True
            for key in ("paper_id", "transaction_id"):
                if item.get(key) is not None:
                    entry[key] = item[key]
        elif status in {"partial", "failed", "agent_required"}:
            errors = item.get("errors") or []
            if isinstance(errors, list):
                entry["reason"] = errors[0] if errors else status
            else:
                entry["reason"] = str(errors)
        else:
            entry["reason"] = str(item.get("errors") or status or "unknown")
        results.append(entry)
    return results


def _auto_resolve_abbreviations():
    """摄入后轻量自动消解裸缩写（代码驱动，零 LLM）。

    共享层 lightweight_abbr_resolve 做 alias 查询；
    摄入层额外做 raw 定义扫描 + 命题节点 apply。
    """
    # 层1：共享 alias 消解（与 query 后消解同一入口）
    base = ic.lightweight_abbr_resolve(REPO)

    # 层2：raw 定义扫描（仅摄入后，query 后跳过以保持轻量）
    todo_path = REPO / "cross-domain" / "abbreviation-todo.jsonl"
    if not todo_path.exists():
        return {**base, "prop_resolved": 0}

    entries = []
    try:
        for line in todo_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        entries = []

    resolved = []
    remaining = []
    for entry in entries:
        abbr = entry.get("object", "").strip()
        page = entry.get("page", "")
        if abbr and page:
            try:
                abbr_map = ic.load_raw_abbr_map(page)
                if abbr and abbr in abbr_map:
                    resolved.append({"abbr": abbr, "method": "raw_definition",
                                     "full_name": abbr_map[abbr]})
                    continue
            except Exception:
                pass
        remaining.append(entry)

    if resolved:
        with todo_path.open("w", encoding="utf-8") as handle:
            for entry in remaining:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 层3：命题节点消解（raw 有定义的自动建 keyword）
    prop_applied = 0
    try:
        result = subprocess.run(
            [sys.executable, str(REPO / ".scripts" / "resolve_abbreviations.py"), "--apply"],
            cwd=REPO, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "消解" in line and "→" in line:
                    prop_applied += 1
    except Exception:
        pass

    return {**base, "raw_resolved": len(resolved),
           "slot_resolved": base.get("resolved", 0) + len(resolved),
           "slot_remaining": len(remaining),
           "prop_resolved": prop_applied}


def _auto_create_hubs(session_id: str) -> dict:
    """摄入末期自动建 Hub 检查：纯代码分析达标候选，触发 agent 处理。

    调 hub_semantics auto-create --check，达标候选写文件供 agent 读取；
    不达标候选静默进 backlog，不向用户报告。
    """
    try:
        result = subprocess.run(
            [sys.executable, str(REPO / ".scripts" / "hub_semantics.py"),
             "auto-create", "--check"],
            cwd=REPO, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"status": "error", "error": result.stderr[:300]}
        check = json.loads(result.stdout)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if check.get("status") != "agent_required":
        return {
            "status": "no_action",
            "backlog_count": check.get("backlog_count", 0),
        }
    eligible = check.get("eligible", [])
    hub_dir = REPO / "temp" / "hub-auto-create"
    hub_dir.mkdir(parents=True, exist_ok=True)
    candidates_file = hub_dir / f"{session_id}.json"
    candidates_file.write_text(
        json.dumps(eligible, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "agent_required",
        "candidates_file": str(candidates_file.relative_to(REPO)),
        "eligible_count": len(eligible),
        "backlog_count": check.get("backlog_count", 0),
    }


def _run_post_ingest_maintenance(results: list[dict], session_id: str) -> tuple[dict, dict, dict]:
    """Only scan global backlogs when this run actually changed the knowledge base."""
    if not any(item.get("ok") for item in results):
        skipped = {"status": "skipped", "reason": "no_successful_files"}
        return skipped, skipped.copy(), skipped.copy()

    abbr_summary = _auto_resolve_abbreviations()
    abbr_summary["scope"] = "global_backlog_after_ingest"
    people_summary = ic.detect_people_page_candidates(REPO)
    try:
        from build_people_pages import build_pending_people
        people_summary["auto_built"] = build_pending_people()
    except Exception as exc:
        people_summary["auto_built"] = {"error": str(exc)}
    return abbr_summary, people_summary, _auto_create_hubs(session_id)


def _compact_summary(report: dict, report_path: Path) -> dict:
    """构造给 Agent 的稳定小结果；完整逐文件诊断只保留在 report。"""
    item_statuses = {item.get("status") for item in report["files"]}
    if "failed" in item_statuses or "error" in item_statuses:
        status = "failed"
    elif "classification_required" in item_statuses:
        status = "classification_required"
    elif "agent_required" in item_statuses:
        status = "agent_required"
    elif "partial" in item_statuses or report["skipped"]:
        status = "partial"
    else:
        status = "completed"
    files = []
    for item in report["files"]:
        compact = {"file": item.get("file"), "status": item.get("status", "unknown")}
        for key in ("quality_status", "reason", "transaction_id"):
            if item.get(key) is not None:
                compact[key] = item[key]
        files.append(compact)
    compact = {
        "status": status,
        "total": report["total"],
        "completed": report["completed"],
        "degraded": report["degraded"],
        "failed": report["failed"],
        "skipped": report["skipped"],
        "report_path": str(report_path.relative_to(REPO)),
        "files": files,
    }
    return compact


def main():
    ap = argparse.ArgumentParser(description="inbox 统一摄入入口：程序分流 + 边界 API 复核")
    ap.add_argument("--file", help="指定单个文件（不扫描 inbox 全部）")
    ap.add_argument("--run", action="store_true", help="实际执行分发（默认 dry run）")
    ap.add_argument("--subproject", default="academic",
                    choices=["academic", "admin", "teaching", "business"],
                    help="meeting/document 的存储域（默认 academic）")
    ap.add_argument("--download", nargs="+", metavar="URL",
                    help="下载 PDF URL 到 inbox/ 再处理")
    args = ap.parse_args()

    # 下载模式
    if args.download:
        files = download_pdfs(args.download, verbose=not args.run)
        if not files:
            print("无文件下载成功", file=sys.stderr)
            sys.exit(1)
    elif args.file:
        f = (REPO / args.file).resolve()
        if not f.is_file():
            print(f"文件不存在: {args.file}", file=sys.stderr)
            sys.exit(1)
        files = [f]
    else:
        files = scan_inbox()

    if not files:
        print("inbox/ 无待摄入文件")
        return

    # 分类；--run 的 stdout 留给最终紧凑摘要。
    if not args.run:
        print(f"{'文件':<40} {'类型':<10} {'分发':<20}")
        print("-" * 75)
    classified = []
    academic_document_types: dict[str, str | None] = {}
    classification_details: dict[str, dict] = {}
    classification_reviews: dict[str, dict] = {}
    classification_blocks: dict[str, str] = {}
    for f in files:
        rel = str(f.relative_to(REPO))
        decision = classify_file_details(f)
        ftype = decision["file_type"]
        classification_details[rel] = {
            key: value for key, value in decision.items() if key != "review_text"
        }
        if args.run and decision.get("needs_api_review"):
            review = review_low_confidence_classification(f, decision)
            classification_reviews[rel] = review
            confirmed, reason = reconcile_classification(decision, review)
            if not confirmed:
                classification_blocks[rel] = reason
        document_type = None
        if ftype == "document" and args.subproject == "academic":
            document_type = classify_academic_document(f)
            academic_document_types[rel] = document_type
        script = {"paper": "ingest_paper.py", "meeting": "ingest_meeting.py",
                  "document": "ingest_document.py"}[ftype]
        if not args.run:
            display_type = (f"document:{document_type}" if document_type else
                            "classification_required" if ftype == "document" and args.subproject == "academic"
                            else ftype)
            if decision.get("needs_api_review"):
                display_type = f"{display_type}?({decision['score']}/{decision['threshold']})"
            print(f"{f.name:<40} {display_type:<24} {script:<20}")
        classified.append((f, ftype, rel))

    if not args.run:
        print(f"\n共 {len(classified)} 个文件。加 --run 执行分发。")
        # 关联提示：帮助 agent 识别版本/补充关系，制定关联摄入计划
        hints = _relation_hints(classified)
        if hints:
            print("\n--- 关联提示（供 agent 制定摄入计划）---")
            for hint in hints:
                print(hint)
        return

    # 多文档计划：主文档优先，版本/补充靠后（改进 3）
    classified, plan_notes = _plan_ingest_order(classified)

    # 执行分发：经 DSH guard/session/tool seam 调用底层 ingest_* 脚本
    from dsh.agent_loop import IngestAgentLoop
    loop = IngestAgentLoop(mode="ingest_inbox")
    results = []
    tool_outputs = []
    paper_batch = (not args.file and len(classified) > 1 and
                   not classification_blocks and
                   all(ftype == "paper" for _, ftype, _ in classified))
    if paper_batch:
        content = loop.execute("ingest_paper_inbox", {})
        tool_outputs.append({"tool": "ingest_paper_inbox", "output": content})
        parsed = loop.last_structured or _extract_last_json(content)
        results = _map_paper_batch_results(parsed)
        if not results:
            reason = "批量入口未返回 items"
            if isinstance(parsed, dict):
                reason = str(parsed.get("errors") or parsed.get("status") or reason)
            results.append({"file": "inbox paper batch", "type": "paper", "ok": False,
                            "status": "failed", "reason": reason})
    else:
        for f, ftype, rel in classified:
            if rel in classification_blocks:
                review = classification_reviews.get(rel, {})
                entry = {
                    "file": f.name,
                    "type": ftype,
                    "ok": False,
                    "skipped": True,
                    "status": "classification_required",
                    "reason": classification_blocks[rel],
                    "program_classification": classification_details.get(rel, {}),
                    "api_classification": review,
                }
                results.append(entry)
                loop.session_log.append("ingest/skip", {
                    "file": rel,
                    "status": "classification_required",
                    "reason": entry["reason"],
                    "program_classification": entry["program_classification"],
                    "api_classification": review,
                })
                continue
            document_type = academic_document_types.get(rel)
            if ftype == "document" and args.subproject == "academic" and not document_type:
                entry = {
                    "file": f.name,
                    "type": ftype,
                    "ok": False,
                    "skipped": True,
                    "status": "classification_required",
                    "reason": "academic 非论文文档缺少强分类信号；请显式选择 editorial 或 academic-reference",
                }
                results.append(entry)
                loop.session_log.append("ingest/skip", {
                    "file": rel,
                    "status": "classification_required",
                    "reason": entry["reason"],
                })
                continue
            tool_name, tool_args = dsi_tool(
                ftype, rel, args.subproject, document_type=document_type)
            content = loop.execute(tool_name, tool_args)
            tool_outputs.append({"file": f.name, "tool": tool_name, "output": content})
            # 优先使用 DSH 解析出的结构化结果；失败/拒绝时回退字符串判定
            parsed = loop.last_structured or _extract_last_json(content)
            if parsed:
                parsed_status = parsed.get("status", "")
                if parsed_status == "type_mismatch":
                    skipped_type = parsed.get("errors", [""])[0] if parsed.get("errors") else "type_mismatch"
                    results.append({"file": f.name, "type": ftype, "ok": False, "skipped": True,
                                    "reason": skipped_type, "status": parsed_status})
                elif parsed_status in {"completed", "duplicate_found"}:
                    entry = {"file": f.name, "type": ftype, "ok": True, "status": parsed_status}
                    for key in ("paper_id", "admin_id", "raw_dir", "wiki_path", "engine",
                                "graph_report", "transaction_id", "proposition_status",
                                "proposition_details", "quality_status", "quality_warnings"):
                        if parsed.get(key) is not None:
                            entry[key] = parsed[key]
                    results.append(entry)
                elif parsed_status == "classification_required":
                    reason = (parsed.get("errors") or ["需要显式分类"])[0]
                    results.append({"file": f.name, "type": ftype, "ok": False,
                                    "skipped": True, "status": parsed_status, "reason": reason})
                elif parsed_status in {"agent_required", "failed", "partial"}:
                    if parsed_status == "agent_required":
                        reason = "agent 接管"
                    elif parsed_status == "partial":
                        reason = "部分完成"
                    else:
                        reason = parsed.get("errors", ["failed"])[0] if parsed.get("errors") else "failed"
                    results.append({"file": f.name, "type": ftype, "ok": False,
                                    "status": parsed_status, "reason": reason})
                else:
                    results.append({"file": f.name, "type": ftype, "ok": False,
                                    "status": parsed_status or "unknown", "reason": content[:200]})
            else:
                reason = (content.splitlines()[0][:200] if content and content.splitlines()
                          else "DSH 无结构化输出")
                results.append({"file": f.name, "type": ftype, "ok": False,
                                "status": "failed", "reason": reason})

    # 持久化 DSH session log，保证 guard/tool 执行可审计
    dsh_dir = REPO / "temp" / "inbox-dsh"
    dsh_dir.mkdir(parents=True, exist_ok=True)
    dsh_log = dsh_dir / f"{loop.session_log.session_id}.jsonl"
    dsh_log.write_text(loop.session_log.to_jsonl() + "\n", encoding="utf-8")

    # 只有本次真正摄入成功才扫描全局 backlog。分类闸门/全失败必须快速返回。
    abbr_summary, people_summary, hub_summary = _run_post_ingest_maintenance(
        results, loop.session_log.session_id)

    # 摄入报告持久化：复盘用（引擎选择、逐文件状态、建图统计）
    report_dir = REPO / "cross-domain" / "ingest-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_time = datetime.now()
    report_path = report_dir / f"{report_time.strftime('%Y%m%d-%H%M%S')}.json"
    report = {
        "timestamp": report_time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": loop.session_log.session_id,
        "dsh_log": str(dsh_log.relative_to(REPO)),
        "total": len(results),
        "completed": len([r for r in results if r["ok"]]),
        "degraded": len([r for r in results if r.get("quality_status") == "degraded"]),
        "failed": len([r for r in results if not r["ok"] and not r.get("skipped")]),
        "skipped": len([r for r in results if r.get("skipped")]),
        "files": results,
        "classification_decisions": classification_details,
        "classification_reviews": classification_reviews,
        "plan_notes": plan_notes,
        "tool_outputs": tool_outputs,
        "auto_resolve_abbreviations": abbr_summary,
        "people_page_candidates": people_summary,
        "hub_auto_create": hub_summary,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compact = _compact_summary(report, report_path)
    print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    if report["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ingest_inbox.py — inbox 统一摄入入口：确定性分流 + 双后端语义裁决。

扫描 inbox/ 下的文件，按扩展名+内容关键词分类，分发到对应摄入脚本：
  - PDF + 学术特征（Abstract/References/arXiv/DOI）→ ingest_paper.py
  - PDF 非学术 → ingest_document.py
  - .txt + 会议特征（会议/参会/元宝会议助手/时间戳）→ ingest_meeting.py
  - .txt 非会议 / .docx / .doc / .pptx / .md → ingest_document.py；会议速记保留 meeting source_kind

分类先由 Python 完成（pymupdf 前2页 + 关键词评分）。`--run` 遇到不确定
样本时，Agent backend 返回单个批量任务，API backend 调用受限分类器；高置信度文件
和 dry-run 都不触发语义裁决。

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
import shlex
import subprocess
import sys
import urllib.parse
import unicodedata
from datetime import datetime
from pathlib import Path

ACADEMIC_EDITORIAL_MARKERS = (
    "专题导言", "特邀编辑", "本期专题", "编者按", "guest editorial", "guest editor",
)
ACADEMIC_CONFERENCE_NAME_RE = re.compile(
    r"会议|研讨会|论坛|年会|conference|workshop|symposium", re.I,
)
ACADEMIC_CONFERENCE_RECORD_MARKERS = (
    "信息整理", "会议信息", "会议记录", "会议总结", "会议议程", "会议通知",
    "会议安排", "参会信息", "资料汇总", "日程安排", "会务", "meeting record",
    "conference notes",
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

MEETING_TRANSCRIPT_NAME_RE = re.compile(r"会议|部署会|工作会|座谈会|研讨会|交流会")
TRANSCRIPT_NAME_RE = re.compile(r"速记|逐字稿|转写")
MEETING_TRANSCRIPT_BODY_PATTERNS = (
    ("body_transcript", r"速记|逐字稿|会议转写"),
    ("body_meeting", r"会议|参会|主持"),
    ("body_speaker", r"校长讲话|书记讲话|院长讲话|主任讲话|\b发言\b|汇报"),
)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ingest_common as ic
import agent_task
import inbox_plan
import ingest_user_assertions
import source_fingerprints as sf
import trash_util
INBOX = REPO / "inbox"
SKIP_FILES = {".gitkeep", ".DS_Store"}


def call_json(*args, **kwargs):
    """API-only adapter kept patchable for classification tests."""
    from llm_structured import call_json as api_call_json
    return api_call_json(*args, **kwargs)


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


def read_document_preview(path: Path, max_chars: int = 8000) -> str:
    """Read enough local document text to classify its source kind without an API call."""
    suffix = path.suffix.lower()
    try:
        if suffix in {".txt", ".md"}:
            return path.read_text(encoding="utf-8")[:max_chars]
        if suffix in {".docx", ".doc"}:
            result = subprocess.run(
                ["textutil", "-convert", "txt", "-stdout", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout[:max_chars] if result.returncode == 0 else ""
        if suffix == ".pptx":
            result = subprocess.run(
                ["pandoc", "-t", "plain", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout[:max_chars] if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return ""


def meeting_transcript_markers(path: Path, text: str) -> list[str]:
    """Return only strong, auditable transcript markers."""
    name = path.stem
    markers = _matched_markers(text, MEETING_TRANSCRIPT_BODY_PATTERNS)
    name_meeting = bool(MEETING_TRANSCRIPT_NAME_RE.search(name))
    name_transcript = bool(TRANSCRIPT_NAME_RE.search(name))
    if name_meeting:
        markers.append("filename_meeting")
    if name_transcript:
        markers.append("filename_transcript")
    strong = (
        name_meeting and name_transcript
        or name_transcript and "body_meeting" in markers
        or {"body_transcript", "body_meeting", "body_speaker"} <= set(markers)
    )
    return list(dict.fromkeys(markers)) if strong else []


def classify_file_details(path: Path) -> dict:
    """返回程序类型、得分、命中标记及是否需要 API 裁决。"""
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
                "source_kind": "ordinary",
            }
        markers = _matched_markers(text, ACADEMIC_PDF_PATTERNS)
        score = len(markers)
        return {
            "file_type": "paper" if score >= 3 else "document",
            "score": score,
            "threshold": 3,
            "markers": markers,
            "confidence": "low" if score in {1, 2, 3} else "high",
            "needs_api_review": score in {1, 2, 3},
            "review_text": text[:8000],
            "source_kind": "ordinary",
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
            "source_kind": "meeting" if score >= 2 else "ordinary",
        }
    if suffix in {".docx", ".doc", ".pptx", ".md"}:
        text = read_document_preview(path)
        transcript_markers = meeting_transcript_markers(path, text)
        if transcript_markers:
            return {
                "file_type": "document",
                "score": len(transcript_markers),
                "threshold": 2,
                "markers": transcript_markers,
                "confidence": "high",
                "needs_api_review": False,
                "review_text": text,
                "source_kind": "meeting",
            }
    return {
        "file_type": "document", "score": None, "threshold": None,
        "markers": [f"extension:{suffix or 'none'}"], "confidence": "high",
        "needs_api_review": False, "review_text": "", "source_kind": "ordinary",
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
    """用受限 API 分类器裁决不确定样本；不读知识库、不写任何持久层。"""
    if agent_task.ingest_backend() != "api":
        raise RuntimeError("Agent backend 不调用 API 分类器")
    allowed_types = ({"paper", "document"} if path.suffix.lower() == ".pdf"
                     else {"meeting", "document"})
    allowed = "|".join(sorted(allowed_types))
    prompt = f"""你是受程序约束的文档类型裁决组件。只判断来源类型，不生成 Wiki 或三元组。

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
        system="你是文档类型裁决组件，只输出符合契约的 JSON。",
    )
    if not result.get("ok"):
        return {"status": "review_error", "error": result.get("error", "API 分类复核失败"),
                "classification_transaction_id": review_id}
    review = dict(result.get("parsed") or {})
    if review.get("doc_type") not in allowed_types | {"ambiguous"}:
        return {
            "status": "review_error",
            "error": f"API 分类类型不适用于 {path.suffix.lower()}: {review.get('doc_type')}",
            "classification_transaction_id": review_id,
        }
    review["status"] = "ok"
    review["classification_transaction_id"] = review_id
    return review


def reconcile_classification(decision: dict, review: dict) -> tuple[str | None, str]:
    """用 API 或当前 Agent 的中高置信度结果裁决程序不确定样本。"""
    if review.get("status") != "ok":
        return None, f"分类复核失败: {review.get('error', 'unknown')}"
    api_type = review.get("doc_type")
    if api_type == "ambiguous":
        return None, "分类复核认为证据不足（ambiguous）"
    if review.get("confidence") == "low":
        return None, "分类复核置信度仍为 low"
    return str(api_type), ""


def _classification_task(pending: list[dict], args, issues: list | None = None) -> dict:
    identity = [
        {"file": item["file"], "sha256": hashlib.sha256(
            item["review_text"].encode("utf-8")
        ).hexdigest()}
        for item in pending
    ]
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    directory = REPO / "temp" / "inbox-classification"
    directory.mkdir(parents=True, exist_ok=True)
    input_path = directory / f"{digest}-input.json"
    output_path = directory / f"{digest}-result.json"
    input_path.write_text(json.dumps({
        "schema": "inbox-classification-input-v1",
        "items": pending,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rerun = ["python3", ".scripts/ingest_inbox.py", "--run", "--subproject", args.subproject]
    if args.file:
        rerun.extend(["--file", args.file])
    if getattr(args, "ocr_result", None):
        rerun.extend(["--ocr-result", args.ocr_result])
    if getattr(args, "allow_remote_ocr", False):
        rerun.append("--allow-remote-ocr")
    rerun.extend(["--classification-file", str(output_path.relative_to(REPO))])
    return agent_task.make_task(
        kind="inbox_classification",
        transaction_id=f"inbox-classification-{digest}",
        inputs=[{
            "name": "classification_candidates",
            "path": str(input_path.relative_to(REPO)),
            "role": "program_scored_bounded_source_text",
            "read": "full",
        }],
        outputs=[{
            "name": "classification_decisions",
            "path": str(output_path.relative_to(REPO)),
            "format": "inbox-classification-result-v1",
        }],
        protocol={
            "name": "inbox-classification-result-v1",
            "shape": {"decisions": [{
                "file": "input file",
                "doc_type": "allowed_types item or ambiguous",
                "confidence": "high|medium|low",
                "reasons": "1-4 strings",
                "evidence_quotes": "0-4 exact source quotes",
            }]},
            "validator": "ingest_inbox.classification_review_schema",
        },
        issues=list(issues or []),
        commands={"resume": "INGEST_BACKEND=agent " + " ".join(
            shlex.quote(part) for part in rerun
        )},
    )


def _load_agent_classifications(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(decisions, list):
        raise ValueError("classification result 缺少 decisions")
    result = {}
    for decision in decisions:
        file_name = str(decision.get("file") or "") if isinstance(decision, dict) else ""
        if not file_name or file_name in result or not classification_review_schema(decision):
            raise ValueError("classification decision 不符合 schema 或 file 重复")
        result[file_name] = {**decision, "status": "ok"}
    return result


def _validate_agent_classification(path: Path, decision: dict, review: dict) -> None:
    allowed_types = ({"paper", "document"} if path.suffix.lower() == ".pdf"
                     else {"meeting", "document"})
    if review.get("doc_type") not in allowed_types | {"ambiguous"}:
        raise ValueError(f"{path}: doc_type 不适用于来源格式")
    source = _normalized_evidence_text(decision.get("review_text", ""))
    if any(
            quote and _normalized_evidence_text(quote) not in source
            for quote in review.get("evidence_quotes", [])
    ):
        raise ValueError(f"{path}: evidence_quotes 不在分类输入中")


def _normalized_evidence_text(value: str) -> str:
    """Normalize representation noise while preserving lexical evidence."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.split())


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
    conference_title = path.stem
    heading = re.search(r"^#\s+(.+)$", text, re.M)
    if heading:
        conference_title += "\n" + heading.group(1)
    conference_title = conference_title.lower()
    if (ACADEMIC_CONFERENCE_NAME_RE.search(conference_title)
            and any(marker in conference_title for marker in ACADEMIC_CONFERENCE_RECORD_MARKERS)):
        return "conference-summary"
    return None


def dispatch_command(file_type: str, rel_path: str, subproject: str,
                     document_type: str | None = None,
                     source_kind: str = "ordinary", *, ocr_result: str | None = None,
                     allow_remote_ocr: bool = False) -> list[str]:
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
    if source_kind != "ordinary":
        command.extend(["--source-kind", source_kind])
    if ocr_result:
        command.extend(["--ocr-result", ocr_result])
    if allow_remote_ocr:
        command.append("--allow-remote-ocr")
    return command


def dsi_tool(file_type: str, rel_path: str, subproject: str,
             document_type: str | None = None,
             source_kind: str = "ordinary", *, ocr_result: str | None = None,
             allow_remote_ocr: bool = False) -> tuple[str, dict]:
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
    if source_kind != "ordinary":
        args["source_kind"] = source_kind
    if ocr_result:
        args["ocr_result"] = ocr_result
    if allow_remote_ocr:
        args["allow_remote_ocr"] = True
    return "ingest_document_file", args


def scan_inbox() -> list[Path]:
    """扫描 inbox/，仅在 facts-pending.md 含事实条目时纳入处理。"""
    if not INBOX.is_dir():
        return []
    files = []
    for p in sorted(INBOX.iterdir()):
        if not p.is_file() or p.name in SKIP_FILES or p.name.startswith("."):
            continue
        if p.name == "facts-pending.md" and inbox_plan.fact_entries(p) == 0:
            continue
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
        "completed", "duplicate_found", "prepared", "agent_required", "failed",
        "type_mismatch", "classification_required", "bibliographic_review_required",
        "validation_error", "partial", "error",
    }
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(stdout):
        idx = stdout.find("{", cursor)
        if idx < 0:
            break
        try:
            obj, end = decoder.raw_decode(stdout[idx:])
        except json.JSONDecodeError:
            cursor = idx + 1
            continue
        cursor = idx + end
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
                        "bibliographic_worker", "workspace_worker", "relationship_worker",
                        "semantic_repair_worker", "execution_backend",
                        "quality_status", "quality_warnings"):
                if item.get(key) is not None:
                    entry[key] = item[key]
        elif status == "duplicate_found":
            entry["ok"] = True
            for key in ("paper_id", "transaction_id"):
                if item.get(key) is not None:
                    entry[key] = item[key]
        elif status in {
            "partial", "failed", "prepared", "agent_required", "bibliographic_review_required",
            "validation_error", "classification_required",
        }:
            errors = item.get("errors") or []
            if isinstance(errors, list):
                entry["reason"] = (
                    errors[0] if errors else
                    "API worker 自动恢复耗尽，等待宿主 Agent 修正暂存产物"
                    if status == "agent_required" and item.get("execution_backend") == "api"
                    else status
                )
            else:
                entry["reason"] = str(errors)
            for key in (
                "transaction_id", "retryable", "next_action", "resume_from",
                "failure_signature", "bibliographic_review", "agent_task", "write_to",
                "workspace_worker", "execution_backend", "failure_disposition",
            ):
                if item.get(key) is not None:
                    entry[key] = item[key]
        else:
            entry["reason"] = str(item.get("errors") or status or "unknown")
        results.append(entry)
    return results


def _result_entry(file_name: str, file_type: str, parsed: dict, content: str = "") -> dict:
    status = str(parsed.get("status") or "unknown") if isinstance(parsed, dict) else "unknown"
    if status == "type_mismatch":
        errors = parsed.get("errors") or ["type_mismatch"]
        return {"file": file_name, "type": file_type, "ok": False, "skipped": True,
                "reason": errors[0], "status": status}
    if status in {"completed", "duplicate_found"}:
        entry = {"file": file_name, "type": file_type, "ok": True, "status": status}
        for key in (
            "paper_id", "admin_id", "raw_dir", "wiki_path", "engine",
            "graph_report", "transaction_id", "proposition_status",
            "proposition_details", "bibliographic_worker", "workspace_worker",
            "relationship_worker", "execution_backend",
            "semantic_repair_worker", "quality_status", "quality_warnings",
        ):
            if parsed.get(key) is not None:
                entry[key] = parsed[key]
        return entry
    if status == "classification_required":
        errors = parsed.get("errors") or ["需要显式分类"]
        return {"file": file_name, "type": file_type, "ok": False, "skipped": True,
                "status": status, "reason": errors[0]}
    pending_statuses = {
        "prepared", "agent_required", "partial", "bibliographic_review_required",
        "validation_error", "graph_ready",
    }
    if status in pending_statuses:
        errors = parsed.get("errors") or []
        review_error = (parsed.get("bibliographic_review") or {}).get("error", "")
        reason = (
            "Agent task prepared" if status == "prepared" else
            (
                "API worker 自动恢复耗尽，等待宿主 Agent 修正暂存产物"
                if parsed.get("execution_backend") == "api"
                else "等待宿主 Agent 接管暂存任务"
            ) if status == "agent_required" else
            "部分完成" if status == "partial" else
            errors[0] if errors else review_error or status
        )
        entry = {"file": file_name, "type": file_type, "ok": False,
                 "status": status, "reason": reason}
        for key in (
            "transaction_id", "errors", "retryable", "next_action", "resume_from",
            "failure_signature", "bibliographic_review", "agent_task", "write_to",
            "failure_disposition", "workflow_status", "internal_status",
            "workspace_worker", "execution_backend",
        ):
            if parsed.get(key) is not None:
                entry[key] = parsed[key]
        return entry
    reason = (
        content.splitlines()[0][:200]
        if content and content.splitlines() else str(parsed.get("errors") or status)
    )
    return {"file": file_name, "type": file_type, "ok": False,
            "status": status, "reason": reason}


def _write_json_atomic(path: Path, value: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp_path.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_duplicate_path(match: dict) -> Path:
    raw_value = Path(str(match.get("raw_path") or ""))
    if not raw_value.parts or raw_value.is_absolute():
        raise ValueError("fingerprint raw_path 必须是仓库相对路径")
    raw_path = (REPO / raw_value).resolve()
    try:
        relative = raw_path.relative_to(REPO.resolve())
    except ValueError as exc:
        raise ValueError("fingerprint raw_path 越出仓库") from exc
    if len(relative.parts) < 3 or relative.parts[1] != "raw":
        raise ValueError("fingerprint raw_path 不在受管 raw/ 目录")
    if not raw_path.is_file():
        raise ValueError("fingerprint 指向的 Raw 文件不存在")
    return raw_path


def cleanup_exact_duplicate(source: Path, match: dict) -> dict:
    """Reverify an exact duplicate, write a receipt, then move it to Trash."""
    expected_hash = str(match.get("binary_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("fingerprint 缺少合法 binary_sha256")
    repo_root = REPO.resolve()
    source_path = source.resolve()
    try:
        source_relative = source_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("重复源文件越出仓库") from exc
    if len(source_relative.parts) < 2 or source_relative.parts[0] != "inbox":
        raise ValueError("重复源文件必须位于 inbox/")
    if not source_path.is_file():
        raise ValueError("重复源文件不存在")
    raw_path = _raw_duplicate_path(match)
    source_hash = _sha256_file(source_path)
    raw_hash = _sha256_file(raw_path)
    if source_hash != expected_hash or raw_hash != expected_hash:
        raise ValueError("清理前 source/Raw SHA-256 复核不一致")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-") or "duplicate"
    receipt_path = REPO / "temp" / "inbox-duplicate-receipts" / (
        f"{timestamp}-{safe_stem}-{expected_hash[:12]}.json"
    )
    receipt = {
        "schema": "exact-duplicate-cleanup-v1",
        "status": "verified",
        "source": str(source_relative),
        "raw_path": str(raw_path.relative_to(repo_root)),
        "binary_sha256": expected_hash,
        "verified_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json_atomic(receipt_path, receipt)
    try:
        trash_util.trash_path(source_path)
        if source_path.exists():
            raise RuntimeError("trash 返回后源文件仍存在")
    except Exception as exc:
        receipt["status"] = "failed"
        receipt["errors"] = [str(exc)]
        _write_json_atomic(receipt_path, receipt)
        raise RuntimeError(f"精确重复清理失败: {exc}") from exc
    receipt["status"] = "trashed"
    receipt["trashed_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json_atomic(receipt_path, receipt)
    return {
        "status": "trashed",
        "receipt_path": str(receipt_path.relative_to(REPO)),
    }


def user_assertions_handoff(path: Path) -> dict:
    result = ingest_user_assertions.prepare_transaction(path=path, repo=REPO)
    count = result.get("fact_entries", inbox_plan.fact_entries(path))
    result.update({
        "file": path.name,
        "type": "user-assertions",
        "reason": (
            f"facts-pending.md 含 {count} 条用户申明事实；"
            "受管事务已准备，等待填写受限语义提案"
        ),
        "failure_disposition": {
            "category": "semantic_decision",
            "domain": "semantic",
            "disposition": "specialist_review",
            "retryable": False,
            "owner": "primary_agent",
            "next_action": result.get("next_action", "complete_agent_task"),
            "fingerprints": [result.get("transaction_id", "")],
        },
    })
    return result


def _report_counts(results: list[dict]) -> dict:
    pending_statuses = {
        "prepared", "agent_required", "partial", "bibliographic_review_required",
        "validation_error", "graph_ready",
    }
    return {
        "completed": sum(item.get("status") == "completed" for item in results),
        "duplicates": sum(item.get("status") == "duplicate_found" for item in results),
        "awaiting_agent": sum(
            item.get("status") in {"prepared", "agent_required"} for item in results
        ),
        "pending": sum(item.get("status") in pending_statuses for item in results),
        "degraded": sum(item.get("quality_status") == "degraded" for item in results),
        "failed": sum(
            item.get("status") in {"failed", "error", "unknown"}
            or (
                not item.get("ok")
                and not item.get("skipped")
                and item.get("status") not in pending_statuses
            )
            for item in results
        ),
        "skipped": sum(bool(item.get("skipped")) for item in results),
    }


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-.") or "session"


def _auto_resolve_abbreviations(session_id: str) -> dict:
    """Resolve the bounded abbreviation backlog and emit typed Agent review."""
    base_before = ic.lightweight_abbr_resolve(REPO)
    todo_path = REPO / "cross-domain" / "abbreviation-todo.jsonl"
    errors = list(base_before.get("errors", []))
    resolver_report: dict = {
        "status": "completed", "resolved": [], "resolved_count": 0,
        "warning_count": 0, "candidates": [],
    }
    command = [
        sys.executable, str(REPO / ".scripts" / "resolve_abbreviations.py"),
        "--apply", "--todo", str(todo_path), "--json",
    ]
    try:
        result = subprocess.run(
            command, cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        try:
            parsed = json.loads(result.stdout)
            if not isinstance(parsed, dict):
                raise ValueError("resolver JSON must be an object")
            resolver_report = parsed
        except (json.JSONDecodeError, ValueError) as exc:
            errors.append(f"abbreviation resolver invalid JSON: {exc}")
        if result.returncode != 0:
            detail = result.stderr.strip() or str(resolver_report.get("errors") or "")
            errors.append(f"abbreviation resolver exit {result.returncode}: {detail[:300]}")
    except subprocess.TimeoutExpired:
        errors.append("abbreviation resolver timed out after 120s")
    except OSError as exc:
        errors.append(f"abbreviation resolver failed: {exc}")

    base_after = ic.lightweight_abbr_resolve(REPO)
    errors.extend(base_after.get("errors", []))
    candidates = resolver_report.get("candidates", [])
    review_file = ""
    if candidates:
        review_path = (
            REPO / "temp" / "abbreviation-review" /
            f"{_safe_session_id(session_id)}.json"
        )
        try:
            _write_json_atomic(review_path, {
                "schema": "abbreviation-review-v1",
                "session_id": session_id,
                "allowed_resolution_kinds": [
                    "alias_to_full_name", "canonical_name", "unit_or_standard",
                    "dataset_or_model", "ambiguous",
                ],
                "candidate_token_count": len(candidates),
                "candidate_occurrence_count": sum(
                    len(item.get("occurrences", [])) for item in candidates
                ),
                "candidates": candidates,
            })
            review_file = str(review_path.relative_to(REPO))
        except Exception as exc:
            errors.append(f"abbreviation review write failed: {exc}")

    summary = {
        "status": "error" if errors else "agent_required" if candidates else "completed",
        "alias_resolved": (
            int(base_before.get("resolved", 0)) + int(base_after.get("resolved", 0))
        ),
        "prop_resolved": int(resolver_report.get("resolved_count", 0)),
        "remaining": int(base_after.get("remaining", 0)),
        "remaining_tokens": len(candidates),
        "remaining_occurrences": int(base_after.get("remaining", 0)),
        "warning_count": int(resolver_report.get("warning_count", len(candidates))),
    }
    if review_file:
        summary.update({
            "review_file": review_file,
            "next_action": "agent_review_abbreviation_candidates",
        })
    if errors:
        summary["errors"] = errors
    return summary


def _hub_affected_nodes(results: list[dict]) -> list[str]:
    nodes = []
    for item in results:
        dynamics = ((item.get("graph_report") or {}).get("hub_dynamics") or {})
        nodes.extend(str(node) for node in dynamics.get("affected_nodes", []) if node)
    return list(dict.fromkeys(nodes))


def _hub_route_reviews(results: list[dict]) -> list[dict]:
    """Extract low-margin canonical routes for strong-agent review."""
    reviews = []
    for item in results:
        graph_report = item.get("graph_report") or {}
        current_route = graph_report.get("hub_scope_route_current") or {}
        if (current_route.get("decision") == "resolved"
                and current_route.get("reason") == "agent_confirmed_override"):
            continue
        route = graph_report.get("hub_scope_route") or {}
        if (route.get("decision") != "candidates"
                or route.get("reason") not in {
                    "scope_margin_too_small", "child_specificity_unsupported",
                }):
            continue
        canonical = [
            {
                key: candidate.get(key)
                for key in ("path", "title", "scope", "score", "canonical")
                if candidate.get(key) is not None
            }
            for candidate in route.get("candidates", [])
            if candidate.get("canonical")
        ]
        reviews.append({
            "file": item.get("file", ""),
            "wiki_path": item.get("wiki_path", ""),
            "transaction_id": item.get("transaction_id", ""),
            "decision": "agent_route_review_required",
            "reason": route.get("reason"),
            "top_score": route.get("top_score"),
            "margin": route.get("margin"),
            "profile": route.get("profile", {}),
            "canonical_candidates": canonical,
            "apply_command_template": (
                "python3 .scripts/hub_semantics.py route-apply "
                f"--page '{item.get('wiki_path', '')}' "
                "--hub '<agent-selected-canonical-hub>' --agent-confirmed "
                f"--transaction-id '{item.get('transaction_id', '')}'"
            ),
        })
    return reviews


def _auto_create_hubs(session_id: str, results: list[dict] | None = None) -> dict:
    """摄入末期 Hub 检查：把新建与分裂候选交给主 Agent。

    调 hub_semantics auto-create --check，达标候选分别写文件供 agent 读取；
    不达标候选静默进 backlog，不向用户报告。
    """
    route_reviews = _hub_route_reviews(results or [])
    affected_nodes = _hub_affected_nodes(results or [])
    if results is not None and not affected_nodes and not route_reviews:
        return {
            "status": "no_action", "reason": "no_affected_nodes",
            "affected_node_count": 0,
        }
    command = [
        sys.executable, str(REPO / ".scripts" / "hub_semantics.py"),
        "auto-create", "--check",
    ]
    for node in affected_nodes:
        command.extend(["--node", node])
    if affected_nodes:
        try:
            result = subprocess.run(
                command,
                cwd=REPO, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return {"status": "error", "error": result.stderr[:300]}
            check = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return {
                "status": "deferred",
                "reason": "timeout",
                "retryable": True,
                "timeout_seconds": 120,
                "affected_node_count": len(affected_nodes),
                "next_action": "retry_hub_maintenance",
            }
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
    else:
        check = {"status": "no_action", "affected_node_count": 0}
    eligible = check.get("eligible", [])
    split_candidates = check.get("split_candidates", [])
    redistribution_candidates = check.get("redistribution_candidates", [])
    if (check.get("status") != "agent_required" and not route_reviews) or not (
            eligible or split_candidates or redistribution_candidates or route_reviews):
        return {
            "status": "no_action",
            "affected_node_count": check.get("affected_node_count", len(affected_nodes)),
            "backlog_count": check.get("backlog_count", 0),
            "split_backlog_count": check.get("split_backlog_count", 0),
        }
    summary = {
        "status": "agent_required",
        "affected_node_count": check.get("affected_node_count", len(affected_nodes)),
        "eligible_count": len(eligible),
        "split_count": len(split_candidates),
        "redistribution_count": len(redistribution_candidates),
        "route_review_count": len(route_reviews),
        "backlog_count": check.get("backlog_count", 0),
        "split_backlog_count": check.get("split_backlog_count", 0),
        "next_action": "agent_review_hub_routes_and_maintenance",
    }
    if route_reviews:
        route_dir = REPO / "temp" / "hub-route-review"
        route_dir.mkdir(parents=True, exist_ok=True)
        route_file = route_dir / f"{session_id}.json"
        _write_json_atomic(route_file, route_reviews)
        summary["route_review_file"] = str(route_file.relative_to(REPO))
    if eligible:
        hub_dir = REPO / "temp" / "hub-auto-create"
        hub_dir.mkdir(parents=True, exist_ok=True)
        candidates_file = hub_dir / f"{session_id}.json"
        candidates_file.write_text(
            json.dumps(eligible, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["candidates_file"] = str(candidates_file.relative_to(REPO))
    if split_candidates:
        split_dir = REPO / "temp" / "hub-auto-split"
        split_dir.mkdir(parents=True, exist_ok=True)
        split_file = split_dir / f"{session_id}.json"
        split_file.write_text(
            json.dumps(split_candidates, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary["split_candidates_file"] = str(split_file.relative_to(REPO))
    if redistribution_candidates:
        redistribute_dir = REPO / "temp" / "hub-auto-redistribute"
        redistribute_dir.mkdir(parents=True, exist_ok=True)
        redistribute_file = redistribute_dir / f"{session_id}.json"
        redistribute_file.write_text(
            json.dumps(redistribution_candidates, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary["redistribution_candidates_file"] = str(
            redistribute_file.relative_to(REPO))
    return summary


def compact_maintenance(envelope: dict) -> dict:
    """Return the bounded maintenance handoff retained by inbox and DSH."""
    compact = {
        "status": envelope.get("status", "error"),
        "receipt_path": envelope.get("receipt_path", ""),
        "actions": envelope.get("actions", [])[:10],
        "errors": [str(error)[:300] for error in envelope.get("errors", [])[:10]],
    }
    for key in ("publication", "retryable", "next_action", "report_path"):
        if key in envelope:
            compact[key] = envelope[key]
    components = {}
    allowed = {
        "status", "alias_resolved", "prop_resolved", "remaining", "remaining_tokens",
        "remaining_occurrences", "warning_count",
        "review_file", "candidate_count", "built_count", "eligible_count", "split_count",
        "redistribution_count", "route_review_count", "candidates_file", "split_candidates_file",
        "redistribution_candidates_file", "next_action", "reason", "retryable",
        "route_review_file", "timeout_seconds", "affected_node_count",
    }
    for name, summary in envelope.get("components", {}).items():
        if not isinstance(summary, dict):
            continue
        components[name] = {
            key: value for key, value in summary.items()
            if key in allowed and value is not None
        }
    compact["components"] = components
    return compact


def run_post_ingest_maintenance(results: list[dict], session_id: str) -> dict:
    """Run and persist the unified post-ingest maintenance transaction."""
    safe_session = _safe_session_id(session_id)
    receipt_path = REPO / "temp" / "inbox-maintenance" / f"{safe_session}.json"
    if not any(item.get("ok") and item.get("status") == "completed" for item in results):
        skipped = {"status": "skipped", "reason": "no_successful_files"}
        envelope = {
            "status": "skipped", "session_id": session_id, "actions": [], "errors": [],
            "receipt_path": str(receipt_path.relative_to(REPO)),
            "components": {
                "abbreviations": skipped, "people": skipped.copy(), "hubs": skipped.copy(),
            },
        }
        try:
            _write_json_atomic(receipt_path, envelope)
        except Exception as exc:
            envelope["status"] = "error"
            envelope["errors"].append(f"maintenance receipt write failed: {exc}")
        return envelope

    invalid_results = []
    for index, item in enumerate(results):
        if not (item.get("ok") and item.get("status") == "completed"):
            continue
        graph_report = item.get("graph_report")
        hub_dynamics = graph_report.get("hub_dynamics") if isinstance(graph_report, dict) else None
        if not isinstance(graph_report, dict) or not isinstance(hub_dynamics, dict) \
                or not isinstance(hub_dynamics.get("affected_nodes"), list) \
                or hub_dynamics.get("status") == "error":
            invalid_results.append({
                "index": index,
                "file": item.get("file", ""),
                "transaction_id": item.get("transaction_id", ""),
                "missing": "graph_report.hub_dynamics.affected_nodes",
            })
    if invalid_results:
        skipped = {"status": "skipped", "reason": "invalid_result_envelope"}
        envelope = {
            "status": "validation_error",
            "session_id": session_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "actions": [],
            "errors": [
                "completed maintenance trigger is missing the full graph_report envelope"
            ],
            "invalid_results": invalid_results,
            "receipt_path": str(receipt_path.relative_to(REPO)),
            "components": {
                "abbreviations": skipped,
                "people": skipped.copy(),
                "hubs": skipped.copy(),
            },
        }
        try:
            _write_json_atomic(receipt_path, envelope)
        except Exception as exc:
            envelope["status"] = "error"
            envelope["errors"].append(f"maintenance receipt write failed: {exc}")
        return envelope

    try:
        abbr_summary = _auto_resolve_abbreviations(session_id)
    except Exception as exc:
        abbr_summary = {"status": "error", "errors": [f"{type(exc).__name__}: {exc}"]}
    abbr_summary["scope"] = "global_backlog_after_ingest"
    try:
        people_summary = ic.detect_people_page_candidates(REPO)
        try:
            from build_people_pages import build_pending_people
            people_summary["auto_built"] = build_pending_people()
        except Exception as exc:
            people_summary["auto_built"] = {"error": str(exc)}
    except Exception as exc:
        people_summary = {"status": "error", "errors": [f"{type(exc).__name__}: {exc}"]}
    try:
        hub_summary = _auto_create_hubs(session_id, results)
    except Exception as exc:
        hub_summary = {"status": "error", "errors": [f"{type(exc).__name__}: {exc}"]}

    errors = []
    actions = []
    for name, summary in (
        ("abbreviations", abbr_summary), ("people", people_summary), ("hubs", hub_summary),
    ):
        if not isinstance(summary, dict):
            errors.append(f"{name}: invalid maintenance result")
            continue
        if summary.get("status") == "error" or summary.get("error"):
            component_errors = summary.get("errors") or [summary.get("error")]
            errors.extend(f"{name}: {error}" for error in component_errors if error)
        if summary.get("status") == "agent_required":
            action = {"component": name, "next_action": summary.get("next_action", "agent_review")}
            for key in (
                "review_file", "candidates_file", "split_candidates_file",
                "redistribution_candidates_file",
                "route_review_file",
            ):
                if summary.get(key):
                    action[key] = summary[key]
            actions.append(action)

    auto_built = people_summary.get("auto_built", {}) if isinstance(people_summary, dict) else {}
    if isinstance(auto_built, dict) and auto_built.get("error"):
        errors.append(f"people: {auto_built['error']}")
    deferred = any(
        isinstance(summary, dict) and summary.get("status") == "deferred"
        for summary in (abbr_summary, people_summary, hub_summary)
    )
    envelope = {
        "status": (
            "error" if errors else "agent_required" if actions else
            "deferred" if deferred else "completed"
        ),
        "session_id": session_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "trigger_files": [
            {key: item.get(key) for key in ("file", "status", "transaction_id")
             if item.get(key) is not None}
            for item in results
        ],
        "actions": actions,
        "errors": errors,
        "receipt_path": str(receipt_path.relative_to(REPO)),
        "components": {
            "abbreviations": abbr_summary,
            "people": people_summary,
            "hubs": hub_summary,
        },
    }
    try:
        _write_json_atomic(receipt_path, envelope)
    except Exception as exc:
        envelope["status"] = "error"
        envelope["errors"].append(f"maintenance receipt write failed: {exc}")
    return envelope


def publish_maintenance_report(report_path: Path, report: dict, *,
                               state_overrides: dict | None = None,
                               repo: Path | None = None) -> bool:
    """Publish a shared maintenance report and bind its completed transactions."""
    import inbox_state
    repo = (repo or REPO).resolve()
    report_path = report_path.resolve()
    report_path.relative_to((repo / "cross-domain/ingest-reports").resolve())
    maintenance = report.get("maintenance") or {}
    updates = []
    report_rel = str(report_path.relative_to(repo))
    checkpoint_written = False
    try:
        if maintenance.get("receipt_path"):
            receipt_path = (repo / maintenance["receipt_path"]).resolve()
            receipt_path.relative_to((repo / "temp/inbox-maintenance").resolve())
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict):
                raise ValueError("maintenance receipt must be an object")
            maintenance = {**receipt, "receipt_path": str(receipt_path.relative_to(repo))}
            linked = compact_maintenance({**maintenance, "publication": {"status": "completed"}})
            linked["report_path"] = report_rel
            for item in report.get("files", []):
                transaction_id = item.get("transaction_id")
                if item.get("status") != "completed" or not transaction_id:
                    continue
                state = (state_overrides or {}).get(transaction_id)
                if state is None:
                    state = inbox_state.load(transaction_id)
                if state is None or state.get("status") != "completed":
                    raise ValueError(f"completed maintenance transaction missing: {transaction_id}")
                state_page = str(state.get("wiki_path") or "").removesuffix(".md")
                item_page = str(item.get("wiki_path") or "").removesuffix(".md")
                if not state_page or state_page != item_page:
                    raise ValueError(f"maintenance transaction page mismatch: {transaction_id}")
                state["maintenance"] = linked
                updates.append((transaction_id, state))
        report["maintenance"] = {
            **maintenance, "status": "deferred", "retryable": True,
            "next_action": "reconcile_maintenance_report", "report_path": report_rel,
            "publication": {"status": "pending"},
        }
        _write_json_atomic(report_path, report)
        checkpoint_written = True
        for transaction_id, state in updates:
            inbox_state.save(transaction_id, state)
        report["maintenance"] = {**maintenance, "publication": {"status": "completed"}}
        _write_json_atomic(report_path, report)
        return True
    except (OSError, ValueError) as exc:
        report["maintenance"] = {
            **maintenance, "status": "error", "retryable": isinstance(exc, OSError),
            "errors": [*maintenance.get("errors", []), f"maintenance publication failed: {exc}"],
            "report_path": report_rel,
            "next_action": "reconcile_maintenance_report" if checkpoint_written else "repair_report_publication",
            "publication": {"status": "error", "report_persisted": checkpoint_written},
        }
        if checkpoint_written:
            try:
                _write_json_atomic(report_path, report)
            except OSError as persist_error:
                report["maintenance"]["errors"].append(f"publication error checkpoint: {persist_error}")
        return False


def reconcile_maintenance_report(report_path: Path) -> dict:
    """Repair one historical report using only recorded transaction decisions."""
    import inbox_state
    import hub_semantics
    report_path = report_path.resolve()
    report_path.relative_to((REPO / "cross-domain/ingest-reports").resolve())
    report = json.loads(report_path.read_text(encoding="utf-8"))
    receipt_rel = (report.get("maintenance") or {}).get("receipt_path")
    if receipt_rel:
        receipt_path = (REPO / receipt_rel).resolve()
        receipt_path.relative_to((REPO / "temp/inbox-maintenance").resolve())
        report["maintenance"] = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not publish_maintenance_report(report_path, report):
        return _compact_summary(report, report_path)
    for item in report.get("files", []):
        if item.get("status") != "completed" or not item.get("transaction_id"):
            continue
        state = inbox_state.load(item["transaction_id"])
        if state is None:
            continue
        for correction in state.get("route_corrections", []):
            if correction.get("kind") == "agent_confirmed_hub_route":
                closed_report = hub_semantics._close_route_review_handoff(state, correction)
                if closed_report and (closed_report.get("maintenance") or {}).get("publication", {}).get("status") == "error":
                    return _compact_summary(closed_report, report_path)
        inbox_state.save(item["transaction_id"], state)
    repaired = json.loads(report_path.read_text(encoding="utf-8"))
    publish_maintenance_report(report_path, repaired)
    return _compact_summary(repaired, report_path)


def _compact_summary(report: dict, report_path: Path) -> dict:
    """构造给 Agent 的稳定小结果；完整逐文件诊断只保留在 report。"""
    item_statuses = {item.get("status") for item in report["files"]}
    if "failed" in item_statuses or "error" in item_statuses:
        status = "failed"
    elif "validation_error" in item_statuses:
        status = "validation_error"
    elif "bibliographic_review_required" in item_statuses:
        status = "bibliographic_review_required"
    elif "classification_required" in item_statuses:
        status = "classification_required"
    elif "prepared" in item_statuses:
        status = "prepared"
    elif "agent_required" in item_statuses:
        status = "agent_required"
    elif "partial" in item_statuses or report["skipped"]:
        status = "partial"
    elif item_statuses == {"duplicate_found"}:
        status = "duplicate_found"
    else:
        status = "completed"
    files = []
    for item in report["files"]:
        compact = {"file": item.get("file"), "status": item.get("status", "unknown")}
        for key in (
            "quality_status", "reason", "transaction_id", "retryable",
            "next_action", "resume_from", "failure_signature", "bibliographic_worker",
            "relationship_worker", "semantic_repair_worker", "failure_disposition",
            "fact_entries", "write_to", "manifest_path", "apply_command", "cleanup",
            "agent_task", "workflow_status", "internal_status",
        ):
            if item.get(key) is not None:
                compact[key] = item[key]
        warnings = item.get("quality_warnings") or []
        if warnings:
            compact["quality_warning_count"] = len(warnings)
            compact["quality_warnings"] = [
                {key: str(warning[key])[:240] for key in ("issue", "detail") if key in warning}
                if isinstance(warning, dict) else {"detail": str(warning)[:240]}
                for warning in warnings[:5]
            ]
        files.append(compact)
    compact = {
        "status": status,
        "file_status": status,
        "backend": report.get("backend"),
        "total": report["total"],
        "completed": report["completed"],
        "duplicates": report.get("duplicates", 0),
        "awaiting_agent": report.get("awaiting_agent", 0),
        "pending": report.get("pending", 0),
        "degraded": report["degraded"],
        "failed": report["failed"],
        "skipped": report["skipped"],
        "report_path": str(report_path.relative_to(REPO)),
        "files": files,
    }
    if report.get("maintenance"):
        compact["maintenance"] = compact_maintenance(report["maintenance"])
    return compact


def main():
    ap = argparse.ArgumentParser(description="inbox 统一摄入入口：确定性分流 + 双后端语义复核")
    ap.add_argument("--file", help="指定单个文件（不扫描 inbox 全部）")
    ap.add_argument("--run", action="store_true", help="实际执行分发（默认 dry run）")
    ap.add_argument("--subproject", default="academic",
                    choices=["academic", "admin", "teaching", "business"],
                    help="meeting/document 的存储域（默认 academic）")
    ap.add_argument("--document-type",
                    choices=["editorial", "academic-reference", "conference-summary"],
                    help="单个 academic 非论文文档的显式子类型；须与 --file 同用")
    ap.add_argument("--download", nargs="+", metavar="URL",
                    help="下载 PDF URL 到 inbox/ 再处理")
    ap.add_argument("--classification-file", default="",
                    help="Agent backend 的 temp/ 分类裁决 JSON")
    ap.add_argument("--ocr-result", help="单图片已有 OCR JSON 回执，校验后复用")
    ap.add_argument("--allow-remote-ocr", action="store_true", help="显式授权单图片上传 OCR API")
    ap.add_argument("--reconcile-maintenance-report", type=Path,
                    help="修复指定历史摄入报告的维护关联并重放已有裁决（不重摄入）")
    args = ap.parse_args()
    ocr_options = {}
    if args.ocr_result or args.allow_remote_ocr:
        if not args.file or Path(args.file).suffix.lower() not in inbox_plan.IMAGE_SUFFIXES:
            ap.error("OCR 参数只用于 --file 指定的单张图片")
        if args.ocr_result:
            ocr_options["ocr_result"] = args.ocr_result
        if args.allow_remote_ocr:
            ocr_options["allow_remote_ocr"] = True
    if args.reconcile_maintenance_report:
        if args.run or args.file or args.download or args.classification_file or args.document_type:
            ap.error("--reconcile-maintenance-report 不得与摄入操作组合")
        try:
            result = reconcile_maintenance_report(REPO / args.reconcile_maintenance_report)
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "validation_error", "errors": [str(exc)]}, ensure_ascii=False))
            raise SystemExit(1)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.document_type and (args.subproject != "academic" or not args.file):
        ap.error("--document-type 仅用于 --file 指定的 academic 文档")
    backend = agent_task.ingest_backend()
    if args.run:
        print(f"ingest backend={backend}", file=sys.stderr, flush=True)
    supplied_classifications = {}
    if args.classification_file:
        classification_path = (REPO / args.classification_file).resolve()
        try:
            classification_path.relative_to((REPO / "temp" / "inbox-classification").resolve())
            supplied_classifications = _load_agent_classifications(classification_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "status": "validation_error",
                "errors": [f"Agent 分类裁决无效: {exc}"],
            }, ensure_ascii=False, indent=2))
            raise SystemExit(1)

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
    classification_pending: list[dict] = []
    used_classifications: set[str] = set()
    fingerprint_matches: dict[str, dict] = {}
    fingerprint_index_error = ""
    try:
        sf.ensure_index()
    except Exception as exc:
        fingerprint_index_error = str(exc)
    for f in files:
        rel = str(f.relative_to(REPO))
        if f.name == "facts-pending.md":
            decision = inbox_plan.classify(f)
            classification_details[rel] = {
                "file_type": "user-assertions",
                **decision,
            }
            if not args.run:
                print(f"{f.name:<40} {'user-assertions':<24} {'agent handoff':<20}")
            classified.append((f, "user-assertions", rel))
            continue
        if not fingerprint_index_error:
            try:
                match = sf.lookup_exact(f)
            except Exception as exc:
                fingerprint_index_error = str(exc)
                match = None
            if match:
                fingerprint_matches[rel] = match
        decision = classify_file_details(f)
        ftype = decision["file_type"]
        if args.document_type:
            decision = {
                **decision,
                "program_file_type": ftype,
                "file_type": "document",
                "needs_api_review": False,
                "explicit_document_type": args.document_type,
            }
            ftype = "document"
        classification_details[rel] = {
            key: value for key, value in decision.items() if key != "review_text"
        }
        if args.run and decision.get("needs_api_review") and rel not in fingerprint_matches:
            if backend == "api":
                review = review_low_confidence_classification(f, decision)
            elif rel in supplied_classifications:
                review = supplied_classifications[rel]
                try:
                    _validate_agent_classification(f, decision, review)
                except ValueError as exc:
                    review = {"status": "review_error", "error": str(exc)}
                used_classifications.add(rel)
            else:
                allowed = ["document", "paper"] if f.suffix.lower() == ".pdf" else ["document", "meeting"]
                classification_pending.append({
                    "file": rel,
                    "extension": f.suffix.lower(),
                    "allowed_types": allowed,
                    "program": {key: value for key, value in decision.items() if key != "review_text"},
                    "review_text": decision.get("review_text", ""),
                })
                review = None
            if review is None:
                classified.append((f, ftype, rel))
                continue
            classification_reviews[rel] = review
            resolved_type, reason = reconcile_classification(decision, review)
            if resolved_type is None:
                classification_blocks[rel] = reason
            else:
                ftype = resolved_type
        document_type = None
        if ftype == "document" and args.subproject == "academic":
            document_type = args.document_type or classify_academic_document(f)
            academic_document_types[rel] = document_type
            classification_details[rel]["document_type"] = document_type
            classification_details[rel]["document_type_source"] = (
                "explicit" if args.document_type else "strong_signal"
            )
        script = {"paper": "ingest_paper.py", "meeting": "ingest_meeting.py",
                  "document": "ingest_document.py"}[ftype]
        if not args.run:
            display_type = (f"document:{document_type}" if document_type else
                            "classification_required" if ftype == "document" and args.subproject == "academic"
                            else ftype)
            if decision.get("needs_api_review"):
                display_type = f"{display_type}?({decision['score']}/{decision['threshold']})"
            if rel in fingerprint_matches:
                display_type = "duplicate:sha256"
            print(f"{f.name:<40} {display_type:<24} {script:<20}")
        classified.append((f, ftype, rel))

    unused_classifications = sorted(set(supplied_classifications) - used_classifications)
    if unused_classifications:
        print(json.dumps({
            "status": "validation_error",
            "errors": ["分类裁决包含非本次待裁决文件: " + ", ".join(unused_classifications)],
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    if args.run and classification_pending:
        print(json.dumps({
            "status": "prepared",
            "agent_task": _classification_task(classification_pending, args),
            "total": len(files),
        }, ensure_ascii=False, indent=2))
        return

    if not args.run:
        print(f"\n共 {len(classified)} 个文件。加 --run 执行分发。")
        # 关联提示：帮助 agent 识别版本/补充关系，制定关联摄入计划
        hints = _relation_hints(classified)
        if hints:
            print("\n--- 关联提示（供 agent 制定摄入计划）---")
            for hint in hints:
                print(hint)
        if fingerprint_matches:
            print("\n--- 精确重复（不会进入提取/API）---")
            for rel, match in fingerprint_matches.items():
                print(f"{rel} -> {match['raw_path']}")
        return

    # 多文档计划：主文档优先，版本/补充靠后（改进 3）
    classified, plan_notes = _plan_ingest_order(classified)

    # API 使用 DSH adapter；当前 Agent 直接调用确定性脚本入口。
    loop = None
    agent_events = []
    if backend == "api":
        from dsh.agent_loop import IngestAgentLoop
        loop = IngestAgentLoop(mode="api")
        session_id = loop.session_log.session_id
    else:
        session_id = "agent-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")

    def audit(event: str, payload: dict) -> None:
        if loop is not None:
            loop.session_log.append(event, payload)
        else:
            agent_events.append({"event": event, "payload": payload})

    results = []
    tool_outputs = []
    for f, ftype, rel in classified:
        match = fingerprint_matches.get(rel)
        if not match:
            continue
        try:
            cleanup = cleanup_exact_duplicate(f, match)
            entry = {
                "file": f.name,
                "type": ftype,
                "ok": True,
                "status": "duplicate_found",
                "reason": "binary_sha256 exact match; source moved to recoverable Trash",
                "raw_path": match["raw_path"],
                "binary_sha256": match["binary_sha256"],
                "cleanup": cleanup,
            }
        except (OSError, RuntimeError, ValueError) as exc:
            entry = {
                "file": f.name,
                "type": ftype,
                "ok": False,
                "status": "failed",
                "reason": str(exc),
                "raw_path": match.get("raw_path", ""),
                "binary_sha256": match.get("binary_sha256", ""),
                "exact_duplicate": True,
            }
        results.append(entry)
        audit("ingest/duplicate_cleanup", entry)
    classified = [item for item in classified if item[2] not in fingerprint_matches]
    paper_batch = (backend == "api" and not args.file and len(classified) > 1 and
                   not classification_blocks and
                   all(ftype == "paper" for _, ftype, _ in classified))
    if paper_batch:
        assert loop is not None
        content = loop.execute("ingest_paper_inbox", {})
        tool_outputs.append({"tool": "ingest_paper_inbox", "output": content})
        parsed = loop.last_structured or _extract_last_json(content)
        batch_results = _map_paper_batch_results(parsed)
        results.extend(batch_results)
        if not batch_results:
            reason = "批量入口未返回 items"
            if isinstance(parsed, dict):
                reason = str(parsed.get("errors") or parsed.get("status") or reason)
            results.append({"file": "inbox paper batch", "type": "paper", "ok": False,
                            "status": "failed", "reason": reason})
    else:
        for f, ftype, rel in classified:
            if ftype == "user-assertions":
                count = inbox_plan.fact_entries(f)
                if count:
                    entry = user_assertions_handoff(f)
                else:
                    entry = {
                        "file": f.name,
                        "type": ftype,
                        "ok": True,
                        "skipped": True,
                        "status": "completed",
                        "reason": "facts-pending.md 无事实条目",
                        "fact_entries": 0,
                    }
                results.append(entry)
                audit("ingest/handoff", {
                    "file": rel,
                    "status": entry["status"],
                    "reason": entry["reason"],
                    "next_action": entry.get("next_action", ""),
                })
                continue
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
                audit("ingest/skip", {
                    "file": rel,
                    "status": "classification_required",
                    "reason": entry["reason"],
                    "program_classification": entry["program_classification"],
                    "api_classification": review,
                })
                continue
            document_type = academic_document_types.get(rel)
            if ftype == "document" and args.subproject == "academic" and not document_type:
                resume_command = (
                    "python3 .scripts/ingest_inbox.py --run "
                    f"--file {shlex.quote(rel)} --subproject academic "
                    "--document-type <editorial|academic-reference|conference-summary>"
                )
                entry = {
                    "file": f.name,
                    "type": ftype,
                    "ok": False,
                    "skipped": True,
                    "status": "classification_required",
                    "reason": (
                        "academic 非论文文档缺少强分类信号；请显式选择 editorial、"
                        "academic-reference 或 conference-summary"
                    ),
                    "allowed_document_types": [
                        "editorial", "academic-reference", "conference-summary",
                    ],
                    "next_action": "rerun_with_document_type",
                    "resume_command": resume_command,
                }
                results.append(entry)
                audit("ingest/skip", {
                    "file": rel,
                    "status": "classification_required",
                    "reason": entry["reason"],
                })
                continue
            source_kind = classification_details.get(rel, {}).get("source_kind", "ordinary")
            if backend == "api":
                assert loop is not None
                tool_name, tool_args = dsi_tool(
                    ftype, rel, args.subproject, document_type=document_type,
                    source_kind=source_kind,
                    **ocr_options,
                )
                content = loop.execute(tool_name, tool_args)
                parsed = loop.last_structured or _extract_last_json(content)
                tool_outputs.append({"file": f.name, "tool": tool_name, "output": content})
            else:
                command = dispatch_command(
                    ftype, rel, args.subproject, document_type=document_type,
                    source_kind=source_kind,
                    **ocr_options,
                )
                completed = subprocess.run(
                    command, cwd=REPO, text=True, capture_output=True, check=False,
                )
                content = completed.stdout
                parsed = _extract_last_json(content)
                tool_outputs.append({
                    "file": f.name,
                    "command": [str(part) for part in command],
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2000:],
                })
            results.append(_result_entry(f.name, ftype, parsed, content))
            audit("ingest/result", {
                "file": rel, "status": results[-1]["status"],
                "transaction_id": results[-1].get("transaction_id", ""),
            })

    dsh_log = None
    agent_log = None
    if loop is not None:
        dsh_dir = REPO / "temp" / "inbox-dsh"
        dsh_dir.mkdir(parents=True, exist_ok=True)
        dsh_log = dsh_dir / f"{session_id}.jsonl"
        dsh_log.write_text(loop.session_log.to_jsonl() + "\n", encoding="utf-8")
    else:
        agent_dir = REPO / "temp" / "inbox-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        agent_log = agent_dir / f"{session_id}.jsonl"
        agent_log.write_text("\n".join(
            json.dumps(item, ensure_ascii=False) for item in agent_events
        ) + ("\n" if agent_events else ""), encoding="utf-8")

    # 只有本次真正摄入成功才扫描全局 backlog。分类闸门/全失败必须快速返回。
    maintenance = run_post_ingest_maintenance(results, session_id)

    # 摄入报告持久化：复盘用（引擎选择、逐文件状态、建图统计）
    report_dir = REPO / "cross-domain" / "ingest-reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_time = datetime.now()
    report_path = report_dir / f"{report_time.strftime('%Y%m%d-%H%M%S')}.json"
    counts = _report_counts(results)
    report = {
        "timestamp": report_time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "backend": backend,
        "dsh_log": str(dsh_log.relative_to(REPO)) if dsh_log else "",
        "agent_log": str(agent_log.relative_to(REPO)) if agent_log else "",
        "total": len(results),
        **counts,
        "files": results,
        "classification_decisions": classification_details,
        "classification_reviews": classification_reviews,
        "fingerprint_matches": fingerprint_matches,
        "fingerprint_index_error": fingerprint_index_error,
        "plan_notes": plan_notes,
        "tool_outputs": tool_outputs,
        "maintenance": maintenance,
    }
    publish_maintenance_report(report_path, report)
    compact = _compact_summary(report, report_path)
    print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))
    if report["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""ingest_inbox.py — inbox 统一摄入入口：纯 Python 分流，零 LLM 类型判断。

扫描 inbox/ 下的文件，按扩展名+内容关键词分类，分发到对应摄入脚本：
  - PDF + 学术特征（Abstract/References/arXiv/DOI）→ ingest_paper.py
  - PDF 非学术 → ingest_document.py
  - .txt + 会议特征（会议/参会/元宝会议助手/时间戳）→ ingest_meeting.py
  - .txt 非会议 / .docx / .doc / .pptx / .md → ingest_document.py

分类纯 Python（pymupdf 读 PDF 前2页 + 关键词匹配），不调 LLM。
各脚本内部已有合并 LLM 调用（agent 模式一次输出 wiki+slots），不重复阅读。

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
import json
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import ingest_common as ic
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


def is_academic_pdf(path: Path) -> bool:
    """检查 PDF 是否为学术论文：读前几页（短篇全读），按学术特征评分。

    特征（每个 +1）：Abstract/摘要、References/参考文献、arXiv ID、DOI、
    Keywords/关键词、Introduction/引言、University/Institute/Department、
    引用模式 [1] 或 (Author et al.)、LaTeX 公式 $、
    PACS 编号、Phys. Rev. / Rev. Mod. Phys. 期刊标识。
    """
    text = read_pdf_text(path, max_pages=2)
    if not text or len(text) < 200:
        return _is_academic_by_metadata(path)
    # 文本提取质量低（自定义字体/旧式 dvips PDF 常见乱码），回退元数据判断
    if _text_quality(text) < 0.5:
        return _is_academic_by_metadata(path)
    score = 0
    patterns = [
        r"\bAbstract\b", r"摘要", r"\bReferences\b", r"参考文献",
        r"arxiv[:\s]?\d{4}\.\d{4,5}", r"\b\d{4}\.\d{4,5}\b",
        r"\bDOI\b", r"doi\.org", r"\bKeywords\b", r"关键词",
        r"\bIntroduction\b", r"引言", r"\d+\.\s+Introduction",
        r"[Uu]niversity|[Ii]nstitute|[Dd]epartment|[Ll]aboratory",
        r"\[\d+\]", r"\([A-Z]\w+\s+et\s+al\.", r"\$.*?\$",
        r"PACS", r"Phys\.\s*Rev", r"Rev\.\s*Mod\.\s*Phys",
    ]
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            score += 1
    return score >= 3


def is_meeting_txt(path: Path) -> bool:
    """检查 .txt 是否为会议纪要：按会议特征评分。

    特征（每个 +1）：会议/参会/纪要、元宝会议助手/腾讯会议/飞书、
    时间戳 (MM:SS)、汇报、讨论。
    """
    try:
        text = path.read_text(encoding="utf-8")[:3000]
    except Exception:
        return False
    if not text:
        return False
    score = 0
    patterns = [
        "会议", "参会", "纪要", "元宝会议助手", "腾讯会议", "飞书",
        r"\(\d{2}:\d{2}\)", "汇报", "讨论",
    ]
    for pat in patterns:
        if re.search(pat, text):
            score += 1
    return score >= 2


def classify_file(path: Path) -> str:
    """返回文件类型：'paper' / 'meeting' / 'document'。"""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "paper" if is_academic_pdf(path) else "document"
    if suffix == ".txt":
        return "meeting" if is_meeting_txt(path) else "document"
    return "document"


def dispatch_command(file_type: str, rel_path: str, subproject: str) -> list[str]:
    """返回对应类型的分发命令。document 不接受 academic 域,自动映射到 admin。"""
    if file_type == "paper":
        return [sys.executable, str(REPO / ".scripts/ingest_paper.py"), "--pdf", rel_path]
    if file_type == "meeting":
        return [sys.executable, str(REPO / ".scripts/ingest_meeting.py"),
                "--txt", rel_path, "--subproject", subproject]
    doc_subproject = "admin" if subproject == "academic" else subproject
    return [sys.executable, str(REPO / ".scripts/ingest_document.py"),
            "--file", rel_path, "--subproject", doc_subproject]


def dsi_tool(file_type: str, rel_path: str, subproject: str) -> tuple[str, dict]:
    """返回 DSH ingest tool 名与参数，替代直接 subprocess dispatch。"""
    if file_type == "paper":
        return "ingest_paper_pdf", {"pdf": rel_path}
    if file_type == "meeting":
        return "ingest_meeting_txt", {"file": rel_path, "subproject": subproject}
    doc_subproject = "admin" if subproject == "academic" else subproject
    return "ingest_document_file", {"file": rel_path, "subproject": doc_subproject}


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


def _compact_summary(report: dict, report_path: Path) -> dict:
    """构造给 Agent 的稳定小结果；完整逐文件诊断只保留在 report。"""
    item_statuses = {item.get("status") for item in report["files"]}
    if "failed" in item_statuses or "error" in item_statuses:
        status = "failed"
    elif "agent_required" in item_statuses:
        status = "agent_required"
    elif "partial" in item_statuses or report["skipped"]:
        status = "partial"
    else:
        status = "completed"
    hub_auto = report.get("hub_auto_create", {})
    if status == "completed" and hub_auto.get("status") == "agent_required":
        status = "agent_required"
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
    hub_auto = report.get("hub_auto_create", {})
    if hub_auto.get("status") == "agent_required":
        compact["hub_auto_create"] = {
            "status": "agent_required",
            "candidates_file": hub_auto.get("candidates_file", ""),
            "eligible_count": hub_auto.get("eligible_count", 0),
        }
    return compact


def main():
    ap = argparse.ArgumentParser(description="inbox 统一摄入入口：纯 Python 分流")
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
    for f in files:
        rel = str(f.relative_to(REPO))
        ftype = classify_file(f)
        script = {"paper": "ingest_paper.py", "meeting": "ingest_meeting.py",
                  "document": "ingest_document.py"}[ftype]
        if not args.run:
            print(f"{f.name:<40} {ftype:<10} {script:<20}")
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
            tool_name, tool_args = dsi_tool(ftype, rel, args.subproject)
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

    # 轻量自动消解：新摄入可能带来新 alias/raw 定义，消解积压的裸缩写
    abbr_summary = _auto_resolve_abbreviations()
    abbr_summary["scope"] = "global_backlog_after_ingest"

    # 人物页面候选检测：达标的 person entity 记入 people-pending.jsonl
    people_summary = ic.detect_people_page_candidates(REPO)
    # 自动建立极简 people page：达标人物即建页，不再积压 pending 队列
    try:
        from build_people_pages import build_pending_people
        people_summary["auto_built"] = build_pending_people()
    except Exception as exc:
        people_summary["auto_built"] = {"error": str(exc)}

    # Hub 自动创建检查：达标候选触发 agent 处理，不达标静默进 backlog
    hub_summary = _auto_create_hubs(loop.session_log.session_id)

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

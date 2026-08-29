#!/usr/bin/env python3
"""
PDF → Markdown 提取工具（多引擎级联版）

引擎优先级: MinerU (本地/API) > BLSC OCR > Docling > PyMuPDF
高质量可覆盖低质量，低质量不可覆盖高质量。

用法:
    python scripts/extractor.py --paper <paper-id>
    python scripts/extractor.py --paper <paper-id> --engine docling
    python scripts/extractor.py --paper <paper-id> --engine blsc_ocr
    python scripts/extractor.py --paper <paper-id> --force           # 强制重提取
    python scripts/extractor.py --batch                              # 批量处理全部论文
"""

import base64
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Optional

import yaml

from mineru_api import MinerUAuthError, MinerUError, extract_pdf_with_mineru

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during minimal local runs
    load_dotenv = None

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("extractor")

# ─── 引擎优先级 ─────────────────────────────────────────────
ENGINE_PRIORITY = {
    "mineru": 4,     # 最高（本地结果或专用 PDF API）
    "blsc_ocr": 3,   # 高（BLSC 视觉模型逐页 OCR）
    "docling": 2,    # 中等（本地离线 OCR）
    "pymupdf": 1,    # 最低（文字层兜底）
}

BLSC_OCR_PROMPT = (
    "请识别这张 PDF 页面渲染图中的全部文字，按阅读顺序输出 Markdown。"
    "保留标题、段落、列表、表格和公式（公式可转成 LaTeX），不要解释，不要添加图片外内容。"
)

DOCLING_VENV = Path(__file__).resolve().parent.parent / ".venv-docling"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / ".project" / "config.yaml"
# 论文提取产物目录(学术域内,符合四域结构;原 CodexInspiration 用 raw/papers/)
PAPERS_DIR = PROJECT_ROOT / "academic" / "raw" / "works" / "papers"

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")
elif (PROJECT_ROOT / ".env").exists():
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ═══════════════════════════════════════════════════════════
#  元数据管理
# ═══════════════════════════════════════════════════════════

def load_parse_meta(paper_dir: Path) -> dict:
    """加载 parse_meta.yaml"""
    meta_path = paper_dir / "parse_meta.yaml"
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save_parse_meta(paper_dir: Path, meta: dict):
    """保存 parse_meta.yaml。写入时检查 source.yaml / meta.yaml 的外部路径并填入 source 段（规则 3）。"""
    # 规则 3：外部产物位置记录——把外部 PDF/MD 路径同步进 parse_meta.source
    src_yaml = paper_dir / "source.yaml"
    if src_yaml.exists():
        sdata = yaml.safe_load(src_yaml.read_text(encoding="utf-8")) or {}
        ext_pdf = sdata.get("external_path")
        if ext_pdf:
            meta.setdefault("source", {})["external_pdf_path"] = ext_pdf
    meta_yaml = paper_dir / "meta.yaml"
    if meta_yaml.exists():
        mdata = yaml.safe_load(meta_yaml.read_text(encoding="utf-8")) or {}
        ext_md = (mdata.get("source") or {}).get("external_md_path")
        if ext_md:
            meta.setdefault("source", {})["external_md_path"] = ext_md
    meta_path = paper_dir / "parse_meta.yaml"
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(meta, f, allow_unicode=True, default_flow_style=False)


def remove_lower_quality_backups(paper_dir: Path, preferred_engine: str) -> None:
    """Remove backups made before a higher-quality parser replaced paper.md."""
    preferred_priority = ENGINE_PRIORITY.get(preferred_engine, 0)
    backup_path = paper_dir / "paper.md.bak"
    if not backup_path.exists():
        return

    meta = load_parse_meta(paper_dir)
    engine_records = meta.get("engines") or {}
    previous_engines = [
        engine for engine in engine_records
        if engine != preferred_engine
        and ENGINE_PRIORITY.get(engine, 0) < preferred_priority
    ]
    if previous_engines:
        backup_path.unlink()
        logger.info(
            "   🧹 已删除低质量全文备份: %s (%s → %s)",
            backup_path.name,
            ", ".join(previous_engines),
            preferred_engine,
        )


def get_current_engine(paper_dir: Path) -> Optional[str]:
    """获取当前使用的解析引擎"""
    meta = load_parse_meta(paper_dir)
    return meta.get("preferred")


def can_override(current_engine: Optional[str], new_engine: str) -> bool:
    """判断新引擎是否可以覆盖当前引擎。
    来源标记缺失（current_engine is None）视为最低档 pymupdf（low，优先级 1），
    只有更高优先级引擎可覆盖；同档或更低需 --force（规则 2）。"""
    if current_engine is None:
        current_priority = 1  # 无来源标记视为 low，等同 pymupdf (priority 1)
    else:
        current_priority = ENGINE_PRIORITY.get(current_engine, 0)
    new_priority = ENGINE_PRIORITY.get(new_engine, 0)
    return new_priority > current_priority


# ─── 外部产物位置记录 ───────────────────────────────────────
# 规则 3：PDF 原文与全文 paper.md 是跨项目唯一共用产物。PDF 实体复制进本项目
# （不符号链接——符号链接指向 inbox/中转会断链,2026-07-22 修复），
# 在 source.yaml / meta.yaml 记录来源路径，供后续复用与溯源。

def load_config() -> dict:
    """加载 config.yaml（不存在返回空 dict）。"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def resolve_synology_path(uri: str, config: Optional[dict] = None) -> Optional[Path]:
    """把 synology://WikiRan/... 解析为本地绝对路径。
    非协议路径原样返回 Path；找不到匹配根时返回 None。复用 config.yaml 的 synology_roots。"""
    if not uri:
        return None
    if not uri.startswith("synology://"):
        p = Path(uri)
        if not p.is_absolute():
            p = PROJECT_ROOT / p  # 相对路径基于知识库根解析,而非 CWD
        return p
    rel = uri[len("synology://"):]
    cfg = config if config is not None else load_config()
    roots = (cfg.get("external_sources", {}).get("synology_roots")) or []
    for root in roots:
        candidate = Path(root) / rel
        if candidate.exists():
            return candidate
    return None


def load_source_yaml(paper_dir: Path) -> dict:
    """加载 source.yaml（不存在返回空 dict）。"""
    sp = paper_dir / "source.yaml"
    if sp.exists():
        with open(sp, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def save_source_yaml(paper_dir: Path, src: dict) -> None:
    """保存 source.yaml。"""
    with open(paper_dir / "source.yaml", "w", encoding="utf-8") as f:
        yaml.dump(src, f, allow_unicode=True, default_flow_style=False)


def link_external_pdf(paper_dir: Path, external_pdf_uri: str) -> Optional[Path]:
    """规则 3：外部 PDF 实体复制到 paper.pdf（不符号链接）。
    inbox/ 是中转区会被清空/替换，符号链接指向它会在 inbox 变动后断链，
    违反证据保持与可回溯原则。改为 copy2 实体复制，原始文件可随后清理。
    source.yaml 记录来源路径供追溯。"""
    pdf_path = paper_dir / "paper.pdf"
    src = resolve_synology_path(external_pdf_uri)
    if not src or not src.exists():
        logger.error(f"❌ 无法解析外部 PDF: {external_pdf_uri}")
        return None
    # 前置安全检查：拒绝以断链符号链接作为源（防链式符号链接指向失效目标）
    if src.is_symlink():
        real = src.resolve()
        if not real.exists():
            logger.error(f"❌ 外部 PDF 是断链符号链接: {src} → {real}")
            return None
        logger.warning(f"⚠️ 外部 PDF 是符号链接,解析到实体: {real}")
        src = real
    paper_dir.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists() or pdf_path.is_symlink():
        pdf_path.unlink()
    shutil.copy2(src, pdf_path)
    logger.info(f"   📄 实体复制 paper.pdf ← {src} ({pdf_path.stat().st_size} bytes)")
    sdata = load_source_yaml(paper_dir)
    sdata["source_type"] = "copy"
    sdata["acquired_method"] = "copy"
    # 存相对知识库根的路径(跨机可移植);synology:// 原样保留
    if external_pdf_uri.startswith("synology://"):
        sdata["external_path"] = external_pdf_uri
    else:
        ep = resolve_synology_path(external_pdf_uri)
        try:
            sdata["external_path"] = str(ep.relative_to(PROJECT_ROOT))
        except ValueError:
            sdata["external_path"] = str(ep)  # 不在知识库根下,存绝对路径
    sdata["acquired_date"] = datetime.now().strftime("%Y-%m-%d")
    save_source_yaml(paper_dir, sdata)
    return pdf_path


def record_external_md_path(paper_dir: Path, external_md_uri: str) -> None:
    """规则 3：全文 MD 来自外部时，把位置记入 meta.yaml.source.external_md_path。"""
    meta_path = paper_dir / "meta.yaml"
    meta = {}
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
    meta.setdefault("source", {})["external_md_path"] = external_md_uri
    with open(meta_path, "w", encoding="utf-8") as f:
        yaml.dump(meta, f, allow_unicode=True, default_flow_style=False)
    meta = load_meta_yaml(paper_dir)
    ext_pdf = (meta.get("source") or {}).get("external_path")
    if not ext_pdf:
        src_yaml = paper_dir / "source.yaml"
        if src_yaml.exists():
            src = yaml.safe_load(src_yaml.read_text(encoding="utf-8")) or {}
            ext_pdf = src.get("external_pdf")
    if not ext_pdf:
        return None
    src = resolve_synology_path(ext_pdf)
    if not src or not src.exists():
        logger.warning(f"   ⚠️ 外部 PDF 无法解析: {ext_pdf}")
        return None
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists() or pdf_path.is_symlink():
        pdf_path.unlink()
    shutil.copy2(src, pdf_path)
    logger.info(f"   📄 实体复制 paper.pdf ← {src} ({pdf_path.stat().st_size} bytes)")
    return pdf_path


# ═══════════════════════════════════════════════════════════
#  引擎 1: MinerU（本地结果或 API）
# ═══════════════════════════════════════════════════════════

def extract_mineru(paper_dir: Path, paper_id: str) -> Optional[str]:
    """
    检查用户是否已手动放置 MinerU 解析结果。
    
    优先使用本地手动结果；没有本地结果时，若配置了 Token，自动调用 MinerU API。
    """
    mineru_path = paper_dir / "paper_mineru.md"
    
    if mineru_path.exists():
        content = mineru_path.read_text(encoding="utf-8")
        if content.strip():
            logger.info(f"  📦 MinerU: 发现 {mineru_path.name} ({len(content):,} 字节)")
            return content

    config = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    mineru_config = (config.get("extraction") or {}).get("mineru") or {}
    if mineru_config.get("enabled", True) is False:
        logger.info("  MinerU API: 已禁用")
        return None

    token_env = mineru_config.get("token_env", "MINERU_API_TOKEN")
    token = os.getenv(token_env, "").strip()
    if not token:
        logger.info("  MinerU API: 未配置 %s，跳过", token_env)
        return None

    pdf_path = paper_dir / "paper.pdf"
    if not pdf_path.exists():
        return None

    # 重试 3 次（初次 + 2 次），退避递增；MinerU 是外部 API，失败多为限流/网络抖动
    backoff = [0, 5, 15]
    for attempt, delay in enumerate(backoff):
        if delay:
            logger.info(f"  MinerU API: 等待 {delay}s 后重试（第 {attempt+1}/{len(backoff)} 次）...")
            time.sleep(delay)
        try:
            return extract_pdf_with_mineru(
                pdf_path,
                token,
                model_version=mineru_config.get("model_version", "vlm"),
                base_url=mineru_config.get("api_base_url", "https://mineru.net/api/v4"),
                timeout_sec=int(mineru_config.get("timeout_sec", 1800)),
                interval_sec=int(mineru_config.get("poll_interval_sec", 5)),
                request_timeout_sec=int(mineru_config.get("request_timeout_sec", 60)),
                work_dir=paper_dir / ".mineru",
                keep_archive=bool(mineru_config.get("keep_archive", False)),
            )
        except MinerUAuthError as exc:
            # 认证错误不重试（token 问题重试无意义）
            logger.warning(
                "  ⚠️ MinerU API 认证失败：MINERU_API_TOKEN 可能已过期或无效，"
                "请更新项目根目录 .env 后重试。详情: %s",
                exc,
            )
            return None
        except (MinerUError, OSError) as exc:
            logger.warning(
                "  ⚠️ MinerU API 调用失败（第 %d/%d 次）: %s",
                attempt+1, len(backoff), exc,
            )
    logger.warning(
        "  ⚠️ MinerU API 连续 %d 次失败，放弃；论文 PDF 不回落其他引擎（质量问题）",
        len(backoff),
    )
    return None


# ═══════════════════════════════════════════════════════════
#  引擎 2: BLSC OCR（视觉模型逐页识别）
# ═══════════════════════════════════════════════════════════

def _blsc_ocr_config() -> dict:
    """读取 extraction.blsc_ocr 配置。"""
    return (load_config().get("extraction") or {}).get("blsc_ocr") or {}


def _render_pdf_pages(pdf_path: Path, dpi: int) -> Optional[list]:
    """将 PDF 每页渲染为 PNG 字节列表；PyMuPDF 不可用时返回 None。"""
    try:
        import fitz
    except ImportError:
        logger.warning("  ⚠️ BLSC OCR: PyMuPDF 未安装，无法渲染 PDF 页面")
        return None

    try:
        document = fitz.open(str(pdf_path))
    except Exception as exc:
        logger.warning("  ⚠️ BLSC OCR: 打开 PDF 失败 - %s", exc)
        return None

    try:
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pages = []
        for page in document:
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pages.append(pixmap.tobytes("png"))
        return pages
    finally:
        document.close()


def _call_blsc_chat(base_url: str, key: str, model: str, messages: list, timeout_sec: int) -> str:
    """调用 BLSC OpenAI 兼容 chat.completions，返回纯文本内容。"""
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4096,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        data = json.loads(response.read().decode("utf-8"))
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError("BLSC OCR 返回空内容")
    return content.strip()


def extract_blsc_ocr(paper_dir: Path, paper_id: str) -> Optional[str]:
    """使用 BLSC 视觉模型对 PDF 页面逐页 OCR，返回合并 Markdown。"""
    config = _blsc_ocr_config()
    if config.get("enabled", True) is False:
        logger.info("  BLSC OCR: 已禁用")
        return None

    base_url = os.getenv(config.get("base_env", "LLM_API_BASE"), "").strip()
    key = os.getenv(config.get("key_env", "LLM_API_KEY"), "").strip()
    model = config.get("model", "GLM-4.6V").strip()
    fallback_model = config.get("fallback_model", "GLM-4.5V").strip()
    if not base_url or not key or not model:
        logger.info("  BLSC OCR: 未配置 API base/key/model，跳过")
        return None

    pdf_path = paper_dir / "paper.pdf"
    if not pdf_path.exists():
        return None

    dpi = int(config.get("dpi", 160))
    timeout_sec = int(config.get("request_timeout_sec", 180))
    max_pages = int(config.get("max_pages", 200))
    pages = _render_pdf_pages(pdf_path, dpi)
    if pages is None:
        return None
    if max_pages > 0 and len(pages) > max_pages:
        logger.warning("  ⚠️ BLSC OCR: PDF 页数 %d 超过 max_pages=%d，跳过", len(pages), max_pages)
        return None

    models = list(dict.fromkeys([model, fallback_model]))
    models = [item for item in models if item]
    logger.info("  📄 BLSC OCR: %d 页，模型 %s", len(pages), " / ".join(models))

    sections = []
    for page_no, png_bytes in enumerate(pages, 1):
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": BLSC_OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        page_text = None
        for current_model in models:
            try:
                page_text = _call_blsc_chat(base_url, key, current_model, messages, timeout_sec)
                if page_text:
                    break
            except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                logger.warning(
                    "  ⚠️ BLSC OCR 第 %d 页 %s 失败: %s",
                    page_no,
                    current_model,
                    exc,
                )
        if page_text is None:
            logger.warning("  ❌ BLSC OCR: 第 %d 页所有模型均失败，放弃本次提取", page_no)
            return None
        sections.append(f"<!-- Page {page_no} -->\n\n{page_text}")
        logger.info("  BLSC OCR: 第 %d/%d 页完成", page_no, len(pages))

    content = "\n\n---\n\n".join(sections)
    logger.info("  ✅ BLSC OCR: 完成 (%d 字节, %d 页)", len(content), len(pages))
    return content


# ═══════════════════════════════════════════════════════════
#  引擎 3: Docling
# ═══════════════════════════════════════════════════════════

def extract_docling(paper_dir: Path, paper_id: str) -> Optional[str]:
    """使用 Docling 解析 PDF"""
    pdf_path = paper_dir / "paper.pdf"
    if not pdf_path.exists():
        return None
    
    # 检查 Docling 虚拟环境
    docling_python = DOCLING_VENV / "bin" / "python"
    if not docling_python.exists():
        logger.warning("  ⚠️ Docling 虚拟环境不存在，跳过")
        return None
    
    logger.info(f"  📄 Docling: 解析中...")
    
    import subprocess
    
    # 使用 Docling 虚拟环境的 Python 运行解析
    script = f'''
import sys
import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from pathlib import Path
from docling.document_converter import DocumentConverter

pdf_path = Path("{pdf_path}")
converter = DocumentConverter()
result = converter.convert(str(pdf_path))
print(result.document.export_to_markdown())
'''
    
    try:
        result = subprocess.run(
            [str(docling_python), "-c", script],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(PROJECT_ROOT),
        )
        
        if result.returncode == 0 and result.stdout.strip():
            content = result.stdout.strip()
            logger.info(f"  ✅ Docling: 成功 ({len(content):,} 字节)")
            return content
        else:
            logger.warning(f"  ❌ Docling: 失败 — {result.stderr[:200] if result.stderr else '空输出'}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.warning(f"  ⏰ Docling: 超时 (300s)")
        return None
    except Exception as e:
        logger.warning(f"  ❌ Docling: 异常 — {e}")
        return None


# ═══════════════════════════════════════════════════════════
#  引擎 4: PyMuPDF（备选）
# ═══════════════════════════════════════════════════════════

def extract_pymupdf(paper_dir: Path, paper_id: str) -> Optional[str]:
    """使用 PyMuPDF 提取 PDF 文本"""
    pdf_path = paper_dir / "paper.pdf"
    if not pdf_path.exists():
        return None
    
    try:
        import fitz
    except ImportError:
        logger.warning("  ⚠️ PyMuPDF 未安装，跳过")
        return None
    
    logger.info(f"  📄 PyMuPDF: 提取中...")
    
    doc = fitz.open(str(pdf_path))
    sections = []
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text()
        text = _clean_pymupdf_text(text, page_num + 1)
        sections.append(f"<!-- Page {page_num + 1} -->\n\n{text}")
    
    doc.close()
    
    content = "\n\n---\n\n".join(sections)
    logger.info(f"  ✅ PyMuPDF: 完成 ({len(content):,} 字节, {len(sections)} 页)")
    return content


def _clean_pymupdf_text(text: str, page_num: int) -> str:
    """清理 PyMuPDF 提取的文本"""
    # 移除多余空白行
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+$', '', text, flags=re.MULTILINE)
    
    # 合并断行
    lines = text.split('\n')
    merged = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            merged.append('')
            i += 1
            continue
        if (i + 1 < len(lines) and
            not line.endswith(('.', '?', '!', ':', ';')) and
            len(line) > 30 and
            lines[i + 1].strip() and
            not lines[i + 1].strip().startswith(('•', '-', '*', '1.', '2.', '3.'))):
            merged.append(line + ' ' + lines[i + 1].strip())
            i += 2
        else:
            merged.append(line)
            i += 1
    
    text = '\n'.join(merged)
    
    # 标记数学公式
    if any(c in text for c in '∫∑∏∂∇√∞≈≠≤≥±×÷'):
        text = re.sub(r'[∫∑∏∂∇√∞≈≠≤≥±×÷][^\n]{0,50}',
                     '[FORMULA - 需要人工校对]', text)
    
    return text


# ═══════════════════════════════════════════════════════════
#  主流程
# ═══════════════════════════════════════════════════════════

def extract_paper(paper_id: str, engine: Optional[str] = None, force: bool = False, external_pdf: Optional[str] = None, papers_dir: Optional[Path] = None) -> bool:
    """
    提取单篇论文。
    
    参数:
        paper_id: 论文 ID
        engine: 强制使用指定引擎（mineru/docling/pymupdf）
        force: 强制重新提取（忽略优先级）
    """
    papers_dir = papers_dir or PAPERS_DIR
    paper_dir = papers_dir / paper_id

    # Inbox/archive callers may preserve the original title as the PDF filename.
    # The extractor contract is one canonical local input: paper.pdf.
    if not external_pdf and paper_dir.exists() and not (paper_dir / "paper.pdf").exists():
        pdfs = sorted(path for path in paper_dir.glob("*.pdf") if path.is_file())
        if len(pdfs) == 1:
            canonical_pdf = paper_dir / "paper.pdf"
            shutil.copy2(pdfs[0], canonical_pdf)
            logger.warning(
                "⚠️ 归档 PDF 文件名已规范化: %s → paper.pdf",
                pdfs[0].name,
            )
        elif len(pdfs) > 1:
            logger.error("❌ 论文目录存在多个 PDF 且缺少 paper.pdf，无法安全选择: %s",
                         ", ".join(path.name for path in pdfs))
            return False
    
    # 规则 3：外部 PDF 实体复制到 paper.pdf（不符号链接，见 link_external_pdf）
    if external_pdf:
        paper_dir.mkdir(parents=True, exist_ok=True)
        linked = link_external_pdf(paper_dir, external_pdf)
        if linked is None and not (paper_dir / "paper.pdf").exists():
            logger.error(f"❌ 外部 PDF 不可用且无本地 paper.pdf: {external_pdf}")
            return False
    elif not paper_dir.exists():
        logger.error(f"❌ 论文目录不存在: {paper_dir}")
        return False
    
    md_path = paper_dir / "paper.md"
    has_existing_md = md_path.exists()  # 规则 2：仅"已有 md"才适用无标记=low
    meta = load_parse_meta(paper_dir)
    current_engine = meta.get("preferred")
    
    logger.info(f"\n{'='*60}")
    logger.info(f"📄 {paper_id}")
    logger.info(f"   当前引擎: {current_engine or '无'}")
    
    # ── 确定提取策略 ──
    if engine:
        # 用户指定引擎：已有 md 才检查覆盖（新论文无 md 任意引擎可提取）
        if not force and has_existing_md and not can_override(current_engine, engine):
            cur_desc = current_engine or "无标记(low)"
            cur_pri = 1 if current_engine is None else ENGINE_PRIORITY.get(current_engine, 0)
            logger.info(f"   ⚠️ {engine} (优先级 {ENGINE_PRIORITY[engine]}) 不能覆盖 "
                       f"{cur_desc} (优先级 {cur_pri})；同档或更低需 --force")
            return False
        engines_to_try = [engine]
    elif force:
        # 强制重提取：按优先级尝试所有引擎
        engines_to_try = ["mineru", "blsc_ocr", "docling", "pymupdf"]
    else:
        # 自动模式：只尝试比当前引擎更高优先级的引擎
        if not has_existing_md:
            current_priority = 0  # 新论文无 md：尝试所有引擎（含 pymupdf 兜底）
        elif current_engine is None:
            current_priority = 1  # 规则 2：无标记现有 md 视为 low
        else:
            current_priority = ENGINE_PRIORITY.get(current_engine, 0)
        engines_to_try = [
            e for e in ["mineru", "blsc_ocr", "docling", "pymupdf"]
            if ENGINE_PRIORITY[e] > current_priority
        ]
        
        if not engines_to_try:
            logger.info(f"   ✅ 已是最高质量 ({current_engine or '无标记'})，跳过")
            return True
    
    # ── 按优先级尝试引擎 ──
    extractors = {
        "mineru": extract_mineru,
        "blsc_ocr": extract_blsc_ocr,
        "docling": extract_docling,
        "pymupdf": extract_pymupdf,
    }
    
    best_content = None
    best_engine = None
    
    for eng in engines_to_try:
        extractor = extractors[eng]
        content = extractor(paper_dir, paper_id)
        
        if content:
            best_content = content
            best_engine = eng
            # 第一个成功的引擎就是最佳选择，停止尝试
            break
        # MinerU 失败仅允许回落 BLSC OCR；不静默回落到本地 docling/pymupdf。
        # 用户显式 --engine docling/pymupdf 时 engines_to_try 只含单个引擎，不受此限制。
        if eng == "mineru" and len(engines_to_try) > 1 and "blsc_ocr" not in engines_to_try:
            logger.warning(
                "  ⚠️ MinerU 失败且未启用 BLSC OCR 回退，论文 PDF 不回落 docling/pymupdf；"
                "如需强制使用低优先级引擎，请 --engine docling 或 --engine pymupdf"
            )
            break
        if eng == "blsc_ocr" and len(engines_to_try) > 1 and "docling" in engines_to_try:
            logger.warning(
                "  ⚠️ BLSC OCR 失败，论文 PDF 不回落 docling/pymupdf（静默回落会掩盖质量问题）；"
                "如需强制使用低优先级引擎，请 --engine docling 或 --engine pymupdf"
            )
            break
    
    # ── 保存结果 ──
    if best_content is None:
        # 所有引擎都失败，保留现有的 paper.md（如果有）
        if not md_path.exists():
            logger.error(f"   ❌ 所有引擎均失败，且无现有 paper.md")
            return False
        logger.warning(f"   ⚠️ 所有引擎均失败，保留现有 paper.md ({current_engine})")
        return True

    if best_engine != "mineru":
        logger.warning(
            "⚠️ MinerU 未产出本次论文解析，当前使用 %s；结果可能需要人工复核。",
            best_engine,
        )
    
    # 备份旧文件
    if md_path.exists():
        backup_path = md_path.with_suffix('.md.bak')
        shutil.copy2(md_path, backup_path)
        logger.info(f"   💾 备份: {backup_path.name}")
    
    # 写入新文件
    md_path.write_text(best_content, encoding="utf-8")
    
    # 更新元数据
    meta["preferred"] = best_engine
    meta["engines"] = meta.get("engines", {})
    meta["engines"][best_engine] = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "size_bytes": len(best_content.encode("utf-8")),
        "line_count": best_content.count('\n') + 1,
    }
    save_parse_meta(paper_dir, meta)
    remove_lower_quality_backups(paper_dir, best_engine)
    
    logger.info(f"   ✅ 写入 paper.md (引擎: {best_engine}, {len(best_content):,} 字节)")
    return True


def batch_extract(force: bool = False, engine: Optional[str] = None, papers_dir: Optional[Path] = None):
    """批量提取所有论文"""
    raw_dir = papers_dir or PAPERS_DIR
    
    if not raw_dir.exists():
        logger.error(f"❌ 目录不存在: {raw_dir}")
        return
    
    paper_dirs = sorted(raw_dir.iterdir())
    paper_dirs = [d for d in paper_dirs if d.is_dir() and (d / "paper.pdf").exists()]
    
    logger.info(f"\n{'='*60}")
    logger.info(f"📚 批量提取: {len(paper_dirs)} 篇论文")
    logger.info(f"   引擎: {engine or '自动 (MinerU > BLSC OCR > Docling > PyMuPDF)'}")
    logger.info(f"   强制: {'是' if force else '否'}")
    logger.info(f"{'='*60}")
    
    success = 0
    skipped = 0
    failed = 0
    
    for paper_dir in paper_dirs:
        paper_id = paper_dir.name
        result = extract_paper(paper_id, engine=engine, force=force)
        
        if result:
            meta = load_parse_meta(paper_dir)
            current = meta.get("preferred", "unknown")
            if current == meta.get("preferred"):
                success += 1
            else:
                skipped += 1
        else:
            failed += 1
    
    logger.info(f"\n{'='*60}")
    logger.info(f"📊 批量提取完成")
    logger.info(f"   成功: {success}")
    logger.info(f"   跳过: {skipped}")
    logger.info(f"   失败: {failed}")
    logger.info(f"{'='*60}")


def main():
    import argparse

    ingest_backend = os.getenv("INGEST_BACKEND", "agent").strip().lower()
    if ingest_backend == "api":
        model = os.getenv("LLM_MODEL", "未配置").strip() or "未配置"
        print(f"[摄入后端] LLM={ingest_backend.upper()}（{model}）；后续语义结果须经程序校验。", file=sys.stderr)
    
    parser = argparse.ArgumentParser(description="PDF → Markdown 提取工具（多引擎级联）")
    parser.add_argument("--paper", type=str, help="论文 ID（目录名,位于 papers-dir 下）")
    parser.add_argument("--engine", type=str, choices=["mineru", "blsc_ocr", "docling", "pymupdf"],
                       help="强制使用指定引擎")
    parser.add_argument("--force", action="store_true", help="强制重新提取")
    parser.add_argument("--external-pdf", type=str, default=None,
                        help="外部 PDF 源（绝对路径或 synology:// 路径），实体复制到 paper.pdf（不符号链接）")
    parser.add_argument("--batch", action="store_true", help="批量处理全部论文")
    parser.add_argument("--papers-dir", type=str, default=None,
                       help="论文目录(默认 academic/raw/works/papers;他人论文传 academic/raw/references/);也可从 config extraction.papers_dir 读")
    
    args = parser.parse_args()
    
    # papers_dir 解析优先级: CLI > config > 默认 PAPERS_DIR
    cli_papers_dir = Path(args.papers_dir) if args.papers_dir else None
    cfg_papers_dir = None
    cfg = load_config()
    cfg_val = (cfg.get("extraction") or {}).get("papers_dir")
    if cfg_val:
        if cfg_val.startswith("synology://"):
            resolved = resolve_synology_path(cfg_val, cfg)
            cfg_papers_dir = resolved
        else:
            cfg_papers_dir = PROJECT_ROOT / cfg_val
    papers_dir = cli_papers_dir or cfg_papers_dir
    
    if args.batch:
        batch_extract(force=args.force, engine=args.engine, papers_dir=papers_dir)
    elif args.paper:
        success = extract_paper(args.paper, engine=args.engine, force=args.force, external_pdf=args.external_pdf, papers_dir=papers_dir)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

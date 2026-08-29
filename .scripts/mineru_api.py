"""MinerU API client used by the PDF extraction pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in missing-dependency environments
    requests = None

logger = logging.getLogger("extractor.mineru")

DEFAULT_BASE_URL = "https://mineru.net/api/v4"
STATE_LABELS = {
    "waiting-file": "等待文件上传",
    "pending": "排队中",
    "running": "解析中",
    "converting": "格式转换中",
    "done": "完成",
    "failed": "失败",
}


class MinerUError(RuntimeError):
    """Raised when a MinerU API operation cannot produce Markdown."""


class MinerUAuthError(MinerUError):
    """Raised when MinerU rejects the configured API token."""


def _require_requests() -> None:
    if requests is None:
        raise MinerUError("缺少 requests 依赖，请运行: pip install requests")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _request_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise MinerUError(f"{operation}返回了非 JSON 响应: HTTP {response.status_code}") from exc

    if response.status_code in (401, 403) or payload.get("code") in (401, 403):
        raise MinerUAuthError(
            f"{operation}认证失败: HTTP {response.status_code} {payload}"
        )
    if response.status_code != 200 or payload.get("code") != 0:
        raise MinerUError(f"{operation}失败: HTTP {response.status_code} {payload}")
    return payload


def _data_id(pdf_path: Path) -> str:
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:12]
    return f"{pdf_path.stem}-{digest}"


def apply_upload_url(
    token: str,
    pdf_path: Path,
    model_version: str,
    base_url: str,
    timeout_sec: int,
) -> tuple[str, str]:
    _require_requests()
    response = requests.post(
        f"{base_url.rstrip('/')}/file-urls/batch",
        headers=_headers(token),
        json={
            "files": [{"name": pdf_path.name, "data_id": _data_id(pdf_path)}],
            "model_version": model_version,
        },
        timeout=timeout_sec,
    )
    payload = _request_json(response, "申请 MinerU 上传链接")
    data = payload.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or not file_urls:
        raise MinerUError(f"申请上传链接成功但响应不完整: {payload}")
    return batch_id, file_urls[0]


def upload_file(upload_url: str, pdf_path: Path, timeout_sec: int) -> None:
    _require_requests()
    with pdf_path.open("rb") as source:
        response = requests.put(upload_url, data=source, timeout=timeout_sec)
    if response.status_code != 200:
        raise MinerUError(f"上传 PDF 失败: HTTP {response.status_code}")


def poll_result(
    token: str,
    batch_id: str,
    pdf_path: Path,
    base_url: str,
    timeout_sec: int,
    interval_sec: int,
    request_timeout_sec: int,
) -> dict[str, Any]:
    _require_requests()
    deadline = time.monotonic() + timeout_sec
    url = f"{base_url.rstrip('/')}/extract-results/batch/{batch_id}"

    while time.monotonic() < deadline:
        response = requests.get(
            url,
            headers=_headers(token),
            timeout=request_timeout_sec,
        )
        payload = _request_json(response, "查询 MinerU 解析状态")
        results = (payload.get("data") or {}).get("extract_result") or []
        item = _select_result(results, pdf_path)
        if item is None:
            time.sleep(interval_sec)
            continue

        state = item.get("state", "")
        label = STATE_LABELS.get(state, state)
        logger.info("  MinerU 状态: %s", label)
        progress = item.get("extract_progress") or {}
        if progress.get("total_pages"):
            logger.info(
                "  MinerU 进度: %s/%s 页",
                progress.get("extracted_pages", 0),
                progress.get("total_pages"),
            )

        if state == "done":
            return item
        if state == "failed":
            raise MinerUError(f"MinerU 解析失败: {item.get('err_msg', '未知错误')}")
        time.sleep(interval_sec)

    raise MinerUError(f"MinerU 解析轮询超时 ({timeout_sec}s)，batch_id={batch_id}")


def _select_result(results: list[dict[str, Any]], pdf_path: Path) -> Optional[dict[str, Any]]:
    if not results:
        return None
    for item in results:
        file_name = item.get("file_name") or item.get("name")
        if file_name == pdf_path.name:
            return item
    if len(results) == 1:
        return results[0]
    return None


def download_file(url: str, destination: Path, timeout_sec: int) -> None:
    _require_requests()
    response = requests.get(url, timeout=timeout_sec)
    if response.status_code != 200:
        raise MinerUError(f"下载 MinerU 结果失败: HTTP {response.status_code}")
    destination.write_bytes(response.content)


def safe_extract_zip(zip_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    root = output_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            target = (output_dir / member.filename).resolve()
            if target != root and root not in target.parents:
                raise MinerUError(f"MinerU ZIP 包含非法路径: {member.filename}")
        archive.extractall(output_dir)


def find_main_markdown(job_dir: Path, pdf_stem: str) -> Optional[Path]:
    candidates = []
    for path in job_dir.rglob("*.md"):
        lowered = path.name.lower()
        if any(marker in lowered for marker in ("asset", "image", "layout")):
            continue
        try:
            if path.stat().st_size > 0:
                candidates.append(path)
        except OSError:
            continue

    if not candidates:
        return None

    def score(path: Path) -> tuple[int, int]:
        name = path.name.lower()
        priority = 0
        if "full" in name:
            priority += 100
        if pdf_stem.lower() in name:
            priority += 50
        return priority, path.stat().st_size

    return max(candidates, key=score)


def extract_pdf_with_mineru(
    pdf_path: Path,
    token: str,
    *,
    model_version: str = "vlm",
    base_url: str = DEFAULT_BASE_URL,
    timeout_sec: int = 1800,
    interval_sec: int = 5,
    request_timeout_sec: int = 60,
    work_dir: Optional[Path] = None,
    keep_archive: bool = False,
) -> str:
    """Upload one PDF to MinerU and return its main Markdown output."""
    _require_requests()
    if not pdf_path.is_file():
        raise MinerUError(f"PDF 不存在: {pdf_path}")
    if not token.strip():
        raise MinerUError("未配置 MinerU API Token")

    temp_root: Optional[Path] = None
    if work_dir is None:
        temp_root = Path(tempfile.mkdtemp(prefix="mineru-"))
        job_dir = temp_root
    else:
        job_dir = work_dir / f"MinerU_{pdf_path.stem}"
        if job_dir.exists():
            shutil.rmtree(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)

    zip_path = job_dir / f"{pdf_path.stem}_mineru.zip"
    try:
        logger.info("  MinerU API: 申请上传链接...")
        batch_id, upload_url = apply_upload_url(
            token, pdf_path, model_version, base_url, request_timeout_sec
        )
        logger.info("  MinerU API: 上传 PDF...")
        upload_file(upload_url, pdf_path, request_timeout_sec * 10)
        logger.info("  MinerU API: 等待解析完成...")
        result = poll_result(
            token,
            batch_id,
            pdf_path,
            base_url,
            timeout_sec,
            interval_sec,
            request_timeout_sec,
        )
        zip_url = result.get("full_zip_url")
        if not zip_url:
            raise MinerUError("MinerU 任务完成但没有 full_zip_url")

        logger.info("  MinerU API: 下载并解压 Markdown...")
        download_file(zip_url, zip_path, request_timeout_sec * 10)
        safe_extract_zip(zip_path, job_dir)
        markdown_path = find_main_markdown(job_dir, pdf_path.stem)
        if markdown_path is None:
            raise MinerUError("MinerU 结果 ZIP 中没有可用 Markdown")
        content = markdown_path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            raise MinerUError(f"MinerU Markdown 为空: {markdown_path}")

        meta = {
            "pdf_name": pdf_path.name,
            "batch_id": batch_id,
            "model_version": model_version,
            "state": result.get("state"),
            "markdown_name": str(markdown_path.relative_to(job_dir)),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        (job_dir / "mineru_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return content
    finally:
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
        elif not keep_archive:
            shutil.rmtree(job_dir, ignore_errors=True)

#!/usr/bin/env python3
"""Bounded, resumable extraction using the official PaddleOCR document API."""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

try:
    import requests
except ImportError:
    requests = None


MODEL = "PaddleOCR-VL-1.6"
JOBS_URL = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:<)?([^\s)>]+)")
HTML_IMAGE_RE = re.compile(r"<img\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE)
MAX_RESULT_BYTES = 50 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_ASSET_BYTES = 50 * 1024 * 1024


class PaddleOCRError(RuntimeError):
    pass


class PaddleOCRRejectedError(PaddleOCRError):
    pass


@dataclass(frozen=True)
class PaddleOCRExtraction:
    markdown: str
    artifact_root: Path
    meta: dict
    sidecar_paths: tuple[Path, ...] = ()


def is_official_paddleocr_record(record: dict) -> bool:
    return (isinstance(record, dict)
            and record.get("source") == "paddleocr_official_api"
            and record.get("model") == MODEL and record.get("task") == "doc_parsing"
            and record.get("upload_authorization") == "public_pdf"
            and isinstance(record.get("page_count"), int) and record["page_count"] > 0
            and bool(record.get("job_id")) and bool(record.get("fallback_reason"))
            and all(isinstance(record.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{64}", record[key])
                    for key in ("input_sha256", "request_sha256")))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_checkpoint(path: Path, checkpoint: dict) -> None:
    temporary = path.with_suffix(".partial")
    if path.is_symlink() or temporary.is_symlink():
        raise PaddleOCRError("checkpoint 路径不能是软链接")
    temporary.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _api_data(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaddleOCRError("官方 API 返回无效 JSON") from exc
    if not isinstance(payload, dict):
        raise PaddleOCRError("官方 API 响应必须是对象")
    if payload.get("code", 0) not in (0, None):
        raise PaddleOCRRejectedError("官方 API 拒绝请求；检查账户权限、配额和输入限制")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PaddleOCRError("官方 API 响应缺少 data 对象")
    return data


def _request(method: str, url: str, *, timeout: float, **kwargs) -> requests.Response:
    attempts = 2 if method == "GET" else 1
    deadline = time.monotonic() + timeout
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PaddleOCRError("官方 API 请求预算耗尽；保留任务供恢复")
        try:
            response = requests.request(
                method, url, timeout=remaining, allow_redirects=False, **kwargs,
            )
        except requests.RequestException:
            if attempt + 1 == attempts:
                raise PaddleOCRError("官方 API 网络请求失败；保留任务供恢复") from None
            time.sleep(min(1, max(0, deadline - time.monotonic())))
            continue
        if 200 <= response.status_code < 300:
            return response
        status = response.status_code
        response.close()
        if status in {408, 425} or status >= 500:
            if attempt + 1 < attempts:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
        error_type = PaddleOCRRejectedError if 400 <= status < 500 and status != 408 else PaddleOCRError
        raise error_type(f"官方 API HTTP {status}；不自动重提任务")
    raise PaddleOCRError("官方 API 请求失败")


def _resource_url(url: str) -> None:
    if not isinstance(url, str):
        raise PaddleOCRError("资源 URL 必须是字符串")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    allowed = host == "paddleocr.aistudio-app.com" or host.endswith(".bcebos.com")
    try:
        port = parsed.port
    except ValueError:
        raise PaddleOCRError("资源 URL 端口非法") from None
    if (parsed.scheme != "https" or not allowed or port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise PaddleOCRError("拒绝非官方 HTTPS 资源地址")


def _download(url: str, target: Path, *, timeout: float, max_bytes: int) -> None:
    deadline = time.monotonic() + timeout
    temporary = target.with_suffix(target.suffix + ".partial")
    if target.is_symlink() or temporary.is_symlink():
        raise PaddleOCRError("下载目标不能是软链接")
    response = None
    try:
        for _ in range(4):
            _resource_url(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PaddleOCRError("资源下载预算耗尽")
            response = requests.get(url, timeout=remaining, stream=True, allow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = response.headers.get("Location", "")
                response.close()
                continue
            if response.status_code != 200:
                raise PaddleOCRError(f"资源下载 HTTP {response.status_code}")
            break
        else:
            raise PaddleOCRError("资源重定向次数超限")
        target.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        with temporary.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                total += len(chunk)
                if total > max_bytes or time.monotonic() >= deadline:
                    raise PaddleOCRError("资源下载超出大小或时间预算")
                stream.write(chunk)
        if not total:
            raise PaddleOCRError("资源下载为空")
        os.replace(temporary, target)
    except requests.RequestException:
        raise PaddleOCRError("资源下载失败；保留远端任务供恢复") from None
    finally:
        if response is not None:
            response.close()
        temporary.unlink(missing_ok=True)


class _ImageTagParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


def _normalize_html_images(text: str) -> str:
    def normalize(match):
        parser = _ImageTagParser()
        parser.feed(match.group(0))
        parser.close()
        if len(parser.tags) != 1 or parser.tags[0][0] != "img":
            raise PaddleOCRError("HTML 图片标签格式非法")
        attributes = parser.tags[0][1]
        names = [name for name, value in attributes]
        if (len(names) != len(set(names))
                or set(names) - {"src", "alt", "width", "height", "title"}):
            raise PaddleOCRError("HTML 图片包含重复或未经授权的属性")
        values = dict(attributes)
        reference = values.get("src")
        if not reference or re.search(r"[\s<>\"'()]", reference):
            raise PaddleOCRError("HTML 图片缺少合法 src")
        alt = " ".join((values.get("alt") or "Image").split())
        alt = html.escape(alt.replace("[", "(").replace("]", ")"), quote=False)
        return f"![{alt}]({reference})"

    normalized = HTML_IMAGE_RE.sub(normalize, text)
    if re.search(r"<img\b", normalized, re.IGNORECASE):
        raise PaddleOCRError("HTML 图片标签格式非法")
    return normalized


def _materialize_pages(records: list, root: Path, *, timeout: float) -> tuple[str, int]:
    if root.is_symlink() or (root / "images").is_symlink():
        raise PaddleOCRError("图片暂存目录不能是软链接")
    pages = []
    total_assets = 0
    deadline = time.monotonic() + timeout
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("result"), dict):
            raise PaddleOCRError("官方 JSONL 缺少 result 对象")
        results = record["result"].get("layoutParsingResults")
        if not isinstance(results, list):
            raise PaddleOCRError("官方 JSONL 缺少 layoutParsingResults")
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("markdown"), dict):
                raise PaddleOCRError("解析页缺少 markdown 对象")
            text = item["markdown"].get("text")
            images = item["markdown"].get("images", {})
            if not isinstance(text, str) or not isinstance(images, dict):
                raise PaddleOCRError("解析页 Markdown 或图片映射格式错误")
            text = _normalize_html_images(text)
            replacements = {}
            for match in IMAGE_RE.finditer(text):
                reference = unquote(match.group(1))
                relative = PurePosixPath(reference)
                parsed = urlsplit(reference)
                if (parsed.scheme or parsed.netloc or relative.is_absolute()
                        or ".." in relative.parts or "\\" in reference):
                    raise PaddleOCRError("Markdown 图片路径非法")
                if reference in replacements:
                    continue
                url = images.get(reference)
                if not isinstance(url, str) or not url:
                    raise PaddleOCRError("Markdown 引用的图片缺少资源映射")
                suffix = relative.suffix.lower()
                if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    raise PaddleOCRError("图片格式不在受管 allowlist")
                name = hashlib.sha256(reference.encode()).hexdigest()[:16]
                destination = f"images/page-{len(pages) + 1}-{name}{suffix}"
                remaining = deadline - time.monotonic()
                if remaining <= 0 or total_assets >= MAX_ASSET_BYTES:
                    raise PaddleOCRError("图片资源总预算耗尽")
                _download(url, root / destination, timeout=remaining,
                          max_bytes=min(MAX_IMAGE_BYTES, MAX_ASSET_BYTES - total_assets))
                total_assets += (root / destination).stat().st_size
                replacements[reference] = destination
            def replace_reference(match):
                start = match.start(1) - match.start()
                end = match.end(1) - match.start()
                return (match.group(0)[:start] + replacements[unquote(match.group(1))]
                        + match.group(0)[end:])

            text = IMAGE_RE.sub(replace_reference, text)
            pages.append(text)
            if len(pages) > 200:
                raise PaddleOCRError("解析页数超出本地 200 页预算")
    if not pages or not any(text.strip() for text in pages):
        raise PaddleOCRError("官方解析未产出非空 Markdown")
    return "\n\n".join(pages).strip() + "\n", len(pages)


def extract_pdf_bundle_with_paddleocr(
    pdf_path: Path, token: str, *, work_dir: Path,
    request_timeout_sec: int = 60, poll_timeout_sec: int = 300,
    interval_sec: int = 5,
) -> PaddleOCRExtraction:
    if requests is None:
        raise PaddleOCRError("缺少 requests 依赖；请安装已有 API 客户端依赖 requests")
    if not token.strip():
        raise PaddleOCRError("未配置 PADDLEOCR_ACCESS_TOKEN")
    if (not pdf_path.is_file() or pdf_path.is_symlink()
            or not 0 < pdf_path.stat().st_size <= 200 * 1024 * 1024):
        raise PaddleOCRError("PDF 非普通文件或超出本地 200MB 预算")
    if min(request_timeout_sec, poll_timeout_sec, interval_sec) <= 0:
        raise PaddleOCRError("请求、轮询和间隔预算必须为正数")
    if work_dir.is_symlink():
        raise PaddleOCRError("任务暂存目录不能是软链接")
    work_dir.mkdir(parents=True, exist_ok=True)
    input_hash = _sha256_file(pdf_path)
    options = {"returnMarkdownImages": True}
    request_hash = hashlib.sha256(json.dumps({
        "input_sha256": input_hash, "model": MODEL,
        "endpoint": JOBS_URL, "optionalPayload": options,
    }, sort_keys=True).encode()).hexdigest()
    checkpoint_path = work_dir / "paddleocr-job-v1.json"
    checkpoint = {}
    if checkpoint_path.exists():
        if checkpoint_path.is_symlink():
            raise PaddleOCRError("任务 checkpoint 不能是软链接")
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise PaddleOCRError("任务 checkpoint 损坏；不得猜测或重复提交") from None
        if (not isinstance(checkpoint, dict)
                or checkpoint.get("input_sha256") != input_hash
                or checkpoint.get("request_sha256") != request_hash):
            raise PaddleOCRError("任务输入/请求哈希不匹配；不得复用或自动重提")
    checkpoint.update(input_sha256=input_hash, request_sha256=request_hash)
    headers = {"Authorization": f"Bearer {token}"}
    job_id = checkpoint.get("job_id")
    if not job_id:
        if checkpoint.get("status") == "submitting":
            raise PaddleOCRError("上次提交结果不明；先核对官网任务，不自动重复上传")
        checkpoint["status"] = "submitting"
        _save_checkpoint(checkpoint_path, checkpoint)
        try:
            with pdf_path.open("rb") as stream:
                response = _request(
                    "POST", JOBS_URL, timeout=request_timeout_sec, headers=headers,
                    data={"model": MODEL, "optionalPayload": json.dumps(options)},
                    files={"file": stream},
                )
            try:
                job_id = _api_data(response).get("jobId")
            finally:
                response.close()
        except PaddleOCRRejectedError:
            checkpoint["status"] = "rejected"
            _save_checkpoint(checkpoint_path, checkpoint)
            raise
        if not isinstance(job_id, str) or not job_id:
            raise PaddleOCRError("提交响应缺少 jobId；不自动重复上传")
        checkpoint.update(job_id=job_id, status="submitted")
        _save_checkpoint(checkpoint_path, checkpoint)
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise PaddleOCRError("checkpoint jobId 格式错误")
    deadline = time.monotonic() + poll_timeout_sec
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PaddleOCRError("官方 API 轮询超时；恢复时继续原 jobId")
        response = _request(
            "GET", f"{JOBS_URL}/{quote(job_id, safe='')}",
            timeout=min(request_timeout_sec, remaining), headers=headers,
        )
        try:
            status = _api_data(response)
        finally:
            response.close()
        state = status.get("state")
        if state == "done":
            result = status.get("resultUrl")
            result_url = result.get("jsonUrl") if isinstance(result, dict) else None
            if not isinstance(result_url, str):
                raise PaddleOCRError("完成任务缺少 resultUrl.jsonUrl")
            break
        if state == "failed":
            raise PaddleOCRError("官方解析任务失败；不自动重复提交")
        if state not in {"pending", "running"}:
            raise PaddleOCRError("官方 API 返回未知任务状态")
        time.sleep(min(interval_sec, max(0, deadline - time.monotonic())))
    result_path = work_dir / "result.jsonl"
    _download(result_url, result_path, timeout=request_timeout_sec, max_bytes=MAX_RESULT_BYTES)
    try:
        records = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
    except (ValueError, UnicodeError):
        raise PaddleOCRError("官方结果不是合法 UTF-8 JSONL") from None
    artifact_root = work_dir / "artifacts"
    if artifact_root.is_symlink():
        raise PaddleOCRError("资源暂存目录不能是软链接")
    artifact_root.mkdir(exist_ok=True)
    markdown, pages = _materialize_pages(records, artifact_root, timeout=request_timeout_sec)
    checkpoint["status"] = "complete"
    _save_checkpoint(checkpoint_path, checkpoint)
    return PaddleOCRExtraction(markdown, artifact_root, {
        "source": "paddleocr_official_api", "model": MODEL,
        "task": "doc_parsing", "job_id": job_id, "page_count": pages,
        "input_sha256": input_hash, "request_sha256": request_hash,
    })

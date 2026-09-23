"""Resumable MinerU API client used by the PDF extraction pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import shutil
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in missing-dependency environments
    requests = None

logger = logging.getLogger("extractor.mineru")

DEFAULT_BASE_URL = "https://mineru.net/api/v4"
STANDARD_MAX_BYTES = 200 * 1024 * 1024
STANDARD_MAX_PAGES = 200
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
RETRY_ATTEMPTS = 3
RETRY_MAX_DELAY = 30.0
AUTH_CODES = {"A0202", "A0211", 401, 403}
QUOTA_CODES = {-60017, -60018, -60019}
INPUT_CODES = {-60005, -60006, -60012, -60013, -60014}
UNSUPPORTED_CODES = {-60007, -60008}
RETRYABLE_API_CODES = {-10001, -60001, -60009, -60020}
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
STATE_LABELS = {
    "waiting-file": "等待文件上传",
    "pending": "排队中",
    "running": "解析中",
    "converting": "格式转换中",
    "done": "完成",
    "failed": "失败",
}
SIDECAR_SUFFIXES = ("content_list.json", "content-list.json", "layout.json", "middle.json")


class MinerUError(RuntimeError):
    """Base error for a MinerU operation that cannot produce a valid bundle."""

    def __init__(self, message: str, *, code: Any = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MinerUAuthError(MinerUError):
    """MinerU rejected the configured API token."""


class MinerUQuotaError(MinerUError):
    """MinerU quota is exhausted; retrying the same run cannot help."""


class MinerUInputError(MinerUError):
    """The input or requested operation violates a MinerU constraint."""


class MinerUTransientError(MinerUError):
    """A bounded retryable transport or service failure."""


class MinerURemoteError(MinerUError):
    """The accepted MinerU job reached a terminal failed state."""


@dataclass(frozen=True)
class MinerUExtraction:
    markdown: str
    markdown_path: Path
    artifact_root: Path
    image_paths: tuple[Path, ...]
    sidecar_paths: tuple[Path, ...]
    meta: dict[str, Any]
    checkpoint_path: Path


def _require_requests() -> None:
    if requests is None:
        raise MinerUError("缺少 requests 依赖，请运行: pip install requests")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _error_for(operation: str, status: int, payload: dict[str, Any]) -> MinerUError:
    code = payload.get("msgCode") if payload.get("success") is False else payload.get("code")
    if code is None and status != 200:
        code = status
    detail = payload.get("msg") or payload.get("message") or payload
    message = f"{operation}失败: HTTP {status} code={code!r} {detail}"
    if status in (401, 403) or code in AUTH_CODES:
        return MinerUAuthError(message, code=code)
    if code in QUOTA_CODES:
        return MinerUQuotaError(message, code=code)
    if code in INPUT_CODES or code in UNSUPPORTED_CODES:
        return MinerUInputError(message, code=code)
    if status in RETRYABLE_HTTP_STATUSES or code in RETRYABLE_API_CODES:
        return MinerUTransientError(message, code=code, retryable=True)
    return MinerUError(message, code=code)


def _request_json(response: Any, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise MinerUError(f"{operation}返回了非 JSON 响应: HTTP {response.status_code}") from exc
    if not isinstance(payload, dict):
        raise MinerUError(f"{operation}返回了非对象 JSON: HTTP {response.status_code}")
    success = payload.get("success") is not False
    code = payload.get("code")
    if response.status_code != 200 or not success or code not in (0, None):
        raise _error_for(operation, response.status_code, payload)
    if code is None and "success" not in payload and "data" not in payload:
        raise MinerUError(f"{operation}返回结构不符合 MinerU API 契约: {payload}")
    return payload


def _backoff_delay(attempt: int, retry_after: Any = None) -> float:
    if retry_after is not None:
        try:
            return min(float(retry_after), RETRY_MAX_DELAY)
        except (TypeError, ValueError):
            pass
    ceiling = min(RETRY_MAX_DELAY, 1.0 * (2 ** attempt))
    return ceiling * (0.5 + random.random() / 2.0)


def _request_with_retry(
    method: str,
    url: str,
    *,
    timeout: int,
    retry_state: Optional[dict[str, int]] = None,
    **kwargs: Any,
) -> Any:
    _require_requests()
    last_error: Optional[BaseException] = None
    for attempt in range(RETRY_ATTEMPTS):
        body = kwargs.get("data")
        if hasattr(body, "seek"):
            body.seek(0)
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code not in RETRYABLE_HTTP_STATUSES:
                return response
            last_error = MinerUTransientError(
                f"MinerU HTTP {response.status_code}", code=response.status_code, retryable=True
            )
            retry_after = (getattr(response, "headers", None) or {}).get("Retry-After")
            try:
                response.close()
            except Exception:
                pass
        except requests.RequestException as exc:
            last_error = exc
            retry_after = None
        if attempt + 1 >= RETRY_ATTEMPTS:
            break
        if retry_state is not None:
            retry_state["http"] = int(retry_state.get("http", 0)) + 1
        time.sleep(_backoff_delay(attempt, retry_after))
    raise MinerUTransientError(
        f"MinerU 网络/服务错误，{RETRY_ATTEMPTS} 次请求后仍失败: {last_error}",
        code=getattr(last_error, "code", None),
        retryable=True,
    )


def _request_api_json(
    method: str,
    url: str,
    *,
    operation: str,
    timeout: int,
    retry_state: Optional[dict[str, int]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    for attempt in range(RETRY_ATTEMPTS):
        response = _request_with_retry(
            method, url, timeout=timeout, retry_state=retry_state, **kwargs
        )
        try:
            return _request_json(response, operation)
        except MinerUTransientError:
            if attempt + 1 >= RETRY_ATTEMPTS:
                raise
            if retry_state is not None:
                retry_state["api"] = int(retry_state.get("api", 0)) + 1
            time.sleep(_backoff_delay(attempt))
    raise MinerUTransientError(f"{operation}重试耗尽", retryable=True)


def _data_id(pdf_path: Path) -> str:
    return f"{pdf_path.stem}-{_sha256_file(pdf_path)[:12]}"


def _pdf_page_count(pdf_path: Path) -> Optional[int]:
    try:
        import fitz
        with fitz.open(str(pdf_path)) as document:
            return len(document)
    except Exception:
        return None


def _preflight_pdf(pdf_path: Path) -> None:
    size = pdf_path.stat().st_size
    if size <= 0:
        raise MinerUInputError("MinerU 输入 PDF 为空")
    if size > STANDARD_MAX_BYTES:
        raise MinerUInputError(
            f"MinerU Standard API 文件上限为 200MB，当前 {size} bytes", code=-60005
        )
    pages = _pdf_page_count(pdf_path)
    if pages is not None and pages > STANDARD_MAX_PAGES:
        raise MinerUInputError(
            f"MinerU Standard API 页数上限为 200，当前 {pages} 页", code=-60006
        )


def apply_upload_url(
    token: str,
    pdf_path: Path,
    model_version: str,
    base_url: str,
    timeout_sec: int,
    *,
    language: Optional[str] = None,
    is_ocr: bool = False,
    enable_formula: bool = True,
    enable_table: bool = True,
    retry_state: Optional[dict[str, int]] = None,
) -> tuple[str, str]:
    file_entry: dict[str, Any] = {"name": pdf_path.name, "data_id": _data_id(pdf_path)}
    if is_ocr:
        file_entry["is_ocr"] = True
    body: dict[str, Any] = {
        "files": [file_entry],
        "model_version": model_version,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    if language:
        body["language"] = language
    payload = _request_api_json(
        "POST",
        f"{base_url.rstrip('/')}/file-urls/batch",
        operation="申请 MinerU 上传链接",
        timeout=timeout_sec,
        retry_state=retry_state,
        headers=_headers(token),
        json=body,
    )
    data = payload.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or len(file_urls) != 1:
        raise MinerUError(f"申请上传链接成功但响应不完整: {payload}")
    upload_url = file_urls[0]
    if isinstance(upload_url, dict):
        upload_url = upload_url.get("url") or upload_url.get("file_url") or upload_url.get("upload_url")
    if not upload_url:
        raise MinerUError(f"申请上传链接成功但没有上传地址: {payload}")
    return str(batch_id), str(upload_url)


def upload_file(
    upload_url: str,
    pdf_path: Path,
    timeout_sec: int,
    *,
    retry_state: Optional[dict[str, int]] = None,
) -> None:
    with pdf_path.open("rb") as source:
        response = _request_with_retry(
            "PUT",
            upload_url,
            timeout=timeout_sec,
            retry_state=retry_state,
            data=source,
            headers={"Content-Length": str(pdf_path.stat().st_size)},
        )
    if response.status_code not in (200, 201, 203):
        raise MinerUError(f"上传 PDF 失败: HTTP {response.status_code}", code=response.status_code)


def poll_result(
    token: str,
    batch_id: str,
    pdf_path: Path,
    base_url: str,
    timeout_sec: int,
    interval_sec: int,
    request_timeout_sec: int,
    *,
    retry_state: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    url = f"{base_url.rstrip('/')}/extract-results/batch/{batch_id}"
    current_interval = max(float(interval_sec), 0.0)
    previous_progress: tuple[Any, Any, Any] | None = None
    while time.monotonic() < deadline:
        payload = _request_api_json(
            "GET",
            url,
            operation="查询 MinerU 解析状态",
            timeout=request_timeout_sec,
            retry_state=retry_state,
            headers=_headers(token),
        )
        results = (payload.get("data") or {}).get("extract_result") or []
        item = _select_result(results, pdf_path)
        if item is not None:
            state = item.get("state", "")
            progress = item.get("extract_progress") or {}
            marker = (state, progress.get("extracted_pages"), progress.get("total_pages"))
            logger.info("  MinerU 状态: %s", STATE_LABELS.get(state, state))
            if progress.get("total_pages"):
                logger.info(
                    "  MinerU 进度: %s/%s 页",
                    progress.get("extracted_pages", 0),
                    progress.get("total_pages"),
                )
            if state == "done":
                return item
            if state == "failed":
                code = item.get("err_code") or item.get("code")
                raise MinerURemoteError(
                    f"MinerU 解析失败: {item.get('err_msg', '未知错误')}", code=code
                )
            progressed = marker != previous_progress
            previous_progress = marker
            current_interval = max(float(interval_sec), 0.0) if progressed else min(
                max(current_interval * 1.5, float(interval_sec)), 15.0
            )
        if current_interval:
            time.sleep(current_interval)
    raise MinerUTransientError(
        f"MinerU 解析轮询超时 ({timeout_sec}s)，batch_id={batch_id}", retryable=True
    )


def _select_result(results: list[dict[str, Any]], pdf_path: Path) -> Optional[dict[str, Any]]:
    if not results:
        return None
    data_id = _data_id(pdf_path)
    for item in results:
        if item.get("data_id") == data_id:
            return item
    for item in results:
        file_name = item.get("file_name") or item.get("name")
        if file_name == pdf_path.name:
            return item
    if len(results) == 1:
        return results[0]
    return None


def download_file(
    url: str,
    destination: Path,
    timeout_sec: int,
    *,
    retry_state: Optional[dict[str, int]] = None,
) -> None:
    partial = destination.with_name(f".{destination.name}.partial")
    try:
        response = _request_with_retry(
            "GET", url, timeout=timeout_sec, retry_state=retry_state, stream=True
        )
        if response.status_code != 200:
            raise MinerUError(
                f"下载 MinerU 结果失败: HTTP {response.status_code}", code=response.status_code
            )
        total = 0
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise MinerUInputError("MinerU 结果包超过本地 1GB 安全上限")
                handle.write(chunk)
        os.replace(partial, destination)
    finally:
        if partial.exists():
            partial.unlink()


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    name = member.filename
    parts = PurePosixPath(name).parts
    if PurePosixPath(name).is_absolute() or ".." in parts:
        raise MinerUInputError(f"MinerU ZIP 包含非法路径: {name}")
    file_type = (member.external_attr >> 16) & stat.S_IFMT(0o170000)
    if file_type == stat.S_IFLNK:
        raise MinerUInputError(f"MinerU ZIP 包含软链接: {name}")
    if member.flag_bits & 0x1:
        raise MinerUInputError(f"MinerU ZIP 包含加密成员: {name}")


def safe_extract_zip(zip_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise MinerUInputError(f"MinerU ZIP 成员过多: {len(members)}")
        total = 0
        for member in members:
            _validate_zip_member(member)
            total += member.file_size
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise MinerUInputError("MinerU ZIP 解压体积超过 1GB 安全上限")
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

    def score(path: Path) -> tuple[int, int, str]:
        name = path.name.lower()
        priority = (100 if "full" in name else 0) + (50 if pdf_stem.lower() in name else 0)
        return priority, path.stat().st_size, path.as_posix()

    return max(candidates, key=score)


def _bundle_files(markdown_path: Path) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    root = markdown_path.parent
    images_dir = root / "images"
    images = tuple(sorted(path for path in images_dir.rglob("*") if path.is_file())) \
        if images_dir.is_dir() else ()
    sidecars = tuple(sorted(
        path for path in root.rglob("*.json")
        if path.is_file() and path.name.lower().endswith(SIDECAR_SUFFIXES)
    ))
    return images, sidecars


def _request_fingerprint(
    pdf_path: Path,
    input_sha256: str,
    *,
    model_version: str,
    base_url: str,
    language: Optional[str],
    is_ocr: bool,
    enable_formula: bool,
    enable_table: bool,
) -> str:
    request = {
        "pdf_name": pdf_path.name,
        "input_sha256": input_sha256,
        "model_version": model_version,
        "base_url": base_url.rstrip("/"),
        "language": language,
        "is_ocr": is_ocr,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
    }
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode("utf-8")).hexdigest()


def _load_checkpoint(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def extract_pdf_bundle_with_mineru(
    pdf_path: Path,
    token: str,
    *,
    work_dir: Path,
    model_version: str = "vlm",
    base_url: str = DEFAULT_BASE_URL,
    timeout_sec: int = 1800,
    interval_sec: int = 5,
    request_timeout_sec: int = 60,
    keep_archive: bool = False,
    language: Optional[str] = None,
    is_ocr: bool = False,
    enable_formula: bool = True,
    enable_table: bool = True,
) -> MinerUExtraction:
    """Resume or run one Standard API job and return its validated artifact bundle."""
    _require_requests()
    if not pdf_path.is_file():
        raise MinerUInputError(f"PDF 不存在: {pdf_path}")
    if not token.strip():
        raise MinerUAuthError("未配置 MinerU API Token")
    _preflight_pdf(pdf_path)

    input_sha256 = _sha256_file(pdf_path)
    request_sha256 = _request_fingerprint(
        pdf_path,
        input_sha256,
        model_version=model_version,
        base_url=base_url,
        language=language,
        is_ocr=is_ocr,
        enable_formula=enable_formula,
        enable_table=enable_table,
    )
    job_dir = work_dir / f"MinerU_{pdf_path.stem}"
    checkpoint_path = job_dir / "mineru-job-v1.json"
    checkpoint = _load_checkpoint(checkpoint_path)
    if checkpoint and (
        checkpoint.get("input_sha256") != input_sha256
        or checkpoint.get("request_sha256") != request_sha256
    ):
        shutil.rmtree(job_dir)
        checkpoint = {}
    job_dir.mkdir(parents=True, exist_ok=True)
    result_dir = job_dir / "result"
    zip_path = job_dir / f"{pdf_path.stem}_mineru.zip"
    retry_state = dict(checkpoint.get("retry_count") or {"http": 0, "api": 0})

    def save(**updates: Any) -> None:
        nonlocal checkpoint
        checkpoint.update({
            "schema": "mineru-job-v1",
            "input_sha256": input_sha256,
            "request_sha256": request_sha256,
            "pdf_name": pdf_path.name,
            "data_id": _data_id(pdf_path),
            "model_version": model_version,
            "base_url": base_url.rstrip("/"),
            "options": {
                "language": language,
                "is_ocr": is_ocr,
                "enable_formula": enable_formula,
                "enable_table": enable_table,
            },
            "retry_count": retry_state,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            **updates,
        })
        _atomic_write_json(checkpoint_path, checkpoint)

    if checkpoint.get("status") == "remote_failed":
        raise MinerURemoteError(
            f"MinerU 任务已终态失败，batch_id={checkpoint.get('batch_id')}: "
            f"{checkpoint.get('error', '未知错误')}"
        )
    if checkpoint.get("status") == "complete":
        markdown_rel = checkpoint.get("markdown_path")
        markdown_path = job_dir / str(markdown_rel or "")
        if markdown_path.is_file() and _sha256_file(markdown_path) == checkpoint.get("markdown_sha256"):
            content = markdown_path.read_text(encoding="utf-8", errors="replace").strip()
            images, sidecars = _bundle_files(markdown_path)
            meta = dict(checkpoint.get("result_meta") or {})
            return MinerUExtraction(
                content, markdown_path, markdown_path.parent, images, sidecars, meta, checkpoint_path
            )

    try:
        batch_id = checkpoint.get("batch_id")
        upload_url = checkpoint.get("upload_url")
        if not batch_id:
            logger.info("  MinerU API: 申请上传链接...")
            batch_id, upload_url = apply_upload_url(
                token,
                pdf_path,
                model_version,
                base_url,
                request_timeout_sec,
                language=language,
                is_ocr=is_ocr,
                enable_formula=enable_formula,
                enable_table=enable_table,
                retry_state=retry_state,
            )
            save(status="reserved", batch_id=batch_id, upload_url=upload_url)
        if not checkpoint.get("uploaded"):
            logger.info("  MinerU API: 上传 PDF...")
            upload_file(
                str(upload_url), pdf_path, request_timeout_sec * 10, retry_state=retry_state
            )
            save(status="uploaded", uploaded=True)

        zip_url = checkpoint.get("zip_url")
        remote_result = dict(checkpoint.get("remote_result") or {})
        if not zip_url:
            logger.info("  MinerU API: 等待解析完成...")
            remote_result = poll_result(
                token,
                str(batch_id),
                pdf_path,
                base_url,
                timeout_sec,
                interval_sec,
                request_timeout_sec,
                retry_state=retry_state,
            )
            zip_url = remote_result.get("full_zip_url")
            if not zip_url:
                raise MinerUError("MinerU 任务完成但没有 full_zip_url")
            save(status="done", zip_url=zip_url, remote_result=remote_result)

        if not zip_path.is_file() or checkpoint.get("status") not in {"downloaded", "complete"}:
            logger.info("  MinerU API: 流式下载结果包...")
            download_file(
                str(zip_url), zip_path, request_timeout_sec * 10, retry_state=retry_state
            )
            save(status="downloaded", archive_sha256=_sha256_file(zip_path))

        if result_dir.exists():
            shutil.rmtree(result_dir)
        safe_extract_zip(zip_path, result_dir)
        markdown_path = find_main_markdown(result_dir, pdf_path.stem)
        if markdown_path is None:
            raise MinerUError("MinerU 结果 ZIP 中没有可用 Markdown")
        content = markdown_path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            raise MinerUError(f"MinerU Markdown 为空: {markdown_path}")
        images, sidecars = _bundle_files(markdown_path)
        meta = {
            "schema": "mineru-extraction-v1",
            "pdf_name": pdf_path.name,
            "input_sha256": input_sha256,
            "request_sha256": request_sha256,
            "batch_id": batch_id,
            "data_id": _data_id(pdf_path),
            "model_version": model_version,
            "api_kind": "standard-v4",
            "options": checkpoint["options"],
            "state": remote_result.get("state") or "done",
            "retry_count": retry_state,
            "markdown_name": markdown_path.relative_to(result_dir).as_posix(),
            "markdown_sha256": _sha256_file(markdown_path),
            "image_count": len(images),
            "sidecar_count": len(sidecars),
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _atomic_write_json(job_dir / "mineru_meta.json", meta)
        save(
            status="complete",
            markdown_path=markdown_path.relative_to(job_dir).as_posix(),
            markdown_sha256=meta["markdown_sha256"],
            result_meta=meta,
        )
        if not keep_archive and zip_path.exists():
            zip_path.unlink()
        return MinerUExtraction(
            content, markdown_path, markdown_path.parent, images, sidecars, meta, checkpoint_path
        )
    except MinerURemoteError as exc:
        save(status="remote_failed", error=str(exc), error_code=exc.code)
        raise
    except MinerUError:
        save()
        raise


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
    language: Optional[str] = None,
    is_ocr: bool = False,
    enable_formula: bool = True,
    enable_table: bool = True,
) -> str:
    """Compatibility wrapper returning Markdown while the bundle API owns recovery."""
    temporary: Optional[Path] = None
    if work_dir is None:
        temporary = Path(tempfile.mkdtemp(prefix="mineru-"))
        work_dir = temporary
    try:
        result = extract_pdf_bundle_with_mineru(
            pdf_path,
            token,
            work_dir=work_dir,
            model_version=model_version,
            base_url=base_url,
            timeout_sec=timeout_sec,
            interval_sec=interval_sec,
            request_timeout_sec=request_timeout_sec,
            keep_archive=keep_archive,
            language=language,
            is_ocr=is_ocr,
            enable_formula=enable_formula,
            enable_table=enable_table,
        )
        return result.markdown
    finally:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)

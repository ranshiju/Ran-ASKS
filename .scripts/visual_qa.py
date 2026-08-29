#!/usr/bin/env python3
"""Visual QA for images, PDF pages, and statically rendered PPT/PPTX pages.

The checker combines deterministic image diagnostics with an optional
OpenAI-compatible vision model.  It is deliberately read-only with respect to
the input artifact: all renders and resumable receipts live under
``temp/visual-qa`` (or an explicitly supplied receipt root).

PPT/PPTX support covers the static slide rendering only.  Animations, embedded
video, speaker notes, and transitions are outside this checker's scope.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - reported at use site
    fitz = None

try:
    from PIL import Image, ImageOps, ImageStat
except ImportError:  # pragma: no cover - reported at use site
    Image = ImageOps = ImageStat = None


REPO = Path(__file__).resolve().parent.parent
DEFAULT_RECEIPT_ROOT = REPO / "temp" / "visual-qa"
MODEL_CATALOG = REPO / "operations" / "config" / "llm-models.yaml"
DEFAULT_MODEL = "GLM-4.6V"
DEFAULT_FALLBACK_MODEL = "GLM-4.5V"
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}
SUPPORTED_DOCUMENTS = {".pdf", ".ppt", ".pptx"}
SUPPORTED_EXTENSIONS = SUPPORTED_IMAGES | SUPPORTED_DOCUMENTS
PROFILES = {"auto", "figure", "paper", "slides", "document"}
RENDER_VERSION = "visual-qa-render-v1"
PROMPT_VERSION = "visual-qa-prompt-v1"
SCHEMA_VERSION = "visual-qa-schema-v1"


class VisualQAError(RuntimeError):
    """Expected, user-facing visual QA failure."""


def load_visual_env(env_file: Path | None = None) -> dict[str, str]:
    """Load visual/API settings from the repository .env with reference expansion.

    Process environment values override the file.  The function returns values
    without mutating ``os.environ`` so API secrets stay local to the caller.
    """
    values: dict[str, str] = {}
    source = env_file or (REPO / ".env")
    if source.is_file():
        for raw_line in source.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    known = {
        "LLM_API_BASE", "LLM_API_KEY",
        "VISUAL_QA_API_BASE", "VISUAL_QA_API_KEY",
        "VISUAL_QA_MODEL", "VISUAL_QA_FALLBACK_MODEL",
    }
    values.update({name: value for name, value in os.environ.items() if name in known})
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for _ in range(len(values) + 1):
        expanded = {
            name: pattern.sub(lambda match: values.get(match.group(1), match.group(0)), value)
            for name, value in values.items()
        }
        if expanded == values:
            break
        values = expanded
    return values


@dataclass(frozen=True)
class RemoteConfig:
    api_base: str
    api_key: str
    timeout: int = 90


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(payload)


def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _auto_profile(path: Path) -> str:
    if path.suffix.lower() in {".ppt", ".pptx"}:
        return "slides"
    if path.suffix.lower() == ".pdf":
        return "paper"
    return "figure"


def _sensitive_path(path: Path) -> bool:
    """Whether remote upload requires an explicit override.

    The check is lexical and intentionally conservative.  It covers raw facts,
    inbox/source-local material, and private stores without requiring the path
    to be inside this repository.
    """
    sensitive_names = {"raw", "inbox", "private", "sources", "source-local"}
    resolved = path.resolve()
    try:
        relative_parts = resolved.relative_to(REPO).parts
        return any(part.casefold() in sensitive_names for part in relative_parts)
    except ValueError:
        pass
    # On macOS, /tmp resolves beneath the system mount /private/var.  That
    # root-level implementation detail must not classify every temp fixture as
    # private user material; nested directories named private still do.
    parts = resolved.parts
    for index, part in enumerate(parts):
        lowered = part.casefold()
        if lowered == "private" and index == 1:
            continue
        if lowered in sensitive_names:
            return True
    return False


def _validate_receipt_root(root: Path) -> None:
    """Keep repository-local runtime writes inside the designated temp tree."""
    try:
        root.relative_to(REPO)
    except ValueError:
        return
    try:
        root.relative_to(REPO / "temp")
    except ValueError as exc:
        raise VisualQAError(
            "repository-local receipt_root must be under temp/; raw/wiki/private and source trees are read-only"
        ) from exc


def parse_page_selector(selector: str | None, total_pages: int) -> list[int]:
    """Parse a one-based selector (``all`` or ``1,3-5``)."""
    if total_pages < 1:
        raise VisualQAError("artifact contains no renderable pages")
    text = (selector or "all").strip().lower()
    if text in {"", "all", "*"}:
        return list(range(1, total_pages + 1))
    selected: set[int] = set()
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError as exc:
                raise VisualQAError(f"invalid page selector: {selector}") from exc
            if start > end:
                raise VisualQAError(f"invalid descending page range: {token}")
            selected.update(range(start, end + 1))
        else:
            try:
                selected.add(int(token))
            except ValueError as exc:
                raise VisualQAError(f"invalid page selector: {selector}") from exc
    if not selected or min(selected) < 1 or max(selected) > total_pages:
        raise VisualQAError(
            f"page selector {selector!r} is outside 1-{total_pages}"
        )
    return sorted(selected)


def _find_soffice(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("SOFFICE_BIN"):
        candidates.append(Path(os.environ["SOFFICE_BIN"]).expanduser())
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        candidates.append(Path(found))
    candidates.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    runtime_root = Path.home() / ".cache" / "codex-runtimes"
    if runtime_root.exists():
        candidates.extend(sorted(runtime_root.glob(
            "*/dependencies/bin/override/soffice"
        )))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise VisualQAError(
        "PPT/PPTX rendering requires LibreOffice/soffice; set SOFFICE_BIN"
    )


def _convert_slides_to_pdf(path: Path, target_pdf: Path,
                           soffice_bin: str | None = None) -> None:
    soffice = _find_soffice(soffice_bin)
    target_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-qa-slides-") as tmp_dir:
        out_dir = Path(tmp_dir)
        cmd = [
            str(soffice), "--headless", "--convert-to", "pdf",
            "--outdir", str(out_dir), str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        converted = out_dir / f"{path.stem}.pdf"
        if proc.returncode != 0 or not converted.is_file():
            detail = (proc.stderr or proc.stdout or "conversion produced no PDF").strip()
            raise VisualQAError(f"slide conversion failed: {detail[:500]}")
        tmp_target = target_pdf.with_name(f".{target_pdf.name}.{os.getpid()}.tmp")
        shutil.copyfile(converted, tmp_target)
        os.replace(tmp_target, target_pdf)


def _image_frame_count(path: Path) -> int:
    if Image is None:
        raise VisualQAError("Pillow is required for image QA")
    try:
        with Image.open(path) as image:
            return int(getattr(image, "n_frames", 1) or 1)
    except Exception as exc:
        raise VisualQAError(f"cannot open image: {exc}") from exc


def _pdf_page_count(path: Path) -> int:
    if fitz is None:
        raise VisualQAError("PyMuPDF is required for PDF QA")
    try:
        with fitz.open(path) as doc:
            return int(doc.page_count)
    except Exception as exc:
        raise VisualQAError(f"cannot open PDF: {exc}") from exc


def _prepare_source(path: Path, render_dir: Path,
                    soffice_bin: str | None = None) -> tuple[str, Path, int]:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_IMAGES:
        return "image", path, _image_frame_count(path)
    if suffix == ".pdf":
        return "pdf", path, _pdf_page_count(path)
    if suffix in {".ppt", ".pptx"}:
        converted_pdf = render_dir / "source-slides.pdf"
        if not converted_pdf.is_file():
            _convert_slides_to_pdf(path, converted_pdf, soffice_bin=soffice_bin)
        return "slides", converted_pdf, _pdf_page_count(converted_pdf)
    raise VisualQAError(
        f"unsupported extension {suffix or '<none>'}; supported: "
        + ", ".join(sorted(SUPPORTED_EXTENSIONS))
    )


def _save_pil_png(image: Any, target: Path) -> None:
    image = ImageOps.exif_transpose(image)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    image.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, target)


def render_page(source_kind: str, source_path: Path, page_number: int,
                target: Path, dpi: int = 170) -> None:
    """Render one one-based page/frame to a normalized PNG."""
    if target.is_file():
        return
    if source_kind == "image":
        try:
            with Image.open(source_path) as image:
                image.seek(page_number - 1)
                _save_pil_png(image.copy(), target)
        except Exception as exc:
            raise VisualQAError(f"cannot render image frame {page_number}: {exc}") from exc
        return
    if fitz is None:
        raise VisualQAError("PyMuPDF is required for page rendering")
    try:
        with fitz.open(source_path) as doc:
            page = doc.load_page(page_number - 1)
            scale = dpi / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            tmp.write_bytes(pix.tobytes("png"))
            os.replace(tmp, target)
    except Exception as exc:
        raise VisualQAError(f"cannot render page {page_number}: {exc}") from exc


def _issue(code: str, severity: str, region: str, evidence: str,
           suggested_fix: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "region": region,
        "evidence": evidence,
        "suggested_fix": suggested_fix,
    }


def deterministic_checks(image_path: Path, profile: str) -> dict[str, Any]:
    """Run cheap, reproducible technical checks on one rendered page."""
    if Image is None:
        raise VisualQAError("Pillow is required for deterministic visual QA")
    issues: list[dict[str, str]] = []
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            gray = ImageOps.grayscale(rgb)
            # Work on a bounded thumbnail for stable and cheap statistics.
            sample = gray.copy()
            sample.thumbnail((1024, 1024))
            stat = ImageStat.Stat(sample)
            mean = float(stat.mean[0])
            stddev = float(stat.stddev[0])
            extrema = sample.getextrema()
            histogram = sample.histogram()
            total = max(1, sum(histogram))
            near_white_ratio = sum(histogram[248:]) / total
            near_black_ratio = sum(histogram[:8]) / total

            min_expected = 700 if profile == "figure" else 900
            if min(width, height) < 256:
                issues.append(_issue(
                    "resolution_too_small", "fail", "whole page",
                    f"rendered size is {width}x{height}px",
                    "export at a larger pixel size or higher resolution",
                ))
            elif min(width, height) < min_expected:
                issues.append(_issue(
                    "resolution_low", "warn", "whole page",
                    f"rendered size is {width}x{height}px",
                    "verify labels remain legible at the intended publication size",
                ))

            aspect = max(width / max(height, 1), height / max(width, 1))
            if aspect > 10:
                issues.append(_issue(
                    "extreme_aspect_ratio", "warn", "whole page",
                    f"long-to-short dimension ratio is {aspect:.2f}",
                    "check that the export canvas and crop are intentional",
                ))

            if stddev < 1.2 and (mean > 248 or mean < 7):
                issues.append(_issue(
                    "blank_or_near_blank", "fail", "whole page",
                    f"grayscale mean={mean:.1f}, stddev={stddev:.2f}",
                    "re-export the artifact and confirm the page content is present",
                ))
            elif near_white_ratio > 0.997 or near_black_ratio > 0.997:
                issues.append(_issue(
                    "almost_empty", "warn", "whole page",
                    f"near-white={near_white_ratio:.3f}, near-black={near_black_ratio:.3f}",
                    "confirm that the page is intentionally sparse",
                ))

            # Border occupancy is only a warning heuristic.  It catches common
            # raster export clipping without claiming that edge contact is wrong.
            edge = max(1, min(width, height) // 200)
            border_samples = {
                "top": rgb.crop((0, 0, width, edge)),
                "bottom": rgb.crop((0, height - edge, width, height)),
                "left": rgb.crop((0, 0, edge, height)),
                "right": rgb.crop((width - edge, 0, width, height)),
            }
            crowded: list[str] = []
            for name, band in border_samples.items():
                band_gray = ImageOps.grayscale(band)
                band_hist = band_gray.histogram()
                band_total = max(1, sum(band_hist))
                non_white = sum(band_hist[:245]) / band_total
                if non_white > 0.45:
                    crowded.append(f"{name}={non_white:.2f}")
            if crowded:
                issues.append(_issue(
                    "possible_edge_clipping", "warn", "page border",
                    "non-background border occupancy: " + ", ".join(crowded),
                    "inspect the named borders for clipped text, markers, or panels",
                ))

            metrics = {
                "width_px": width,
                "height_px": height,
                "aspect_ratio": round(width / max(height, 1), 4),
                "grayscale_mean": round(mean, 3),
                "grayscale_stddev": round(stddev, 3),
                "grayscale_extrema": list(extrema),
                "near_white_ratio": round(near_white_ratio, 6),
                "near_black_ratio": round(near_black_ratio, 6),
            }
    except Exception as exc:
        raise VisualQAError(f"deterministic image check failed: {exc}") from exc

    verdict = "pass"
    if any(item["severity"] == "fail" for item in issues):
        verdict = "fail"
    elif issues:
        verdict = "warn"
    return {"verdict": verdict, "metrics": metrics, "issues": issues}


VISION_SCHEMA = {
    "verdict": "pass|warn|fail",
    "scores": {
        "legibility": "0-100",
        "layout": "0-100",
        "visual_hierarchy": "0-100",
        "consistency": "0-100",
        "accessibility": "0-100",
    },
    "issues": [{
        "code": "short_machine_code",
        "severity": "warn|fail",
        "region": "specific region",
        "evidence": "visible evidence only",
        "suggested_fix": "concrete visual correction",
    }],
    "needs_human_review": "boolean",
    "summary": "one concise sentence",
}


def _build_prompt(profile: str, artifact_name: str, page_number: int,
                  page_count: int, deterministic: dict[str, Any],
                  context: str) -> str:
    context_block = context.strip()[:16000] if context else "(none supplied)"
    return f"""You are performing visual QA on a static {profile} artifact.
Artifact: {artifact_name}
Page/frame: {page_number} of {page_count}

Inspect only what is visibly supported by this rendered page. Check for:
- clipped, overlapping, truncated, or missing elements;
- unreadable labels, legends, axes, captions, equations, and body text;
- weak contrast, problematic color reliance, and inconsistent visual encoding;
- layout imbalance, accidental whitespace, misalignment, and broken hierarchy;
- apparent duplicated panels, placeholder text, rendering corruption, or export defects;
- for plots, visibly missing units/legend mappings or ambiguous panel references.

Do not judge whether scientific claims or underlying data are true. Do not invent
hidden content. A visual model finding is advisory, not a factual source. If the
page cannot support a confident decision, set needs_human_review=true.

Deterministic diagnostics:
{json.dumps(deterministic, ensure_ascii=False, sort_keys=True)}

Optional author/context note:
{context_block}

Return one JSON object only, following this shape:
{json.dumps(VISION_SCHEMA, ensure_ascii=False, indent=2)}
"""


def _extract_json(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise VisualQAError("vision model returned no valid JSON object")


def _normalize_vision_result(obj: dict[str, Any]) -> dict[str, Any]:
    verdict = str(obj.get("verdict", "")).lower()
    if verdict not in {"pass", "warn", "fail"}:
        raise VisualQAError("vision result has invalid verdict")
    raw_scores = obj.get("scores", {})
    if not isinstance(raw_scores, dict):
        raise VisualQAError("vision result scores must be an object")
    scores: dict[str, float] = {}
    for name in ("legibility", "layout", "visual_hierarchy", "consistency", "accessibility"):
        value = raw_scores.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise VisualQAError(f"vision result score {name} is missing or non-numeric")
        scores[name] = round(max(0.0, min(100.0, float(value))), 2)
    raw_issues = obj.get("issues", [])
    if not isinstance(raw_issues, list):
        raise VisualQAError("vision result issues must be a list")
    issues = []
    for index, item in enumerate(raw_issues):
        if not isinstance(item, dict):
            raise VisualQAError(f"vision issue {index} must be an object")
        severity = str(item.get("severity", "warn")).lower()
        if severity not in {"warn", "fail"}:
            raise VisualQAError(f"vision issue {index} has invalid severity")
        issues.append({
            "code": str(item.get("code") or f"visual_issue_{index + 1}"),
            "severity": severity,
            "region": str(item.get("region") or "unspecified"),
            "evidence": str(item.get("evidence") or "")[:1000],
            "suggested_fix": str(item.get("suggested_fix") or "")[:1000],
        })
    if any(item["severity"] == "fail" for item in issues):
        verdict = "fail"
    elif issues and verdict == "pass":
        verdict = "warn"
    needs_review = obj.get("needs_human_review", False)
    if not isinstance(needs_review, bool):
        raise VisualQAError("vision result needs_human_review must be boolean")
    return {
        "verdict": verdict,
        "scores": scores,
        "issues": issues,
        "needs_human_review": needs_review,
        "summary": str(obj.get("summary") or "")[:1000],
    }


def _chat_completions_url(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _call_vision_api(model: str, image_path: Path, prompt: str,
                     config: RemoteConfig) -> dict[str, Any]:
    mime = "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{encoded}",
                }},
            ],
        }],
        "temperature": 0,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        _chat_completions_url(config.api_base),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise VisualQAError(f"vision API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VisualQAError(f"vision API request failed: {exc}") from exc
    try:
        envelope = json.loads(body)
        content = envelope["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise VisualQAError("vision API returned an invalid response envelope") from exc
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", "")) if isinstance(part, dict) else str(part)
            for part in content
        )
    return _normalize_vision_result(_extract_json(str(content)))


def _read_context(context_path: Path | None) -> tuple[str, str]:
    if context_path is None:
        return "", ""
    if not context_path.is_file():
        raise VisualQAError(f"context file not found: {context_path}")
    data = context_path.read_bytes()
    digest = _sha256_bytes(data)
    if len(data) > 256 * 1024:
        data = data[:256 * 1024]
    return data.decode("utf-8", errors="replace"), digest


def _overall_verdict(deterministic: dict[str, Any], vision: dict[str, Any] | None,
                     remote_status: str, deterministic_only: bool) -> str:
    if deterministic["verdict"] == "fail":
        return "fail"
    if vision and vision["verdict"] == "fail":
        return "fail"
    if deterministic["verdict"] == "warn":
        return "warn"
    if vision and vision["verdict"] == "warn":
        return "warn"
    if deterministic_only or remote_status == "checked":
        return "pass"
    return "not_checked"


def _summarize(page_receipts: list[dict[str, Any]], selected: list[int],
               receipt_dir: Path, artifact_sha: str, profile: str,
               total_pages: int) -> dict[str, Any]:
    counts = {"pass": 0, "warn": 0, "fail": 0, "not_checked": 0}
    issue_counts = {"warn": 0, "fail": 0}
    partial = False
    pages = []
    for receipt in sorted(page_receipts, key=lambda item: item["page"]):
        verdict = receipt.get("verdict", "not_checked")
        counts[verdict if verdict in counts else "not_checked"] += 1
        if receipt.get("state") != "complete":
            partial = True
        for issue in receipt.get("issues", []):
            severity = issue.get("severity")
            if severity in issue_counts:
                issue_counts[severity] += 1
        pages.append({
            "page": receipt["page"],
            "state": receipt.get("state"),
            "verdict": verdict,
            "remote_status": receipt.get("remote", {}).get("status"),
            "receipt": receipt.get("receipt_path"),
        })
    if counts["fail"]:
        verdict = "fail"
    elif counts["warn"]:
        verdict = "warn"
    elif counts["not_checked"] or partial:
        verdict = "not_checked"
    else:
        verdict = "pass"
    status = "partial" if partial or counts["not_checked"] else "completed"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "verdict": verdict,
        "artifact_sha256": artifact_sha,
        "profile": profile,
        "total_pages": total_pages,
        "selected_pages": selected,
        "checked_pages": len(page_receipts),
        "verdict_counts": counts,
        "issue_counts": issue_counts,
        "pages": pages,
        "receipt_dir": str(receipt_dir),
        "summary_path": str(receipt_dir / "summary.json"),
        "completed_at": _utc_now(),
    }


VisionCall = Callable[[str, Path, str, RemoteConfig], dict[str, Any]]


def run_visual_qa(
    path: str | Path,
    *,
    pages: str = "all",
    profile: str = "auto",
    context_path: str | Path | None = None,
    resume: bool = True,
    allow_remote: bool = False,
    deterministic_only: bool = False,
    receipt_root: str | Path | None = None,
    model: str | None = None,
    fallback_model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    timeout: int = 90,
    dpi: int = 170,
    soffice_bin: str | None = None,
    vision_call: VisionCall | None = None,
) -> dict[str, Any]:
    """Check an artifact and return a resumable summary dictionary."""
    artifact = Path(path).expanduser().resolve()
    if not artifact.is_file():
        raise VisualQAError(f"artifact not found: {artifact}")
    if artifact.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise VisualQAError(f"unsupported artifact type: {artifact.suffix or '<none>'}")
    if profile not in PROFILES:
        raise VisualQAError(f"invalid profile {profile!r}; choose {sorted(PROFILES)}")
    chosen_profile = _auto_profile(artifact) if profile == "auto" else profile
    if not 72 <= int(dpi) <= 400:
        raise VisualQAError("dpi must be between 72 and 400")

    artifact_sha = sha256_file(artifact)
    context = Path(context_path).expanduser().resolve() if context_path else None
    context_text, context_sha = _read_context(context)
    env = load_visual_env()
    chosen_model = model or env.get("VISUAL_QA_MODEL", DEFAULT_MODEL)
    chosen_fallback = fallback_model or env.get(
        "VISUAL_QA_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL
    )
    base = api_base if api_base is not None else env.get("VISUAL_QA_API_BASE", "")
    key = api_key if api_key is not None else env.get("VISUAL_QA_API_KEY", "")
    render_config = {"version": RENDER_VERSION, "dpi": int(dpi), "format": "png"}
    check_key_data = {
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "profile": chosen_profile,
        "model": chosen_model,
        "fallback_model": chosen_fallback,
        "render": render_config,
        "context_sha256": context_sha,
        "deterministic_only": bool(deterministic_only),
        "allow_remote": bool(allow_remote),
    }
    check_key = _stable_hash(check_key_data)[:20]
    root = Path(receipt_root).expanduser().resolve() if receipt_root else DEFAULT_RECEIPT_ROOT
    _validate_receipt_root(root)
    receipt_dir = root / artifact_sha / chosen_profile / check_key
    render_dir = receipt_dir / "renders"
    pages_dir = receipt_dir / "pages"
    render_dir.mkdir(parents=True, exist_ok=True)
    pages_dir.mkdir(parents=True, exist_ok=True)

    source_kind, render_source, total_pages = _prepare_source(
        artifact, render_dir, soffice_bin=soffice_bin
    )
    selected = parse_page_selector(pages, total_pages)
    sensitive = (
        _sensitive_path(artifact)
        or (context is not None and _sensitive_path(context))
        or (artifact.suffix.lower() == ".pdf" and chosen_profile == "paper")
    )
    remote_allowed = not deterministic_only and (not sensitive or allow_remote)
    remote_configured = bool(base and key)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now(),
        "artifact": str(artifact),
        "artifact_sha256": artifact_sha,
        "artifact_size": artifact.stat().st_size,
        "source_kind": source_kind,
        "profile": chosen_profile,
        "total_pages": total_pages,
        "check_key": check_key,
        "check_key_inputs": check_key_data,
        "sensitive_path": sensitive,
        "remote_allowed": remote_allowed,
        "remote_configured": remote_configured,
        "model_catalog": str(MODEL_CATALOG),
        "api_key_present": bool(key),
    }
    _write_json_atomic(receipt_dir / "manifest.json", manifest)

    call_vision = vision_call or _call_vision_api
    page_receipts: list[dict[str, Any]] = []
    for page_number in selected:
        receipt_path = pages_dir / f"page-{page_number:04d}.json"
        if resume and receipt_path.is_file():
            try:
                prior = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prior = None
            if (
                isinstance(prior, dict)
                and prior.get("state") == "complete"
                and prior.get("artifact_sha256") == artifact_sha
                and prior.get("check_key") == check_key
            ):
                prior["resumed"] = True
                prior["receipt_path"] = str(receipt_path)
                page_receipts.append(prior)
                continue

        render_path = render_dir / f"page-{page_number:04d}.png"
        render_page(source_kind, render_source, page_number, render_path, dpi=int(dpi))
        render_sha = sha256_file(render_path)
        deterministic = deterministic_checks(render_path, chosen_profile)
        prompt = _build_prompt(
            chosen_profile, artifact.name, page_number, total_pages,
            deterministic, context_text,
        )

        remote: dict[str, Any]
        vision: dict[str, Any] | None = None
        if deterministic_only:
            remote = {"status": "skipped", "reason": "deterministic_only"}
        elif sensitive and not allow_remote:
            remote = {
                "status": "blocked",
                "reason": "sensitive_path_requires_explicit_allow_remote",
            }
        elif not remote_configured:
            remote = {
                "status": "not_configured",
                "reason": "VISUAL_QA_API_BASE and VISUAL_QA_API_KEY are required",
            }
        else:
            config = RemoteConfig(api_base=base, api_key=key, timeout=int(timeout))
            attempts = []
            models = [chosen_model]
            if chosen_fallback and chosen_fallback != chosen_model:
                models.append(chosen_fallback)
            for candidate in models:
                started = time.monotonic()
                try:
                    raw_result = call_vision(candidate, render_path, prompt, config)
                    vision = _normalize_vision_result(raw_result)
                    attempts.append({
                        "model": candidate,
                        "status": "checked",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    })
                    remote = {
                        "status": "checked",
                        "model_used": candidate,
                        "attempts": attempts,
                    }
                    break
                except Exception as exc:
                    attempts.append({
                        "model": candidate,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    })
            else:
                remote = {
                    "status": "error",
                    "reason": "all vision model attempts failed",
                    "attempts": attempts,
                }

        verdict = _overall_verdict(
            deterministic, vision, remote["status"], deterministic_only
        )
        issues = list(deterministic["issues"])
        if vision:
            issues.extend(vision["issues"])
        state = "complete" if deterministic_only or remote["status"] == "checked" else "partial"
        page_receipt = {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "verdict": verdict,
            "artifact": str(artifact),
            "artifact_sha256": artifact_sha,
            "check_key": check_key,
            "page": page_number,
            "total_pages": total_pages,
            "profile": chosen_profile,
            "render_path": str(render_path),
            "render_sha256": render_sha,
            "deterministic": deterministic,
            "remote": remote,
            "vision": vision,
            "issues": issues,
            "needs_human_review": bool(
                state != "complete"
                or (vision and vision.get("needs_human_review"))
                or verdict == "fail"
            ),
            "completed_at": _utc_now(),
            "receipt_path": str(receipt_path),
            "resumed": False,
        }
        _write_json_atomic(receipt_path, page_receipt)
        page_receipts.append(page_receipt)

    summary = _summarize(
        page_receipts, selected, receipt_dir, artifact_sha,
        chosen_profile, total_pages,
    )
    summary.update({
        "artifact": str(artifact),
        "source_kind": source_kind,
        "check_key": check_key,
        "model": chosen_model,
        "fallback_model": chosen_fallback,
        "deterministic_only": bool(deterministic_only),
        "remote_allowed": remote_allowed,
        "sensitive_path": sensitive,
        "resumed_pages": sum(bool(item.get("resumed")) for item in page_receipts),
    })
    _write_json_atomic(receipt_dir / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resumable visual QA for images, PDF pages, and PPT/PPTX pages"
    )
    parser.add_argument("path", help="image, PDF, PPT, or PPTX path")
    parser.add_argument("--pages", default="all", help="all or one-based selector such as 1,3-5")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="auto")
    parser.add_argument("--context", dest="context_path", help="optional UTF-8 context/data note")
    parser.add_argument("--receipt-root", help="receipt/cache root (default: temp/visual-qa)")
    parser.add_argument("--no-resume", action="store_true", help="do not reuse completed page receipts")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow remote upload for protected paths and paper-profile PDFs")
    parser.add_argument("--deterministic-only", action="store_true",
                        help="run local checks only; a clean page may receive pass")
    parser.add_argument("--model", default=None)
    parser.add_argument("--fallback-model", default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--dpi", type=int, default=170)
    parser.add_argument("--soffice-bin", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_visual_qa(
            args.path,
            pages=args.pages,
            profile=args.profile,
            context_path=args.context_path,
            resume=not args.no_resume,
            allow_remote=args.allow_remote,
            deterministic_only=args.deterministic_only,
            receipt_root=args.receipt_root,
            model=args.model,
            fallback_model=args.fallback_model,
            api_base=args.api_base,
            api_key=args.api_key,
            timeout=args.timeout,
            dpi=args.dpi,
            soffice_bin=args.soffice_bin,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        error = {
            "status": "failed",
            "verdict": "not_checked",
            "error": f"{type(exc).__name__}: {str(exc)}",
        }
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

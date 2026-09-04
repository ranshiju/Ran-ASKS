#!/usr/bin/env python3
"""Generate project-scoped comic images through an explicit remote image API."""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml


REPO = Path(__file__).resolve().parent.parent
MODEL_CATALOG = REPO / "operations" / "config" / "llm-models.yaml"
DEFAULT_ENDPOINT = "/v1/images/generations"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ASSET_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
IMAGE_EXTENSIONS = ("png", "jpg", "webp", "gif")
RESERVED_PARAMETERS = {"model", "prompt", "n", "size", "response_format"}


class ComicGenerationError(RuntimeError):
    pass


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _expand_env(values: dict[str, str]) -> dict[str, str]:
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    expanded = dict(values)
    for _ in range(len(values) + 1):
        updated = {
            key: pattern.sub(lambda match: expanded.get(match.group(1), match.group(0)), value)
            for key, value in expanded.items()
        }
        if updated == expanded:
            return updated
        expanded = updated
    return expanded


def load_comic_env(env_file: Path | None = None) -> dict[str, str]:
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
        "LLM_API_BASE",
        "LLM_API_KEY",
        "COMIC_IMAGE_API_BASE",
        "COMIC_IMAGE_API_KEY",
        "COMIC_IMAGE_MODEL",
        "COMIC_IMAGE_ENDPOINT",
        "COMIC_IMAGE_RESPONSE_FORMAT",
    }
    values.update({name: value for name, value in os.environ.items() if name in known})
    return _expand_env(values)


def load_catalog(path: Path = MODEL_CATALOG) -> dict[str, Any]:
    if not path.is_file():
        raise ComicGenerationError(f"model catalog not found: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ComicGenerationError("model catalog must be a YAML mapping")
    return value


def image_candidates(path: Path = MODEL_CATALOG) -> list[dict[str, Any]]:
    section = load_catalog(path).get("image_generation") or {}
    candidates = section.get("candidates") or []
    if not isinstance(candidates, list):
        raise ComicGenerationError("image_generation.candidates must be a list")
    normalized: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict) or not str(item.get("model") or "").strip():
            raise ComicGenerationError("each image-generation candidate needs a model")
        normalized.append(dict(item))
    return normalized


def require_candidate(model: str, catalog_path: Path = MODEL_CATALOG) -> dict[str, Any]:
    for candidate in image_candidates(catalog_path):
        if candidate["model"] == model:
            return candidate
    raise ComicGenerationError(f"model is not registered for image generation: {model}")


def _safe_segment(value: str, field: str, *, ascii_only: bool = False) -> str:
    if not value or value != value.strip() or value in {".", ".."} or len(value) > 100:
        raise ComicGenerationError(f"invalid {field}: {value!r}")
    if Path(value).name != value or "/" in value or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise ComicGenerationError(f"invalid {field}: {value!r}")
    if ascii_only and not ASSET_ID_RE.fullmatch(value):
        raise ComicGenerationError(f"invalid {field}: {value!r}")
    return value


def project_root(project: str, repo: Path = REPO) -> Path:
    _safe_segment(project, "project")
    projects_root = (repo / "projects").resolve()
    result = (projects_root / project).resolve()
    if not result.is_relative_to(projects_root) or not result.is_dir():
        raise ComicGenerationError(f"research project not found: {project}")
    if not (result / "schema.yaml").is_file() or not (result / "outputs").is_dir():
        raise ComicGenerationError(f"not a validated research project: {project}")
    return result


def allowed_output_root(output_root: Path, repo: Path = REPO) -> Path:
    root = repo.resolve()
    candidate = output_root.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    result = candidate.resolve()
    if not result.is_relative_to(root) or result.name != "outputs" or not result.is_dir():
        raise ComicGenerationError("output root must be an existing repository outputs directory")
    relative_parts = set(result.relative_to(root).parts)
    if relative_parts.intersection({"raw", "wiki", "private", "inbox", ".git", ".research-memory"}):
        raise ComicGenerationError("output root is inside a forbidden repository area")
    return result


def output_directory(
    project: str | None,
    article_id: str,
    repo: Path = REPO,
    explicit_output_root: Path | None = None,
) -> Path:
    if bool(project) == bool(explicit_output_root):
        raise ComicGenerationError("choose exactly one of project or output_root")
    if project:
        outputs = allowed_output_root(project_root(project, repo) / "outputs", repo)
    else:
        outputs = allowed_output_root(Path(explicit_output_root), repo)
    _safe_segment(article_id, "article_id")
    result = (outputs / article_id / "images").resolve()
    if not result.is_relative_to(outputs):
        raise ComicGenerationError("output path escapes the project outputs directory")
    return result


def resolve_storyboard(project: str | None, storyboard: Path, repo: Path = REPO) -> tuple[Path, Path]:
    root = project_root(project, repo) if project else repo.resolve()
    resolved = storyboard.expanduser().resolve()
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise ComicGenerationError("storyboard must be an existing file inside the project")
    relative = resolved.relative_to(root)
    if set(relative.parts).intersection({"outputs", ".research-memory", "raw", "wiki", "private", "inbox"}):
        raise ComicGenerationError("storyboard must be source material, not generated output or memory")
    if resolved.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ComicGenerationError("storyboard must be YAML or JSON")
    return root, resolved


def load_storyboard(project: str | None, path: Path, repo: Path = REPO) -> dict[str, Any]:
    _, resolved = resolve_storyboard(project, path, repo)
    if resolved.suffix.lower() == ".json":
        value = json.loads(resolved.read_text(encoding="utf-8"))
    else:
        value = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ComicGenerationError("storyboard must be a mapping")
    _safe_segment(str(value.get("article_id") or ""), "article_id")
    assets = value.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ComicGenerationError("storyboard assets must be a non-empty list")
    for asset in assets:
        if not isinstance(asset, dict):
            raise ComicGenerationError("each storyboard asset must be a mapping")
        _safe_segment(str(asset.get("id") or ""), "asset_id", ascii_only=True)
        prompt = asset.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ComicGenerationError(f"asset {asset.get('id')!r} needs a prompt")
        _validate_parameters(asset.get("parameters") or {})
    return value


def endpoint_url(base: str, endpoint: str = DEFAULT_ENDPOINT) -> str:
    if endpoint.startswith(("http://", "https://")):
        result = endpoint
    else:
        base = base.rstrip("/")
        endpoint = "/" + endpoint.lstrip("/")
        if base.endswith("/v1") and endpoint.startswith("/v1/"):
            endpoint = endpoint[3:]
        result = base + endpoint
    parsed = urllib.parse.urlparse(result)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ComicGenerationError("comic image API endpoint must be an HTTPS URL without credentials")
    return result


def _validate_parameters(parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        raise ComicGenerationError("parameters must be a mapping")
    forbidden = RESERVED_PARAMETERS.intersection(parameters)
    if forbidden:
        raise ComicGenerationError(f"parameters may not override: {', '.join(sorted(forbidden))}")
    try:
        json.dumps(parameters, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ComicGenerationError(f"parameters must be JSON-compatible: {exc}") from exc
    return dict(parameters)


def _read_limited(response: Any, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise ComicGenerationError("API response exceeds the size limit")
    return data


def call_image_api(url: str, key: str, body: dict[str, Any], timeout: float) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = _read_limited(response)
            request_id = str(response.headers.get("x-request-id") or response.headers.get("request-id") or "")
    except urllib.error.HTTPError as exc:
        raise ComicGenerationError(f"image API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ComicGenerationError(f"image API transport failed: {exc.reason}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComicGenerationError("image API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ComicGenerationError("image API response must be a JSON object")
    return payload, request_id


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_image(url: str, timeout: float) -> bytes:
    _validate_download_url(url)
    request = urllib.request.Request(url, headers={"Accept": "image/*"}, method="GET")
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return _read_limited(response)
    except urllib.error.HTTPError as exc:
        raise ComicGenerationError(f"generated image download returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ComicGenerationError(f"generated image download failed: {exc.reason}") from exc


def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ComicGenerationError("generated image URL must be HTTP(S) without credentials")
    hostname = parsed.hostname or ""
    if hostname.lower() == "localhost" or hostname.lower().endswith(".local"):
        raise ComicGenerationError("generated image URL may not target localhost or local domains")
    try:
        default_port = 443 if parsed.scheme == "https" else 80
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or default_port)}
    except socket.gaierror as exc:
        raise ComicGenerationError("generated image URL hostname cannot be resolved") from exc
    for address in addresses:
        value = ipaddress.ip_address(address)
        if not value.is_global:
            raise ComicGenerationError("generated image URL may not target private or non-global addresses")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _decode_image_value(value: str) -> bytes:
    encoded = value
    if value.startswith("data:"):
        header, separator, encoded = value.partition(",")
        if not separator or ";base64" not in header:
            raise ComicGenerationError("unsupported generated image data URL")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ComicGenerationError("generated image contains invalid base64") from exc


def extract_image(payload: dict[str, Any], timeout: float) -> tuple[bytes, str]:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        items = payload.get("images")
    if not isinstance(items, list) or not items:
        raise ComicGenerationError("image API response contains no images")
    item = items[0]
    if isinstance(item, str):
        if item.startswith(("http://", "https://")):
            return _download_image(item, timeout), "url"
        return _decode_image_value(item), "base64"
    if not isinstance(item, dict):
        raise ComicGenerationError("unsupported generated image item")
    for key in ("b64_json", "base64", "image"):
        if isinstance(item.get(key), str) and item[key]:
            return _decode_image_value(item[key]), "base64"
    if isinstance(item.get("url"), str) and item["url"]:
        return _download_image(item["url"], timeout), "url"
    raise ComicGenerationError("generated image item has no supported payload")


def detect_image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise ComicGenerationError("API payload is not a supported PNG, JPEG, WebP, or GIF image")


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def _existing_asset(directory: Path, asset_id: str) -> Path | None:
    for extension in IMAGE_EXTENSIONS:
        candidate = directory / f"{asset_id}.{extension}"
        if candidate.exists():
            return candidate
    return None


def _record_result(directory: Path, receipt: dict[str, Any], prompt: str) -> None:
    run_id = receipt["run_id"]
    _write_json_atomic(directory / "receipts" / f"{run_id}.json", receipt)
    if receipt["status"] != "complete":
        return
    _append_jsonl(directory / "prompts.jsonl", {
        "run_id": run_id,
        "asset_id": receipt["asset_id"],
        "model": receipt["model"],
        "prompt": prompt,
        "prompt_sha256": receipt["prompt_sha256"],
        "created_at": receipt["created_at"],
    })
    manifest_path = directory / "manifest.json"
    manifest = {"version": 1, "runs": []}
    if manifest_path.is_file():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("runs"), list):
            manifest = loaded
    manifest["runs"].append(receipt)
    _write_json_atomic(manifest_path, manifest)


def generate_asset(
    *,
    project: str | None,
    explicit_output_root: Path | None,
    article_id: str,
    asset_id: str,
    prompt: str,
    model: str,
    size: str | None,
    parameters: dict[str, Any] | None,
    catalog_path: Path = MODEL_CATALOG,
    repo: Path = REPO,
    env_file: Path | None = None,
    endpoint: str | None = None,
    allow_remote: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
    timeout: float = 180.0,
) -> dict[str, Any]:
    require_candidate(model, catalog_path)
    _safe_segment(asset_id, "asset_id", ascii_only=True)
    prompt = prompt.strip()
    if not prompt or len(prompt) > 20000:
        raise ComicGenerationError("prompt must contain 1-20000 characters")
    directory = output_directory(project, article_id, repo, explicit_output_root)
    existing = _existing_asset(directory, asset_id)
    if existing and not overwrite:
        raise ComicGenerationError(f"asset already exists: {existing}")
    options = _validate_parameters(parameters or {})
    config = load_comic_env(env_file)
    api_base = config.get("COMIC_IMAGE_API_BASE") or config.get("LLM_API_BASE") or ""
    api_key = config.get("COMIC_IMAGE_API_KEY") or config.get("LLM_API_KEY") or ""
    api_endpoint = endpoint or config.get("COMIC_IMAGE_ENDPOINT") or DEFAULT_ENDPOINT
    response_format = config.get("COMIC_IMAGE_RESPONSE_FORMAT") or ""
    plan = {
        "status": "dry_run" if dry_run else "planned",
        "project": project,
        "output_root": str(directory.parent.parent.relative_to(repo.resolve())),
        "article_id": article_id,
        "asset_id": asset_id,
        "model": model,
        "size": size or "provider-default",
        "output_directory": str(directory),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    if dry_run:
        return plan
    if not allow_remote:
        raise ComicGenerationError("remote image generation requires --allow-remote")
    if not api_base or not api_key:
        raise ComicGenerationError("COMIC_IMAGE_API_BASE/KEY or LLM_API_BASE/KEY is not configured")
    url = endpoint_url(api_base, api_endpoint)
    body: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
    if size:
        body["size"] = size
    if response_format:
        body["response_format"] = response_format
    body.update(options)
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    started = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        payload, provider_request_id = call_image_api(url, api_key, body, timeout)
        image_data, delivery = extract_image(payload, timeout)
        extension = detect_image_extension(image_data)
        output = directory / f"{asset_id}.{extension}"
        if output.exists() and not overwrite:
            raise ComicGenerationError(f"asset already exists: {output}")
        _write_bytes_atomic(output, image_data)
        if overwrite and existing and existing != output:
            os.remove(existing)
        receipt = {
            "version": 1,
            "run_id": run_id,
            "status": "complete",
            "created_at": _utc_now(),
            "project": project,
            "output_root": plan["output_root"],
            "article_id": article_id,
            "asset_id": asset_id,
            "model": model,
            "size": size or "provider-default",
            "prompt_sha256": plan["prompt_sha256"],
            "file": output.name,
            "sha256": hashlib.sha256(image_data).hexdigest(),
            "bytes": len(image_data),
            "delivery": delivery,
            "provider_request_id": provider_request_id,
            "latency_sec": round(time.monotonic() - started, 3),
            "review_status": "pending",
        }
        _record_result(directory, receipt, prompt)
        return {**receipt, "output_path": str(output)}
    except Exception as exc:
        receipt = {
            "version": 1,
            "run_id": run_id,
            "status": "failed",
            "created_at": _utc_now(),
            "project": project,
            "output_root": plan["output_root"],
            "article_id": article_id,
            "asset_id": asset_id,
            "model": model,
            "size": size or "provider-default",
            "prompt_sha256": plan["prompt_sha256"],
            "error_class": type(exc).__name__,
            "latency_sec": round(time.monotonic() - started, 3),
        }
        _record_result(directory, receipt, prompt)
        if isinstance(exc, ComicGenerationError):
            raise
        raise ComicGenerationError(f"image generation failed: {type(exc).__name__}") from exc


def generate_batch(
    *,
    project: str | None,
    explicit_output_root: Path | None,
    storyboard_path: Path,
    model: str | None,
    size: str | None,
    catalog_path: Path = MODEL_CATALOG,
    repo: Path = REPO,
    env_file: Path | None = None,
    endpoint: str | None = None,
    allow_remote: bool = False,
    overwrite: bool = False,
    dry_run: bool = False,
    timeout: float = 180.0,
) -> dict[str, Any]:
    storyboard = load_storyboard(project, storyboard_path, repo)
    article_id = str(storyboard["article_id"])
    style = str(storyboard.get("style") or "").strip()
    results = []
    for asset in storyboard["assets"]:
        prompt = str(asset["prompt"]).strip()
        if style:
            prompt = style + "\n\n" + prompt
        asset_model = str(asset.get("model") or model or "").strip()
        if not asset_model:
            raise ComicGenerationError("batch requires --model or an asset-level model")
        results.append(generate_asset(
            project=project,
            explicit_output_root=explicit_output_root,
            article_id=article_id,
            asset_id=str(asset["id"]),
            prompt=prompt,
            model=asset_model,
            size=str(asset.get("size") or size or "") or None,
            parameters=asset.get("parameters") or {},
            catalog_path=catalog_path,
            repo=repo,
            env_file=env_file,
            endpoint=endpoint,
            allow_remote=allow_remote,
            overwrite=overwrite,
            dry_run=dry_run,
            timeout=timeout,
        ))
    return {
        "status": "dry_run" if dry_run else "complete",
        "project": project,
        "output_root": str(explicit_output_root) if explicit_output_root else None,
        "article_id": article_id,
        "assets": results,
    }


def run_batch(args: argparse.Namespace) -> dict[str, Any]:
    return generate_batch(
        project=args.project,
        explicit_output_root=args.output_root,
        storyboard_path=Path(args.storyboard),
        model=args.model,
        size=args.size,
        catalog_path=args.catalog,
        repo=args.repo,
        env_file=args.env_file,
        endpoint=args.endpoint,
        allow_remote=args.allow_remote,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
        timeout=args.timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(catalog=MODEL_CATALOG, repo=REPO, env_file=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("models", help="List registered image-generation candidates")

    def add_generation_options(command: argparse.ArgumentParser) -> None:
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--project")
        scope.add_argument("--output-root", type=Path)
        command.add_argument("--model")
        command.add_argument("--size")
        command.set_defaults(endpoint=None)
        command.add_argument("--timeout", type=float, default=180.0)
        command.add_argument("--allow-remote", action="store_true")
        command.add_argument("--overwrite", action="store_true")
        command.add_argument("--dry-run", action="store_true")

    generate = subparsers.add_parser("generate", help="Generate one comic image")
    add_generation_options(generate)
    generate.add_argument("--article-id", required=True)
    generate.add_argument("--asset-id", required=True)
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--parameters", default="{}", help="Extra JSON request parameters")

    batch = subparsers.add_parser("batch", help="Generate all assets in a project storyboard")
    add_generation_options(batch)
    batch.add_argument("--storyboard", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "models":
            _json_print({"status": "ok", "candidates": image_candidates(args.catalog)})
            return 0
        if args.command == "generate":
            if not args.model:
                config = load_comic_env(args.env_file)
                args.model = config.get("COMIC_IMAGE_MODEL") or ""
            if not args.model:
                raise ComicGenerationError("generate requires --model or COMIC_IMAGE_MODEL")
            try:
                parameters = json.loads(args.parameters)
            except json.JSONDecodeError as exc:
                raise ComicGenerationError(f"--parameters is not valid JSON: {exc}") from exc
            result = generate_asset(
                project=args.project,
                explicit_output_root=args.output_root,
                article_id=args.article_id,
                asset_id=args.asset_id,
                prompt=args.prompt,
                model=args.model,
                size=args.size,
                parameters=parameters,
                catalog_path=args.catalog,
                repo=args.repo,
                env_file=args.env_file,
                endpoint=args.endpoint,
                allow_remote=args.allow_remote,
                overwrite=args.overwrite,
                dry_run=args.dry_run,
                timeout=args.timeout,
            )
        else:
            if not args.model:
                config = load_comic_env(args.env_file)
                args.model = config.get("COMIC_IMAGE_MODEL") or None
            result = run_batch(args)
        _json_print(result)
        return 0
    except (ComicGenerationError, json.JSONDecodeError, yaml.YAMLError) as exc:
        _json_print({"status": "error", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

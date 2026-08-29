#!/usr/bin/env python3
"""Finalize one manifest-declared inbox extraction into raw and wiki destinations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_within(path: Path, parent: Path, description: str, allow_parent: bool = False) -> None:
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{description} must be inside {parent}: {path}") from exc
    if not allow_parent and relative == Path("."):
        raise ValueError(f"{description} must not be {parent} itself")


def load_manifest(path: Path) -> tuple[list[str], str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid manifest: {path} ({exc})") from exc
    raw_files = data.get("raw_files")
    wiki_file = data.get("wiki_file")
    if not isinstance(raw_files, list) or not raw_files or not all(isinstance(item, str) for item in raw_files):
        raise ValueError("manifest.raw_files must be a non-empty list of file paths")
    if not isinstance(wiki_file, str):
        raise ValueError("manifest.wiki_file must be a file path")
    if len(set(raw_files)) != len(raw_files):
        raise ValueError("manifest.raw_files must not contain duplicates")
    return raw_files, wiki_file


def manifest_file(extract_dir: Path, name: str, label: str) -> Path:
    if Path(name).name != name:
        raise ValueError(f"manifest {label} must name a top-level file: {name}")
    candidate = extract_dir / name
    require_within(candidate.resolve(strict=False), extract_dir, f"manifest {label}")
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"manifest {label} must name an existing non-symlink file: {name}")
    return candidate


def verify_real_file(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"not an entity file: {path}")


def staged_copy(source: Path, destination: Path) -> dict[str, str | int]:
    shutil.copy2(source, destination)
    verify_real_file(destination)
    return {"path": destination.name, "bytes": destination.stat().st_size, "sha256": sha256_file(destination)}


def run_ingest_check(project_root: Path, wiki_path: Path) -> None:
    command = [sys.executable, str(project_root / ".scripts" / "ingest_check.py"), str(wiki_path)]
    result = subprocess.run(command, cwd=project_root, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise ValueError(f"ingest_check failed; temporary extraction is retained: {detail}")


def finalize(project_root: Path, paper_id: str, raw_dir: Path, wiki_path: Path,
             extract_dir: Path, manifest_path: Path, cleanup: bool,
             allow_existing_raw_dir: bool = False) -> Path:
    project_root = project_root.resolve()
    extract_dir = extract_dir.resolve()
    raw_dir = raw_dir.resolve(strict=False)
    wiki_path = wiki_path.resolve(strict=False)
    manifest_path = manifest_path.resolve()
    temp_root = project_root / "temp" / "inbox-extract"

    require_within(extract_dir, temp_root, "extract directory")
    require_within(manifest_path, extract_dir, "manifest")
    require_within(raw_dir, project_root, "raw directory")
    require_within(wiki_path, project_root, "wiki path")
    if "raw" not in raw_dir.relative_to(project_root).parts:
        raise ValueError(f"raw directory must be below a raw/ directory: {raw_dir}")
    if "wiki" not in wiki_path.relative_to(project_root).parts:
        raise ValueError(f"wiki path must be below a wiki/ directory: {wiki_path}")
    if raw_dir.is_symlink():
        raise ValueError(f"destination raw directory must not be a symlink: {raw_dir}")
    if raw_dir.exists() and not raw_dir.is_dir():
        raise ValueError(f"destination raw path must be a directory: {raw_dir}")
    if raw_dir.exists() and not allow_existing_raw_dir:
        raise ValueError(f"destination raw directory already exists: {raw_dir}")
    if wiki_path.exists() or wiki_path.is_symlink():
        raise ValueError(f"destination wiki file already exists: {wiki_path}")

    raw_names, wiki_name = load_manifest(manifest_path)
    raw_sources = [manifest_file(extract_dir, name, "raw_files entry") for name in raw_names]
    wiki_source = manifest_file(extract_dir, wiki_name, "wiki_file")
    if wiki_source in raw_sources:
        raise ValueError("manifest wiki_file must not also be a raw_files entry")
    if len({source.name for source in raw_sources}) != len(raw_sources):
        raise ValueError("manifest.raw_files must not resolve to duplicate destination names")
    if raw_dir.exists():
        collisions = [source.name for source in raw_sources if (raw_dir / source.name).exists()]
        if collisions:
            raise ValueError(f"destination raw files already exist: {', '.join(collisions)}")

    raw_dir.parent.mkdir(parents=True, exist_ok=True)
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staged_raw = raw_dir.parent / f".inbox-finalize-{raw_dir.name}-{token}"
    staged_wiki = wiki_path.parent / f".{wiki_path.name}.inbox-finalize-{token}"
    committed_raw = False
    committed_wiki = False
    try:
        staged_raw.mkdir()
        raw_receipt = [staged_copy(source, staged_raw / source.name) for source in raw_sources]
        wiki_receipt = staged_copy(wiki_source, staged_wiki)
        if raw_dir.exists():
            for source in raw_sources:
                os.replace(staged_raw / source.name, raw_dir / source.name)
            staged_raw.rmdir()
        else:
            os.replace(staged_raw, raw_dir)
        committed_raw = True
        os.replace(staged_wiki, wiki_path)
        committed_wiki = True
    except Exception:
        if committed_wiki and wiki_path.exists():
            wiki_path.unlink()
        if committed_raw and raw_dir.exists():
            if allow_existing_raw_dir:
                for source in raw_sources:
                    landed = raw_dir / source.name
                    if landed.exists():
                        landed.unlink()
            else:
                shutil.rmtree(raw_dir)
        raise
    finally:
        if staged_raw.exists():
            shutil.rmtree(staged_raw)
        if staged_wiki.exists():
            staged_wiki.unlink()

    receipt_dir = project_root / "temp" / "inbox-receipts"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{paper_id}-{token}.json"
    receipt = {
        "status": "committed",
        "paper_id": paper_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest_path.relative_to(project_root)),
        "raw_dir": str(raw_dir.relative_to(project_root)),
        "wiki_path": str(wiki_path.relative_to(project_root)),
        "raw_files": raw_receipt,
        "wiki_file": wiki_receipt,
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if cleanup:
        run_ingest_check(project_root, wiki_path)
        shutil.rmtree(extract_dir)
        receipt["cleanup"] = "completed"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--wiki-path", required=True)
    parser.add_argument("--extract-dir", required=True)
    parser.add_argument("--manifest", help="defaults to <extract-dir>/manifest.json")
    parser.add_argument("--cleanup", action="store_true", help="remove this item’s extract directory after commit")
    parser.add_argument("--allow-existing-raw-dir", action="store_true",
                        help="append manifest files to an existing raw container after collision checks")
    parser.add_argument("--project-root", help=argparse.SUPPRESS)
    args = parser.parse_args()
    project_root = Path(args.project_root).resolve() if args.project_root else Path(__file__).resolve().parent.parent
    extract_dir = Path(args.extract_dir)
    manifest = Path(args.manifest) if args.manifest else extract_dir / "manifest.json"
    try:
        receipt = finalize(project_root, args.paper_id, Path(args.raw_dir), Path(args.wiki_path),
                           extract_dir, manifest, args.cleanup, args.allow_existing_raw_dir)
    except (OSError, ValueError) as exc:
        print(f"❌ inbox finalize failed: {exc}", file=sys.stderr)
        return 1
    print(f"✅ inbox finalize committed; receipt: {receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

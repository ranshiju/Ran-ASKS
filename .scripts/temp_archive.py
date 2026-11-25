#!/usr/bin/env python3
"""Archive explicitly closed temp experiments, preserving evidence and recovery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from urllib.parse import unquote
import zipfile

import workspace_checkpoint as cp
from platform_compat import safe_open

SCHEMA = "wikigraph-temp-archive-v1"
META = "__archive_manifest__.json"


def paths(root, name):
    root = cp.reject_link_components(Path(root).absolute())
    if not re.fullmatch(r"[\w][\w.-]{0,159}", name):
        raise ValueError("Choose one top-level temp directory name")
    source = cp.reject_link_components(root / "temp" / name)
    return root, source, root / "archives/temporary" / (name + ".zip")


def archived_script(root, file, cache):
    """Only byte-identical retained scripts in verified prior archives are historical."""
    rel = file.relative_to(root)
    if len(rel.parts) < 3 or rel.parts[0] != "temp":
        return False
    name = rel.parts[1]
    _, source, packed = paths(root, name)
    marker = source / "ARCHIVED.json"
    if not marker.is_file():
        return False
    if name not in cache:
        cp.reject_link_components(marker)
        cp.reject_link_components(packed)
        receipt_path = cp.reject_link_components(packed.with_suffix(".json"))
        receipt = json.loads(marker.read_text())
        if (receipt != json.loads(receipt_path.read_text()) or receipt.get("schema") != SCHEMA
                or receipt.get("source") != "temp/" + name
                or receipt.get("archive") != packed.relative_to(root).as_posix()
                or cp.digest(packed) != receipt.get("archive_sha256")):
            raise ValueError("Historical script archive receipt/hash mismatch: " + name)
        with zipfile.ZipFile(packed) as z:
            manifest = json.loads(z.read(META))
        if manifest.get("schema") != SCHEMA or manifest.get("source") != receipt["source"]:
            raise ValueError("Historical script archive identity mismatch: " + name)
        cache[name] = manifest
    entry = cache[name]["entries"].get(file.relative_to(source).as_posix(), {})
    cp.reject_link_components(file)
    if entry.get("kind") != "file" or cp.digest(file) != entry.get("sha256"):
        raise ValueError("Historical script changed since archive: " + rel.as_posix())
    return True


def references(root, name):
    prefix = "temp/" + name
    cmd = ["rg", "-l", "--null", "--hidden", "--no-ignore", "--fixed-strings", prefix]
    for ext in ("md", "json", "jsonl", "yaml", "yml", "py", "js", "ts", "sh", "txt"):
        cmd += ["--glob", "*." + ext]
    # Later globs take precedence: exclusions must follow extension inclusions.
    cmd += ["--glob", "!**/.git/**", "--glob", "!**/.venv*/**", "--glob", "!**/node_modules/**",
            "--glob", "!archives/**", "--glob", "!" + prefix + "/**"]
    result = subprocess.run([*cmd, "."], cwd=root, capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError("Reference scan failed: " + result.stderr.decode(errors="replace")[:500])
    files = [root / p for p in result.stdout.decode().split("\0") if p]
    keep, found, historical = set(), [], {}
    pattern = re.compile(re.escape(prefix) + r"(?![\w.-])(?:/([^\s`'\"<>\]\[(){}，。；：]*))?")
    for file in files:
        if file.is_symlink():
            raise ValueError("Reference document is a symlink; inspect it explicitly first")
        text = file.read_text(encoding="utf-8", errors="replace")
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        if file.suffix in {".py", ".js", ".ts", ".sh"} and not archived_script(root, file, historical):
            raise ValueError(f"Code still references this experiment: {file.relative_to(root)}")
        found.append(file.relative_to(root).as_posix())
        for match in matches:
            tail = unquote((match.group(1) or "").split("#", 1)[0]).rstrip(".,;:/")
            if tail:
                rel = cp.safe_relative(tail)
                p = root / prefix / rel
                if p.is_file() and not p.is_symlink():
                    cp.reject_link_components(p)
                    keep.add(rel.as_posix())
    return sorted(keep), sorted(found)


def check_archive(path, expected_sha=None):
    if expected_sha and cp.digest(path) != expected_sha:
        raise ValueError("Archive SHA-256 mismatch")
    with zipfile.ZipFile(path) as z:
        doc = json.loads(z.read(META))
        if doc.get("schema") != SCHEMA:
            raise ValueError("Unsupported archive schema")
        entries = doc["entries"]
        for name, entry in entries.items():
            cp.safe_relative(name)
            if name in {".", META} or entry["kind"] not in {"file", "dir", "link"}:
                raise ValueError("Invalid archive entry")
            for parent in Path(name).parents:
                if str(parent) != "." and entries.get(parent.as_posix(), {}).get("kind") != "dir":
                    raise ValueError("Archive entry has a missing or non-directory parent")
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != {META} | {n for n, e in entries.items() if e["kind"] == "file"}:
            raise ValueError("Archive inventory mismatch")
        for name, e in entries.items():
            if e["kind"] != "file":
                continue
            h, size = hashlib.sha256(), 0
            with z.open(name) as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk); size += len(chunk)
            if size != e["size"] or h.hexdigest() != e["sha256"]:
                raise ValueError("Archive payload mismatch: " + name)
    return doc


def restore(archive, target, expected_sha=None):
    archive = cp.reject_link_components(Path(archive).absolute())
    target = cp.reject_link_components(Path(target).absolute())
    if target.exists():
        raise FileExistsError("Restore target must not exist")
    doc = check_archive(archive, expected_sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-archive-", dir=target.parent) as tmp:
        work = Path(tmp) / "contents"
        work.mkdir(mode=0o700)
        with zipfile.ZipFile(archive) as z:
            for name, e in sorted(doc["entries"].items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
                p = work / name
                if e["kind"] == "dir":
                    p.mkdir()
                elif e["kind"] == "file":
                    with z.open(name) as f, p.open("xb") as out:
                        shutil.copyfileobj(f, out, 1024 * 1024)
                    if cp.digest(p) != e["sha256"]:
                        raise ValueError("Restored payload mismatch")
                    os.chmod(p, e["mode"])
            # Links are materialized last; extraction never traverses a link.
            for name, e in doc["entries"].items():
                if e["kind"] == "link":
                    os.symlink(e["target"], work / name)
            for name, e in sorted(doc["entries"].items(), reverse=True):
                if e["kind"] == "dir":
                    os.chmod(work / name, e["mode"])
        target.mkdir(exist_ok=False)
        os.replace(work, target)
    return {"target": str(target), "entries": len(doc["entries"]), "verified": True}


def archive(root, name, evidence, reason, quarantine):
    root, source, destination = paths(root, name)
    cp.reject_link_components(destination.parent)
    evidence_rel = cp.safe_relative(evidence)
    if evidence_rel.parts[0] not in {"projects", "operations"}:
        raise ValueError("Supply a retained project/operations completion record")
    evidence_path = cp.reject_link_components(root / evidence_rel)
    if not evidence_path.is_file() or not reason.strip():
        raise ValueError("Completion evidence and a reason are required")
    if not source.is_dir() or (source / "ARCHIVED.json").exists():
        raise ValueError("Source is missing or already archived")
    if destination.exists() or destination.with_suffix(".json").exists():
        raise FileExistsError("Archive destination already exists")
    quarantine = cp.outside(quarantine, root)
    if quarantine.exists():
        raise FileExistsError("Choose an unused quarantine path")
    if (root / ".git").exists():
        tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--", "temp/" + name])
        if tracked:
            raise ValueError("Tracked experiment files need a separate Git migration plan")
    keep, refs = references(root, name)
    before, _ = cp.inventory(source, filter_runtime=False)
    if META in before or "ARCHIVED.md" in before:
        raise ValueError("Source uses reserved archive metadata names")
    keep = sorted(set(keep) | {n for n, e in before.items() if e[0] == "file" and "/" not in n})
    entries = {}
    with tempfile.TemporaryDirectory(prefix="wg-temp-archive-") as tmp:
        staging = Path(tmp).resolve()
        packed = staging / "bundle.zip"
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
            for rel, info in before.items():
                kind, mode = info[:2]
                e = {"kind": kind, "mode": mode}
                if kind == "file":
                    cp.reject_link_components((source / rel).parent)
                    h, size = hashlib.sha256(), 0
                    with os.fdopen(safe_open(source / rel), "rb") as f, z.open(rel, "w", force_zip64=True) as out:
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            out.write(chunk); h.update(chunk); size += len(chunk)
                    e.update(size=size, sha256=h.hexdigest())
                elif kind == "link":
                    e["target"] = info[5]
                entries[rel] = e
            doc = {"schema": SCHEMA, "source": "temp/" + name, "reason": reason,
                   "evidence": evidence, "evidence_sha256": cp.digest(evidence_path),
                   "created_at": datetime.now(timezone.utc).isoformat(), "entries": entries,
                   "kept_files": keep, "reference_documents": refs}
            z.writestr(META, json.dumps(doc, ensure_ascii=False))
        sha = cp.digest(packed)
        # Actual independent extraction is required before replacing the source.
        restore(packed, staging / "roundtrip", sha)
        stub = staging / "stub"
        stub.mkdir()
        for rel in keep:
            p = stub / rel; p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / "roundtrip" / rel, p)
        archive_rel = destination.relative_to(root).as_posix()
        receipt = {k: v for k, v in doc.items() if k != "entries"}
        receipt.update(archive=archive_rel, archive_sha256=sha,
                       original_files=sum(e["kind"] == "file" for e in entries.values()),
                       original_bytes=sum(e.get("size", 0) for e in entries.values()),
                       archive_bytes=packed.stat().st_size)
        historical_refs = [r for r in refs if Path(r).suffix in {".py", ".js", ".ts", ".sh"}]
        cp.write_json(stub / "ARCHIVED.json", receipt)
        (stub / "ARCHIVED.md").write_text(
            f"# 已归档的历史实验\n\n{reason}\n\n完成记录：`{evidence}`。顶层结果与报告直接引用的文件仍在原位置。\n\n"
            f"完整原件：[压缩包](../../{archive_rel})，SHA-256：`{sha}`。\n\n"
            f"恢复到新的本机目录：\n\n```bash\npython3 .scripts/wg.py checkpoint restore-temp {name} --target ~/WikiGraphData/restored/{name}\n```\n\n"
            "本目录保存历史工程证据，不是可续跑的生产摄入事务。运行旧实验前先恢复完整目录及所依赖的实验目录；保留脚本仅供查证。\n"
            + ("\n已归档的上游引用脚本（已核验归档及脚本哈希）：\n\n" + "\n".join("- `" + r + "`" for r in historical_refs) + "\n" if historical_refs else ""), encoding="utf-8")
        after, _ = cp.inventory(source, filter_runtime=False)
        if before != after:
            raise ValueError("Source changed during archive; original retained")
        latest_keep, latest_refs = references(root, name)
        if not set(latest_keep).issubset(keep) or latest_refs != refs or cp.digest(evidence_path) != doc["evidence_sha256"]:
            raise ValueError("References/evidence changed during archive; original retained")
        destination.parent.mkdir(parents=True, exist_ok=True)
        quarantine.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # All moves are on the host filesystem. Fail without deleting originals if
        # cross-device rename is unavailable; there is no copy-and-delete fallback.
        os.link(packed, destination)
        cp.write_json(destination.with_suffix(".json"), receipt)
        source.rename(quarantine)
        try:
            os.rename(stub, source)
        except Exception:
            quarantine.rename(source)
            raise
        cp.write_json(quarantine.parent / (quarantine.name + ".quarantine.json"),
                      {**receipt, "original_path": str(source), "quarantine": str(quarantine), "automatic_delete": False})
    return {**receipt, "quarantine": str(quarantine), "remaining_files": len(keep) + 2}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=str(cp.ROOT))
    sub = p.add_subparsers(dest="action", required=True)
    a = sub.add_parser("archive")
    a.add_argument("name"); a.add_argument("--evidence", required=True)
    a.add_argument("--reason", required=True); a.add_argument("--quarantine", required=True)
    a = sub.add_parser("restore")
    a.add_argument("name"); a.add_argument("--target", required=True)
    args = p.parse_args()
    if args.action == "archive":
        result = archive(Path(args.root), args.name, args.evidence, args.reason, Path(args.quarantine).expanduser())
    else:
        root, source, packed = paths(Path(args.root), args.name)
        receipt = json.loads(packed.with_suffix(".json").read_text())
        result = restore(packed, Path(args.target).expanduser(), receipt["archive_sha256"])
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

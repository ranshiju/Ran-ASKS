#!/usr/bin/env python3
"""Build and verify the manifest-governed public WikiGraph release."""
from __future__ import annotations

import argparse
import copy
import fnmatch
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "operations/engineering/open-source-manifest.yaml"
VERSION_PATH = REPO / "VERSION"
MARKER = ".wikigraph-public-release"
PUBLIC_GRAPH_PATH = "operations/engineering/graph.yaml"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
PRIVATE_PREFIXES = (
    "academic/raw/", "academic/wiki/", "academic/outputs/",
    "admin/raw/", "admin/wiki/", "admin/outputs/",
    "teaching/raw/", "teaching/wiki/", "teaching/outputs/",
    "business/raw/", "business/wiki/", "business/outputs/",
    "cross-domain/raw/", "cross-domain/topics/", "cross-domain/outputs/",
    "inbox/", "memory/", "temp/", "slide-library/inbox/",
)
SENSITIVE_PATTERNS = (
    re.compile("s" "jran", re.IGNORECASE),
    re.compile(r"/(?:U" "sers)/[^/\\\\]+/", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\\\(?:U" "sers)\\\\[^\\\\]+\\\\", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def load_manifest() -> dict:
    with MANIFEST_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def read_version() -> str | None:
    """读取发布版本号；缺失或非 semver 返回 None。"""
    try:
        text = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if VERSION_PATTERN.fullmatch(text) else None


def release_badge(version: str) -> str:
    return f"> Current release: v{version}"


def matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or Path(path).match(pattern)


def selected_files(manifest: dict) -> set[str]:
    files: set[str] = set()
    excludes = manifest.get("exclude", [])
    for pattern in manifest["include"]:
        prefix = pattern.split("**", 1)[0].rstrip("/")
        root = REPO / prefix
        candidates = root.rglob("*") if "**" in pattern else (root,)
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            relative = candidate.relative_to(REPO).as_posix()
            if matches(relative, pattern) and not any(matches(relative, excluded) for excluded in excludes):
                files.add(relative)
    return files


def expected_files(manifest: dict) -> set[str]:
    files = selected_files(manifest)
    files.update(manifest.get("public_assets", {}).keys())
    files.update(f"{directory}/.gitkeep" for directory in manifest["template_dirs"])
    files.add(MARKER)
    return files


def projected_engineering_graph(destination: Path) -> dict:
    source = yaml.safe_load((REPO / PUBLIC_GRAPH_PATH).read_text(encoding="utf-8"))
    projected = copy.deepcopy(source)
    nodes = projected.get("nodes", {})

    def available(node: dict) -> bool:
        path = str(node.get("path", ""))
        if "<" in path:
            return True
        if path.startswith("projects/") or path.startswith(".project/"):
            return False
        return bool(node.get("optional", False)) or (destination / path).exists()

    kept = {node_id for node_id, node in nodes.items() if available(node)}
    projected["nodes"] = {node_id: node for node_id, node in nodes.items() if node_id in kept}
    projected["edges"] = [edge for edge in projected.get("edges", [])
                          if edge[0] in kept and edge[2] in kept]

    for name, capability in projected.get("capabilities", {}).items():
        missing_required = [node_id for node_id in capability.get("required", []) if node_id not in kept]
        if missing_required:
            raise ValueError(f"public capability depends on private nodes: {name}/{missing_required}")
        for field in ("required", "optional", "forbidden"):
            capability[field] = [node_id for node_id in capability.get(field, []) if node_id in kept]

    for contract in projected.get("contracts", []):
        if "nodes" in contract:
            contract["nodes"] = [node_id for node_id in contract["nodes"] if node_id in kept]
    projected["verification"] = {
        node_id: commands for node_id, commands in projected.get("verification", {}).items()
        if node_id in kept
    }
    projected["script_contracts"] = {
        node_id: contract for node_id, contract in projected.get("script_contracts", {}).items()
        if node_id in kept
    }
    return projected


def write_projected_engineering_graph(destination: Path) -> None:
    target = destination / PUBLIC_GRAPH_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(projected_engineering_graph(destination), allow_unicode=True,
                       width=100000, sort_keys=False),
        encoding="utf-8",
    )


def clear_destination(destination: Path) -> None:
    for child in destination.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def build(destination: Path, clean: bool, force: bool) -> None:
    manifest = load_manifest()
    destination = destination.resolve()
    if destination == REPO:
        raise ValueError("destination must not be the source repository")
    if destination.exists() and any(destination.iterdir()):
        if not clean:
            raise ValueError("destination is non-empty; use --clean --force")
        if not force:
            raise ValueError("--clean requires --force")
        clear_destination(destination)
    destination.mkdir(parents=True, exist_ok=True)
    version = read_version()
    if version is None:
        raise ValueError(f"invalid or missing release version: {VERSION_PATH}")
    for relative in sorted(selected_files(manifest)):
        if relative == PUBLIC_GRAPH_PATH:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    for relative, asset in manifest.get("public_assets", {}).items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / asset, target)
    for directory in manifest["template_dirs"]:
        target = destination / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / ".gitkeep").touch()
    write_projected_engineering_graph(destination)
    stamp_readme(destination, version)
    (destination / MARKER).write_text("Generated by .scripts/open_source_release.py\n", encoding="utf-8")
    print(f"Built {len(expected_files(manifest))} public files in {destination}")


def stamp_readme(destination: Path, version: str) -> None:
    """把发布版本号写入公开 README 的固定位置。"""
    readme = destination / "README.md"
    if not readme.is_file():
        raise ValueError("destination README.md missing during version stamping")
    text = readme.read_text(encoding="utf-8")
    badge = release_badge(version)
    marker = re.compile(r"^> Current release: v[^\n]*$", re.MULTILINE)
    if marker.search(text):
        text = marker.sub(lambda _match: badge, text, count=1)
    else:
        heading = re.compile(r"^# .+?\n", re.MULTILINE)
        match = heading.search(text)
        if not match:
            raise ValueError("destination README.md has no first heading for version stamping")
        insert_at = match.end()
        text = text[:insert_at] + badge + "\n" + text[insert_at:]
    readme.write_text(text, encoding="utf-8")


def actual_files(destination: Path) -> set[str]:
    return {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(destination).parts
    }


def verify(destination: Path) -> int:
    manifest = load_manifest()
    destination = destination.resolve()
    failures: list[str] = []
    if not (destination / MARKER).is_file():
        failures.append(f"missing release marker: {MARKER}")
    expected = expected_files(manifest)
    actual = actual_files(destination)
    version = read_version()
    if version is None:
        failures.append(f"invalid or missing source release version: {VERSION_PATH.name}")
    else:
        version_path = destination / "VERSION"
        if "VERSION" not in actual:
            failures.append("missing expected file: VERSION")
        elif version_path.read_text(encoding="utf-8", errors="ignore").strip() != version:
            failures.append(f"VERSION mismatch: destination != {version}")
        readme_path = destination / "README.md"
        if readme_path.is_file():
            readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
            if release_badge(version).lower() not in readme_text.lower():
                failures.append(f"README missing release badge: {release_badge(version)}")
    for path in sorted(expected - actual):
        failures.append(f"missing expected file: {path}")
    for path in sorted(actual - expected):
        failures.append(f"unexpected file: {path}")
    public_graph_path = destination / PUBLIC_GRAPH_PATH
    if public_graph_path.is_file():
        try:
            actual_graph = yaml.safe_load(public_graph_path.read_text(encoding="utf-8"))
            if actual_graph != projected_engineering_graph(destination):
                failures.append(f"public engineering graph projection mismatch: {PUBLIC_GRAPH_PATH}")
        except (OSError, ValueError, yaml.YAMLError) as error:
            failures.append(f"invalid public engineering graph: {error}")
    for path in sorted(actual):
        if path.endswith(".db"):
            failures.append(f"database file is private: {path}")
        if path.startswith(PRIVATE_PREFIXES) and not path.endswith("/.gitkeep"):
            failures.append(f"private content path: {path}")
        content_path = destination / path
        if content_path.stat().st_size > 10_000_000:
            failures.append(f"oversized release file: {path}")
            continue
        if content_path.suffix.lower() in {".md", ".py", ".yaml", ".yml", ".txt", ".json", ".sh", ".example"}:
            text = content_path.read_text(encoding="utf-8", errors="ignore").lower()
            for pattern in SENSITIVE_PATTERNS:
                if pattern.search(text):
                    failures.append(f"sensitive marker '{pattern.pattern}' in {path}")
    if failures:
        print("\n".join(f"ERROR: {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(f"Public release verified: {len(actual)} files, no unexpected content")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("destination", type=Path)
    build_parser.add_argument("--clean", action="store_true")
    build_parser.add_argument("--force", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            build(args.destination, args.clean, args.force)
        else:
            sys.exit(verify(args.destination))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

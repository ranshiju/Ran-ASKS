#!/usr/bin/env python3
"""Build and verify the manifest-governed public WikiGraph release."""
from __future__ import annotations

import argparse
import copy
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "operations/engineering/open-source-manifest.yaml"
VERSION_PATH = REPO / "VERSION"
MARKER = ".wikigraph-public-release"
PUBLIC_GRAPH_PATH = "operations/engineering/graph.yaml"
PUBLIC_CONTACT = "sjran@cnu.edu.cn"
RELEASE_DOCUMENTATION_PATHS = {
    "README.md",
    "README.zh-CN.md",
    "docs/introduction/README.md",
}
INTRODUCTION_PREFIX = "docs/introduction/ASKS-Chinese-Introduction-"
NORMALIZED_PDF_PRODUCER = b"GPL Ghostscript"
VERSION_PATTERN = re.compile(r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$")
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


def next_version(current: str, level: str) -> str:
    if not VERSION_PATTERN.fullmatch(current) or level not in {"patch", "minor", "major"}:
        raise ValueError("valid MAJOR.MINOR.PATCH and patch/minor/major level required")
    major, minor, patch = (int(part) for part in current.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def prepare_version(level: str, reason: str, from_version: str, *, apply: bool = False,
                    release_date: str | None = None) -> dict:
    current = read_version()
    if current != from_version:
        raise ValueError(f"VERSION changed: expected {from_version}, found {current}; do not bump twice on retry")
    if not reason.strip() or "\n" in reason or "\r" in reason:
        raise ValueError("a non-empty single-line semantic change/compatibility rationale is required")
    new_version = next_version(current, level)
    dated = date.fromisoformat(release_date).isoformat() if release_date else date.today().isoformat()
    manifest = load_manifest()
    changelog = REPO / manifest["public_assets"]["CHANGELOG.md"]
    text = changelog.read_text(encoding="utf-8")
    match = re.search(r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    if not match or not re.search(r"^- \S", match[1], re.M):
        raise ValueError("public CHANGELOG Unreleased must contain reviewed user-facing changes")
    if re.search(rf"^## \[{re.escape(new_version)}\]", text, re.M):
        raise ValueError(f"CHANGELOG already contains {new_version}")
    section = (f"## [Unreleased]\n\n## [{new_version}] - {dated}\n\n"
               f"### Version decision\n\n- {level.upper()}: {reason.strip()}\n\n"
               + match[1].strip() + "\n\n")
    updated = text[:match.start()] + section + text[match.end():]
    writes = {VERSION_PATH: new_version + "\n", changelog: updated, REPO / "CHANGELOG.md": updated}
    plan = {"status": "applied" if apply else "planned", "from": current, "to": new_version,
            "level": level, "reason": reason.strip(), "date": dated,
            "writes": [str(path.relative_to(REPO)) for path in writes]}
    if apply:
        originals = {path: path.read_bytes() if path.exists() else None for path in writes}
        try:
            for path, content in writes.items():
                path.write_text(content, encoding="utf-8")
        except OSError:
            for path, content in originals.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
    return plan


def localized_release_badge(version: str) -> str:
    return f"> 当前发布版本: v{version}"


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


def dated_introduction_paths(manifest: dict, suffix: str) -> set[str]:
    return {
        path
        for path in manifest.get("public_assets", {})
        if path.startswith(INTRODUCTION_PREFIX) and path.endswith(suffix)
    }


def documentation_sync_paths(manifest: dict) -> set[str]:
    """Return the reader-facing Markdown documents required in every update."""
    introduction_markdown = dated_introduction_paths(manifest, ".md")
    introduction_pdfs = dated_introduction_paths(manifest, ".pdf")
    if len(introduction_markdown) != 1:
        raise ValueError(
            "public_assets must declare exactly one dated ASKS Chinese introduction Markdown"
        )
    if len(introduction_pdfs) != 1:
        raise ValueError(
            "public_assets must declare exactly one dated ASKS Chinese introduction PDF"
        )
    return RELEASE_DOCUMENTATION_PATHS | introduction_markdown


def git_publication_diff(destination: Path) -> tuple[set[str], str | None]:
    """Return the pending release diff, or the latest committed diff when clean."""
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=destination,
        text=True,
        capture_output=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return set(), None
    has_head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=destination,
        text=True,
        capture_output=True,
    )
    if has_head.returncode != 0:
        return set(), None
    tracked = subprocess.run(
        ["git", "diff", "HEAD", "--name-only", "--"],
        cwd=destination,
        text=True,
        capture_output=True,
        check=True,
    )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=destination,
        text=True,
        capture_output=True,
        check=True,
    )
    pending = {
        path
        for path in (*tracked.stdout.splitlines(), *untracked.stdout.splitlines())
        if path and path != MARKER
    }
    if pending:
        return pending, "HEAD"
    has_parent = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^"],
        cwd=destination,
        text=True,
        capture_output=True,
    )
    if has_parent.returncode != 0:
        return set(), None
    latest = subprocess.run(
        ["git", "diff", "HEAD^", "HEAD", "--name-only", "--"],
        cwd=destination,
        text=True,
        capture_output=True,
        check=True,
    )
    return {path for path in latest.stdout.splitlines() if path and path != MARKER}, "HEAD^"


def git_publication_changes(destination: Path) -> set[str]:
    return git_publication_diff(destination)[0]


def version_progression_errors(destination: Path, version: str, changes: set[str], base: str | None) -> list[str]:
    if not changes or base is None:
        return []
    previous = subprocess.run(["git", "show", f"{base}:VERSION"], cwd=destination,
                              text=True, capture_output=True)
    if previous.returncode != 0:
        return ["public update baseline has no VERSION; review the baseline before publishing"]
    old_version = previous.stdout.strip()
    if not VERSION_PATTERN.fullmatch(old_version):
        return ["public update baseline has an invalid VERSION"]
    if tuple(map(int, version.split("."))) <= tuple(map(int, old_version.split("."))):
        return [f"public update must advance VERSION beyond {old_version}; classify changes and run prepare-version"]
    return []


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
    stamp_readmes(destination, version)
    (destination / MARKER).write_text("Generated by .scripts/open_source_release.py\n", encoding="utf-8")
    print(f"Built {len(expected_files(manifest))} public files in {destination}")


def stamp_readmes(destination: Path, version: str) -> None:
    """把发布版本号写入中英文公开 README 的固定位置。"""
    badges = {
        "README.md": release_badge(version),
        "README.zh-CN.md": localized_release_badge(version),
    }
    for name, badge in badges.items():
        readme = destination / name
        if not readme.is_file():
            raise ValueError(f"destination {name} missing during version stamping")
        text = readme.read_text(encoding="utf-8")
        marker = re.compile(
            r"^> (?:Current release|当前发布版本): v[^\n]*$", re.MULTILINE
        )
        if marker.search(text):
            text = marker.sub(lambda _match: badge, text, count=1)
        else:
            heading = re.compile(r"^# .+?\n", re.MULTILINE)
            match = heading.search(text)
            if not match:
                raise ValueError(f"destination {name} has no first heading for version stamping")
            insert_at = match.end()
            text = text[:insert_at] + badge + "\n" + text[insert_at:]
        readme.write_text(text, encoding="utf-8")


def actual_files(destination: Path) -> set[str]:
    return {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(destination).parts
    }


def git_ignored_files(destination: Path, paths: set[str]) -> list[str]:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=destination,
        text=True,
        capture_output=True,
    )
    if probe.returncode != 0:
        return []
    checked = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=destination,
        input="\n".join(sorted(paths)) + "\n",
        text=True,
        capture_output=True,
    )
    if checked.returncode not in (0, 1):
        raise ValueError(f"git ignore audit failed: {checked.stderr.strip()}")
    return [line for line in checked.stdout.splitlines() if line]


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
        readme_badges = {
            "README.md": release_badge(version),
            "README.zh-CN.md": localized_release_badge(version),
        }
        for name, badge in readme_badges.items():
            readme_path = destination / name
            if readme_path.is_file():
                readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
                if badge.lower() not in readme_text.lower():
                    failures.append(f"{name} missing release badge: {badge}")
        changelog_path = destination / "CHANGELOG.md"
        if changelog_path.is_file():
            changelog_text = changelog_path.read_text(encoding="utf-8", errors="ignore")
            if not re.search(rf"^## \[{re.escape(version)}\](?:\s|$)", changelog_text, re.M):
                failures.append(f"CHANGELOG.md missing current release heading: [{version}]")
            latest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog_text, re.M)
            if not latest or latest[1] != version:
                failures.append("CHANGELOG.md newest release heading must match VERSION")
    for path in sorted(expected - actual):
        failures.append(f"missing expected file: {path}")
    for path in sorted(actual - expected):
        failures.append(f"unexpected file: {path}")
    try:
        publication_changes, base = git_publication_diff(destination)
        if version:
            failures.extend(version_progression_errors(destination, version, publication_changes, base))
        required_documentation = documentation_sync_paths(manifest)
        missing_documentation = required_documentation - publication_changes
        if publication_changes and missing_documentation:
            failures.append(
                "public update must synchronize README.md, README.zh-CN.md, the Chinese "
                "introduction Markdown, and its scope note; missing from this update: "
                + ", ".join(sorted(missing_documentation))
            )
        for path in sorted(dated_introduction_paths(manifest, ".pdf")):
            if path not in actual:
                continue
            content = (destination / path).read_bytes()
            if not content.startswith(b"%PDF-") or NORMALIZED_PDF_PRODUCER not in content:
                failures.append(
                    f"public introduction PDF is not normalized: {path}"
                )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        failures.append(f"unable to verify release documentation synchronization: {error}")
    try:
        for path in git_ignored_files(destination, actual):
            failures.append(f"release file ignored by destination .gitignore: {path}")
    except ValueError as error:
        failures.append(str(error))
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
        if content_path.suffix.lower() in {
            ".csv", ".example", ".json", ".jsonl", ".md", ".py", ".sh", ".txt", ".yaml", ".yml"
        }:
            text = content_path.read_text(encoding="utf-8", errors="ignore").lower()
            text = text.replace(PUBLIC_CONTACT, "")
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
    prepare_parser = subparsers.add_parser("prepare-version")
    prepare_parser.add_argument("--level", choices=["patch", "minor", "major"], required=True)
    prepare_parser.add_argument("--reason", required=True)
    prepare_parser.add_argument("--from-version", required=True)
    prepare_parser.add_argument("--date")
    prepare_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare-version":
            print(json.dumps(prepare_version(args.level, args.reason, args.from_version,
                                            apply=args.apply, release_date=args.date), ensure_ascii=False, indent=2))
            return
        if args.command == "build":
            build(args.destination, args.clean, args.force)
        else:
            sys.exit(verify(args.destination))
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

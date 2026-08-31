#!/usr/bin/env python3
"""Verify integrity and the publication boundary of audit artifact 1.1.0."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CHECKSUMS = ROOT / "CHECKSUMS.sha256"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_values(item)


def main() -> None:
    errors: list[str] = []
    required = {
        "README.md",
        "metadata.json",
        "LICENSE-DATA.md",
        "LICENSE-PHYSH.md",
        "THIRD_PARTY_NOTICES.md",
        "physh/metrics/permutation_values.csv.gz",
        "model-judge/outputs/judge-outputs.jsonl",
        "model-judge/outputs/trial-key.csv",
        "figures/figure5-external-audit-evidence.pdf",
    }
    for relative in sorted(required):
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")

    records: dict[str, str] = {}
    for line in CHECKSUMS.read_text(encoding="utf-8").splitlines():
        expected, relative = line.split("  ", 1)
        records[relative] = expected
    actual_files = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() and path != CHECKSUMS
    }
    if actual_files != set(records):
        for relative in sorted(actual_files - set(records)):
            errors.append(f"unrecorded file: {relative}")
        for relative in sorted(set(records) - actual_files):
            errors.append(f"checksum path missing: {relative}")
    for relative, expected in sorted(records.items()):
        path = ROOT / relative
        if path.is_file() and sha256(path) != expected:
            errors.append(f"checksum mismatch: {relative}")

    metadata = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("artifact_version") != "1.1.0":
        errors.append("metadata artifact_version is not 1.1.0")
    boundary = metadata.get("release_boundary", {})
    if any(boundary.get(key) is not False for key in (
        "complete_abstracts_distributed",
        "source_pdfs_distributed",
        "credentials_distributed",
        "production_graph_distributed",
    )):
        errors.append("metadata release boundary is incomplete")

    output_path = ROOT / "model-judge" / "outputs" / "judge-outputs.jsonl"
    if output_path.is_file():
        for line_number, line in enumerate(output_path.read_text(encoding="utf-8").splitlines(), start=1):
            record = json.loads(line)
            for key, _ in walk_values(record):
                if key.lower() in {"abstract", "source_text", "api_key"}:
                    errors.append(f"private field {key!r} in judge output line {line_number}")

    forbidden_names = {"private_inputs", "raw", ".env", "paper.pdf"}
    for path in ROOT.rglob("*"):
        if any(part.lower() in forbidden_names for part in path.relative_to(ROOT).parts):
            errors.append(f"forbidden release path: {path.relative_to(ROOT)}")

    if errors:
        raise SystemExit("Audit artifact verification failed:\n- " + "\n- ".join(errors))
    print(f"Audit artifact 1.1.0 verified: {len(records)} files, release boundary passed")


if __name__ == "__main__":
    main()

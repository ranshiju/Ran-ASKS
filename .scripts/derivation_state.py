#!/usr/bin/env python3
"""Deterministic fingerprints and replay metadata for derived ingest outputs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def provenance(path: Path, *, schema_version: str, rule_version: str,
               prompt_version: str = "", model: str = "") -> dict[str, Any]:
    return {
        "source_fingerprint": sha256_file(path),
        "schema_version": schema_version,
        "rule_version": rule_version,
        "prompt_version": prompt_version,
        "model": model,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def is_stale(record: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = ("source_fingerprint", "schema_version", "rule_version", "prompt_version", "model")
    return any(record.get(key, "") != current.get(key, "") for key in keys)

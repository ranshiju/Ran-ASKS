#!/usr/bin/env python3
"""自动治理论文摄入中产生的候选谓词。"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUEUE = REPO / "cross-domain" / "predicate-candidates.jsonl"
STATE = REPO / "cross-domain" / "predicate-governance.json"
REGISTRY = REPO / ".scripts" / "predicate-registry.json"
CONFIG = REPO / ".scripts" / "predicate-governance.yaml"

DEFAULT_CONFIG = {
    "aliases": {"应用到": "应用于", "用于": "应用于", "建立于": "基于"},
    "observation_min_pages": 3,
    "observation_min_sources": 2,
    "formal_min_pages": 10,
    "consistent_subject_ratio": 0.8,
}


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(path: Path = CONFIG) -> dict:
    """读取可调阈值；配置缺失时保持安全默认值。"""
    if not path.exists():
        return dict(DEFAULT_CONFIG)
    import yaml
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {**DEFAULT_CONFIG, **loaded}


def load_candidates(queue: Path) -> list[dict]:
    if not queue.exists():
        return []
    rows = []
    for line in queue.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def normalize_predicate(predicate: str, config: dict | None = None) -> str:
    """将低风险同义谓词归一为既有规范谓词。"""
    config = config or DEFAULT_CONFIG
    return config.get("aliases", {}).get(predicate, predicate)


def classify(records: list[dict], config: dict) -> dict:
    aliases = config["aliases"]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        predicate = normalize_predicate(record["predicate"], config)
        grouped[predicate].append(record)
    entries = {}
    for predicate, group in grouped.items():
        pages = {item.get("wiki_path", "") for item in group if item.get("wiki_path")}
        sources = {item.get("source", item.get("paper_id", "")) for item in group if item.get("source", item.get("paper_id", ""))}
        subjects = [item.get("subject", "") for item in group]
        subject_ratio = max((subjects.count(subject) for subject in set(subjects)), default=0) / len(subjects)
        status = "candidate"
        if len(pages) >= config["observation_min_pages"] and len(sources) >= config["observation_min_sources"] and subject_ratio >= config["consistent_subject_ratio"]:
            status = "observation"
        if len(pages) >= config["formal_min_pages"] and subject_ratio >= config["consistent_subject_ratio"]:
            status = "formal"
        entries[predicate] = {
            "status": status,
            "uses": len(group),
            "pages": sorted(pages),
            "sources": sorted(sources),
            "subject_consistency": round(subject_ratio, 3),
            "aliases": sorted({item["predicate"] for item in group if item["predicate"] != predicate}),
            "examples": [{key: item.get(key, "") for key in ("subject", "object", "wiki_path")} for item in group[:3]],
        }
    return entries


def govern(queue: Path = QUEUE, state_path: Path = STATE, registry_path: Path = REGISTRY,
           config_path: Path = CONFIG) -> dict:
    config = load_config(config_path)
    entries = classify(load_candidates(queue), config)
    state = {"updated_at": datetime.now(timezone.utc).isoformat(), "config": config, "predicates": entries}
    registry = {"formal": sorted(predicate for predicate, entry in entries.items() if entry["status"] == "formal"),
                "observation": sorted(predicate for predicate, entry in entries.items() if entry["status"] == "observation")}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"candidates": len(entries), "observation": len(registry["observation"]), "formal": len(registry["formal"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=QUEUE)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args()
    print(json.dumps(govern(args.queue, args.state, args.registry, args.config), ensure_ascii=False))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Recall bounded, generic experience patterns for capability workflows."""

import argparse
import json
import re
from pathlib import Path

import yaml


EXPERIENCE_DIR = Path(__file__).resolve().parent.parent / "memory" / "experiences"
CAPABILITIES = ("query", "ingest", "write", "build")
MAX_LIMIT = 3
MAX_FILE_BYTES = 16_384


def _load_patterns(capability: str, experience_dir: Path = EXPERIENCE_DIR) -> list[dict]:
    if capability not in CAPABILITIES:
        return []
    path = experience_dir / f"{capability}.md"
    if not path.exists() or path.stat().st_size > MAX_FILE_BYTES:
        return []
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(.*?)\n```", text, re.S)
    if not match:
        return []
    data = yaml.safe_load(match.group(1)) or {}
    patterns = data.get("patterns", [])
    return [pattern for pattern in patterns if isinstance(pattern, dict)] if isinstance(patterns, list) else []


def _normalized_terms(value) -> list[str]:
    if isinstance(value, list):
        terms = [str(item).strip().casefold() for item in value if str(item).strip()]
    elif isinstance(value, str) and value.strip():
        terms = [value.strip().casefold()]
    else:
        terms = []
    return terms


def recall(
    capability: str,
    context: str,
    event: str = "",
    playbook_hit: bool = False,
    playbook_allows_experience: bool = False,
    limit: int = MAX_LIMIT,
    experience_dir: Path = EXPERIENCE_DIR,
) -> dict:
    if playbook_hit and not playbook_allows_experience:
        return {
            "capability": capability,
            "patterns": [],
            "reason": "skipped",
            "skipped_reason": "playbook_priority",
            "recall_cost": {"llm_calls": 0, "raw_scans": 0, "files_read": 0},
        }

    haystack_context = context.casefold()
    haystack_event = event.casefold()
    scored = []
    for pattern in _load_patterns(capability, experience_dir):
        if pattern.get("status") == "deprecated":
            continue
        allowed_events = _normalized_terms(pattern.get("events"))
        if haystack_event and allowed_events and haystack_event not in allowed_events:
            continue
        score = 0
        matched_triggers = []
        for term in _normalized_terms(pattern.get("triggers")):
            if term in haystack_context or (haystack_event and term in haystack_event):
                score += 5
                matched_triggers.append(term)
        if not matched_triggers:
            continue
        score += min(len(matched_triggers), 3)
        scored.append((score, pattern, matched_triggers))

    scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))
    patterns = []
    for score, pattern, matched_triggers in scored[: min(max(limit, 0), MAX_LIMIT)]:
        patterns.append({
            "id": pattern.get("id", ""),
            "score": score,
            "matched_triggers": matched_triggers,
            "advice": pattern.get("advice", ""),
            "boundaries": pattern.get("boundaries", ""),
            "status": pattern.get("status", "experimental"),
            "source_trace": pattern.get("source_trace", ""),
        })

    return {
        "capability": capability,
        "patterns": patterns,
        "reason": "matched" if patterns else "no_match",
        "recall_cost": {"llm_calls": 0, "raw_scans": 0, "files_read": 1},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    recall_parser = sub.add_parser("recall", help="recall at most three generic patterns")
    recall_parser.add_argument("--capability", choices=CAPABILITIES, required=True)
    recall_parser.add_argument("--context", required=True)
    recall_parser.add_argument("--event", default="")
    recall_parser.add_argument("--playbook-hit", action="store_true")
    recall_parser.add_argument("--playbook-allows-experience", action="store_true")
    recall_parser.add_argument("--limit", type=int, default=MAX_LIMIT)
    args = parser.parse_args()
    result = recall(
        capability=args.capability,
        context=args.context,
        event=args.event,
        playbook_hit=args.playbook_hit,
        playbook_allows_experience=args.playbook_allows_experience,
        limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

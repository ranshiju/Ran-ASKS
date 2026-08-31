#!/usr/bin/env python3
"""Resumable runner for the blinded E2b agent navigation audit."""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import itertools
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[4]
SPEC_PATH = ROOT / "config" / "experiment-spec.json"
STATE = ROOT / "state"
PRIVATE = ROOT / "private_inputs"
INPUTS = ROOT / "inputs"
OUTPUTS = ROOT / "outputs"
METRICS = ROOT / "metrics"
VALIDATION = ROOT / "validation"

MEMBERSHIP_SCHEMA = "asks-e2b-judge-response-v1"
ALLOWED_CHOICES = {"A", "B", "tie"}
ALLOWED_RELATIONS = {
    "method",
    "physical_system",
    "property",
    "research_problem",
    "application",
    "cross_field_bridge",
}
SCIENTIFIC_PATTERN = re.compile(
    r"\b(we|this (?:work|paper|study)|propose|present|investigat|demonstrat|results?|"
    r"model|method|algorithm|quantum|physics|phase|network|learning|simulation|theory)\b|"
    r"本文|本研究|提出|研究|结果|模型|方法|算法|量子|物理",
    re.I,
)
METADATA_PATTERN = re.compile(
    r"copyright|exclusive license|no claim to original|university|institute|department|"
    r"laboratory|corresponding author|distributed under|creative commons",
    re.I,
)
ACTION_PATTERN = re.compile(
    r"\b(we|this (?:work|paper|study)|propose|present|investigat|demonstrat|results?|"
    r"model|method|algorithm|show|find|develop|study|analy[sz])\b|"
    r"本文|本研究|提出|研究|结果|模型|方法|算法|发现|表明",
    re.I,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_rank(*parts: object) -> str:
    return sha256_text("|".join(str(part) for part in parts))


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path: Path, obj: object) -> None:
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_spec() -> dict:
    return read_json(SPEC_PATH)


def repo_path(relative: str) -> Path:
    return REPO / relative


def stage_record(stage: str, details: dict) -> None:
    atomic_json(STATE / f"{stage}.json", {
        "schema": f"asks-e2b-{stage}-state-v1",
        "stage": stage,
        "completed_at": utc_now(),
        "status": "pass",
        **details,
    })


def command_audit() -> dict:
    spec = load_spec()
    locked = []
    for item in spec["input_locks"]:
        path = repo_path(item["path"])
        if not path.is_file():
            raise RuntimeError(f"missing locked input: {item['path']}")
        actual = sha256_path(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"locked input changed: {item['path']} ({actual})")
        locked.append({"path": item["path"], "sha256": actual, "size": path.stat().st_size})

    manifest = read_json(repo_path(spec["input_locks"][0]["path"]))
    included = [entry for entry in manifest["entries"] if entry["decision"] == "include"]
    if len(included) != spec["corpus"]["papers"]:
        raise RuntimeError(f"expected 56 included papers, found {len(included)}")
    raw_rows = []
    for entry in included:
        path = repo_path(entry["paper_md"])
        if not path.is_file():
            raise RuntimeError(f"missing Raw paper: {entry['paper_md']}")
        actual = sha256_path(path)
        if actual != entry["paper_md_sha256"]:
            raise RuntimeError(f"Raw paper hash changed: {entry['paper_md']}")
        raw_rows.append({
            "work_id": entry["work_id"],
            "path": entry["paper_md"],
            "sha256": actual,
            "size": path.stat().st_size,
        })

    incidence = read_csv(repo_path("projects/ASKS/experiments/e2-physh-alignment/inputs/hub_paper_incidence.csv"))
    hubs = {row["hub_id"] for row in incidence}
    if len(hubs) != spec["corpus"]["final_hubs"]:
        raise RuntimeError(f"expected 18 Hubs, found {len(hubs)}")

    report = {
        "schema": "asks-e2b-audit-report-v1",
        "status": "pass",
        "checked_at": utc_now(),
        "locked_inputs": locked,
        "raw_papers": len(raw_rows),
        "raw_manifest_digest": sha256_text("\n".join(row["sha256"] for row in raw_rows)),
        "final_hubs": len(hubs),
        "raw_modified": False,
    }
    atomic_json(VALIDATION / "audit-report.json", report)
    stage_record("audit", report)
    return report


def load_read_paper_module():
    path = repo_path(".scripts/read_paper.py")
    module_spec = importlib.util.spec_from_file_location("asks_e2b_read_paper", path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("cannot load .scripts/read_paper.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def clean_markdown_evidence(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"^#{1,6}\s+.*$", " ", text, flags=re.M)
    text = re.sub(r"^DOI\s*:.*$", " ", text, flags=re.I | re.M)
    text = re.sub(r"<sup>.*?</sup>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def select_preamble_abstract(text: str) -> str:
    blocks = [clean_markdown_evidence(block) for block in re.split(r"\n\s*\n", text)]
    candidates = []
    for index, block in enumerate(blocks):
        if len(block) < 120:
            continue
        score = min(len(block), 3000) + 300 * len(SCIENTIFIC_PATTERN.findall(block))
        if METADATA_PATTERN.search(block) or "@" in block:
            score -= 1000
        candidates.append((score, -index, block))
    if not candidates:
        return clean_markdown_evidence(text)
    return max(candidates)[2]


def evidence_quality(text: str) -> bool:
    scientific_hits = len(SCIENTIFIC_PATTERN.findall(text))
    if scientific_hits < 2:
        return False
    if METADATA_PATTERN.search(text) and len(ACTION_PATTERN.findall(text)) < 2:
        return False
    return True


def fallback_abstract_after_title(text: str, expected_title: str) -> str | None:
    lines = text.splitlines()
    expected_tokens = set(re.findall(r"[a-z0-9]+", expected_title.lower()))
    candidates = []
    for index, line in enumerate(lines):
        match = re.match(r"^#\s+(.+)$", line.strip())
        if not match:
            continue
        heading_tokens = set(re.findall(r"[a-z0-9]+", match.group(1).lower()))
        overlap = len(expected_tokens & heading_tokens) / max(len(expected_tokens), 1)
        if overlap >= 0.6:
            candidates.append((overlap, index))
    if not candidates:
        return None
    best_overlap = max(overlap for overlap, _ in candidates)
    start = min(index for overlap, index in candidates if overlap == best_overlap)
    end = min(start + 80, len(lines))
    for index in range(start + 1, min(start + 80, len(lines))):
        if re.match(r"^##\s+(?:\d+[.\s]+)?(?:introduction|引言)\b", lines[index].strip(), re.I):
            end = index
            break
    return select_preamble_abstract("\n".join(lines[start + 1:end]))


def extract_abstract(module, paper_path: Path, maximum: int, expected_title: str = "") -> str:
    full_text = None
    hits, misses, _ = module.extract_sections(str(paper_path), ["abstract"])
    if misses or not hits:
        full_text = paper_path.read_text(encoding="utf-8")
        content = fallback_abstract_after_title(full_text, expected_title)
        if not content:
            raise RuntimeError(f"abstract not found: {paper_path}")
    else:
        _, matched_title, content = hits[0]
        if "preamble" in matched_title:
            content = select_preamble_abstract(content)
        else:
            content = clean_markdown_evidence(content)
        if not evidence_quality(content):
            full_text = full_text or paper_path.read_text(encoding="utf-8")
            fallback = fallback_abstract_after_title(full_text, expected_title)
            if fallback:
                content = fallback
    if len(content) > maximum:
        shortened = content[:maximum]
        boundary = max(shortened.rfind(". "), shortened.rfind("。"))
        content = shortened[:boundary + 1] if boundary >= int(maximum * 0.65) else shortened.rstrip()
    content = content.strip()
    if not evidence_quality(content):
        raise RuntimeError(f"abstract evidence failed scientific-text validation: {paper_path}")
    return content


def command_packets() -> dict:
    if not (STATE / "audit.json").is_file():
        raise RuntimeError("run audit first")
    spec = load_spec()
    manifest = read_json(repo_path("projects/ASKS/experiments/e1-chronological-56/config/manifest-frozen.json"))
    included = {entry["work_id"]: entry for entry in manifest["entries"] if entry["decision"] == "include"}
    paper_rows = read_csv(repo_path("paper-artifacts/v0.2.0/metrics/paper_to_hub.csv"))
    if len(paper_rows) != spec["corpus"]["papers"]:
        raise RuntimeError("paper_to_hub row count changed")
    module = load_read_paper_module()
    packets = []
    index_rows = []
    minimum = spec["evidence"]["minimum_abstract_characters"]
    maximum = spec["evidence"]["maximum_abstract_characters"]
    for row in sorted(paper_rows, key=lambda item: item["paper_id"]):
        entry = included[row["work_id"]]
        abstract = extract_abstract(module, repo_path(entry["paper_md"]), maximum, row["title"])
        if len(abstract) < minimum:
            raise RuntimeError(f"abstract too short for {row['paper_id']}: {len(abstract)} chars")
        packet = {
            "paper_id": row["paper_id"],
            "work_id": row["work_id"],
            "year": int(row["year"]),
            "macro_domain": row["macro_domain"],
            "title": clean_markdown_evidence(row["title"]),
            "abstract": abstract,
            "abstract_sha256": sha256_text(abstract),
            "raw_sha256": entry["paper_md_sha256"],
        }
        packets.append(packet)
        index_rows.append({
            "paper_id": packet["paper_id"],
            "work_id": packet["work_id"],
            "year": packet["year"],
            "macro_domain": packet["macro_domain"],
            "title": packet["title"],
            "abstract_characters": len(abstract),
            "abstract_sha256": packet["abstract_sha256"],
            "raw_sha256": packet["raw_sha256"],
        })
    private_text = "".join(json.dumps(packet, ensure_ascii=False, sort_keys=True) + "\n" for packet in packets)
    atomic_text(PRIVATE / "evidence-packets.jsonl", private_text)
    write_csv(
        VALIDATION / "packet-index.csv",
        index_rows,
        ["paper_id", "work_id", "year", "macro_domain", "title", "abstract_characters", "abstract_sha256", "raw_sha256"],
    )
    details = {
        "packets": len(packets),
        "private_packet_sha256": sha256_text(private_text),
        "minimum_abstract_characters": min(len(packet["abstract"]) for packet in packets),
        "maximum_abstract_characters": max(len(packet["abstract"]) for packet in packets),
        "release_contains_abstracts": False,
    }
    stage_record("packets", details)
    return details


def load_packets() -> dict[str, dict]:
    packets = {}
    with (PRIVATE / "evidence-packets.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                packet = json.loads(line)
                packets[packet["paper_id"]] = packet
    return packets


def time_stratum(year: int, strata: list[list[int]]) -> str:
    for start, end in strata:
        if start <= year <= end:
            return f"{start}-{end}"
    raise RuntimeError(f"year {year} is outside frozen strata")


def choose_negative(
    papers: dict[str, dict],
    supporting: set[str],
    positive: dict,
    used: set[str],
    hub_id: str,
    seed: int,
    strata: list[list[int]],
) -> tuple[dict, bool, bool]:
    target_stratum = time_stratum(positive["year"], strata)
    candidates = [
        paper for paper_id, paper in papers.items()
        if paper_id not in supporting and time_stratum(paper["year"], strata) == target_stratum
    ]
    if not candidates:
        raise RuntimeError(f"no time-matched negative for {hub_id} / {positive['paper_id']}")
    unused = [paper for paper in candidates if paper["paper_id"] not in used]
    pool = unused or candidates
    same_domain = [paper for paper in pool if paper["macro_domain"] == positive["macro_domain"]]
    domain_pool = same_domain or pool
    selected = min(domain_pool, key=lambda paper: stable_rank(seed, hub_id, positive["paper_id"], paper["paper_id"]))
    return selected, bool(same_domain), bool(unused)


def command_trials() -> dict:
    if not (STATE / "packets.json").is_file():
        raise RuntimeError("run packets first")
    spec = load_spec()
    packets = load_packets()
    incidence = read_csv(repo_path("projects/ASKS/experiments/e2-physh-alignment/inputs/hub_paper_incidence.csv"))
    by_hub = defaultdict(list)
    labels = {}
    for row in incidence:
        by_hub[row["hub_id"]].append(row["paper_id"])
        labels[row["hub_id"]] = row["hub_label"]
    if len(by_hub) != spec["corpus"]["final_hubs"]:
        raise RuntimeError("Hub count changed")

    blinded_hubs = []
    private_keys = []
    membership_count = 0
    set_count = 0
    macro_matches = 0
    negative_reuses = 0
    maximum = spec["membership_task"]["maximum_positive_papers_per_hub"]
    seed = spec["seed"]
    for hub_index, hub_id in enumerate(sorted(by_hub), start=1):
        supporting = set(by_hub[hub_id])
        positive_ids = sorted(supporting, key=lambda paper_id: stable_rank(seed, "positive", hub_id, paper_id))[:maximum]
        used_negatives = set()
        membership = []
        matched_negatives = []
        for trial_index, positive_id in enumerate(positive_ids, start=1):
            positive = packets[positive_id]
            negative, macro_matched, unused = choose_negative(
                packets, supporting, positive, used_negatives, hub_id, seed, spec["time_strata"]
            )
            used_negatives.add(negative["paper_id"])
            matched_negatives.append(negative["paper_id"])
            macro_matches += int(macro_matched)
            negative_reuses += int(not unused)
            true_position = "A" if int(stable_rank(seed, "side", hub_id, positive_id)[-1], 16) % 2 == 0 else "B"
            options = {
                true_position: positive_id,
                "B" if true_position == "A" else "A": negative["paper_id"],
            }
            trial_id = f"M-H{hub_index:02d}-{trial_index:02d}"
            membership.append({
                "trial_id": trial_id,
                "option_A": options["A"],
                "option_B": options["B"],
            })
            private_keys.append({
                "trial_id": trial_id,
                "task": "membership",
                "hub_id": hub_id,
                "true_position": true_position,
                "true_paper_id": positive_id,
                "control_paper_id": negative["paper_id"],
                "time_stratum": time_stratum(positive["year"], spec["time_strata"]),
                "macro_domain_matched": macro_matched,
            })
            membership_count += 1

        set_trial = None
        minimum_support = spec["set_task"]["minimum_supporting_papers"]
        set_size = spec["set_task"]["papers_per_set"]
        if len(supporting) >= minimum_support:
            true_ids = positive_ids[:set_size]
            if len(true_ids) < set_size:
                raise RuntimeError(f"not enough selected positives for set task: {hub_id}")
            control_ids = matched_negatives[:set_size]
            true_position = "A" if int(stable_rank(seed, "set-side", hub_id)[-1], 16) % 2 == 0 else "B"
            sets = {
                true_position: true_ids,
                "B" if true_position == "A" else "A": control_ids,
            }
            trial_id = f"S-H{hub_index:02d}"
            set_trial = {"trial_id": trial_id, "set_A": sets["A"], "set_B": sets["B"]}
            private_keys.append({
                "trial_id": trial_id,
                "task": "set_coherence",
                "hub_id": hub_id,
                "true_position": true_position,
                "true_paper_ids": true_ids,
                "control_paper_ids": control_ids,
            })
            set_count += 1

        blinded_hubs.append({
            "hub_index": hub_index,
            "hub_id": hub_id,
            "hub_label": labels[hub_id],
            "membership_trials": membership,
            "set_trial": set_trial,
        })

    blinded = {
        "schema": "asks-e2b-blinded-trials-v1",
        "seed": seed,
        "hubs": blinded_hubs,
    }
    keys = {"schema": "asks-e2b-private-trial-key-v1", "trials": private_keys}
    atomic_json(INPUTS / "blinded-trials.json", blinded)
    atomic_json(PRIVATE / "trial-key.json", keys)
    details = {
        "hubs": len(blinded_hubs),
        "membership_trials": membership_count,
        "set_trials": set_count,
        "macro_domain_matched_membership_trials": macro_matches,
        "reused_negative_assignments": negative_reuses,
        "blinded_trials_sha256": sha256_path(INPUTS / "blinded-trials.json"),
        "private_trial_key_sha256": sha256_path(PRIVATE / "trial-key.json"),
    }
    stage_record("trials", details)
    return details


def load_dotenv() -> dict[str, str]:
    values = {}
    env_path = REPO / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    values.update(os.environ)
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for _ in range(len(values) + 1):
        updated = {key: pattern.sub(lambda match: values.get(match.group(1), match.group(0)), value)
                   for key, value in values.items()}
        if updated == values:
            break
        values = updated
    return values


def api_credentials(spec: dict) -> tuple[str, str]:
    values = load_dotenv()
    api = spec["api"]
    base = values.get(api["base_env"], "") or values.get(api["base_fallback_env"], "")
    key = values.get(api["key_env"], "") or values.get(api["key_fallback_env"], "")
    if not base or not key:
        raise RuntimeError("E2b requires configured API base and key; credentials are not written to outputs")
    return base, key


def parse_json_response(text: str):
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def normalize_choice(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() == "tie":
        return "tie"
    return text.upper()


def validate_judge_response(obj: object, hub: dict) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("response is not a JSON object")
    if obj.get("schema") != MEMBERSHIP_SCHEMA:
        raise ValueError("wrong response schema")
    if obj.get("hub_id") != hub["hub_id"]:
        raise ValueError("wrong hub_id")
    expected = [trial["trial_id"] for trial in hub["membership_trials"]]
    items = obj.get("membership")
    if not isinstance(items, list) or [item.get("trial_id") for item in items] != expected:
        raise ValueError("membership trial IDs or order changed")
    normalized_items = []
    for item in items:
        choice = normalize_choice(item.get("choice"))
        if choice not in ALLOWED_CHOICES:
            raise ValueError(f"invalid choice: {choice}")
        scores = item.get("fit_scores") or {}
        score_a, score_b = int(scores.get("A", 0)), int(scores.get("B", 0))
        confidence = int(item.get("confidence", 0))
        if score_a not in range(1, 6) or score_b not in range(1, 6) or confidence not in range(1, 6):
            raise ValueError("scores and confidence must be integers 1..5")
        relations = list(dict.fromkeys(item.get("relation_basis") or []))
        if any(value not in ALLOWED_RELATIONS for value in relations):
            raise ValueError("invalid relation_basis")
        reason = re.sub(r"\s+", " ", str(item.get("reason", ""))).strip()[:600]
        normalized_items.append({
            "trial_id": item["trial_id"],
            "choice": choice,
            "fit_scores": {"A": score_a, "B": score_b},
            "relation_basis": relations,
            "cross_or_emerging": bool(item.get("cross_or_emerging", False)),
            "confidence": confidence,
            "reason": reason,
        })

    expected_set = hub.get("set_trial")
    set_item = obj.get("set_coherence")
    normalized_set = None
    if expected_set is not None:
        if not isinstance(set_item, dict) or set_item.get("trial_id") != expected_set["trial_id"]:
            raise ValueError("missing or invalid set-coherence result")
        choice = normalize_choice(set_item.get("choice"))
        if choice not in ALLOWED_CHOICES:
            raise ValueError("invalid set choice")
        scores = set_item.get("coherence_scores") or {}
        score_a, score_b = int(scores.get("A", 0)), int(scores.get("B", 0))
        bridge = int(set_item.get("bridge_quality", 0))
        confidence = int(set_item.get("confidence", 0))
        if any(value not in range(1, 6) for value in (score_a, score_b, bridge, confidence)):
            raise ValueError("set scores must be integers 1..5")
        normalized_set = {
            "trial_id": expected_set["trial_id"],
            "choice": choice,
            "coherence_scores": {"A": score_a, "B": score_b},
            "bridge_quality": bridge,
            "confidence": confidence,
            "theme": re.sub(r"\s+", " ", str(set_item.get("theme", ""))).strip()[:400],
            "reason": re.sub(r"\s+", " ", str(set_item.get("reason", ""))).strip()[:600],
        }
    elif set_item not in (None, {}):
        raise ValueError("unexpected set-coherence result")

    return {
        "schema": MEMBERSHIP_SCHEMA,
        "hub_id": hub["hub_id"],
        "membership": normalized_items,
        "set_coherence": normalized_set,
    }


SYSTEM_PROMPT = """You are an independent evaluator of scientific knowledge navigation.
Use only the supplied paper titles and abstracts. Do not use external knowledge or search.
A useful Hub may be interdisciplinary or emerging. Do not reward identical taxonomy alone.
Recognize scientifically interpretable method-to-problem, system-to-technique,
theory-to-application, property, and cross-field relationships. The A/B order is random.
Do not guess which option came from ASKS. Return only the requested JSON and never quote
source sentences. Keep each reason under 60 words."""


def paper_card(packet: dict) -> dict:
    return {"title": packet["title"], "abstract": packet["abstract"]}


def build_prompt(hub: dict, packets: dict[str, dict]) -> str:
    membership = []
    for trial in hub["membership_trials"]:
        membership.append({
            "trial_id": trial["trial_id"],
            "A": paper_card(packets[trial["option_A"]]),
            "B": paper_card(packets[trial["option_B"]]),
        })
    set_task = None
    if hub.get("set_trial"):
        trial = hub["set_trial"]
        set_task = {
            "trial_id": trial["trial_id"],
            "A": [paper_card(packets[paper_id]) for paper_id in trial["set_A"]],
            "B": [paper_card(packets[paper_id]) for paper_id in trial["set_B"]],
        }
    payload = {
        "hub_id": hub["hub_id"],
        "hub_label": hub["hub_label"],
        "membership_trials": membership,
        "set_coherence_trial": set_task,
    }
    instructions = {
        "membership": {
            "question": "Which paper better supports this Hub as a useful scientific navigation entry?",
            "choice": "A, B, or tie",
            "fit_scores": "integer 1..5 for A and B",
            "relation_basis": sorted(ALLOWED_RELATIONS),
            "cross_or_emerging": "boolean",
            "confidence": "integer 1..5",
        },
        "set_coherence": {
            "question": "Which three-paper set better supports a coherent and useful entry under this Hub label?",
            "choice": "A, B, or tie",
            "coherence_scores": "integer 1..5 for A and B",
            "bridge_quality": "integer 1..5 for the chosen set, or the common theme if tied",
            "confidence": "integer 1..5",
        },
        "output_schema": {
            "schema": MEMBERSHIP_SCHEMA,
            "hub_id": hub["hub_id"],
            "membership": [{
                "trial_id": "copy exactly",
                "choice": "A|B|tie",
                "fit_scores": {"A": 1, "B": 1},
                "relation_basis": ["one or more allowed values"],
                "cross_or_emerging": False,
                "confidence": 1,
                "reason": "under 60 words, no quotations",
            }],
            "set_coherence": None if set_task is None else {
                "trial_id": set_task["trial_id"],
                "choice": "A|B|tie",
                "coherence_scores": {"A": 1, "B": 1},
                "bridge_quality": 1,
                "confidence": 1,
                "theme": "short theme",
                "reason": "under 60 words, no quotations",
            },
        },
    }
    return json.dumps({"task": payload, "instructions": instructions}, ensure_ascii=False, indent=2)


def call_model(model: str, prompt: str, spec: dict) -> tuple[dict, dict]:
    base, key = api_credentials(spec)
    api = spec["api"]
    last_error = None
    for attempt in range(1, api["retries"] + 2):
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": api["temperature"],
            "max_tokens": api["max_tokens"],
        }).encode("utf-8")
        request = urllib.request.Request(
            base.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=api["timeout_seconds"]) as response:
                data = json.loads(response.read())
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            parsed = parse_json_response(content)
            metadata = {
                "attempt": attempt,
                "latency_seconds": round(time.time() - started, 3),
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage", {}),
                "response_sha256": sha256_text(content),
            }
            return parsed, metadata
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                raise RuntimeError(f"model {model} authorization failed: HTTP {exc.code}") from exc
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt <= api["retries"]:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"model {model} failed after retries: {last_error}")


def synthetic_preflight_hub() -> tuple[dict, dict[str, dict]]:
    hub = {
        "hub_id": "synthetic/preflight",
        "hub_label": "Quantum methods for emerging interdisciplinary systems",
        "membership_trials": [{"trial_id": "PREFLIGHT-M1", "option_A": "P-A", "option_B": "P-B"}],
        "set_trial": None,
    }
    packets = {
        "P-A": {"title": "A hybrid quantum method for graph learning", "abstract": "A quantum circuit is combined with graph representations to learn structural patterns across scientific data. The work connects quantum machine learning methods with graph analysis and evaluates the combined method on several graph tasks."},
        "P-B": {"title": "Classical heat transport in a uniform rod", "abstract": "A finite-difference calculation studies one-dimensional heat diffusion in a homogeneous rod under fixed boundary temperatures. The work reports convergence of the classical discretization for this established thermal model."},
    }
    return hub, packets


def command_preflight() -> dict:
    if not (STATE / "trials.json").is_file():
        raise RuntimeError("run trials first")
    spec = load_spec()
    hub, packets = synthetic_preflight_hub()
    prompt = build_prompt(hub, packets)
    rows = []
    for judge in spec["judges"]:
        parsed, metadata = call_model(judge["model"], prompt, spec)
        normalized = validate_judge_response(parsed, hub)
        record = {
            "schema": "asks-e2b-preflight-v1",
            "judge_id": judge["judge_id"],
            "model": judge["model"],
            "prompt_sha256": sha256_text(prompt),
            "validated_response": normalized,
            **metadata,
        }
        atomic_json(STATE / "preflight" / f"{judge['judge_id']}.json", record)
        rows.append({"judge_id": judge["judge_id"], "model": judge["model"], "status": "pass"})
    details = {"judges": rows, "synthetic_only": True, "included_in_analysis": False}
    stage_record("preflight", details)
    return details


def checkpoint_path(judge_id: str, hub_index: int) -> Path:
    return STATE / "judge-checkpoints" / judge_id / f"hub-{hub_index:02d}.json"


def checkpoint_valid(path: Path, judge: dict, hub: dict, prompt_hash: str) -> bool:
    if not path.is_file():
        return False
    try:
        record = read_json(path)
        if record.get("model") != judge["model"] or record.get("hub_id") != hub["hub_id"]:
            return False
        if record.get("prompt_sha256") != prompt_hash:
            return False
        validate_judge_response(record.get("response"), hub)
        return True
    except Exception:
        return False


def command_judge() -> dict:
    if not (STATE / "preflight.json").is_file():
        raise RuntimeError("run preflight first")
    spec = load_spec()
    trials = read_json(INPUTS / "blinded-trials.json")
    packets = load_packets()
    completed = 0
    reused = 0
    for judge in spec["judges"]:
        for hub in trials["hubs"]:
            prompt = build_prompt(hub, packets)
            prompt_hash = sha256_text(prompt)
            path = checkpoint_path(judge["judge_id"], hub["hub_index"])
            if checkpoint_valid(path, judge, hub, prompt_hash):
                reused += 1
                continue
            parsed, metadata = call_model(judge["model"], prompt, spec)
            normalized = validate_judge_response(parsed, hub)
            packet_ids = set()
            for trial in hub["membership_trials"]:
                packet_ids.update((trial["option_A"], trial["option_B"]))
            if hub.get("set_trial"):
                packet_ids.update(hub["set_trial"]["set_A"])
                packet_ids.update(hub["set_trial"]["set_B"])
            record = {
                "schema": "asks-e2b-judge-checkpoint-v1",
                "judge_id": judge["judge_id"],
                "model": judge["model"],
                "family": judge["family"],
                "hub_index": hub["hub_index"],
                "hub_id": hub["hub_id"],
                "prompt_sha256": prompt_hash,
                "evidence_hashes": {paper_id: packets[paper_id]["abstract_sha256"] for paper_id in sorted(packet_ids)},
                "response": normalized,
                "completed_at": utc_now(),
                **metadata,
            }
            atomic_json(path, record)
            completed += 1
    expected = len(spec["judges"]) * len(trials["hubs"])
    available = sum(
        checkpoint_path(judge["judge_id"], hub["hub_index"]).is_file()
        for judge in spec["judges"] for hub in trials["hubs"]
    )
    if available != expected:
        raise RuntimeError(f"judge checkpoints incomplete: {available}/{expected}")
    details = {"expected_checkpoints": expected, "new_checkpoints": completed, "reused_checkpoints": reused}
    stage_record("judge", details)
    return details


def score_choice(choice: str, true_position: str) -> float:
    if choice == "tie":
        return 0.5
    return 1.0 if choice == true_position else 0.0


def relative_choice(choice: str, true_position: str) -> str:
    if choice == "tie":
        return "tie"
    return "true" if choice == true_position else "control"


def exact_sign_flip(values: list[float], null: float = 0.5) -> dict:
    deviations = [value - null for value in values]
    observed = sum(deviations) / len(deviations)
    total = 1 << len(deviations)
    greater = 0
    tolerance = 1e-15
    for mask in range(total):
        permuted = sum((1 if (mask >> index) & 1 else -1) * value for index, value in enumerate(deviations)) / len(deviations)
        greater += int(permuted >= observed - tolerance)
    return {"observed_minus_null": observed, "exact_one_sided_p": greater / total, "sign_flips": total}


def cohens_kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    categories = ["true", "control", "tie"]
    observed = sum(a == b for a, b in pairs) / len(pairs)
    counts_a = Counter(a for a, _ in pairs)
    counts_b = Counter(b for _, b in pairs)
    expected = sum((counts_a[c] / len(pairs)) * (counts_b[c] / len(pairs)) for c in categories)
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else None
    return (observed - expected) / (1 - expected)


def command_analyze() -> dict:
    if not (STATE / "judge.json").is_file():
        raise RuntimeError("run judge first")
    spec = load_spec()
    trials = read_json(INPUTS / "blinded-trials.json")
    key_rows = read_json(PRIVATE / "trial-key.json")["trials"]
    keys = {row["trial_id"]: row for row in key_rows}
    judge_outputs = []
    hub_model_scores = defaultdict(list)
    hub_set_scores = defaultdict(list)
    judge_trials = defaultdict(list)
    judge_set_trials = defaultdict(list)
    position_counts = defaultdict(Counter)
    relation_counts = defaultdict(Counter)
    by_trial_choice = defaultdict(dict)
    disagreements = []

    for judge in spec["judges"]:
        for hub in trials["hubs"]:
            record = read_json(checkpoint_path(judge["judge_id"], hub["hub_index"]))
            response = validate_judge_response(record["response"], hub)
            output_record = {
                "judge_id": judge["judge_id"],
                "model": judge["model"],
                "hub_index": hub["hub_index"],
                "hub_id": hub["hub_id"],
                "prompt_sha256": record["prompt_sha256"],
                "response": response,
            }
            judge_outputs.append(output_record)
            hub_values = []
            for item in response["membership"]:
                key = keys[item["trial_id"]]
                value = score_choice(item["choice"], key["true_position"])
                relative = relative_choice(item["choice"], key["true_position"])
                hub_values.append(value)
                judge_trials[judge["judge_id"]].append(value)
                position_counts[judge["judge_id"]][item["choice"]] += 1
                relation_counts[judge["judge_id"]].update(item["relation_basis"])
                by_trial_choice[item["trial_id"]][judge["judge_id"]] = relative
            hub_model_scores[(hub["hub_id"], judge["judge_id"])] = hub_values
            if response["set_coherence"]:
                item = response["set_coherence"]
                key = keys[item["trial_id"]]
                value = score_choice(item["choice"], key["true_position"])
                judge_set_trials[judge["judge_id"]].append(value)
                hub_set_scores[(hub["hub_id"], judge["judge_id"])].append(value)

    judge_ids = [judge["judge_id"] for judge in spec["judges"]]
    hub_rows = []
    consensus_hub_values = []
    judge_hub_values = {judge_id: [] for judge_id in judge_ids}
    for hub in trials["hubs"]:
        row = {"hub_id": hub["hub_id"], "hub_label": hub["hub_label"]}
        model_values = []
        for judge_id in judge_ids:
            values = hub_model_scores[(hub["hub_id"], judge_id)]
            mean_value = sum(values) / len(values)
            row[judge_id] = mean_value
            judge_hub_values[judge_id].append(mean_value)
            model_values.append(mean_value)
        row["consensus"] = sum(model_values) / len(model_values)
        row["membership_trials"] = len(hub["membership_trials"])
        consensus_hub_values.append(row["consensus"])
        hub_rows.append(row)

    primary_score = sum(consensus_hub_values) / len(consensus_hub_values)
    primary_test = exact_sign_flip(consensus_hub_values)
    judge_summaries = []
    position_ok = True
    judge_direction_ok = True
    for judge in spec["judges"]:
        judge_id = judge["judge_id"]
        values = judge_hub_values[judge_id]
        score = sum(values) / len(values)
        test = exact_sign_flip(values)
        total = sum(position_counts[judge_id].values())
        position_difference = abs(position_counts[judge_id]["A"] / total - position_counts[judge_id]["B"] / total)
        position_ok &= position_difference <= spec["confirmatory_gate"]["maximum_absolute_position_choice_difference"]
        judge_direction_ok &= score > spec["confirmatory_gate"]["each_judge_score_greater_than"]
        judge_summaries.append({
            "judge_id": judge_id,
            "model": judge["model"],
            "membership_hub_macro_score": score,
            "membership_exact_p": test["exact_one_sided_p"],
            "membership_trials": len(judge_trials[judge_id]),
            "set_score": (sum(judge_set_trials[judge_id]) / len(judge_set_trials[judge_id])) if judge_set_trials[judge_id] else None,
            "set_trials": len(judge_set_trials[judge_id]),
            "choice_A": position_counts[judge_id]["A"],
            "choice_B": position_counts[judge_id]["B"],
            "choice_tie": position_counts[judge_id]["tie"],
            "absolute_position_choice_difference": position_difference,
        })

    agreement_pairs = []
    for trial_id, choices in sorted(by_trial_choice.items()):
        if all(judge_id in choices for judge_id in judge_ids):
            pair = (choices[judge_ids[0]], choices[judge_ids[1]])
            agreement_pairs.append(pair)
            if pair[0] != pair[1]:
                disagreements.append({
                    "trial_id": trial_id,
                    judge_ids[0]: pair[0],
                    judge_ids[1]: pair[1],
                    "hub_id": keys[trial_id]["hub_id"],
                })
    exact_agreement = sum(a == b for a, b in agreement_pairs) / len(agreement_pairs)
    tie_aware = sum(a == b or "tie" in (a, b) for a, b in agreement_pairs) / len(agreement_pairs)
    kappa = cohens_kappa(agreement_pairs)

    gate = {
        "primary_p": primary_test["exact_one_sided_p"] < spec["confirmatory_gate"]["primary_p_less_than"],
        "each_judge_direction_positive": judge_direction_ok,
        "position_bias_within_limit": position_ok,
        "complete_hub_coverage": all(len(judge_hub_values[judge_id]) == spec["confirmatory_gate"]["required_hubs_per_judge"] for judge_id in judge_ids),
    }
    gate["passed"] = all(gate.values())
    primary = {
        "schema": "asks-e2b-primary-result-v1",
        "endpoint": spec["primary_endpoint"]["name"],
        "hubs": len(consensus_hub_values),
        "judges": len(judge_ids),
        "membership_trials_per_judge": len(judge_trials[judge_ids[0]]),
        "score": primary_score,
        "null": spec["primary_endpoint"]["null"],
        **primary_test,
        "confirmatory_gate": gate,
        "agreement": {
            "exact": exact_agreement,
            "tie_aware": tie_aware,
            "cohens_kappa": kappa,
        },
        "scope": "Blinded evidence-bounded model-judge audit of semantic fit and navigation usefulness.",
    }
    atomic_json(METRICS / "primary.json", primary)
    write_csv(
        METRICS / "hub_scores.csv",
        hub_rows,
        ["hub_id", "hub_label", *judge_ids, "consensus", "membership_trials"],
    )
    write_csv(
        METRICS / "judge_summary.csv",
        judge_summaries,
        ["judge_id", "model", "membership_hub_macro_score", "membership_exact_p", "membership_trials", "set_score", "set_trials", "choice_A", "choice_B", "choice_tie", "absolute_position_choice_difference"],
    )
    write_csv(
        METRICS / "disagreements.csv",
        disagreements,
        ["trial_id", judge_ids[0], judge_ids[1], "hub_id"],
    )
    relation_rows = []
    for judge_id in judge_ids:
        for relation in sorted(ALLOWED_RELATIONS):
            relation_rows.append({"judge_id": judge_id, "relation_basis": relation, "count": relation_counts[judge_id][relation]})
    write_csv(METRICS / "relation_bases.csv", relation_rows, ["judge_id", "relation_basis", "count"])
    output_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in judge_outputs)
    atomic_text(OUTPUTS / "judge-outputs.jsonl", output_text)
    trial_release_rows = []
    for row in key_rows:
        trial_release_rows.append({
            "trial_id": row["trial_id"],
            "task": row["task"],
            "hub_id": row["hub_id"],
            "true_position": row["true_position"],
            "true_papers": row.get("true_paper_id") or ";".join(row.get("true_paper_ids", [])),
            "control_papers": row.get("control_paper_id") or ";".join(row.get("control_paper_ids", [])),
        })
    write_csv(OUTPUTS / "trial-key.csv", trial_release_rows, ["trial_id", "task", "hub_id", "true_position", "true_papers", "control_papers"])
    stage_record("analyze", {"primary": primary, "judge_summaries": judge_summaries})
    return primary


def command_freeze() -> dict:
    if not (STATE / "analyze.json").is_file():
        raise RuntimeError("run analyze first")
    spec = load_spec()
    primary = read_json(METRICS / "primary.json")
    trials = read_json(INPUTS / "blinded-trials.json")
    expected_checkpoints = len(spec["judges"]) * len(trials["hubs"])
    actual_checkpoints = sum(1 for judge in spec["judges"] for hub in trials["hubs"] if checkpoint_path(judge["judge_id"], hub["hub_index"]).is_file())
    checks = {
        "papers": len(read_csv(VALIDATION / "packet-index.csv")) == spec["corpus"]["papers"],
        "hubs": len(trials["hubs"]) == spec["corpus"]["final_hubs"],
        "judge_checkpoints": actual_checkpoints == expected_checkpoints,
        "primary_hubs": primary["hubs"] == spec["corpus"]["final_hubs"],
        "private_abstracts_excluded": True,
    }
    if not all(checks.values()):
        raise RuntimeError(f"freeze checks failed: {checks}")
    summary = {
        "schema": "asks-e2b-final-summary-v1",
        "status": "frozen",
        "frozen_at": utc_now(),
        "checks": checks,
        "primary_result": primary,
        "models": spec["judges"],
        "release_scope": spec["release_boundary"],
    }
    atomic_json(VALIDATION / "final-summary.json", summary)
    release_files = [
        ROOT / "README.md",
        SPEC_PATH,
        ROOT / "run.py",
        ROOT / "test_run.py",
        INPUTS / "blinded-trials.json",
        OUTPUTS / "judge-outputs.jsonl",
        OUTPUTS / "trial-key.csv",
        METRICS / "primary.json",
        METRICS / "hub_scores.csv",
        METRICS / "judge_summary.csv",
        METRICS / "disagreements.csv",
        METRICS / "relation_bases.csv",
        VALIDATION / "audit-report.json",
        VALIDATION / "packet-index.csv",
        VALIDATION / "final-summary.json",
    ]
    missing = [path for path in release_files if not path.is_file()]
    if missing:
        raise RuntimeError("release files missing: " + ", ".join(str(path) for path in missing))
    lines = [f"{sha256_path(path)}  {path.relative_to(ROOT)}" for path in sorted(release_files)]
    atomic_text(ROOT / "CHECKSUMS.sha256", "\n".join(lines) + "\n")
    stage_record("freeze", {"checks": checks, "checksums": len(lines)})
    return summary


COMMANDS = {
    "audit": command_audit,
    "packets": command_packets,
    "trials": command_trials,
    "preflight": command_preflight,
    "judge": command_judge,
    "analyze": command_analyze,
    "freeze": command_freeze,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=COMMANDS)
    args = parser.parse_args()
    try:
        result = COMMANDS[args.stage]()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

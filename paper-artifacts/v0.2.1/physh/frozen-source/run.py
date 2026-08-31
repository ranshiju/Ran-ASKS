#!/usr/bin/env python3
"""Resumable runner for E2 external PhySH alignment."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import itertools
import json
import math
import os
import random
import shutil
import statistics
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


EXPERIMENT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = EXPERIMENT_ROOT / "config" / "experiment-spec.json"


def find_repo_root() -> Path:
    for candidate in [EXPERIMENT_ROOT, *EXPERIMENT_ROOT.parents]:
        if (candidate / ".scripts").is_dir() and (candidate / "projects").is_dir():
            return candidate
    raise RuntimeError("Cannot locate WikiGraph repository root")


REPO_ROOT = find_repo_root()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_spec() -> dict[str, Any]:
    return load_json(CONFIG_PATH)


def repo_path(relative: str) -> Path:
    return REPO_ROOT / relative


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    )
    atomic_write_text(path, text)


def atomic_write_csv(
    path: Path, fieldnames: list[str], rows: Iterable[dict[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def checkpoint_path(stage: str) -> Path:
    return EXPERIMENT_ROOT / "state" / f"{stage}.json"


def write_checkpoint(
    stage: str, outputs: Iterable[Path], details: dict[str, Any]
) -> None:
    output_rows = []
    for output in outputs:
        output_rows.append(
            {
                "path": str(output.relative_to(EXPERIMENT_ROOT)),
                "sha256": sha256_file(output),
                "size": output.stat().st_size,
            }
        )
    atomic_write_json(
        checkpoint_path(stage),
        {
            "schema": "asks-e2-stage-checkpoint-v1",
            "stage": stage,
            "status": "complete",
            "completed_at": utc_now(),
            "outputs": output_rows,
            "details": details,
        },
    )


def checkpoint_valid(stage: str) -> bool:
    path = checkpoint_path(stage)
    if not path.is_file():
        return False
    try:
        payload = load_json(path)
        if payload.get("status") != "complete" or payload.get("stage") != stage:
            return False
        for output in payload.get("outputs", []):
            target = EXPERIMENT_ROOT / output["path"]
            if not target.is_file() or sha256_file(target) != output["sha256"]:
                return False
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def require_checkpoint(stage: str) -> None:
    if not checkpoint_valid(stage):
        raise RuntimeError(f"Required stage is incomplete or invalid: {stage}")


def normalize_doi(value: str) -> str:
    doi = value.strip().lower()
    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
            break
    return doi.strip()


def set_f1(left: set[str], right: set[str]) -> float:
    denominator = len(left) + len(right)
    if denominator == 0:
        return 0.0
    return 2.0 * len(left & right) / denominator


def quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("Cannot compute a quantile of an empty sequence")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def stage_audit() -> dict[str, Any]:
    spec = load_spec()
    checks: list[dict[str, Any]] = []

    def verify(relative: str, expected: str) -> None:
        path = repo_path(relative)
        if not path.is_file():
            raise FileNotFoundError(path)
        observed = sha256_file(path)
        if observed != expected:
            raise RuntimeError(
                f"Frozen input hash mismatch: {relative}: {observed} != {expected}"
            )
        checks.append(
            {
                "path": relative,
                "sha256": observed,
                "size": path.stat().st_size,
            }
        )

    asks = spec["asks_inputs"]
    verify(asks["manifest"], asks["manifest_sha256"])
    verify(asks["final_snapshot"], asks["final_snapshot_sha256"])
    verify(asks["released_edges"], asks["released_edges_sha256"])
    verify(asks["hub_summary"], asks["hub_summary_sha256"])
    verify(asks["paper_to_hub"], asks["paper_to_hub_sha256"])

    report_root = repo_path(
        "projects/ASKS/experiments/e1-chronological-56/runs/"
        f"{asks['run_id']}/logs/fusion"
    )
    reports = sorted(report_root.glob("G*-report.json"))
    expected_reports = int(asks["fusion_report_count"])
    if len(reports) != expected_reports:
        raise RuntimeError(f"Expected {expected_reports} fusion reports, found {len(reports)}")
    report_digest = hashlib.sha256()
    for index, path in enumerate(reports, start=1):
        payload = load_json(path)
        if payload.get("step") != index or not isinstance(payload.get("node_support"), list):
            raise RuntimeError(f"Invalid fusion report contract: {path}")
        report_digest.update(path.name.encode("utf-8"))
        report_digest.update(bytes.fromhex(sha256_file(path)))

    arxiv_pdf = repo_path(spec["manuscript_boundary"]["arxiv_v1_pdf"])
    if not arxiv_pdf.is_file():
        raise FileNotFoundError(arxiv_pdf)

    output = EXPERIMENT_ROOT / "validation" / "audit-report.json"
    result = {
        "schema": "asks-e2-audit-v1",
        "status": "pass",
        "checked_at": utc_now(),
        "files": checks,
        "fusion_report_count": len(reports),
        "fusion_reports_digest": report_digest.hexdigest(),
        "arxiv_v1_pdf": str(arxiv_pdf.relative_to(REPO_ROOT)),
        "arxiv_v1_pdf_sha256": sha256_file(arxiv_pdf),
    }
    atomic_write_json(output, result)
    write_checkpoint("audit", [output], {"status": "pass"})
    return result


def github_content_url(path: str, commit: str) -> str:
    return (
        "https://api.github.com/repos/Ma-Lab-Cal/aps-physh/contents/"
        f"{path}?ref={commit}"
    )


def download_with_resume(url: str, target: Path, expected_sha256: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        observed = sha256_file(target)
        if observed != expected_sha256:
            raise RuntimeError(f"Existing cached file has wrong hash: {target}")
        return

    partial = target.with_name(target.name + ".part")
    start = partial.stat().st_size if partial.exists() else 0
    headers = {
        "Accept": "application/vnd.github.raw+json",
        "User-Agent": "ASKS-E2-PhySH/1.0",
    }
    if start:
        headers["Range"] = f"bytes={start}-"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status = getattr(response, "status", 200)
            mode = "ab" if start and status == 206 else "wb"
            with partial.open(mode) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Download interrupted, rerun to resume: {url}: {exc}") from exc

    observed = sha256_file(partial)
    if observed != expected_sha256:
        raise RuntimeError(
            f"Downloaded file hash mismatch, partial retained: {target.name}: {observed}"
        )
    os.replace(partial, target)


def stage_fetch() -> dict[str, Any]:
    require_checkpoint("audit")
    spec = load_spec()
    physh = spec["physh_inputs"]
    commit = physh["commit"]
    cache = EXPERIMENT_ROOT / "cache" / "physh"
    files = [
        (physh["gold_path"], "ground_truth.jsonl.gz", physh["gold_sha256"]),
        (physh["concepts_path"], "concepts.json", physh["concepts_sha256"]),
        (physh["facets_path"], "facets.json", physh["facets_sha256"]),
        ("DATA_LICENSE", "DATA_LICENSE", physh["data_license_sha256"]),
        ("NOTICE", "NOTICE", physh["notice_sha256"]),
    ]
    outputs: list[Path] = []
    for upstream, local_name, expected in files:
        target = cache / local_name
        download_with_resume(github_content_url(upstream, commit), target, expected)
        outputs.append(target)

    third_party = EXPERIMENT_ROOT / "third_party" / "physh"
    license_target = third_party / "DATA_LICENSE"
    notice_target = third_party / "NOTICE"
    atomic_write_bytes(license_target, (cache / "DATA_LICENSE").read_bytes())
    atomic_write_bytes(notice_target, (cache / "NOTICE").read_bytes())
    outputs.extend([license_target, notice_target])

    source_lock = EXPERIMENT_ROOT / "validation" / "physh-source-lock.json"
    result = {
        "schema": "asks-e2-physh-source-lock-v1",
        "status": "pass",
        "repository": physh["repository"],
        "commit": commit,
        "retrieved_at": utc_now(),
        "files": [
            {
                "upstream_path": upstream,
                "local_path": str((cache / local_name).relative_to(EXPERIMENT_ROOT)),
                "sha256": expected,
                "size": (cache / local_name).stat().st_size,
            }
            for upstream, local_name, expected in files
        ],
        "label_license": physh["gold_license"],
        "taxonomy_license": physh["concepts_license"],
    }
    atomic_write_json(source_lock, result)
    outputs.append(source_lock)
    write_checkpoint("fetch", outputs, {"source_count": len(files)})
    return result


def manifest_works() -> list[dict[str, Any]]:
    spec = load_spec()
    manifest = load_json(repo_path(spec["asks_inputs"]["manifest"]))
    included = [entry for entry in manifest["entries"] if entry["decision"] == "include"]
    return sorted(included, key=lambda entry: int(entry["sequence_index"]))


def stage_match() -> dict[str, Any]:
    require_checkpoint("fetch")
    spec = load_spec()
    expected = spec["acceptance_counts"]
    works = manifest_works()
    by_doi = {
        normalize_doi(entry["publication_evidence"]["doi"]): entry
        for entry in works
        if normalize_doi(entry["publication_evidence"]["doi"])
    }

    matched: list[dict[str, Any]] = []
    gold_path = EXPERIMENT_ROOT / "cache" / "physh" / "ground_truth.jsonl.gz"
    with gzip.open(gold_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            doi = normalize_doi(row["doi"])
            entry = by_doi.get(doi)
            if not entry:
                continue
            matched.append(
                {
                    "paper_id": f"D{int(entry['sequence_index']):03d}",
                    "work_id": entry["entry_id"],
                    "sequence_index": int(entry["sequence_index"]),
                    "year": int(entry["publication_date"]),
                    "doi": entry["publication_evidence"]["doi"],
                    "manifest_title": entry["title_hint"],
                    "physh_title": row["title"],
                    "disciplines": row["disciplines"],
                    "concepts": row["concepts"],
                    "label_source": "APS-assigned PhySH gold",
                }
            )
    matched.sort(key=lambda row: row["sequence_index"])
    if len(matched) != int(expected["official_gold_matches"]):
        raise RuntimeError(
            f"Expected {expected['official_gold_matches']} PhySH matches, found {len(matched)}"
        )

    concepts = load_json(EXPERIMENT_ROOT / "cache" / "physh" / "concepts.json")
    label_to_facet: dict[str, str] = {}
    duplicate_labels: set[str] = set()
    for payload in concepts.values():
        label = payload["label"]
        if label in label_to_facet and label_to_facet[label] != payload["facet"]:
            duplicate_labels.add(label)
        label_to_facet[label] = payload["facet"]
    missing_concepts = sorted(
        {
            concept
            for row in matched
            for concept in row["concepts"]
            if concept not in label_to_facet
        }
    )
    if duplicate_labels or missing_concepts:
        raise RuntimeError(
            f"Taxonomy mapping ambiguity: duplicates={sorted(duplicate_labels)}, "
            f"missing={missing_concepts}"
        )

    matched_by_work = {row["work_id"]: row for row in matched}
    coverage_rows: list[dict[str, Any]] = []
    for entry in works:
        work_id = entry["entry_id"]
        doi = entry["publication_evidence"]["doi"]
        gold = matched_by_work.get(work_id)
        is_aps = normalize_doi(doi).startswith("10.1103/")
        if gold:
            reason = "official_gold"
        elif is_aps:
            reason = "aps_without_released_gold"
        else:
            reason = "non_aps"
        coverage_rows.append(
            {
                "paper_id": f"D{int(entry['sequence_index']):03d}",
                "work_id": work_id,
                "year": int(entry["publication_date"]),
                "doi": doi,
                "is_aps_doi": int(is_aps),
                "official_gold": int(gold is not None),
                "discipline_count": len(gold["disciplines"]) if gold else 0,
                "concept_count": len(gold["concepts"]) if gold else 0,
                "coverage_status": reason,
            }
        )

    aps_count = sum(row["is_aps_doi"] for row in coverage_rows)
    if aps_count != int(expected["aps_doi_papers"]):
        raise RuntimeError(f"Expected {expected['aps_doi_papers']} APS DOI rows, found {aps_count}")

    matched_path = EXPERIMENT_ROOT / "inputs" / "physh_gold_matched.jsonl"
    coverage_path = EXPERIMENT_ROOT / "inputs" / "physh_coverage.csv"
    atomic_write_jsonl(matched_path, matched)
    atomic_write_csv(
        coverage_path,
        [
            "paper_id",
            "work_id",
            "year",
            "doi",
            "is_aps_doi",
            "official_gold",
            "discipline_count",
            "concept_count",
            "coverage_status",
        ],
        coverage_rows,
    )
    result = {
        "manifest_papers": len(works),
        "aps_doi_papers": aps_count,
        "official_gold_matches": len(matched),
        "taxonomy_concepts": len(concepts),
    }
    write_checkpoint("match", [matched_path, coverage_path], result)
    return result


def final_memberships(snapshot_path: Path, allowed_hubs: set[str]) -> dict[str, set[str]]:
    memberships: dict[str, set[str]] = defaultdict(set)
    with snapshot_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("_table") != "edges" or row.get("predicate") != "聚类于":
                continue
            if row.get("object") in allowed_hubs:
                memberships[row["object"]].add(row["subject"])
    return memberships


def stage_incidence() -> dict[str, Any]:
    require_checkpoint("match")
    spec = load_spec()
    asks = spec["asks_inputs"]
    hub_summary = read_csv(repo_path(asks["hub_summary"]))
    hub_by_id = {row["hub_id"]: row for row in hub_summary}
    memberships = final_memberships(
        repo_path(asks["final_snapshot"]), set(hub_by_id)
    )

    support: dict[str, set[str]] = defaultdict(set)
    run_root = repo_path(
        "projects/ASKS/experiments/e1-chronological-56/runs/"
        f"{asks['run_id']}/logs/fusion"
    )
    reports = sorted(run_root.glob("G*-report.json"))
    for report_path in reports:
        report = load_json(report_path)
        for row in report["node_support"]:
            support[row["canonical_node_id"]].add(row["work_id"])

    paper_rows = read_csv(repo_path(asks["paper_to_hub"]))
    paper_by_work = {row["work_id"]: row for row in paper_rows}
    gold_rows = [json.loads(line) for line in (EXPERIMENT_ROOT / "inputs" / "physh_gold_matched.jsonl").read_text(encoding="utf-8").splitlines() if line]
    gold_work_ids = {row["work_id"] for row in gold_rows}

    incidence_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    mismatch: list[dict[str, Any]] = []
    for hub_id, summary in sorted(
        hub_by_id.items(), key=lambda item: int(item[1]["birth_step"])
    ):
        work_node_counts: dict[str, int] = defaultdict(int)
        for member in memberships.get(hub_id, set()):
            for work_id in support.get(member, set()):
                work_node_counts[work_id] += 1
        expected_count = int(summary["supporting_papers_at_final"])
        if len(work_node_counts) != expected_count:
            mismatch.append(
                {
                    "hub_id": hub_id,
                    "expected": expected_count,
                    "observed": len(work_node_counts),
                }
            )
        gold_count = sum(work_id in gold_work_ids for work_id in work_node_counts)
        coverage_rows.append(
            {
                "hub_id": hub_id,
                "hub_label": summary["hub_label"],
                "supporting_papers_final": len(work_node_counts),
                "official_gold_papers": gold_count,
                "eligible_primary_analysis": int(gold_count >= 2),
            }
        )
        for work_id, member_count in sorted(work_node_counts.items()):
            paper = paper_by_work.get(work_id)
            if paper is None:
                raise RuntimeError(f"Missing paper metadata for support work: {work_id}")
            incidence_rows.append(
                {
                    "hub_id": hub_id,
                    "hub_label": summary["hub_label"],
                    "work_id": work_id,
                    "paper_id": paper["paper_id"],
                    "year": paper["year"],
                    "doi": next(
                        (
                            entry["publication_evidence"]["doi"]
                            for entry in manifest_works()
                            if entry["entry_id"] == work_id
                        ),
                        "",
                    ),
                    "supporting_member_node_count": member_count,
                    "official_gold": int(work_id in gold_work_ids),
                }
            )
    if mismatch:
        raise RuntimeError(f"Hub supporting-paper count mismatch: {mismatch}")

    eligible_count = sum(row["eligible_primary_analysis"] for row in coverage_rows)
    expected_eligible = int(
        spec["acceptance_counts"]["eligible_hubs_min_two_gold_papers"]
    )
    if eligible_count != expected_eligible:
        raise RuntimeError(f"Expected {expected_eligible} eligible Hubs, found {eligible_count}")

    incidence_path = EXPERIMENT_ROOT / "inputs" / "hub_paper_incidence.csv"
    coverage_path = EXPERIMENT_ROOT / "inputs" / "hub_gold_coverage.csv"
    atomic_write_csv(
        incidence_path,
        [
            "hub_id",
            "hub_label",
            "work_id",
            "paper_id",
            "year",
            "doi",
            "supporting_member_node_count",
            "official_gold",
        ],
        incidence_rows,
    )
    atomic_write_csv(
        coverage_path,
        [
            "hub_id",
            "hub_label",
            "supporting_papers_final",
            "official_gold_papers",
            "eligible_primary_analysis",
        ],
        coverage_rows,
    )
    result = {
        "hub_count": len(coverage_rows),
        "incidence_rows": len(incidence_rows),
        "eligible_hubs": eligible_count,
        "supporting_paper_counts_reproduced": True,
    }
    write_checkpoint("incidence", [incidence_path, coverage_path], result)
    return result


def label_profiles() -> dict[str, dict[str, Any]]:
    concepts = load_json(EXPERIMENT_ROOT / "cache" / "physh" / "concepts.json")
    label_to_facet = {payload["label"]: payload["facet"] for payload in concepts.values()}
    profiles: dict[str, dict[str, Any]] = {}
    matched_path = EXPERIMENT_ROOT / "inputs" / "physh_gold_matched.jsonl"
    for line in matched_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        row = json.loads(line)
        profiles[row["work_id"]] = {
            "work_id": row["work_id"],
            "year": int(row["year"]),
            "concepts": set(row["concepts"]),
            "disciplines": set(row["disciplines"]),
            "facets": {label_to_facet[concept] for concept in row["concepts"]},
        }
    return profiles


def incidence_groups(profiles: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(EXPERIMENT_ROOT / "inputs" / "hub_paper_incidence.csv"):
        if row["work_id"] in profiles:
            groups[row["hub_id"]].add(row["work_id"])
    minimum = int(load_spec()["primary_analysis"]["minimum_gold_papers_per_hub"])
    return {
        hub_id: sorted(work_ids)
        for hub_id, work_ids in sorted(groups.items())
        if len(work_ids) >= minimum
    }


def primary_hub_groups(profiles: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    path = repo_path(load_spec()["asks_inputs"]["paper_to_hub"])
    groups: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(path):
        if row["work_id"] in profiles and row["primary_hub_id"]:
            groups[row["primary_hub_id"]].add(row["work_id"])
    return {
        hub_id: sorted(work_ids)
        for hub_id, work_ids in sorted(groups.items())
        if len(work_ids) >= 2
    }


def macro_coherence(
    groups: dict[str, list[str]],
    profiles: dict[str, dict[str, Any]],
    field: str,
) -> tuple[float, dict[str, dict[str, Any]]]:
    details: dict[str, dict[str, Any]] = {}
    for group_id, work_ids in groups.items():
        similarities = [
            set_f1(profiles[left][field], profiles[right][field])
            for left, right in itertools.combinations(work_ids, 2)
        ]
        if not similarities:
            continue
        details[group_id] = {
            "paper_count": len(work_ids),
            "pair_count": len(similarities),
            "coherence": statistics.fmean(similarities),
        }
    if not details:
        raise RuntimeError(f"No eligible groups for field: {field}")
    return statistics.fmean(row["coherence"] for row in details.values()), details


def analysis_digest() -> str:
    paths = [
        CONFIG_PATH,
        EXPERIMENT_ROOT / "inputs" / "physh_gold_matched.jsonl",
        EXPERIMENT_ROOT / "inputs" / "hub_paper_incidence.csv",
        repo_path(load_spec()["asks_inputs"]["paper_to_hub"]),
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(REPO_ROOT)).encode("utf-8"))
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def permutation_seed(master_seed: int, chunk_index: int) -> int:
    payload = f"{master_seed}:{chunk_index}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def permuted_profiles(
    profiles: dict[str, dict[str, Any]],
    rng: random.Random,
    strata: list[list[int]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    assigned: set[str] = set()
    for start, end in strata:
        work_ids = sorted(
            work_id
            for work_id, profile in profiles.items()
            if start <= int(profile["year"]) <= end
        )
        shuffled = [profiles[work_id] for work_id in work_ids]
        rng.shuffle(shuffled)
        for target, source in zip(work_ids, shuffled):
            result[target] = {
                "work_id": target,
                "year": profiles[target]["year"],
                "concepts": source["concepts"],
                "disciplines": source["disciplines"],
                "facets": source["facets"],
            }
            assigned.add(target)
    missing = set(profiles) - assigned
    if missing:
        raise RuntimeError(f"Profiles outside frozen time strata: {sorted(missing)}")
    return result


def chunk_valid(path: Path, digest: str, expected_count: int) -> bool:
    if not path.is_file():
        return False
    try:
        payload = load_json(path)
        if payload.get("analysis_digest") != digest:
            return False
        values = payload.get("values", {})
        return all(len(series) == expected_count for series in values.values())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def metric_summary(observed: float, null_values: list[float]) -> dict[str, float]:
    null_mean = statistics.fmean(null_values)
    null_std = statistics.pstdev(null_values)
    return {
        "observed": observed,
        "null_mean": null_mean,
        "difference": observed - null_mean,
        "null_q025": quantile(null_values, 0.025),
        "null_q975": quantile(null_values, 0.975),
        "standardized_difference": (
            (observed - null_mean) / null_std if null_std else math.inf
        ),
        "permutation_p_greater": (
            1 + sum(value >= observed for value in null_values)
        )
        / (len(null_values) + 1),
    }


def stage_analyze() -> dict[str, Any]:
    require_checkpoint("incidence")
    spec = load_spec()
    analysis = spec["primary_analysis"]
    profiles = label_profiles()
    groups = incidence_groups(profiles)
    primary_groups = primary_hub_groups(profiles)
    expected_groups = int(
        spec["acceptance_counts"]["eligible_hubs_min_two_gold_papers"]
    )
    if len(groups) != expected_groups:
        raise RuntimeError(f"Expected {expected_groups} eligible groups, found {len(groups)}")

    observed_concept, concept_details = macro_coherence(groups, profiles, "concepts")
    observed_discipline, discipline_details = macro_coherence(
        groups, profiles, "disciplines"
    )
    observed_facet, facet_details = macro_coherence(groups, profiles, "facets")
    observed_primary, _ = macro_coherence(primary_groups, profiles, "concepts")
    observed = {
        "Q_concept_exact": observed_concept,
        "Q_discipline_exact": observed_discipline,
        "Q_facet_exact": observed_facet,
        "Q_primary_hub_concept_exact": observed_primary,
    }

    total = int(analysis["permutations_total_B"])
    chunk_size = int(analysis["permutation_chunk_size"])
    if total % chunk_size:
        raise RuntimeError("Permutation total must be divisible by chunk size")
    chunk_count = total // chunk_size
    digest = analysis_digest()
    chunks_root = EXPERIMENT_ROOT / "state" / "permutation-chunks"
    chunks_root.mkdir(parents=True, exist_ok=True)
    strata = analysis["time_strata"]
    chunk_paths: list[Path] = []
    for chunk_index in range(chunk_count):
        path = chunks_root / f"chunk-{chunk_index:04d}.json"
        chunk_paths.append(path)
        if chunk_valid(path, digest, chunk_size):
            continue
        seed = permutation_seed(int(analysis["master_seed"]), chunk_index)
        rng = random.Random(seed)
        values = {key: [] for key in observed}
        for _ in range(chunk_size):
            permuted = permuted_profiles(profiles, rng, strata)
            values["Q_concept_exact"].append(
                macro_coherence(groups, permuted, "concepts")[0]
            )
            values["Q_discipline_exact"].append(
                macro_coherence(groups, permuted, "disciplines")[0]
            )
            values["Q_facet_exact"].append(
                macro_coherence(groups, permuted, "facets")[0]
            )
            values["Q_primary_hub_concept_exact"].append(
                macro_coherence(primary_groups, permuted, "concepts")[0]
            )
        atomic_write_json(
            path,
            {
                "schema": "asks-e2-permutation-chunk-v1",
                "analysis_digest": digest,
                "chunk_index": chunk_index,
                "start_b": chunk_index * chunk_size + 1,
                "count": chunk_size,
                "seed": seed,
                "values": values,
            },
        )

    null_values = {key: [] for key in observed}
    for path in chunk_paths:
        payload = load_json(path)
        for key in null_values:
            null_values[key].extend(float(value) for value in payload["values"][key])
    if any(len(values) != total for values in null_values.values()):
        raise RuntimeError("Incomplete permutation aggregation")

    summaries = {
        key: metric_summary(observed[key], null_values[key]) for key in observed
    }
    primary_payload = {
        "schema": "asks-e2-primary-result-v1",
        "analysis_digest": digest,
        "endpoint": "Q_concept_exact",
        "label_source": "APS-assigned PhySH gold",
        "official_gold_papers": len(profiles),
        "eligible_hubs": len(groups),
        "permutations_B": total,
        "time_strata": strata,
        **summaries["Q_concept_exact"],
        "scope": (
            "External semantic alignment of the final approximate navigation "
            "structure on the officially labeled subset."
        ),
    }
    primary_path = EXPERIMENT_ROOT / "metrics" / "primary.json"
    atomic_write_json(primary_path, primary_payload)

    secondary_rows = []
    for key in (
        "Q_concept_exact",
        "Q_discipline_exact",
        "Q_facet_exact",
        "Q_primary_hub_concept_exact",
    ):
        secondary_rows.append(
            {
                "metric": key,
                "is_primary": int(key == "Q_concept_exact"),
                **summaries[key],
            }
        )
    secondary_path = EXPERIMENT_ROOT / "metrics" / "secondary.csv"
    atomic_write_csv(
        secondary_path,
        [
            "metric",
            "is_primary",
            "observed",
            "null_mean",
            "difference",
            "null_q025",
            "null_q975",
            "standardized_difference",
            "permutation_p_greater",
        ],
        secondary_rows,
    )

    hub_labels = {
        row["hub_id"]: row["hub_label"]
        for row in read_csv(EXPERIMENT_ROOT / "inputs" / "hub_gold_coverage.csv")
    }
    hub_rows = []
    for hub_id in sorted(groups):
        hub_rows.append(
            {
                "hub_id": hub_id,
                "hub_label": hub_labels[hub_id],
                "gold_paper_count": concept_details[hub_id]["paper_count"],
                "paper_pair_count": concept_details[hub_id]["pair_count"],
                "concept_exact_f1": concept_details[hub_id]["coherence"],
                "discipline_exact_f1": discipline_details[hub_id]["coherence"],
                "facet_exact_f1": facet_details[hub_id]["coherence"],
            }
        )
    hub_path = EXPERIMENT_ROOT / "metrics" / "hub_coherence.csv"
    atomic_write_csv(
        hub_path,
        [
            "hub_id",
            "hub_label",
            "gold_paper_count",
            "paper_pair_count",
            "concept_exact_f1",
            "discipline_exact_f1",
            "facet_exact_f1",
        ],
        hub_rows,
    )

    permutation_summary_path = EXPERIMENT_ROOT / "metrics" / "permutation_summary.json"
    atomic_write_json(
        permutation_summary_path,
        {
            "schema": "asks-e2-permutation-summary-v1",
            "analysis_digest": digest,
            "permutations_B": total,
            "chunk_count": chunk_count,
            "chunk_size": chunk_size,
            "metrics": summaries,
        },
    )

    values_path = EXPERIMENT_ROOT / "metrics" / "permutation_values.csv.gz"
    values_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = values_path.with_name(values_path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["b", *observed.keys()])
        writer.writeheader()
        for index in range(total):
            writer.writerow(
                {
                    "b": index + 1,
                    **{key: null_values[key][index] for key in observed},
                }
            )
    os.replace(temporary, values_path)

    outputs = [
        primary_path,
        secondary_path,
        hub_path,
        permutation_summary_path,
        values_path,
        *chunk_paths,
    ]
    write_checkpoint(
        "analyze",
        outputs,
        {
            "analysis_digest": digest,
            "permutations_B": total,
            "primary": summaries["Q_concept_exact"],
        },
    )
    return primary_payload


def stage_figure() -> dict[str, Any]:
    require_checkpoint("analyze")
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for the figure stage") from exc

    primary = load_json(EXPERIMENT_ROOT / "metrics" / "primary.json")
    secondary = read_csv(EXPERIMENT_ROOT / "metrics" / "secondary.csv")
    values_path = EXPERIMENT_ROOT / "metrics" / "permutation_values.csv.gz"
    null_values: list[float] = []
    with gzip.open(values_path, "rt", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            null_values.append(float(row["Q_concept_exact"]))

    figure_data = [
        row
        for row in secondary
        if row["metric"] in {
            "Q_concept_exact",
            "Q_discipline_exact",
            "Q_facet_exact",
        }
    ]
    labels = ["Concepts", "Disciplines", "Facets"]
    effects = [float(row["difference"]) for row in figure_data]

    plt.rcParams.update({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9})
    fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.35), constrained_layout=True)
    axes[0].hist(null_values, bins=36, color="#b9bec6", edgecolor="white")
    axes[0].axvline(
        float(primary["observed"]), color="#b43c35", linewidth=2, label="Observed"
    )
    axes[0].axvline(
        float(primary["null_mean"]),
        color="#30343b",
        linewidth=1.5,
        linestyle="--",
        label="Null mean",
    )
    axes[0].set_title("(a) Exact PhySH concept alignment")
    axes[0].set_xlabel("Hub-macro coherence $Q_{concept}$")
    axes[0].set_ylabel("Permutation count")
    axes[0].legend(frameon=False)

    colors = ["#b43c35", "#3b6f8a", "#4f7c56"]
    axes[1].bar(labels, effects, color=colors, width=0.64)
    axes[1].axhline(0.0, color="#30343b", linewidth=1)
    axes[1].set_title("(b) Observed minus time-matched null")
    axes[1].set_ylabel("Alignment effect")

    figures = EXPERIMENT_ROOT / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    pdf_path = figures / "figure-e2-physh-alignment.pdf"
    png_path = figures / "figure-e2-physh-alignment.png"
    fig.savefig(pdf_path, dpi=300)
    fig.savefig(png_path, dpi=300)
    plt.close(fig)

    data_path = figures / "figure-e2-data.csv"
    atomic_write_csv(
        data_path,
        [
            "metric",
            "observed",
            "null_mean",
            "difference",
            "null_q025",
            "null_q975",
            "permutation_p_greater",
        ],
        [
            {
                key: row[key]
                for key in (
                    "metric",
                    "observed",
                    "null_mean",
                    "difference",
                    "null_q025",
                    "null_q975",
                    "permutation_p_greater",
                )
            }
            for row in figure_data
        ],
    )
    result = {
        "pdf": str(pdf_path.relative_to(EXPERIMENT_ROOT)),
        "png": str(png_path.relative_to(EXPERIMENT_ROOT)),
        "data": str(data_path.relative_to(EXPERIMENT_ROOT)),
    }
    write_checkpoint("figure", [pdf_path, png_path, data_path], result)
    return result


def release_files() -> list[Path]:
    roots = [
        EXPERIMENT_ROOT / "README.md",
        EXPERIMENT_ROOT / "run.py",
        EXPERIMENT_ROOT / "config",
        EXPERIMENT_ROOT / "inputs",
        EXPERIMENT_ROOT / "metrics",
        EXPERIMENT_ROOT / "figures",
        EXPERIMENT_ROOT / "third_party",
        EXPERIMENT_ROOT / "validation",
    ]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file())
    return sorted(
        path
        for path in files
        if path.name != "CHECKSUMS.sha256" and "__pycache__" not in path.parts
    )


def stage_freeze() -> dict[str, Any]:
    for stage in ("audit", "fetch", "match", "incidence", "analyze", "figure"):
        require_checkpoint(stage)
    spec = load_spec()
    coverage = read_csv(EXPERIMENT_ROOT / "inputs" / "physh_coverage.csv")
    matched_count = sum(int(row["official_gold"]) for row in coverage)
    hub_coverage = read_csv(EXPERIMENT_ROOT / "inputs" / "hub_gold_coverage.csv")
    eligible_count = sum(int(row["eligible_primary_analysis"]) for row in hub_coverage)
    primary = load_json(EXPERIMENT_ROOT / "metrics" / "primary.json")

    expected = spec["acceptance_counts"]
    checks = {
        "manifest_papers": len(coverage) == int(expected["manifest_papers"]),
        "official_gold_matches": matched_count == int(expected["official_gold_matches"]),
        "final_hubs": len(hub_coverage) == int(expected["final_hubs"]),
        "eligible_hubs": eligible_count
        == int(expected["eligible_hubs_min_two_gold_papers"]),
        "permutations": int(primary["permutations_B"])
        == int(spec["primary_analysis"]["permutations_total_B"]),
    }
    if not all(checks.values()):
        raise RuntimeError(f"Freeze validation failed: {checks}")

    summary_path = EXPERIMENT_ROOT / "validation" / "final-summary.json"
    result = {
        "schema": "asks-e2-final-summary-v1",
        "status": "frozen",
        "frozen_at": utc_now(),
        "checks": checks,
        "primary_result": primary,
        "release_scope": (
            "Release-safe E2 protocol, matched CC BY 4.0 labels, exact Hub-paper "
            "incidence, metrics, permutation outputs, figure data, and validation records."
        ),
        "excluded": [
            "full upstream APS gold cache",
            "source PDFs and parsed Raw text",
            "production graph state",
        ],
    }
    atomic_write_json(summary_path, result)

    checksums_path = EXPERIMENT_ROOT / "CHECKSUMS.sha256"
    lines = [
        f"{sha256_file(path)}  {path.relative_to(EXPERIMENT_ROOT)}"
        for path in release_files()
    ]
    atomic_write_text(checksums_path, "\n".join(lines) + "\n")
    write_checkpoint(
        "freeze",
        [summary_path, checksums_path],
        {"status": "frozen", "release_file_count": len(lines)},
    )
    return result


def stage_status() -> dict[str, Any]:
    stages = ["audit", "fetch", "match", "incidence", "analyze", "figure", "freeze"]
    return {stage: "complete" if checkpoint_valid(stage) else "pending" for stage in stages}


def run_all() -> dict[str, Any]:
    results = {}
    for name, function in (
        ("audit", stage_audit),
        ("fetch", stage_fetch),
        ("match", stage_match),
        ("incidence", stage_incidence),
        ("analyze", stage_analyze),
        ("figure", stage_figure),
        ("freeze", stage_freeze),
    ):
        results[name] = function()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stage",
        choices=[
            "audit",
            "fetch",
            "match",
            "incidence",
            "analyze",
            "figure",
            "freeze",
            "status",
            "all",
        ],
    )
    args = parser.parse_args()
    functions = {
        "audit": stage_audit,
        "fetch": stage_fetch,
        "match": stage_match,
        "incidence": stage_incidence,
        "analyze": stage_analyze,
        "figure": stage_figure,
        "freeze": stage_freeze,
        "status": stage_status,
        "all": run_all,
    }
    result = functions[args.stage]()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

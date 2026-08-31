#!/usr/bin/env python3
"""Recompute the published E2 and E2b endpoints from release-safe files."""

from __future__ import annotations

import csv
import gzip
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def set_f1(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return 2.0 * len(left & right) / (len(left) + len(right))


def exact_sign_flip(values: list[float], null: float = 0.5) -> float:
    deviations = [value - null for value in values]
    observed = mean(deviations)
    greater = 0
    for signs in itertools.product((-1, 1), repeat=len(deviations)):
        permuted = mean([sign * value for sign, value in zip(signs, deviations)])
        greater += int(permuted >= observed - 1e-15)
    return greater / (1 << len(deviations))


def cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    categories = ("true", "control", "tie")
    observed = sum(left == right for left, right in pairs) / len(pairs)
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    expected = sum(
        (left_counts[item] / len(pairs)) * (right_counts[item] / len(pairs))
        for item in categories
    )
    return (observed - expected) / (1.0 - expected)


def physh_result() -> dict:
    labels = {}
    labels_path = ROOT / "physh" / "inputs" / "physh_gold_matched.jsonl"
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        labels[record["paper_id"]] = set(record["concepts"])

    papers_by_hub: dict[str, set[str]] = defaultdict(set)
    for row in read_csv(ROOT / "physh" / "inputs" / "hub_paper_incidence.csv"):
        if row["official_gold"] == "1":
            papers_by_hub[row["hub_id"]].add(row["paper_id"])

    hub_values = []
    for paper_ids in papers_by_hub.values():
        if len(paper_ids) < 2:
            continue
        pair_values = [
            set_f1(labels[left], labels[right])
            for left, right in itertools.combinations(sorted(paper_ids), 2)
        ]
        hub_values.append(mean(pair_values))
    observed = mean(hub_values)

    null_values = []
    with gzip.open(
        ROOT / "physh" / "metrics" / "permutation_values.csv.gz",
        "rt",
        encoding="utf-8",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            null_values.append(float(row["Q_concept_exact"]))
    p_value = (1 + sum(value >= observed for value in null_values)) / (len(null_values) + 1)
    return {
        "eligible_hubs": len(hub_values),
        "observed": observed,
        "null_mean": mean(null_values),
        "permutations": len(null_values),
        "one_sided_p": p_value,
    }


def relative_choice(choice: str, true_position: str) -> str:
    if choice == "tie":
        return "tie"
    return "true" if choice == true_position else "control"


def choice_score(choice: str, true_position: str) -> float:
    return {"true": 1.0, "tie": 0.5, "control": 0.0}[
        relative_choice(choice, true_position)
    ]


def model_judge_result() -> dict:
    keys = {
        row["trial_id"]: row
        for row in read_csv(ROOT / "model-judge" / "outputs" / "trial-key.csv")
    }
    membership: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    set_scores: dict[str, list[float]] = defaultdict(list)
    choices: dict[str, dict[str, str]] = defaultdict(dict)
    judges = []
    output_path = ROOT / "model-judge" / "outputs" / "judge-outputs.jsonl"
    for line in output_path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        judge_id = record["judge_id"]
        if judge_id not in judges:
            judges.append(judge_id)
        hub_id = record["hub_id"]
        for item in record["response"]["membership"]:
            key = keys[item["trial_id"]]
            score = choice_score(item["choice"], key["true_position"])
            membership[hub_id][judge_id].append(score)
            choices[item["trial_id"]][judge_id] = relative_choice(
                item["choice"], key["true_position"]
            )
        set_item = record["response"].get("set_coherence")
        if set_item:
            key = keys[set_item["trial_id"]]
            set_scores[judge_id].append(choice_score(set_item["choice"], key["true_position"]))

    judge_hub_values: dict[str, list[float]] = {judge_id: [] for judge_id in judges}
    consensus = []
    for hub_id in sorted(membership):
        hub_judge_values = []
        for judge_id in judges:
            value = mean(membership[hub_id][judge_id])
            judge_hub_values[judge_id].append(value)
            hub_judge_values.append(value)
        consensus.append(mean(hub_judge_values))

    pairs = [
        (trial_choices[judges[0]], trial_choices[judges[1]])
        for _, trial_choices in sorted(choices.items())
    ]
    return {
        "hubs": len(consensus),
        "membership_trials_per_judge": len(pairs),
        "Q_nav": mean(consensus),
        "one_sided_p": exact_sign_flip(consensus),
        "judge_scores": {judge_id: mean(judge_hub_values[judge_id]) for judge_id in judges},
        "secondary_set_scores": {judge_id: mean(set_scores[judge_id]) for judge_id in judges},
        "exact_agreement": sum(left == right for left, right in pairs) / len(pairs),
        "tie_aware_agreement": sum(left == right or "tie" in (left, right) for left, right in pairs) / len(pairs),
        "cohens_kappa": cohens_kappa(pairs),
    }


def assert_close(actual: float, expected: float, label: str) -> None:
    if abs(actual - expected) > 1e-12:
        raise RuntimeError(f"{label} mismatch: {actual} != {expected}")


def main() -> None:
    physh = physh_result()
    model_judge = model_judge_result()
    metadata = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    assert_close(physh["observed"], metadata["physh_audit"]["primary_observed"], "PhySH observed")
    assert_close(physh["null_mean"], metadata["physh_audit"]["primary_null_mean"], "PhySH null mean")
    assert_close(physh["one_sided_p"], metadata["physh_audit"]["primary_one_sided_p"], "PhySH p")
    assert_close(model_judge["Q_nav"], metadata["model_judge_audit"]["primary_score"], "Q_nav")
    assert_close(model_judge["one_sided_p"], metadata["model_judge_audit"]["primary_exact_one_sided_p"], "model-judge p")
    assert_close(model_judge["exact_agreement"], metadata["model_judge_audit"]["exact_agreement"], "exact agreement")
    assert_close(model_judge["tie_aware_agreement"], metadata["model_judge_audit"]["tie_aware_agreement"], "tie-aware agreement")
    assert_close(model_judge["cohens_kappa"], metadata["model_judge_audit"]["cohens_kappa"], "Cohen kappa")
    print(json.dumps({"physh": physh, "model_judge": model_judge}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

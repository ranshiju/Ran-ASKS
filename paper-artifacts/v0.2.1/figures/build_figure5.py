#!/usr/bin/env python3
"""Build the manuscript figure that combines the frozen E2 and E2b audits."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
E2 = ROOT / "physh"
E2B = ROOT / "model-judge"
OUTPUT = Path(__file__).resolve().parent


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_inputs() -> tuple[dict, list[float], list[dict[str, str]]]:
    primary = load_json(E2 / "metrics" / "primary.json")
    null_values: list[float] = []
    with gzip.open(
        E2 / "metrics" / "permutation_values.csv.gz",
        "rt",
        encoding="utf-8",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            null_values.append(float(row["Q_concept_exact"]))

    hub_scores = load_csv(E2B / "metrics" / "hub_scores.csv")
    if len(null_values) != 10_000:
        raise RuntimeError(f"Expected 10,000 permutation values, found {len(null_values)}")
    if len(hub_scores) != 18:
        raise RuntimeError(f"Expected 18 Hub scores, found {len(hub_scores)}")
    return primary, null_values, hub_scores


def write_summary(primary: dict, hub_scores: list[dict[str, str]]) -> Path:
    path = OUTPUT / "figure5-data.csv"
    fields = [
        "record_type",
        "record_id",
        "label",
        "gpt56sol",
        "claude48",
        "consensus",
        "membership_trials",
        "observed",
        "null_mean",
        "p_value",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "record_type": "physh_summary",
                "record_id": "Q_concept_exact",
                "label": "Exact PhySH concept alignment",
                "observed": primary["observed"],
                "null_mean": primary["null_mean"],
                "p_value": primary["permutation_p_greater"],
            }
        )
        for index, row in enumerate(hub_scores, start=1):
            writer.writerow(
                {
                    "record_type": "model_judge_hub",
                    "record_id": f"H{index:02d}",
                    "label": row["hub_label"],
                    "gpt56sol": row["gpt56sol"],
                    "claude48": row["claude48"],
                    "consensus": row["consensus"],
                    "membership_trials": row["membership_trials"],
                }
            )
    return path


def build_figure(primary: dict, null_values: list[float], hub_scores: list[dict[str, str]]) -> list[Path]:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9.5,
            "axes.titlesize": 10.5,
            "axes.labelsize": 9.5,
            "axes.linewidth": 0.8,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.fontsize": 8.5,
        }
    )
    fig, axes = plt.subplots(1, 2, figsize=(8.15, 3.25), constrained_layout=True)

    observed = float(primary["observed"])
    null_mean = float(primary["null_mean"])
    p_value = float(primary["permutation_p_greater"])
    axes[0].hist(null_values, bins=38, color="#b7bcc5", edgecolor="white", linewidth=0.5)
    axes[0].axvline(observed, color="#b33b32", linewidth=2.0, label=f"Observed = {observed:.3f}")
    axes[0].axvline(
        null_mean,
        color="#30343b",
        linewidth=1.4,
        linestyle="--",
        label=f"Null mean = {null_mean:.3f}",
    )
    axes[0].set_title("(a) PhySH concept alignment", loc="left")
    axes[0].set_xlabel(r"Hub-macro alignment $Q_{\mathrm{concept}}$")
    axes[0].set_ylabel("Permutation count")
    axes[0].legend(frameon=False, loc="upper right")
    axes[0].text(
        0.97,
        0.70,
        rf"one-sided $p={p_value:.3f}$",
        transform=axes[0].transAxes,
        ha="right",
        va="top",
        fontsize=8.5,
    )

    gpt = [float(row["gpt56sol"]) for row in hub_scores]
    claude = [float(row["claude48"]) for row in hub_scores]
    consensus = [float(row["consensus"]) for row in hub_scores]
    nav_score = sum(consensus) / len(consensus)
    colors = ["#b33b32" if score < 0.5 else "#c58b25" if score == 0.5 else "#287c78" for score in consensus]
    axes[1].plot([0, 1], [0, 1], color="#777b82", linewidth=1.0, linestyle="--", zorder=1)
    axes[1].axvline(0.5, color="#c7c9cc", linewidth=0.8, linestyle=":", zorder=0)
    axes[1].axhline(0.5, color="#c7c9cc", linewidth=0.8, linestyle=":", zorder=0)
    axes[1].scatter(gpt, claude, c=colors, s=46, edgecolor="white", linewidth=0.7, zorder=2)
    for index, (x_value, y_value, score) in enumerate(zip(gpt, claude, consensus), start=1):
        if score <= 0.5:
            axes[1].annotate(
                f"H{index:02d}",
                (x_value, y_value),
                xytext=(5, 4),
                textcoords="offset points",
                fontsize=7.5,
            )
    axes[1].set_xlim(-0.04, 1.04)
    axes[1].set_ylim(-0.04, 1.04)
    axes[1].set_aspect("equal", adjustable="box")
    axes[1].set_title("(b) Blinded model-judge navigation", loc="left")
    axes[1].set_xlabel("GPT-family Hub score")
    axes[1].set_ylabel("Claude-family Hub score")
    axes[1].text(
        0.04,
        0.96,
        rf"$Q_{{\mathrm{{nav}}}}={nav_score:.3f}$" + "\n" + r"$p=6.48\times10^{-4}$",
        transform=axes[1].transAxes,
        ha="left",
        va="top",
        fontsize=8.5,
        bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.82, "pad": 1.5},
    )

    pdf_path = OUTPUT / "figure5-external-audit-evidence.pdf"
    png_path = OUTPUT / "figure5-external-audit-evidence.png"
    fig.savefig(pdf_path, dpi=300, bbox_inches="tight")
    fig.savefig(png_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return [pdf_path, png_path]


def main() -> None:
    primary, null_values, hub_scores = load_inputs()
    paths = build_figure(primary, null_values, hub_scores)
    paths.append(write_summary(primary, hub_scores))
    for path in paths:
        print(f"{sha256(path)}  {path.name}")


if __name__ == "__main__":
    main()

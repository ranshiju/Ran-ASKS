#!/usr/bin/env python3
"""Build and verify the sanitized paper artifact released with Ran-ASKS."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
ASKS_RELEASE_VERSION = "0.2.0"
ARTIFACT_VERSION = "1.0.0"
DEFAULT_EXPERIMENT = REPO / "projects/ASKS/experiments/e1-chronological-56"
DEFAULT_RUN = DEFAULT_EXPERIMENT / "runs/bd7dc4b6e166"
DEFAULT_MANUSCRIPT_METRICS = (
    REPO / "projects/ASKS/manu/v16/figures/manuscript-derived-metrics.json"
)
DEFAULT_OUTPUT = REPO / f"paper-artifacts/v{ASKS_RELEASE_VERSION}"
GENERATED_DIRS = ("config", "corpus", "graph", "metrics", "validation", "wiki")
MAINTAINED_DOCS = ("README.md", "DATA_DICTIONARY.md", "LICENSE-DATA.md")
REQUIRED_DOCS = MAINTAINED_DOCS + ("CODE_PROVENANCE.md",)
GRAPH_FIELDS = {
    "nodes": (
        "path", "title", "type", "entity_subtype", "source_type", "date", "status",
        "has_raw_source", "ingest_version", "description",
    ),
    "aliases": ("alias", "node_path"),
    "edges": ("id", "subject", "predicate", "object", "source", "confidence", "score", "is_sr"),
    "edge_origins": ("edge_id", "origin_page", "source", "recorded_at"),
}
TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt"}
FORBIDDEN_SUFFIXES = {".db", ".pdf", ".ppt", ".pptx", ".sqlite", ".sqlite3"}
FORBIDDEN_CONTENT = (
    re.compile("s" "jran", re.IGNORECASE),
    re.compile(r"/(?:Users)/[^/\\]+/", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\(?:Users)\\[^\\]+\\", re.IGNORECASE),
    re.compile(r"SynologyDrive", re.IGNORECASE),
    re.compile(r"projects/(?:ASKS|ForBetterScience)/experiments", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:api[_-]?key|authorization|bearer)\s*[:=]", re.IGNORECASE),
)
MACRO_DOMAIN = {
    "PEPS 与 iPEPS 更新方法": "TN-core",
    "张量重正化群及可微变体": "TN-core",
    "PEPS/PESS 表示及高维推广": "TN-core",
    "张量网络方法与机器学习": "TN-ML",
    "神经网络求解电子薛定谔方程": "quantum-AI",
    "msQFM 及其编码方案": "quantum-AI",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sanitize_artifact_path(text: str) -> str:
    text = text.replace("\x0c", r"\f")
    patterns = (
        r"projects/(?:ASKS|ForBetterScience)/experiments/e1-chronological-56/"
        r"runs/[0-9a-f]+/local-artifacts/",
        r"projects/(?:ASKS|ForBetterScience)/experiments/e1-chronological-56/local-artifacts/",
        r"projects/(?:ASKS|ForBetterScience)/experiments/e1-chronological/local-artifacts/",
    )
    for pattern in patterns:
        text = re.sub(pattern, "raw-not-distributed/", text)
    return text


def sanitize_wiki(text: str, artifact_id: str | None = None) -> str:
    text = sanitize_artifact_path(text)
    text = text.replace("academic/raw/references/", "raw-not-distributed/")
    if artifact_id:
        text = re.sub(
            r"raw-not-distributed/[^/\s\"']+/paper\.md",
            f"raw-not-distributed/{artifact_id}/paper.md",
            text,
        )
    return text


def sanitize_graph_record(record: dict) -> dict:
    result = dict(record)
    for key in ("description", "source"):
        if isinstance(result.get(key), str):
            result[key] = sanitize_artifact_path(result[key])
    return result


def load_wiki_titles(run: Path) -> dict[str, str]:
    titles = {}
    for path in (run / "derived/academic/wiki/papers").rglob("*.md"):
        match = re.search(r'^title:\s*"(.*)"\s*$', path.read_text(encoding="utf-8"), re.MULTILINE)
        if not match:
            raise ValueError(f"missing quoted Wiki title: {path}")
        run_id = path.stem.split("-", 1)[0]
        titles[run_id] = match.group(1).replace(r'\"', '"')
    return titles


def write_csv(path: Path, fieldnames: tuple[str, ...] | list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def prepare_generated_dirs(output: Path) -> None:
    for name in MAINTAINED_DOCS:
        if not (output / name).is_file():
            raise ValueError(f"missing maintained artifact document: {output / name}")
    for name in GENERATED_DIRS:
        target = output / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    checksum_path = output / "CHECKSUMS.sha256"
    if checksum_path.exists():
        checksum_path.unlink()
    metadata_path = output / "metadata.json"
    if metadata_path.exists():
        metadata_path.unlink()
    provenance_path = output / "CODE_PROVENANCE.md"
    if provenance_path.exists():
        provenance_path.unlink()


def build_corpus(
    experiment: Path, canonical_titles: dict[str, str], output: Path
) -> tuple[list[dict], str]:
    source = experiment / "config/manifest-frozen.json"
    manifest = load_json(source)
    rows = []
    public_entries = []
    for entry in manifest["entries"]:
        evidence = entry.get("publication_evidence") or {}
        sequence = entry.get("sequence_index")
        run_id = f"D{int(sequence):03d}" if sequence else ""
        public = {
            "run_id": run_id,
            "sequence_index": sequence,
            "entry_id": entry.get("entry_id", ""),
            "work_id": entry.get("work_id", ""),
            "decision": entry.get("decision", ""),
            "role": entry.get("role", ""),
            "related_to": entry.get("related_to", ""),
            "publication_year": entry.get("publication_date", ""),
            "title": canonical_titles.get(run_id, entry.get("title_hint", "")),
            "doi": evidence.get("doi", ""),
            "canonical_pdf_sha256": entry.get("canonical_pdf_sha256", ""),
            "formal_publication_signal": evidence.get("formal_publication_signal", False),
            "publication_evidence": evidence.get("evidence", []),
            "venue_signals": evidence.get("venue_signals", []),
            "review_reasons": entry.get("review_reasons", []),
            "publication_date_evidence": entry.get("publication_date_evidence", ""),
            "adjudication_note": entry.get("adjudication_note", ""),
        }
        public_entries.append(public)
        rows.append({
            **public,
            "publication_evidence": ";".join(public["publication_evidence"]),
            "venue_signals": ";".join(public["venue_signals"]),
            "review_reasons": ";".join(public["review_reasons"]),
        })
    public_manifest = {
        "schema": "ran-asks.paper-artifact.corpus.v1",
        "source_manifest_schema": manifest.get("schema"),
        "source_manifest_sha256": sha256_file(source),
        "entries": public_entries,
    }
    write_json(output / "corpus/manifest.json", public_manifest)
    fields = list(rows[0])
    write_csv(output / "corpus/manifest.csv", fields, rows)
    return public_entries, sha256_file(source)


def build_wiki(run: Path, output: Path) -> dict[str, int]:
    source_root = run / "derived/academic/wiki"
    counts = {}
    for kind in ("papers", "hubs"):
        sources = sorted((source_root / kind).rglob("*.md"))
        target_root = output / "wiki" / kind
        target_root.mkdir(parents=True, exist_ok=True)
        for source in sources:
            target = target_root / source.relative_to(source_root / kind)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                sanitize_wiki(
                    source.read_text(encoding="utf-8"),
                    artifact_id=source.stem if kind == "papers" else None,
                ),
                encoding="utf-8",
            )
        counts[kind] = len(sources)
    return counts


def build_graph(run: Path, canonical_titles: dict[str, str], output: Path) -> dict[str, int]:
    source = run / "snapshots/G056.jsonl"
    target = output / "graph/final-graph.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[dict]] = {table: [] for table in GRAPH_FIELDS}
    with source.open(encoding="utf-8") as source_handle, target.open("w", encoding="utf-8") as target_handle:
        for line in source_handle:
            record = sanitize_graph_record(json.loads(line))
            if record.get("_table") == "nodes" and (
                record.get("type") == "raw"
                or str(record.get("path", "")).startswith("academic/wiki/papers/")
            ):
                match = re.search(r"/(D\d{3})-", record.get("path", ""))
                if match and match.group(1) in canonical_titles:
                    record["title"] = canonical_titles[match.group(1)]
            target_handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            table = record.get("_table")
            if table in tables:
                tables[table].append(record)
    for table, rows in tables.items():
        write_csv(output / f"graph/{table}.csv", list(GRAPH_FIELDS[table]), rows)
    return {table: len(rows) for table, rows in tables.items()}


def load_step_reports(run: Path) -> dict[int, dict]:
    reports = {}
    for path in sorted((run / "logs/fusion").glob("G*-report.json")):
        report = load_json(path)
        reports[int(report["step"])] = report
    return reports


def snapshot_hub_members(snapshot: Path, hub_paths: set[str]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = defaultdict(set)
    with snapshot.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if (
                record.get("_table") == "edges"
                and record.get("predicate") == "聚类于"
                and record.get("object") in hub_paths
            ):
                members[record["object"]].add(record["subject"])
    return members


def macro_domain(hub_path: str) -> str:
    if not hub_path:
        return "unmapped"
    return MACRO_DOMAIN.get(hub_path.rsplit("/", 1)[-1], "many-body")


def build_derived_tables(
    experiment: Path,
    run: Path,
    manuscript_metrics: Path,
    canonical_titles: dict[str, str],
    output: Path,
) -> dict:
    trajectory = read_csv(experiment / "metrics/trajectory.csv")
    trajectory_by_step = {int(row["step"]): row for row in trajectory}
    trajectory_by_work = {row["work_id"]: row for row in trajectory}
    lineage = load_json(experiment / "metrics/hub-lineage.json")
    navigation = load_json(manuscript_metrics)
    reports = load_step_reports(run)
    hubs = {hub["path"]: hub for hub in lineage["hubs"]}
    supports_by_node: dict[str, set[str]] = defaultdict(set)
    over_time = []
    counts_by_step_hub = {}
    for step in range(0, len(trajectory) + 1):
        if step:
            report = reports[step]
            for item in report.get("node_support", []):
                node_id = item.get("canonical_node_id")
                if node_id:
                    supports_by_node[node_id].add(report["work_id"])
        members = snapshot_hub_members(run / f"snapshots/G{step:03d}.jsonl", set(hubs))
        for hub_path, hub in hubs.items():
            if step < int(hub["birth_step"]):
                continue
            hub_members = members.get(hub_path, set())
            expected_member_count = int(hub["series"][step]["member_count"])
            if len(hub_members) != expected_member_count:
                raise ValueError(
                    f"Hub member mismatch at G{step:03d}: {hub_path} "
                    f"snapshot={len(hub_members)} lineage={expected_member_count}"
                )
            supporting = set()
            for member in hub_members:
                supporting.update(supports_by_node.get(member, set()))
            row = {
                "step": step,
                "year": trajectory_by_step[step]["publication_date"] if step else "",
                "hub_id": hub_path,
                "hub_label": hub["title"],
                "member_count": len(hub_members),
                "supporting_paper_count": len(supporting),
            }
            over_time.append(row)
            counts_by_step_hub[(step, hub_path)] = row
    write_csv(
        output / "metrics/hub_membership_over_time.csv",
        ["step", "year", "hub_id", "hub_label", "member_count", "supporting_paper_count"],
        over_time,
    )

    final_step = len(trajectory)
    hub_summary = []
    for hub in sorted(hubs.values(), key=lambda item: (int(item["birth_step"]), item["title"])):
        birth = counts_by_step_hub[(int(hub["birth_step"]), hub["path"])]
        final = counts_by_step_hub[(final_step, hub["path"])]
        expected_support = int(navigation["supporting_papers_final"][hub["path"]])
        if final["supporting_paper_count"] != expected_support:
            raise ValueError(
                f"Hub support mismatch: {hub['path']} "
                f"derived={final['supporting_paper_count']} manuscript={expected_support}"
            )
        hub_summary.append({
            "hub_id": hub["path"],
            "hub_label": hub["title"],
            "birth_step": hub["birth_step"],
            "birth_year": trajectory_by_step[int(hub["birth_step"])]["publication_date"],
            "parent_hub_id": ";".join(hub.get("parent_hubs", [])),
            "active_at_final": int(hub.get("status") == "active"),
            "supporting_papers_at_birth": birth["supporting_paper_count"],
            "supporting_papers_at_final": final["supporting_paper_count"],
            "member_count_at_birth": birth["member_count"],
            "member_count_at_final": final["member_count"],
            "macro_domain": macro_domain(hub["path"]),
        })
    write_csv(output / "metrics/hub_summary.csv", list(hub_summary[0]), hub_summary)

    paper_to_hub = []
    for mapping in navigation["paper_mapping"]:
        paper = trajectory_by_work[mapping["work_id"]]
        title = canonical_titles[f"D{int(paper['step']):03d}"]
        paper_to_hub.append({
            "paper_id": f"D{int(paper['step']):03d}",
            "work_id": mapping["work_id"],
            "year": paper["publication_date"],
            "title": title,
            "title_short": title if len(title) <= 96 else title[:93].rstrip() + "...",
            "primary_hub_id": mapping["primary"],
            "secondary_hub_id": mapping["secondary"],
            "macro_domain": macro_domain(mapping["primary"]),
            "max_overlap": mapping["max_overlap"],
        })
    write_csv(output / "metrics/paper_to_hub.csv", list(paper_to_hub[0]), paper_to_hub)
    write_csv(
        output / "metrics/period_summary.csv",
        ["period", "papers", "hub_births", "pooled_reuse", "mean_churn"],
        navigation["period_summary"],
    )
    write_json(output / "metrics/manuscript-derived-metrics.json", navigation)
    return {
        "hub_count": len(hub_summary),
        "hub_state_rows": len(over_time),
        "paper_mapping_rows": len(paper_to_hub),
        "mapped_papers": navigation["mapped_papers"],
        "unmapped_papers": navigation["unmapped_papers"],
    }


def copy_metrics_and_validation(
    experiment: Path, canonical_titles: dict[str, str], output: Path
) -> None:
    for name in (
        "trajectory.csv",
        "hub-membership-trajectory.csv",
        "hub-lineage.json",
        "density-outlier-sensitivity.csv",
        "density-outlier-sensitivity.json",
        "final-summary.json",
    ):
        source = experiment / "metrics" / name
        target = output / "metrics" / name
        if source.suffix == ".csv":
            rows = read_csv(source)
            if name == "trajectory.csv":
                for row in rows:
                    run_id = Path(row["page"]).name.split("-", 1)[0]
                    row["title"] = canonical_titles[run_id]
            if not rows:
                raise ValueError(f"empty metric table: {source}")
            write_csv(target, list(rows[0]), rows)
        else:
            write_json(target, load_json(source))
    for name in ("formal-summary.json", "formal-semantic-audit.json"):
        source = experiment / "logs" / name
        value = load_json(source)
        if name == "formal-semantic-audit.json":
            value["mechanical_audit"] = "validation/formal-summary.json"
        write_json(output / "validation" / name, value)


def build_run_metadata(experiment: Path, run: Path, manuscript_metrics: Path, output: Path) -> dict:
    run_lock_path = experiment / "config/run-lock.json"
    fusion_lock_path = experiment / "config/fusion-lock.json"
    final_graph_path = run / "snapshots/G056.jsonl"
    run_lock = load_json(run_lock_path)
    fusion_lock = load_json(fusion_lock_path)
    llm_profiles = {}
    for profile, backends in run_lock["llm"]["profiles"].items():
        llm_profiles[profile] = [
            {"name": item["name"], "model": item["model"]} for item in backends
        ]
    frozen_code_hashes = fusion_lock["code_hashes"]
    code_files = {}
    for relative, frozen_hash in sorted(frozen_code_hashes.items()):
        release_path = REPO / relative
        release_hash = sha256_file(release_path) if release_path.is_file() else None
        code_files[relative] = {
            "frozen_run_sha256": frozen_hash,
            "release_candidate_sha256": release_hash,
            "exact_match": release_hash == frozen_hash,
        }
    exact_matches = sum(item["exact_match"] for item in code_files.values())
    code_compatibility = {
        "comparison_target": f"Ran-ASKS v{ASKS_RELEASE_VERSION} release candidate",
        "files_recorded_by_frozen_run": len(code_files),
        "exact_matches": exact_matches,
        "post_run_drift": len(code_files) - exact_matches,
        "files": code_files,
    }
    metadata = {
        "schema": "ran-asks.paper-artifact.run-metadata.v1",
        "asks_release_version": ASKS_RELEASE_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "source_run_id": run.name,
        "source_finished_at": load_json(experiment / "metrics/final-summary.json")["source_finished_at"],
        "corpus_size": run_lock["corpus_size"],
        "manifest_sha256": run_lock["manifest_sha256"],
        "source_hashes": {
            "run_lock_sha256": sha256_file(run_lock_path),
            "fusion_lock_sha256": sha256_file(fusion_lock_path),
            "final_graph_jsonl_sha256": sha256_file(final_graph_path),
            "manuscript_metrics_sha256": sha256_file(manuscript_metrics),
        },
        "code_hashes": frozen_code_hashes,
        "code_compatibility": code_compatibility,
        "runtime": run_lock["runtime"],
        "llm": {
            "backend": run_lock["llm"]["backend"],
            "profiles": llm_profiles,
            "reasoning": run_lock["llm"]["reasoning"],
        },
        "embedding": {"model": fusion_lock["embedding"]["model"]},
        "thresholds": fusion_lock["thresholds"],
        "hub_gate": fusion_lock["hub_gate"],
        "isolation": {
            "clean_derived_state": True,
            "production_graph_used": False,
            "production_wiki_used": False,
        },
    }
    write_json(output / "config/run-metadata.json", metadata)
    write_json(output / "config/code-compatibility.json", code_compatibility)
    drift_rows = [
        (relative, item)
        for relative, item in code_files.items()
        if not item["exact_match"]
    ]
    lines = [
        f"# Code Provenance for Paper Artifact {ARTIFACT_VERSION}",
        "",
        f"This frozen data artifact is bound to Ran-ASKS `v{ASKS_RELEASE_VERSION}` and source run",
        f"`{run.name}`. The run recorded SHA-256 hashes for {len(code_files)} implementation and",
        f"configuration files. The release candidate exactly matches {exact_matches} of those files.",
        "",
        "The remaining difference is disclosed below. It arose after the frozen run; the released",
        "data, metrics, graph, and Wiki pages were not regenerated with the later file. The exact",
        "frozen-run file content is not distributed, so the release supports inspection and partial",
        "re-execution rather than a claim of byte-identical end-to-end regeneration.",
        "",
        "| File | Frozen-run SHA-256 | Release SHA-256 |",
        "| --- | --- | --- |",
    ]
    for relative, item in drift_rows:
        release_hash = item["release_candidate_sha256"] or "not present"
        lines.append(
            f"| `{relative}` | `{item['frozen_run_sha256']}` | `{release_hash}` |"
        )
    lines.extend(
        [
            "",
            "The complete per-file comparison is available in",
            "`config/code-compatibility.json`; the original frozen hashes remain in",
            "`config/run-metadata.json`.",
            "",
        ]
    )
    (output / "CODE_PROVENANCE.md").write_text("\n".join(lines), encoding="utf-8")
    return metadata


def write_checksums(output: Path) -> int:
    files = sorted(
        path for path in output.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    )
    lines = [f"{sha256_file(path)}  {path.relative_to(output).as_posix()}" for path in files]
    (output / "CHECKSUMS.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(files)


def build(experiment: Path, run: Path, manuscript_metrics: Path, output: Path) -> None:
    prepare_generated_dirs(output)
    canonical_titles = load_wiki_titles(run)
    corpus, manifest_hash = build_corpus(experiment, canonical_titles, output)
    wiki_counts = build_wiki(run, output)
    graph_counts = build_graph(run, canonical_titles, output)
    copy_metrics_and_validation(experiment, canonical_titles, output)
    derived_counts = build_derived_tables(
        experiment, run, manuscript_metrics, canonical_titles, output
    )
    run_metadata = build_run_metadata(experiment, run, manuscript_metrics, output)
    decisions: dict[str, int] = defaultdict(int)
    for entry in corpus:
        decisions[entry["decision"]] += 1
    metadata = {
        "schema": "ran-asks.paper-artifact.v1",
        "artifact_id": "ran-asks-arxiv-initial-e1-56",
        "artifact_version": ARTIFACT_VERSION,
        "asks_release_version": ASKS_RELEASE_VERSION,
        "paper_title": "LLMs Interpret, Embeddings Organize, Graphs Emerge: "
        "Agent-Driven Compilation of Scientific Knowledge",
        "source_run_id": run.name,
        "source_finished_at": run_metadata["source_finished_at"],
        "code_compatibility": {
            "files_recorded_by_frozen_run": run_metadata["code_compatibility"][
                "files_recorded_by_frozen_run"
            ],
            "exact_matches": run_metadata["code_compatibility"]["exact_matches"],
            "post_run_drift": run_metadata["code_compatibility"]["post_run_drift"],
            "details": "config/code-compatibility.json",
        },
        "corpus_manifest_sha256": manifest_hash,
        "corpus_entries": len(corpus),
        "corpus_decisions": dict(sorted(decisions.items())),
        "wiki": wiki_counts,
        "graph": graph_counts,
        "derived_tables": derived_counts,
        "raw_distributed": False,
    }
    write_json(output / "metadata.json", metadata)
    file_count = write_checksums(output)
    print(
        f"Built paper artifact v{ARTIFACT_VERSION} for Ran-ASKS "
        f"v{ASKS_RELEASE_VERSION}: {file_count + 1} files, "
        f"{wiki_counts['papers']} paper Wikis, {wiki_counts['hubs']} Hub Wikis, "
        f"{graph_counts['nodes']} nodes, {graph_counts['edges']} edges"
    )


def verify_checksums(output: Path, failures: list[str]) -> None:
    checksum_path = output / "CHECKSUMS.sha256"
    if not checksum_path.is_file():
        failures.append("missing CHECKSUMS.sha256")
        return
    expected = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
            failures.append(f"invalid checksum line: {line}")
            continue
        expected[relative] = digest
    actual = {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in output.rglob("*")
        if path.is_file() and path.name != "CHECKSUMS.sha256"
    }
    for relative in sorted(expected.keys() - actual.keys()):
        failures.append(f"missing checksummed file: {relative}")
    for relative in sorted(actual.keys() - expected.keys()):
        failures.append(f"unchecksummed file: {relative}")
    for relative in sorted(expected.keys() & actual.keys()):
        if expected[relative] != actual[relative]:
            failures.append(f"checksum mismatch: {relative}")


def verify(output: Path) -> int:
    failures: list[str] = []
    for name in REQUIRED_DOCS:
        if not (output / name).is_file():
            failures.append(f"missing required document: {name}")
    verify_checksums(output, failures)
    all_files = [path for path in output.rglob("*") if path.is_file()]
    for path in all_files:
        relative = path.relative_to(output).as_posix()
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            failures.append(f"forbidden binary/source artifact: {relative}")
        if path.stat().st_size > 10_000_000:
            failures.append(f"oversized artifact file: {relative}")
        if path.suffix.lower() in TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN_CONTENT:
                if pattern.search(text):
                    failures.append(f"private or sensitive marker '{pattern.pattern}' in {relative}")
    try:
        metadata = load_json(output / "metadata.json")
        if metadata.get("artifact_version") != ARTIFACT_VERSION:
            failures.append("metadata artifact version mismatch")
        if metadata.get("asks_release_version") != ASKS_RELEASE_VERSION:
            failures.append("metadata ASKS release version mismatch")
        if metadata.get("corpus_decisions") != {"exclude": 3, "include": 56, "related": 6}:
            failures.append("unexpected corpus decision counts")
        if metadata.get("wiki") != {"hubs": 18, "papers": 56}:
            failures.append("unexpected Wiki counts")
        if metadata.get("graph") != {
            "aliases": 872, "edge_origins": 1966, "edges": 2089, "nodes": 1104
        }:
            failures.append("unexpected Graph counts")
        if metadata.get("raw_distributed") is not False:
            failures.append("raw_distributed must be false")
        if metadata.get("code_compatibility") != {
            "files_recorded_by_frozen_run": 16,
            "exact_matches": 15,
            "post_run_drift": 1,
            "details": "config/code-compatibility.json",
        }:
            failures.append("unexpected frozen-run code compatibility summary")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        failures.append(f"invalid metadata.json: {error}")
    try:
        compatibility = load_json(output / "config/code-compatibility.json")
        drift = {
            path: item
            for path, item in compatibility["files"].items()
            if not item["exact_match"]
        }
        if set(drift) != {".scripts/graph_ingest.py"}:
            failures.append(f"unexpected post-run code drift: {sorted(drift)}")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        failures.append(f"invalid code compatibility record: {error}")
    try:
        corpus = load_json(output / "corpus/manifest.json")["entries"]
        if len(corpus) != 65 or sum(row["decision"] == "include" for row in corpus) != 56:
            failures.append("corpus manifest count mismatch")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        failures.append(f"invalid corpus manifest: {error}")
    expected_rows = {
        "graph/nodes.csv": 1104,
        "graph/edges.csv": 2089,
        "graph/aliases.csv": 872,
        "graph/edge_origins.csv": 1966,
        "metrics/trajectory.csv": 56,
        "metrics/hub_summary.csv": 18,
        "metrics/paper_to_hub.csv": 56,
    }
    for relative, expected in expected_rows.items():
        try:
            actual = len(read_csv(output / relative))
            if actual != expected:
                failures.append(f"row count mismatch for {relative}: {actual} != {expected}")
        except (OSError, csv.Error) as error:
            failures.append(f"invalid CSV {relative}: {error}")
    wiki_papers = list((output / "wiki/papers").rglob("*.md"))
    wiki_hubs = list((output / "wiki/hubs").rglob("*.md"))
    if len(wiki_papers) != 56:
        failures.append(f"Wiki paper count mismatch: {len(wiki_papers)}")
    if len(wiki_hubs) != 18:
        failures.append(f"Wiki Hub count mismatch: {len(wiki_hubs)}")
    for path in wiki_papers:
        text = path.read_text(encoding="utf-8")
        locator_ids = set(re.findall(r"raw-not-distributed/([^/\s\"']+)/paper\.md", text))
        if locator_ids and locator_ids != {path.stem}:
            failures.append(
                f"Wiki locator ID mismatch for {path.relative_to(output)}: {sorted(locator_ids)}"
            )
    if failures:
        print("\n".join(f"ERROR: {failure}" for failure in failures), file=sys.stderr)
        return 1
    print(
        f"Paper artifact verified: {len(all_files)} files, 56 Wikis, 18 Hubs, "
        "1104 nodes, 2089 edges, no Raw files"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="rebuild generated data from a frozen private run")
    build_parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    build_parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    build_parser.add_argument("--manuscript-metrics", type=Path, default=DEFAULT_MANUSCRIPT_METRICS)
    build_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    verify_parser = sub.add_parser("verify", help="verify the committed sanitized artifact")
    verify_parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "build":
        build(args.experiment.resolve(), args.run.resolve(), args.manuscript_metrics.resolve(), args.output.resolve())
        return 0
    return verify(args.path.resolve())


if __name__ == "__main__":
    raise SystemExit(main())

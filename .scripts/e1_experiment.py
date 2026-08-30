#!/usr/bin/env python3
"""Experiment 1 harness: manifest, isolation, metrics, and graph snapshots.

The default commands are read-only with respect to raw/wiki/graph.db.  All
generated files live under the ForBetterScience E1 experiment workspace.
Promotion into the main knowledge base is deliberately not implemented until
the frozen-artifact and per-paper validation contracts are in place.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - repository runtime provides PyYAML
    yaml = None


REPO = Path(__file__).resolve().parent.parent
WORKS_ROOT = REPO / "academic/raw/works/papers"
DEFAULT_WORKSPACE = REPO / "projects/ForBetterScience/experiments/e1-chronological"
MANIFEST_SCHEMA = "wikigraph.e1.manifest.v1"
SNAPSHOT_SCHEMA = "wikigraph.e1.graph-snapshot.v1"
RUN_STATE_SCHEMA = "wikigraph.e1.run-state.v1"
RUN_LOCK_SCHEMA = "wikigraph.e1.run-lock.v1"
FUSION_LOCK_SCHEMA = "wikigraph.e1.fusion-lock.v1"
DECISIONS = {"include", "exclude", "related", "review"}
STEP_STATUSES = {"pending", "running", "completed", "failed", "agent_required", "interrupted"}
RUN_PHASES = ("extract", "bibliography", "wiki", "semantics", "local_validate", "fusion", "snapshot", "promotion")
LOCAL_COMPILE_PHASES = ("bibliography", "wiki", "semantics", "local_validate")
RUN_LOCK_FILES = (
    ".scripts/e1_experiment.py",
    ".scripts/extractor.py",
    ".scripts/ingest_paper.py",
    ".scripts/llm_structured.py",
    ".scripts/wiki_skeleton.py",
    ".scripts/wiki_locator.py",
    ".scripts/ingest_check.py",
    ".scripts/graph_lib.py",
    ".scripts/graph_delta.py",
    ".scripts/graph_ingest.py",
    ".scripts/graph_validate.py",
    ".scripts/hub_semantics.py",
    ".scripts/embed_helper.py",
    ".scripts/predicate-registry.json",
    ".scripts/predicate_tiers.yaml",
    "operations/config/graph-schema.yaml",
    "operations/HUB.md",
)
FUSION_LOCK_FILES = (
    ".scripts/e1_experiment.py",
    ".scripts/graph_delta.py",
    ".scripts/graph_ingest.py",
    ".scripts/graph_lib.py",
    ".scripts/graph_validate.py",
    ".scripts/hub_semantics.py",
    ".scripts/node_semantics.py",
    ".scripts/embed_helper.py",
    ".scripts/direction_matcher.py",
    ".scripts/sync_keyword_aliases.py",
    ".scripts/wiki_locator.py",
    ".scripts/predicate-registry.json",
    ".scripts/predicate_tiers.yaml",
    "operations/config/arxiv-directions.yaml",
    "operations/config/graph-schema.yaml",
    "operations/HUB.md",
)


class ContractError(RuntimeError):
    """Raised when an experiment safety or reproducibility contract fails."""


class AgentRequired(ContractError):
    """Raised when a resumable run reaches a required Agent decision gate."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(payload: object) -> str:
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _gate_hash_basis(value: object) -> object:
    """Normalize insignificant embedding-score jitter without hiding structural drift."""
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_gate_hash_basis(item) for item in value]
    if isinstance(value, dict):
        return {key: _gate_hash_basis(item) for key, item in value.items()}
    return value


def _candidate_gate_signature(candidate: dict) -> str:
    """Step-independent signature for reusing an explicit rejection unchanged."""
    return sha256_json(_gate_hash_basis(candidate))


def _relative_or_absolute(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _safe_workspace(path: Path) -> Path:
    resolved = path.resolve()
    allowed = DEFAULT_WORKSPACE.parent.resolve()
    if resolved != DEFAULT_WORKSPACE.resolve() and allowed not in resolved.parents:
        raise ContractError(f"实验 workspace 必须位于 {allowed}: {resolved}")
    protected = {
        (REPO / "academic/raw").resolve(),
        (REPO / "academic/wiki").resolve(),
        (REPO / "cross-domain").resolve(),
    }
    if resolved in protected or any(item in resolved.parents for item in protected):
        raise ContractError(f"实验 workspace 不得位于生产 raw/wiki/graph 路径: {resolved}")
    return resolved


def _load_source_yaml(entry_dir: Path) -> dict:
    source = entry_dir / "source.yaml"
    if not source.is_file() or yaml is None:
        return {}
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _canonical_pdf(entry_dir: Path, source: dict) -> tuple[Path | None, str]:
    direct = entry_dir / "paper.pdf"
    if direct.is_file():
        return direct.resolve(), "paper.pdf"
    external = str(source.get("external_path") or "").strip()
    if external:
        candidate = Path(external)
        if not candidate.is_absolute():
            candidate = REPO / candidate
        if candidate.is_file():
            return candidate.resolve(), "source.yaml.external_path"
    return None, "missing"


def _title_hint(paper_md: Path) -> str:
    with paper_md.open("r", encoding="utf-8", errors="replace") as handle:
        for _index, line in zip(range(160), handle):
            clean = re.sub(r"<[^>]+>", "", line).strip().lstrip("#").strip()
            if len(clean) >= 12 and not clean.startswith(("![]", "---", "http")):
                return " ".join(clean.split())[:500]
    return paper_md.parent.name


def _pdf_publication_evidence(pdf: Path | None) -> dict:
    result = {
        "formal_publication_signal": False,
        "doi": "",
        "published_year_hint": "",
        "venue_signals": [],
        "evidence": [],
    }
    if pdf is None:
        return result
    try:
        import fitz
        doc = fitz.open(str(pdf))
        try:
            metadata = doc.metadata or {}
            first_text = doc[0].get_text("text") if len(doc) else ""
        finally:
            doc.close()
    except Exception as exc:
        result["evidence"].append(f"pdf-open-failed:{type(exc).__name__}")
        return result

    compact = "\n".join(" ".join(line.split()) for line in first_text.splitlines() if line.strip())
    doi_match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", compact, re.I)
    if doi_match:
        result["doi"] = doi_match.group(0).rstrip(".,;)")
        result["formal_publication_signal"] = True
        result["evidence"].append("first-page-doi")

    formal_patterns = {
        "published-statement": r"\bpublished\b(?:\s+as|\s+in|\s+by|\s+online|\s*:)",
        "proceedings": r"\bproceedings of\b",
        "citation-banner": r"\bto cite this article\b",
        "epl-journal": r"\bEPL\s*,?\s*\d+\s*\((?:19|20)\d{2}\)",
        "physical-review": r"\bphys(?:ical)?\.?\s+rev(?:iew)?\.?\s+[A-Z]?\b",
        "journal-header": r"\b(?:new journal of physics|scientific reports|chinese physics [bcl]|npj quantum information|quantum science and technology)\b",
        "cn-article-number": r"文章编号.{0,80}(?:19|20)\d{2}",
    }
    for label, pattern in formal_patterns.items():
        if re.search(pattern, compact, re.I):
            result["formal_publication_signal"] = True
            result["venue_signals"].append(label)
            result["evidence"].append(f"first-page-{label}")

    year_match = re.search(
        r"\b(?:published|publication|copyright|©)\b.{0,80}?\b((?:19|20)\d{2})\b",
        compact,
        re.I,
    )
    if year_match:
        result["published_year_hint"] = year_match.group(1)
        result["evidence"].append("first-page-published-year")
    if not result["published_year_hint"]:
        creation = str(metadata.get("creationDate") or "")
        creation_match = re.search(r"D:((?:19|20)\d{2})", creation)
        if creation_match:
            result["published_year_hint"] = creation_match.group(1)
            result["evidence"].append("pdf-metadata-creation-year-weak")
    return result


def _proposal(
    entry_id: str, has_pdf: bool, formal_signal: bool, title_hint: str = ""
) -> tuple[str, str, list[str]]:
    lower = entry_id.lower()
    reasons: list[str] = []
    role = "publication_candidate"
    if "封面" in entry_id or "版权页" in entry_id:
        role = "non_paper_candidate"
        reasons.append("book-cover-or-copyright-page")
    elif (
        "supplementary" in lower
        or re.search(r"(?:^|[-_])supp(?:$|[-_])", lower)
        or re.search(r"\bsupplement(?:al|ary)\s+material\b", title_hint, re.I)
    ):
        role = "supplementary_candidate"
        reasons.append("supplementary-name-signal")
    if "子会议" in entry_id:
        reasons.append("possible-conference-version")
    if not has_pdf:
        reasons.append("canonical-pdf-missing")
    elif not formal_signal:
        reasons.append("formal-publication-evidence-not-detected")
    return ("review" if reasons else "include", role, reasons)


def inventory_entries(works_root: Path = WORKS_ROOT) -> list[dict]:
    entries: list[dict] = []
    for paper_md in sorted(works_root.glob("*/paper.md")):
        entry_dir = paper_md.parent
        source = _load_source_yaml(entry_dir)
        pdf, pdf_origin = _canonical_pdf(entry_dir, source)
        title_hint = _title_hint(paper_md)
        publication_evidence = _pdf_publication_evidence(pdf)
        decision, role, reasons = _proposal(
            entry_dir.name, pdf is not None,
            bool(publication_evidence["formal_publication_signal"]),
            title_hint,
        )
        year_match = re.match(r"((?:19|20)\d{2})", entry_dir.name)
        entries.append({
            "entry_id": entry_dir.name,
            "work_id": entry_dir.name,
            "decision": decision,
            "role": role,
            "related_to": "",
            "publication_date": year_match.group(1) if year_match else "",
            "title_hint": title_hint,
            "paper_md": _relative_or_absolute(paper_md),
            "paper_md_sha256": sha256_file(paper_md),
            "canonical_pdf": _relative_or_absolute(pdf) if pdf else "",
            "canonical_pdf_origin": pdf_origin,
            "canonical_pdf_sha256": sha256_file(pdf) if pdf else "",
            "publication_evidence": publication_evidence,
            "review_reasons": reasons,
        })
    return entries


def manifest_summary(entries: Iterable[dict]) -> dict:
    rows = list(entries)
    by_decision = {key: 0 for key in sorted(DECISIONS)}
    by_role: dict[str, int] = {}
    with_pdf = 0
    for item in rows:
        decision = str(item.get("decision") or "review")
        by_decision[decision] = by_decision.get(decision, 0) + 1
        role = str(item.get("role") or "unspecified")
        by_role[role] = by_role.get(role, 0) + 1
        with_pdf += bool(item.get("canonical_pdf"))
    return {
        "entries": len(rows),
        "with_canonical_pdf": with_pdf,
        "without_canonical_pdf": len(rows) - with_pdf,
        "by_decision": by_decision,
        "by_role": dict(sorted(by_role.items())),
    }


def build_candidate_manifest(works_root: Path = WORKS_ROOT) -> dict:
    entries = inventory_entries(works_root)
    return {
        "schema": MANIFEST_SCHEMA,
        "status": "candidate",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "works_root": _relative_or_absolute(works_root),
        "entries": entries,
        "summary": manifest_summary(entries),
    }


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def atomic_write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        temp = Path(temp_name)
        if temp.exists():
            temp.unlink()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        temp = Path(temp_name)
        if temp.exists():
            temp.unlink()


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        raise ContractError(f"manifest schema 无效: {path}")
    if not isinstance(data.get("entries"), list):
        raise ContractError("manifest.entries 必须为列表")
    return data


def apply_decisions(manifest: dict, decisions: dict) -> dict:
    if not isinstance(decisions, dict):
        raise ContractError("decisions 文件必须是 entry_id → decision object 的 JSON object")
    entries = [dict(item) for item in manifest.get("entries") or []]
    by_id = {str(item.get("entry_id") or ""): item for item in entries}
    unknown = sorted(set(decisions) - set(by_id))
    if unknown:
        raise ContractError(f"decisions 含未知 entry_id: {', '.join(unknown)}")
    for entry_id, override in decisions.items():
        if not isinstance(override, dict):
            raise ContractError(f"{entry_id}: decision override 必须为 object")
        allowed = {
            "decision", "work_id", "related_to", "adjudication_note",
            "publication_date", "publication_date_evidence",
        }
        extra = sorted(set(override) - allowed)
        if extra:
            raise ContractError(f"{entry_id}: 未允许字段 {', '.join(extra)}")
        by_id[entry_id].update(override)
    result = dict(manifest)
    result["status"] = "reviewed" if not any(
        item.get("decision") == "review" for item in entries
    ) else "partially_reviewed"
    result["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    result["entries"] = entries
    result["summary"] = manifest_summary(entries)
    return result


def validate_manifest(manifest: dict, expected_publications: int | None = None) -> list[str]:
    errors: list[str] = []
    entries = manifest.get("entries") or []
    seen_entries: set[str] = set()
    included_work_ids: set[str] = set()
    known_ids = {str(item.get("entry_id") or "") for item in entries}
    for index, item in enumerate(entries, 1):
        entry_id = str(item.get("entry_id") or "")
        decision = str(item.get("decision") or "")
        work_id = str(item.get("work_id") or "")
        if not entry_id or entry_id in seen_entries:
            errors.append(f"entry[{index}] entry_id 缺失或重复: {entry_id!r}")
        seen_entries.add(entry_id)
        if decision not in DECISIONS:
            errors.append(f"{entry_id}: decision 非法: {decision!r}")
        if decision == "review":
            errors.append(f"{entry_id}: 尚未完成人工裁决")
        if decision == "include":
            if not work_id:
                errors.append(f"{entry_id}: include 缺 work_id")
            elif work_id in included_work_ids:
                errors.append(f"{entry_id}: include work_id 重复: {work_id}")
            included_work_ids.add(work_id)
            if not item.get("canonical_pdf"):
                errors.append(f"{entry_id}: include 缺 canonical PDF")
            else:
                pdf = _artifact_path(str(item["canonical_pdf"]))
                if not pdf.is_file():
                    errors.append(f"{entry_id}: canonical PDF 不可读: {pdf}")
                elif item.get("canonical_pdf_sha256") and sha256_file(pdf) != item["canonical_pdf_sha256"]:
                    errors.append(f"{entry_id}: canonical PDF 哈希漂移")
            publication_date = str(item.get("publication_date") or "")
            if not re.fullmatch(r"(?:19|20)\d{2}(?:-\d{2}-\d{2})?", publication_date):
                errors.append(f"{entry_id}: publication_date 格式无效: {publication_date!r}")
        if decision == "related":
            target = str(item.get("related_to") or "")
            if not target or target not in known_ids:
                errors.append(f"{entry_id}: related_to 缺失或不是 manifest entry")
    if expected_publications is not None and len(included_work_ids) != expected_publications:
        errors.append(
            f"独立 publication 数为 {len(included_work_ids)}，预期 {expected_publications}"
        )
    return errors


def init_run_state(manifest_path: Path, manifest: dict) -> dict:
    if manifest.get("status") != "frozen":
        raise ContractError(f"init-run 只接受 frozen manifest，当前为 {manifest.get('status')!r}")
    errors = validate_manifest(manifest)
    if errors:
        raise ContractError("manifest 尚未冻结: " + "; ".join(errors[:8]))
    included = [item for item in manifest["entries"] if item.get("decision") == "include"]
    included.sort(key=lambda item: int(item.get("sequence_index") or 0))
    sequence = [int(item.get("sequence_index") or 0) for item in included]
    if sequence != list(range(1, len(included) + 1)):
        raise ContractError("frozen manifest sequence_index 必须是连续的 1..N")
    entry_ids = [str(item["entry_id"]) for item in included]
    steps = {
        phase: {
            entry_id: {
                "status": "pending", "attempts": 0, "updated_at": "",
                "error": "", "artifacts": [],
            }
            for entry_id in entry_ids
        }
        for phase in RUN_PHASES
    }
    return {
        "schema": RUN_STATE_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": _relative_or_absolute(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "entry_order": entry_ids,
        "steps": steps,
    }


def load_run_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("schema") != RUN_STATE_SCHEMA:
        raise ContractError(f"run state schema 无效: {path}")
    return state


def checkpoint_step(
    state_path: Path,
    state: dict,
    phase: str,
    entry_id: str,
    status: str,
    *,
    artifacts: Iterable[Path] = (),
    error: str = "",
) -> dict:
    if phase not in RUN_PHASES:
        raise ContractError(f"未知 phase: {phase}")
    if status not in STEP_STATUSES:
        raise ContractError(f"未知 status: {status}")
    try:
        step = state["steps"][phase][entry_id]
    except KeyError as exc:
        raise ContractError(f"run state 无此步骤: {phase}/{entry_id}") from exc
    if status == "running":
        step["attempts"] = int(step.get("attempts") or 0) + 1
    artifact_rows = []
    for artifact in artifacts:
        path = Path(artifact)
        if not path.is_file():
            raise ContractError(f"checkpoint 产物不存在: {path}")
        artifact_rows.append({"path": _relative_or_absolute(path), "sha256": sha256_file(path)})
    if status == "completed" and not artifact_rows:
        raise ContractError("completed checkpoint 必须至少有一个可复验产物")
    if artifact_rows:
        step["artifacts"] = artifact_rows
    step["status"] = status
    step["error"] = error
    step["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(state_path, state)
    return step


def _artifact_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO / path


def completed_step_is_valid(step: dict) -> bool:
    if step.get("status") != "completed" or not step.get("artifacts"):
        return False
    for artifact in step["artifacts"]:
        path = _artifact_path(str(artifact.get("path") or ""))
        if not path.is_file() or sha256_file(path) != artifact.get("sha256"):
            return False
    return True


def completed_phase_chain_is_valid(state: dict, phase: str, entry_id: str) -> bool:
    """A downstream checkpoint is valid only while every prerequisite remains valid."""
    if phase not in RUN_PHASES:
        raise ContractError(f"未知 phase: {phase}")
    phase_index = RUN_PHASES.index(phase)
    for dependency in RUN_PHASES[: phase_index + 1]:
        if not completed_step_is_valid(state["steps"][dependency][entry_id]):
            return False
    return True


def resume_queue(state: dict, phase: str) -> list[str]:
    if phase not in RUN_PHASES:
        raise ContractError(f"未知 phase: {phase}")
    return [
        entry_id for entry_id in state.get("entry_order", [])
        if not completed_phase_chain_is_valid(state, phase, entry_id)
    ]


def highest_contiguous_completed(state: dict, phase: str) -> int:
    completed = 0
    for entry_id in state.get("entry_order", []):
        if not completed_phase_chain_is_valid(state, phase, entry_id):
            break
        completed += 1
    return completed


def _verified_state_manifest(state: dict) -> tuple[Path, dict]:
    manifest_path = _artifact_path(str(state.get("manifest") or ""))
    if not manifest_path.is_file():
        raise ContractError(f"run state manifest 不存在: {manifest_path}")
    if sha256_file(manifest_path) != state.get("manifest_sha256"):
        raise ContractError("frozen manifest 哈希与 run state 不一致")
    manifest = load_manifest(manifest_path)
    if manifest.get("status") != "frozen":
        raise ContractError("run state manifest 不再是 frozen 状态")
    return manifest_path, manifest


def local_artifact_id(entry: dict) -> str:
    sequence = int(entry.get("sequence_index") or 0)
    if sequence <= 0:
        raise ContractError(f"{entry.get('entry_id')}: frozen manifest 缺 sequence_index")
    safe_id = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", str(entry["entry_id"])).strip("-")
    return f"D{sequence:03d}-{safe_id}"


def _local_artifact_dir(
    workspace: Path,
    entry: dict,
    artifact_root: Path | None = None,
) -> Path:
    root = (artifact_root or (workspace / "local-artifacts")).resolve()
    artifact_dir = root / local_artifact_id(entry)
    if workspace != artifact_dir.resolve() and workspace not in artifact_dir.resolve().parents:
        raise ContractError(f"local artifact 越出实验 workspace: {artifact_dir}")
    return artifact_dir


def _run_artifact_root(workspace: Path, state: dict) -> Path:
    configured = str(state.get("artifact_root") or "").strip()
    root = _artifact_path(configured) if configured else workspace / "local-artifacts"
    root = root.resolve()
    if workspace != root and workspace not in root.parents:
        raise ContractError(f"run artifact root 越出实验 workspace: {root}")
    return root


def _prepare_formal_artifact_reference(
    workspace: Path,
    entry: dict,
    artifact_dir: Path,
) -> Path:
    source_dir = _local_artifact_dir(workspace, entry)
    required = [source_dir / "paper.md", source_dir / "paper.pdf"]
    if any(not path.is_file() for path in required):
        raise ContractError(f"formal run 缺 fresh extraction: {entry['entry_id']}")
    files = [*required]
    parse_meta = source_dir / "parse_meta.yaml"
    if parse_meta.is_file():
        files.append(parse_meta)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    target = artifact_dir / "extraction-ref.json"
    atomic_write_json(target, {
        "schema": "wikigraph.e1.extraction-ref.v1",
        "entry_id": entry["entry_id"],
        "source_artifact_dir": _relative_or_absolute(source_dir),
        "files": [
            {"path": _relative_or_absolute(path), "sha256": sha256_file(path)}
            for path in files
        ],
    })
    return target


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO).as_posix()
    except ValueError as exc:
        raise ContractError(f"当前编译组件要求实验产物位于仓库内: {path}") from exc


def _next_attempt_path(directory: Path, stem: str) -> Path:
    attempt = 1
    while True:
        candidate = directory / f"{stem}-attempt-{attempt:02d}.json"
        if not candidate.exists():
            return candidate
        attempt += 1


def _compile_bibliography(
    entry: dict,
    artifact_dir: Path,
    source_dir: Path | None = None,
) -> list[Path]:
    import ingest_paper as ip

    source_dir = source_dir or artifact_dir
    paper_md = source_dir / "paper.md"
    local_pdf = source_dir / "paper.pdf"
    if not paper_md.is_file() or not local_pdf.is_file():
        raise ContractError("bibliography 前置提取产物不完整")
    md_text = paper_md.read_text(encoding="utf-8")
    bibliography = ip.extract_pdf_bibliography(local_pdf)
    candidates = ip.build_bibliographic_candidates(bibliography, md_text)
    prompt = ip.build_bibliographic_review_prompt(candidates, md_text)
    result = ip.call_json(
        prompt,
        ip.bibliographic_review_schema,
        max_tokens=1800,
        retries=1,
        operation=ip.BIBLIOGRAPHIC_REVIEW_OPERATION,
        system="你是受程序约束的论文书目预审组件，只裁决程序候选并输出 JSON。",
    )
    call_path = artifact_dir / "bibliography-call.json"
    atomic_write_json(call_path, {
        "schema": "wikigraph.e1.llm-call.v1",
        "phase": "bibliography",
        "entry_id": entry["entry_id"],
        "prompt": prompt,
        "result": result,
    })
    if result.get("status") == "agent_required":
        raise AgentRequired("书目预审需要 agent 接管")
    if not result.get("ok"):
        raise ContractError(f"书目预审调用失败: {result.get('error', 'unknown')}")
    review = json.loads(json.dumps(result.get("parsed") or {}, ensure_ascii=False))
    normalizations = []
    author_review = ((review.get("bibliographic") or {}).get("authors") or {})
    allowed_authors = set(candidates.get("authors") or [])
    rejected = list(author_review.get("rejected") or [])
    bounded_rejected = [item for item in rejected if item in allowed_authors]
    if bounded_rejected != rejected:
        author_review["rejected"] = bounded_rejected
        normalizations.append({
            "field": "authors.rejected",
            "action": "drop_out_of_candidate_values",
            "dropped": [item for item in rejected if item not in allowed_authors],
        })
    normalizations.extend(ip.repair_bibliographic_evidence_locators(review, md_text))
    errors = ip.validate_bibliographic_review(review, candidates, md_text)
    if errors:
        raise ContractError("书目预审候选校验失败: " + "; ".join(errors))
    manifest_adjudication = str(entry.get("adjudication_note") or "").strip()
    if review.get("doc_type") != "paper" or (
        review.get("review_status") in {"ambiguous", "manual_required"}
        and not manifest_adjudication
    ):
        raise AgentRequired(
            f"书目预审无法锁定: doc_type={review.get('doc_type')}, "
            f"review_status={review.get('review_status')}"
        )
    if review.get("review_status") in {"ambiguous", "manual_required"}:
        normalizations.append({
            "field": "review_status",
            "action": "apply_frozen_manifest_adjudication",
            "from": review.get("review_status"),
            "adjudication_note": manifest_adjudication,
        })
    merged = ip.merge_bibliographic_review(bibliography, review)
    required = [name for name in ("title", "authors", "year") if not merged.get(name)]
    if required:
        raise ContractError("锁定书目缺字段: " + ", ".join(required))
    manifest_year = str(entry.get("publication_date") or "")[:4]
    if str(merged.get("year") or "") != manifest_year:
        date_evidence = str(entry.get("publication_date_evidence") or "").strip()
        if manifest_adjudication and date_evidence:
            normalizations.append({
                "field": "year",
                "action": "apply_frozen_manifest_adjudication",
                "from": str(merged.get("year") or ""),
                "to": manifest_year,
                "publication_date_evidence": date_evidence,
                "adjudication_note": manifest_adjudication,
            })
            merged["year"] = manifest_year
        else:
            raise AgentRequired(
                f"锁定书目年份 {merged.get('year')!r} 与 manifest 正式发表年份 {manifest_year!r} 不一致"
            )
    bibliography_path = artifact_dir / "bibliography.json"
    atomic_write_json(bibliography_path, {
        "schema": "wikigraph.e1.bibliography.v1",
        "entry_id": entry["entry_id"],
        "work_id": entry["work_id"],
        "manifest_publication_date": entry.get("publication_date"),
        "bibliographic": merged,
        "review": review,
        "candidates": candidates,
        "normalizations": normalizations,
    })
    return [bibliography_path, call_path]


def _validate_local_wiki(wiki_path: Path, paper_md: Path) -> dict:
    import ingest_paper as ip

    command = [sys.executable, str(REPO / ".scripts/ingest_check.py"), _repo_relative(wiki_path)]
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    errors = [] if result.returncode == 0 else ip.parse_check_errors(result.stdout + result.stderr)
    # Production validators intentionally require paths under raw/.  Phase L uses a
    # physically isolated source package, so replace only that path-class error
    # with an equally strict experiment-local path + line-number check below.
    errors = [item for item in errors if "不是精确 Raw locator" not in item]
    source = _repo_relative(paper_md)
    locator_errors = ip.wl.validate_wiki_page(
        wiki_path,
        require_citations=True,
        raw_overrides={source: paper_md},
    )
    errors.extend(item for item in locator_errors if "不是精确 Raw locator" not in item)
    wiki_text = wiki_path.read_text(encoding="utf-8")
    source_lines = paper_md.read_text(encoding="utf-8").splitlines()
    definitions = {
        match.group(1): (match.group(2).strip(), int(match.group(3)))
        for match in re.finditer(r'^\[\^([^\]]+)\]:\s+(.+?paper\.md)#L(\d+)\s*$', wiki_text, re.M)
    }
    used = set(re.findall(r'\[\^([^\]]+)\]', re.sub(r'^\[\^[^\]]+\]:.*$', '', wiki_text, flags=re.M)))
    for ref in sorted(used):
        if ref not in definitions:
            errors.append(f"脚注 [^{ref}] 缺定义")
    for ref, (target, line_number) in definitions.items():
        if target != source:
            errors.append(f"脚注 [^{ref}] 越出本地 source package: {target}")
        if line_number < 1 or line_number > len(source_lines):
            errors.append(f"脚注 [^{ref}] 行号越界: L{line_number}")
    direction = ip.wl.get_wiki_section(wiki_path, "研究方向定位")
    if direction is None:
        errors.append("缺少 ## 研究方向定位")
    elif not re.search(r'\[\^[^\]]+\]', direction.text):
        errors.append("研究方向定位没有精确 source locator 脚注")
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "errors": list(dict.fromkeys(errors)),
    }


def _compile_wiki(
    entry: dict,
    artifact_dir: Path,
    source_dir: Path | None = None,
) -> list[Path]:
    import ingest_paper as ip

    source_dir = source_dir or artifact_dir
    paper_md = source_dir / "paper.md"
    bibliography_path = artifact_dir / "bibliography.json"
    bibliography = json.loads(bibliography_path.read_text(encoding="utf-8"))["bibliographic"]
    artifact_id = local_artifact_id(entry)
    source = _repo_relative(paper_md)
    skeleton_path = artifact_dir / "skeleton.md"
    command = [
        sys.executable, str(REPO / ".scripts/wiki_skeleton.py"),
        "--page", f"academic/wiki/papers/{artifact_id}",
        "--raw", source,
        "--source", source,
        "--output", _repo_relative(skeleton_path),
    ]
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0 or not skeleton_path.is_file():
        raise ContractError(f"wiki skeleton 失败: {result.stderr.strip()}")
    skeleton = ip.apply_bibliographic_frontmatter(
        skeleton_path.read_text(encoding="utf-8"), bibliography)
    atomic_write_text(skeleton_path, skeleton)
    md_text = paper_md.read_text(encoding="utf-8")
    wiki_path = artifact_dir / "wiki.md"
    attempt_artifacts: list[Path] = []
    retry_errors: list[str] | None = None
    for _attempt in range(1, 4):
        prompt = ip.build_wiki_prompt(md_text, skeleton, retry_errors, paper_md)
        llm_result = ip.call_text(
            prompt,
            max_tokens=32768,
            retries=1,
            operation="ingest_wiki_write",
            system="你是受程序约束的知识库摄入组件，基于论文定向摘要撰写 wiki 页面。",
        )
        call_path = _next_attempt_path(artifact_dir, "wiki-call")
        atomic_write_json(call_path, {
            "schema": "wikigraph.e1.llm-call.v1",
            "phase": "wiki",
            "entry_id": entry["entry_id"],
            "prompt": prompt,
            "retry_errors": retry_errors or [],
            "result": llm_result,
        })
        attempt_artifacts.append(call_path)
        if llm_result.get("status") == "agent_required":
            raise AgentRequired("Wiki 编译需要 agent 接管")
        if not llm_result.get("ok"):
            raise ContractError(f"Wiki LLM 调用失败: {llm_result.get('error', 'unknown')}")
        text_value = str(llm_result.get("text") or "")
        meta = ip.parse_meta_block(text_value)
        if meta:
            mismatches = ip.validate_meta(meta, {"doc_type": "paper", "year": str(bibliography.get("year") or "")})
            if ip.has_type_mismatch(mismatches) or ip.has_year_mismatch(mismatches):
                raise AgentRequired("Wiki META 与锁定书目不一致: " + "; ".join(mismatches))
        wiki_content = ip.parse_delimited(text_value, ip.WIKI_DELIMITER)
        if not wiki_content:
            wiki_content = ip.salvage_wiki_without_delimiter(text_value)
        if not wiki_content:
            retry_errors = ["输出缺少有效 <<<WIKI>>> 段"]
            continue
        wiki_content = ip.apply_bibliographic_frontmatter(wiki_content, bibliography)
        wiki_content = re.sub(
            r'(sources:\s*\n\s*-\s*)(?:path:\s*)?"?[^\n]+"?',
            f'\\1"{source}"', wiki_content, count=1,
        )
        atomic_write_text(wiki_path, wiki_content.rstrip() + "\n")
        validation = _validate_local_wiki(wiki_path, paper_md)
        validation_path = _next_attempt_path(artifact_dir, "wiki-validation")
        atomic_write_json(validation_path, validation)
        attempt_artifacts.append(validation_path)
        if not validation["errors"]:
            return [skeleton_path, wiki_path, *attempt_artifacts]
        retry_errors = validation["errors"]
    raise ContractError("Wiki 结构/证据校验失败: " + "; ".join((retry_errors or [])[:8]))


def _validate_local_semantics(semantic_path: Path, wiki_path: Path) -> tuple[dict, dict]:
    import graph_ingest as gi
    import ingest_paper as ip

    sem_text = semantic_path.read_text(encoding="utf-8")
    errors = list(gi.detect_inline_section_headers(sem_text))
    warnings: list[dict] = []
    try:
        triples, keywords, main_direction, corresponding, cross_directions, direction_predicates = \
            gi.parse_semantic_text(sem_text, _repo_relative(wiki_path))
    except Exception as exc:
        return {"errors": [*errors, f"语义槽解析失败: {exc}"], "warnings": []}, {}
    allowed = set(ip.SEMANTIC_PREDICATES)
    registry = REPO / ".scripts/predicate-registry.json"
    try:
        allowed.update(json.loads(registry.read_text(encoding="utf-8")).get("formal", []))
    except (OSError, json.JSONDecodeError):
        pass
    seen = set()
    candidates = []
    for triple in triples:
        key = (triple.get("subject", ""), triple.get("predicate", ""), triple.get("object", ""))
        if key in seen:
            warnings.append({"issue": "duplicate_line", "triple": key})
        seen.add(key)
        predicate = str(triple.get("predicate") or "")
        if predicate and predicate not in allowed:
            if ip.is_valid_predicate_candidate(predicate):
                candidates.append(triple)
            else:
                errors.append(f"谓词格式不合法: {predicate}")
        subject = str(triple.get("subject") or "")
        obj = str(triple.get("object") or "")
        if subject and gi.is_descriptive_phrase(subject):
            warnings.append({"issue": "descriptive_subject", "triple": key})
        if obj and gi.is_descriptive_phrase(obj):
            warnings.append({"issue": "descriptive_object", "triple": key})
        if gi.is_bare_abbreviation(subject) or gi.is_bare_abbreviation(obj):
            warnings.append({"issue": "bare_abbreviation", "triple": key})
    local_units = {
        "schema": "wikigraph.e1.local-units.v1",
        "page_path": _repo_relative(wiki_path),
        "triples": triples,
        "keywords": keywords,
        "main_direction": main_direction,
        "corresponding": sorted(corresponding),
        "cross_directions": cross_directions,
        "direction_predicates": direction_predicates,
    }
    report = {
        "errors": list(dict.fromkeys(errors)),
        "warnings": warnings,
        "predicate_candidates": candidates,
        "triple_count": len(triples),
        "keyword_count": len(keywords),
    }
    return report, local_units


def _compile_semantics(
    entry: dict,
    artifact_dir: Path,
    _source_dir: Path | None = None,
) -> list[Path]:
    import ingest_paper as ip

    wiki_path = artifact_dir / "wiki.md"
    wiki_content = wiki_path.read_text(encoding="utf-8")
    prompt = ip.build_slots_prompt(wiki_content)
    call_path = artifact_dir / "semantics-call.json"
    result = None
    reused_prior_call = False
    call_artifacts: list[Path] = []
    if call_path.is_file():
        try:
            prior = json.loads(call_path.read_text(encoding="utf-8"))
            if prior.get("entry_id") == entry["entry_id"] and (prior.get("result") or {}).get("ok"):
                prior_result = prior["result"]
                prior_text = str(prior_result.get("text") or "")
                if (ip.parse_delimited(prior_text, ip.SLOTS_DELIMITER)
                        or ip.salvage_slots_without_delimiter(prior_text)):
                    result = prior_result
                    reused_prior_call = True
                    call_artifacts.append(call_path)
        except (OSError, json.JSONDecodeError):
            pass
    slots = ""
    delimiter_salvaged = False
    for _attempt in range(1, 4):
        if result is None:
            result = ip.call_text(
                prompt,
                max_tokens=32768,
                retries=1,
                operation="ingest_wiki_write",
                system="你是受程序约束的知识库摄入组件，基于 wiki 页面抽取语义槽。",
            )
            call_path = _next_attempt_path(artifact_dir, "semantics-call")
            atomic_write_json(call_path, {
                "schema": "wikigraph.e1.llm-call.v1",
                "phase": "semantics",
                "entry_id": entry["entry_id"],
                "prompt": prompt,
                "result": result,
            })
            call_artifacts.append(call_path)
        if result.get("status") == "agent_required":
            raise AgentRequired("语义槽编译需要 agent 接管")
        if not result.get("ok"):
            raise ContractError(f"语义槽 LLM 调用失败: {result.get('error', 'unknown')}")
        result_text = str(result.get("text") or "")
        slots = ip.parse_delimited(result_text, ip.SLOTS_DELIMITER)
        if not slots:
            slots = ip.salvage_slots_without_delimiter(result_text)
            delimiter_salvaged = bool(slots)
        if slots:
            break
        result = None
    if not slots:
        raise ContractError("语义槽输出缺少可恢复的 <<<SLOTS>>> 段")
    semantic_path = artifact_dir / "semantic.txt"
    atomic_write_text(semantic_path, ip.normalize_slots(slots))
    validation, _units = _validate_local_semantics(semantic_path, wiki_path)
    validation["delimiter_salvaged"] = delimiter_salvaged
    validation["reused_prior_call"] = reused_prior_call
    validation_path = artifact_dir / "semantics-validation.json"
    atomic_write_json(validation_path, validation)
    if validation["errors"]:
        raise ContractError("语义槽结构校验失败: " + "; ".join(validation["errors"][:8]))
    return [semantic_path, *call_artifacts, validation_path]


def _compile_local_validation(
    entry: dict,
    artifact_dir: Path,
    source_dir: Path | None = None,
) -> list[Path]:
    wiki_path = artifact_dir / "wiki.md"
    source_dir = source_dir or artifact_dir
    paper_md = source_dir / "paper.md"
    semantic_path = artifact_dir / "semantic.txt"
    wiki_validation = _validate_local_wiki(wiki_path, paper_md)
    semantic_validation, local_units = _validate_local_semantics(semantic_path, wiki_path)
    semantic_stage_path = artifact_dir / "semantics-validation.json"
    if semantic_stage_path.is_file():
        try:
            semantic_stage = json.loads(semantic_stage_path.read_text(encoding="utf-8"))
            semantic_validation["compiler_normalizations"] = {
                "delimiter_salvaged": bool(semantic_stage.get("delimiter_salvaged")),
                "reused_prior_call": bool(semantic_stage.get("reused_prior_call")),
            }
        except (OSError, json.JSONDecodeError):
            semantic_validation["compiler_normalizations"] = {"unreadable": True}
    errors = [*wiki_validation["errors"], *semantic_validation["errors"]]
    report_path = artifact_dir / "local-validation.json"
    units_path = artifact_dir / "local-units.json"
    local_units.update({"entry_id": entry["entry_id"], "work_id": entry["work_id"]})
    atomic_write_json(units_path, local_units)
    atomic_write_json(report_path, {
        "schema": "wikigraph.e1.local-validation.v1",
        "entry_id": entry["entry_id"],
        "valid": not errors,
        "errors": errors,
        "wiki": wiki_validation,
        "semantics": semantic_validation,
        "validated_at": datetime.now(timezone.utc).isoformat(),
    })
    if errors:
        raise ContractError("local validation 失败: " + "; ".join(errors[:8]))
    bundle_files = [
        path for path in sorted(artifact_dir.iterdir())
        if path.is_file() and path.name != "bundle.json"
    ]
    bundle_path = artifact_dir / "bundle.json"
    atomic_write_json(bundle_path, {
        "schema": "wikigraph.e1.local-bundle.v1",
        "entry_id": entry["entry_id"],
        "work_id": entry["work_id"],
        "sequence_index": entry["sequence_index"],
        "files": [
            {"path": path.name, "sha256": sha256_file(path)} for path in bundle_files
        ],
    })
    return [report_path, units_path, bundle_path]


def verify_local_bundle(artifact_dir: Path) -> list[str]:
    bundle_path = artifact_dir / "bundle.json"
    if not bundle_path.is_file():
        return ["缺 bundle.json"]
    try:
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"bundle.json 不可读: {exc}"]
    if bundle.get("schema") != "wikigraph.e1.local-bundle.v1":
        return ["bundle schema 无效"]
    errors = []
    for item in bundle.get("files") or []:
        name = str(item.get("path") or "")
        path = artifact_dir / name
        if path.parent.resolve() != artifact_dir.resolve():
            errors.append(f"bundle 路径越界: {name}")
        elif not path.is_file():
            errors.append(f"bundle 文件缺失: {name}")
        elif sha256_file(path) != item.get("sha256"):
            errors.append(f"bundle 文件哈希漂移: {name}")
    return errors


def _git_value(*arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=REPO, text=True, capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def build_run_lock_payload(manifest_path: Path, manifest: dict, workspace: Path, *, engine: str, seed: int) -> dict:
    import hub_semantics as hs
    import llm_structured as ls
    import embed_helper

    config = ls.load_env()
    operations = ("ingest_bibliographic_review", "ingest_wiki_write")
    llm_profiles = {}
    for operation in operations:
        llm_profiles[operation] = [
            {
                "name": item["name"],
                "model": item["model"],
                "api_base_sha256": hashlib.sha256(item["base"].encode("utf-8")).hexdigest()
                if item.get("base") else "",
            }
            for item in ls.api_profiles(config, operation)
        ]
    included = sorted(
        (item for item in manifest["entries"] if item.get("decision") == "include"),
        key=lambda item: int(item["sequence_index"]),
    )
    code_hashes = {}
    for relative in RUN_LOCK_FILES:
        path = REPO / relative
        if not path.is_file():
            raise ContractError(f"run-lock 输入缺失: {relative}")
        code_hashes[relative] = sha256_file(path)
    tracked_paths = [relative for relative in RUN_LOCK_FILES if (REPO / relative).exists()]
    sources = []
    for item in included:
        extraction_dir = _local_artifact_dir(workspace, item)
        extraction_files = [extraction_dir / "paper.md", extraction_dir / "paper.pdf"]
        parse_meta = extraction_dir / "parse_meta.yaml"
        if parse_meta.is_file():
            extraction_files.append(parse_meta)
        missing = [path for path in extraction_files[:2] if not path.is_file()]
        if missing:
            raise ContractError(
                f"run-lock 缺 fresh extraction: {item['entry_id']} "
                + ", ".join(str(path) for path in missing)
            )
        sources.append({
            "sequence_index": item["sequence_index"],
            "entry_id": item["entry_id"],
            "work_id": item["work_id"],
            "canonical_pdf_sha256": item["canonical_pdf_sha256"],
            "extraction_artifacts": [
                {"path": _relative_or_absolute(path), "sha256": sha256_file(path)}
                for path in extraction_files
            ],
        })
    return {
        "schema": RUN_LOCK_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "manifest": _relative_or_absolute(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "corpus_size": len(included),
        "sources": sources,
        "code_hashes": code_hashes,
        "git": {
            "commit": _git_value("rev-parse", "HEAD"),
            "status": _git_value("status", "--short", "--", *tracked_paths),
            "diff_stat": _git_value("diff", "--stat", "--", *tracked_paths),
        },
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "extractor_engine": engine,
            "random_seed": seed,
        },
        "llm": {
            "backend": ls.ingest_mode(config),
            "profiles": llm_profiles,
            "reasoning": {
                operation: ls.reasoning_profile(config, operation)
                for operation in operations
            },
        },
        "embedding": {
            "model": str(embed_helper._MODEL),
            "api_base_sha256": hashlib.sha256(str(embed_helper._API_BASE).encode("utf-8")).hexdigest()
            if embed_helper._API_BASE else "",
            "cache": _relative_or_absolute(workspace / "state/embeddings.db"),
        },
        "hub_thresholds": {
            name: getattr(hs, name)
            for name in (
                "MEMBERSHIP_ENTER", "MEMBERSHIP_RETAIN", "MEMBERSHIP_MAX_HUBS",
                "NEW_HUB_MIN_MEMBERS", "NEW_HUB_SIMILARITY", "AUTO_CREATE_COHESION",
                "AUTO_CREATE_MIN_MEMBERS", "MERGE_CANDIDATE_SIMILARITY",
                "SPLIT_DISTINCTION_THRESHOLD", "HUB_MEMBER_LIMIT",
            )
        },
        "isolation": {
            "workspace": _relative_or_absolute(workspace),
            "graph_db": _relative_or_absolute(workspace / "state/graph-working.db"),
            "hub_page_root": _relative_or_absolute(workspace / "derived"),
            "production_graph_forbidden": _relative_or_absolute(REPO / "cross-domain/graph.db"),
        },
    }


def validate_run_lock(path: Path, manifest_path: Path) -> list[str]:
    if not path.is_file():
        return [f"run-lock 不存在: {path}"]
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"run-lock 不可读: {exc}"]
    errors = []
    if lock.get("schema") != RUN_LOCK_SCHEMA:
        errors.append("run-lock schema 无效")
    if lock.get("manifest_sha256") != sha256_file(manifest_path):
        errors.append("run-lock manifest 哈希漂移")
    for relative, expected in (lock.get("code_hashes") or {}).items():
        file_path = REPO / relative
        if not file_path.is_file():
            errors.append(f"run-lock 代码输入缺失: {relative}")
        elif sha256_file(file_path) != expected:
            errors.append(f"run-lock 代码哈希漂移: {relative}")
    for source in lock.get("sources") or []:
        for artifact in source.get("extraction_artifacts") or []:
            artifact_path = _artifact_path(str(artifact.get("path") or ""))
            if not artifact_path.is_file():
                errors.append(f"run-lock extraction 缺失: {artifact_path}")
            elif sha256_file(artifact_path) != artifact.get("sha256"):
                errors.append(f"run-lock extraction 哈希漂移: {artifact_path}")
    return errors


def _formal_run_root(workspace: Path, state: dict) -> Path:
    run_lock_sha256 = str(state.get("run_lock_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", run_lock_sha256):
        raise ContractError("formal state 缺有效 run_lock_sha256")
    root = (workspace / "runs" / run_lock_sha256[:12]).resolve()
    expected_artifacts = root / "local-artifacts"
    if _run_artifact_root(workspace, state) != expected_artifacts:
        raise ContractError(
            "formal artifact root 必须由 Phase-L run-lock 派生且位于 run-specific root: "
            f"{expected_artifacts}"
        )
    return root


def _fusion_layout(workspace: Path, state: dict, *, shakedown: bool) -> dict[str, Path | str]:
    if shakedown:
        runtime_root = workspace
        return {
            "mode": "shakedown",
            "runtime_root": runtime_root,
            "db": runtime_root / "state/shakedown-graph.db",
            "state": runtime_root / "state/shakedown-run-state.json",
            "graph_run": runtime_root / "state/shakedown-graph-run.json",
            "snapshots": runtime_root / "snapshots/shakedown",
            "fusion_logs": runtime_root / "logs/fusion-shakedown",
        }
    if state.get("mode") != "formal":
        raise ContractError("正式 fusion 只接受 mode=formal 的 Phase-L state")
    runtime_root = _formal_run_root(workspace, state)
    return {
        "mode": "formal",
        "runtime_root": runtime_root,
        "db": runtime_root / "state/graph-working.db",
        "state": runtime_root / "state/fusion-run-state.json",
        "graph_run": runtime_root / "state/graph-run.json",
        "snapshots": runtime_root / "snapshots",
        "fusion_logs": runtime_root / "logs/fusion",
    }


def _fusion_runtime_contract(workspace: Path, state: dict, *, seed: int) -> dict:
    import embed_helper
    import graph_ingest as gi
    import hub_semantics as hs
    import node_semantics as ns

    layout = _fusion_layout(workspace, state, shakedown=False)
    runtime_root = Path(layout["runtime_root"])
    hub_names = (
        "ROUTE_FLOOR", "ROUTE_MARGIN", "SPLIT_MIN_MEMBERS", "SPLIT_ROUTE_SUCCESS",
        "SPLIT_ROUTE_MARGIN", "MEMBERSHIP_ENTER", "MEMBERSHIP_RETAIN",
        "MEMBERSHIP_MAX_HUBS", "NEW_HUB_MIN_MEMBERS", "NEW_HUB_SIMILARITY",
        "AUTO_CREATE_COHESION", "AUTO_CREATE_MIN_MEMBERS",
        "MERGE_CANDIDATE_SIMILARITY", "SPLIT_DISTINCTION_THRESHOLD",
        "HUB_MEMBER_LIMIT", "MEMBERSHIP_CHILD_BONUS", "PROFILE_MEMBER_LIMIT",
    )
    return {
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "random_seed": seed,
        },
        "embedding": {
            "model": str(embed_helper._MODEL),
            "api_base_sha256": (
                hashlib.sha256(str(embed_helper._API_BASE).encode("utf-8")).hexdigest()
                if embed_helper._API_BASE else ""
            ),
            "cache": _relative_or_absolute(runtime_root / "state/embeddings.db"),
        },
        "thresholds": {
            "identity": dict(ns.DEFAULT_IDENTITY),
            "semantic_search_floor": ns.DEFAULT_SEARCH_FLOOR,
            "semantic_direction_hub": gi.SEMANTIC_DIRECTION_HUB_THRESHOLD,
            "proposition_alignment": {"merge_above": 0.98, "relate_at_or_above": 0.9},
            "hub": {name: getattr(hs, name) for name in hub_names},
        },
        "hub_gate": {
            "schema": "wikigraph.e1.hub-gate.v1",
            "candidate_hash_float_decimals": 3,
            "allowed_decisions": ["commit_birth", "reject_birth"],
            "all_candidates_must_be_decided": True,
            "automatic_creation_forbidden": True,
        },
        "isolation": {
            "run_root": _relative_or_absolute(runtime_root),
            "graph_db": _relative_or_absolute(Path(layout["db"])),
            "fusion_state": _relative_or_absolute(Path(layout["state"])),
            "snapshots": _relative_or_absolute(Path(layout["snapshots"])),
            "fusion_logs": _relative_or_absolute(Path(layout["fusion_logs"])),
            "hub_page_root": _relative_or_absolute(runtime_root / "derived"),
            "hub_gate_config": _relative_or_absolute(runtime_root / "config/hub-gates"),
            "production_graph_forbidden": _relative_or_absolute(REPO / "cross-domain/graph.db"),
            "production_wiki_forbidden": _relative_or_absolute(REPO / "academic/wiki"),
        },
    }


def _verified_phase_l_bundles(workspace: Path, state: dict, manifest: dict) -> list[dict]:
    included = sorted(
        (item for item in manifest["entries"] if item.get("decision") == "include"),
        key=lambda item: int(item["sequence_index"]),
    )
    expected_order = [str(item["entry_id"]) for item in included]
    if state.get("entry_order") != expected_order:
        raise ContractError("Phase-L state 与 frozen manifest 顺序不一致")
    artifact_root = _run_artifact_root(workspace, state)
    rows = []
    for entry in included:
        entry_id = str(entry["entry_id"])
        if not completed_phase_chain_is_valid(state, "local_validate", entry_id):
            raise ContractError(f"Phase-L bundle checkpoint 无效: {entry_id}")
        artifact_dir = _local_artifact_dir(workspace, entry, artifact_root)
        bundle_errors = verify_local_bundle(artifact_dir)
        if bundle_errors:
            raise ContractError(f"Phase-L bundle 无效 {entry_id}: {'; '.join(bundle_errors[:4])}")
        bundle_path = artifact_dir / "bundle.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        if (
            bundle.get("entry_id") != entry_id
            or bundle.get("work_id") != entry.get("work_id")
            or int(bundle.get("sequence_index") or 0) != int(entry["sequence_index"])
        ):
            raise ContractError(f"Phase-L bundle identity 与 manifest 不一致: {entry_id}")
        rows.append({
            "sequence_index": int(entry["sequence_index"]),
            "entry_id": entry_id,
            "work_id": entry["work_id"],
            "path": _relative_or_absolute(bundle_path),
            "sha256": sha256_file(bundle_path),
        })
    return rows


def build_fusion_lock_payload(
    workspace: Path,
    state_path: Path,
    *,
    audit_path: Path,
    semantic_audit_path: Path,
    seed: int,
) -> dict:
    state = load_run_state(state_path)
    if state.get("mode") != "formal":
        raise ContractError("fusion-lock 只接受已完成 Phase L 的 formal state")
    manifest_path, manifest = _verified_state_manifest(state)
    run_lock_path = _artifact_path(str(state.get("run_lock") or ""))
    run_lock_sha256 = str(state.get("run_lock_sha256") or "")
    if not run_lock_path.is_file() or sha256_file(run_lock_path) != run_lock_sha256:
        raise ContractError("Phase-L run-lock 缺失或哈希漂移")
    run_lock = json.loads(run_lock_path.read_text(encoding="utf-8"))
    if (
        run_lock.get("schema") != RUN_LOCK_SCHEMA
        or run_lock.get("manifest_sha256") != state.get("manifest_sha256")
    ):
        raise ContractError("Phase-L run-lock 与 formal state/manifest 不一致")
    reports = []
    for label, path, schema in (
        ("formal_audit", audit_path, "wikigraph.e1.local-audit.v1"),
        ("semantic_audit", semantic_audit_path, "wikigraph.e1.semantic-audit.v1"),
    ):
        if not path.is_file():
            raise ContractError(f"fusion-lock 缺 {label}: {path}")
        report = json.loads(path.read_text(encoding="utf-8"))
        if (
            report.get("schema") != schema
            or not report.get("passed")
            or int(report.get("entry_count") or 0) != len(state["entry_order"])
            or report.get("manifest_sha256") != state.get("manifest_sha256")
        ):
            raise ContractError(f"fusion-lock 的 {label} 未通过或 corpus binding 无效")
        if label == "semantic_audit" and report.get("run_lock_sha256") != run_lock_sha256:
            raise ContractError("semantic audit 未绑定当前 Phase-L run-lock")
        reports.append({
            "kind": label,
            "path": _relative_or_absolute(path),
            "sha256": sha256_file(path),
        })
    code_hashes = {}
    for relative in FUSION_LOCK_FILES:
        path = REPO / relative
        if not path.is_file():
            raise ContractError(f"fusion-lock 输入缺失: {relative}")
        code_hashes[relative] = sha256_file(path)
    runtime_contract = _fusion_runtime_contract(workspace, state, seed=seed)
    return {
        "schema": FUSION_LOCK_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "phase_l": {
            "state": _relative_or_absolute(state_path),
            "state_sha256": sha256_file(state_path),
            "run_lock": _relative_or_absolute(run_lock_path),
            "run_lock_sha256": run_lock_sha256,
            "manifest": _relative_or_absolute(manifest_path),
            "manifest_sha256": state["manifest_sha256"],
            "reports": reports,
            "bundles": _verified_phase_l_bundles(workspace, state, manifest),
        },
        "corpus_size": len(state["entry_order"]),
        "code_hashes": code_hashes,
        **runtime_contract,
    }


def validate_fusion_lock(path: Path, workspace: Path, state: dict) -> list[str]:
    if not path.is_file():
        return [f"fusion-lock 不存在: {path}"]
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"fusion-lock 不可读: {exc}"]
    errors = []
    if lock.get("schema") != FUSION_LOCK_SCHEMA:
        errors.append("fusion-lock schema 无效")
        return errors
    phase_l = lock.get("phase_l") or {}
    phase_state_path = _artifact_path(str(phase_l.get("state") or ""))
    if not phase_state_path.is_file():
        errors.append(f"fusion-lock Phase-L state 缺失: {phase_state_path}")
        return errors
    if sha256_file(phase_state_path) != phase_l.get("state_sha256"):
        errors.append("fusion-lock Phase-L state 哈希漂移")
    try:
        phase_state = load_run_state(phase_state_path)
        manifest_path, manifest = _verified_state_manifest(phase_state)
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"fusion-lock Phase-L state 无效: {exc}")
        return errors
    for field in ("run_lock_sha256", "manifest_sha256", "artifact_root"):
        if state.get(field) != phase_state.get(field):
            errors.append(f"fusion state 的 {field} 未绑定 Phase L")
    run_lock_path = _artifact_path(str(phase_l.get("run_lock") or ""))
    if not run_lock_path.is_file():
        errors.append(f"fusion-lock Phase-L run-lock 缺失: {run_lock_path}")
    elif sha256_file(run_lock_path) != phase_l.get("run_lock_sha256"):
        errors.append("fusion-lock Phase-L run-lock 哈希漂移")
    if phase_l.get("manifest_sha256") != sha256_file(manifest_path):
        errors.append("fusion-lock manifest 哈希漂移")
    for report in phase_l.get("reports") or []:
        report_path = _artifact_path(str(report.get("path") or ""))
        if not report_path.is_file():
            errors.append(f"fusion-lock audit 缺失: {report_path}")
        elif sha256_file(report_path) != report.get("sha256"):
            errors.append(f"fusion-lock audit 哈希漂移: {report_path}")
    try:
        actual_bundles = _verified_phase_l_bundles(workspace, phase_state, manifest)
        expected_bundles = phase_l.get("bundles") or []
        if actual_bundles != expected_bundles:
            errors.append("fusion-lock Phase-L bundle 集合或哈希漂移")
    except ContractError as exc:
        errors.append(str(exc))
    for relative, expected in (lock.get("code_hashes") or {}).items():
        file_path = REPO / relative
        if not file_path.is_file():
            errors.append(f"fusion-lock 代码/配置输入缺失: {relative}")
        elif sha256_file(file_path) != expected:
            errors.append(f"fusion-lock 代码/配置哈希漂移: {relative}")
    try:
        current = _fusion_runtime_contract(
            workspace, phase_state,
            seed=int((lock.get("runtime") or {}).get("random_seed") or 0),
        )
        for field in ("runtime", "embedding", "thresholds", "hub_gate", "isolation"):
            if lock.get(field) != current.get(field):
                errors.append(f"fusion-lock {field} 漂移")
    except ContractError as exc:
        errors.append(f"fusion-lock isolation 无效: {exc}")
    if int(lock.get("corpus_size") or 0) != len(phase_state["entry_order"]):
        errors.append("fusion-lock corpus_size 漂移")
    return errors


def identity_metrics(decisions: Iterable[dict]) -> dict:
    counts = {"reuse": 0, "create": 0, "abstain": 0}
    for item in decisions:
        if not item.get("eligible", False):
            continue
        action = str(item.get("action") or "")
        if action.startswith("reuse"):
            counts["reuse"] += 1
        elif action == "create_local":
            counts["create"] += 1
        elif action.startswith("abstain"):
            counts["abstain"] += 1
    denominator = counts["reuse"] + counts["create"]
    counts["reuse_fraction"] = counts["reuse"] / denominator if denominator else None
    return counts


def membership_churn(before: dict[str, Iterable[str]], after: dict[str, Iterable[str]]) -> dict:
    old_nodes = sorted(before)
    changed = 0
    transitions: list[dict] = []
    for node_id in old_nodes:
        old = set(before.get(node_id) or [])
        new = set(after.get(node_id) or [])
        if old != new:
            changed += 1
        transitions.append({
            "node_id": node_id,
            "join": sorted(new - old),
            "retain": sorted(new & old),
            "leave": sorted(old - new),
            "unassigned": not new,
            "changed": old != new,
        })
    return {
        "old_nodes": len(old_nodes),
        "changed_nodes": changed,
        "membership_churn": changed / len(old_nodes) if old_nodes else None,
        "transitions": transitions,
    }


def _configure_experiment_runtime(runtime_root: Path) -> None:
    import embed_helper
    import hub_semantics as hs

    hs.configure_page_root(runtime_root / "derived")
    embed_helper.configure_cache(runtime_root / "state/embeddings.db")


def _eligible_memberships(conn: sqlite3.Connection, node_ids: Iterable[str] | None = None) -> dict[str, list[str]]:
    parameters: list[str] = []
    where = "WHERE n.type='entity' AND n.entity_subtype IN ('keyword','proposition')"
    if node_ids is not None:
        ids = sorted(set(node_ids))
        if not ids:
            return {}
        where += " AND n.path IN (" + ",".join("?" for _ in ids) + ")"
        parameters.extend(ids)
    rows = conn.execute(
        "SELECT n.path,e.object FROM nodes n "
        "LEFT JOIN edges e ON e.subject=n.path AND e.predicate='聚类于' "
        f"{where} ORDER BY n.path,e.object",
        parameters,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(str(row[0]), [])
        if row[1]:
            result[str(row[0])].append(str(row[1]))
    return result


def _decision_classes(page: str, triples: list[dict]) -> dict[str, str]:
    import graph_ingest as gi

    classes: dict[str, str] = {}
    scaffolding = {"作者", "第一作者", "通讯作者", "发表于", "所属"}
    for triple in triples:
        predicate = str(triple.get("predicate") or "")
        if predicate in scaffolding:
            continue
        subject = str(triple.get("subject") or "")
        obj = str(triple.get("object") or "")
        if subject and subject != page:
            classes.setdefault(subject, "concept")
        if obj and obj != page:
            classes[obj] = "proposition" if predicate in gi.PROPOSITION_PREDICATES else classes.get(obj, "concept")
    return classes


def _canonical_target(conn: sqlite3.Connection, decision: dict) -> str:
    action = str(decision.get("action") or "")
    if action.startswith("reuse"):
        return str(decision.get("target") or "")
    if action != "create_local":
        return ""
    mention = str(decision.get("mention") or "")
    rows = conn.execute(
        "SELECT path FROM nodes WHERE type='entity' AND (path=? OR title=?) ORDER BY path",
        (mention, mention),
    ).fetchall()
    return str(rows[0][0]) if len(rows) == 1 else ""


def _multi_source_metrics(conn: sqlite3.Connection, support_rows: list[dict]) -> dict:
    supports: dict[str, set[str]] = {}
    for row in support_rows:
        target = str(row.get("canonical_node_id") or "")
        work_id = str(row.get("work_id") or "")
        if target and work_id:
            supports.setdefault(target, set()).add(work_id)
    eligible = [row[0] for row in conn.execute(
        "SELECT path FROM nodes WHERE type='entity' "
        "AND entity_subtype IN ('keyword','proposition') ORDER BY path"
    )]
    consolidated = [node for node in eligible if len(supports.get(str(node), set())) >= 2]
    return {
        "eligible_canonical_nodes": len(eligible),
        "multi_source_nodes": len(consolidated),
        "multi_source_consolidation_fraction": len(consolidated) / len(eligible) if eligible else None,
        "support_counts": {node: len(supports.get(str(node), set())) for node in eligible},
    }


def _graph_validation(conn: sqlite3.Connection) -> dict:
    import graph_validate

    return graph_validate.validate_graph(conn, graph_validate.load_config())


def _mirror_local_page(runtime_root: Path, entry: dict, artifact_dir: Path) -> tuple[str, str, Path]:
    artifact_id = local_artifact_id(entry)
    page = f"academic/wiki/papers/{artifact_id}"
    raw_package = f"academic/raw/references/{artifact_id}"
    virtual_source = f"{raw_package}/paper.md"
    content = (artifact_dir / "wiki.md").read_text(encoding="utf-8")
    content = re.sub(
        r'(sources:\s*\n\s*-\s*)(?:path:\s*)?"?[^\n]+"?',
        f'\\1"{virtual_source}"', content, count=1,
    )
    target = runtime_root / "derived" / f"{page}.md"
    atomic_write_text(target, content)
    return page, raw_package, target


def _load_prior_support(log_dir: Path, step: int) -> list[dict]:
    rows = []
    for path in sorted(log_dir.glob("G*-report.json")):
        match = re.fullmatch(r"G(\d+)-report\.json", path.name)
        if not match or int(match.group(1)) >= step:
            continue
        try:
            rows.extend(json.loads(path.read_text(encoding="utf-8")).get("node_support", []))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def _load_hub_birth_gate(
    runtime_root: Path,
    entry: dict,
    step: int,
    candidates: list[dict],
) -> tuple[str, Path, dict | None]:
    """Persist the exact gate input and load a complete, hash-bound decision."""
    candidate_input = {"step": step, "candidates": candidates}
    hash_basis = _gate_hash_basis(candidate_input)
    candidate_input_sha256 = sha256_json(hash_basis)
    gate_path = runtime_root / "config/hub-gates" / f"G{step:03d}.json"
    if not candidates:
        return candidate_input_sha256, gate_path, None
    request_path = runtime_root / "logs/hub-gate-inputs" / f"G{step:03d}.json"
    reusable_rejections: dict[str, dict] = {}
    gate_root = runtime_root / "config/hub-gates"
    input_root = runtime_root / "logs/hub-gate-inputs"
    for prior_gate_path in sorted(gate_root.glob("G*.json")) if gate_root.is_dir() else []:
        match = re.fullmatch(r"G(\d{3})", prior_gate_path.stem)
        if not match or int(match.group(1)) >= step:
            continue
        prior_step = int(match.group(1))
        prior_input_path = input_root / f"G{prior_step:03d}.json"
        try:
            prior_gate = json.loads(prior_gate_path.read_text(encoding="utf-8"))
            prior_input = json.loads(prior_input_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        prior_candidates = list(prior_input.get("candidates") or [])
        expected_prior_hash = sha256_json(_gate_hash_basis({
            "step": prior_step,
            "candidates": prior_candidates,
        }))
        if (
            prior_gate.get("schema") != "wikigraph.e1.hub-gate.v1"
            or prior_input.get("schema") != "wikigraph.e1.hub-gate-input.v1"
            or prior_input.get("candidate_input_sha256") != expected_prior_hash
            or prior_gate.get("candidate_input_sha256") != expected_prior_hash
        ):
            continue
        for decision in prior_gate.get("decisions") or []:
            index = int(decision.get("candidate_index") or 0)
            if decision.get("decision") != "reject_birth" or not (1 <= index <= len(prior_candidates)):
                continue
            signature = _candidate_gate_signature(prior_candidates[index - 1])
            reusable_rejections.setdefault(signature, {
                "reused_from_step": prior_step,
                "rationale": str(decision.get("rationale") or ""),
                "source_gate": _relative_or_absolute(prior_gate_path),
            })
    reuse_rows = []
    for index, candidate in enumerate(candidates, 1):
        signature = _candidate_gate_signature(candidate)
        prior = reusable_rejections.get(signature)
        if prior:
            reuse_rows.append({
                "candidate_index": index,
                "candidate_signature": signature,
                **prior,
            })
    atomic_write_json(request_path, {
        "schema": "wikigraph.e1.hub-gate-input.v1",
        "step": step,
        "entry_id": entry["entry_id"],
        "work_id": entry["work_id"],
        "candidate_input_sha256": candidate_input_sha256,
        "hash_basis": hash_basis,
        "candidates": candidates,
        "reusable_rejections": reuse_rows,
    })
    if len(reuse_rows) == len(candidates):
        decisions = [{
            "candidate_index": item["candidate_index"],
            "decision": "reject_birth",
            "rationale": item["rationale"],
            "reused_from_step": item["reused_from_step"],
            "candidate_signature": item["candidate_signature"],
        } for item in reuse_rows]
        reuse_path = runtime_root / "logs/hub-gate-reuses" / f"G{step:03d}.json"
        atomic_write_json(reuse_path, {
            "schema": "wikigraph.e1.hub-gate-reuse.v1",
            "step": step,
            "candidate_input_sha256": candidate_input_sha256,
            "decisions": decisions,
            "source_gates": sorted({item["source_gate"] for item in reuse_rows}),
        })
        return candidate_input_sha256, reuse_path, {
            "schema": "wikigraph.e1.hub-gate.v1",
            "candidate_input_sha256": candidate_input_sha256,
            "decisions": decisions,
        }
    if not gate_path.is_file():
        raise AgentRequired(f"Hub lifecycle gate required: {request_path}")
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if gate.get("schema") != "wikigraph.e1.hub-gate.v1":
        raise ContractError(f"Hub gate schema 无效: {gate_path}")
    expected_hash = str(gate.get("candidate_input_sha256") or "")
    if expected_hash != candidate_input_sha256:
        raise ContractError(
            f"Hub gate 输入哈希漂移: {gate_path}; "
            f"gate={expected_hash or '(empty)'} actual={candidate_input_sha256}; "
            f"input={request_path}"
        )
    return candidate_input_sha256, gate_path, gate


def _fuse_one(
    runtime_root: Path,
    db_path: Path,
    entry: dict,
    artifact_dir: Path,
    log_dir: Path,
    snapshot_dir: Path,
) -> tuple[Path, Path, Path]:
    import graph_delta as gd
    import graph_ingest as gi
    import graph_lib as gl
    import hub_semantics as hs

    step = int(entry["sequence_index"])
    page, raw_package, mirror_page = _mirror_local_page(runtime_root, entry, artifact_dir)
    fm = gl.read_frontmatter(mirror_page)
    units = json.loads((artifact_dir / "local-units.json").read_text(encoding="utf-8"))
    local_page = str(units.get("page_path") or "")
    triples = []
    for item in units.get("triples") or []:
        triple = dict(item)
        if triple.get("subject") == local_page:
            triple["subject"] = page
        if triple.get("object") == local_page:
            triple["object"] = page
        triple["source"] = f"{_repo_relative(artifact_dir / 'wiki.md')}#semantic"
        triples.append(triple)
    gi.fill_defaults(triples, fm)
    deterministic_count = 0
    for triple in triples:
        if triple.get("predicate") in {"作者", "第一作者", "通讯作者", "发表于"}:
            deterministic_count += 1
        else:
            break
    delta_fm = dict(fm)
    delta_fm["sources"] = [f"{raw_package}/paper.md"]
    delta = gd.build_document_delta(
        page, delta_fm, triples,
        deterministic_triple_count=deterministic_count,
    )
    if len(delta.raw_packages) != 1:
        raise ContractError(f"实验 source package 应映射为唯一 Raw node: {delta.raw_packages}")
    raw_node = delta.raw_packages[0]
    conn = gl.connect(db_path)
    try:
        old_memberships = _eligible_memberships(conn)
        old_ids = set(old_memberships)
        attach_plan = gd.plan_attachment(conn, delta)
        classes = _decision_classes(page, triples)
        for decision in attach_plan["decisions"]:
            unit_class = classes.get(str(decision.get("mention") or ""), "")
            decision["eligible"] = bool(unit_class)
            decision["unit_class"] = unit_class or "scaffolding"
        inspection = gd.inspect_delta(conn, delta, attach_plan=attach_plan)

        def writer():
            gl.ensure_node(
                conn, page, fm.get("title") or entry["entry_id"], "page",
                fm.get("source_type", "official-doc"), fm.get("date", ""),
                fm.get("status", "current"), 1,
                ingest_version=gl.CURRENT_PIPELINE_VERSION,
            )
            gl.ensure_node(
                conn, raw_node, fm.get("title") or entry["entry_id"], "raw",
                "official-doc", fm.get("date", ""), "current", 0,
                description=_repo_relative(artifact_dir / "paper.md"),
            )
            source_edge = conn.execute(
                "SELECT id FROM edges WHERE subject=? AND predicate='来源' AND object=?",
                (page, raw_node),
            ).fetchone()
            if source_edge:
                edge_id = source_edge[0]
            else:
                conn.execute(
                    "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
                    "VALUES(?, '来源', ?, '可追溯', ?, 0)",
                    (page, raw_node, _repo_relative(artifact_dir / "paper.md")),
                )
                edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            gl.add_edge_origin(conn, edge_id, page, _repo_relative(artifact_dir / "paper.md"))
            return gi.add_knowledge_edges(
                conn, page, gd.knowledge_edges(delta),
                page_source_note=_repo_relative(artifact_dir / "paper.md"),
                attach_plan=attach_plan,
            )

        ingest_result, delta_report = gd.fuse_with_savepoint(
            conn, delta, writer, inspection=inspection,
        )
        support_rows = []
        for decision in attach_plan["decisions"]:
            if not decision.get("eligible"):
                continue
            target = _canonical_target(conn, decision)
            if not target:
                continue
            support_rows.append({
                "step": step,
                "work_id": entry["work_id"],
                "local_unit_id": decision["mention"],
                "unit_class": decision["unit_class"],
                "decision": decision["action"],
                "canonical_node_id": target,
            })
        all_support = _load_prior_support(log_dir, step) + support_rows
        hub_plan = hs.dynamics_plan(conn, apply_membership=True)
        new_hubs = hub_plan.get("new_hubs") or {}
        candidates = list(new_hubs.get("candidates") or [])
        candidate_input_sha256, gate_path, gate = _load_hub_birth_gate(
            runtime_root, entry, step, candidates,
        )
        candidate_events = [
            {
                "event_id": f"G{step:03d}-birth-candidate-{index:02d}",
                "step": step,
                "event_type": "birth",
                "trigger_work_id": entry["work_id"],
                "gate_status": "pending",
                **candidate,
            }
            for index, candidate in enumerate(candidates, 1)
        ]
        committed_events = []
        if candidates:
            assert gate is not None
            definitions = []
            decisions_by_index = {}
            for decision in gate.get("decisions") or []:
                index = int(decision.get("candidate_index") or 0)
                if index < 1 or index > len(candidates) or index in decisions_by_index:
                    raise ContractError(f"Hub gate candidate_index 无效或重复: {index}")
                decisions_by_index[index] = decision
                action = decision.get("decision")
                if action == "commit_birth":
                    members = sorted(set(decision.get("members") or []))
                    if members != sorted(candidates[index - 1].get("members") or []):
                        raise ContractError(f"Hub gate members 与 candidate {index} 不一致")
                    definitions.append({
                        "title": decision.get("title"),
                        "scope": decision.get("scope"),
                        "parent": decision.get("parent", ""),
                        "members": members,
                        "_candidate_index": index,
                    })
                elif action != "reject_birth":
                    raise ContractError(f"Hub gate decision 无效: {action}")
            if set(decisions_by_index) != set(range(1, len(candidates) + 1)):
                raise ContractError("Hub gate 必须逐个裁决全部 candidates")
            create_input = [
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in definitions
            ]
            create_report = hs.create_hubs_from_definitions(conn, create_input) if create_input else {
                "created": [], "errors": [], "memberships_reapplied": 0,
            }
            if create_report.get("errors"):
                raise ContractError("Hub gate apply 失败: " + "; ".join(
                    str(item) for item in create_report["errors"]
                ))
            created_iter = iter(create_report.get("created") or [])
            for index, event in enumerate(candidate_events, 1):
                decision = decisions_by_index[index]
                event["gate_status"] = "committed" if decision["decision"] == "commit_birth" else "rejected"
                event["gate_rationale"] = decision.get("rationale", "")
                if decision.get("reused_from_step"):
                    event["gate_reused_from_step"] = int(decision["reused_from_step"])
                    event["candidate_signature"] = decision.get("candidate_signature", "")
                if decision["decision"] != "commit_birth":
                    continue
                created = next(created_iter)
                contributing = sorted({
                    row["work_id"] for row in all_support
                    if row.get("canonical_node_id") in set(event.get("members") or [])
                })
                committed_events.append({
                    "event_id": f"G{step:03d}-birth-{index:02d}",
                    "step": step,
                    "event_type": "birth",
                    "involved_hubs": [created["created"]],
                    "parent_hubs": [created["parent"]] if created.get("parent") else [],
                    "child_hubs": [],
                    "trigger_work_id": entry["work_id"],
                    "member_set_before": [],
                    "member_set_after": sorted(event.get("members") or []),
                    "contributing_work_ids": contributing,
                    "agent_gate_input_hash": candidate_input_sha256,
                    "agent_gate_output_hash": sha256_file(gate_path),
                    "title": created["title"],
                    "scope": created["scope"],
                })
            hub_plan = hs.dynamics_plan(conn, apply_membership=True)
        new_memberships = _eligible_memberships(conn, old_ids)
        churn = membership_churn(old_memberships, new_memberships)
        consolidation = _multi_source_metrics(conn, all_support)
        validation = _graph_validation(conn)
        if validation.get("errors"):
            raise ContractError("实验 graph 校验失败: " + "; ".join(
                str(item) for item in validation["errors"][:8]
            ))
        conn.commit()
        report = {
            "schema": "wikigraph.e1.fusion-step.v1",
            "step": step,
            "entry_id": entry["entry_id"],
            "work_id": entry["work_id"],
            "page": page,
            "graph_delta": delta_report,
            "ingest": ingest_result._asdict(),
            "identity": identity_metrics(attach_plan["decisions"]),
            "identity_decisions": attach_plan["decisions"],
            "node_support": support_rows,
            "consolidation": consolidation,
            "membership_churn": churn,
            "hub_dynamics": hub_plan,
            "lifecycle": {
                "candidate_input_sha256": candidate_input_sha256,
                "candidate_events": candidate_events,
                "committed_events": committed_events,
            },
            "graph_validation": validation,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    report_path = log_dir / f"G{step:03d}-report.json"
    atomic_write_json(report_path, report)
    db_snapshot = snapshot_dir / f"G{step:03d}.db"
    jsonl_snapshot = snapshot_dir / f"G{step:03d}.jsonl"
    snapshot_sqlite(db_path, db_snapshot)
    export_graph_jsonl(db_snapshot, jsonl_snapshot)
    return report_path, db_snapshot, jsonl_snapshot


def _checkpoint_fusion_snapshot(
    state_path: Path,
    state: dict,
    entry_id: str,
    fusion_artifacts: Iterable[Path],
    snapshot_artifacts: Iterable[Path],
) -> None:
    for phase, artifacts in (
        ("fusion", fusion_artifacts),
        ("snapshot", snapshot_artifacts),
    ):
        rows = []
        for artifact in artifacts:
            if not artifact.is_file():
                raise ContractError(f"{phase} checkpoint 产物不存在: {artifact}")
            rows.append({"path": _relative_or_absolute(artifact), "sha256": sha256_file(artifact)})
        step = state["steps"][phase][entry_id]
        step.update({
            "status": "completed",
            "error": "",
            "artifacts": rows,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    atomic_write_json(state_path, state)


def snapshot_sqlite(source_db: Path, destination_db: Path) -> None:
    if not source_db.is_file():
        raise ContractError(f"graph DB 不存在: {source_db}")
    destination_db.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination_db.name}.", suffix=".tmp", dir=destination_db.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as source:
            with sqlite3.connect(temp_path) as destination:
                source.backup(destination)
        os.replace(temp_path, destination_db)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def export_graph_jsonl(db_path: Path, output_path: Path) -> dict:
    tables = [
        "nodes", "aliases", "edges", "edge_evidence", "edge_origins",
        "temporal_facts", "metadata",
    ]
    counts: dict[str, int] = {}
    lines = [json.dumps({"_schema": SNAPSHOT_SCHEMA}, ensure_ascii=False)]
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for table in tables:
            if not _table_exists(conn, table):
                continue
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            order = ", ".join(f'"{column}"' for column in columns)
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
            count = 0
            for row in rows:
                lines.append(json.dumps({"_table": table, **dict(row)}, ensure_ascii=False))
                count += 1
            counts[table] = count
    atomic_write_text(output_path, "\n".join(lines) + "\n")
    return counts


def command_inventory(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    manifest = build_candidate_manifest(Path(args.works_root))
    target = workspace / "config/manifest-candidate.json"
    write_json(target, manifest)
    print(json.dumps({
        "status": "candidate_created",
        "manifest": _relative_or_absolute(target),
        "summary": manifest["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    manifest = load_manifest(Path(args.manifest))
    errors = validate_manifest(manifest, args.expected_publications)
    print(json.dumps({
        "status": "ok" if not errors else "invalid",
        "summary": manifest_summary(manifest["entries"]),
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def command_apply_decisions(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    manifest = load_manifest(Path(args.manifest))
    decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
    reviewed = apply_decisions(manifest, decisions)
    target = Path(args.output) if args.output else workspace / "config/manifest-reviewed.json"
    target_resolved = target.resolve()
    if workspace != target_resolved and workspace not in target_resolved.parents:
        raise ContractError(f"reviewed manifest 必须写入 experiment workspace: {target}")
    write_json(target, reviewed)
    print(json.dumps({
        "status": reviewed["status"],
        "manifest": _relative_or_absolute(target),
        "summary": reviewed["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


def command_freeze_manifest(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    manifest = load_manifest(Path(args.manifest))
    errors = validate_manifest(manifest, args.expected_publications)
    if errors:
        print(json.dumps({"status": "invalid", "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    entries = [dict(item) for item in manifest["entries"]]
    included = [item for item in entries if item.get("decision") == "include"]
    included.sort(key=lambda item: (
        str(item.get("publication_date") or "9999"),
        str(item.get("publication_evidence", {}).get("doi") or ""),
        str(item.get("title_hint") or ""),
        str(item.get("entry_id") or ""),
    ))
    for sequence_index, item in enumerate(included, 1):
        item["sequence_index"] = sequence_index
    frozen = dict(manifest)
    frozen["status"] = "frozen"
    frozen["frozen_at"] = datetime.now(timezone.utc).isoformat()
    frozen["corpus_size"] = len(included)
    frozen["entries"] = entries
    frozen["summary"] = manifest_summary(entries)
    target = workspace / "config/manifest-frozen.json"
    atomic_write_json(target, frozen)
    digest = sha256_file(target)
    atomic_write_json(workspace / "config/manifest-frozen.sha256.json", {
        "manifest": _relative_or_absolute(target), "sha256": digest,
    })
    print(json.dumps({
        "status": "frozen", "manifest": _relative_or_absolute(target),
        "sha256": digest, "corpus_size": len(included),
    }, ensure_ascii=False, indent=2))
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    step = int(args.step)
    db_target = workspace / "snapshots" / f"G{step:03d}.db"
    jsonl_target = workspace / "snapshots" / f"G{step:03d}.jsonl"
    snapshot_sqlite(Path(args.db), db_target)
    counts = export_graph_jsonl(db_target, jsonl_target)
    print(json.dumps({
        "status": "snapshotted",
        "db": _relative_or_absolute(db_target),
        "jsonl": _relative_or_absolute(jsonl_target),
        "tables": counts,
    }, ensure_ascii=False, indent=2))
    return 0


def command_init_run(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    state = init_run_state(manifest_path, manifest)
    target = workspace / "state/run-state.json"
    if target.exists() and not args.force:
        raise ContractError(f"run state 已存在；使用 status/resume，或显式 --force: {target}")
    atomic_write_json(target, state)
    print(json.dumps({
        "status": "initialized", "state": _relative_or_absolute(target),
        "entries": len(state["entry_order"]),
    }, ensure_ascii=False, indent=2))
    return 0


def command_init_formal_run(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    source_state_path = Path(args.state)
    source_state = load_run_state(source_state_path)
    manifest_path, manifest = _verified_state_manifest(source_state)
    run_lock_path = workspace / "config/run-lock.json"
    lock_errors = validate_run_lock(run_lock_path, manifest_path)
    if lock_errors:
        raise ContractError("formal run-lock 无效: " + "; ".join(lock_errors[:8]))
    incomplete = [
        entry_id for entry_id in source_state["entry_order"]
        if not completed_phase_chain_is_valid(source_state, "extract", entry_id)
    ]
    if incomplete:
        raise ContractError(
            f"formal run 前须完成全部 fresh extraction: {len(incomplete)} 篇未完成"
        )
    run_lock_sha256 = sha256_file(run_lock_path)
    artifact_root = workspace / "runs" / run_lock_sha256[:12] / "local-artifacts"
    state = init_run_state(manifest_path, manifest)
    for entry_id in state["entry_order"]:
        state["steps"]["extract"][entry_id] = json.loads(json.dumps(
            source_state["steps"]["extract"][entry_id], ensure_ascii=False,
        ))
    state.update({
        "mode": "formal",
        "run_lock": _relative_or_absolute(run_lock_path),
        "run_lock_sha256": run_lock_sha256,
        "artifact_root": _relative_or_absolute(artifact_root),
        "source_extraction_state": _relative_or_absolute(source_state_path),
    })
    target = Path(args.output) if args.output else workspace / "state/formal-run-state.json"
    if workspace != target.resolve() and workspace not in target.resolve().parents:
        raise ContractError(f"formal state 必须位于实验 workspace: {target}")
    if target.exists() and not args.force:
        raise ContractError(f"formal state 已存在；继续运行或显式 --force: {target}")
    atomic_write_json(target, state)
    print(json.dumps({
        "status": "initialized",
        "mode": "formal",
        "state": _relative_or_absolute(target),
        "artifact_root": _relative_or_absolute(artifact_root),
        "reused_extractions": len(state["entry_order"]),
        "run_lock_sha256": run_lock_sha256,
    }, ensure_ascii=False, indent=2))
    return 0


def command_status(args: argparse.Namespace) -> int:
    state = load_run_state(Path(args.state))
    phases = {}
    for phase in RUN_PHASES:
        rows = state["steps"][phase]
        counts = {status: 0 for status in sorted(STEP_STATUSES)}
        for step in rows.values():
            counts[str(step.get("status") or "pending")] += 1
        phases[phase] = {
            "counts": counts,
            "resume_pending": len(resume_queue(state, phase)),
            "highest_contiguous_completed": highest_contiguous_completed(state, phase),
        }
    print(json.dumps({"status": "ok", "phases": phases}, ensure_ascii=False, indent=2))
    return 0


def command_extract_local(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    state_path = Path(args.state)
    state = load_run_state(state_path)
    _manifest_path, manifest = _verified_state_manifest(state)
    included = {
        str(item["entry_id"]): item for item in manifest["entries"]
        if item.get("decision") == "include"
    }
    queue = resume_queue(state, "extract")
    if args.entry:
        if args.entry not in queue:
            print(json.dumps({"status": "already_complete", "entry_id": args.entry}, ensure_ascii=False))
            return 0
        queue = [args.entry]
    if args.limit is not None:
        queue = queue[: args.limit]
    completed = []
    failed = []
    for entry_id in queue:
        entry = included[entry_id]
        pdf = _artifact_path(str(entry.get("canonical_pdf") or ""))
        if not pdf.is_file():
            checkpoint_step(
                state_path, state, "extract", entry_id, "failed",
                error=f"canonical PDF 不可读: {pdf}",
            )
            failed.append({"entry_id": entry_id, "error": "canonical PDF 不可读"})
            if not args.keep_going:
                break
            continue
        artifact_id = local_artifact_id(entry)
        artifact_dir = workspace / "local-artifacts" / artifact_id
        artifact_dir.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_step(state_path, state, "extract", entry_id, "running")
        command = [
            sys.executable, str(REPO / ".scripts/extractor.py"),
            "--external-pdf", str(pdf),
            "--paper", artifact_id,
            "--papers-dir", str(workspace / "local-artifacts"),
        ]
        if args.engine:
            command.extend(["--engine", args.engine])
        if args.force:
            command.append("--force")
        result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
        receipt = {
            "entry_id": entry_id,
            "artifact_id": artifact_id,
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt_path = workspace / "logs/extract" / f"{artifact_id}.json"
        atomic_write_json(receipt_path, receipt)
        paper_md = artifact_dir / "paper.md"
        parse_meta = artifact_dir / "parse_meta.yaml"
        local_pdf = artifact_dir / "paper.pdf"
        if result.returncode == 0 and paper_md.is_file() and local_pdf.is_file():
            artifacts = [paper_md, local_pdf, receipt_path]
            if parse_meta.is_file():
                artifacts.append(parse_meta)
            checkpoint_step(
                state_path, state, "extract", entry_id, "completed",
                artifacts=artifacts,
            )
            completed.append(entry_id)
        else:
            message = f"extractor rc={result.returncode}; paper.md={paper_md.is_file()}"
            checkpoint_step(state_path, state, "extract", entry_id, "failed", error=message)
            failed.append({"entry_id": entry_id, "error": message, "receipt": str(receipt_path)})
            if not args.keep_going:
                break
    print(json.dumps({
        "status": "completed" if not failed else "partial_failure",
        "completed": completed,
        "failed": failed,
        "remaining": len(resume_queue(state, "extract")),
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def command_compile_local(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    if args.shakedown:
        if args.limit is None or args.limit > 5:
            raise ContractError("shakedown 必须显式 --limit，且最多 5 篇")
    state_path = Path(args.state)
    state = load_run_state(state_path)
    manifest_path, manifest = _verified_state_manifest(state)
    if not args.shakedown:
        lock_errors = validate_run_lock(workspace / "config/run-lock.json", manifest_path)
        if lock_errors:
            raise ContractError("正式 local compilation 的 run-lock 无效: " + "; ".join(lock_errors[:8]))
        if state.get("mode") != "formal":
            raise ContractError("正式 local compilation 必须使用 init-formal-run 创建的独立 state")
        if state.get("run_lock_sha256") != sha256_file(workspace / "config/run-lock.json"):
            raise ContractError("formal state 绑定的 run-lock 已漂移")
    elif state.get("mode") == "formal":
        raise ContractError("formal state 不能以 --shakedown 运行")
    artifact_root = _run_artifact_root(workspace, state)
    included = {
        str(item["entry_id"]): item for item in manifest["entries"]
        if item.get("decision") == "include"
    }
    entry_ids = [
        entry_id for entry_id in state["entry_order"]
        if not completed_phase_chain_is_valid(state, "local_validate", entry_id)
    ]
    if args.entry:
        if args.entry not in included:
            raise ContractError(f"manifest 无 include entry: {args.entry}")
        if args.entry not in entry_ids:
            print(json.dumps({"status": "already_complete", "entry_id": args.entry}, ensure_ascii=False))
            return 0
        entry_ids = [args.entry]
    if args.limit is not None:
        entry_ids = entry_ids[: args.limit]
    handlers = {
        "bibliography": _compile_bibliography,
        "wiki": _compile_wiki,
        "semantics": _compile_semantics,
        "local_validate": _compile_local_validation,
    }
    completed: list[str] = []
    failed: list[dict] = []
    for entry_id in entry_ids:
        entry = included[entry_id]
        artifact_dir = _local_artifact_dir(workspace, entry, artifact_root)
        source_dir = _local_artifact_dir(workspace, entry)
        if not completed_phase_chain_is_valid(state, "extract", entry_id):
            failed.append({"entry_id": entry_id, "phase": "extract", "error": "fresh extraction 未完成或哈希失效"})
            if not args.keep_going:
                break
            continue
        if not args.shakedown:
            _prepare_formal_artifact_reference(workspace, entry, artifact_dir)
        entry_failed = False
        for phase in LOCAL_COMPILE_PHASES:
            if completed_phase_chain_is_valid(state, phase, entry_id):
                continue
            dependency_index = RUN_PHASES.index(phase) - 1
            dependency = RUN_PHASES[dependency_index]
            if not completed_phase_chain_is_valid(state, dependency, entry_id):
                message = f"前置阶段 {dependency} 未完成或哈希失效"
                checkpoint_step(state_path, state, phase, entry_id, "failed", error=message)
                failed.append({"entry_id": entry_id, "phase": phase, "error": message})
                entry_failed = True
                break
            checkpoint_step(state_path, state, phase, entry_id, "running")
            try:
                artifacts = handlers[phase](entry, artifact_dir, source_dir)
                checkpoint_step(
                    state_path, state, phase, entry_id, "completed",
                    artifacts=artifacts,
                )
            except AgentRequired as exc:
                checkpoint_step(state_path, state, phase, entry_id, "agent_required", error=str(exc))
                failed.append({"entry_id": entry_id, "phase": phase, "status": "agent_required", "error": str(exc)})
                entry_failed = True
                break
            except Exception as exc:
                checkpoint_step(state_path, state, phase, entry_id, "failed", error=str(exc))
                failed.append({"entry_id": entry_id, "phase": phase, "status": "failed", "error": str(exc)})
                entry_failed = True
                break
        if not entry_failed and completed_phase_chain_is_valid(state, "local_validate", entry_id):
            completed.append(entry_id)
        elif entry_failed and not args.keep_going:
            break
    print(json.dumps({
        "status": "completed" if not failed else "partial_failure",
        "mode": "shakedown" if args.shakedown else "formal",
        "completed": completed,
        "failed": failed,
        "remaining": len(resume_queue(state, "local_validate")),
        "highest_contiguous_completed": highest_contiguous_completed(state, "local_validate"),
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def command_audit_local(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    state = load_run_state(Path(args.state))
    _manifest_path, manifest = _verified_state_manifest(state)
    artifact_root = _run_artifact_root(workspace, state)
    included = {
        str(item["entry_id"]): item for item in manifest["entries"]
        if item.get("decision") == "include"
    }
    entry_ids = state["entry_order"][: args.limit] if args.limit else list(state["entry_order"])
    rows = []
    for entry_id in entry_ids:
        entry = included[entry_id]
        artifact_dir = _local_artifact_dir(workspace, entry, artifact_root)
        bundle_errors = verify_local_bundle(artifact_dir)
        report_path = artifact_dir / "local-validation.json"
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
        semantic_stage_path = artifact_dir / "semantics-validation.json"
        semantic_stage = json.loads(semantic_stage_path.read_text(encoding="utf-8")) \
            if semantic_stage_path.is_file() else {}
        bibliography_path = artifact_dir / "bibliography.json"
        bibliography = json.loads(bibliography_path.read_text(encoding="utf-8")) \
            if bibliography_path.is_file() else {}
        rows.append({
            "entry_id": entry_id,
            "artifact_id": local_artifact_id(entry),
            "checkpoint_valid": completed_phase_chain_is_valid(state, "local_validate", entry_id),
            "bundle_errors": bundle_errors,
            "local_valid": bool(report.get("valid")),
            "triple_count": (report.get("semantics") or {}).get("triple_count", 0),
            "keyword_count": (report.get("semantics") or {}).get("keyword_count", 0),
            "semantic_warning_count": len((report.get("semantics") or {}).get("warnings") or []),
            "predicate_candidate_count": len((report.get("semantics") or {}).get("predicate_candidates") or []),
            "wiki_call_count": len(list(artifact_dir.glob("wiki-call*.json"))),
            "bibliography_normalization_count": len(bibliography.get("normalizations") or []),
            "delimiter_salvaged": bool(semantic_stage.get("delimiter_salvaged")),
            "reused_prior_semantics_call": bool(semantic_stage.get("reused_prior_call")),
        })
    hard_failures = [
        row["entry_id"] for row in rows
        if not row["checkpoint_valid"] or row["bundle_errors"] or not row["local_valid"]
    ]
    payload = {
        "schema": "wikigraph.e1.local-audit.v1",
        "kind": args.kind,
        "manifest_sha256": state["manifest_sha256"],
        "entry_count": len(rows),
        "hard_failures": hard_failures,
        "passed": not hard_failures,
        "totals": {
            "triples": sum(row["triple_count"] for row in rows),
            "keywords": sum(row["keyword_count"] for row in rows),
            "semantic_warnings": sum(row["semantic_warning_count"] for row in rows),
            "wiki_calls": sum(row["wiki_call_count"] for row in rows),
            "bibliography_normalizations": sum(row["bibliography_normalization_count"] for row in rows),
            "delimiter_salvages": sum(row["delimiter_salvaged"] for row in rows),
        },
        "rows": rows,
        "audited_at": datetime.now(timezone.utc).isoformat(),
    }
    output = Path(args.output) if args.output else workspace / "logs" / f"{args.kind}-summary.json"
    if workspace != output.resolve() and workspace not in output.resolve().parents:
        raise ContractError(f"audit output 必须位于实验 workspace: {output}")
    atomic_write_json(output, payload)
    print(json.dumps({**payload, "output": _relative_or_absolute(output)}, ensure_ascii=False, indent=2))
    return 0 if payload["passed"] else 2


def command_freeze_run_lock(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    if manifest.get("status") != "frozen":
        raise ContractError("run-lock 只接受 frozen manifest")
    audit_path = Path(args.audit) if args.audit else workspace / "logs/shakedown-summary.json"
    if not audit_path.is_file():
        raise ContractError(f"冻结 run-lock 前缺 shakedown audit: {audit_path}")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if not audit.get("passed") or int(audit.get("entry_count") or 0) < 5:
        raise ContractError("shakedown audit 未通过或少于 5 篇")
    source_state = load_run_state(Path(args.state))
    state_manifest_path, _state_manifest = _verified_state_manifest(source_state)
    if state_manifest_path.resolve() != manifest_path.resolve():
        raise ContractError("run-lock manifest 与 extraction state manifest 不一致")
    incomplete = [
        entry_id for entry_id in source_state["entry_order"]
        if not completed_phase_chain_is_valid(source_state, "extract", entry_id)
    ]
    if incomplete:
        raise ContractError(f"冻结 run-lock 前仍有 {len(incomplete)} 篇 fresh extraction 未完成")
    target = workspace / "config/run-lock.json"
    if target.exists() and not args.force:
        raise ContractError(f"run-lock 已存在；确认废弃旧正式运行后才可 --force: {target}")
    payload = build_run_lock_payload(
        manifest_path, manifest, workspace,
        engine=args.engine, seed=args.seed,
    )
    archived_previous_lock = ""
    if target.exists():
        previous_sha256 = sha256_file(target)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = workspace / "archive" / f"formal-run-{previous_sha256[:12]}-{stamp}"
        archive.mkdir(parents=True, exist_ok=False)
        archived_lock = archive / "config/run-lock.json"
        archived_lock.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, archived_lock)
        formal_state = workspace / "state/formal-run-state.json"
        if formal_state.exists():
            archived_state = archive / "state/formal-run-state.json"
            archived_state.parent.mkdir(parents=True, exist_ok=True)
            os.replace(formal_state, archived_state)
        archived_previous_lock = _relative_or_absolute(archive)
    atomic_write_json(target, payload)
    print(json.dumps({
        "status": "frozen",
        "run_lock": _relative_or_absolute(target),
        "sha256": sha256_file(target),
        "manifest_sha256": payload["manifest_sha256"],
        "corpus_size": payload["corpus_size"],
        "archived_previous_lock": archived_previous_lock,
    }, ensure_ascii=False, indent=2))
    return 0


def command_freeze_fusion_lock(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    state_path = Path(args.state)
    audit_path = Path(args.audit) if args.audit else workspace / "logs/formal-summary.json"
    semantic_audit_path = (
        Path(args.semantic_audit)
        if args.semantic_audit else workspace / "logs/formal-semantic-audit.json"
    )
    target = Path(args.output) if args.output else workspace / "config/fusion-lock.json"
    if workspace != target.resolve() and workspace not in target.resolve().parents:
        raise ContractError(f"fusion-lock 必须位于实验 workspace: {target}")
    if target.exists() and not args.force:
        raise ContractError(f"fusion-lock 已存在；改变 Phase G 契约须显式 --force: {target}")
    payload = build_fusion_lock_payload(
        workspace,
        state_path,
        audit_path=audit_path,
        semantic_audit_path=semantic_audit_path,
        seed=args.seed,
    )
    archived_previous_lock = ""
    if target.exists():
        previous_sha256 = sha256_file(target)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = workspace / "archive" / f"fusion-lock-{previous_sha256[:12]}-{stamp}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, archive)
        archived_previous_lock = _relative_or_absolute(archive)
    atomic_write_json(target, payload)
    print(json.dumps({
        "status": "frozen",
        "fusion_lock": _relative_or_absolute(target),
        "sha256": sha256_file(target),
        "phase_l_run_lock_sha256": payload["phase_l"]["run_lock_sha256"],
        "manifest_sha256": payload["phase_l"]["manifest_sha256"],
        "corpus_size": payload["corpus_size"],
        "bundle_count": len(payload["phase_l"]["bundles"]),
        "archived_previous_lock": archived_previous_lock,
    }, ensure_ascii=False, indent=2))
    return 0


def command_validate_fusion_lock(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    state = load_run_state(Path(args.state))
    lock_path = Path(args.fusion_lock) if args.fusion_lock else workspace / "config/fusion-lock.json"
    errors = validate_fusion_lock(lock_path, workspace, state)
    print(json.dumps({
        "status": "valid" if not errors else "invalid",
        "fusion_lock": _relative_or_absolute(lock_path),
        "sha256": sha256_file(lock_path) if lock_path.is_file() else "",
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def _clone_state_for_fusion(
    source: dict,
    *,
    mode: str = "shakedown",
    fusion_lock_path: Path | None = None,
) -> dict:
    clone = json.loads(json.dumps(source, ensure_ascii=False))
    clone["created_at"] = datetime.now(timezone.utc).isoformat()
    clone["purpose"] = f"fusion-{mode}"
    if mode == "formal":
        if fusion_lock_path is None or not fusion_lock_path.is_file():
            raise ContractError("正式 fusion state 缺 fusion-lock")
        clone["mode"] = "formal"
        clone["fusion_lock"] = _relative_or_absolute(fusion_lock_path)
        clone["fusion_lock_sha256"] = sha256_file(fusion_lock_path)
    for phase in ("fusion", "snapshot", "promotion"):
        for entry_id in clone["entry_order"]:
            clone["steps"][phase][entry_id] = {
                "status": "pending", "attempts": 0, "updated_at": "",
                "error": "", "artifacts": [],
            }
    return clone


def _validate_fusion_state_binding(state: dict, fusion_lock_path: Path) -> None:
    if state.get("mode") != "formal" or state.get("purpose") != "fusion-formal":
        raise ContractError("正式 fusion ledger mode/purpose 无效")
    if _artifact_path(str(state.get("fusion_lock") or "")).resolve() != fusion_lock_path.resolve():
        raise ContractError("正式 fusion ledger 指向不同 fusion-lock")
    if state.get("fusion_lock_sha256") != sha256_file(fusion_lock_path):
        raise ContractError("正式 fusion ledger 的 fusion-lock 哈希漂移")


def _graph_runtime_targets(layout: dict[str, Path | str]) -> list[Path]:
    runtime_root = Path(layout["runtime_root"])
    db_path = Path(layout["db"])
    return [
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        Path(layout["state"]),
        Path(layout["graph_run"]),
        runtime_root / "state/embeddings.db",
        Path(layout["snapshots"]),
        Path(layout["fusion_logs"]),
        runtime_root / "logs/hub-gate-inputs",
        runtime_root / "config/hub-gates",
        runtime_root / "derived",
    ]


def _archive_graph_runtime(
    workspace: Path,
    layout: dict[str, Path | str],
    *,
    label: str,
) -> str:
    existing = [path for path in _graph_runtime_targets(layout) if path.exists()]
    if not existing:
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = workspace / "archive" / f"{label}-{stamp}"
    archive.mkdir(parents=True, exist_ok=False)
    runtime_root = Path(layout["runtime_root"])
    for path in existing:
        base = runtime_root if runtime_root == path or runtime_root in path.parents else workspace
        destination = archive / path.relative_to(base)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
    return _relative_or_absolute(archive)


def _validate_graph_run_g0(layout: dict[str, Path | str], *, mode: str) -> dict:
    run_path = Path(layout["graph_run"])
    if not run_path.is_file():
        raise ContractError(f"graph run receipt 缺失: {run_path}")
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if run.get("schema") != "wikigraph.e1.graph-run.v1" or run.get("mode") != mode:
        raise ContractError("graph run receipt schema/mode 无效")
    normalized_g0 = {}
    for kind in ("db", "jsonl"):
        artifact = (run.get("g0") or {}).get(kind) or {}
        if isinstance(artifact, str):
            if mode != "shakedown":
                raise ContractError(f"正式 G0 {kind} receipt 缺 SHA-256")
            artifact_path = _artifact_path(artifact)
            expected_sha256 = ""
        else:
            artifact_path = _artifact_path(str(artifact.get("path") or ""))
            expected_sha256 = str(artifact.get("sha256") or "")
        if not artifact_path.is_file():
            raise ContractError(f"G0 {kind} 缺失: {artifact_path}")
        actual_sha256 = sha256_file(artifact_path)
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ContractError(f"G0 {kind} 哈希漂移: {artifact_path}")
        normalized_g0[kind] = {
            "path": _relative_or_absolute(artifact_path),
            "sha256": actual_sha256,
        }
    run["g0"] = normalized_g0
    return run


def command_init_graph(args: argparse.Namespace) -> int:
    import graph_lib as gl

    workspace = _safe_workspace(Path(args.workspace))
    if args.restart and args.reuse:
        raise ContractError("--restart 与 --reuse 不能同时使用")
    source_state_path = Path(args.state) if args.state else (
        workspace / "state/run-state.json" if args.shakedown
        else workspace / "state/formal-run-state.json"
    )
    source_state = load_run_state(source_state_path)
    manifest_path, _manifest = _verified_state_manifest(source_state)
    layout = _fusion_layout(workspace, source_state, shakedown=args.shakedown)
    mode = str(layout["mode"])
    runtime_root = Path(layout["runtime_root"])
    fusion_lock_path = None
    fusion_lock_sha256 = ""
    if args.shakedown:
        audit_path = workspace / "logs/shakedown-summary.json"
        if not audit_path.is_file() or not json.loads(audit_path.read_text(encoding="utf-8")).get("passed"):
            raise ContractError("init-graph 前必须通过 shakedown local audit")
    else:
        fusion_lock_path = (
            Path(args.fusion_lock) if args.fusion_lock else workspace / "config/fusion-lock.json"
        )
        lock_errors = validate_fusion_lock(fusion_lock_path, workspace, source_state)
        if lock_errors:
            raise ContractError("formal fusion-lock 无效: " + "; ".join(lock_errors[:8]))
        fusion_lock_sha256 = sha256_file(fusion_lock_path)
    default_db = Path(layout["db"])
    db_path = Path(args.db) if args.db else default_db
    if runtime_root != db_path.resolve() and runtime_root not in db_path.resolve().parents:
        raise ContractError("实验 graph DB 必须位于本次 runtime root")
    if not args.shakedown and db_path.resolve() != default_db.resolve():
        raise ContractError("正式 graph DB 路径由 fusion-lock 固定，不接受 --db 改写")
    default_state = Path(layout["state"])
    state_path = Path(args.fusion_state) if args.fusion_state else default_state
    if not args.shakedown and state_path.resolve() != default_state.resolve():
        raise ContractError("正式 fusion state 路径由 fusion-lock 固定")
    archived_to = ""
    existing = [path for path in _graph_runtime_targets(layout) if path.exists()]
    if args.restart:
        archived_to = _archive_graph_runtime(
            workspace, layout,
            label=f"{mode}-graph-{source_state.get('run_lock_sha256', '')[:12]}".rstrip("-"),
        )
        existing = []
    if existing:
        if not args.reuse:
            raise ContractError(
                f"{mode} graph runtime 已存在；断点续做直接运行 fuse-graph，"
                "仅核验用 --reuse，重启用 --restart"
            )
        state = load_run_state(state_path)
        if not args.shakedown:
            assert fusion_lock_path is not None
            _validate_fusion_state_binding(state, fusion_lock_path)
        run = _validate_graph_run_g0(layout, mode=mode)
        print(json.dumps({
            "status": "reused",
            "mode": mode,
            "db": _relative_or_absolute(db_path),
            "state": _relative_or_absolute(state_path),
            "g0_sha256": run["g0"]["db"]["sha256"],
            "highest_contiguous_snapshot": highest_contiguous_completed(state, "snapshot"),
            "archived_previous_run": "",
        }, ensure_ascii=False, indent=2))
        return 0
    _configure_experiment_runtime(runtime_root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = gl.connect(db_path)
    try:
        gl.init_schema(conn)
        validation = _graph_validation(conn)
        if validation["errors"]:
            raise ContractError("G0 graph schema 校验失败")
        conn.commit()
    finally:
        conn.close()
    atomic_write_json(
        state_path,
        _clone_state_for_fusion(
            source_state,
            mode=mode,
            fusion_lock_path=fusion_lock_path,
        ),
    )
    snapshot_dir = Path(layout["snapshots"])
    g0_db = snapshot_dir / "G000.db"
    g0_jsonl = snapshot_dir / "G000.jsonl"
    snapshot_sqlite(db_path, g0_db)
    export_graph_jsonl(g0_db, g0_jsonl)
    run_path = Path(layout["graph_run"])
    atomic_write_json(run_path, {
        "schema": "wikigraph.e1.graph-run.v1",
        "mode": mode,
        "manifest": _relative_or_absolute(manifest_path),
        "manifest_sha256": source_state["manifest_sha256"],
        "phase_l_run_lock_sha256": source_state.get("run_lock_sha256", ""),
        "fusion_lock": _relative_or_absolute(fusion_lock_path) if fusion_lock_path else "",
        "fusion_lock_sha256": fusion_lock_sha256,
        "db": _relative_or_absolute(db_path),
        "state": _relative_or_absolute(state_path),
        "g0": {
            "db": {"path": _relative_or_absolute(g0_db), "sha256": sha256_file(g0_db)},
            "jsonl": {"path": _relative_or_absolute(g0_jsonl), "sha256": sha256_file(g0_jsonl)},
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    print(json.dumps({
        "status": "initialized",
        "mode": mode,
        "db": _relative_or_absolute(db_path),
        "state": _relative_or_absolute(state_path),
        "g0_sha256": sha256_file(g0_db),
        "fusion_lock_sha256": fusion_lock_sha256,
        "archived_previous_run": archived_to,
    }, ensure_ascii=False, indent=2))
    return 0


def command_fuse_graph(args: argparse.Namespace) -> int:
    workspace = _safe_workspace(Path(args.workspace))
    if args.shakedown:
        state_path = Path(args.state) if args.state else workspace / "state/shakedown-run-state.json"
        state = load_run_state(state_path)
        layout = _fusion_layout(workspace, state, shakedown=True)
        fusion_lock_path = None
    else:
        if args.state:
            state_path = Path(args.state)
            state = load_run_state(state_path)
            layout = _fusion_layout(workspace, state, shakedown=False)
        else:
            phase_state = load_run_state(workspace / "state/formal-run-state.json")
            layout = _fusion_layout(workspace, phase_state, shakedown=False)
            state_path = Path(layout["state"])
            state = load_run_state(state_path)
        if state_path.resolve() != Path(layout["state"]).resolve():
            raise ContractError("正式 fusion state 必须使用 fusion-lock 固定的 run-specific ledger")
        fusion_lock_path = (
            Path(args.fusion_lock) if args.fusion_lock
            else _artifact_path(str(state.get("fusion_lock") or workspace / "config/fusion-lock.json"))
        )
        lock_errors = validate_fusion_lock(fusion_lock_path, workspace, state)
        if lock_errors:
            raise ContractError("formal fusion-lock 无效: " + "; ".join(lock_errors[:8]))
        _validate_fusion_state_binding(state, fusion_lock_path)
    mode = str(layout["mode"])
    runtime_root = Path(layout["runtime_root"])
    _manifest_path, manifest = _verified_state_manifest(state)
    included = {
        str(item["entry_id"]): item for item in manifest["entries"]
        if item.get("decision") == "include"
    }
    default_db = Path(layout["db"])
    db_path = Path(args.db) if args.db else default_db
    if runtime_root != db_path.resolve() and runtime_root not in db_path.resolve().parents:
        raise ContractError("实验 graph DB 必须位于本次 runtime root")
    if not args.shakedown and db_path.resolve() != default_db.resolve():
        raise ContractError("正式 graph DB 路径由 fusion-lock 固定")
    if not db_path.is_file():
        raise ContractError(f"{mode} graph 不存在；先运行 init-graph" + (" --shakedown" if args.shakedown else ""))
    graph_run = _validate_graph_run_g0(layout, mode=mode)
    if not args.shakedown:
        if graph_run.get("fusion_lock_sha256") != sha256_file(fusion_lock_path):
            raise ContractError("graph run receipt 未绑定当前 fusion-lock")
    _configure_experiment_runtime(runtime_root)
    completed_count = highest_contiguous_completed(state, "snapshot")
    entry_ids = state["entry_order"][completed_count:]
    if args.limit is not None:
        entry_ids = entry_ids[: args.limit]
    completed = []
    failed = []
    log_dir = Path(layout["fusion_logs"])
    snapshot_dir = Path(layout["snapshots"])
    artifact_root = _run_artifact_root(workspace, state)
    for entry_id in entry_ids:
        entry = included[entry_id]
        step = int(entry["sequence_index"])
        if step != highest_contiguous_completed(state, "snapshot") + 1:
            raise ContractError(f"fusion 只能从最高连续 snapshot 的下一步恢复: G{step:03d}")
        if not completed_phase_chain_is_valid(state, "local_validate", entry_id):
            failed.append({"entry_id": entry_id, "phase": "local_validate", "error": "local bundle 未验证"})
            break
        artifact_dir = _local_artifact_dir(workspace, entry, artifact_root)
        bundle_errors = verify_local_bundle(artifact_dir)
        if bundle_errors:
            failed.append({
                "entry_id": entry_id,
                "phase": "local_validate",
                "error": "; ".join(bundle_errors[:4]),
            })
            break
        previous = snapshot_dir / f"G{step - 1:03d}.db"
        if not previous.is_file():
            raise ContractError(f"缺最高连续恢复快照: {previous}")
        snapshot_sqlite(previous, db_path)
        checkpoint_step(state_path, state, "fusion", entry_id, "running")
        checkpoint_step(state_path, state, "snapshot", entry_id, "running")
        try:
            report, db_snapshot, jsonl_snapshot = _fuse_one(
                runtime_root,
                db_path,
                entry,
                artifact_dir,
                log_dir,
                snapshot_dir,
            )
            _checkpoint_fusion_snapshot(
                state_path, state, entry_id,
                fusion_artifacts=[report],
                snapshot_artifacts=[db_snapshot, jsonl_snapshot],
            )
            completed.append(entry_id)
        except AgentRequired as exc:
            request_path = runtime_root / "logs/hub-gate-inputs" / f"G{step:03d}.json"
            request_artifacts = [request_path] if request_path.is_file() else []
            checkpoint_step(
                state_path, state, "fusion", entry_id, "agent_required",
                artifacts=request_artifacts, error=str(exc),
            )
            checkpoint_step(
                state_path, state, "snapshot", entry_id, "agent_required", error=str(exc),
            )
            failed.append({
                "entry_id": entry_id,
                "phase": "fusion",
                "status": "agent_required",
                "error": str(exc),
            })
            break
        except Exception as exc:
            checkpoint_step(state_path, state, "fusion", entry_id, "failed", error=str(exc))
            checkpoint_step(state_path, state, "snapshot", entry_id, "failed", error=str(exc))
            failed.append({"entry_id": entry_id, "phase": "fusion", "error": str(exc)})
            # Chronology forbids skipping the failed step even with --keep-going.
            break
    print(json.dumps({
        "status": (
            "completed" if not failed else
            "agent_required" if failed[0].get("status") == "agent_required" else
            "partial_failure"
        ),
        "mode": mode,
        "completed": completed,
        "failed": failed,
        "highest_contiguous_snapshot": highest_contiguous_completed(state, "snapshot"),
    }, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inventory = sub.add_parser("inventory", help="生成 works 语料候选 manifest")
    inventory.add_argument("--works-root", default=str(WORKS_ROOT))
    inventory.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    inventory.set_defaults(func=command_inventory)

    validate = sub.add_parser("validate-manifest", help="校验已裁决 manifest")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--expected-publications", type=int)
    validate.set_defaults(func=command_validate)

    adjudicate = sub.add_parser("apply-decisions", help="将小型裁决文件应用到候选 manifest")
    adjudicate.add_argument("--manifest", required=True)
    adjudicate.add_argument("--decisions", required=True)
    adjudicate.add_argument("--output")
    adjudicate.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    adjudicate.set_defaults(func=command_apply_decisions)

    freeze = sub.add_parser("freeze-manifest", help="验证裁决并冻结 chronological corpus")
    freeze.add_argument("--manifest", required=True)
    freeze.add_argument("--expected-publications", type=int)
    freeze.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    freeze.set_defaults(func=command_freeze_manifest)

    snapshot = sub.add_parser("snapshot", help="一致性复制并完整导出实验 graph DB")
    snapshot.add_argument("--db", required=True)
    snapshot.add_argument("--step", required=True, type=int)
    snapshot.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    snapshot.set_defaults(func=command_snapshot)

    init_run = sub.add_parser("init-run", help="从冻结 manifest 初始化可恢复运行 ledger")
    init_run.add_argument("--manifest", required=True)
    init_run.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    init_run.add_argument("--force", action="store_true")
    init_run.set_defaults(func=command_init_run)

    init_formal = sub.add_parser(
        "init-formal-run",
        help="以冻结 run-lock 和已验证 extraction 初始化独立正式运行",
    )
    init_formal.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    init_formal.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    init_formal.add_argument("--output")
    init_formal.add_argument("--force", action="store_true")
    init_formal.set_defaults(func=command_init_formal_run)

    status = sub.add_parser("status", help="汇总断点与可恢复队列")
    status.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    status.set_defaults(func=command_status)

    extract = sub.add_parser("extract-local", help="从 frozen manifest 可恢复地 fresh-extract PDF")
    extract.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    extract.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    extract.add_argument("--entry")
    extract.add_argument("--limit", type=int)
    extract.add_argument("--engine", choices=["mineru", "blsc_ocr", "docling", "pymupdf"])
    extract.add_argument("--force", action="store_true")
    extract.add_argument("--keep-going", action="store_true")
    extract.set_defaults(func=command_extract_local)

    compile_local = sub.add_parser(
        "compile-local",
        help="可恢复地执行 bibliography→wiki→semantics→local validation",
    )
    compile_local.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    compile_local.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    compile_local.add_argument("--entry")
    compile_local.add_argument("--limit", type=int)
    compile_local.add_argument("--shakedown", action="store_true")
    compile_local.add_argument("--keep-going", action="store_true")
    compile_local.set_defaults(func=command_compile_local)

    audit_local = sub.add_parser("audit-local", help="复验 local bundle 并汇总质量信号")
    audit_local.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    audit_local.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    audit_local.add_argument("--limit", type=int)
    audit_local.add_argument("--kind", default="shakedown")
    audit_local.add_argument("--output")
    audit_local.set_defaults(func=command_audit_local)

    run_lock = sub.add_parser("freeze-run-lock", help="冻结正式运行的 code/model/config/source 契约")
    run_lock.add_argument("--manifest", required=True)
    run_lock.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    run_lock.add_argument("--state", default=str(DEFAULT_WORKSPACE / "state/run-state.json"))
    run_lock.add_argument("--audit")
    run_lock.add_argument("--engine", default="mineru", choices=["mineru", "blsc_ocr", "docling", "pymupdf"])
    run_lock.add_argument("--seed", type=int, default=0)
    run_lock.add_argument("--force", action="store_true")
    run_lock.set_defaults(func=command_freeze_run_lock)

    fusion_lock = sub.add_parser(
        "freeze-fusion-lock",
        help="冻结独立 Phase-G 代码/阈值/Hub gate 与 Phase-L bundle 契约",
    )
    fusion_lock.add_argument(
        "--state",
        default=str(DEFAULT_WORKSPACE / "state/formal-run-state.json"),
    )
    fusion_lock.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    fusion_lock.add_argument("--audit")
    fusion_lock.add_argument("--semantic-audit")
    fusion_lock.add_argument("--output")
    fusion_lock.add_argument("--seed", type=int, default=0)
    fusion_lock.add_argument("--force", action="store_true")
    fusion_lock.set_defaults(func=command_freeze_fusion_lock)

    validate_fusion_lock_parser = sub.add_parser(
        "validate-fusion-lock",
        help="复验 Phase-G 锁、59 个 Phase-L bundle 与隔离路径",
    )
    validate_fusion_lock_parser.add_argument(
        "--state",
        default=str(DEFAULT_WORKSPACE / "state/formal-run-state.json"),
    )
    validate_fusion_lock_parser.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    validate_fusion_lock_parser.add_argument("--fusion-lock")
    validate_fusion_lock_parser.set_defaults(func=command_validate_fusion_lock)

    init_graph = sub.add_parser("init-graph", help="初始化隔离 G0 与 fusion ledger")
    init_graph.add_argument("--state")
    init_graph.add_argument("--fusion-state")
    init_graph.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    init_graph.add_argument("--db")
    init_graph.add_argument("--fusion-lock")
    init_graph.add_argument("--shakedown", action="store_true")
    init_graph.add_argument("--reuse", action="store_true")
    init_graph.add_argument("--restart", action="store_true")
    init_graph.set_defaults(func=command_init_graph)

    fuse_graph = sub.add_parser("fuse-graph", help="严格按 sequence_index 融合并原子快照")
    fuse_graph.add_argument("--state")
    fuse_graph.add_argument("--workspace", default=str(DEFAULT_WORKSPACE))
    fuse_graph.add_argument("--db")
    fuse_graph.add_argument("--fusion-lock")
    fuse_graph.add_argument("--limit", type=int)
    fuse_graph.add_argument("--shakedown", action="store_true")
    fuse_graph.add_argument("--keep-going", action="store_true")
    fuse_graph.set_defaults(func=command_fuse_graph)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except ContractError as exc:
        print(json.dumps({"status": "contract_error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())

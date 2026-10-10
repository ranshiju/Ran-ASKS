#!/usr/bin/env python3
"""Repair meeting identity or semantic projection through a validated transaction."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))

import graph_lib as gl
import ingest_meeting as meeting
import student_guidance_projection as guidance
import source_locator
from meeting_compiler_contract import validate_meeting_ir


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _managed_repair_input(path_value: str) -> Path:
    path = (REPO / path_value).resolve()
    path.relative_to((REPO / "temp" / "meeting-repair").resolve())
    if not path.is_file():
        raise ValueError(f"managed repair input does not exist: {path_value}")
    return path


def _apply_meeting_ir_patch(meeting_ir: dict, patch: dict) -> dict:
    if not isinstance(patch, dict) or patch.get("schema") != "meeting-ir-repair-patch-v1":
        raise ValueError("meeting IR patch must use meeting-ir-repair-patch-v1")
    replacements = patch.get("replace_decisions")
    if not isinstance(replacements, list) or not replacements:
        raise ValueError("meeting IR patch requires replace_decisions")
    repaired = json.loads(json.dumps(meeting_ir, ensure_ascii=False))
    decisions = repaired.get("decisions", [])
    for index, replacement in enumerate(replacements):
        if not isinstance(replacement, dict) or set(replacement) != {
            "old_text", "new_text", "evidence_ids",
        }:
            raise ValueError(f"replace_decisions[{index}] has invalid fields")
        matches = [row for row in decisions if row.get("text") == replacement["old_text"]]
        if len(matches) != 1:
            raise ValueError(
                f"replace_decisions[{index}] must match exactly one decision; matched={len(matches)}"
            )
        matches[0]["text"] = str(replacement["new_text"] or "").strip()
        matches[0]["evidence_ids"] = list(replacement["evidence_ids"])
    return repaired


def _run_graph(page: str, wiki: Path, semantic: Path, transaction_id: str,
               knowledge_ir: Path, graph_plan: Path, *, plan_only: bool) -> dict:
    command = [
        sys.executable, str(REPO / ".scripts" / "graph_ingest.py"), "ingest",
        "--page", page, "--page-file", str(wiki), "--semantic", str(semantic),
        "--transaction-id", transaction_id,
        "--knowledge-ir-out", str(knowledge_ir),
        "--graph-plan-out", str(graph_plan), "--clean",
    ]
    if plan_only:
        command.append("--plan-only")
    result = subprocess.run(command, cwd=REPO, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return json.loads(result.stdout)


def _replace_identity(meeting_ir: dict, old_entity: str, new_entity: str,
                      new_label: str) -> int:
    changed = 0
    for row in meeting_ir.get("attendees", []):
        if row.get("person") == old_entity:
            row["person"], row["label"] = new_entity, new_label
            changed += 1
    for row in meeting_ir.get("reports", []):
        if row.get("person") == old_entity:
            row["person"], row["person_label"] = new_entity, new_label
            changed += 1
    for row in meeting_ir.get("tasks", []):
        if row.get("assignee") == old_entity:
            row["assignee"], row["assignee_label"] = new_entity, new_label
            changed += 1
    for row in meeting_ir.get("relations", []):
        for field in ("subject", "object"):
            if row.get(field) == old_entity:
                row[field] = new_entity
                changed += 1
    return changed


def _apply_identity_corrections(resolution: dict, source_text: str, corrections: list) -> list[dict]:
    """Resolve source mentions only against archived, anchored user assertions."""
    if not isinstance(corrections, list) or not corrections:
        raise ValueError("identity corrections must be a nonempty list")
    validated = []
    seen = set()
    for correction in corrections:
        if not isinstance(correction, dict) or set(correction) != {
            "mention", "canonical", "label", "source", "quote",
        } or any(not isinstance(value, str) or not value.strip() for value in correction.values()):
            raise ValueError("identity correction requires mention, canonical, label, source and quote strings")
        mention = correction["mention"]
        if mention in seen or mention not in source_text:
            raise ValueError("identity correction must name a unique exact Raw mention")
        seen.add(mention)
        rows = [row for row in resolution.get("compiler_entity_resolutions", [])
                if row.get("mention") == mention]
        if len(rows) != 1:
            raise ValueError("identity correction must match one compiler entity decision")
        path, fragment = source_locator.split_locator(correction["source"])
        if path != "cross-domain/raw/facts/user-assertions.md" or not fragment.startswith("fact-"):
            raise ValueError("identity correction requires an archived user assertion fact anchor")
        target = (REPO / path).resolve()
        target.relative_to((REPO / "cross-domain/raw").resolve())
        if not target.is_file() or source_locator.locator_status(fragment, target) != "present":
            raise ValueError("identity correction assertion locator is missing")
        excerpt = source_locator.read_locator_text(target, fragment) or ""
        quote = correction["quote"]
        if quote not in excerpt or mention not in quote or correction["label"] not in quote:
            raise ValueError("identity correction quote does not bind the mention and canonical label")
        person_path = (REPO / (correction["canonical"] + ".md")).resolve()
        person_relative = person_path.relative_to(REPO.resolve())
        if len(person_relative.parts) < 4 or person_relative.parts[0] not in {
            "academic", "admin", "teaching", "business",
        } or person_relative.parts[1] != "wiki":
            raise ValueError("identity correction canonical page must be in public Wiki")
        if not person_path.is_file():
            raise ValueError("identity correction canonical people page is missing")
        frontmatter, _body = meeting._meeting_frontmatter(person_path.read_text(encoding="utf-8"))
        if frontmatter.get("type") != "people" or frontmatter.get("title") != correction["label"]:
            raise ValueError("identity correction canonical people page does not match its label")
        if path not in [source_locator.split_locator(value)[0]
                        for value in frontmatter.get("sources", [])]:
            raise ValueError("identity correction people page lacks the assertion source")
        validated.append({**correction, "source_sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    for correction in validated:
        mention = correction["mention"]
        decision = next(row for row in resolution["compiler_entity_resolutions"] if row["mention"] == mention)
        decision.update({
            "canonical": correction["canonical"], "status": "resolved",
            "reason": f"Archived user assertion: {correction['source']}",
            "identity_evidence": correction,
        })
        matched = [row for row in resolution.get("resolved", []) if row.get("original") == mention]
        if not matched:
            matched = [{"original": mention}]
            resolution.setdefault("resolved", []).extend(matched)
        for row in matched:
            row.update({"normalized": correction["label"], "entity": correction["canonical"],
                        "method": "user_assertion_repair", "confidence": "medium",
                        "identity_evidence": correction})
    return validated


def repair(args: argparse.Namespace) -> dict:
    state_path = REPO / "temp" / "inbox-state" / f"{args.transaction_id}.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "completed":
        raise ValueError("only a completed meeting transaction can be repaired")
    page = str(state["wiki_path"])
    wiki_path = REPO / f"{page}.md"
    raw_source = REPO / state["raw_dir"] / state["source_filename"]
    resolution_path = raw_source.parent / "entity-resolution.json"
    source_text = raw_source.read_text(encoding="utf-8")
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    projection_mode = bool(args.meeting_ir_file or args.meeting_ir_patch_file)
    if projection_mode:
        if args.meeting_ir_file:
            repaired_ir = json.loads(
                _managed_repair_input(args.meeting_ir_file).read_text(encoding="utf-8")
            )
        else:
            patch = json.loads(
                _managed_repair_input(args.meeting_ir_patch_file).read_text(encoding="utf-8")
            )
            repaired_ir = _apply_meeting_ir_patch(state["meeting_ir_content"], patch)
        wiki_draft = _managed_repair_input(args.wiki_file).read_text(encoding="utf-8")
        repaired_resolution = json.loads(json.dumps(resolution, ensure_ascii=False))
        updates = repaired_ir.get("person_updates", [])
        identity_changes = resolution_changes = 0
        digest = hashlib.sha256(
            _json_bytes(repaired_ir) + wiki_draft.encode("utf-8")
        ).hexdigest()[:8]
        repair_id = f"{args.transaction_id}-projection-{digest}"
        audit = {
            "repair_id": repair_id,
            "kind": "meeting-projection-conflict-fix",
            "raw_source": str(raw_source.relative_to(REPO)),
            "identity_changes": 0,
            "guidance_updates": len(updates),
        }
    else:
        if args.mention not in source_text:
            raise ValueError(f"Raw does not contain exact mention: {args.mention}")
        matched = [row for row in resolution.get("resolved", [])
                   if row.get("original") == args.mention]
        if not matched:
            raise ValueError(f"entity-resolution has no exact mention: {args.mention}")
        old_entities = {str(row.get("entity") or "") for row in matched}
        if len(old_entities) != 1:
            raise ValueError(f"mention resolves to multiple entities: {sorted(old_entities)}")
        old_entity = next(iter(old_entities))
        repaired_ir = json.loads(json.dumps(state["meeting_ir_content"], ensure_ascii=False))
        identity_changes = _replace_identity(
            repaired_ir, old_entity, args.canonical_entity, args.canonical_label,
        )
        updates = json.loads((REPO / args.updates_file).read_text(encoding="utf-8"))
        repaired_ir["person_updates"] = updates
        wiki_draft = wiki_path.read_text(encoding="utf-8")

    catalog, _ = meeting._write_evidence_catalog(state, source_text)
    evidence_ids = {row["evidence_id"] for row in catalog}
    guidance.prepare_candidate_catalog(state, REPO)
    errors = validate_meeting_ir(repaired_ir, evidence_ids)
    errors.extend(guidance.validate_person_updates(
        repaired_ir["person_updates"], guidance.load_candidate_catalog(state, REPO)
    ))
    if errors:
        raise ValueError("; ".join(errors[:10]))

    if not projection_mode:
        repaired_resolution = json.loads(json.dumps(resolution, ensure_ascii=False))
        resolution_changes = 0
        for row in repaired_resolution.get("resolved", []):
            if row.get("original") != args.mention:
                continue
            row.update({
                "normalized": args.canonical_label,
                "entity": args.canonical_entity,
                "method": "raw_exact_repair",
                "confidence": "high",
            })
            resolution_changes += 1
        for row in repaired_resolution.get("compiler_entity_resolutions", []):
            if row.get("mention") == args.mention:
                row.update({
                    "canonical": args.canonical_entity,
                    "status": "resolved",
                    "reason": "Raw exact identity correction validated by managed repair",
                })
        repair_id = f"{args.transaction_id}-identity-{hashlib.sha256(args.mention.encode()).hexdigest()[:8]}"
        audit = {
            "repair_id": repair_id,
            "kind": "meeting-identity-conflict-fix",
            "mention": args.mention,
            "from": old_entity,
            "to": args.canonical_entity,
            "raw_source": str(raw_source.relative_to(REPO)),
            "resolution_changes": resolution_changes,
            "identity_changes": identity_changes,
            "guidance_updates": len(updates),
        }
    corrections_path = getattr(args, "identity_corrections_file", None)
    if corrections_path:
        corrections = json.loads(_managed_repair_input(corrections_path).read_text(encoding="utf-8"))
        identity_evidence = _apply_identity_corrections(repaired_resolution, source_text, corrections)
        for correction in identity_evidence:
            if correction["source"] not in wiki_draft:
                raise ValueError("identity correction Wiki must cite the assertion locator")
            for section, endpoint, label in (
                ("attendees", "person", "label"), ("reports", "person", "person_label"),
                ("tasks", "assignee", "assignee_label"),
            ):
                for row in repaired_ir[section]:
                    if row[label] in {correction["mention"], correction["label"]} and (
                        row[endpoint] != correction["canonical"] or row[label] != correction["label"]
                    ):
                        raise ValueError("identity correction disagrees with the proposed role projection")
        repair_id += "-" + hashlib.sha256(_json_bytes(identity_evidence)).hexdigest()[:8]
        audit.update({"repair_id": repair_id, "identity_evidence": identity_evidence})
        audit["resolution_changes"] = len(identity_evidence)
    repairs = repaired_resolution.setdefault("managed_repairs", [])
    if not any(row.get("repair_id") == repair_id for row in repairs):
        repairs.append(audit)

    stage = REPO / "temp" / "meeting-repair" / repair_id
    stage.mkdir(parents=True, exist_ok=True)
    repaired_wiki = meeting.compile_meeting_navigation(
        wiki_draft, repaired_ir,
        raw_source=str(raw_source.relative_to(REPO)), date=state["date"], catalog=catalog,
    )
    staged_wiki = stage / "wiki.md"
    staged_semantic = stage / "semantic.txt"
    staged_ir = stage / "meeting-ir.json"
    staged_resolution = stage / "entity-resolution.json"
    staged_knowledge = stage / "knowledge-ir.json"
    staged_plan = stage / "graph-plan.json"
    staged_wiki.write_text(repaired_wiki, encoding="utf-8")
    staged_semantic.write_text(meeting.compile_meeting_slots(repaired_ir), encoding="utf-8")
    staged_ir.write_bytes(_json_bytes(repaired_ir))
    staged_resolution.write_bytes(_json_bytes(repaired_resolution))

    check_state = dict(state)
    check_state.update({
        "wiki_content": repaired_wiki,
        "meeting_ir_content": repaired_ir,
        "source": str(raw_source.relative_to(REPO)),
        "extract_dir": str(stage.relative_to(REPO)),
    })
    wiki_errors = meeting.step_validate_wiki(check_state)
    if wiki_errors:
        raise ValueError("; ".join(wiki_errors[:10]))
    plan = _run_graph(
        page, staged_wiki, staged_semantic, repair_id,
        staged_knowledge, staged_plan, plan_only=True,
    )
    if not args.apply:
        return {"status": "planned", **audit, "stage": str(stage.relative_to(REPO)),
                "graph_plan_only": bool(plan.get("plan_only"))}

    backup = stage / "backup"
    backup.mkdir(exist_ok=True)
    graph_backup = backup / "graph.db"
    if graph_backup.exists():
        graph_backup.unlink()
    with gl.connect(read_only=True) as conn:
        gl.backup_graph(conn, graph_backup)
    targets = {
        wiki_path: backup / "wiki.md",
        resolution_path: backup / "raw-entity-resolution.json",
        state_path: backup / "state.json",
        REPO / state["meeting_ir"]: backup / "meeting-ir.json",
        REPO / state["entity_resolution"]: backup / "extract-entity-resolution.json",
        REPO / state["semantic_path"]: backup / "semantic.txt",
        REPO / "temp/inbox-state" / f"{args.transaction_id}-knowledge-ir.json":
            backup / "knowledge-ir.json",
        REPO / "temp/inbox-state" / f"{args.transaction_id}-graph-plan.json":
            backup / "graph-plan.json",
        REPO / "projects/学生指导/notes/status.md": backup / "guidance-status.md",
    }
    log_path = REPO / state["log_path"]
    targets[log_path] = backup / "wiki-log.md"
    student_catalog = guidance.load_candidate_catalog(state, REPO)
    by_key = {row["student_key"]: row for row in student_catalog["students"]}
    for update in updates:
        record = REPO / by_key[update["person"]]["record"]
        targets.setdefault(record, backup / f"student-{record.name}")
    missing_targets = set()
    for target, saved in targets.items():
        if target.exists():
            shutil.copy2(target, saved)
        else:
            missing_targets.add(target)

    try:
        graph_report = _run_graph(
            page, staged_wiki, staged_semantic, repair_id,
            staged_knowledge, staged_plan, plan_only=False,
        )
        _atomic_bytes(wiki_path, repaired_wiki.encode("utf-8"))
        _atomic_bytes(resolution_path, _json_bytes(repaired_resolution))
        _atomic_bytes(REPO / state["meeting_ir"], _json_bytes(repaired_ir))
        _atomic_bytes(REPO / state["entity_resolution"], _json_bytes(repaired_resolution))
        _atomic_bytes(REPO / state["semantic_path"], staged_semantic.read_bytes())
        original_knowledge = REPO / "temp/inbox-state" / f"{args.transaction_id}-knowledge-ir.json"
        original_plan = REPO / "temp/inbox-state" / f"{args.transaction_id}-graph-plan.json"
        _atomic_bytes(original_knowledge, staged_knowledge.read_bytes())
        _atomic_bytes(original_plan, staged_plan.read_bytes())

        state["meeting_ir_content"] = repaired_ir
        state["wiki_content"] = repaired_wiki
        state["slots_content"] = staged_semantic.read_text(encoding="utf-8")
        state["graph_report"] = graph_report
        state.setdefault("managed_repairs", []).append(audit)
        unresolved = [
            row for row in repaired_resolution.get("compiler_entity_resolutions", [])
            if row.get("status") == "unresolved"
        ]
        preserved_warnings = [
            warning for warning in state.get("quality_warnings", [])
            if warning.get("issue") != "meeting_entity_unresolved"
        ]
        preserved_warnings.extend({
            "issue": "meeting_entity_unresolved",
            "mention": str(row.get("mention") or ""),
            "reason": str(row.get("reason") or ""),
        } for row in unresolved)
        state["quality_warnings"] = preserved_warnings
        state["quality_status"] = "degraded" if preserved_warnings else "clean"
        ok, error = guidance.apply_projection(state, REPO)
        if not ok:
            raise RuntimeError(error)
        state["student_guidance_report"] = state.get("student_guidance_report", {})
        _atomic_bytes(state_path, _json_bytes(state))

        check = subprocess.run(
            [sys.executable, str(REPO / ".scripts/ingest_check.py"),
             str(wiki_path), "--graph"], cwd=REPO, text=True, capture_output=True,
        )
        if check.returncode:
            raise RuntimeError(check.stdout.strip() or check.stderr.strip())
        marker = f"conflict-fix | {state['meeting_id']} | {repair_id}"
        log_text = log_path.read_text(encoding="utf-8")
        if marker not in log_text:
            if projection_mode:
                repair_detail = (
                    f"- **会议投影纠正**：依据 `{raw_source.relative_to(REPO)}`，"
                    "更新 evidence-bound MEETING_IR、Wiki 与对应 Graph 导航。\n"
                )
            else:
                repair_detail = (
                    f"- **实体纠正**：依据 `{raw_source.relative_to(REPO)}`，"
                    f"将 `{args.mention}` 从 `{old_entity}` 纠正为 `{args.canonical_entity}`。\n"
                )
            log_text = log_text.rstrip() + (
                f"\n\n## [{datetime.now().date().isoformat()}] {marker}\n"
                f"{repair_detail}"
                f"- **学生指导投影**：提取 {len(updates)} 条更新并同步已有学生档案。\n"
                "- **验证**：来源级 Graph 重建与 `ingest_check --graph` 通过。\n"
            )
            _atomic_bytes(log_path, log_text.encode("utf-8"))
        state["verification_receipt"] = meeting.inbox_state.build_verification_receipt(state, REPO)
        if state["verification_receipt"]["status"] != "PASS":
            raise RuntimeError("repaired meeting verification receipt failed")
        _atomic_bytes(state_path, _json_bytes(state))
    except Exception:
        with gl.graph_writer_lock(gl.GRAPH_DB):
            gl.restore_graph(graph_backup, gl.GRAPH_DB)
        for target, saved in targets.items():
            if target in missing_targets:
                if target.exists():
                    target.unlink()
            else:
                shutil.copy2(saved, target)
        raise

    report_path = REPO / "cross-domain" / "ingest-reports" / f"{repair_id}.json"
    final = {"status": "completed", **audit, "graph_report": graph_report,
             "student_guidance_report": state.get("student_guidance_report", {}),
             "quality_status": state.get("quality_status"),
             "quality_warnings": state.get("quality_warnings", []),
             "verification_receipt": state["verification_receipt"]}
    _atomic_bytes(report_path, _json_bytes(final))
    return final | {"report": str(report_path.relative_to(REPO))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transaction-id", required=True)
    parser.add_argument("--mention")
    parser.add_argument("--canonical-label")
    parser.add_argument("--canonical-entity")
    parser.add_argument("--updates-file")
    parser.add_argument("--meeting-ir-file")
    parser.add_argument("--meeting-ir-patch-file")
    parser.add_argument("--wiki-file")
    parser.add_argument("--identity-corrections-file")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    projection_mode = bool(
        args.meeting_ir_file or args.meeting_ir_patch_file or args.wiki_file
    )
    if projection_mode:
        if not args.wiki_file or bool(args.meeting_ir_file) == bool(args.meeting_ir_patch_file):
            parser.error(
                "projection repair requires --wiki-file and exactly one of "
                "--meeting-ir-file/--meeting-ir-patch-file"
            )
        if any((args.mention, args.canonical_label, args.canonical_entity, args.updates_file)):
            parser.error("projection repair cannot be combined with identity repair arguments")
    elif not all((args.mention, args.canonical_label, args.canonical_entity, args.updates_file)):
        parser.error(
            "identity repair requires --mention, --canonical-label, "
            "--canonical-entity and --updates-file"
        )
    if args.identity_corrections_file and not projection_mode:
        parser.error("identity-corrections-file requires a complete projection repair")
    print(json.dumps(repair(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

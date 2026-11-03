"""Shared preparation/composition of existing assertion and meeting transactions.

The host supplies the meaning of a user's confirmation. This module generates
drafts and runs the existing validators; it makes no model calls or identity
guesses. Each durable stage retains its own rollback and completion receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from datetime import date
from pathlib import Path

import yaml

import agent_task
import ingest_check
import ingest_user_assertions as assertions
import repair_meeting_identity as repair

SCHEMA = "meeting-identity-confirmation-v1"
RAW_FACTS = "cross-domain/raw/facts/user-assertions.md"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _write(path: Path, value) -> None:
    repair._atomic_bytes(path, repair._json_bytes(value))


def _state(repo: Path, transaction_id: str) -> dict:
    if not re.fullmatch(r"[\w-]+", transaction_id):
        raise ValueError("invalid meeting transaction id")
    state = _json(repo / "temp/inbox-state" / f"{transaction_id}.json")
    if state.get("status") != "completed" or not state.get("meeting_ir_content"):
        raise ValueError("confirmation requires a completed meeting with MEETING_IR")
    return state


def _public_path(repo: Path, value: str, *, raw: bool = False) -> Path:
    parts = Path(value).parts
    if len(parts) < 3 or parts[0] not in {"academic", "admin", "teaching", "business"} or parts[1] != ("raw" if raw else "wiki") or ".." in parts:
        raise ValueError(f"unmanaged public path: {value}")
    target = (repo / value).resolve()
    target.relative_to(repo)
    return target


def _bundle(repo: Path, value: str) -> Path:
    path = (repo / value).resolve()
    path.relative_to((repo / "temp/meeting-repair").resolve())
    if not (path / "confirmation.json").is_file():
        raise ValueError("prepared confirmation is missing")
    return path


def _source_mentions(mention: str, source: str) -> list[str]:
    values = [mention] if mention in source else mention.split("/")
    if len(set(values)) != len(values) or any(not v or v != v.strip() or v not in source for v in values):
        raise ValueError(f"mention has no exact source binding: {mention}")
    return values


def _replace_text(text: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return text
    pattern = "|".join(re.escape(v) for v in sorted(mapping, key=len, reverse=True))
    return re.sub(pattern, lambda match: mapping[match[0]], text)


def _replace_prose(text: str, mapping: dict[str, str]) -> str:
    """Change visible names without changing source addresses or link targets."""
    protected = r"(\[\[[^\]]+\]\]|\[[^\]]*\]\([^)]*\)|`[^`]*`)"
    parts = re.split(protected, text)
    for index, part in enumerate(parts):
        if index % 2 == 0:
            parts[index] = _replace_text(part, mapping)
        elif part.startswith("[[") and "|" in part:
            target, label = part[2:-2].split("|", 1)
            parts[index] = "[[" + target + "|" + _replace_text(label, mapping) + "]]"
        elif part.startswith("[") and not part.startswith("[["):
            label, target = part[1:].split("](", 1)
            parts[index] = "[" + _replace_text(label, mapping) + "](" + target
    return "".join(parts)


def _people_draft(repo: Path, canonical: str, label: str, when: str,
                  source: str, quote: str) -> dict:
    path = _public_path(repo, canonical + ".md")
    if path.exists():
        fm, _, body = ingest_check.parse_frontmatter(path.read_text())
        if not isinstance(fm, dict) or fm.get("type") != "people" or fm.get("title") != label:
            raise ValueError(f"people page conflicts with confirmed label: {canonical}")
        sources = fm.get("sources", [])
        if not isinstance(sources, list):
            raise ValueError(f"people sources must be a list: {canonical}")
        fm["sources"] = list(dict.fromkeys([*sources, RAW_FACTS]))
        fm["updated"] = when
    else:
        fm = {"title": label, "type": "people", "sources": [RAW_FACTS],
              "source_type": "user-assertion", "date": when, "confidence": "medium",
              "status": "current", "created": when, "updated": when}
        body = f"# {label}\n\n## Navigation\n\n{label}的会议姓名确认入口。\n\n## Content\n"
    footnote = "identity-" + source.split("#", 1)[1]
    body = body.rstrip() + f"\n\n### 姓名确认 {when} {footnote[-8:]}\n\n{quote}[^{footnote}]\n\n[^{footnote}]: {source}\n"
    content = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + body.lstrip()
    return {"page": str(path.relative_to(repo)), "before_sha256": _hash(path), "content": content}


def _task(repo: Path, folder: Path, manifest: dict) -> dict:
    relative = str(folder.relative_to(repo))
    command = ("python3 .scripts/repair_meeting_identity.py --transaction-id "
               + shlex.quote(manifest["transaction_id"]) + " --apply-confirmation " + shlex.quote(relative))
    task = agent_task.make_task(
        kind="meeting_identity_confirmation", transaction_id=manifest["transaction_id"],
        inputs=[{"name": "confirmation", "path": relative + "/confirmation.json",
                 "role": "user_confirmation_and_frozen_inputs", "read": "full"}],
        outputs=[{"name": "wiki_draft", "path": relative + "/wiki.md", "format": "markdown"}],
        protocol={"name": SCHEMA, "validator": "meeting_identity_confirmation.apply"},
        commands={"commit": command},
        context={"correction_count": len(manifest["corrections"]),
                 "review": "复核已预填的局部姓名修改；移除已被本次确认取代的身份待确认表述，保留其他内容、来源限制与引用。IR、人物页和引用已机械生成。"},
    )
    return {"status": "prepared", "schema": SCHEMA, "agent_task": task,
            "bundle": relative, "correction_count": len(manifest["corrections"]),
            "next_command": command}


def prepare(transaction_id: str, input_path: Path) -> dict:
    repo = repair.REPO.resolve()
    supplied = _json(input_path)
    if not isinstance(supplied, dict) or set(supplied) != {"schema", "date", "statement", "corrections"} or supplied["schema"] != SCHEMA:
        raise ValueError("confirmation requires schema, date, statement and corrections")
    when = supplied["date"]
    if not isinstance(when, str) or date.fromisoformat(when).isoformat() != when:
        raise ValueError("confirmation date must be YYYY-MM-DD")
    if not isinstance(supplied["statement"], str) or not supplied["statement"].strip() or len(supplied["statement"]) > 12000:
        raise ValueError("statement must preserve the user's confirmation, at most 12000 characters")
    rows = supplied["corrections"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("corrections must contain 1 to 16 mappings")
    state = _state(repo, transaction_id)
    raw = _public_path(repo, f"{state['raw_dir']}/{state['source_filename']}", raw=True)
    wiki = _public_path(repo, state["wiki_path"] + ".md")
    resolution_path = raw.parent / "entity-resolution.json"
    source_text = raw.read_text()
    resolution = _json(resolution_path)
    seen, people, old_targets = set(), set(), {}
    mapping, decisions = {}, []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"mention", "label", "canonical"} or any(not isinstance(v, str) or not v.strip() for v in row.values()):
            raise ValueError("each correction requires mention, label and canonical strings")
        if len(row["label"]) > 100 or any(c in row["label"] for c in "\n\r[]{}<>|"):
            raise ValueError("label must be a plain person name")
        if not re.fullmatch(r"academic/wiki/authors/[\w-]+", row["canonical"]):
            raise ValueError("canonical must be an explicit academic/wiki/authors/<id> path")
        mentions = _source_mentions(row["mention"], source_text)
        if seen.intersection(mentions) or row["canonical"] in people:
            raise ValueError("overlapping mentions or repeated target; group spellings in one correction")
        found = [item for item in resolution.get("compiler_entity_resolutions", []) if item["mention"] == row["mention"]]
        if len(found) != 1:
            raise ValueError("each correction must select one existing compiler identity decision")
        seen.update(mentions)
        people.add(row["canonical"])
        mapping.update({mention: row["label"] for mention in mentions})
        decisions.append(found[0])
        if found[0].get("canonical"):
            old = found[0]["canonical"]
            if any(item.get("canonical") == old and item["mention"] != row["mention"] for item in resolution["compiler_entity_resolutions"]):
                raise ValueError("existing canonical identity is shared; use explicit projection repair")
            old_targets[old] = row["canonical"]
    digest = hashlib.sha256(transaction_id.encode() + b"\0" + repair._json_bytes(supplied)).hexdigest()[:16]
    folder = repo / "temp/meeting-repair" / f"{transaction_id}-confirmation-{digest}"
    if (folder / "confirmation.json").exists():
        return _task(repo, folder, _json(folder / "confirmation.json"))
    if all(d.get("status") == "resolved" and d.get("canonical") == r["canonical"] for d, r in zip(decisions, rows)):
        for row in rows:
            page = _public_path(repo, row["canonical"] + ".md")
            fm, _, _ = ingest_check.parse_frontmatter(page.read_text())
            if not isinstance(fm, dict) or fm.get("title") != row["label"] or fm.get("type") != "people":
                raise ValueError("resolved identity conflicts with confirmed people page")
        return {"status": "no_action", "reason": "selected identities already resolved", "correction_count": len(rows)}
    folder.mkdir(parents=True, exist_ok=True)
    facts, corrections, updates, relations = [], [], [], []
    for index, row in enumerate(rows):
        fact_id = f"fact-meeting-name-{digest}-{index + 1}"
        quote = (f"针对会议 {state['meeting_id']}，用户确认原文提及“{row['mention']}”对应姓名“{row['label']}”。"
                 f"用户原话（JSON 字符串）：{json.dumps(supplied['statement'], ensure_ascii=False)}")
        source = f"{RAW_FACTS}#{fact_id}"
        facts.append(f"- [{when}] **{quote}** {{: #{fact_id}}}")
        corrections.append({**row, "source": source, "quote": quote})
        updates.append(_people_draft(repo, row["canonical"], row["label"], when, source, quote))
        relations.append({"page": updates[-1]["page"], "fact_id": fact_id,
                          "subject": state["wiki_path"], "predicate": "涉及", "object": row["canonical"],
                          "confidence": "[可追溯]"})
    pending = folder / "facts-pending.md"
    repair._atomic_bytes(pending, ("\n\n".join(facts) + "\n").encode())
    prepared = assertions.prepare_transaction(path=pending, repo=repo)
    assertion_manifest = _json(repo / prepared["manifest_path"])
    proposal = {"schema": assertions.PROPOSAL_SCHEMA, "transaction_id": prepared["transaction_id"],
                "handled_fact_ids": [fact["fact_id"] for fact in assertion_manifest["facts"]],
                "wiki_updates": updates, "relations": relations}
    assertions._validate_proposal(repo, assertion_manifest, proposal)
    _write(repo / prepared["write_to"], proposal)
    ir = json.loads(json.dumps(state["meeting_ir_content"]))
    for section, endpoint, label in (("attendees", "person", "label"), ("reports", "person", "person_label"), ("tasks", "assignee", "assignee_label")):
        for item in ir[section]:
            for row, decision in zip(rows, decisions):
                if item[label] in [row["mention"], *_source_mentions(row["mention"], source_text)] or (decision.get("canonical") and item[endpoint] == decision["canonical"]):
                    item[endpoint], item[label] = row["canonical"], row["label"]
    for section, items in ir.items():
        if not isinstance(items, list):
            continue
        for item in items:
            for key in ("text", "topic", "label", "person_label", "assignee_label"):
                if key in item:
                    item[key] = _replace_text(item[key], mapping)
            if section == "relations":
                for key in ("subject", "object"):
                    item[key] = old_targets.get(item[key], item[key])
    original_wiki = wiki.read_text()
    fm, _, body = ingest_check.parse_frontmatter(original_wiki)
    fm["updated"] = when
    # Preserve source addresses and footnote definitions. The host reviews prose.
    body = "\n".join(line if re.match(r"\s*\[\^[^]]+\]:", line) else _replace_prose(line, mapping) for line in body.splitlines())
    note = f"\n\n### 姓名确认 {when}\n\n"
    for index, row in enumerate(corrections):
        note += f"- 原文“{row['mention']}”按用户确认记为 [[authors/{row['canonical'].split('/')[-1]}|{row['label']}]]。[^name-{digest}-{index}]\n"
    note += "\n" + "\n".join(f"[^name-{digest}-{i}]: {row['source']}" for i, row in enumerate(corrections)) + "\n"
    body = body.replace("<!-- meeting-navigation:start -->", note + "\n<!-- meeting-navigation:start -->", 1) if "<!-- meeting-navigation:start -->" in body else body + note
    draft = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n" + body.lstrip()
    _write(folder / "meeting-ir.json", ir)
    _write(folder / "identity-corrections.json", corrections)
    repair._atomic_bytes(folder / "wiki.md", draft.encode())
    manifest = {"schema": SCHEMA, "transaction_id": transaction_id, "input": supplied,
                "corrections": corrections, "assertion_transaction": prepared["transaction_id"],
                "assertion_manifest": prepared["manifest_path"], "assertion_proposal": prepared["write_to"],
                "input_hashes": {str(p.relative_to(repo)): _hash(p) for p in (raw, wiki, resolution_path)},
                "ir_before": hashlib.sha256(repair._json_bytes(state["meeting_ir_content"])).hexdigest(),
                "generated_hashes": {str(p.relative_to(repo)): _hash(p) for p in (folder / "meeting-ir.json", folder / "identity-corrections.json", repo / prepared["write_to"])}}
    _write(folder / "confirmation.json", manifest)
    return _task(repo, folder, manifest)


def _preflight(repo: Path, folder: Path, manifest: dict, state: dict) -> None:
    for name, digest in manifest["generated_hashes"].items():
        if _hash(repo / name) != digest:
            raise ValueError(f"generated confirmation input changed: {name}")
    for name, digest in manifest["input_hashes"].items():
        if _hash(repo / name) != digest:
            raise ValueError(f"meeting changed after preparation: {name}")
    if hashlib.sha256(repair._json_bytes(state["meeting_ir_content"])).hexdigest() != manifest["ir_before"]:
        raise ValueError("meeting IR changed after preparation")
    wiki = (folder / "wiki.md").read_text()
    fm, _, _ = ingest_check.parse_frontmatter(wiki)
    errors, _ = ingest_check.check_frontmatter(fm, state["wiki_path"] + ".md")
    expected_source = f"{state['raw_dir']}/{state['source_filename']}"
    if isinstance(fm, dict) and fm.get("sources") != [expected_source]:
        errors.append("meeting sources must retain exactly the original Raw path")
    source_text = (repo / expected_source).read_text()
    catalog, _ = repair.meeting._write_evidence_catalog(state, source_text)
    ir = _json(folder / "meeting-ir.json")
    errors.extend(repair.validate_meeting_ir(ir, {item['evidence_id'] for item in catalog}, evidence_catalog=catalog))
    errors.extend(repair.guidance.validate_person_updates(
        ir.get("person_updates", []), repair.guidance.discover_student_records(repo)))
    for heading in ("## Navigation", "## Content"):
        if heading not in wiki:
            errors.append(f"missing {heading}")
    for correction in manifest["corrections"]:
        if correction["source"] not in wiki:
            errors.append("meeting draft lacks confirmation citation")
    staged_raw = folder / "assertions-preview.md"
    archive = repo / RAW_FACTS
    projected = (archive.read_text() if archive.exists() else "") + "\n" + (folder / "facts-pending.md").read_text()
    repair._atomic_bytes(staged_raw, projected.encode())
    if not errors:
        projected_wiki = folder / "wiki-preview.md"
        compiled = repair.meeting.compile_meeting_navigation(
            wiki, ir, raw_source=expected_source, date=state["date"], catalog=catalog)
        repair._atomic_bytes(projected_wiki, compiled.encode())
        errors.extend(repair.meeting.wl.validate_wiki_page(projected_wiki, require_citations=True,
            base_path=repo / (state["wiki_path"] + ".md"), raw_overrides={RAW_FACTS: staged_raw}))
    if errors:
        raise ValueError("confirmation preflight failed: " + "; ".join(errors[:12]))


def _completed_repair(state: dict, corrections: list[dict]) -> dict | None:
    for audit in reversed(state.get("managed_repairs", [])):
        recorded = audit.get("identity_evidence", [])
        if len(recorded) == len(corrections) and all(all(a.get(k) == b[k] for k in b) for a, b in zip(recorded, corrections)):
            receipt = repair.meeting.inbox_state.verify_receipt(state, repair.REPO)
            if receipt["status"] != "PASS":
                raise ValueError("completed correction artifacts changed; explicit repair required")
            source = repair.REPO / state["raw_dir"] / state["source_filename"]
            resolution = _json(source.parent / "entity-resolution.json")
            for correction in corrections:
                matches = [row for row in resolution.get("compiler_entity_resolutions", [])
                           if row.get("mention") == correction["mention"]]
                if len(matches) != 1 or matches[0].get("status") != "resolved" or matches[0].get("canonical") != correction["canonical"]:
                    raise ValueError("completed correction was superseded; explicit repair required")
            # Reuse the existing evidence check: later appended facts are allowed,
            # but the cited assertion and confirmed people page must still agree.
            repair._apply_identity_corrections(resolution, source.read_text(), corrections)
            report = f"cross-domain/ingest-reports/{audit['repair_id']}.json"
            result = {"status": "completed", **audit,
                      "verification_receipt": receipt, "quality_warnings": state.get("quality_warnings", []),
                      "quality_status": state.get("quality_status"), "graph_report": state.get("graph_report", {}),
                      "student_guidance_report": state.get("student_guidance_report", {})}
            if not (repair.REPO / report).exists():
                _write(repair.REPO / report, result)
            return {**result, "report": report}
    return None


def apply(transaction_id: str, bundle_path: str) -> dict:
    repo = repair.REPO.resolve()
    folder = _bundle(repo, bundle_path)
    manifest = _json(folder / "confirmation.json")
    if manifest.get("schema") != SCHEMA or manifest.get("transaction_id") != transaction_id:
        raise ValueError("confirmation bundle belongs to a different meeting")
    state = _state(repo, transaction_id)
    result = _completed_repair(state, manifest["corrections"])
    if result is None:
        _preflight(repo, folder, manifest, state)
        receipt_path = repo / "temp/user-assertions" / manifest["assertion_transaction"] / "receipt.json"
        archived = _json(receipt_path) if receipt_path.exists() else {}
        if archived.get("status") != "completed":
            archived = assertions.apply_transaction(manifest["assertion_transaction"], repo=repo)
            if archived.get("status") != "completed":
                return {"status": "failed", "phase": "confirmation_archive", "retryable": True,
                        "errors": archived.get("errors", []), "receipt_path": str(receipt_path.relative_to(repo))}
        args = argparse.Namespace(transaction_id=transaction_id, mention=None, canonical_label=None,
                                  canonical_entity=None, updates_file=None, meeting_ir_patch_file=None,
                                  meeting_ir_file=str((folder / "meeting-ir.json").relative_to(repo)),
                                  wiki_file=str((folder / "wiki.md").relative_to(repo)),
                                  identity_corrections_file=str((folder / "identity-corrections.json").relative_to(repo)), apply=True)
        try:
            result = repair.repair(args)
        except Exception as exc:
            return {"status": "failed", "phase": "meeting_repair", "confirmation_archived": True,
                    "retryable": True, "errors": [str(exc)], "bundle": bundle_path}
    current = _state(repo, transaction_id)
    report = current.get("maintenance", {}).get("report_path")
    publication = {"status": "not_applicable"}
    if report:
        try:
            import ingest_inbox
            refreshed = ingest_inbox.reconcile_maintenance_report(repo / report)
            publication = refreshed.get("maintenance", {}).get("publication", {})
            if publication.get("status") != "completed":
                raise RuntimeError("maintenance report publication incomplete")
        except Exception as exc:
            return {"status": "pending", "phase": "report_publication", "meeting_repaired": True,
                    "retryable": True, "errors": [str(exc)], "bundle": bundle_path}
    summary = {"status": "completed", "schema": SCHEMA, "transaction_id": transaction_id,
               "correction_count": len(manifest["corrections"]), "verification": result['verification_receipt']['status'],
               "quality_warning_count": len(result.get("quality_warnings", [])),
               "report": result["report"], "report_publication": publication,
               "confirmation_receipt": f"temp/user-assertions/{manifest['assertion_transaction']}/receipt.json",
               "semantic_llm_calls": 0}
    _write(folder / "result.json", summary)
    return summary

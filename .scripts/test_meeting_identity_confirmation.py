#!/usr/bin/env python3
"""Small isolated regressions for confirmation composition and durable retries."""
from __future__ import annotations

import json
import re
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import ingest_inbox
import meeting_identity_confirmation as confirmation
from test_ingest_user_assertions import _install_graph_runtime, _initialize_graph
from meeting_compiler_contract import PROTOCOL_VERSION

repair = confirmation.repair
TXN = "test-name-confirmation"
PAGE = "academic/wiki/conferences/test-names"
RAW = "academic/raw/conferences/2026/test-names/近名甲.txt"
NAMES = ["近名甲/近名乙", "另一姓名", "未确认者"]


def write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_json(root, relative, value):
    return write(root, relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory).resolve()
        for module in (repair, repair.meeting, repair.meeting.inbox_state,
                       repair.meeting.wl, repair.source_locator, repair.gl,
                       confirmation.ingest_check, ingest_inbox):
            stack.enter_context(patch.object(module, "REPO", root))
        stack.enter_context(patch.object(repair.gl, "GRAPH_DB", root / "cross-domain/graph.db"))
        _install_graph_runtime(root)
        graph = root / "cross-domain/graph.db"
        graph.parent.mkdir(exist_ok=True)
        _initialize_graph(root, graph)
        write(root, "inbox/facts-pending.md", "# Unrelated pending facts\n")
        write(root, confirmation.RAW_FACTS, "# Assertions\n")
        source = "近名甲讨论知识库，近名乙补充。另一姓名负责验证；未确认者待确认。\n"
        write(root, RAW, source)
        write(root, str(Path(RAW).parent / "corrected.txt"), source)
        ir = {"protocol_version": PROTOCOL_VERSION,
              "attendees": [{"person": "", "label": name, "evidence_ids": ["s0001"]} for name in NAMES],
              "topics": [{"label": "知识库", "predicate": "讨论", "evidence_ids": ["s0001"]}],
              "reports": [{"person": "", "person_label": "近名甲", "topic": "知识库", "evidence_ids": ["s0001"]}],
              "decisions": [], "tasks": [{"text": "验证知识库", "assignee": "", "assignee_label": "另一姓名", "evidence_ids": ["s0001"]}],
              "relations": [], "person_updates": []}
        resolution = {"resolved": [], "compiler_entity_resolutions": [
            {"mention": name, "canonical": "", "status": "unresolved", "reason": "待确认"} for name in NAMES]}
        state = {"transaction_id": TXN, "status": "completed", "meeting_id": "test-names",
                 "date": "2026-11-03", "subproject": "academic", "source": RAW,
                 "source_filename": Path(RAW).name, "raw_dir": str(Path(RAW).parent),
                 "wiki_path": PAGE, "meeting_ir_content": ir,
                 "extract_dir": "temp/inbox-extract/test-names", "errors": [],
                 "meeting_ir": "temp/inbox-extract/test-names/meeting-ir.json",
                 "entity_resolution": "temp/inbox-extract/test-names/entity-resolution.json",
                 "semantic_path": "temp/inbox-state/test-name-confirmation-semantic.txt",
                 "log_path": "academic/wiki/log.md",
                 "quality_warnings": [{"issue": "meeting_entity_unresolved", "mention": name} for name in NAMES],
                 "maintenance": {"report_path": "cross-domain/ingest-reports/batch.json"}}
        write_json(root, str(Path(RAW).parent / "entity-resolution.json"), resolution)
        write_json(root, state["entity_resolution"], resolution)
        write_json(root, state["meeting_ir"], ir)
        write(root, state["semantic_path"], repair.meeting.compile_meeting_slots(ir))
        catalog, _ = repair.meeting._write_evidence_catalog(state, source)
        wiki = ("---\ntitle: 姓名确认测试\ntype: conference-summary\nsources:\n  - " + RAW + "\n"
                "source_type: speech-recognition\ndate: 2026-11-03\nconfidence: low\nstatus: current\n"
                "created: 2026-11-03\nupdated: 2026-11-03\n---\n# 姓名确认测试\n"
                "## Navigation\n讨论知识库。\n## Content\n近名甲与另一姓名讨论知识库。[^original]\n"
                "[近名甲转写](" + RAW + ")\n\n[^original]: " + RAW + "#L1\n")
        wiki = repair.meeting.compile_meeting_navigation(wiki, ir, raw_source=RAW, date=state["date"], catalog=catalog)
        write(root, PAGE + ".md", wiki)
        write(root, state["log_path"], "# Log\n")
        state["verification_receipt"] = repair.meeting.inbox_state.build_verification_receipt(state, root)
        write_json(root, "temp/inbox-state/" + TXN + ".json", state)
        maintenance = {"status": "completed", "components": {}, "actions": [], "errors": [],
                       "receipt_path": "temp/inbox-maintenance/batch.json"}
        write_json(root, maintenance["receipt_path"], maintenance)
        write_json(root, state["maintenance"]["report_path"], {
            "session_id": "batch", "total": 1, "completed": 1, "failed": 0, "skipped": 0, "maintenance": maintenance,
            "files": [{"file": "近名甲.txt", "status": "completed", "transaction_id": TXN,
                       "wiki_path": PAGE, "quality_warnings": state["quality_warnings"]}]})
        supplied = {"schema": confirmation.SCHEMA, "date": "2026-11-03", "statement": "近名甲和近名乙是曲悦诚，另一姓名是曲锐诚。",
                    "corrections": [
                        {"mention": NAMES[0], "label": "曲悦诚", "canonical": "academic/wiki/authors/qu-yuecheng"},
                        {"mention": NAMES[1], "label": "曲锐诚", "canonical": "academic/wiki/authors/qu-ruicheng"}]}
        input_path = write_json(root, "temp/confirmation-input.json", supplied)
        yield root, state, input_path


def prepared(root, input_path):
    result = confirmation.prepare(TXN, input_path)
    return result, root / result["bundle"]


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for domain in ("academic", "cross-domain", "inbox")
            for p in (root / domain).rglob("*") if p.is_file()}


def rejected(call, message):
    try:
        call()
    except ValueError as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError("expected rejection: " + message)


def test_prepare_keeps_sources_separates_identities_and_preserves_review():
    with fixture() as (root, state, input_path):
        before = snapshot(root)
        result, folder = prepared(root, input_path)
        assert snapshot(root) == before
        assert result["agent_task"]["schema"] == "agent-task-v1"
        ir = confirmation._json(folder / "meeting-ir.json")
        assert [row["label"] for row in ir["attendees"]] == ["曲悦诚", "曲锐诚", "未确认者"]
        assert ir["attendees"][2]["person"] == ""
        assert ir["reports"][0]["person"].endswith("qu-yuecheng")
        assert ir["tasks"][0]["assignee"].endswith("qu-ruicheng")
        draft = (folder / "wiki.md").read_text()
        assert "[^original]: " + RAW + "#L1" in draft
        assert "[曲悦诚转写](" + RAW + ")" in draft
        (folder / "wiki.md").write_text(draft + "\n复核保留的说明。\n")
        assert confirmation.prepare(TXN, input_path)["bundle"] == result["bundle"]
        assert "复核保留" in (folder / "wiki.md").read_text()
        # Identical confirmations for different meetings need distinct Raw anchors.
        other_id = TXN + "-other"
        write_json(root, "temp/inbox-state/" + other_id + ".json", {**state, "transaction_id": other_id})
        other = confirmation.prepare(other_id, input_path)
        first_sources = {row["source"] for row in confirmation._json(folder / "identity-corrections.json")}
        other_sources = {row["source"] for row in confirmation._json(root / other["bundle"] / "identity-corrections.json")}
        assert first_sources.isdisjoint(other_sources)


def test_stale_and_invalid_inputs_fail_before_archive():
    with fixture() as (root, state, input_path):
        result, folder = prepared(root, input_path)
        original = (folder / "wiki.md").read_text()
        with patch.object(confirmation.assertions, "apply_transaction") as archive:
            for invalid in (re.sub(r"(?m)^date:.*\n", "", original),
                            original.replace("## Content", "## Missing")):
                before = snapshot(root)
                (folder / "wiki.md").write_text(invalid)
                rejected(lambda: confirmation.apply(TXN, result["bundle"]), "preflight")
                assert snapshot(root) == before
            (folder / "wiki.md").write_text(original)
            for target in (root / RAW, root / (PAGE + ".md"), folder / "meeting-ir.json"):
                content = target.read_bytes()
                target.write_bytes(content + b"\n")
                before = snapshot(root)
                rejected(lambda: confirmation.apply(TXN, result["bundle"]), "changed")
                assert snapshot(root) == before
                target.write_bytes(content)
            archive.assert_not_called()


def test_real_transactions_resume_at_failed_stage_and_reject_stale_completion():
    with fixture() as (root, state, input_path):
        result, folder = prepared(root, input_path)
        source_before = (root / RAW).read_bytes()
        inbox_before = (root / "inbox/facts-pending.md").read_bytes()
        # Fail during the managed repair after assertion commit, then recover it.
        with patch.object(repair.guidance, "apply_projection", return_value=(False, "injected repair failure")):
            failed = confirmation.apply(TXN, result["bundle"])
        assert failed["phase"] == "meeting_repair" and failed["confirmation_archived"], failed
        assert confirmation._state(root, TXN)["meeting_ir_content"] == state["meeting_ir_content"]
        archived = (root / confirmation.RAW_FACTS).read_bytes()
        assert len(confirmation.assertions.parse_fact_entries(archived.decode())) == 2
        with patch.object(confirmation.assertions, "apply_transaction", side_effect=AssertionError("archive duplicated")), \
             patch.object(ingest_inbox, "reconcile_maintenance_report", side_effect=OSError("injected publication failure")):
            pending = confirmation.apply(TXN, result["bundle"])
        assert pending["phase"] == "report_publication" and pending["meeting_repaired"], pending
        current = confirmation._state(root, TXN)
        assert len(current["managed_repairs"]) == 1
        assert current["quality_warnings"][0]["mention"] == "未确认者"
        assert len(current["quality_warnings"]) == 1
        # Report-only recovery must not rewrite the graph, archive or meeting.
        before_graph = (root / "cross-domain/graph.db").read_bytes()
        with patch.object(confirmation.assertions, "apply_transaction", side_effect=AssertionError("archive duplicated")), \
             patch.object(repair, "repair", side_effect=AssertionError("repair duplicated")):
            completed = confirmation.apply(TXN, result["bundle"])
            assert completed["status"] == "completed", completed
            assert confirmation.apply(TXN, result["bundle"])["status"] == "completed"
        assert completed["verification"] == "PASS" and completed["semantic_llm_calls"] == 0
        assert (root / "cross-domain/graph.db").read_bytes() == before_graph
        assert (root / confirmation.RAW_FACTS).read_bytes() == archived
        assert (root / RAW).read_bytes() == source_before
        assert (root / Path(RAW).parent / "corrected.txt").read_bytes() == source_before
        assert (root / "inbox/facts-pending.md").read_bytes() == inbox_before
        batch = confirmation._json(root / state["maintenance"]["report_path"])
        assert len(batch["files"][0]["quality_warnings"]) == 1
        # Raw assertions are outside the meeting receipt: recheck exact evidence.
        (root / confirmation.RAW_FACTS).write_text("# Removed facts\n")
        rejected(lambda: confirmation.apply(TXN, result["bundle"]), "assertion locator is missing")
        (root / confirmation.RAW_FACTS).write_bytes(archived)
        resolution_path = root / Path(RAW).parent / "entity-resolution.json"
        resolution_before = resolution_path.read_bytes()
        state_before = confirmation._state(root, TXN)
        resolution = confirmation._json(resolution_path)
        resolution["compiler_entity_resolutions"][0]["canonical"] = "academic/wiki/authors/superseded"
        write_json(root, resolution_path, resolution)
        changed_state = dict(state_before)
        changed_state["verification_receipt"] = repair.meeting.inbox_state.build_verification_receipt(changed_state, root)
        write_json(root, "temp/inbox-state/" + TXN + ".json", changed_state)
        rejected(lambda: confirmation.apply(TXN, result["bundle"]), "superseded")
        resolution_path.write_bytes(resolution_before)
        write_json(root, "temp/inbox-state/" + TXN + ".json", state_before)
        write(root, PAGE + ".md", (root / (PAGE + ".md")).read_text() + "\nchanged\n")
        rejected(lambda: confirmation.apply(TXN, result["bundle"]), "artifacts changed")


def main():
    tests = [test for name, test in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()

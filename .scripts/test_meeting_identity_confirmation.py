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
def fixture(domain="academic"):
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory).resolve()
        section = "meetings" if domain == "admin" else "conferences"
        page = f"{domain}/wiki/{section}/test-names"
        raw = f"{domain}/raw/{section}/2026/test-names/近名甲.txt"
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
        write(root, raw, source)
        write(root, str(Path(raw).parent / "corrected.txt"), source)
        ir = {"protocol_version": PROTOCOL_VERSION,
              "attendees": [{"person": "", "label": name, "evidence_ids": ["s0001"]} for name in NAMES],
              "topics": [{"label": "知识库", "predicate": "讨论", "evidence_ids": ["s0001"]}],
              "reports": [{"person": "", "person_label": "近名甲", "topic": "知识库", "evidence_ids": ["s0001"]}],
              "decisions": [], "tasks": [{"text": "验证知识库", "assignee": "", "assignee_label": "另一姓名", "evidence_ids": ["s0001"]}],
              "relations": [], "person_updates": []}
        resolution = {"resolved": [], "compiler_entity_resolutions": [
            {"mention": name, "canonical": "", "status": "unresolved", "reason": "待确认"} for name in NAMES]}
        state = {"transaction_id": TXN, "status": "completed", "meeting_id": "test-names",
                 "date": "2026-11-03", "subproject": domain, "source": raw,
                 "source_filename": Path(raw).name, "raw_dir": str(Path(raw).parent),
                 "wiki_path": page, "meeting_ir_content": ir,
                 "extract_dir": "temp/inbox-extract/test-names", "errors": [],
                 "meeting_ir": "temp/inbox-extract/test-names/meeting-ir.json",
                 "entity_resolution": "temp/inbox-extract/test-names/entity-resolution.json",
                 "semantic_path": "temp/inbox-state/test-name-confirmation-semantic.txt",
                 "log_path": f"{domain}/wiki/log.md",
                 "quality_warnings": [{"issue": "meeting_entity_unresolved", "mention": name} for name in NAMES],
                 "maintenance": {"report_path": "cross-domain/ingest-reports/batch.json"}}
        write_json(root, str(Path(raw).parent / "entity-resolution.json"), resolution)
        write_json(root, state["entity_resolution"], resolution)
        write_json(root, state["meeting_ir"], ir)
        write(root, state["semantic_path"], repair.meeting.compile_meeting_slots(ir))
        catalog, _ = repair.meeting._write_evidence_catalog(state, source)
        wiki = ("---\ntitle: 姓名确认测试\ntype: conference-summary\nsources:\n  - " + raw + "\n"
                "source_type: speech-recognition\ndate: 2026-11-03\nconfidence: low\nstatus: current\n"
                "created: 2026-11-03\nupdated: 2026-11-03\n---\n# 姓名确认测试\n"
                "## Navigation\n讨论知识库。\n## Content\n近名甲与另一姓名讨论知识库。[^original]\n"
                "[近名甲转写](" + raw + ")\n\n[^original]: " + raw + "#L1\n")
        if domain != "academic":
            wiki = wiki.replace("type: conference-summary", "type: meeting-summary").replace("status: current", "status: draft")
        wiki = repair.meeting.compile_meeting_navigation(wiki, ir, raw_source=raw, date=state["date"], catalog=catalog)
        write(root, page + ".md", wiki)
        write(root, state["log_path"], "# Log\n")
        state["verification_receipt"] = repair.meeting.inbox_state.build_verification_receipt(state, root)
        write_json(root, "temp/inbox-state/" + TXN + ".json", state)
        maintenance = {"status": "completed", "components": {}, "actions": [], "errors": [],
                       "receipt_path": "temp/inbox-maintenance/batch.json"}
        write_json(root, maintenance["receipt_path"], maintenance)
        write_json(root, state["maintenance"]["report_path"], {
            "session_id": "batch", "total": 1, "completed": 1, "failed": 0, "skipped": 0, "maintenance": maintenance,
            "files": [{"file": "近名甲.txt", "status": "completed", "transaction_id": TXN,
                       "wiki_path": page, "quality_warnings": state["quality_warnings"]}]})
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


def test_tentative_identity_stays_unresolved_and_explicit_nickname_is_reused():
    import speech_entity_resolver as resolver
    with fixture() as (root, state, input_path):
        data = confirmation._json(input_path)
        data["statement"] = "近名甲和近名乙应该是曲悦诚，另一姓名是曲锐诚。"
        data["corrections"][0].update(certainty="tentative", statement="近名甲和近名乙应该是曲悦诚")
        data["corrections"][1].update(certainty="confirmed", statement="另一姓名是曲锐诚", nickname="另一姓名")
        write_json(root, str(input_path.relative_to(root)), data)
        result, folder = prepared(root, input_path)
        manifest = confirmation._json(folder / "confirmation.json")
        proposal = confirmation._json(root / manifest["assertion_proposal"])
        assert "另一姓名" not in proposal["wiki_updates"][0]["content"]
        assert "近名甲" not in proposal["wiki_updates"][1]["content"]
        archived_draft = (folder / "facts-pending.md").read_text()
        assert archived_draft.count("用户完整原话") == 1
        assert "应该是" in proposal["wiki_updates"][0]["content"]
        completed = confirmation.apply(TXN, result["bundle"])
        assert completed["status"] == "completed", completed
        current = confirmation._state(root, TXN)
        assert current["meeting_ir_content"]["reports"][0]["person"] == ""
        assert current["meeting_ir_content"]["tasks"][0]["assignee"].endswith("qu-ruicheng")
        resolution = confirmation._json(root / Path(RAW).parent / "entity-resolution.json")
        decision = resolution["compiler_entity_resolutions"][0]
        assert decision["status"] == "unresolved" and decision["canonical"] == ""
        assert decision["candidate_canonical"].endswith("qu-yuecheng")
        assert confirmation.apply(TXN, result["bundle"])["status"] == "completed"
        with patch.object(resolver, "DB", root / "cross-domain/graph.db"):
            _, aliases, _, _ = resolver._read_people_from_db()
        assert aliases["另一姓名"].endswith("qu-ruicheng")
        assert "近名甲" not in aliases and "近名乙" not in aliases
        # A second explicit target makes the alias ambiguous, never last-write-wins.
        conn = repair.gl.connect()
        try:
            repair.gl.insert_aliases(conn, "academic/wiki/authors/qu-yuecheng", ["另一姓名"])
            conn.commit()
        finally:
            conn.close()
        with patch.object(resolver, "DB", root / "cross-domain/graph.db"):
            assert "另一姓名" not in resolver._read_people_from_db()[1]


def test_archive_refresh_is_explicit_append_only_and_preserves_review():
    with fixture() as (root, state, input_path):
        result, folder = prepared(root, input_path)
        draft = (folder / "wiki.md").read_text() + "\n宿主已复核说明。\n"
        (folder / "wiki.md").write_text(draft)
        raw = root / confirmation.RAW_FACTS
        original = raw.read_text()
        appended = original + "\n- [2026-11-03] **其他用户事实** {: #fact-other}\n"
        raw.write_text(appended)
        rejected(lambda: confirmation.apply(TXN, result["bundle"]), "changed after")
        assert confirmation.refresh(TXN, result["bundle"])["archive_refresh_count"] == 1
        assert (folder / "wiki.md").read_text() == draft
        assert confirmation.refresh(TXN, result["bundle"])["archive_refresh_count"] == 1
        raw.write_text(appended.replace("其他用户事实", "修改历史事实"))
        rejected(lambda: confirmation.refresh(TXN, result["bundle"]), "frozen prefix")
        raw.write_text(appended)
        target = root / (confirmation._json(input_path)["corrections"][0]["canonical"] + ".md")
        target_before = target.read_bytes() if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("concurrent page edit")
        rejected(lambda: confirmation.refresh(TXN, result["bundle"]), "changed after")
        if target_before is None:
            target.unlink()
        else:
            target.write_bytes(target_before)
        assert confirmation.apply(TXN, result["bundle"])["status"] == "completed"
        assert "fact-other" in raw.read_text()


def test_tentative_update_retracts_prior_role_projection_without_editing_raw():
    with fixture() as (root, state, input_path):
        result, _ = prepared(root, input_path)
        assert confirmation.apply(TXN, result["bundle"])["status"] == "completed"
        raw_before = (root / RAW).read_bytes()
        wiki_before = (root / (PAGE + ".md")).read_text()
        history = wiki_before[wiki_before.index("### 姓名确认 "):wiki_before.index("<!-- meeting-navigation:start -->")].strip()
        data = confirmation._json(input_path)
        data["statement"] = "近名甲和近名乙应该是曲悦诚，另一姓名是曲锐诚。"
        data["corrections"] = [data["corrections"][0] | {
            "certainty": "tentative", "statement": "近名甲和近名乙应该是曲悦诚"}]
        write_json(root, str(input_path.relative_to(root)), data)
        result, folder = prepared(root, input_path)
        assert history in (folder / "wiki.md").read_text()
        completed = confirmation.apply(TXN, result["bundle"])
        assert completed["status"] == "completed", completed
        current = confirmation._state(root, TXN)
        assert current["meeting_ir_content"]["reports"][0]["person"] == ""
        assert current["meeting_ir_content"]["reports"][0]["person_label"] == NAMES[0]
        assert (root / RAW).read_bytes() == raw_before
        assert confirmation.apply(TXN, result["bundle"])["status"] == "completed"


def test_four_confirmations_prepare_and_commit_in_dependency_order():
    with fixture() as (root, original, input_path):
        base_wiki = (root / (PAGE + ".md")).read_text()
        base_resolution = confirmation._json(root / Path(RAW).parent / "entity-resolution.json")
        source = (root / RAW).read_bytes()
        for index in range(4):
            state = json.loads(json.dumps(original))
            tid = TXN + f"-{index}"
            raw = RAW.replace("/test-names/", f"/test-names-{index}/")
            page = PAGE + f"-{index}"
            state.update(transaction_id=tid, raw_dir=str(Path(raw).parent), source=raw,
                         wiki_path=page, extract_dir=f"temp/inbox-extract/{tid}", maintenance={})
            state["semantic_path"] = f"temp/inbox-state/{tid}-semantic.txt"
            write(root, raw, source.decode())
            write(root, str(Path(raw).parent / "corrected.txt"), source.decode())
            write_json(root, str(Path(raw).parent / "entity-resolution.json"), base_resolution)
            write(root, page + ".md", base_wiki.replace(RAW, raw))
            write(root, state["semantic_path"], repair.meeting.compile_meeting_slots(state["meeting_ir_content"]))
            state["verification_receipt"] = repair.meeting.inbox_state.build_verification_receipt(state, root)
            write_json(root, f"temp/inbox-state/{tid}.json", state)
            prepared = confirmation.prepare(tid, input_path)
            result = confirmation.apply(tid, prepared["bundle"])
            assert result["status"] == "completed", result
            assert confirmation.apply(tid, prepared["bundle"])["status"] == "completed"
            assert (root / raw).read_bytes() == source
        assert len(confirmation.assertions.parse_fact_entries((root / confirmation.RAW_FACTS).read_text())) == 8


def test_admin_and_business_confirmation_use_domain_schema_and_keep_raw():
    for domain in ("admin", "business"):
        with fixture(domain) as (root, state, input_path):
            source = root / state["source"]
            original = source.read_bytes()
            prepared_result, folder = prepared(root, input_path)
            result = confirmation.apply(TXN, prepared_result["bundle"])
            assert result["status"] == "completed", (domain, result)
            assert result["verification"] == "PASS"
            current = confirmation._state(root, TXN)
            wiki = (root / (current["wiki_path"] + ".md")).read_text()
            fm, _, _ = confirmation.ingest_check.parse_frontmatter(wiki)
            assert fm["type"] == "meeting-summary" and fm["status"] == "draft"
            assert current["meeting_ir_content"]["attendees"][0]["person"].endswith("qu-yuecheng")
            assert len(current["quality_warnings"]) == 1
            assert source.read_bytes() == original
            assert confirmation.apply(TXN, prepared_result["bundle"])["status"] == "completed"


def main():
    tests = [test for name, test in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    main()

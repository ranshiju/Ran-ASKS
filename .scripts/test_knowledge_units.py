#!/usr/bin/env python3
"""Small isolated lifecycle tests; no real model, parser or network calls."""
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import source_ingest as si
import source_locator as sl
import knowledge_store as ks
import knowledge_compile as kc
import knowledge_compile_api as ka
import query_actions as qa
import wg


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("offline tests")))
        for module,name in ((qa,"_REPO"),(sl,"REPO"),(wg,"REPO")):
            self.stack.enter_context(patch.object(module,name,self.root))
        self.source = self.receive("first.md", "# Conditional algorithm\nUnder condition A, method X may improve efficiency.\n")

    def receive(self, name, text):
        path = self.root/name; path.write_text(text)
        return si.ingest(path, repo=self.root)

    def prepare(self, source=None, **kw):
        task = kc.prepare((source or self.source)["source_id"],repo=self.root,**kw)
        return task, kc.load_job(self.root,task["job_id"])[1]

    def op(self, packet, **kw):
        return {"id":"item-1","action":"create","kind":"statement","title":"Conditional algorithm",
            "statement":"Under condition A, method X may improve efficiency.", "claim_mode":"conjecture",
            "reason":"Preserve the conditional, uncertain source claim", "unresolved":["Generality unknown"],
            "evidence":[{"block_id":packet["blocks"][0]["id"],"relation":"supports"}], **kw}

    def stage(self, task, ops, unresolved=None):
        si.atomic_json(kc.job_dir(self.root,task["job_id"])/"delta.json",
            {"schema":kc.PROTOCOL,"job_id":task["job_id"],"operations":ops,"unresolved":unresolved or []})

    def create(self):
        task,packet=self.prepare(); self.stage(task,[self.op(packet)])
        result=kc.apply(task["job_id"],repo=self.root)
        return result["results"]["item-1"]["id"],task

    def host_call(self, task, action="inspect", command="read"):
        output = io.StringIO()
        with redirect_stdout(output):
            wg.cmd_task(SimpleNamespace(transaction_id=task["job_id"],
                                        task_action=action, task_command=command))
        return json.loads(output.getvalue())

    def host_executor(self, calls):
        def execute(state, action):
            calls.append(action)
            result = {"check": kc.check, "commit": kc.apply}[action](state["job_id"], repo=self.root)
            return {"action": action, "returncode": int(result.get("ready") is False or result.get("status") == "partial"),
                    "command": [], "environment_overrides": [], "result": result, "error": ""}
        return execute

    def test_host_task_discovers_knowledge_without_duplicate_inbox_state(self):
        task, _ = self.prepare()
        inspected = self.host_call(task)
        self.assertTrue(inspected["ok"], inspected)
        self.assertEqual(inspected["result"]["kind"], "knowledge_delta")
        self.assertEqual(inspected["result"]["next_action"], "write_outputs")
        self.assertEqual(self.host_call(task, "next")["result"]["next_action"], "write_outputs")
        with patch.object(wg, "_run_task_action", side_effect=AssertionError("missing output must not execute")):
            self.assertFalse(self.host_call(task, "advance")["ok"])
        self.assertFalse((self.root / "temp/inbox-state").exists())

    def test_host_task_checks_commits_and_reopens_completed_job_without_output(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet)])
        calls = []
        with patch.object(wg, "_run_task_action", side_effect=self.host_executor(calls)):
            advanced = self.host_call(task, "advance")
        self.assertTrue(advanced["ok"], advanced)
        self.assertEqual(calls, ["check", "commit"])
        control = advanced["result"]["control"]
        self.assertEqual(control["workflow_status"], "completed")
        self.assertEqual(control["next_action"], "none")
        uid = control["results"]["item-1"]["id"]
        self.assertEqual(ks.read(uid, repo=self.root)["statement"], self.op(packet)["statement"])
        (kc.job_dir(self.root, task["job_id"])/"delta.json").unlink()
        with patch.object(wg, "_run_task_action", side_effect=AssertionError("completed work must not rerun")):
            self.assertTrue(self.host_call(task, "advance")["ok"])
            self.assertEqual(self.host_call(task, "status")["result"]["next_action"], "none")

    def test_host_task_rejected_evidence_never_commits(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet, evidence=[{"block_id": "B-invalid", "relation": "supports"}])])
        calls = []
        with patch.object(wg, "_run_task_action", side_effect=self.host_executor(calls)):
            result = self.host_call(task, "advance")
        self.assertEqual(calls, ["check"])
        self.assertEqual(result["result"]["next_action"], "repair_outputs")
        checked = result["result"]["executions"][0]["result"]
        self.assertFalse(checked["ready"])
        self.assertNotEqual(checked["workflow_status"], "ready_to_commit")
        self.assertFalse(list((self.root / "academic/knowledge/units").glob("*.md")))

    def test_host_task_partial_preserves_saved_work_and_unresolved_scope(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet)], unresolved=["Remaining source not read"])
        calls = []
        with patch.object(wg, "_run_task_action", side_effect=self.host_executor(calls)):
            result = self.host_call(task, "advance")
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["executions"][-1]["returncode"], 1)
        control = result["result"]["control"]
        self.assertEqual(control["internal_status"], "partial")
        self.assertEqual(control["next_action"], "review_partial")
        self.assertEqual(control["unresolved"], ["Remaining source not read"])
        self.assertEqual(control["results"]["item-1"]["status"], "saved")
        self.assertEqual(si.inspect_source(self.source["source_id"], repo=self.root)["status"], "source_ready")

    def test_host_partial_with_failed_commit_remains_error(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet)])
        with patch.object(wg, "_run_task_action", side_effect=self.host_executor([])), \
             patch.object(ks, "commit", side_effect=ValueError("publication failed")):
            result = self.host_call(task, "advance")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        control = result["result"]["control"]
        self.assertEqual(control["internal_status"], "partial")
        self.assertEqual(control["results"]["item-1"]["status"], "failed")

    def test_host_partial_does_not_hide_other_process_failure(self):
        task, packet = self.prepare()
        self.stage(task, [], unresolved=["Unread scope"])
        execute = self.host_executor([])
        def action(state, name):
            result = execute(state, name)
            if name == "commit":
                result["returncode"] = 2
            return result
        with patch.object(wg, "_run_task_action", side_effect=action):
            result = self.host_call(task, "advance")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_host_task_rejects_api_ownership_and_tampered_packet(self):
        task, _ = self.prepare(executor="api:configured-model")
        with patch.object(ka, "run", side_effect=AssertionError("no implicit API call")):
            for action in ("status", "next"):
                result = self.host_call(task, action)
                self.assertTrue(result["ok"])
                self.assertEqual(result["result"]["executor"], "api:configured-model")
                self.assertEqual(result["result"]["next_action"], "inspect_api_job")
            for action in ("inspect", "advance", "run"):
                result = self.host_call(task, action)
                self.assertFalse(result["ok"])
                self.assertIn("API-owned", result["error"])
        host, _ = self.prepare()
        packet_path = kc.job_dir(self.root, host["job_id"])/"packet.json"
        packet = json.loads(packet_path.read_text()); packet["focus"] = "tampered"
        si.atomic_json(packet_path, packet)
        result = self.host_call(host)
        self.assertFalse(result["ok"])
        self.assertIn("hash mismatch", result["error"])

    def test_host_knowledge_verify_does_not_claim_ingest_receipt_validation(self):
        task, _ = self.prepare()
        result = self.host_call(task, "verify")
        self.assertFalse(result["ok"])
        self.assertIn("knowledge check", result["error"])

    def test_minimal_output_omits_optional_labels_without_losing_conditions(self):
        task, packet = self.prepare()
        operation = {k:v for k,v in self.op(packet).items()
                     if k in {"id", "action", "title", "statement", "evidence", "reason"}}
        self.stage(task, [operation])
        self.assertTrue(kc.check(task["job_id"], repo=self.root)["ready"])
        out = kc.apply(task["job_id"], repo=self.root)
        unit = ks.read(out["results"]["item-1"]["id"], repo=self.root)
        self.assertEqual(unit["statement"], operation["statement"])
        self.assertEqual(unit["claim_mode"], "unknown")
        self.assertEqual(unit["content_role"], "unspecified")
        self.assertEqual(packet["usage_roles"]["blocks[].text"], "source_material")
        self.assertEqual(packet["usage_roles"]["output"], "knowledge")
        self.assertEqual(unit["status"], "draft")

    def test_optional_labels_do_not_relax_evidence_binding(self):
        task, packet = self.prepare()
        operation = {k:v for k,v in self.op(packet).items()
                     if k in {"id", "action", "title", "statement", "evidence", "reason"}}
        operation["evidence"] = [{"block_id": "B-not-in-packet", "relation": "supports"}]
        self.stage(task, [operation])
        self.assertFalse(kc.check(task["job_id"], repo=self.root)["ready"])

    def test_agent_manifest_and_first_ku_are_usable_without_api(self):
        with patch.object(ka,"run",side_effect=AssertionError("no API")):
            task,packet=self.prepare()
            self.assertEqual(task["agent_task"]["schema"],"agent-task-v1")
            self.stage(task,[self.op(packet)])
            self.assertTrue(kc.check(task["job_id"],repo=self.root)["ready"])
            result=kc.apply(task["job_id"],repo=self.root)
        uid=result["results"]["item-1"]["id"]
        unit=ks.read(uid,repo=self.root)
        self.assertEqual(unit["claim_mode"],"conjecture")
        self.assertIn("condition A",unit["statement"])
        self.assertEqual(unit["status"],"draft")
        hits=qa.execute("knowledge_search",{"query":"conditional algorithm"})
        self.assertTrue(hits["ok"],hits)
        self.assertEqual(json.loads(hits["text"])["candidates"][0]["id"],uid)
        read=qa.execute("knowledge_read",{"unit_id":uid})
        ref=json.loads(read["text"])["evidence"][0]
        self.assertTrue(qa.execute("read_raw",{"locator":ref["locator"]})["ok"])
        self.assertFalse((self.root/"cross-domain/graph.db").exists())

    def test_search_provides_raw_navigation_without_certifying_binding(self):
        uid,_=self.create()
        with patch.object(ks,'resolve_evidence',side_effect=AssertionError('search must not read Raw')):
            candidate=ks.search('conditional algorithm',repo=self.root)['candidates'][0]
        self.assertEqual(candidate['id'],uid)
        preview=candidate['evidence_preview'][0]
        self.assertEqual(preview['relation'],'supports')
        self.assertEqual(preview['assessed_revision'],1)
        self.assertEqual(candidate['status'],'draft')
        self.assertEqual(candidate['unresolved'],['Generality unknown'])
        self.assertEqual(candidate['evidence_count'],1)
        self.assertFalse(candidate['evidence_truncated'])
        self.assertTrue(qa.execute('read_raw',{'locator':preview['locator']})['ok'])
        # The candidate remains navigation if its underlying source later changes.
        path=self.root/preview['locator'].split('#')[0]
        path.write_text(path.read_text()+'\nChanged source bytes.\n')
        self.assertEqual(ks.search('conditional algorithm',repo=self.root)['candidates'][0]['evidence_preview'],[preview])
        self.assertFalse(qa.execute('read_raw',{'locator':preview['locator']})['ok'])

    def test_evidence_preview_bounds_preserve_relation_order_and_unknowns(self):
        uid,_=self.create()
        record=ks.read(uid,repo=self.root)
        refs=[{**record['evidence'][0],'relation':relation}
              for relation in ['contradicts','unknown','supports','related']]
        # A controlled store read exercises projection without rewriting a KU.
        with patch.object(ks,'read',return_value={**record,'evidence':refs}):
            candidate=ks.search('conditional algorithm',repo=self.root,kind='statement')['candidates'][0]
        self.assertEqual([r['relation'] for r in candidate['evidence_preview']],['contradicts','unknown','supports'])
        self.assertEqual(candidate['evidence_count'],4)
        self.assertTrue(candidate['evidence_truncated'])
        self.assertTrue(all(set(r)=={'locator','relation','assessed_revision'} for r in candidate['evidence_preview']))

    def test_second_source_revises_same_ku_and_retains_old_revision(self):
        uid,_=self.create()
        second=self.receive("second.md","# Conditional algorithm\nUnder condition A and noise below B, method X may improve efficiency.\n")
        task,packet=self.prepare(second,focus="Conditional algorithm")
        self.assertEqual(packet["candidates"][0]["id"],uid)
        self.stage(task,[self.op(packet,action="revise",target=uid,
            statement="Under condition A and noise below B, method X may improve efficiency.")])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["status"],"completed",result)
        current=ks.read(uid,repo=self.root)
        self.assertEqual(current["revision"],2)
        self.assertIn("noise below B",current["statement"])
        self.assertNotIn("noise below B",ks.read(uid,repo=self.root,revision=1)["statement"])
        self.assertTrue(all(ref["assessed_revision"]==2 for ref in current["evidence"]))
        again=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],2)

    def directed_fixture(self):
        uid, _ = self.create()
        source = self.receive("distinct.md", "# Separate topic\nA separately named observation.\nIts limiting condition.\n")
        locator = si.selected_parse(si.load_receipt(self.root, source["source_id"]))["text_path"] + "#L2-L3"
        return uid, source, locator

    def test_directed_cross_source_revision_bypasses_search_and_keeps_history(self):
        uid, source, locator = self.directed_fixture()
        old = ks.read(uid, repo=self.root)
        with patch.object(kc, "retrieve_candidates", side_effect=AssertionError("no discovery")), \
             patch.object(kc, "focused_chunks", side_effect=AssertionError("no source survey")), \
             patch.object(ka, "run", side_effect=AssertionError("no API from Agent")):
            task, packet = self.prepare(source, target=uid, source_locators=[locator])
        candidate = packet["candidates"][0]
        self.assertEqual([c["id"] for c in packet["candidates"]], [uid])
        self.assertEqual(candidate["statement"], old["statement"])
        self.assertEqual(candidate["snapshot_sha256"], old["snapshot_sha256"])
        self.assertEqual(len(candidate["old_evidence_blocks"]), len(old["evidence"]))
        self.assertEqual({b["ref"]["locator"] for b in packet["blocks"]},
                         {locator, *[r["locator"] for r in old["evidence"]]})
        self.assertEqual(packet["coverage"]["provided_ranges"], [[2, 3]])
        self.assertEqual(packet["coverage"]["unread_ranges"], [[1, 1]])
        self.assertIn(kc.DIRECTED_RULES, task["agent_task"]["protocol"]["instructions"])
        self.stage(task, [self.op(packet, action="revise", target=uid, origin_kind="synthesis",
            statement="A conditional comparison, with each source's limiting conditions retained.",
            evidence=[{"block_id": b["id"], "relation": "supports"} for b in packet["blocks"]])])
        self.assertTrue(kc.check(task["job_id"], repo=self.root)["ready"])
        result = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed", result)
        current = ks.read(uid, repo=self.root)
        self.assertEqual((current["revision"], current["origin_kind"]), (2, "synthesis"))
        self.assertEqual(len({r["source_id"] for r in current["evidence"]}), 2)
        self.assertEqual(ks.read(uid, repo=self.root, revision=1)["snapshot_sha256"], old["snapshot_sha256"])
        kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 2)

    def test_directed_requires_paired_bounded_current_parse_locators(self):
        uid, source, locator = self.directed_fixture()
        wrong_source = ks.read(uid, repo=self.root)["evidence"][0]["locator"]
        for options in (
            {"target": uid},
            {"target": "Q-native", "source_locators": [locator]},
            {"target": uid, "source_locators": locator},
            {"target": uid, "source_locators": [locator] * 6},
            *[{"target": uid, "source_locators": [value]} for value in (
                wrong_source, "private/raw/a.md#L1", "../outside.md#L1", "/tmp/a.md#L1",
                locator.split("#")[0], locator.replace("L2-L3", "L2-L99"),
                locator.replace("L2-L3", "L0-L1"))]):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.prepare(source, **options)
        with self.assertRaises(FileNotFoundError):
            self.prepare(source, target="KU-" + "0" * 24, source_locators=[locator])
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 1)

    def test_directed_old_evidence_and_budget_fail_before_api(self):
        uid, source, locator = self.directed_fixture()
        call = self.mock_api()
        before = si.digest(self.root / "academic/sources" / (source["source_id"] + ".json"))
        with patch.object(kc, "token_count", return_value=20000), self.assertRaisesRegex(ValueError, "budget"):
            ka.run(source["source_id"], repo=self.root, target=uid, source_locators=[locator])
        with patch.object(ks, "resolve_evidence", side_effect=ValueError("evidence binding changed")), \
             self.assertRaisesRegex(ValueError, "binding changed"):
            ka.run(source["source_id"], repo=self.root, target=uid, source_locators=[locator])
        self.assertEqual(call.call_count, 0)
        self.assertEqual(si.digest(self.root / "academic/sources" / (source["source_id"] + ".json")), before)
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 1)

    def test_directed_multiple_old_spans_and_requested_spans_are_all_retained(self):
        uid, source, locator = self.directed_fixture()
        task, packet = self.prepare(source, target=uid, source_locators=[locator])
        self.stage(task, [self.op(packet, action="attach", target=uid,
            evidence=[{"block_id": b["id"], "relation": "related"} for b in packet["blocks"]])])
        kc.apply(task["job_id"], repo=self.root)
        old = ks.read(uid, repo=self.root)
        locators = [locator.replace("L2-L3", "L1"), locator]
        task, packet = self.prepare(source, target=uid,
            source_locators=locators, refresh=True)
        self.assertEqual(len(packet["candidates"][0]["old_evidence_blocks"]), 2)
        self.assertEqual(len(packet["blocks"]), 3)
        self.assertTrue(all(r["locator"] in {b["ref"]["locator"] for b in packet["blocks"]} for r in old["evidence"]))
        self.assertEqual(packet["coverage"]["provided_ranges"], [[1, 3]])
        # Measure and reject the same requested spans; a smaller packet may fit.
        budget = kc.token_count(packet) + kc.token_count(kc.instructions(packet)) - 2001
        with self.assertRaisesRegex(ValueError, "budget"):
            self.prepare(source, target=uid, source_locators=locators, max_tokens=budget)

    def test_directed_blocks_create_other_targets_and_multiple_writes(self):
        uid, source, locator = self.directed_fixture()
        task, packet = self.prepare(source, target=uid, source_locators=[locator])
        for change in ({}, {"action": "revise", "target": "KU-" + "0" * 24},
                       {"action": "revise", "target": uid, "kind": "question"}):
            self.stage(task, [self.op(packet, **change)])
            self.assertFalse(kc.check(task["job_id"], repo=self.root)["ready"])
        self.stage(task, [self.op(packet, action="defer"), self.op(packet, id="item-2", action="defer")])
        with self.assertRaisesRegex(ValueError, "at most one"):
            kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 1)
        self.stage(task, [self.op(packet, action="defer")])
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["status"], "partial")

    def test_directed_cas_and_source_binding_remain_required(self):
        uid, source, locator = self.directed_fixture()
        task, packet = self.prepare(source, target=uid, source_locators=[locator])
        operation = self.op(packet, action="attach", target=uid,
            evidence=[{"block_id": b["id"], "relation": "supports"} for b in packet["blocks"]])
        self.stage(task, [operation])
        concurrent, _ = self.prepare(source, target=uid, source_locators=[locator], refresh=True)
        self.stage(concurrent, [operation])
        kc.apply(concurrent["job_id"], repo=self.root)
        self.assertFalse(kc.check(task["job_id"], repo=self.root)["ready"])
        fresh, packet = self.prepare(source, target=uid, source_locators=[locator], refresh=True)
        self.stage(fresh, [operation])
        path = self.root / locator.split("#")[0]
        path.write_text(path.read_text() + "Changed source.\n")
        self.assertFalse(kc.check(fresh["job_id"], repo=self.root)["ready"])
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 2)

    def test_directed_request_identity_and_automatic_cache_compatibility(self):
        uid, source, locator = self.directed_fixture()
        auto, packet = self.prepare(source)
        expected = "KJ-" + si.fingerprint([source["source_id"], source["parse_id"], kc.PROTOCOL,
            kc.PACKET_POLICY, si.fingerprint(kc.RULES), "", "agent", 14000])[:24]
        self.assertEqual(auto["job_id"], expected)
        self.assertNotIn("maintenance", packet)
        task, _ = self.prepare(source, target=uid, source_locators=[locator])
        replay, _ = self.prepare(source, target=uid, source_locators=[locator])
        changed, _ = self.prepare(source, target=uid, source_locators=[locator.replace("L2-L3", "L2")])
        self.assertEqual(task["job_id"], replay["job_id"])
        self.assertEqual(len({auto["job_id"], task["job_id"], changed["job_id"]}), 3)
        directed_key = [source["source_id"], source["parse_id"], kc.PROTOCOL, kc.PACKET_POLICY,
                        si.fingerprint(kc.RULES), "", "agent", 14000,
                        ["directed-maintenance-v1", kc.directed_request(si.load_receipt(self.root, source["source_id"]), uid, [locator]),
                         si.fingerprint(kc.DIRECTED_RULES)]]
        self.assertEqual(task["job_id"], "KJ-" + si.fingerprint(directed_key)[:24])

    def test_explicit_source_selection_keeps_whole_span_across_chunks_without_discovery(self):
        lines = ["# Long source"] + [f"Condition {i}: only under the specified assumptions." for i in range(1, 100)]
        source = self.receive("long-selected.md", "\n".join(lines) + "\n")
        locator = source["parse_path"] + "#L30-L70"
        with patch.object(ks, "search", side_effect=AssertionError("caller owns discovery")), \
             patch.object(kc, "focused_chunks", side_effect=AssertionError("no automatic survey")):
            _, packet = self.prepare(source, source_locators=[locator], focus="chosen condition")
        self.assertEqual(packet["source_selection"]["source_locators"], [locator])
        self.assertNotIn("maintenance", packet)
        self.assertNotIn("candidate_retrieval", packet)
        self.assertEqual(packet["candidates"], [])
        self.assertEqual(len(packet["blocks"]), 1)
        self.assertEqual(packet["blocks"][0]["text"].rstrip("\n"), "\n".join(lines[29:70]))
        self.assertEqual(packet["coverage"]["provided_ranges"], [[30, 70]])
        self.assertIn(kc.SELECTED_SOURCE_RULES, kc.instructions(packet))

    def test_comparison_context_preserves_selected_raw_and_complete_read_only_knowledge(self):
        uid, _ = self.create()
        source = self.receive("comparison-new.md", "# New source\nDifferent condition B.\n")
        locator = source["parse_path"] + "#L2"
        with patch.object(ks, "search", side_effect=AssertionError("caller owns discovery")), \
             patch.object(ks, "resolve_evidence", side_effect=AssertionError("comparison loads no old Raw")):
            task, packet = self.prepare(source, source_locators=[locator], comparison_ids=[uid, uid])
        old = ks.read(uid, repo=self.root)
        self.assertEqual(packet["source_selection"]["comparison_ids"], [uid])
        self.assertEqual(len(packet["blocks"]), 1)
        self.assertEqual(packet["blocks"][0]["ref"]["source_id"], source["source_id"])
        self.assertEqual(packet["blocks"][0]["text"].strip(), "Different condition B.")
        candidate = packet["candidates"][0]
        for field in ("id", "title", "statement", "unresolved", "snapshot_sha256"):
            self.assertEqual(candidate[field], old[field])
        self.assertFalse(candidate["can_update"])
        self.assertEqual(candidate["old_evidence_blocks"], [])
        self.assertIn(kc.COMPARISON_RULES, task["agent_task"]["protocol"]["instructions"])
        for action in ("attach", "revise"):
            self.stage(task, [self.op(packet, action=action, target=uid)])
            rejected = kc.check(task["job_id"], repo=self.root)
            self.assertFalse(rejected["ready"])
            self.assertIn("read-only", rejected["operations"][0]["error"])
        self.stage(task, [self.op(packet, statement="Different condition B.")])
        self.assertTrue(kc.check(task["job_id"], repo=self.root)["ready"])
        result = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(ks.read(uid, repo=self.root)["snapshot_sha256"], old["snapshot_sha256"])

    def test_comparison_rejects_invalid_options_before_job_or_api(self):
        uid, _ = self.create()
        locator = self.source["parse_path"] + "#L2"
        call = self.mock_api()
        before = set((self.root/"temp/knowledge-compile").glob("*/state.json"))
        receipt = si.digest(si.receipt_path(self.root, self.source["source_id"]))
        invalid = [uid, [None], [17], ["../private/a"], [uid]*6]
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                ka.run(self.source["source_id"], repo=self.root, source_locators=[locator], comparison_ids=ids)
        for opts in ({}, {"target": uid, "source_locators": [locator]}):
            with self.assertRaisesRegex(ValueError, "without target"):
                self.prepare(comparison_ids=[uid], **opts)
        with self.assertRaises(FileNotFoundError):
            self.prepare(source_locators=[locator], comparison_ids=["KU-"+"0"*24])
        self.assertEqual(call.call_count, 0)
        self.assertEqual(set((self.root/"temp/knowledge-compile").glob("*/state.json")), before)
        self.assertEqual(si.digest(si.receipt_path(self.root, self.source["source_id"])), receipt)

    def test_comparison_zero_output_checks_snapshot_before_any_state_write(self):
        uid, _ = self.create()
        opts = {"source_locators": [self.source["parse_path"]+"#L2"], "comparison_ids": [uid]}
        task, _ = self.prepare(**opts)
        self.stage(task, [])
        folder = kc.job_dir(self.root, task["job_id"])
        state_hash = si.digest(folder/"state.json")
        path = ks.unit_path(self.root, uid)
        initial = path.read_text()
        path.write_text(initial+"\nChanged knowledge scope.\n")
        for fn in (kc.check, kc.apply):
            with self.assertRaisesRegex(ValueError, "comparison knowledge changed"):
                fn(task["job_id"], repo=self.root)
        self.assertEqual(si.digest(folder/"state.json"), state_hash)
        new, _ = self.prepare(**opts)
        self.assertNotEqual(new["job_id"], task["job_id"])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "comparison knowledge unavailable"):
            kc.apply(task["job_id"], repo=self.root)
        path.write_text(initial)
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["status"], "completed")
        path.write_text(initial+"\nLater change.\n")
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["status"], "completed")

    def test_comparison_preserves_legacy_identity_and_binds_scoped_rules(self):
        uid, _ = self.create()
        locator = self.source["parse_path"]+"#L2"
        plain, packet = self.prepare(source_locators=[locator])
        empty, _ = self.prepare(source_locators=[locator], comparison_ids=[])
        self.assertEqual(plain["job_id"], empty["job_id"])
        self.assertNotIn(kc.COMPARISON_RULES, kc.instructions(packet))
        task, _ = self.prepare(source_locators=[locator], comparison_ids=[uid])
        replay, _ = self.prepare(source_locators=[locator], comparison_ids=[uid, uid])
        self.assertEqual(task["job_id"], replay["job_id"])
        with patch.object(kc, "COMPARISON_RULES", kc.COMPARISON_RULES+"Extra scope guidance."):
            changed, _ = self.prepare(source_locators=[locator], comparison_ids=[uid])
            same, _ = self.prepare(source_locators=[locator])
        self.assertNotEqual(task["job_id"], changed["job_id"])
        self.assertEqual(plain["job_id"], same["job_id"])

    def test_comparison_api_rejects_snapshot_changed_after_prepare_without_call(self):
        uid, _ = self.create()
        opts = {"source_locators": [self.source["parse_path"]+"#L2"], "comparison_ids": [uid]}
        task, _ = self.prepare(executor="api:mock", **opts)
        path = ks.unit_path(self.root, uid)
        path.write_text(path.read_text()+"\nConcurrent change.\n")
        call = self.mock_api()
        with patch.object(kc, "prepare", return_value=task), self.assertRaisesRegex(ValueError, "comparison knowledge changed"):
            ka.run(self.source["source_id"], repo=self.root, **opts)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(kc.load_job(self.root, task["job_id"])[0]["status"], "prepared")

    def test_comparison_preparation_detects_race_without_persisting_packet(self):
        uid, _ = self.create()
        old = ks.read(uid, repo=self.root)
        before = set((self.root/"temp/knowledge-compile").glob("*/state.json"))
        with patch.object(ks, "read", side_effect=[old, {**old, "snapshot_sha256": "f"*64}]), \
             self.assertRaisesRegex(ValueError, "changed during preparation"):
            self.prepare(source_locators=[self.source["parse_path"]+"#L2"], comparison_ids=[uid])
        self.assertEqual(set((self.root/"temp/knowledge-compile").glob("*/state.json")), before)

    def test_comparison_native_question_is_read_only(self):
        question, packet = self.prepare()
        self.stage(question, [self.op(packet, kind="question", statement="Does condition A generalize?")])
        qid = kc.apply(question["job_id"], repo=self.root)["results"]["item-1"]["id"]
        task, packet = self.prepare(source_locators=[self.source["parse_path"]+"#L2"], comparison_ids=[qid])
        self.assertEqual(packet["candidates"][0]["kind"], "question")
        self.assertFalse(packet["candidates"][0]["can_update"])
        self.stage(task, [])
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["status"], "completed")

    def test_comparison_budget_rejects_whole_input_before_model(self):
        uid, _ = self.create()
        call = self.mock_api()
        before = set((self.root/"temp/knowledge-compile").glob("*/state.json"))
        with patch.object(kc, "token_count", return_value=20000), self.assertRaisesRegex(ValueError, "budget"):
            ka.run(self.source["source_id"], repo=self.root, source_locators=[self.source["parse_path"]+"#L2"], comparison_ids=[uid])
        self.assertEqual(call.call_count, 0)
        self.assertEqual(set((self.root/"temp/knowledge-compile").glob("*/state.json")), before)

    def test_comparison_api_and_cli_share_context_and_replay(self):
        uid, _ = self.create()
        locator = self.source["parse_path"]+"#L2"
        def response(packet):
            self.assertEqual(packet["source_selection"]["comparison_ids"], [uid])
            return {"ok": True, "parsed": {"schema": kc.PROTOCOL, "operations": []}, "history": [{"status": "ok"}]}
        call = self.mock_api(response)
        opts = {"source_locators": [locator], "comparison_ids": [uid]}
        result = ka.run(self.source["source_id"], repo=self.root, **opts)
        replay = ka.run(self.source["source_id"], repo=self.root, **opts)
        self.assertEqual((result["status"], replay["new_model_calls"], call.call_count), ("completed", 0, 1))
        for action, module, fn in (("prepare", kc, "prepare"), ("run", ka, "run")):
            with patch.object(module, fn, return_value={"status": "prepared"}) as invoked, redirect_stdout(io.StringIO()):
                args = wg.build_parser().parse_args(["knowledge", action, self.source["source_id"],
                    "--source-locator", locator, "--compare-with", uid])
                args.func(args)
                self.assertEqual(invoked.call_args.kwargs["comparison_ids"], [uid])
        with patch.object(kc, "prepare", return_value={"status": "prepared"}) as invoked, redirect_stdout(io.StringIO()):
            kc.main(["prepare", self.source["source_id"], "--source-locator", locator, "--compare-with", uid])
            self.assertEqual(invoked.call_args.kwargs["comparison_ids"], [uid])
        with redirect_stdout(io.StringIO()) as output:
            wg.main(["knowledge", "apply", result["job_id"], "--compare-with", uid])
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_explicit_selection_rejects_invalid_ranges_before_job_or_model(self):
        call = self.mock_api()
        source = self.source
        locator = source["parse_path"] + "#L2"
        before = si.digest(self.root/"academic/sources"/(source["source_id"]+".json"))
        invalid = [locator, [None], [123], [locator]*6,
                   [source["parse_path"]], [source["parse_path"]+"#L2-L99"],
                   [source["parse_path"]+"#L0-L1"], ["private/raw/a.md#L1"],
                   ["../outside.md#L1"], ["/tmp/a.md#L1"],
                   ["academic/raw/sources/S-other/parses/P-old/paper.md#L1"]]
        for locators in invalid:
            with self.subTest(locators=locators), self.assertRaises(ValueError):
                ka.run(source["source_id"], repo=self.root, source_locators=locators)
        self.assertEqual(call.call_count, 0)
        self.assertEqual(si.digest(self.root/"academic/sources"/(source["source_id"]+".json")), before)
        self.assertFalse(list((self.root/"temp/knowledge-compile").glob("*/packet.json")))
        self.assertFalse(list((self.root/"temp/knowledge-compile").glob("*/state.json")))

    def test_explicit_selection_is_all_or_error_under_actual_budget(self):
        source = self.receive("budget-selected.md", "# Topic\nA necessary premise.\n" + ("detail "*1100 + "\n")*4)
        locators = [source["parse_path"]+f"#L{line}" for line in range(2, 7)]
        before = si.digest(self.root/"academic/sources"/(source["source_id"]+".json"))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.prepare(source, source_locators=locators, max_tokens=4000)
        self.assertFalse(list((self.root/"temp/knowledge-compile").glob("*/packet.json")))
        self.assertEqual(si.digest(self.root/"academic/sources"/(source["source_id"]+".json")), before)

    def test_explicit_selection_metadata_and_instructions_are_budgeted(self):
        locator = self.source["parse_path"] + "#L2"
        _, packet = self.prepare(source_locators=[locator])
        budget = kc.token_count(packet) + kc.token_count(kc.instructions(packet)) - 2001
        before = set((self.root/"temp/knowledge-compile").glob("*/packet.json"))
        with self.assertRaisesRegex(ValueError, "budget"):
            self.prepare(source_locators=[locator], max_tokens=budget)
        self.assertEqual(set((self.root/"temp/knowledge-compile").glob("*/packet.json")), before)

    def test_explicit_selection_identity_and_completed_zero_output_replay(self):
        locator = self.source["parse_path"] + "#L2"
        task, packet = self.prepare(source_locators=[locator, locator])
        replay, _ = self.prepare(source_locators=[locator+"-L2"])
        other, _ = self.prepare(source_locators=[self.source["parse_path"]+"#L1-L2"])
        automatic, _ = self.prepare()
        self.assertEqual(task["job_id"], replay["job_id"])
        self.assertEqual(len({task["job_id"], other["job_id"], automatic["job_id"]}), 3)
        self.stage(task, [])
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["status"], "completed")
        replay, _ = self.prepare(source_locators=[locator])
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(replay["results"], {})
        self.assertFalse(list((self.root/"academic/knowledge/units").glob("*.md")))

    def test_explicit_selection_creates_only_from_bound_blocks(self):
        locator = self.source["parse_path"] + "#L2"
        task, packet = self.prepare(source_locators=[locator])
        self.stage(task, [self.op(packet, evidence=[{"block_id": "B-not-selected", "relation": "supports"}])])
        self.assertFalse(kc.check(task["job_id"], repo=self.root)["ready"])
        self.stage(task, [self.op(packet, action="revise", target="KU-"+"0"*24)])
        self.assertFalse(kc.check(task["job_id"], repo=self.root)["ready"])
        self.stage(task, [self.op(packet)])
        self.assertTrue(kc.check(task["job_id"], repo=self.root)["ready"])
        saved = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(saved["status"], "completed")
        current = ks.read(saved["results"]["item-1"]["id"], repo=self.root)
        self.assertEqual(current["evidence"][0]["locator"], locator+"-L2")
        fresh, packet = self.prepare(source_locators=[locator], refresh=True)
        self.stage(fresh, [self.op(packet)])
        source_path = self.root/self.source["parse_path"]
        source_path.write_text(source_path.read_text()+"Changed.\n")
        self.assertFalse(kc.check(fresh["job_id"], repo=self.root)["ready"])

    def test_explicit_selection_api_uses_shared_packet_and_zero_call_replay(self):
        call = self.mock_api()
        locator = self.source["parse_path"] + "#L2"
        result = ka.run(self.source["source_id"], repo=self.root, source_locators=[locator])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn(kc.SELECTED_SOURCE_RULES, call.call_args.args[0])
        state, packet = kc.load_job(self.root, result["job_id"])
        self.assertTrue(state["executor"].startswith("api:"))
        self.assertEqual(packet["blocks"][0]["ref"]["locator"], locator+"-L2")
        self.assertEqual(packet["candidates"], [])
        replay = ka.run(self.source["source_id"], repo=self.root, source_locators=[locator])
        self.assertEqual((call.call_count, replay["new_model_calls"]), (1, 0))
        self.assertEqual(result["results"], replay["results"])

    def test_explicit_selection_cli_accepts_no_target_and_rejects_apply_options(self):
        locator = self.source["parse_path"]+"#L2"
        for action, module, name in (("prepare", kc, "prepare"), ("run", ka, "run")):
            with patch.object(module, name, return_value={"status": "prepared"}) as call, redirect_stdout(io.StringIO()):
                args = wg.build_parser().parse_args(["knowledge", action, self.source["source_id"], "--source-locator", locator])
                args.func(args)
                self.assertIsNone(call.call_args.kwargs["target"])
                self.assertEqual(call.call_args.kwargs["source_locators"], [locator])
        with redirect_stdout(io.StringIO()) as output:
            wg.main(["knowledge", "apply", "KJ-unused", "--source-locator", locator])
        self.assertFalse(json.loads(output.getvalue())["ok"])
        with patch.object(kc, "prepare", return_value={"status": "prepared"}) as call, redirect_stdout(io.StringIO()):
            kc.main(["prepare", self.source["source_id"], "--source-locator", locator])
        self.assertIsNone(call.call_args.kwargs["target"])
        self.assertEqual(call.call_args.kwargs["source_locators"], [locator])

    def test_directed_api_uses_shared_instructions_and_replays_without_call(self):
        uid, source, locator = self.directed_fixture()
        def response(packet):
            return {"ok": True, "parsed": {"schema": kc.PROTOCOL, "job_id": packet["job_id"],
                "operations": [self.op(packet, action="attach", target=uid,
                    evidence=[{"block_id": b["id"], "relation": "supports"} for b in packet["blocks"]])]},
                "model": "mock", "history": [{"status": "ok"}]}
        call = self.mock_api(response)
        result = ka.run(source["source_id"], repo=self.root, target=uid, source_locators=[locator])
        self.assertEqual(result["status"], "completed", result)
        self.assertIn(kc.DIRECTED_RULES, call.call_args.args[0])
        replay = ka.run(source["source_id"], repo=self.root, target=uid, source_locators=[locator])
        self.assertEqual((call.call_count, replay["new_model_calls"]), (1, 0))
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 2)

    def test_directed_cli_forwards_options_and_rejects_them_on_apply(self):
        uid, source, locator = self.directed_fixture()
        for action, module, name in (("prepare", kc, "prepare"), ("run", ka, "run")):
            with patch.object(module, name, return_value={"status": "prepared"}) as call, redirect_stdout(io.StringIO()):
                args = wg.build_parser().parse_args(["knowledge", action, source["source_id"],
                    "--target", uid, "--source-locator", locator])
                args.func(args)
                self.assertEqual(call.call_args.kwargs["target"], uid)
                self.assertEqual(call.call_args.kwargs["source_locators"], [locator])
        with redirect_stdout(io.StringIO()) as output:
            args = wg.build_parser().parse_args(["knowledge", "apply", "KJ-unused", "--target", uid])
            args.func(args)
        self.assertFalse(json.loads(output.getvalue())["ok"])
        with patch.object(kc, "prepare", return_value={"status": "prepared"}) as call, redirect_stdout(io.StringIO()):
            kc.main(["prepare", source["source_id"], "--target", uid, "--source-locator", locator])
            self.assertEqual(call.call_args.kwargs["source_locators"], [locator])

    def test_source_only_can_opt_in_and_never_erases_existing_knowledge(self):
        source=si.resume(self.source["source_id"],repo=self.root,knowledge_required=False)
        self.assertEqual(source["knowledge_status"],"not_required")
        uid,task=self.create()
        current=si.inspect_source(source["source_id"],repo=self.root)
        self.assertEqual(current["knowledge_policy"],"optional")
        self.assertEqual(current["knowledge_status"],"completed")
        before=ks.read(uid,repo=self.root)
        skipped=si.resume(source["source_id"],repo=self.root,knowledge_required=False)
        self.assertEqual(skipped["knowledge_status"],"completed")
        self.assertEqual(skipped["knowledge_run"],task["job_id"])
        self.assertEqual(ks.read(uid,repo=self.root)["snapshot_sha256"],before["snapshot_sha256"])

    def test_source_topics_ignore_procedural_focus_and_keep_cross_source_reuse(self):
        uid,_=self.create()
        unrelated=self.receive("dictionary.md","# 引文指标口径\n## 字段字典\n作者数据的解释依赖具体版本。\n")
        _,packet=self.prepare(unrelated,focus="Conditional algorithm; 优先保留作者的知识，复用数据。")
        self.assertNotIn(uid,[c["id"] for c in packet["candidates"]])
        self.assertFalse(any(b["ref"]["source_id"]==self.source["source_id"] for b in packet["blocks"]))
        related=self.receive("second.md","# Conditional algorithm\nUnder condition A and noise B, method X may improve efficiency.\n")
        _,packet=self.prepare(related,focus="只选择定义，不要求条目数量。")
        self.assertIn(uid,[c["id"] for c in packet["candidates"]])
        self.assertTrue(next(c for c in packet["candidates"] if c["id"]==uid)["can_update"])

    def test_weak_shared_words_do_not_load_unrelated_old_evidence(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title="知识融合方法",statement="作者基于数据建立方法，并解释适用边界。")])
        uid=kc.apply(task["job_id"],repo=self.root)["results"]["item-1"]["id"]
        document=self.receive("metrics.md","# 引文指标与作者行核验\n## 数据的解读边界\n作者数据的时间口径与版本有关。\n")
        _,context=self.prepare(document)
        self.assertNotIn(uid,[c["id"] for c in context["candidates"]])
        self.assertGreater(context["candidate_retrieval"]["rejected_weak_matches"],0)

    def test_topic_matching_accepts_chinese_phrases_and_same_source(self):
        old={"title":"噪声与梯度", "statement":"噪声诱导贫瘠高原的成立条件。", "evidence":[]}
        query,words,phrases=kc.candidate_topics({"title":"paper"},[{"text":"# 噪声诱导贫瘠高原"}],"")
        self.assertTrue(kc.candidate_matches_topic(old,"S-new",phrases))
        old.update(title="Different title",statement="Different words",evidence=[{"source_id":"S-new"}])
        self.assertTrue(kc.candidate_matches_topic(old,"S-new",phrases))

    def test_generic_english_headings_do_not_admit_unrelated_questions(self):
        query,words,phrases=kc.candidate_topics({"title":"paper"},[
            {"text":"# Knowledge Graph Embedding: A Survey of Approaches and Applications"},
            {"text":"## Model Training"}, {"text":"## Incorporating Additional Information"},
            {"text":"## TransE and Its Extensions"}, {"text":"## Logical Rules"}],"")
        for title in ("Training methods for quantum models", "Quantum information in many-body physics", "Feynman rules for renormalization"):
            self.assertFalse(kc.candidate_matches_topic({"title":title,"statement":title},"S-new",phrases))
        self.assertTrue(kc.candidate_matches_topic({"title":"TransE relation scoring","statement":"Translation vectors."},"S-new",phrases,names=['TransE']))
        self.assertTrue(kc.candidate_matches_topic({"title":"Vector representations","statement":"Knowledge graph embedding represents entities."},"S-new",phrases))

    def test_single_word_cross_source_collision_does_not_load_old_context(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title='Quantum transport',statement='Transport in a material requires condition A.')])
        unrelated=kc.apply(task['job_id'],repo=self.root)['results']['item-1']['id']
        ref=ks.read(unrelated,repo=self.root)['evidence'][0]
        related=ks.commit(repo=self.root,operation_id='multiword',title='Gradient statistics',
            statement='Quantum convolutional circuits under a local design assumption.',
            evidence=[ref],reason='Related multiword topic')['id']
        source=self.receive('neural.md','# Quantum convolutional circuits\nVariance depends on initialization assumptions.\n')
        # Freeze recall so admission, not ranking or corpus size, determines the outcome.
        with patch.object(ks,'search',return_value={'candidates':[{'id':unrelated},{'id':related}]}),patch.object(ka,'run',side_effect=AssertionError('no API')):
            _,context=self.prepare(source)
        self.assertNotIn(unrelated,[c['id'] for c in context['candidates']])
        self.assertIn(related,[c['id'] for c in context['candidates']])
        self.assertTrue(next(c for c in context['candidates'] if c['id']==related)['can_update'])

    def test_unrelated_question_single_word_does_not_consume_candidate_slot(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,kind='question',title='Discussion of chemical accuracy',
            statement='Can accuracy improve under different reagents?',content_role='research_question')])
        unrelated=kc.apply(task['job_id'],repo=self.root)['results']['item-1']['id']
        source=self.receive('discussion.md','# Spectral estimation\n## Discussion\nSpectral estimation needs a sampling condition.\n')
        with patch.object(ks,'search',return_value={'candidates':[{'id':unrelated}]}):
            _,context=self.prepare(source)
        self.assertEqual(context['candidates'],[])
        self.assertTrue(all(b['ref']['source_id']==source['source_id'] for b in context['blocks']))

    def test_passage_method_name_recalls_old_chinese_title_under_broad_headings(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title="AsterFlow的适用条件",statement="只有条件 A 成立时才能使用。")])
        uid=kc.apply(task["job_id"],repo=self.root)["results"]["item-1"]["id"]
        ref=ks.read(uid,repo=self.root)['evidence'][0]
        for i in range(6):
            ks.commit(repo=self.root,operation_id=f"generic-{i}",title="广义计算系统",
                statement=f"研究范围与通用问题 {i}。",evidence=[ref],reason="Candidate competition")
        source=self.receive("review.md","# 广义计算系统\n## 方法\n这里采用AsterFlow方法，只在条件 A 下使用。\n")
        with patch.object(ks,'search',wraps=ks.search) as search, patch.object(ka,'run',side_effect=AssertionError('no API')):
            _,context=self.prepare(source)
        self.assertLessEqual(search.call_count,2)
        self.assertEqual(context['candidates'][0]['id'],uid)
        self.assertTrue(context['candidates'][0]['can_update'])
        self.assertLessEqual(len(context['candidates']),5)
        self.assertLessEqual(context['candidate_retrieval']['pool_size'],20)

    def test_named_method_wins_over_ordinary_word_and_substring(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title="ComplEx关系模型",statement="复数表示的条件性方法。")])
        uid=kc.apply(task['job_id'],repo=self.root)['results']['item-1']['id']
        ref=ks.read(uid,repo=self.root)['evidence'][0]
        for i in range(6):
            ks.commit(repo=self.root,operation_id=f"complex-{i}",title=f"Complex networks {i}",
                statement="Research into complex systems.",evidence=[ref],reason="Common word collision")
        source=self.receive('complex.md','# General survey\n## Methods\nComplEx uses complex-valued vectors.\n')
        _,context=self.prepare(source)
        self.assertEqual(context['candidates'][0]['id'],uid)
        self.assertEqual(kc.name_matches(['ComplEx'],'complex systems and Complex networks'),[])
        self.assertEqual(kc.name_matches(['TransE'],'TransE2 and PreTransE'),[])

    def test_compound_name_components_do_not_admit_unrelated_old_evidence(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet, title="EM-LLM 情景记忆", statement="A separate memory mechanism.")])
        noise = kc.apply(task['job_id'], repo=self.root)['results']['item-1']['id']
        definition = self.receive('definition.md', '# LLM 概念\nLanguage models generate tokens.\n')
        task, packet = self.prepare(definition)
        self.stage(task, [self.op(packet, title="LLM 的一般概念", statement="A language model predicts tokens.")])
        useful = kc.apply(task['job_id'], repo=self.root)['results']['item-1']['id']
        source = self.receive('controller.md', '# Retrieval controller\nAn LLM controls the next query.\n')
        with patch.object(ks, 'search', return_value={'candidates': [{'id': noise}, {'id': useful}]}):
            _, packet = self.prepare(source)
        self.assertIn('LLM', packet['candidate_retrieval']['passage_names'])
        self.assertEqual([c['id'] for c in packet['candidates']], [useful])
        self.assertTrue(packet['candidates'][0]['can_update'])
        self.assertNotIn(self.source['source_id'], {b['ref']['source_id'] for b in packet['blocks']})
        self.assertIn(definition['source_id'], {b['ref']['source_id'] for b in packet['blocks']})
        # A full method name still brings in the same old unit and its evidence.
        named = self.receive('named.md', '# Retrieval controller\nWe discuss EM-LLM in this setting.\n')
        with patch.object(ks, 'search', return_value={'candidates': [{'id': noise}, {'id': useful}]}):
            _, packet = self.prepare(named)
        self.assertEqual([c['id'] for c in packet['candidates']], [noise])
        self.assertTrue(packet['candidates'][0]['can_update'])

    def test_compound_name_collision_cannot_crowd_out_complete_name_in_pool(self):
        records = {f'KU-{i:024x}': {'id': f'KU-{i:024x}', 'title': f'Variant{i}-LLM',
                   'statement': 'Unrelated topic.', 'evidence': []} for i in range(20)}
        useful = 'KU-' + 'f' * 24
        records[useful] = {'id': useful, 'title': 'LLM 的一般概念', 'statement': 'Token prediction.', 'evidence': []}
        responses = [{'candidates': [{'id': uid} for uid in list(records)[:20]]},
                     {'candidates': [{'id': useful}]}]
        with patch.object(ks, 'search', side_effect=responses), \
             patch.object(ks, 'read', side_effect=lambda uid, **_: records[uid]):
            pool, info = kc.retrieve_candidates(self.root, 'S-new', 'controller reasoning', ['LLM'])
        self.assertEqual(pool[0]['id'], useful)
        self.assertEqual(len(pool), 20)
        self.assertEqual(len(info['searches']), 2)

    def test_name_boundaries_preserve_whole_names_and_punctuation(self):
        for text in ['EM-LLM', 'LLM-based', 'pre-LLM-post', 'LLM-2', '1-LLM', 'preLLM', 'LLM_v2']:
            with self.subTest(text=text):
                self.assertEqual(kc.name_matches(['LLM'], text), [])
        for text in ['LLM概念', '中文LLM', '(LLM)', 'LLM - methods', 'LLM- 方法', '-LLM', 'a--LLM']:
            with self.subTest(text=text):
                self.assertEqual(kc.name_matches(['LLM'], text), ['LLM'])
        self.assertEqual(kc.name_matches(['EM-LLM', 'LLM'], '中文EM-LLM机制'), ['EM-LLM'])
        self.assertEqual(kc.name_matches(['RAG', 'S2G-RAG'], 'S2G-RAG框架'), ['S2G-RAG'])
        self.assertEqual(kc.name_matches(['TransE', 'ComplEx'], 'TransE和ComplEx；complex'), ['TransE', 'ComplEx'])

    def test_compound_boundary_does_not_override_source_or_topic_admission(self):
        old = {'title': 'EM-LLM 概述', 'statement': 'Memory retrieval under explicit conditions.', 'evidence': []}
        self.assertFalse(kc.candidate_matches_topic(old, 'S-new', set(), ['LLM']))
        self.assertTrue(kc.candidate_matches_topic(old, 'S-new', {'memory retrieval'}, ['LLM']))
        old['evidence'] = [{'source_id': 'S-new'}]
        self.assertTrue(kc.candidate_matches_topic(old, 'S-new', set(), ['LLM']))

    def test_passage_names_do_not_use_unread_text_or_instruction_names(self):
        lines=['General background without any named method. '*8 for _ in range(220)]
        lines[128]='## VisibleFlow method'
        lines[129]='VisibleFlow requires condition A; see $HiddenMath$ and ![](images/HiddenImage.jpg).'
        lines[145]='HiddenFlow is a separate method outside the selected passages.'
        source=self.receive('names.md','\n'.join(lines))
        _,context=self.prepare(source,max_tokens=6000,focus='VisibleFlow；稍后考虑 InstructionOnly')
        shown='\n'.join(b['text'] for b in context['blocks'])
        self.assertNotIn('HiddenFlow',shown)
        names=context['candidate_retrieval']['passage_names']
        self.assertIn('VisibleFlow',names)
        for absent in ['InstructionOnly','HiddenFlow','HiddenMath','HiddenImage']:
            self.assertNotIn(absent,names)
        self.assertLessEqual(len(names),12)

    def test_literal_names_include_uppercase_prefixes_and_hyphen_acronyms(self):
        names = kc.passage_names([
            'RAGTruth Stop-RAG Self-RAG S2G-RAG LLMs ComplEx RAG. Ordinary Title words. '
            '$HiddenMath$ ![](images/HiddenImage.png) https://example.org/HiddenURL <HiddenTag>'
        ])
        self.assertEqual(names, ['RAGTruth', 'Stop-RAG', 'Self-RAG', 'S2G-RAG', 'LLMs', 'ComplEx', 'RAG'])

    def test_complete_focus_name_limits_cross_source_context_and_preserves_evidence(self):
        task, packet = self.prepare()
        self.stage(task, [self.op(packet, title='RAG 的其他机制', statement='An unrelated retrieval procedure.')])
        noise = kc.apply(task['job_id'], repo=self.root)['results']['item-1']['id']
        definitions = self.receive('definitions.md', '# Method descriptions\nDefinitions under explicit conditions.\n')
        _, definition_packet = self.prepare(definitions)
        ref = definition_packet['blocks'][0]['ref']
        targets = {}
        for name in ['RAGTruth', 'Stop-RAG', 'S2G-RAG']:
            targets[name] = ks.commit(repo=self.root, operation_id='define-'+name, title=name+' 机制',
                statement='A named procedure under condition A.', evidence=[ref], reason='Reusable definition')['id']
        source = self.receive('focused.md', '# Evaluation pipeline\nRAG and LLM appear with RAGTruth, Stop-RAG and S2G-RAG.\n')
        hits = {'candidates': [{'id': uid} for uid in [noise, *targets.values()]]}
        for name, uid in targets.items():
            with self.subTest(name=name), patch.object(ks, 'search', return_value=hits) as search:
                _, context = self.prepare(source, focus='只整理'+name.lower()+'；保留条件')
                self.assertEqual([c['id'] for c in context['candidates']], [uid])
                self.assertEqual(context['candidate_retrieval']['priority_names'], [name])
                self.assertEqual(context['candidate_retrieval']['admission_names'], [name])
                self.assertTrue(context['candidates'][0]['can_update'])
                self.assertEqual(len(context['candidates'][0]['old_evidence_blocks']), 1)
                self.assertNotIn(self.source['source_id'], {b['ref']['source_id'] for b in context['blocks']})
                self.assertIn(definitions['source_id'], {b['ref']['source_id'] for b in context['blocks']})
                for block in context['blocks']:
                    self.assertEqual(ks.resolve_evidence(self.root, block['ref']), block['text'])
                self.assertLessEqual(search.call_count, 3)

    def test_focus_name_admission_preserves_source_topic_and_unconfirmed_fallback(self):
        _, old_packet = self.prepare()
        source = self.receive('taxonomy.md', '# Corpus taxonomy\nRAG and LLM appear alongside RAGTruth.\n')
        _, source_packet = self.prepare(source)
        ids = {}
        for key, title, ref in [
            ('generic', 'RAG 其他用途', old_packet['blocks'][0]['ref']),
            ('named', 'RAGTruth 类别', old_packet['blocks'][0]['ref']),
            ('topic', 'Corpus taxonomy 条件', old_packet['blocks'][0]['ref']),
            ('source', 'Different object', source_packet['blocks'][0]['ref']),
        ]:
            ids[key] = ks.commit(repo=self.root, operation_id=key, title=title,
                statement='A conditional statement.', evidence=[ref], reason='Admission fixture')['id']
        for focus in ['RAGTruth', '', 'UnknownMethod']:
            with self.subTest(focus=focus), patch.object(ks, 'search', return_value={
                'candidates': [{'id': uid} for uid in ids.values()]
            }) as search:
                _, context = self.prepare(source, focus=focus, refresh=True)
                expected = set(ids.values()) - ({ids['generic']} if focus == 'RAGTruth' else set())
                self.assertEqual({c['id'] for c in context['candidates']}, expected)
                self.assertTrue(all(c['can_update'] for c in context['candidates']))
                info = context['candidate_retrieval']
                self.assertEqual(info['admission_names'], ['RAGTruth'] if focus == 'RAGTruth' else info['passage_names'])
                self.assertNotIn('UnknownMethod', info['passage_names'])
                if focus:
                    self.assertIn(focus, [call.args[0] for call in search.call_args_list])
                    self.assertEqual(info['purpose_query'], focus)

    def test_focused_compound_target_survives_bounded_generic_name_competition(self):
        uid, _ = self.create()
        template = ks.read(uid, repo=self.root)
        records = {f'KU-{i:024x}': {**template, 'id': f'KU-{i:024x}',
                   'title': f'RAG 独立场景 {i}', 'statement': 'Unrelated retrieval usage.'} for i in range(20)}
        records[uid] = {**template, 'title': 'Stop-RAG 停止机制'}
        source = self.receive('competition.md', '# Retrieval controller\nRAG and LLM support Stop-RAG.\n')
        responses = [{'candidates': [{'id': key} for key in list(records)[:20]]},
                     {'candidates': [{'id': uid}]}, {'candidates': []}]
        with patch.object(ks, 'search', side_effect=responses), \
             patch.object(ks, 'read', side_effect=lambda key, **_: records[key]):
            _, context = self.prepare(source, focus='只整理stop-rag')
        self.assertEqual([c['id'] for c in context['candidates']], [uid])
        self.assertTrue(context['candidates'][0]['can_update'])
        self.assertEqual(context['candidate_retrieval']['pool_size'], 20)
        self.assertEqual(len(context['candidate_retrieval']['searches']), 3)

    def test_incidental_body_acronym_does_not_admit_unrelated_knowledge(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title='导航协议',statement='协议中也提到了 DOI 标识。')])
        uid=kc.apply(task['job_id'],repo=self.root)['results']['item-1']['id']
        source=self.receive('fields.md','# 引文指标\n## 字段解释\nDOI identifies a document.\n')
        _,context=self.prepare(source)
        self.assertNotIn(uid,[c['id'] for c in context['candidates']])
        self.assertGreater(context['candidate_retrieval']['rejected_weak_matches'],0)

    def test_stale_candidate_cannot_overwrite_new_revision(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="revise",target=uid,statement="A new conditional statement.")])
        old=ks.read(uid,repo=self.root)
        ks.commit(repo=self.root,operation_id="another-writer",unit_id=uid,expected_revision=old["revision"],
            expected_sha256=old["snapshot_sha256"],title=old["title"],statement=old["statement"]+" New caveat.",
            evidence=old["evidence"],reason="A concurrent user revision")
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["status"],"partial")
        self.assertIn("conflict",result["results"]["item-1"]["error"])
        self.assertIn("New caveat",ks.read(uid,repo=self.root)["statement"])

    def test_attach_keeps_statement_strength_and_unresolved_conditions(self):
        uid,_=self.create()
        second=self.receive("support.md","# Conditional algorithm\nAnother observation supports method X only under condition A.\n")
        task,packet=self.prepare(second,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid,claim_mode="author_claim",unresolved=[])])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["status"],"completed",result)
        record=ks.read(uid,repo=self.root)
        self.assertEqual(record["claim_mode"],"conjecture")
        self.assertEqual(record["unresolved"],["Generality unknown"])
        self.assertEqual(len(record["evidence"]),2)

    def test_duplicate_attach_preserves_bytes_across_jobs_and_evidence_order(self):
        uid,_=self.create()
        second=self.receive("support.md","# Conditional algorithm\nAnother conditional observation of method X.\n")
        task,packet=self.prepare(second,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        kc.apply(task["job_id"],repo=self.root)
        before=ks.unit_path(self.root,uid).read_bytes()
        history=ks.unit_path(self.root,uid,1).read_bytes()
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],2)
        for executor in ("agent","api"):
            task,packet=self.prepare(refresh=True,focus="conditional algorithm",executor=executor)
            # Reattaching the first of two old refs moves it to the end in validation.
            op=self.op(packet,action="attach",target=uid)
            op["evidence"]*=2
            self.stage(task,[op])
            self.assertTrue(kc.check(task["job_id"],repo=self.root)["ready"])
            result=kc.apply(task["job_id"],repo=self.root)["results"]["item-1"]
            self.assertEqual(result,{"id":uid,"revision":2,"status":"saved","reused":True,"unchanged":True})
            self.assertEqual(kc.apply(task["job_id"],repo=self.root)["results"]["item-1"],result)
            self.assertEqual(ks.unit_path(self.root,uid).read_bytes(),before)
            self.assertEqual(ks.unit_path(self.root,uid,1).read_bytes(),history)
            self.assertFalse(ks.unit_path(self.root,uid,2).exists())
            self.stage(task,[{**op,"reason":"Changed after acknowledgment"}])
            with self.assertRaisesRegex(ValueError,"committed operation"):
                kc.apply(task["job_id"],repo=self.root)

    def test_duplicate_attach_does_not_hide_relation_or_unresolved_changes(self):
        uid,_=self.create()
        for revision,changes in ((2,{"relation":"limits"}),(3,{"unresolved":["Additional boundary unknown"]})):
            task,packet=self.prepare(refresh=True,focus="conditional algorithm")
            op=self.op(packet,action="attach",target=uid)
            op["evidence"][0]["relation"]=changes.get("relation","limits")
            if "unresolved" in changes:
                op["unresolved"]=changes["unresolved"]
            self.stage(task,[op])
            result=kc.apply(task["job_id"],repo=self.root)["results"]["item-1"]
            self.assertEqual(result["revision"],revision)
            self.assertFalse(result["reused"])
            self.assertEqual(ks.read(uid,repo=self.root)["evidence"][0]["relation"],"limits")
        self.assertEqual(ks.read(uid,repo=self.root)["unresolved"],["Generality unknown","Additional boundary unknown"])

    def test_duplicate_attach_revalidates_source_and_commit_snapshot(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        before=ks.unit_path(self.root,uid).read_bytes()
        real_commit=ks.commit
        def race(**kwargs):
            # A writer lands after compiler validation, before the store's lock/CAS.
            ks.unit_path(self.root,uid).write_bytes(before+b"\n")
            return real_commit(**kwargs)
        with patch.object(ks,"commit",side_effect=race):
            result=kc.apply(task["job_id"],repo=self.root)
        self.assertIn("conflict",result["results"]["item-1"]["error"])
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        (self.root/self.source["parse_path"]).write_text("Tampered source")
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["status"],"failed")
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],1)

    def test_duplicate_attach_receipt_interruption_revalidates_without_primary_write(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        before=ks.unit_path(self.root,uid).read_bytes()
        real_save=kc.save_state
        def interrupted(repo,state):
            if state["results"]:
                raise OSError("receipt interrupted")
            real_save(repo,state)
        with patch.object(kc,"save_state",side_effect=interrupted):
            with self.assertRaises(OSError):
                kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(ks.unit_path(self.root,uid).read_bytes(),before)
        recovered=kc.apply(task["job_id"],repo=self.root)
        self.assertTrue(recovered["results"]["item-1"]["unchanged"])

    def test_duplicate_attach_index_failure_retries_without_revision(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        op=self.op(packet,action="attach",target=uid)
        self.stage(task,[op])
        before=ks.unit_path(self.root,uid).read_bytes()
        with patch.object(ks,"index_unit",side_effect=OSError("index busy")):
            result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["status"],"index_pending")
        self.assertEqual(ks.unit_path(self.root,uid).read_bytes(),before)
        self.stage(task,[{**op,"reason":"Changed pending operation"}])
        with self.assertRaisesRegex(ValueError,"committed operation"):
            kc.apply(task["job_id"],repo=self.root)
        self.stage(task,[op])
        recovered=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(recovered["status"],"completed")
        self.assertTrue(recovered["results"]["item-1"]["unchanged"])
        self.assertEqual(ks.unit_path(self.root,uid).read_bytes(),before)

    def test_duplicate_attach_does_not_hide_new_parse_of_same_source(self):
        bundle=self.root/"bundle"; bundle.mkdir()
        (bundle/"paper.pdf").write_bytes(b"%PDF-test-original")
        text="# Conditional algorithm\nUnder condition A, method X may improve efficiency.\n"
        (bundle/"paper.md").write_text(text)
        first=si.ingest(bundle/"paper.pdf",repo=self.root,reuse_parse=bundle)
        task,packet=self.prepare(first); self.stage(task,[self.op(packet)])
        uid=kc.apply(task["job_id"],repo=self.root)["results"]["item-1"]["id"]
        (bundle/"paper.md").write_text(text+"The parse now includes an additional limitation.\n")
        second=si.ingest(bundle/"paper.pdf",repo=self.root,reuse_parse=bundle)
        self.assertEqual(first["source_id"],second["source_id"])
        self.assertNotEqual(first["parse_id"],second["parse_id"])
        task,packet=self.prepare(second,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["revision"],2)
        self.assertEqual({r["parse_id"] for r in ks.read(uid,repo=self.root)["evidence"]},
                         {first["parse_id"],second["parse_id"]})

    def test_duplicate_attach_interrupted_receipt_cannot_ignore_concurrent_revision(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        real_save=kc.save_state
        def interrupted(repo,state):
            if state["results"]:
                raise OSError("receipt interrupted")
            real_save(repo,state)
        with patch.object(kc,"save_state",side_effect=interrupted):
            with self.assertRaises(OSError): kc.apply(task["job_id"],repo=self.root)
        old=ks.read(uid,repo=self.root)
        ks.commit(repo=self.root,operation_id="concurrent",unit_id=uid,
            expected_revision=old["revision"],expected_sha256=old["snapshot_sha256"],
            title=old["title"],statement=old["statement"]+" More restrictions.",
            evidence=old["evidence"],reason="Real revision")
        before=ks.unit_path(self.root,uid).read_bytes()
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertIn("conflict",result["results"]["item-1"]["error"])
        self.assertEqual(ks.unit_path(self.root,uid).read_bytes(),before)

    def test_duplicate_attach_does_not_reuse_assessments_of_another_revision(self):
        uid,_=self.create()
        path=ks.unit_path(self.root,uid)
        from frontier import parse_record
        record,body=parse_record(path)
        record["evidence"][0]["assessed_revision"]=0  # isolated imported legacy fixture
        path.write_text(ks.render({**record,"statement":body}))
        ks.index_unit(self.root,uid)
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid)])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"].get("revision"),2,result)
        self.assertEqual(ks.read(uid,repo=self.root)["evidence"][0]["assessed_revision"],2)

    def test_explicit_revise_still_records_reassessment_of_unchanged_statement(self):
        uid,_=self.create()
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="revise",target=uid,reason="Explicit reassessment")])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["revision"],2)
        self.assertFalse(result["results"]["item-1"]["reused"])

    def test_partial_failure_can_repair_only_failed_object(self):
        task,packet=self.prepare()
        good=self.op(packet)
        bad=self.op(packet,id="bad",evidence=[{"block_id":"invented","relation":"supports"}])
        self.stage(task,[good,bad])
        partial=kc.apply(task["job_id"],repo=self.root)
        uid=partial["results"]["item-1"]["id"]
        self.assertEqual(partial["status"],"partial")
        self.stage(task,[good,self.op(packet,id="bad",action="defer",reason="Need additional evidence")])
        fixed=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(fixed["results"]["bad"]["status"],"deferred")
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],1)
        self.assertEqual(si.inspect_source(self.source["source_id"],repo=self.root)["status"],"source_ready")

    def test_committed_operation_cannot_be_rewritten_in_place(self):
        uid,task=self.create()
        _,packet=kc.load_job(self.root,task["job_id"])
        self.stage(task,[self.op(packet,statement="Changed after commit")])
        with self.assertRaisesRegex(ValueError,"committed operation"):
            kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],1)

    def test_index_failure_preserves_primary_and_replays_without_new_revision(self):
        task,packet=self.prepare(); self.stage(task,[self.op(packet)])
        with patch.object(ks,"index_unit",side_effect=OSError("index busy")):
            result=kc.apply(task["job_id"],repo=self.root)
        uid=result["results"]["item-1"]["id"]
        self.assertEqual(result["results"]["item-1"]["status"],"index_pending")
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],1)
        recovered=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(recovered["status"],"completed")
        self.assertEqual(ks.read(uid,repo=self.root)["revision"],1)

    def test_job_receipt_interruption_after_ku_commit_is_recoverable(self):
        task,packet=self.prepare(); self.stage(task,[self.op(packet)])
        real_save=kc.save_state
        def fail_after_commit(repo,state):
            if state["results"]:
                raise OSError("receipt interrupted")
            real_save(repo,state)
        with patch.object(kc,"save_state",side_effect=fail_after_commit):
            with self.assertRaises(OSError):
                kc.apply(task["job_id"],repo=self.root)
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertTrue(result["results"]["item-1"]["reused"])
        self.assertEqual(result["results"]["item-1"]["revision"],1)

    def test_source_tampering_prevents_new_publication(self):
        task,packet=self.prepare(); self.stage(task,[self.op(packet)])
        (self.root/self.source["parse_path"]).write_text("Tampered source")
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["status"],"failed")
        self.assertFalse((self.root/"academic/knowledge/units").exists())

    def test_preparation_requires_live_source_readiness(self):
        (self.root/"academic/sources/index.sqlite").unlink()
        with self.assertRaisesRegex(ValueError,"resume source"):
            self.prepare()
        self.assertFalse((self.root/"temp/knowledge-compile").exists())
        si.resume(self.source["source_id"],repo=self.root)
        task,_=self.prepare()
        self.assertEqual(task["status"],"prepared")

    def test_question_reuses_frontier_identity_and_states(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,kind="question",statement="Does the conditional algorithm generalize?")])
        result=kc.apply(task["job_id"],repo=self.root)
        uid=result["results"]["item-1"]["id"]
        record=ks.read(uid,repo=self.root)
        self.assertEqual(record["status"],"captured")
        self.assertEqual(record["kb_state"],"unassessed")
        self.assertEqual(record["scientific_state"],"unverified")
        self.assertEqual(record["title"],"Conditional algorithm")
        self.assertEqual(record["unresolved"],["Generality unknown"])
        self.assertIsNone(record["revision"])
        self.assertEqual(ks.search("conditional",repo=self.root)["candidates"][0]["id"],uid)
        self.assertFalse((self.root/"academic/knowledge/units").exists())
        self.assertFalse((self.root/"academic/frontier/frontier.db").exists())

    def test_statement_content_roles_survive_storage_and_retrieval(self):
        task,packet=self.prepare()
        roles=["concept_definition","method_model","scientific_finding"]
        self.stage(task,[self.op(packet,id=f"role-{i}",content_role=role) for i,role in enumerate(roles)])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["status"],"completed",result)
        for i,role in enumerate(roles):
            uid=result["results"][f"role-{i}"]["id"]
            read=qa.execute("knowledge_read",{"unit_id":uid})
            self.assertEqual(json.loads(read["text"])["content_role"],role)
        self.assertEqual({c["content_role"] for c in ks.search("conditional",repo=self.root)["candidates"]},set(roles))

    def test_question_source_position_does_not_upgrade_scientific_or_answer_state(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,id=state,kind="question",content_role="research_question",
            source_question_status=state,statement=f"Does method X work? Source position: {state}.")
            for state in ["addressed","left_open"]])
        result=kc.apply(task["job_id"],repo=self.root)
        for state in ["addressed","left_open"]:
            record=ks.read(result["results"][state]["id"],repo=self.root)
            self.assertEqual(record["content_role"],"research_question")
            self.assertEqual(record["source_question_status"],state)
            self.assertEqual(record["status"],"captured")
            self.assertEqual(record["kb_state"],"unassessed")
            self.assertEqual(record["scientific_state"],"unverified")

    def test_native_question_is_read_only_context_for_reuse(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,kind="question",content_role="research_question",source_question_status="left_open")])
        result=kc.apply(task["job_id"],repo=self.root)
        uid=result["results"]["item-1"]["id"]
        followup,context=self.prepare(refresh=True,focus="conditional algorithm")
        candidate=next(c for c in context["candidates"] if c["id"]==uid)
        self.assertFalse(candidate["can_update"])
        self.assertEqual(candidate["content_role"],"research_question")
        self.assertEqual(candidate["source_question_status"],"left_open")
        self.stage(followup,[self.op(context,action="defer",target=uid,reason="Reuse existing Question via Frontier")])
        kc.apply(followup["job_id"],repo=self.root)
        self.assertEqual(len(list((self.root/"academic/frontier/questions").glob("*.md"))),1)

    def test_legacy_statement_role_is_unknown_without_rewriting_and_revision_keeps_assigned_role(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,content_role="method_model")])
        result=kc.apply(task["job_id"],repo=self.root)
        uid=result["results"]["item-1"]["id"]
        followup,context=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(followup,[self.op(context,action="revise",target=uid,statement="Under condition A, method X may improve efficiency; other conditions remain unknown.")])
        kc.apply(followup["job_id"],repo=self.root)
        self.assertEqual(ks.read(uid,repo=self.root)["content_role"],"method_model")
        self.assertEqual(ks.read(uid,repo=self.root,revision=1)["content_role"],"method_model")
        path=ks.unit_path(self.root,uid)
        from frontier import parse_record
        meta,body=parse_record(path)
        meta.pop("content_role");meta["statement"]=body
        path.write_text(ks.render(meta))  # isolated legacy-format fixture
        before=path.read_bytes()
        self.assertEqual(ks.read(uid,repo=self.root)["content_role"],"unspecified")
        self.assertEqual(path.read_bytes(),before)

    def test_attach_preserves_content_role_and_role_errors_are_local(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,content_role="concept_definition"),
            self.op(packet,id="bad",content_role="research_question")])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["bad"]["status"],"failed")
        uid=result["results"]["item-1"]["id"]
        followup,context=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(followup,[self.op(context,action="attach",target=uid,content_role="scientific_finding")])
        kc.apply(followup["job_id"],repo=self.root)
        self.assertEqual(ks.read(uid,repo=self.root)["content_role"],"concept_definition")

    def test_attach_fills_only_unknown_role_without_changing_claim(self):
        uid,_=self.create()
        old=ks.read(uid,repo=self.root)
        task,packet=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(task,[self.op(packet,action="attach",target=uid,content_role="method_model",
            statement="Unconditional certainty",claim_mode="author_claim",source_question_status=None)])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["status"],"saved")
        current=ks.read(uid,repo=self.root)
        self.assertEqual(current["content_role"],"method_model")
        self.assertEqual(current["statement"],old["statement"])
        self.assertEqual(current["claim_mode"],old["claim_mode"])
        self.assertEqual(ks.read(uid,repo=self.root,revision=1)["content_role"],"unspecified")
        followup,context=self.prepare(refresh=True,focus="conditional algorithm")
        self.stage(followup,[self.op(context,action="revise",target=uid,content_role=None)])
        kc.apply(followup["job_id"],repo=self.root)
        self.assertEqual(ks.read(uid,repo=self.root)["content_role"],"method_model")

    def test_optional_null_status_is_absent_but_invalid_status_stays_local(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,source_question_status=None),
            self.op(packet,id="question",kind="question",content_role=None,source_question_status=None),
            self.op(packet,id="invalid",source_question_status="addressed"),
            self.op(packet,id="malformed",kind="question",source_question_status=[]),
            self.op(packet,id="empty",kind="question",source_question_status="")])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["results"]["item-1"]["status"],"saved")
        question=ks.read(result["results"]["question"]["id"],repo=self.root)
        self.assertEqual(question["source_question_status"],"unspecified")
        self.assertEqual(question["content_role"],"research_question")
        for item in ("invalid","malformed","empty"):
            self.assertEqual(result["results"][item]["status"],"failed")

    def test_private_and_symlink_paths_rejected(self):
        uid,_=self.create()
        for action,inputs in (("knowledge_search",{"query":"conditional"}),
                              ("knowledge_read",{"unit_id":uid})):
            self.assertFalse(qa.execute(action,inputs,subproject="private")["ok"])
        with self.assertRaises(ValueError):
            ks.read("../../private/test",repo=self.root)
        current=ks.unit_path(self.root,uid)
        saved=self.root/"other.md"; saved.write_bytes(current.read_bytes())
        current.unlink(); current.symlink_to(saved)
        with self.assertRaisesRegex(ValueError,"symlink"):
            ks.read(uid,repo=self.root)

    def test_empty_delta_finishes_selected_task_without_fake_ku(self):
        task,packet=self.prepare(); self.stage(task,[])
        result=kc.apply(task["job_id"],repo=self.root)
        self.assertEqual(result["status"],"completed")
        self.assertFalse(result["coverage"]["exhaustive_knowledge"])
        self.assertFalse((self.root/"academic/knowledge/units").exists())

    def test_bounded_packet_reports_unread_ranges(self):
        source=self.receive("long.md","# Long paper\n"+"\n".join(f"Observation {i}: conditional algorithm with unresolved context. "*20 for i in range(200)))
        task,packet=self.prepare(source,max_tokens=4000)
        self.assertTrue(packet["coverage"]["unread_ranges"])
        self.assertLess(task["input_tokens"],6000)

    def test_current_source_unit_survives_five_cross_source_candidates(self):
        uid,_=self.create()
        foreign=self.receive("other.md","# Conditional algorithm\nDifferent source mentioning the topic.\n")
        ref,_=ks.evidence_ref(self.root,foreign['source_id'],foreign['parse_id'],1,2)
        other=[]
        for i in range(5):
            other.append(ks.commit(repo=self.root,operation_id=f"foreign-{i}",title="Conditional algorithm",
                statement=f"Related topic {i}.",evidence=[{**ref,'relation':'supports'}],reason="Test candidate competition")['id'])
        with patch.object(ks,'search',return_value={'candidates':[{'id':x} for x in other+[uid]]}):
            _,packet=self.prepare(refresh=True)
        self.assertEqual(packet['candidates'][0]['id'],uid)
        self.assertTrue(packet['candidates'][0]['can_update'])
        self.assertEqual(len(packet['candidates']),5)

    def test_source_confirmed_focus_target_precedes_same_source_background(self):
        task,packet=self.prepare()
        self.stage(task,[self.op(packet,title='TargetFlow 方法',statement='Only under condition A.')])
        target=kc.apply(task['job_id'],repo=self.root)['results']['item-1']['id']
        source=self.receive('focus-priority.md','# Methods\nTargetFlow and BackgroundFlow have different scopes.\n')
        ref,_=ks.evidence_ref(self.root,source['source_id'],source['parse_id'],1,2)
        background=ks.commit(repo=self.root,operation_id='background',title='BackgroundFlow 方法',
            statement='Background details.',evidence=[{**ref,'relation':'supports'}],reason='test')['id']
        with patch.object(ks,'search',return_value={'candidates':[{'id':background},{'id':target}]}):
            _,focused=self.prepare(source,focus='仅核对 targetflow')
            _,unconfirmed=self.prepare(source,focus='InstructionOnly')
        self.assertEqual(focused['candidates'][0]['id'],target)
        self.assertTrue(focused['candidates'][0]['can_update'])
        self.assertEqual(focused['candidate_retrieval']['priority_names'],['TargetFlow'])
        self.assertEqual(unconfirmed['candidates'][0]['id'],background)
        self.assertEqual(unconfirmed['candidate_retrieval']['priority_names'],[])

    def test_purpose_relevant_existing_unit_survives_incidental_name_competition(self):
        source = self.receive('purpose.md', '# MethodSet\nMethodSet uses ALPHA BETA GAMMA and defines token roles and values.\n')
        ref, _ = ks.evidence_ref(self.root, source['source_id'], source['parse_id'], 1, 2)
        background = [ks.commit(repo=self.root, operation_id=f'purpose-background-{i}',
            title='MethodSet ALPHA BETA GAMMA training', statement=f'Training detail {i}.',
            evidence=[ref], reason='Candidate competition fixture')['id'] for i in range(5)]
        target = ks.commit(repo=self.root, operation_id='purpose-target', title='MethodSet token roles and values',
            statement='A token decides retrieval, another marks support.', evidence=[ref], reason='Existing direct coverage')['id']
        with patch.object(ks, 'search', return_value={'candidates':[{'id':x} for x in background+[target]]}) as search:
            for strategy in ('survey', 'selective'):
                before = search.call_count
                _, packet = self.prepare(source, strategy=strategy, focus='MethodSet token roles and values')
                self.assertEqual(packet['candidates'][0]['id'], target)
                self.assertLessEqual(len(packet['candidates']), 5)
                self.assertLessEqual(search.call_count-before, 3)
            ordered, _ = kc.retrieve_candidates(self.root, source['source_id'], 'methodset', ['MethodSet','ALPHA','BETA','GAMMA'])
            self.assertEqual(ordered[0]['id'], background[0])

    def test_task_language_recalls_same_source_unit_without_source_word_overlap(self):
        source = self.receive('oscillation.md', '# Oscillation encoding\nA phase represents a periodic coordinate.\n')
        ref, _ = ks.evidence_ref(self.root, source['source_id'], source['parse_id'], 1, 2)
        uid = ks.commit(repo=self.root, operation_id='translated-periodic', title='周期相位编码的定义',
            statement='采用周期函数表示序列坐标，维度与输入一致。', evidence=[ref], reason='Translated source knowledge')['id']
        _, unfocused = self.prepare(source)
        self.assertNotIn(uid, [c['id'] for c in unfocused['candidates']])
        for strategy in ('survey', 'selective'):
            with self.subTest(strategy=strategy), patch.object(ks, 'search', wraps=ks.search) as search:
                _, packet = self.prepare(source, focus='补充周期相位编码的定义与适用条件', strategy=strategy)
                self.assertEqual(packet['candidates'][0]['id'], uid)
                self.assertLessEqual(search.call_count, 3)
                self.assertEqual(packet['candidates'][0]['can_update'], strategy == 'survey')
                self.assertLessEqual(len(packet['candidates']), 5)
                self.assertLessEqual(packet['candidate_retrieval']['pool_size'], 20)

    def test_focus_discovery_does_not_admit_unrelated_instruction_word_matches(self):
        uid, _ = self.create()
        old = ks.read(uid, repo=self.root)
        noise = ks.commit(repo=self.root, operation_id='instruction-noise', title='整理摘要维护已有条目',
            statement='整理摘要维护已有条目时如何归类。', evidence=old['evidence'], reason='Unrelated existing topic')['id']
        source = self.receive('periodic.md', '# Oscillation encoding\nA phase represents a periodic coordinate.\n')
        focus = '整理摘要维护已有条目'
        self.assertIn(noise, [c['id'] for c in ks.search(focus, repo=self.root)['candidates']])
        _, packet = self.prepare(source, focus=focus, strategy='selective')
        self.assertNotIn(noise, [c['id'] for c in packet['candidates']])
        self.assertGreater(packet['candidate_retrieval']['rejected_weak_matches'], 0)
        self.assertTrue(all(b['ref']['source_id'] == source['source_id'] for b in packet['blocks']))

    def test_purpose_packet_policy_versions_only_affected_jobs_and_retains_old_tasks(self):
        locator = self.source['parse_path']+'#L1-L2'
        with patch.object(kc, 'CANDIDATE_POLICY', 'previous-candidate-policy'):
            old, _ = self.prepare(focus='conditional algorithm')
            unfocused, _ = self.prepare()
            explicit, _ = self.prepare(focus='conditional algorithm', source_locators=[locator])
        current, _ = self.prepare(focus='conditional algorithm')
        self.assertNotEqual(current['job_id'], old['job_id'])
        self.assertEqual(self.prepare()[0]['job_id'], unfocused['job_id'])
        self.assertEqual(self.prepare(focus='conditional algorithm', source_locators=[locator])[0]['job_id'], explicit['job_id'])
        self.stage(old, [])
        self.assertEqual(kc.apply(old['job_id'], repo=self.root)['status'], 'completed')

    def test_incomplete_old_evidence_releases_only_new_blocks_for_later_candidate(self):
        old=self.receive('large.md','# LargeFlow\n'+'old evidence '*450+'\n'+'second evidence '*450+'\n')
        refs=[ks.evidence_ref(self.root,old['source_id'],old['parse_id'],i,i)[0] for i in [2,3]]
        large=ks.commit(repo=self.root,operation_id='large',title='LargeFlow method',
            statement='Conditional large method.',evidence=[{**r,'relation':'supports'} for r in refs],reason='test')['id']
        small=self.receive('small.md','# TinyFlow\nA compact method under condition A.\n')
        ref,_=ks.evidence_ref(self.root,small['source_id'],small['parse_id'],1,2)
        tiny=ks.commit(repo=self.root,operation_id='tiny',title='TinyFlow method',
            statement='Compact method.',evidence=[{**ref,'relation':'supports'}],reason='test')['id']
        source=self.receive('new.md','# Methods\nLargeFlow and TinyFlow are discussed.\n')
        with patch.object(ks,'search',return_value={'candidates':[{'id':large},{'id':tiny}]}):
            _,context=self.prepare(source,max_tokens=2700)
        candidates={c['id']:c for c in context['candidates']}
        self.assertFalse(candidates[large]['can_update'])
        self.assertEqual(candidates[large]['old_evidence_blocks'],[])
        self.assertTrue(candidates[tiny]['can_update'])
        source_ids={b['ref']['source_id'] for b in context['blocks']}
        self.assertNotIn(old['source_id'],source_ids)
        self.assertIn(source['source_id'],source_ids)
        self.assertIn(small['source_id'],source_ids)

    def test_focus_selects_late_passage_with_adjacent_conditions(self):
        lines=["General background with several ordinary observations. "*8 for _ in range(220)]
        lines[128]="## AsterFlow algorithm"
        lines[129]="AsterFlow supports a bounded queue."
        lines[130]="It only works when the input is REVERSIBLE; other inputs are excluded."
        source=self.receive("focused.md","\n".join(lines))
        _,packet=self.prepare(source,max_tokens=6000,focus="AsterFlow 的机制与限制")
        shown="\n".join(b["text"] for b in packet["blocks"])
        self.assertIn("REVERSIBLE",shown)
        self.assertIn("asterflow",packet['focus_selection']['matched_terms'])
        self.assertLess(kc.token_count(packet)+kc.token_count(kc.RULES),8000)

    def test_named_sections_precede_appendix_with_more_shared_words(self):
        chunks=[
            (1,3,"# Overview\nA general description."),
            (4,6,"## 3.1 Tensor Factorization of Queries, Keys, and Values\nThe core construction."),
            (7,9,"## 3.3 KV Caching and Memory Reduction\nThe cache accounting rule."),
            (10,12,"## Appendix: Alternative construction\nTensor factorization of queries, keys, values and KV caching memory reduction also occur in a different variant."),
        ]
        focus="Tensor Factorization of Queries, Keys, and Values; KV Caching and Memory Reduction"
        selected,_=kc.focused_chunks(chunks,focus)
        self.assertEqual(set(selected),{1,2})
        # A generic single-word heading should not displace a specific match.
        selected,_=kc.focused_chunks([(1,2,"# Attention\nOverview."),
                                     (3,4,"AsterFlow attention projection has a bounded input.")],
                                    "AsterFlow attention projection")
        self.assertEqual(selected[0],1)

    def test_unmatched_focus_keeps_source_selection(self):
        _,normal=self.prepare()
        _,focused=self.prepare(focus="CompletelyUnseenTerm")
        self.assertEqual(normal['coverage']['provided_ranges'],focused['coverage']['provided_ranges'])
        self.assertFalse(focused['focus_selection']['matched_terms'])

    def test_all_named_sections_precede_incidental_lexical_hits(self):
        titles = ['Event Formation', 'Memory Retrieval', 'Cache Accounting', 'Error Conditions']
        chunks = [(1, 2, 'Overview of event formation memory retrieval cache accounting error conditions.')]
        chunks += [(3+i*2, 4+i*2, f'## {title}\nA selected definition.')
                   for i, title in enumerate(titles)]
        for count in (3, 4):
            with self.subTest(count=count):
                selected, _ = kc.focused_chunks(chunks, '; '.join(titles[:count]))
                self.assertEqual(set(selected), set(range(1, count+1)))
                # Repeating a request or a heading within one chunk adds no slots.
                repeated = [*chunks]
                repeated[1] = (3, 4, '## Event Formation\n## Event Formation')
                selected, _ = kc.focused_chunks(repeated, '; '.join(titles[:count]+titles[:1]))
                self.assertEqual(set(selected), set(range(1, count+1)))

    def test_lexical_focus_still_fills_only_two_priority_slots(self):
        chunks = [(1, 2, '## Event Formation\nSelected events.'),
                  (3, 4, 'Memory retrieval strategy cache accounting details.'),
                  (5, 6, 'Another memory retrieval strategy.'),
                  (7, 8, 'Cache accounting elsewhere.')]
        for focus in ('Event Formation; memory retrieval strategy cache accounting',
                      'memory retrieval strategy cache accounting'):
            with self.subTest(focus=focus):
                selected, _ = kc.focused_chunks(chunks, focus)
                self.assertEqual(len(selected), 2)
                self.assertEqual(selected[0], 0 if focus.startswith('Event') else 1)

    def test_three_named_sections_reach_packet_before_neighbors(self):
        lines = ['Background observations with no requested definition. '*4 for _ in range(288)]
        lines[:32] = ['A short introduction.']*32
        titles = ['Event Formation', 'Memory Retrieval', 'Cache Accounting']
        for pos, title in zip((64, 160, 224), titles):
            lines[pos] = f'## {title}'
            lines[pos+1] = f'REQUIRED_{pos} is defined only under LOCAL_CONDITION_{pos}.'
        source = self.receive('three-requested-sections.md', '\n'.join(lines))
        _, packet = self.prepare(source, max_tokens=9000, focus='; '.join(titles))
        shown = '\n'.join(b['text'] for b in packet['blocks'])
        for pos in (64, 160, 224):
            self.assertIn(f'REQUIRED_{pos}', shown)
            self.assertIn(f'LOCAL_CONDITION_{pos}', shown)
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.instructions(packet)), 11000)
        # A tighter budget reports priority separately from actual coverage.
        _, small = self.prepare(source, max_tokens=2700, focus='; '.join(titles))
        self.assertEqual(len(small['focus_selection']['priority_ranges']), 3)
        ranges = small['coverage']['provided_ranges']
        self.assertTrue(any(not any(a <= lo and hi <= b for a,b in ranges)
                            for lo,hi in small['focus_selection']['priority_ranges']))
        self.assertTrue(small['coverage']['unread_ranges'])
        self.assertFalse(small['coverage']['exhaustive_knowledge'])
        self.assertLessEqual(kc.token_count(small)+kc.token_count(kc.instructions(small)), 4700)

    def test_shared_heading_words_do_not_hide_later_requested_section(self):
        cases = [
            ("MEMORY FORMATION VIA SURPRISE", "MEMORY RETRIEVAL",
             "Memory formation surprise retrieval.",
             "3.2 MEMORY FORMATION VIA SURPRISE; 3.4 MEMORY RETRIEVAL"),
            ("记忆形成", "记忆检索", "记忆形成和记忆检索。", "记忆形成；记忆检索"),
        ]
        for first, second, distractor, focus in cases:
            with self.subTest(focus=focus):
                chunks = [(1, 3, f"## 3.2 {first}\nEvent construction."),
                          (4, 6, f"## 3.4 {second}\nSelected events."),
                          (7, 9, f"## Overview\n{distractor}")]
                selected, _ = kc.focused_chunks(chunks, focus)
                self.assertEqual(set(selected), {0, 1})

    def test_separate_focus_requests_do_not_invent_a_heading_match(self):
        chunks = [(1, 3, "## Memory Retrieval\nSelected events."),
                  (4, 6, "## Retrieval Strategy\nA procedure."),
                  (7, 9, "General memory retrieval strategy details.")]
        for separator in (";", "；", "\n"):
            with self.subTest(separator=separator):
                selected, _ = kc.focused_chunks(chunks, f"Memory{separator} Retrieval Strategy")
                self.assertEqual(selected, [1, 2])

    def test_repeated_single_word_heading_has_no_extra_priority(self):
        chunks = [(1, 3, "## Memory memory\nOverview."),
                  (4, 6, "Memory retrieval strategy details."),
                  (7, 9, "Another retrieval strategy explanation.")]
        selected, _ = kc.focused_chunks(chunks, "memory memory retrieval strategy")
        self.assertEqual(selected, [1, 2])

    def test_shared_word_sections_reach_packet_under_existing_budget(self):
        lines = ["Ordinary background with several unrelated observations. " * 4 for _ in range(288)]
        lines[:32] = ["A short introduction."] * 32
        lines[32] = "## Memory Formation"
        lines[33] = "REQUIRED_FORMATION groups observations into events."
        lines[64] = "Memory formation and retrieval are discussed in the background."
        lines[224] = "## Memory Retrieval"
        lines[225] = "REQUIRED_RETRIEVAL selects events for the current query."
        source = self.receive("shared-heading-words.md", "\n".join(lines))
        _, packet = self.prepare(source, max_tokens=5000,
                                 focus="Memory Formation; Memory Retrieval")
        shown = "\n".join(b['text'] for b in packet['blocks'])
        self.assertIn("REQUIRED_FORMATION", shown)
        self.assertIn("REQUIRED_RETRIEVAL", shown)
        self.assertLessEqual(kc.token_count(packet) + kc.token_count(kc.RULES), 7000)

    def test_both_requested_sections_precede_first_section_neighbors(self):
        lines=["Ordinary background with several unrelated observations. "*4 for _ in range(288)]
        lines[:32]=["A short introduction."]*32
        lines[32]="## Cache Accounting Rule"
        lines[33]="REQUIRED_CACHE counts stored scalars only."
        lines[224]="## Factor Shape Definition"
        lines[225]="REQUIRED_FACTORS depend on the current input."
        source=self.receive("two-requested-sections.md","\n".join(lines))
        _,packet=self.prepare(source,max_tokens=5000,
                              focus="Cache Accounting Rule and Factor Shape Definition")
        shown="\n".join(b['text'] for b in packet['blocks'])
        self.assertIn("REQUIRED_CACHE",shown)
        self.assertIn("REQUIRED_FACTORS",shown)
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),7000)

    def test_explicit_focus_retains_required_method_before_unrelated_theorems(self):
        lines=["Background observations unrelated to the requested method. "*4 for _ in range(288)]
        for i in (32,64,96):
            lines[i]=f"Theorem {i}. A separate result about another model."
        lines[223]="The following method requires BOUNDARY_CONDITION."
        lines[224]="## AsterFlow mechanism"
        lines[225]="AsterFlow computes the requested projection."
        source=self.receive("focused-with-theorems.md","\n".join(lines))
        _,packet=self.prepare(source,max_tokens=6000,focus="AsterFlow mechanism")
        shown="\n".join(b['text'] for b in packet['blocks'])
        self.assertIn("AsterFlow computes",shown)
        self.assertIn("BOUNDARY_CONDITION",shown)
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),8000)

    def test_formal_condition_is_prioritized_beyond_abstract(self):
        lines=["General background has no formal result. "*12 for _ in range(240)]
        lines[128]="Theorem 1. The bound holds only for NONDEGENERATE inputs."
        lines[129]="An input exponentially close to identity is excluded."
        source=self.receive("formal.md","\n".join(lines))
        _,packet=self.prepare(source,max_tokens=6000)
        shown="\n".join(b["text"] for b in packet["blocks"])
        self.assertIn("NONDEGENERATE",shown)
        self.assertIn("exponentially close to identity",shown)

    def test_markdown_and_emphasized_formal_conditions_are_selected(self):
        for heading in ("## Theorem 2", "### **Corollary 2**", "**Definition 3.**", "## 定理二"):
            with self.subTest(heading=heading):
                lines=["General background has no formal result. "*12 for _ in range(240)]
                lines[128]=heading
                lines[129]="The result requires NONDEGENERATE inputs; degenerate inputs are excluded."
                source=self.receive("formatted.md","\n".join(lines))
                _,packet=self.prepare(source,max_tokens=6000)
                self.assertIn("NONDEGENERATE","\n".join(b['text'] for b in packet['blocks']))
                self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),8000)

    def test_multiple_main_results_precede_supporting_background(self):
        lines=["Ordinary background without a result. "*3 for _ in range(256)]
        lines[64]="## Proposition 1"
        lines[65]="A preliminary example only."
        lines[96]="## Theorem 1"
        lines[97]="FIRST_RESULT holds with the stated normalization."
        lines[128]="## Corollary 1"
        lines[129]="FIRST_SCALING requires bounded coefficients."
        lines[160]="## Theorem 2"
        lines[161]="SECOND_RESULT requires local structure."
        lines[190]="## Corollary 2"
        lines[192]="SECOND_SCALING additionally requires a nondegenerate input."
        source=self.receive("two-results.md","\n".join(lines))
        _,packet=self.prepare(source,max_tokens=8000)
        shown="\n".join(b['text'] for b in packet['blocks'])
        for term in ('FIRST_RESULT','FIRST_SCALING','SECOND_RESULT','SECOND_SCALING'):
            self.assertIn(term,shown)
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),10000)

    def test_unused_reserve_restores_formal_setup_without_losing_old_evidence(self):
        uid,_=self.create()
        lines=['Background context without a formal result. '*4 for _ in range(128)]
        lines[0]='# Conditional algorithm'
        lines[32]='Initialization requires INDEPENDENT_DRAWS and BOUNDED_MOMENTS.'
        lines[64]='Theorem 1. The variance bound uses the preceding initialization.'
        source=self.receive('formal-setup.md','\n'.join(lines))
        with patch.object(ks,'search',return_value={'candidates':[{'id':uid}]} ) as search:
            _,packet=self.prepare(source,max_tokens=6500)
        shown='\n'.join(b['text'] for b in packet['blocks'])
        self.assertIn('INDEPENDENT_DRAWS',shown)
        candidate=next(c for c in packet['candidates'] if c['id']==uid)
        self.assertTrue(candidate['can_update'])
        self.assertTrue(candidate['old_evidence_blocks'])
        self.assertLessEqual(search.call_count,2)
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),8500)

    def test_context_fill_never_displaces_complete_maintenance_evidence(self):
        old=self.receive('old-context.md','# Conditional algorithm\n'+'evidence '*700+'\n')
        ref,_=ks.evidence_ref(self.root,old['source_id'],old['parse_id'],2,2)
        uid=ks.commit(repo=self.root,operation_id='maintenance',title='Conditional algorithm',
            statement='A conditionally valid result.',evidence=[ref],reason='Test old evidence reservation')['id']
        lines=['Background context without a formal result. '*4 for _ in range(128)]
        lines[0]='# Conditional algorithm'
        lines[32]='Initialization requires INDEPENDENT_DRAWS and BOUNDED_MOMENTS.'
        lines[64]='Theorem 1. The variance bound uses the preceding initialization.'
        source=self.receive('formal-budget.md','\n'.join(lines))
        with patch.object(ks,'search',return_value={'candidates':[{'id':uid}]}):
            _,packet=self.prepare(source,max_tokens=6500)
        candidate=next(c for c in packet['candidates'] if c['id']==uid)
        self.assertTrue(candidate['can_update'])
        self.assertTrue(any(b['ref']['source_id']==old['source_id'] for b in packet['blocks']))
        self.assertNotIn('INDEPENDENT_DRAWS','\n'.join(b['text'] for b in packet['blocks']))
        self.assertLessEqual(kc.token_count(packet)+kc.token_count(kc.RULES),8500)

    def selective_source(self):
        return self.receive("selective.md", "# Conditional algorithm\n" + "\n".join(
            f"Section evidence line {i}: method X has condition A and numbered case {i}." for i in range(2, 180)))

    def read_request(self, packet, start=110, end=112, **kw):
        return {"schema": kc.READ_PROTOCOL, "source_locators": [packet["reading"]["source_path"]+f"#L{start}-L{end}"],
                "reason": "Need the condition in this source passage", **kw}

    def test_selective_packet_limits_initial_reading_and_defers_old_evidence(self):
        uid, _ = self.create()
        source = self.selective_source()
        ordinary, survey = self.prepare(source)
        task, packet = self.prepare(source, strategy="selective")
        self.assertNotEqual(task["job_id"], ordinary["job_id"])
        self.assertLess(kc.token_count(packet), kc.token_count(survey))
        self.assertLess(packet["coverage"]["provided_segments"], packet["coverage"]["total_segments"])
        candidate = next(c for c in packet["candidates"] if c["id"] == uid)
        self.assertFalse(candidate["can_update"])
        self.assertEqual(candidate["old_evidence_blocks"], [])
        self.assertEqual(candidate["source_relation"], "other_source")
        self.assertIn("refresh", task["agent_task"]["commands"])

    def test_selective_host_expansion_preserves_old_packet_and_commits_deep_evidence(self):
        source = self.selective_source()
        task, packet = self.prepare(source, strategy="selective")
        folder = kc.job_dir(self.root, task["job_id"])
        original = (folder/"packet.json").read_bytes()
        si.atomic_json(folder/"read-request.json", self.read_request(packet))
        with patch.object(ka, "run", side_effect=AssertionError("no API fallback")):
            expanded = kc.expand(task["job_id"], repo=self.root)
        state, current = kc.load_job(self.root, task["job_id"])
        self.assertEqual((folder/"packet.json").read_bytes(), original)
        self.assertEqual(current["blocks"][:len(packet["blocks"])], packet["blocks"])
        self.assertIn("packet-1.json", expanded["agent_task"]["inputs"][0]["path"])
        self.stage(task, [self.op(current, evidence=[{"block_id":current["blocks"][-1]["id"],"relation":"supports"}])])
        result = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["results"], result["results"])
        self.assertEqual(len(state["reading_history"]), 1)

    def test_selective_candidate_evidence_is_loaded_only_on_request(self):
        uid, _ = self.create()
        task, packet = self.prepare(strategy="selective")
        candidate = next(c for c in packet["candidates"] if c["id"] == uid)
        self.assertEqual(candidate["source_relation"], "same_source")
        with self.assertRaisesRegex(ValueError, "old evidence incomplete"):
            kc.validate_operation(self.root, packet, self.op(packet, action="attach", target=uid))
        folder = kc.job_dir(self.root, task["job_id"])
        si.atomic_json(folder/"read-request.json", self.read_request(packet, source_locators=[], candidate_ids=[uid]))
        kc.expand(task["job_id"], repo=self.root)
        _, expanded = kc.load_job(self.root, task["job_id"])
        candidate = next(c for c in expanded["candidates"] if c["id"] == uid)
        self.assertTrue(candidate["can_update"])
        self.assertTrue(candidate["old_evidence_blocks"])
        self.assertEqual(kc.validate_operation(self.root, expanded, self.op(expanded, action="attach", target=uid))["action"], "attach")

    def long_read_source(self):
        lines = ["# Exact requested evidence"] + [f"Condition {i}: " + "x" * 400 for i in range(2, 151)]
        return self.receive("long-read.md", "\n".join(lines)), lines

    def test_selective_large_read_preserves_every_line_and_commits_bound_blocks(self):
        source, lines = self.long_read_source()
        task, original = self.prepare(source, strategy="selective")
        folder = kc.job_dir(self.root, task["job_id"])
        request = self.read_request(original, start=70, end=110)
        si.atomic_json(folder/"read-request.json", request)
        kc.expand(task["job_id"], repo=self.root)
        state, packet = kc.load_job(self.root, task["job_id"])
        added = packet["blocks"][len(original["blocks"]):]
        self.assertGreater(len(added), 1)
        self.assertEqual("\n".join(b["text"] for b in added), "\n".join(lines[69:110]))
        self.assertEqual(packet["blocks"][:len(original["blocks"])], original["blocks"])
        self.assertEqual(packet["reading"]["round"], 1)
        self.assertEqual(state["reading_history"][0]["request"], request)
        for block in added:
            self.assertLessEqual(len(block["text"]), 8000)
            self.assertEqual(ks.resolve_evidence(self.root, block["ref"]), block["text"])
        evidence = [{"block_id":b["id"],"relation":"supports"} for b in added]
        self.stage(task, [self.op(packet, evidence=evidence)])
        result = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed", result)
        uid = result["results"]["item-1"]["id"]
        saved = ks.read(uid, repo=self.root)
        self.assertEqual(len(saved["evidence"]), len(added))
        self.assertEqual(kc.apply(task["job_id"], repo=self.root)["results"], result["results"])
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 1)

    def test_selective_unsplittable_or_empty_range_fails_atomically(self):
        for content in ("x" * 8001, "   "):
            with self.subTest(length=len(content)):
                lines = ["# Evidence"] + ["Readable source content"] * 120
                lines[90] = content
                source = self.receive(f"bad-line-{len(content)}.md", "\n".join(lines))
                task, packet = self.prepare(source, strategy="selective")
                folder = kc.job_dir(self.root, task["job_id"])
                before = (folder/"state.json").read_bytes()
                si.atomic_json(folder/"read-request.json", self.read_request(packet, start=91, end=91))
                with self.assertRaises(ValueError):
                    kc.expand(task["job_id"], repo=self.root)
                self.assertEqual((folder/"state.json").read_bytes(), before)
                self.assertFalse((folder/"packet-1.json").exists())

    def test_selective_invalid_or_redundant_reads_do_not_mutate_packet(self):
        source = self.selective_source()
        task, packet = self.prepare(source, strategy="selective")
        folder = kc.job_dir(self.root, task["job_id"])
        before = (folder/"state.json").read_bytes()
        requests = [self.read_request(packet, source_locators=["private/raw/secret.md#L1-L3"]),
                    self.read_request(packet, start=2000, end=2010),
                    self.read_request(packet, start=1, end=2),
                    self.read_request(packet, source_locators=[], candidate_ids=["KU-"+"0"*24]),
                    self.read_request(packet, operations=[])]
        for request in requests:
            si.atomic_json(folder/"read-request.json", request)
            with self.assertRaises(ValueError):
                kc.expand(task["job_id"], repo=self.root)
            self.assertEqual((folder/"state.json").read_bytes(), before)
        self.assertFalse((folder/"packet-1.json").exists())

    def test_selective_stale_candidate_blocks_zero_operation_completion(self):
        uid, _ = self.create()
        task, packet = self.prepare(strategy="selective")
        path = self.root/"academic/knowledge/units"/(uid+".md")
        path.write_text(path.read_text()+"\nChanged conditions.\n")
        self.stage(task, [])
        with self.assertRaisesRegex(ValueError, "comparison knowledge changed"):
            kc.apply(task["job_id"], repo=self.root)

    def test_selective_updated_candidate_recovers_lost_receipt_without_second_revision(self):
        uid, _ = self.create()
        task, packet = self.prepare(strategy="selective")
        folder = kc.job_dir(self.root, task["job_id"])
        si.atomic_json(folder/"read-request.json", self.read_request(packet, source_locators=[], candidate_ids=[uid]))
        kc.expand(task["job_id"], repo=self.root)
        _, packet = kc.load_job(self.root, task["job_id"])
        op = self.op(packet, action="revise", target=uid, statement="Method X requires condition A; its generality is unknown.")
        self.stage(task, [op])
        real_save = kc.save_state
        def interrupted(repo, state):
            if state["results"]:
                raise OSError("receipt interrupted")
            real_save(repo, state)
        with patch.object(kc, "save_state", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "receipt interrupted"):
                kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 2)
        self.stage(task, [{**op, "statement":"Changed after the actual commit"}])
        with self.assertRaisesRegex(ValueError, "comparison knowledge changed"):
            kc.apply(task["job_id"], repo=self.root)
        self.stage(task, [op])
        self.assertTrue(kc.check(task["job_id"], repo=self.root)["ready"])
        result = kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["results"]["item-1"]["reused"])
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 2)

    def test_selective_lost_receipt_does_not_hide_a_later_concurrent_revision(self):
        uid, _ = self.create()
        task, packet = self.prepare(strategy="selective")
        folder = kc.job_dir(self.root, task["job_id"])
        si.atomic_json(folder/"read-request.json", self.read_request(packet, source_locators=[], candidate_ids=[uid]))
        kc.expand(task["job_id"], repo=self.root)
        _, packet = kc.load_job(self.root, task["job_id"])
        self.stage(task, [self.op(packet, action="revise", target=uid, statement="A bounded revised result.")])
        real_save = kc.save_state
        def interrupted(repo, state):
            if state["results"]:
                raise OSError("receipt interrupted")
            real_save(repo, state)
        with patch.object(kc, "save_state", side_effect=interrupted):
            with self.assertRaises(OSError): kc.apply(task["job_id"], repo=self.root)
        current = ks.read(uid, repo=self.root)
        ks.commit(repo=self.root, operation_id="later-concurrent-update", unit_id=uid,
                  expected_revision=current["revision"], expected_sha256=current["snapshot_sha256"],
                  title=current["title"], statement="A later, different result.", evidence=current["evidence"], reason="Concurrent update")
        with self.assertRaisesRegex(ValueError, "comparison knowledge changed"):
            kc.apply(task["job_id"], repo=self.root)
        self.assertEqual(ks.read(uid, repo=self.root)["revision"], 3)

    def test_selective_api_direct_zero_delta_and_resume_use_one_call(self):
        call = self.mock_api(lambda p: {"ok":True,"parsed":{"schema":kc.PROTOCOL,"operations":[]},
                                      "usage":{"total_tokens":12},"history":[{"status":"ok"}]})
        first = ka.run(self.source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(first["status"], "completed", first)
        self.assertEqual(first["results"], {})
        second = ka.run(self.source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(second["new_model_calls"], 0)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(second["api_usage"]["usage"]["total_tokens"], 12)

    def test_selective_api_reads_then_generates_and_recovers_saved_read_response(self):
        source = self.selective_source()
        def response(p):
            value = self.read_request(p) if p["reading"]["round"] == 0 else {"schema":kc.PROTOCOL,"operations":[self.op(p)]}
            return {"ok":True,"parsed":value,"usage":{"total_tokens":13},"history":[{"status":"ok"}]}
        call = self.mock_api(response)
        write = si.atomic_json
        def interrupt(path, value):
            if path.name == "packet-1.json":
                raise OSError("interrupted after response before expansion")
            write(path, value)
        with patch.object(si, "atomic_json", side_effect=interrupt):
            failed = ka.run(source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(failed["status"], "failed", failed)
        recovered = ka.run(source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(recovered["status"], "completed", recovered)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(recovered["api_usage"]["usage"]["total_tokens"], 26)
        state, packet = kc.load_job(self.root, recovered["job_id"])
        self.assertEqual(packet["reading"]["round"], 1)
        with self.assertRaisesRegex(ValueError, "API-owned"):
            kc.expand(recovered["job_id"], repo=self.root)

    def test_selective_api_read_limit_retains_partial_and_stops_replay(self):
        source = self.selective_source()
        call = self.mock_api(lambda p: {"ok":True,"parsed":self.read_request(p, start=100+p["reading"]["round"]*10,
                        end=102+p["reading"]["round"]*10),"history":[{"status":"ok"}]})
        result = ka.run(source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(result["status"], "partial", result)
        self.assertTrue(result["unresolved"])
        self.assertEqual(call.call_count, 3)
        self.assertEqual(ka.run(source["source_id"], repo=self.root, strategy="selective")["new_model_calls"], 0)
        self.assertEqual(si.inspect_source(source["source_id"],repo=self.root)["status"], "source_ready")

    def test_selective_saved_large_read_resumes_without_regenerating_request(self):
        source, _ = self.long_read_source()
        def response(packet):
            value = (self.read_request(packet, start=70, end=110) if packet["reading"]["round"] == 0
                     else {"schema":kc.PROTOCOL,"operations":[]})
            return {"ok":True,"parsed":value,"usage":{"total_tokens":13},"history":[{"status":"ok"}]}
        call = self.mock_api(response)
        with patch.object(kc, "source_read_ranges", side_effect=ValueError("old single-block limit")):
            failed = ka.run(source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(failed["status"], "failed", failed)
        folder = kc.job_dir(self.root, failed["job_id"])
        saved = (folder/"round-0-response-1.json").read_bytes()
        result = ka.run_selective(failed["job_id"], repo=self.root)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(call.call_count, 2)
        self.assertEqual((folder/"round-0-response-1.json").read_bytes(), saved)
        self.assertEqual(result["api_usage"]["usage"]["total_tokens"], 26)
        self.assertEqual(ka.run_selective(failed["job_id"], repo=self.root)["new_model_calls"], 0)

    def test_selective_api_failed_attempt_is_retained_and_requires_explicit_retry(self):
        call = self.mock_api(lambda p: {"ok":False,"error":"offline provider","usage":{"total_tokens":9},"history":[{"status":"api_error"}]})
        failed = ka.run(self.source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(failed["status"], "failed")
        blocked = ka.run(self.source["source_id"], repo=self.root, strategy="selective")
        self.assertEqual(blocked["new_model_calls"], 0)
        call.side_effect = lambda *a,**k: {"ok":True,"parsed":{"schema":kc.PROTOCOL,"operations":[]},
                                        "usage":{"total_tokens":7},"history":[{"status":"ok"}]}
        result = ka.run(self.source["source_id"], repo=self.root, strategy="selective", retry=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["api_usage"]["usage"]["total_tokens"], 16)
        self.assertEqual(len(list(kc.job_dir(self.root,result["job_id"]).glob("round-0-response-*.json"))), 2)

    def test_selective_expansion_budget_failure_is_atomic(self):
        source = self.receive("large.md", "# Detailed conditions\n"+"\n".join(
            f"Condition {i}: "+"alpha beta gamma delta "*45 for i in range(150)))
        task, packet = self.prepare(source, strategy="selective", max_tokens=6000)
        folder = kc.job_dir(self.root,task["job_id"])
        original = (folder/"state.json").read_bytes()
        locators=[packet["reading"]["source_path"]+f"#L{i}-L{i+6}" for i in (50,60,70,80,90)]
        for requested in (locators, [packet["reading"]["source_path"]+"#L50-L100"]):
            si.atomic_json(folder/"read-request.json", self.read_request(packet, source_locators=requested))
            with self.assertRaisesRegex(ValueError, "budget"):
                kc.expand(task["job_id"],repo=self.root)
            self.assertEqual((folder/"state.json").read_bytes(),original)
            self.assertFalse((folder/"packet-1.json").exists())

    def test_selective_response_binding_blocks_replay_and_delta_failure_reuses_response(self):
        call = self.mock_api()
        write = si.atomic_json
        def interrupt(path,value):
            if path.name == "delta.json":
                raise OSError("interrupted after final response")
            write(path,value)
        with patch.object(si,"atomic_json",side_effect=interrupt):
            failed=ka.run(self.source["source_id"],repo=self.root,strategy="selective")
        folder=kc.job_dir(self.root,failed["job_id"])
        state,_=kc.load_job(self.root,failed["job_id"])
        path=folder/state["response_file"]
        original=json.loads(path.read_text())
        damaged={**original,"request_binding":{**original["request_binding"],"packet_sha256":"0"*64}}
        write(path,damaged)
        blocked=ka.run(self.source["source_id"],repo=self.root,strategy="selective")
        self.assertEqual(blocked["status"],"failed")
        self.assertIn("binding mismatch",blocked["error"])
        self.assertEqual(call.call_count,1)
        write(path,original)
        result=ka.run(self.source["source_id"],repo=self.root,strategy="selective")
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(call.call_count,1)

    def mock_api(self, response_fn=None):
        import llm_structured as llm
        self.stack.enter_context(patch.object(ka.agent_task,"ingest_backend",return_value="api"))
        self.stack.enter_context(patch.object(llm,"load_env",return_value={"LLM_MODEL":"mock","LLM_API_BASE":"mock"}))
        def call(prompt,*args,**kw):
            packet=json.loads(prompt.split("材料包：\n",1)[1])
            delta={"schema":kc.PROTOCOL,"job_id":packet["job_id"],"operations":[self.op(packet)]}
            return response_fn(packet) if response_fn else {"ok":True,"parsed":delta,"model":"mock","usage":{"total_tokens":100},"history":[{"status":"ok"}]}
        return self.stack.enter_context(patch.object(llm,"call_json",side_effect=call))

    def test_api_compiles_once_and_replay_makes_zero_calls(self):
        call=self.mock_api()
        first=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(first["status"],"completed",first)
        replay=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(call.call_count,1)
        self.assertEqual(replay["new_model_calls"],0)
        self.assertEqual(first["results"],replay["results"])

    def test_api_failure_requires_explicit_retry_and_source_stays_ready(self):
        call=self.mock_api(lambda _: {"ok":False,"error":"provider unavailable","history":[{"status":"api_error"}]})
        self.assertEqual(ka.run(self.source["source_id"],repo=self.root)["status"],"failed")
        self.assertTrue(ka.run(self.source["source_id"],repo=self.root)["retry_required"])
        self.assertEqual(call.call_count,1)
        ka.run(self.source["source_id"],repo=self.root,retry=True)
        self.assertEqual(call.call_count,2)
        self.assertEqual(si.inspect_source(self.source["source_id"],repo=self.root)["status"],"source_ready")

    def test_api_request_binds_identity_without_changing_model_semantics(self):
        for reported in (None, "KJ-foreign-request", 42):
            with self.subTest(reported=reported):
                def response(packet):
                    value={"schema":kc.PROTOCOL,"operations":[self.op(packet)],"unresolved":[]}
                    if reported is not None:
                        value["job_id"]=reported
                    return {"ok":True,"parsed":value,"text":json.dumps(value),"history":[{"status":"ok"}]}
                call=self.mock_api(response)
                result=ka.run(self.source["source_id"],repo=self.root,refresh=True)
                self.assertEqual(result["status"],"completed",result)
                state,packet=kc.load_job(self.root,result["job_id"])
                folder=kc.job_dir(self.root,result["job_id"])
                saved=json.loads((folder/"model-response.json").read_text())
                delta=json.loads((folder/"delta.json").read_text())
                self.assertEqual(saved["parsed"].get("job_id"),reported)
                self.assertEqual(delta["job_id"],state["job_id"])
                self.assertEqual(delta["operations"],saved["parsed"]["operations"])
                self.assertEqual(saved["request_binding"],ka.request_binding(state))
                checker=call.call_args.args[1]
                self.assertTrue(checker(saved["parsed"]))
                self.assertFalse(checker({"schema":kc.PROTOCOL,"operations":"not an array"}))

    def test_api_request_identity_does_not_authorize_unknown_evidence(self):
        def response(packet):
            return {"ok":True,"parsed":{"schema":kc.PROTOCOL,"operations":[self.op(packet,
                evidence=[{"block_id":"B-not-provided","relation":"supports"}])]},"history":[{"status":"ok"}]}
        self.mock_api(response)
        result=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(result["status"],"partial",result)
        self.assertEqual(result["results"]["item-1"]["status"],"failed")
        self.assertEqual(list((self.root/"academic/knowledge/units").glob("KU-*.md")),[])

    def test_saved_api_response_binding_blocks_cross_job_or_packet_reuse(self):
        self.mock_api(lambda p:{"ok":True,"parsed":{"schema":kc.PROTOCOL,"operations":[]},"history":[{"status":"ok"}]})
        real_write=si.atomic_json
        def fail_delta(path,value):
            if path.name=="delta.json":
                raise OSError("interrupted before delta publication")
            return real_write(path,value)
        with patch.object(si,"atomic_json",side_effect=fail_delta):
            result=ka.run(self.source["source_id"],repo=self.root)
        state,packet=kc.load_job(self.root,result["job_id"])
        folder=kc.job_dir(self.root,result["job_id"])
        original=json.loads((folder/"model-response.json").read_text())
        import llm_structured as llm
        for field in ("job_id","packet_sha256","policy"):
            damaged=json.loads(json.dumps(original));damaged["request_binding"][field]="different"
            si.atomic_json(folder/"model-response.json",damaged)
            with patch.object(llm,"call_json",side_effect=AssertionError("must not call model")):
                rejected=ka.run(self.source["source_id"],repo=self.root,retry=True)
            self.assertEqual(rejected["status"],"failed")
            self.assertIn("binding mismatch",rejected["error"])
            self.assertFalse((folder/"delta.json").exists())
        si.atomic_json(folder/"model-response.json",original)
        with patch.object(llm,"call_json",side_effect=AssertionError("must reuse bound response")):
            recovered=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(recovered["status"],"completed",recovered)
        self.assertEqual(recovered["new_model_calls"],0)

    def test_api_binding_keeps_legacy_identity_and_shared_schema_strict(self):
        state={"job_id":"KJ-"+"a"*24,"packet_sha256":"b"*64}
        legacy={"parsed":{"schema":kc.PROTOCOL,"job_id":state["job_id"],"operations":[]}}
        self.assertEqual(ka.response_delta(legacy,state),legacy["parsed"])
        legacy["parsed"]["job_id"]="KJ-"+"c"*24
        with self.assertRaisesRegex(ValueError,"job identity"):
            ka.response_delta(legacy,state)
        malformed={"parsed":{"schema":kc.PROTOCOL,"operations":"invalid"},"request_binding":ka.request_binding(state)}
        with self.assertRaisesRegex(ValueError,"schema"):
            ka.response_delta(malformed,state)
        self.assertFalse(kc.delta_schema({"schema":kc.PROTOCOL,"operations":[]}))

    def test_saved_model_response_recovers_without_another_model_call(self):
        call=self.mock_api(); real_write=si.atomic_json
        def fail_delta(path,value):
            if path.name=="delta.json":
                raise OSError("interrupted before delta publication")
            return real_write(path,value)
        with patch.object(si,"atomic_json",side_effect=fail_delta):
            result=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(result["status"],"failed")
        result=ka.run(self.source["source_id"],repo=self.root)
        self.assertEqual(result["status"],"completed",result)
        self.assertEqual(call.call_count,1)

    def test_api_adapter_rejects_agent_backend(self):
        with patch.object(ka.agent_task,"ingest_backend",return_value="agent"):
            with self.assertRaisesRegex(ValueError,"API compilation requires"):
                ka.run(self.source["source_id"],repo=self.root)

    def test_inbox_source_and_knowledge_completion_are_independent(self):
        import ingest_inbox as inbox
        argv=["ingest_inbox.py","--run","--profile","source-first","--knowledge","agent","--file",str(self.root/"first.md")]
        with patch.object(inbox,"REPO",self.root),patch("sys.argv",argv),redirect_stdout(io.StringIO()) as output:
            inbox.main()
        result=json.loads(output.getvalue())["files"][0]
        self.assertEqual(result["source_status"],"source_ready")
        self.assertEqual(result["knowledge_status"],"prepared")
        self.assertEqual(result["knowledge"]["agent_task"]["schema"],"agent-task-v1")

    def test_ku_read_does_not_count_as_raw_citation(self):
        import query_orchestrate as qo
        uid,_=self.create()
        session=qo.QuerySession(query="conditional",mode="api",stage="evidence")
        result=qo.execute_plan(session,[{"action":"knowledge_read","input":{"unit_id":uid}}])
        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(session.read_sources,[])

    def test_cli_query_returns_ku_and_can_rebuild_projection(self):
        uid,_=self.create()
        for command in (["ku-search","conditional"],["ku-read",uid]):
            args=wg.build_parser().parse_args(command)
            with redirect_stdout(io.StringIO()) as output:
                args.func(args)
            self.assertTrue(json.loads(output.getvalue())["ok"])
        (self.root/"academic/knowledge/index.sqlite").unlink()
        self.assertEqual(ks.rebuild_index(repo=self.root)["indexed"],1)
        self.assertTrue(ks.search("conditional",repo=self.root)["candidates"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Isolated Source-first transactions, recovery and real query/read contracts."""
import io
import json
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import source_ingest as si
import source_locator as sl
import query_actions as qa
import query_orchestrate as qo
import wg


class SourceIngestTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("unit tests are offline")))
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        for mod, name in ((sl, "REPO"), (qa, "_REPO"), (wg, "REPO")):
            self.stack.enter_context(patch.object(mod, name, self.root))
        self.source = self.root / "input.md"
        self.source.write_text("# Tensor networks\nContraction complexity depends on topology.\n张量网络用于量子模拟。\n")

    def ingest(self, path=None, **kw):
        return si.ingest(path or self.source, repo=self.root, **kw)

    def receipt(self, result):
        return si.load_receipt(self.root, result["source_id"])

    def bundle(self, text="The measured spin correlation is conditional.\n", name="bundle"):
        folder = self.root / name
        folder.mkdir()
        (folder / "paper.pdf").write_bytes(b"%PDF-test-original")
        (folder / "paper.md").write_text(text)
        return folder

    def test_source_is_searchable_without_wiki_graph_or_llm(self):
        result = self.ingest()
        self.assertEqual(result["status"], "source_ready")
        self.assertFalse(result["legacy_completed"])
        self.assertEqual(result["semantic_llm_calls"], 0)
        self.assertTrue(self.source.exists())
        hit = si.search("contraction", repo=self.root)["candidates"][0]
        read = qa.execute("read_raw", {"locator": hit["locator"]})
        self.assertTrue(read["ok"], read)
        self.assertIn("depends on topology", read["text"])
        self.assertTrue(si.search("量子模拟", repo=self.root)["candidates"])
        self.assertFalse((self.root / "cross-domain/graph.db").exists())
        self.assertFalse((self.root / "academic/wiki").exists())

    def test_same_bytes_reuse_identity_and_keep_all_origins(self):
        a = self.ingest()
        other = self.root / "renamed.md"
        other.write_bytes(self.source.read_bytes())
        with patch.object(si, "parse_source", side_effect=AssertionError("no reparse")):
            b = self.ingest(other)
        self.assertEqual(a["source_id"], b["source_id"])
        self.assertEqual(b["origin_count"], 2)
        self.assertEqual(len(self.receipt(b)["pending_jobs"]), 1)
        self.source.write_text("A scientifically different version.\n")
        self.assertNotEqual(self.ingest()["source_id"], a["source_id"])

    def test_late_match_excerpt_keeps_bound_full_passage_locator(self):
        body = "Background context. " * 60 + "\nOnly after approval, quarterly reports include the next work plan.\n"
        self.source.write_text(body)
        self.ingest()
        hit = si.search("quarterly reports approval", repo=self.root)["candidates"][0]
        self.assertIn("Only after approval", hit["excerpt"])
        self.assertTrue(hit["locator"].endswith("#L1-L2"))
        span = hit["excerpt_span"]
        self.assertEqual(hit["excerpt"], body.rstrip("\n")[span["start"]:span["end"]])
        self.assertLessEqual(len(hit["excerpt"]), 700)
        read = qa.execute("read_raw", {"locator": hit["locator"]})
        self.assertTrue(read["ok"], read)
        self.assertIn("Background context.", read["text"])
        self.assertIn("Only after approval", read["text"])

    def test_same_source_body_precedes_reference_but_citations_stay_searchable(self):
        lines=["# Study", "Trainability requires a nondegenerate input."]+[""]*30
        lines += ["## References", "Aster. Trainability and optimization. DOI: example."]
        self.source.write_text("\n".join(lines))
        self.ingest()
        ordinary=si.search("Aster trainability",repo=self.root,topk=5)
        self.assertIn('nondegenerate',ordinary['candidates'][0]['excerpt'])
        self.assertEqual(ordinary['candidates'][0]['section_role'],'unspecified')
        self.assertTrue(any(c['section_role']=='references' for c in ordinary['candidates']))
        cited=si.search("Aster trainability DOI",repo=self.root,topk=5)
        self.assertEqual(cited['candidates'][0]['section_role'],'references')
        only=si.search("Aster",repo=self.root,topk=5)
        self.assertEqual(only['candidates'][0]['section_role'],'references')
        read=qa.execute('read_raw',{'locator':ordinary['candidates'][0]['locator']})
        self.assertTrue(read['ok'],read)
        self.assertIn('nondegenerate',read['text'])

    def test_reference_section_boundaries_and_mixed_segments(self):
        lines=['# Study','## Reference implementation','Aster method.','## IV. References',
               'Aster citation.','### Further reading','More citations.','## Appendix','Aster derivation.']
        self.assertEqual(si.reference_section_lines(lines),{4,5,6,7})
        self.source.write_text('\n'.join(lines))
        self.ingest()
        result=si.search('Aster',repo=self.root)
        self.assertEqual(result['candidates'][0]['section_role'],'unspecified')
        fenced=['```markdown','## References','sample','```','Actual body.']
        self.assertFalse(si.reference_section_lines(fenced))

    def test_source_only_stays_searchable_without_pending_knowledge_after_resume(self):
        result=self.ingest(knowledge_required=False)
        self.assertEqual(result["knowledge_status"],"not_required")
        self.assertEqual(result["knowledge_policy"],"source_only")
        self.assertEqual(result["semantic_llm_calls"],0)
        (self.root/"academic/sources/index.sqlite").unlink()
        resumed=si.resume(result["source_id"],repo=self.root)
        self.assertEqual(resumed["knowledge_status"],"not_required")
        self.assertTrue(si.search("contraction",repo=self.root)["candidates"])
        self.assertTrue(all(j["status"]=="not_required" for j in self.receipt(resumed)["pending_jobs"]))
        self.assertFalse((self.root/"temp/knowledge-compile").exists())

    def test_source_only_uses_same_zero_model_inbox_and_wg_entry(self):
        import ingest_inbox as inbox
        args=["ingest_inbox.py","--run","--profile","source-first","--knowledge","none","--file",str(self.source)]
        with patch.object(inbox,"REPO",self.root),patch("sys.argv",args), \
             patch.object(inbox.agent_task,"ingest_backend",side_effect=AssertionError("no classifier/backend")),redirect_stdout(io.StringIO()) as output:
            inbox.main()
        result=json.loads(output.getvalue())["files"][0]
        self.assertEqual(result["knowledge_status"],"not_required")
        self.assertNotIn("knowledge",result)
        parsed=wg.build_parser().parse_args(["ingest",str(self.source),"--profile","source-first","--knowledge","none"])
        with patch.object(wg,"run_script",return_value=(0,'{"status":"source_ready","files":[]}',"")) as run,redirect_stdout(io.StringIO()):
            parsed.func(parsed)
        command=run.call_args.args[0]
        self.assertEqual(command[command.index("--knowledge")+1],"none")

    def test_concurrent_duplicate_receives_commit_once(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.ingest(), range(3)))
        self.assertEqual(len({r["source_id"] for r in results}), 1)
        record = self.receipt(results[0])
        self.assertEqual(len(record["origins"]), 3)
        self.assertEqual(len(record["pending_jobs"]), 1)
        self.assertEqual(len(record["parses"]), 1)

    def test_parse_failure_retains_original_and_is_resumable(self):
        with patch.object(si, "parse_source", side_effect=ValueError("parser unavailable")):
            failed = self.ingest()
        self.assertEqual(failed["status"], "received")
        self.assertEqual((self.root / failed["source_path"]).read_bytes(), self.source.read_bytes())
        self.assertEqual(si.search("tensor", repo=self.root)["candidates"], [])
        resumed = si.resume(failed["source_id"], repo=self.root)
        self.assertEqual(resumed["status"], "source_ready")

    def test_index_failure_does_not_reparse_on_resume(self):
        with patch.object(si, "connect_index", side_effect=OSError("disk busy")):
            failed = self.ingest()
        self.assertEqual(failed["error"]["phase"], "index")
        self.assertEqual(failed["status"], "received")
        with patch.object(si, "parse_source", side_effect=AssertionError("no reparse")):
            self.assertEqual(si.resume(failed["source_id"], repo=self.root)["status"], "source_ready")

    def test_failed_reindex_revokes_readiness(self):
        ready = self.ingest()
        with patch.object(si, "connect_index", side_effect=OSError("disk busy")):
            failed = si.resume(ready["source_id"], repo=self.root)
        self.assertEqual(failed["status"], "received")
        self.assertFalse(si.search("tensor", repo=self.root)["candidates"])
        self.assertEqual(si.resume(ready["source_id"], repo=self.root)["status"], "source_ready")

    def test_receipt_failure_after_index_commit_replays_without_parser(self):
        original_write = si.write_receipt
        def fail_ready(repo, record):
            if record["source_status"] == "source_ready":
                raise OSError("receipt publication interrupted")
            return original_write(repo, record)
        with patch.object(si, "write_receipt", side_effect=fail_ready):
            with self.assertRaises(OSError):
                self.ingest()
        sid = "S-" + si.digest(self.source)[:24]
        self.assertFalse(si.search("tensor", repo=self.root)["candidates"])
        with patch.object(si, "parse_source", side_effect=AssertionError("no reparse")):
            self.assertEqual(si.resume(sid, repo=self.root)["status"], "source_ready")

    def test_interrupted_receive_reingest_recovers_same_source(self):
        with patch.object(si, "copy_original", side_effect=OSError("copy interrupted")):
            with self.assertRaises(OSError):
                self.ingest()
        sid = "S-" + si.digest(self.source)[:24]
        with self.assertRaisesRegex(ValueError, "re-ingest"):
            si.resume(sid, repo=self.root)
        self.assertEqual(self.ingest()["source_id"], sid)

    def test_deleted_index_can_be_rebuilt_without_reparse(self):
        ready = self.ingest()
        si.index_path(self.root).unlink()
        self.assertFalse(si.search("tensor", repo=self.root)["candidates"])
        self.assertFalse(si.index_path(self.root).exists())
        self.assertEqual(si.inspect_source(ready["source_id"], repo=self.root)["status"], "unavailable")
        with patch.object(si, "parse_source", side_effect=AssertionError("no reparse")):
            si.resume(ready["source_id"], repo=self.root)
        self.assertTrue(si.search("tensor", repo=self.root)["candidates"])

    def test_index_is_only_a_projection_and_cannot_fabricate_excerpt(self):
        ready = self.ingest()
        conn = si.connect_index(self.root, write=True)
        with conn:
            conn.execute("UPDATE chunks SET body='fabricated result' WHERE source_id=?", (ready["source_id"],))
        conn.close()
        result = si.search("tensor", repo=self.root)
        self.assertFalse(result["candidates"])
        self.assertIn("excerpt mismatch", result["issues"][0]["error"])
        si.resume(ready["source_id"], repo=self.root)
        self.assertTrue(si.search("tensor", repo=self.root)["candidates"])

    def test_batch_failure_does_not_block_next_source(self):
        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"\xff\x00")
        batch = si.ingest_many([invalid, self.source], repo=self.root)
        self.assertEqual(batch["status"], "partial")
        self.assertEqual([r["status"] for r in batch["files"]], ["received", "source_ready"])

    def test_bound_pdf_reuse_and_old_parse_locator_survives(self):
        folder = self.bundle()
        first = self.ingest(folder / "paper.pdf", reuse_parse=folder)
        old_locator = si.search("correlation", repo=self.root)["candidates"][0]["locator"]
        (folder / "paper.md").write_text("Updated parse of the same measured correlation.\n")
        second = self.ingest(folder / "paper.pdf", reuse_parse=folder)
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertNotEqual(first["parse_id"], second["parse_id"])
        self.assertEqual(len(self.receipt(second)["parses"]), 2)
        self.assertIn("is conditional", qa.execute("read_raw", {"locator": old_locator})["text"])
        self.assertIn(first["source_path"], qa.raw_citation_sources(old_locator))
        self.assertNotEqual(si.search("correlation", repo=self.root)["candidates"][0]["locator"], old_locator)

    def test_mismatched_bundle_is_rejected_after_original_received(self):
        folder = self.bundle()
        pdf = self.root / "other.pdf"
        pdf.write_bytes(b"%PDF-other-edition")
        result = self.ingest(pdf, reuse_parse=folder)
        self.assertEqual(result["status"], "received")
        self.assertIn("different original", result["error"]["message"])

    def test_missing_image_preserves_searchable_text_with_limitations(self):
        folder = self.bundle("Result ![figure](images/missing.png)\n")
        result = self.ingest(folder / "paper.pdf", reuse_parse=folder)
        self.assertEqual(result["status"], "source_ready")
        self.assertTrue(any("缺少 1 个" in note for note in result["limitations"]))
        self.assertEqual(self.receipt(result)["parses"][0]["missing_assets"], ["images/missing.png"])
        self.assertTrue(si.search("Result", repo=self.root)["candidates"])

    def test_failed_bundle_reuse_stays_on_same_route_until_explicit_refresh(self):
        folder = self.bundle()
        (folder / "paper.md").unlink()
        first = self.ingest(folder / "paper.pdf", reuse_parse=folder)
        self.assertEqual(first["status"], "received")
        with patch.object(si, "parse_source", wraps=si.parse_source) as parse:
            repeated = self.ingest(folder / "paper.pdf")
            self.assertEqual(parse.call_args.kwargs["reuse_parse"], str(folder))
            self.assertEqual(repeated["status"], "received")
            si.resume(first["source_id"], repo=self.root)
            self.assertEqual(parse.call_args.kwargs["reuse_parse"], str(folder))
        (folder / "paper.md").write_text("Restored measured correlation.\n")
        self.assertEqual(si.resume(first["source_id"], repo=self.root)["status"], "source_ready")

    def test_managed_parser_pins_cache_and_refresh_versions_configuration(self):
        folder = self.bundle()
        config = {"extraction": {"mineru": {"model_version": "first"}}}
        calls = []
        def extract(paper_id, *, engine, papers_dir):
            calls.append(engine)
            (papers_dir / paper_id / "paper.md").write_text("Measured result " + config["extraction"]["mineru"]["model_version"])
            return True
        adapter = SimpleNamespace(__file__=str(Path(si.__file__).with_name("extractor.py")),
                                  load_config=lambda: config, extract_paper=extract)
        with patch.dict(sys.modules, {"extractor": adapter}):
            first = self.ingest(folder / "paper.pdf")
            config["extraction"]["mineru"]["model_version"] = "second"
            pinned = self.ingest(folder / "paper.pdf")
            self.assertEqual(first["parse_id"], pinned["parse_id"])
            refreshed = si.resume(first["source_id"], repo=self.root, refresh_parse=True)
            self.assertEqual(refreshed["status"], "source_ready")
            self.assertNotEqual(first["parse_id"], refreshed["parse_id"])
            si.resume(first["source_id"], repo=self.root, refresh_parse=True)
        self.assertEqual(calls, ["mineru", "mineru"])
        self.assertEqual(len(self.receipt(refreshed)["parses"]), 2)

    def test_tampered_text_rejected_by_search_and_direct_read(self):
        ready = self.ingest()
        locator = si.search("tensor", repo=self.root)["candidates"][0]["locator"]
        (self.root / ready["parse_path"]).write_text("Forged claim.\n")
        result = si.search("tensor", repo=self.root)
        self.assertFalse(result["candidates"])
        self.assertTrue(result["issues"])
        self.assertFalse(qa.execute("read_raw", {"locator": locator})["ok"])

    def test_direct_source_read_cannot_escape_binding_through_public_symlink(self):
        ready = self.ingest()
        carrier = self.root / ready["parse_path"]
        carrier.unlink()
        carrier.symlink_to(self.source)
        result = qa.execute("read_raw", {"locator": ready["parse_path"] + "#L1-L3"})
        self.assertFalse(result["ok"], result)

    def test_private_scope_and_symlink_rejected(self):
        self.ingest()
        for inputs, scope in (({"query": "tensor"}, "private"),
                              ({"query": "tensor", "domain": "academic"}, "private"),
                              ({"query": "tensor", "domain": "admin"}, "public")):
            self.assertFalse(qa.execute("source_search", inputs, subproject=scope)["ok"])
        private = self.root / "private/input.md"
        private.parent.mkdir()
        private.write_text("Secret.\n")
        with self.assertRaisesRegex(ValueError, "private"):
            self.ingest(private)
        link = self.root / "link.md"
        link.symlink_to(private)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.ingest(link)
        with self.assertRaisesRegex(ValueError, "invalid source ID"):
            si.resume("../../private/target", repo=self.root)

    def test_managed_index_symlink_is_not_followed(self):
        self.ingest()
        index = si.index_path(self.root)
        index.unlink()
        target = self.root / "outside.sqlite"
        index.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            si.search("tensor", repo=self.root)
        self.assertFalse(target.exists())

    def test_coverage_limits_and_fts_literal_query_are_visible(self):
        self.source.write_text("Readable result.\n" + "X"*7000 + "\nAdditional searchable content.\n")
        result = self.ingest()
        self.assertEqual(result["index"]["skipped_long_lines"], [2])
        self.assertTrue(any("超长行" in x for x in result["limitations"]))
        self.assertTrue(si.search('readable OR " -- NOT (result)', repo=self.root)["candidates"])
        self.assertFalse(si.search("", repo=self.root)["candidates"])

    def test_api_and_cli_source_search_return_real_readable_locator(self):
        ready = self.ingest()
        self.assertIn("source_search", qo.STAGE_ACTIONS["start"])
        self.assertIn("source_search", qo._ACTION_SIGS)
        result = qa.execute("source_search", {"query": "topology"})
        self.assertTrue(result["ok"], result)
        hit = json.loads(result["text"])["candidates"][0]
        args = wg.build_parser().parse_args(["source-search", "topology"])
        output = io.StringIO()
        with redirect_stdout(output):
            args.func(args)
        self.assertTrue(json.loads(output.getvalue())["ok"])
        args = wg.build_parser().parse_args(["read-raw", hit["locator"]])
        output = io.StringIO()
        with redirect_stdout(output):
            args.func(args)
        read = json.loads(output.getvalue())
        self.assertTrue(read["ok"], read)
        self.assertEqual(read["result"]["source_path"], ready["source_path"])

    def test_api_loop_receives_search_locator_and_actual_evidence(self):
        ready = self.ingest()
        hit = si.search("topology", repo=self.root)["candidates"][0]
        calls = []
        def model(prompt):
            calls.append(prompt)
            if len(calls) == 1:
                return {"status": "ok", "parsed": {"decision": "discover",
                    "strategy": {"status": "skipped", "reason": "direct source lookup"},
                    "plan": [{"action": "source_search", "input": {"query": "topology"}}]}}
            if len(calls) == 2:
                self.assertIn(hit["locator"], prompt)
                return {"status": "ok", "parsed": {"decision": "read",
                    "plan": [{"action": "read_raw", "input": {"locator": hit["locator"]}}]}}
            self.assertIn("Contraction complexity depends on topology.", prompt)
            return {"status": "ok", "parsed": {"decision": "answer", "answer": "depends on topology",
                "citations": [ready["source_path"]]}}
        result = qo._api_query_loop(qo.QuerySession(query="topology", mode="api"), model)
        self.assertTrue(result["citation_check"]["ok"], result)
        self.assertEqual(len(calls), 3)

    def test_dsh_prompt_preserves_evidence_after_long_source_header(self):
        from dsh.agent_loop import AgentLoop
        loop = AgentLoop()
        content = "source header "*30 + "EVIDENCE_MUST_REACH_MODEL"
        prompt = loop._build_prompt("question", 2, [{"name": "read_raw", "content": content}])
        self.assertIn("EVIDENCE_MUST_REACH_MODEL", prompt)

    def test_inbox_source_profile_never_enters_semantic_backend(self):
        import ingest_inbox as inbox
        args = ["ingest_inbox.py", "--run", "--profile", "source-first", "--file", str(self.source)]
        with patch.object(inbox, "REPO", self.root), patch("sys.argv", args), \
             patch.object(inbox.agent_task, "ingest_backend", side_effect=AssertionError("legacy backend")), \
             redirect_stdout(io.StringIO()) as output:
            inbox.main()
        self.assertEqual(json.loads(output.getvalue())["status"], "source_ready")

    def test_wg_source_profile_forwards_options_and_partial_failure(self):
        args = wg.build_parser().parse_args(["ingest", str(self.source), "--profile", "source-first", "--refresh-parse"])
        output = io.StringIO()
        with patch.object(wg, "run_script", return_value=(1, '{"status":"partial","files":[]}', "")) as run, redirect_stdout(output):
            args.func(args)
        self.assertIn("--profile", run.call_args.args[0])
        self.assertIn("--refresh-parse", run.call_args.args[0])
        self.assertFalse(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()

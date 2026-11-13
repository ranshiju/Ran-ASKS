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

    def test_structural_read_preserves_hit_and_reaches_condition_across_all_readers(self):
        lines = ["# Fixture"] + ["p" * 210] * 26 + ["", "$$", "anchorformula = x", "$$", "",
                 "where x is a fixed target independent of parameters. " + "q" * 1000,
                 "", "# Unrelated", "Do not include this section."]
        self.source.write_text("\n".join(lines))
        ready = self.ingest()
        before = {p: p.read_bytes() for p in (self.root / "academic").rglob("*") if p.is_file()}
        hit = si.search("anchorformula", repo=self.root)["candidates"][0]
        self.assertTrue(hit['locator'].endswith('#L1-L32'))
        self.assertTrue(hit['read_input']['locator'].endswith('#L1-L33'))
        old = qa.execute('read_raw', {'locator': hit['locator']})
        self.assertNotIn('fixed target', old['text'])
        current = qa.execute(hit['read_action'], hit['read_input'])
        self.assertTrue(current['ok'], current)
        self.assertIn('fixed target', current['text'])
        self.assertNotIn('Do not include', current['text'])
        stream = io.StringIO()
        with redirect_stdout(stream):
            wg.cmd_read_raw(SimpleNamespace(locator=hit['read_input']['locator']))
        cli = json.loads(stream.getvalue())
        self.assertTrue(cli['ok'], cli)
        self.assertGreater(len(cli['result']['text']), 6000)
        self.assertEqual(cli['result']['text'], '\n'.join(lines[:33]))
        outline = si.outline(ready['source_id'], repo=self.root, query='Fixture')['candidates'][0]
        self.assertEqual(outline['read_input'], hit['read_input'])
        session = qo.QuerySession(query='anchorformula', mode='api')
        qo.execute_plan(session, [{'action': 'source_search', 'input': {'query': 'anchorformula'}}])
        self.assertEqual(session.read_sources, [])
        ref = next(k for k,v in session.read_options.items() if v['input'] == hit['read_input'])
        session.stage = 'evidence'
        qo.execute_plan(session, [{'action': 'read_ref', 'input': {'ref': ref}}])
        self.assertIn(hit['read_input']['locator'], session.read_sources)
        self.assertNotIn(hit['locator'], session.read_sources)
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / 'academic').rglob('*') if p.is_file()})

    def test_reading_context_completes_math_from_either_side(self):
        lines = ['# Model', '$$', 'loss =', '1 - overlap', '$$', '', 'where the target is fixed.', '', '# Next']
        for start, end in [(3,4), (7,7), (2,5)]:
            nav = si.reading_navigation(lines, start, end, 'academic/raw/test.md')
            self.assertEqual(nav['read_input']['locator'], 'academic/raw/test.md#L2-L7')
        self.assertEqual(si.reading_navigation(lines, 2, 7, 'academic/raw/test.md'), {})
        self.assertEqual(si.reading_navigation(lines, 9, 9, 'academic/raw/test.md'), {})

    def test_reading_context_keeps_paragraphs_and_skips_fenced_pseudo_math(self):
        lines = ['# A', 'a wrapped', 'paragraph.', '', '# B', 'other paragraph', '',
                 '```md', '$$', 'not math', '$$', 'where code ends.', '```', '',
                 '$$', 'real = x', '$$', '', '# New section', 'where unrelated.']
        nav = si.reading_navigation(lines, 3, 3, 'x.md')
        self.assertEqual(nav['read_input']['locator'], 'x.md#L2-L3')
        self.assertEqual(si.reading_navigation(lines, 10, 10, 'x.md'), {})
        self.assertEqual(si.reading_navigation(lines, 15, 17, 'x.md'), {})

    def test_reading_context_handles_definition_display_and_chinese_markers(self):
        lines = [r'\[', 'a = b', r'\]', '', '其中：', '', '$$', 'b = c', '$$', '', '式中 c 为常量。']
        nav = si.reading_navigation(lines, 7, 9, 'x.md')
        self.assertEqual(nav['read_input']['locator'], 'x.md#L1-L11')

    def test_reading_context_limits_disclose_without_partial_or_unbounded_expansion(self):
        for lines, end in [(['$$','x = y','$$','','where ' + 'z'*2001], 3),
                           (['word']*40, 1), (['a'*6000,'b'*2000], 1)]:
            nav = si.reading_navigation(lines, 1, end, 'x.md')
            self.assertEqual(nav['reading_context']['status'], 'limited')
            self.assertNotIn('read_input', nav)
        unclosed = ['# A', '$$', 'x = y', '', '# B', 'new paragraph']
        nav = si.reading_navigation(unclosed, 3, 3, 'x.md')
        self.assertEqual(nav['reading_context']['reason'], 'unclosed_display_math')
        self.assertEqual(si.reading_navigation(unclosed, 6, 6, 'x.md'), {})

    def test_outline_locates_deep_table_without_knowledge_or_index_and_never_writes(self):
        self.source.write_text("# Experiment\n" + "Unrelated paragraph.\n" * 80 +
                               "## Wall clock time\n<table><tr><td>1.57s</td></tr></table>\n\nTable 10: timing setup\n")
        ready = self.ingest()
        si.index_path(self.root).unlink()
        before = {p: p.read_bytes() for p in (self.root / "academic").rglob("*") if p.is_file()}
        result = qa.execute("source_outline", {"source_id": ready["source_id"], "query": "table 10"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["usage"]["role"], "navigation")
        hits = json.loads(result["text"])["candidates"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Table 10: timing setup")
        read = qa.execute("read_raw", {"locator": hits[0]["locator"]})
        self.assertTrue(read["ok"], read)
        self.assertIn("1.57s", read["text"])
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / "academic").rglob("*") if p.is_file()})

    def test_outline_paginates_literal_structure_ignores_fences_and_does_not_invent_matches(self):
        self.source.write_text("# Start\nOrdinary Table 5 mention\n```md\n# Forged\nTable 5: code sample\n```\n"
                               "## Results\nTable 5: values\nFigure 2: plot\n图3：说明\n")
        sid = self.ingest()["source_id"]
        first = si.outline(sid, repo=self.root, limit=2)
        second = si.outline(sid, repo=self.root, limit=2, start_line=first["next_start_line"])
        third = si.outline(sid, repo=self.root, start_line=second["next_start_line"])
        titles = [c["title"] for page in [first, second, third] for c in page["candidates"]]
        self.assertEqual(titles, ["Start", "Results", "Table 5: values", "Figure 2: plot", "图3：说明"])
        self.assertIsNone(third["next_start_line"])
        self.assertEqual(si.outline(sid, repo=self.root, query="Table 5")["candidates"][0]["line"], 8)
        self.assertFalse(si.outline(sid, repo=self.root, query="missing table")["candidates"])
        self.assertFalse(si.outline(sid, repo=self.root, start_line=1000)["candidates"])
        for kwargs in [{"start_line": 0}, {"limit": 0}, {"limit": 81}]:
            with self.assertRaises(ValueError): si.outline(sid, repo=self.root, **kwargs)

    def test_outline_requires_bound_source_and_rejects_private_and_tampered_carriers(self):
        ready = self.ingest()
        inputs = {"source_id": ready["source_id"]}
        self.assertFalse(qa.execute("source_outline", inputs, subproject="private")["ok"])
        self.assertFalse(qa.execute("source_outline", {"source_id": "../../private/file"})["ok"])
        carrier = self.root / ready["parse_path"]
        carrier.write_text("# Altered\n")
        self.assertFalse(qa.execute("source_outline", inputs)["ok"])
        carrier.unlink(); carrier.symlink_to(self.source)
        self.assertFalse(qa.execute("source_outline", inputs)["ok"])

    def test_outline_api_handles_are_navigation_until_guarded_raw_read(self):
        self.source.write_text("# Experiment\nTable 10: measured timing\nResult = 1.57s.\n")
        ready = self.ingest()
        session = qo.QuerySession(query="Table 10", mode="api")
        out = qo.execute_plan(session, [{"action": "source_outline", "input": {
            "source_id": ready["source_id"], "query": "Table 10"}}])
        self.assertTrue(out["results"][0]["ok"], out)
        self.assertFalse(session.read_sources)
        self.assertFalse(session.retained_results)
        self.assertEqual(len(session.read_options), 1)
        session.stage = "evidence"
        out = qo.execute_plan(session, [{"action": "read_ref", "input": {"ref": next(iter(session.read_options))}}])
        self.assertTrue(out["results"][0]["ok"], out)
        self.assertIn("1.57s", session.retained_results[0]["text"])
        self.assertTrue(session.read_sources)
        session.stage = "answer"
        out = qo.execute_plan(session, [{"action": "source_outline", "input": {"source_id": ready["source_id"]}}])
        self.assertFalse(out["results"][0]["ok"])

    def test_outline_cli_and_dsh_use_same_source_bound_navigation(self):
        from dsh.tools import build_tools
        ready = self.ingest()
        args = wg.build_parser().parse_args(["source-outline", ready["source_id"], "--query", "Tensor"])
        output = io.StringIO()
        with redirect_stdout(output): args.func(args)
        cli = json.loads(output.getvalue())
        self.assertTrue(cli["ok"], cli)
        self.assertEqual(cli["usage"]["role"], "navigation")
        tool = next(t for t in build_tools() if t.name == "source_outline")
        payload = json.loads(tool.execute_fn({"source_id": ready["source_id"], "query": "Tensor"}))
        self.assertEqual(payload["candidates"], cli["result"]["candidates"])
        self.assertEqual(payload["usage"]["role"], "navigation")

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

    def test_search_labels_same_filename_sources_from_bound_opening_headings(self):
        sources = []
        for name, title in (("a", "Trainability of classifiers"), ("b", "Classification with tensor networks")):
            folder = self.root / name
            folder.mkdir()
            path = folder / "paper.md"
            path.write_text(f"# {title}\nClassifier result.\n")
            sources.append(self.ingest(path))
        before = {p: p.read_bytes() for p in (self.root / "academic").rglob("*") if p.is_file()}
        hits = si.search("Classifier", repo=self.root)["candidates"]
        self.assertEqual({h["title"] for h in hits}, {"Trainability of classifiers", "Classification with tensor networks"})
        for hit in hits:
            self.assertEqual(hit["filename_title"], "paper")
            self.assertEqual(hit["title_basis"], "opening_heading_unverified")
            self.assertTrue(qa.execute("read_raw", {"locator": hit["locator"]})["ok"])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_search_heading_is_only_an_opening_navigation_label(self):
        for body in ("Introductory prose.\n# Later result\nClassifier result.",
                     "```md\n# Code example\n```\nClassifier result.",
                     "## Abstract\nClassifier result."):
            with self.subTest(body=body):
                self.source.write_text(body)
                source = self.ingest()
                hit = next(h for h in si.search("Classifier", repo=self.root, topk=20)["candidates"]
                           if h["source_id"] == source["source_id"])
                self.assertEqual(hit["title"], "input")
                self.assertEqual(hit["title_basis"], "filename_unverified")

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

    def source_entry(self, *extra, profile="source-first", resume=None, dry_run=False):
        import ingest_inbox as inbox
        args = ["ingest_inbox.py", "--profile", profile]
        args += ["--resume", resume] if resume else ["--file", str(self.source)]
        if not dry_run:
            args.append("--run")
        args += list(extra)
        code = 0
        with patch.object(inbox, "REPO", self.root), patch("sys.argv", args), \
             redirect_stdout(io.StringIO()) as output:
            try:
                inbox.main()
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def test_scoped_agent_entry_reuses_completed_zero_delta_without_api(self):
        import knowledge_compile as kc
        import knowledge_compile_api as ka
        focus = "仅核验当前用途；已有来源足够，不要求新增知识。"
        with patch.object(ka, "run", side_effect=AssertionError("no API fallback")):
            code, first = self.source_entry("--knowledge", "agent", "--focus", focus)
            self.assertEqual(code, 0)
            self.assertEqual(first["knowledge_status"], "prepared")
            job = first["knowledge_run"]
            state, packet = kc.load_job(self.root, job)
            self.assertEqual(packet["focus"], focus)
            self.assertEqual(state["executor"], "agent")
            self.assertIsNotNone(first["files"][0]["knowledge"]["agent_task"])
            si.atomic_json(kc.job_dir(self.root, job)/"delta.json",
                           {"schema": kc.PROTOCOL, "job_id": job, "operations": [], "unresolved": []})
            self.assertEqual(kc.apply(job, repo=self.root)["status"], "completed")
            code, replay = self.source_entry("--knowledge", "agent", "--focus", focus,
                                            resume=first["files"][0]["source_id"])
            self.assertEqual(code, 0)
            self.assertEqual(replay["knowledge_run"], job)
            self.assertEqual(replay["knowledge_status"], "completed")
            self.assertEqual(replay["knowledge"]["results"], {})
            self.assertIsNone(replay["knowledge"]["agent_task"])
            _, changed = self.source_entry("--knowledge", "agent", "--focus", "different purpose")
            self.assertNotEqual(changed["knowledge_run"], job)

    def test_selective_source_entry_and_wg_forward_strategy(self):
        import knowledge_compile as kc
        code,result=self.source_entry("--knowledge","agent","--strategy","selective")
        self.assertEqual(code,0)
        state,packet=kc.load_job(self.root,result["knowledge_run"])
        self.assertEqual(packet["reading"]["round"],0)
        self.assertEqual(state["executor"],"agent")
        args=wg.build_parser().parse_args(["ingest",str(self.source),"--profile","source-first",
                                           "--knowledge","api","--strategy","selective"])
        with patch.object(wg,"run_script",return_value=(0,'{"status":"source_ready","files":[]}',"")) as run,redirect_stdout(io.StringIO()):
            args.func(args)
        command=run.call_args.args[0]
        self.assertEqual(command[command.index("--strategy")+1],"selective")

    def test_selective_invalid_modes_reject_before_source_write(self):
        for mode in ("none","skip"):
            code,result=self.source_entry("--knowledge",mode,"--strategy","selective")
            self.assertEqual(code,1)
            self.assertIn("strategy",result["error"])
            self.assertFalse((self.root/"academic").exists())
        with patch("sys.stderr",new=io.StringIO()):
            code,_=self.source_entry("--strategy","selective",profile="legacy")
        self.assertEqual(code,2)
        self.assertFalse((self.root/"academic").exists())

    def test_focus_invalid_combinations_fail_before_writes(self):
        for mode in ("none", "skip"):
            for dry_run in (False, True):
                with self.subTest(mode=mode, dry_run=dry_run):
                    code, result = self.source_entry("--knowledge", mode, "--focus", "purpose", dry_run=dry_run)
                    self.assertEqual(code, 1)
                    self.assertIn("--focus", result["error"])
                    self.assertFalse((self.root/"academic").exists())
        with patch("sys.stderr", new=io.StringIO()):
            code, _ = self.source_entry("--focus", "purpose", profile="legacy")
        self.assertEqual(code, 2)
        self.assertFalse((self.root/"academic").exists())

    def test_api_partial_is_visible_and_focus_passed_without_host_takeover(self):
        import knowledge_compile_api as ka
        import knowledge_compile as kc
        response = {"job_id": "KJ-test", "status": "partial", "results": {}, "new_model_calls": 1}
        with patch.object(ka, "run", return_value=response) as run, \
             patch.object(kc, "prepare", side_effect=AssertionError("host must not take over")):
            code, result = self.source_entry("--knowledge", "api", "--focus", "direct sampling")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "source_ready")
        self.assertEqual(result["knowledge_status"], "partial")
        self.assertEqual(result["knowledge_run"], "KJ-test")
        self.assertNotIn("knowledge_error", result)
        self.assertEqual(run.call_args.kwargs["focus"], "direct sampling")

    def test_api_failed_result_returns_nonzero_but_source_remains_searchable(self):
        import knowledge_compile_api as ka
        response = {"job_id": "KJ-test", "status": "failed", "error": "provider unavailable", "new_model_calls": 1}
        with patch.object(ka, "run", return_value=response) as run:
            code, result = self.source_entry("--knowledge", "api", "--focus", "direct sampling")
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "source_ready")
        self.assertEqual(result["knowledge_status"], "failed")
        self.assertEqual(result["knowledge_error"], "provider unavailable")
        self.assertEqual(result["knowledge_run"], "KJ-test")
        self.assertTrue(si.search("contraction", repo=self.root)["candidates"])
        self.assertEqual((self.root/result["files"][0]["source_path"]).read_bytes(), self.source.read_bytes())

    def test_preparation_exception_does_not_mislabel_previous_job(self):
        import knowledge_compile as kc
        import knowledge_compile_api as ka
        original = self.ingest()
        old = kc.prepare(original["source_id"], repo=self.root)
        with patch.object(ka, "run", side_effect=ValueError("backend mismatch")):
            code, result = self.source_entry("--knowledge", "api", "--focus", "purpose", resume=original["source_id"])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "source_ready")
        self.assertIsNone(result["knowledge_run"])
        self.assertEqual(result["knowledge_error"], "backend mismatch")
        self.assertEqual(si.load_receipt(self.root, original["source_id"])["knowledge_run"], old["job_id"])

    def test_partial_failure_is_not_confused_with_coverage_only_partial(self):
        import knowledge_compile_api as ka
        response = {"job_id": "KJ-test", "status": "partial", "unresolved": ["unread scope"],
                    "results": {"item-1": {"status": "failed", "error": "evidence changed"}}}
        with patch.object(ka, "run", return_value=response):
            code, result = self.source_entry("--knowledge", "api")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "source_ready")
        self.assertEqual(result["knowledge_status"], "partial")
        self.assertIn("knowledge_error", result)
        self.assertEqual(result["files"][0]["knowledge"]["unresolved"], ["unread scope"])

    def test_partial_agent_replay_keeps_unresolved_in_intake_receipt(self):
        import knowledge_compile as kc
        _, result = self.source_entry("--knowledge", "agent", "--focus", "purpose")
        job = result["knowledge_run"]
        si.atomic_json(kc.job_dir(self.root, job)/"delta.json",
                       {"schema": kc.PROTOCOL, "job_id": job, "operations": [], "unresolved": ["needed passage missing"]})
        kc.apply(job, repo=self.root)
        code, result = self.source_entry("--knowledge", "agent", "--focus", "purpose")
        self.assertEqual(code, 0)
        self.assertEqual(result["knowledge_status"], "partial")
        self.assertEqual(result["files"][0]["knowledge"]["unresolved"], ["needed passage missing"])
        self.assertNotIn("knowledge_error", result)

    def test_failed_source_never_starts_requested_knowledge(self):
        import knowledge_compile_api as ka
        with patch.object(si, "parse_source", side_effect=ValueError("parse unavailable")), \
             patch.object(ka, "run", side_effect=AssertionError("source not ready")) as run:
            code, result = self.source_entry("--knowledge", "api", "--focus", "purpose")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["files"][0]["source_status"], "received")
        self.assertEqual(run.call_count, 0)

    def test_wg_forwards_focus_and_preserves_independent_failure_receipt(self):
        payload = {"status": "source_ready", "knowledge_status": "failed", "knowledge_run": "KJ-test",
                   "knowledge_error": "provider unavailable", "files": [{"source_path": "academic/raw/source.md"}]}
        args = wg.build_parser().parse_args(["ingest", str(self.source), "--profile", "source-first",
                                            "--knowledge", "api", "--focus", "purpose with spaces"])
        with patch.object(wg, "run_script", return_value=(1, json.dumps(payload), "")) as run, \
             redirect_stdout(io.StringIO()) as output:
            wg.cmd_ingest(args)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("provider unavailable", result["error"])
        self.assertIn("source_ready", json.dumps(result))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--focus")+1], "purpose with spaces")
        for flags in ([], ["--profile", "source-first"], ["--profile", "source-first", "--knowledge", "none"]):
            args = wg.build_parser().parse_args(["ingest", str(self.source), "--focus", "purpose", *flags])
            with patch.object(wg, "run_script", side_effect=AssertionError("reject before invoking intake")), \
                 redirect_stdout(io.StringIO()) as output:
                wg.cmd_ingest(args)
            self.assertFalse(json.loads(output.getvalue())["ok"])

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
        self.assertTrue(any("1 个独立图片资源" in note and "按需提取" in note for note in result["limitations"]))
        self.assertEqual(self.receipt(result)["parses"][0]["missing_assets"], ["images/missing.png"])
        self.assertTrue(si.search("Result", repo=self.root)["candidates"])

    def test_old_missing_resource_notice_is_projected_without_receipt_or_raw_changes(self):
        folder = self.bundle("# Result\nA textual conclusion. ![figure](images/missing.png)\n")
        result = self.ingest(folder / "paper.pdf", reuse_parse=folder, knowledge_required=False)
        record = self.receipt(result)
        record["parses"][0]["limitations"] = [
            "公式尚未核对。",
            "解析包缺少 1 个引用图片；正文可检索，图像须回原件核对，不视为完整图表解析。"]
        si.write_receipt(self.root, record)
        paths = [si.receipt_path(self.root, result["source_id"]),
                 self.root / result["source_path"], self.root / result["parse_path"]]
        before = [p.read_bytes() for p in paths]
        with patch.object(si, "parse_source", side_effect=AssertionError("no image re-extraction")):
            current = si.inspect_source(result["source_id"], repo=self.root)
            hit = si.search("textual conclusion", repo=self.root)["candidates"][0]
        self.assertEqual(current["source_status"], "source_ready")
        self.assertEqual(current["knowledge_status"], "not_required")
        self.assertEqual(current["parse_id"], result["parse_id"])
        self.assertIn("公式尚未核对。", current["limitations"])
        self.assertEqual(current["limitations"], hit["limitations"])
        self.assertTrue(any("1 个独立图片资源" in s for s in current["limitations"]))
        self.assertFalse(any("解析包缺少" in s for s in current["limitations"]))
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertEqual(self.receipt(result)["parses"][0]["missing_assets"], ["images/missing.png"])

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

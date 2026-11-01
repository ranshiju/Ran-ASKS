#!/usr/bin/env python3
"""N3 retrieval behavior in a tiny isolated corpus; no model or network."""
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph_lib as gl
import knowledge_store as ks
import query_actions as qa
import query_orchestrate as qo
import source_ingest as si
import source_locator as sl
import wiki_locator as wl
import wg
import lexical_relevance as lr


class UnifiedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.public = self.root/"cross-domain/graph.db"
        self.private = self.root/"private/graph.db"
        for mod, key, val in ((qa,"_REPO",self.root),(gl,"REPO",self.root),(gl,"GRAPH_DB",self.public),
                (gl,"PRIVATE_GRAPH_DB",self.private),(gl,"PRIVATE_DIR",self.private.parent),
                (sl,"REPO",self.root),(wl,"REPO",self.root),(wg,"REPO",self.root)):
            self.stack.enter_context(patch.object(mod,key,val))
        self.stack.enter_context(patch.object(socket.socket,"connect",side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(qa.ns,"semantic_search",return_value={"candidates":[]}))
        input_path=self.root/"input.md"
        input_path.write_text("# Conditional result\nUnder condition A, method X may improve efficiency.\n")
        self.source=si.ingest(input_path,repo=self.root)
        ref,_=ks.evidence_ref(self.root,self.source["source_id"],self.source["parse_id"],1,2)
        self.ref={**ref,"relation":"supports"}

    def statement(self):
        return ks.commit(repo=self.root,operation_id="first-statement",title="Conditional method X",
            statement="Under condition A, method X may improve efficiency.",claim_mode="conjecture",
            content_role="scientific_finding",
            unresolved=["Generality remains unknown"],evidence=[self.ref],reason="Capture conditional claim")["id"]

    def question(self):
        return ks.capture_question(repo=self.root,operation_id="first-question",title="Conditional method X generality",
            question="Does conditional method X generalize?",claim_mode="conjecture",
            source_question_status="addressed",
            unresolved=["Later studies not assessed"],evidence=[self.ref])["id"]

    def wiki(self, graph=True):
        page="academic/wiki/papers/conditional"
        path=self.root/(page+".md");path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(f"---\ntitle: Conditional result\nstatus: current\nsources:\n  - {self.ref['locator']}\n---\n\n# Conditional result\n"
            "\n## Navigation\n\nConditional method X.\n\n## Content\n\nUnder condition A, method X may improve efficiency.[^r]\n"
            f"\n[^r]: {self.ref['locator']}\n")
        if graph:
            self.public.parent.mkdir(parents=True,exist_ok=True)
            conn=gl.connect(self.public);gl.init_schema(conn)
            gl.ensure_node(conn,page,"Conditional result","page")
            gl.ensure_node(conn,"method-x","Method X","entity")
            conn.execute("INSERT INTO edges(subject,predicate,object) VALUES(?,?,?)",(page,"核心方法","method-x"))
            conn.commit();conn.close()
        return page

    def recall(self,query="conditional method X",domain="academic",topk=8):
        result=qa.execute("hybrid_recall",{"query":query,"domain":domain,"topk":str(topk)},subproject=domain or "public")
        self.assertTrue(result["ok"],result)
        return json.loads(result["text"])

    def test_shared_discovery_exposes_ku_and_question_raw_entries_as_navigation(self):
        statement=self.statement();question=self.question()
        result=self.recall()
        found={c['id']:c for c in result['candidates'] if c['kind'] in {'statement','question'}}
        self.assertEqual(set(found),{statement,question})
        session=qo.QuerySession(query='conditional method X',mode='api',stage='evidence')
        for candidate in found.values():
            self.assertFalse(candidate['evidence_read'])
            self.assertEqual(candidate['read_action'],'knowledge_read')
            self.assertEqual(candidate['evidence_preview'][0]['locator'],self.ref['locator'])
        # Choosing a disclosed Raw address uses the same read/validation path.
        out=qo.execute_plan(session,[{'action':'read_raw','input':{'locator':found[statement]['evidence_preview'][0]['locator']}}])
        self.assertTrue(out['results'][0]['ok'])
        self.assertIn(self.ref['locator'],session.read_sources)
        self.assertEqual(session.plan_count,1)
        self.assertFalse(result['fact_claim'])

    def test_full_name_source_outranks_generic_knowledge_channel_hit(self):
        self.statement()
        path=self.root/"certificate.md"
        path.write_text("# Registration\n" + "Administrative details. "*50 + "\nAsterSignal platform registration code 2031AZ; holder North University.\n")
        source=si.ingest(path,repo=self.root,knowledge_required=False)
        result=self.recall(query="AsterSignal platform efficiency",topk=8)
        self.assertEqual(result["candidates"][0]["source_id"],source["source_id"])
        self.assertIn("AsterSignal",result["candidates"][0]["excerpt"])
        self.assertIn("astersignal",result["candidates"][0]["lexical_match"]["matched_phrases"])
        self.assertIn("statement",[c["kind"] for c in result["candidates"]])
        self.assertFalse(result["fact_claim"])

    def test_excerpt_is_a_bounded_literal_slice_with_late_conditions(self):
        for query, clause in [
            ("report deadline approval", "Only after director approval, report deadline is 30 June."),
            ("季报 审核 内容", "按现行规则，季报经任务负责人审核，内容含进展与下阶段计划。"),
        ]:
            body = "Unrelated background. " * 70 + "\n" + clause + "\n" + "Other material. " * 30
            view = lr.excerpt(query, body)
            self.assertIn(clause, view["excerpt"])
            span = view["excerpt_span"]
            self.assertEqual(view["excerpt"], body[span["start"]:span["end"]])
            self.assertLessEqual(len(view["excerpt"]), 700)
            self.assertTrue(view["excerpt_truncated"])

    def test_excerpt_uses_whole_words_and_distinct_terms_not_repetition(self):
        body = "AsterFlower background. " * 60 + "\nAsterFlow requires finite input; no unconditional guarantee.\n"
        self.assertIn("AsterFlow requires finite input", lr.excerpt("AsterFlow finite input", body)["excerpt"])
        repeated = "deadline " * 150 + "\nOnly after approval, the report deadline applies.\n"
        self.assertIn("Only after approval", lr.excerpt("report deadline approval", repeated)["excerpt"])

    def test_excerpt_short_or_unmatched_body_preserves_prefix(self):
        self.assertEqual(lr.excerpt("absent", "Short text."), {
            "excerpt": "Short text.", "excerpt_truncated": False, "excerpt_span": {"start": 0, "end": 11}})
        body = "AsterFlower background. " * 60
        view = lr.excerpt("AsterFlow", body)
        self.assertEqual(view["excerpt"], body[:700])
        self.assertEqual(view["excerpt_span"]["start"], 0)

    def test_excerpt_ties_leave_context_after_the_last_matched_word(self):
        body = "Background. " * 100 + "review " + "filler " * 31 + "deadline " + "filler " * 31
        body += "approval. Include the complete next work plan.\n" + "Other. " * 100
        self.assertIn("approval. Include the complete next work plan.",
                      lr.excerpt("review deadline approval", body)["excerpt"])

    def test_long_ku_excerpt_preserves_identity_and_full_read(self):
        statement = "Background context. " * 70 + "\nUnder finite memory, AsterQueue has bounded capacity."
        unit = ks.commit(repo=self.root, operation_id="late-condition", title="Queue method",
            statement=statement, evidence=[self.ref], reason="Test late match display")["id"]
        result = self.recall(query="AsterQueue bounded capacity", topk=8)
        hit = next(c for c in result["candidates"] if c.get("id") == unit)
        self.assertIn("Under finite memory", hit["excerpt"])
        self.assertEqual(hit["read_input"], {"unit_id": unit})
        self.assertEqual(ks.read(unit, repo=self.root)["statement"], statement)
        span = hit["excerpt_span"]
        self.assertEqual(hit["excerpt"], statement[span["start"]:span["end"]])

    def test_method_name_beats_scattered_generic_words_inside_ku_search(self):
        relevant=ks.commit(repo=self.root,operation_id="named-method",title="AsterFlow algorithm",
            statement="The method uses a bounded queue.",evidence=[self.ref],reason="Test navigation")["id"]
        ks.commit(repo=self.root,operation_id="generic-method",title="优化方法局限与适用条件",
            statement="优化方法有局限，需要说明适用条件。"*20,evidence=[self.ref],reason="Test navigation")
        hits=ks.search("AsterFlow 一对多 局限",repo=self.root,topk=8)["candidates"]
        self.assertEqual(hits[0]["id"],relevant)
        self.assertEqual(lr.match("AsterFlow", "AsterFlow", "text"), lr.match("AsterFlow", "AsterFlow", "text "*100))
        self.assertEqual(lr.match("AsterFlow", "AsterFlower", "")['term_coverage'],0)

    def test_grouped_source_opens_body_and_keeps_bibliography_alternative(self):
        path=self.root/'paper.md'
        path.write_text('\n'.join(['# Study','Trainability requires nondegenerate input.']+['']*30+
                                  ['## References','Aster. Trainability and optimization. DOI: example.']))
        source=si.ingest(path,repo=self.root)
        result=self.recall(query='Aster trainability')
        item=next(c for c in result['candidates'] if c.get('source_id')==source['source_id'])
        self.assertTrue(item['locator'].endswith('#L1-L32'))
        self.assertEqual(item['section_role'],'unspecified')
        self.assertTrue(any(loc.endswith('#L33-L34') for loc in item['matched_locators']))
        read=qa.execute(item['read_action'],item['read_input'])
        self.assertTrue(read['ok'],read)
        self.assertIn('nondegenerate',read['text'])
        citation=self.recall(query='Aster trainability DOI')
        cited=next(c for c in citation['candidates'] if c.get('source_id')==source['source_id'])
        self.assertEqual(cited['section_role'],'references')

    def test_source_only_works_without_graph_or_ku_and_does_not_create_them(self):
        result=self.recall()
        self.assertEqual([r["kind"] for r in result["candidates"]],["source"])
        item=result["candidates"][0]
        self.assertTrue(qa.execute(item["read_action"],item["read_input"])["ok"])
        self.assertEqual(result["channels"]["navigation"]["status"],"partial")
        self.assertFalse(self.public.exists())
        self.assertFalse((self.root/"academic/knowledge/index.sqlite").exists())

    def test_four_types_have_executable_reads_and_keep_uncertainty(self):
        uid=self.statement();qid=self.question();self.wiki()
        result=self.recall()
        self.assertEqual({r["kind"] for r in result["candidates"]},{"source","statement","question","wiki"})
        for item in result["candidates"]:
            read=qa.execute(item["read_action"],item["read_input"])
            self.assertTrue(read["ok"],read)
            self.assertFalse(item["evidence_read"])
        by_id={r["id"]:r for r in result["candidates"]}
        self.assertEqual(by_id[uid]["claim_mode"],"conjecture")
        self.assertEqual(by_id[uid]["status"],"draft")
        self.assertEqual(by_id[qid]["scientific_state"],"unverified")
        self.assertEqual(by_id[uid]["content_role"],"scientific_finding")
        self.assertEqual(by_id[qid]["content_role"],"research_question")
        self.assertEqual(by_id[qid]["source_question_status"],"addressed")
        self.assertEqual(by_id[qid]["kb_state"],"unassessed")
        self.assertTrue(by_id[uid]["unresolved"])

    def test_wiki_files_remain_discoverable_without_graph(self):
        self.wiki(graph=False)
        self.assertEqual({r["kind"] for r in self.recall()["candidates"]},{"wiki","source"})
        self.assertFalse(self.public.exists())

    def test_legacy_wiki_without_section_citations_keeps_document_source_navigation(self):
        page=self.wiki(graph=False)
        path=self.root/(page+".md")
        path.write_text(path.read_text().replace(".[^r]",".").split("\n[^r]:")[0])
        wiki=next(r for r in self.recall()["candidates"] if r["kind"]=="wiki")
        self.assertEqual(wiki["source_links"],[self.ref["locator"]])
        self.assertFalse(wiki.get("raw_citations"))
        read=qa.execute(wiki["read_action"],wiki["read_input"])
        self.assertFalse(json.loads(read["text"])["raw_citations"])
        self.assertTrue(qa.execute("read_raw",{"locator":wiki["source_links"][0]})["ok"])

    def test_corrupt_graph_and_missing_source_index_preserve_ku(self):
        uid=self.statement()
        self.public.parent.mkdir(parents=True);self.public.write_bytes(b"not a database")
        (self.root/"academic/sources/index.sqlite").unlink()
        result=self.recall()
        self.assertIn(uid,[r["id"] for r in result["candidates"]])
        self.assertEqual(result["channels"]["navigation"]["status"],"partial")
        self.assertEqual(result["channels"]["source"]["status"],"unavailable")
        self.assertFalse((self.root/"academic/sources/index.sqlite").exists())

    def test_broken_ku_index_preserves_raw_source_discovery(self):
        self.statement()
        (self.root/"academic/knowledge/index.sqlite").write_bytes(b"not a database")
        result=self.recall()
        self.assertEqual(result["channels"]["knowledge"]["status"],"unavailable")
        self.assertIn("source",[r["kind"] for r in result["candidates"]])

    def test_unsearchable_query_is_not_reported_as_a_missing_source_index(self):
        result=self.recall(query="X")
        self.assertEqual(result["channels"]["source"]["status"],"ok")
        self.assertEqual(result["channels"]["source"]["count"],0)

    def test_one_source_does_not_fill_final_results_with_repeated_passages(self):
        path=self.root/"long.md"
        path.write_text("\n".join(f"Conditional result number {i}: method X may work under condition A." for i in range(150)))
        source=si.ingest(path,repo=self.root)
        candidates=self.recall(topk=20)["candidates"]
        hits=[r for r in candidates if r.get("source_id")==source["source_id"]]
        self.assertEqual(len(hits),1)
        self.assertGreater(len(hits[0]["matched_locators"]),1)

    def test_updated_statement_is_visible_with_same_identity_and_old_revision_retained(self):
        uid=self.statement();before=self.recall()
        old=ks.read(uid,repo=self.root)
        ks.commit(repo=self.root,operation_id="condition-refinement",unit_id=uid,expected_revision=old["revision"],
            expected_sha256=old["snapshot_sha256"],title=old["title"],
            statement="Under condition A, method X may improve efficiency; generality remains unknown.",
            claim_mode="conjecture",evidence=old["evidence"],reason="Clarify unresolved boundary")
        current=next(r for r in self.recall()["candidates"] if r["id"]==uid)
        self.assertEqual(current["revision"],2)
        read=qa.execute(current["read_action"],current["read_input"])
        self.assertEqual(json.loads(read["text"])["revision"],2)
        self.assertEqual(ks.read(uid,repo=self.root,revision=1)["revision"],1)
        self.assertEqual(next(r for r in before["candidates"] if r["id"]==uid)["revision"],1)

    def test_other_domains_never_call_academic_source_or_ku_search(self):
        for domain in ("private","admin","teaching","business"):
            with patch.object(ks,"search",side_effect=AssertionError("public KU accessed")),patch.object(si,"search",side_effect=AssertionError("public source accessed")):
                result=self.recall(domain=domain)
            self.assertFalse(result["candidates"])
            self.assertEqual(result["channels"]["knowledge"]["status"],"not_applicable")

    def test_repeated_query_is_read_only_and_returns_identical_results(self):
        self.statement();self.question();self.wiki()
        def snapshot():
            return {str(p.relative_to(self.root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        before=snapshot();first=self.recall();second=self.recall()
        self.assertEqual(first,second)
        self.assertEqual(snapshot(),before)

    def test_cli_and_api_use_typed_reads_without_promoting_ku_to_raw_evidence(self):
        uid=self.statement()
        args=wg.build_parser().parse_args(["hybrid-recall","conditional","--domain","academic"])
        with redirect_stdout(io.StringIO()) as output:
            args.func(args)
        self.assertEqual(json.loads(output.getvalue())["result"]["schema"],"unified-recall-v1")
        session=qo.QuerySession(query="conditional",mode="api",stage="evidence")
        qo.execute_plan(session,[{"action":"hybrid_recall","input":{"query":"conditional","domain":"academic"}}])
        qo.execute_plan(session,[{"action":"knowledge_read","input":{"unit_id":uid}}])
        self.assertEqual(session.read_sources,[])
        qo.execute_plan(session,[{"action":"read_raw","input":{"locator":self.ref["locator"]}}])
        self.assertTrue(session.read_sources)

    def test_dsh_has_the_same_unified_discovery_entry(self):
        from dsh.tools import build_tools
        tool=next(t for t in build_tools() if t.name=="hybrid_recall")
        text=tool.execute_fn({"query":"conditional","domain":"academic"})
        self.assertIn("unified-recall-v1",text)


if __name__=="__main__":
    unittest.main()

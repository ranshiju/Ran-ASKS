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

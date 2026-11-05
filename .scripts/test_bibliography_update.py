#!/usr/bin/env python3
"""Managed bibliography update in isolated repositories, including recovery failures."""
import contextlib
import errno
import sqlite3
import copy
import io
import sys
import ingest_inbox
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bibliography_update as bu
import bibliography_sidecar as bs
import external_bibliography as eb
import graph_lib as gl
import graph_ingest as gi
import ingest_check as check
import inbox_finalize as finalizer
from test_bibliography_sidecar import BIB, RIS, MD, TITLE

WIKI = f'''---
title: "{TITLE}"
type: paper-summary
sources:
  - academic/raw/references/example/paper.md
source_type: official-doc
date: 2021
venue: arXiv
authors:
  - Alice Smith
  - Bob Jones
confidence: high
created: 2026-01-01
updated: 2026-01-01
status: current
related: []
---
# {TITLE}
> **作者**：Alice Smith、Bob Jones | **发表**：arXiv 2021

## Navigation
Original scientific content.[^r1]

## Content
Human-authored conclusion: keep exactly.[^r1]

## Sources
[^r1]: academic/raw/references/example/paper.md#L1-L3
'''


class UpdateTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        for module in (gl,check,gi,check.wl,check.wl.raw_locator):
            if hasattr(module,'REPO'):
                self.stack.enter_context(patch.object(module,'REPO',self.root))
        self.graph=self.root/'cross-domain/graph.db';self.graph.parent.mkdir()
        conn=gl.connect(self.graph);gl.init_schema(conn);conn.commit();conn.close()
        extract=self.root/'temp/inbox-extract/initial';extract.mkdir(parents=True)
        (extract/'paper.pdf').write_bytes(b'fixture PDF')
        (extract/'paper.md').write_text(MD)
        (extract/'source.yaml').write_text('origin: preserved-original\nbibliographic:\n  year: "2021"\n  venue: arXiv\n')
        (extract/'wiki.md').write_text(WIKI)
        entries=[{'path':name,'role':'source','sha256':bu.digest(extract/name),'bytes':(extract/name).stat().st_size}
                 for name in ('paper.pdf','paper.md','source.yaml')]
        bu.write_json(extract/'manifest.json',{'schema':finalizer.MANIFEST_V2,'raw_artifacts':entries,'wiki_file':'wiki.md'})
        self.raw=self.root/'academic/raw/references/example'
        self.wiki=self.root/'academic/wiki/papers/example.md'
        finalizer.finalize(self.root,'initial',self.raw,self.wiki,extract,extract/'manifest.json',False)
        self.page=self.wiki.relative_to(self.root).as_posix().removesuffix('.md')
        conn=gl.connect(self.graph)
        gl.ensure_node(conn,self.page,TITLE,'page',date='2021')
        gi.ensure_raw_support_edge(conn,self.page,bu.frontmatter(WIKI)[0],repo=self.root)
        gl.ensure_node(conn,'preserved-concept','Preserved concept','entity',entity_subtype='concept')
        conn.execute("INSERT INTO edges(subject,predicate,object,source) VALUES (?,?,?,?)",
                     (self.page,'核心方法','preserved-concept','original-evidence'))
        gi.refresh_paper_bibliography(conn,self.page,bu.frontmatter(WIKI)[0],
                                      'academic/raw/references/example/source.yaml')
        conn.commit();conn.close()
        self.inbox=self.root/'inbox';self.inbox.mkdir()
        self.pdf=self.inbox/'example.pdf';self.pdf.write_bytes(b'fixture PDF')
        self.pdf.with_suffix('.bib').write_text(BIB)
        self.before={name:(self.raw/name).read_bytes() for name in ('paper.pdf','paper.md','source.yaml')}
        self.stack.enter_context(patch.object(finalizer,'run_ingest_check',side_effect=self.validate))
        self.stack.enter_context(patch.object(eb,'fetch',side_effect=AssertionError('no network')))

    def validate(self, repo, wiki):
        errors,_=check.check_file(wiki,*check.build_wiki_index())
        if errors:
            raise ValueError('; '.join(errors))

    def prepare(self):
        return bu.prepare(self.root,self.pdf,self.raw/'paper.pdf')

    def graph_state(self):
        conn=gl.connect(self.graph,read_only=True)
        try:return gi.bibliography_graph_state(conn,self.page)
        finally:conn.close()

    def test_updates_provenance_wiki_graph_preserves_originals_and_content(self):
        prepared=self.prepare()
        self.assertEqual(self.wiki.read_text(),WIKI)
        result=bu.apply(self.root,prepared['plan'])
        self.assertEqual(result['status'],'completed')
        for name in ('paper.pdf','paper.md'):
            self.assertEqual((self.raw/name).read_bytes(),self.before[name])
        history=self.raw/'bibliography-history'/result['transaction_id']
        self.assertEqual((history/'source.yaml').read_bytes(),self.before['source.yaml'])
        source=bu.yaml.safe_load((self.raw/'source.yaml').read_text())
        self.assertEqual(source['origin'],'preserved-original')
        self.assertEqual(source['bibliographic']['year'],'2022')
        self.assertIsNotNone(eb.replay(source['bibliographic']['external_receipt'],MD,self.raw))
        self.assertEqual(self.wiki.read_text().split('## Navigation',1)[1],WIKI.split('## Navigation',1)[1])
        self.assertEqual(bu.frontmatter(self.wiki.read_text())[0]['authors'],['Alice Smith','Bob Jones'])
        graph=self.graph_state();self.assertEqual(graph['node'],[TITLE,'2022'])
        self.assertEqual(len(graph['edges']),3)
        conn=gl.connect(self.graph,read_only=True)
        self.assertEqual(conn.execute("SELECT source FROM edges WHERE predicate='核心方法'").fetchone()[0],'original-evidence')
        conn.close()
        self.assertIn(result['transaction_id'],(self.root/'academic/wiki/log.md').read_text())

    def test_apply_is_idempotent_after_completed_input_cleanup(self):
        prepared=self.prepare();one=bu.apply(self.root,prepared['plan'])
        before_graph=self.graph_state();log=(self.root/'academic/wiki/log.md').read_bytes()
        self.pdf.unlink();self.pdf.with_suffix('.bib').unlink()
        two=bu.apply(self.root,prepared['plan'])
        self.assertEqual(one['receipt'],two['receipt'])
        self.assertEqual(self.graph_state(),before_graph)
        self.assertEqual((self.root/'academic/wiki/log.md').read_bytes(),log)

    def test_validation_failure_rolls_back_files_graph_and_log(self):
        prepared=self.prepare();before_graph=self.graph_state()
        with patch.object(finalizer,'run_ingest_check',side_effect=ValueError('injected validation failure')):
            with self.assertRaisesRegex(ValueError,'injected'):
                bu.apply(self.root,prepared['plan'])
        self.assertEqual(self.wiki.read_text(),WIKI)
        self.assertEqual((self.raw/'source.yaml').read_bytes(),self.before['source.yaml'])
        self.assertEqual(self.graph_state(),before_graph)
        self.assertFalse((self.raw/'bibliography-history').exists())
        self.assertFalse((self.raw/'bibliography').exists())
        self.assertFalse((self.root/'academic/wiki/log.md').exists())
        self.assertEqual(bu.apply(self.root,prepared['plan'])['status'],'completed')

    def test_recovers_file_commit_when_graph_commit_was_interrupted(self):
        prepared=self.prepare();real=finalizer.finalize
        def file_only(*args,**kwargs):
            kwargs.pop('commit_hook')
            receipt=real(*args,**kwargs)
            raise SystemExit('simulated process stop after durable files')
        with patch.object(finalizer,'finalize',side_effect=file_only):
            with self.assertRaises(SystemExit):bu.apply(self.root,prepared['plan'])
        self.assertEqual(self.graph_state()['node'][1],'2021')
        self.assertEqual(bu.frontmatter(self.wiki.read_text())[0]['date'],'2022')
        result=bu.recover_pending(self.root,self.raw/'paper.pdf')
        self.assertTrue(result['resumed_file_commit'])
        self.assertEqual(self.graph_state()['node'][1],'2022')

    def test_rejects_input_wiki_and_graph_drift(self):
        for kind in ('input','wiki','graph'):
            with self.subTest(kind=kind):
                prepared=self.prepare()
                if kind=='input': self.pdf.with_suffix('.bib').write_text(BIB+' ')
                elif kind=='wiki': self.wiki.write_text(WIKI+'changed')
                else:
                    conn=gl.connect(self.graph);conn.execute('UPDATE nodes SET date=? WHERE path=?',('2000',self.page));conn.commit();conn.close()
                with self.assertRaises(ValueError):bu.apply(self.root,prepared['plan'])
                self.assertEqual((self.raw/'source.yaml').read_bytes(),self.before['source.yaml'])
                self.pdf.with_suffix('.bib').write_text(BIB);self.wiki.write_text(WIKI)
                conn=gl.connect(self.graph);conn.execute('UPDATE nodes SET date=? WHERE path=?',('2021',self.page));conn.commit();conn.close()

    def check_single_record_correction(self, suffix, body):
        first=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(first['status'],'completed')
        previous=(self.raw/'source.yaml').read_bytes()
        previous_receipt=bu.yaml.safe_load(previous)['bibliographic']['external_receipt']
        original_body=self.wiki.read_text().split('## Navigation',1)[1]
        self.pdf.with_suffix('.bib').unlink()
        self.pdf.with_suffix(suffix).write_text(body)
        real_replay=eb.replay
        def new_only(receipt,*args,**kwargs):
            if receipt==previous_receipt:
                raise AssertionError('old bibliography must not authorize the new record')
            return real_replay(receipt,*args,**kwargs)
        with patch.object(eb,'replay',side_effect=new_only):
            result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        accepted=bu.yaml.safe_load((self.raw/'source.yaml').read_text())['bibliographic']
        self.assertEqual(accepted['year'],'2023')
        self.assertEqual(accepted['venue'],'Corrected Journal')
        self.assertEqual((self.raw/'bibliography-history'/result['transaction_id']/'source.yaml').read_bytes(),previous)
        self.assertEqual(self.wiki.read_text().split('## Navigation',1)[1],original_body)
        self.assertEqual(len(bs.discover(self.pdf)),1)
        self.assertEqual(self.graph_state()['node'][1],'2023')
        self.assertEqual([result[key] for key in ('http_requests','llm_calls','ocr_calls')],[0,0,0])
        return accepted

    def test_single_bib_supersedes_locked_descriptive_metadata(self):
        body=BIB.replace('2022','2023').replace('Journal of Quantum Research','Corrected Journal').replace('Smith, Alice','Smith, Alice A.')
        accepted=self.check_single_record_correction('.bib',body)
        self.assertEqual(accepted['authors'][0],'Alice A. Smith')

    def test_single_ris_supersedes_locked_descriptive_metadata(self):
        body=RIS.replace('2022','2023').replace('Journal of Quantum Research','Corrected Journal')
        self.check_single_record_correction('.ris',body)

    def test_missing_doi_is_not_a_conflicting_identity(self):
        body=BIB.replace('2022','2023').replace('Journal of Quantum Research','Corrected Journal').replace(', doi={10.1000/example}','')
        accepted=self.check_single_record_correction('.bib',body)
        self.assertEqual(accepted['doi'],'')

    def test_resubmitting_an_older_export_creates_a_new_bound_update(self):
        original=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.pdf.with_suffix('.bib').write_text(BIB.replace('2022','2023'))
        corrected=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(corrected['status'],'completed',corrected)
        previous=(self.raw/'source.yaml').read_bytes()
        self.pdf.with_suffix('.bib').write_text(BIB)
        again=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(again['status'],'completed',again)
        self.assertNotEqual(again['transaction_id'],original['transaction_id'])
        self.assertEqual((self.raw/'bibliography-history'/again['transaction_id']/'source.yaml').read_bytes(),previous)
        self.assertEqual(self.graph_state()['node'][1],'2022')

    def test_inbox_accepts_single_correction_without_replaying_old_metadata(self):
        first=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(first['status'],'completed')
        old_receipt=bu.yaml.safe_load((self.raw/'source.yaml').read_text())['bibliographic']['external_receipt']
        self.pdf.with_suffix('.bib').write_text(BIB.replace('2022','2023'))
        real=bs.replay
        def new_only(receipt,*args,**kwargs):
            if receipt==old_receipt: raise AssertionError('unnecessary old record replay')
            return real(receipt,*args,**kwargs)
        with patch.object(bs,'replay',side_effect=new_only):
            self.run_inbox_supplement()
        self.assertEqual(self.graph_state()['node'][1],'2023')

    def test_distinct_formal_doi_is_not_overwritten(self):
        prepared=self.prepare();bu.apply(self.root,prepared['plan'])
        previous=(self.raw/'source.yaml').read_bytes();wiki=self.wiki.read_bytes()
        self.pdf.with_suffix('.bib').write_text(BIB.replace('10.1000/example','10.1000/other'))
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'bibliographic_review_required')
        self.assertIn('DOI conflicts',result['reason'])
        self.assertEqual((self.raw/'source.yaml').read_bytes(),previous)
        self.assertEqual(self.wiki.read_bytes(),wiki)

    def test_no_match_and_different_pdf_remain_uncommitted(self):
        self.pdf.with_suffix('.bib').write_text(BIB.replace('Reliable learning with {quantum} circuits','Completely unrelated title'))
        with self.assertRaises(ValueError):self.prepare()
        self.pdf.write_bytes(b'other PDF')
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_commit_hook_failure_rolls_back_files_and_graph(self):
        prepared=self.prepare();before_graph=self.graph_state();real=finalizer.finalize
        def fail(*args,**kwargs):
            def broken_commit(): raise OSError('graph commit unavailable')
            kwargs['commit_hook']=broken_commit
            return real(*args,**kwargs)
        with patch.object(finalizer,'finalize',side_effect=fail):
            with self.assertRaisesRegex(OSError,'graph commit'):
                bu.apply(self.root,prepared['plan'])
        self.assertEqual(self.wiki.read_text(),WIKI)
        self.assertEqual((self.raw/'source.yaml').read_bytes(),self.before['source.yaml'])
        self.assertEqual(self.graph_state(),before_graph)
        self.assertEqual(bu.apply(self.root,prepared['plan'])['status'],'completed')

    def test_refuses_bibliography_edges_owned_by_another_page(self):
        conn=gl.connect(self.graph)
        edge=conn.execute("SELECT id FROM edges WHERE predicate='发表于'").fetchone()[0]
        conn.execute('INSERT INTO edge_origins(edge_id,origin_page,source) VALUES (?,?,?)',
                     (edge,'academic/wiki/papers/other','other-source'))
        conn.commit();conn.close()
        with self.assertRaisesRegex(ValueError,'another page owner'): self.prepare()
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_plan_and_hash_edits_cannot_smuggle_body_metadata_or_extra_raw(self):
        prepared=self.prepare();extract=self.root/Path(prepared['plan']).parent
        originals={p:p.read_bytes() for p in extract.rglob('*') if p.is_file()}
        for kind in ('body','metadata','extra','evidence','graph'):
            with self.subTest(kind=kind):
                plan=json.loads((extract/'update-plan.json').read_text())
                manifest=json.loads((extract/'manifest.json').read_text())
                if kind=='body':
                    (extract/'wiki.md').write_text((extract/'wiki.md').read_text()+'injected conclusion')
                    plan['draft_sha256']=bu.digest(extract/'wiki.md')
                elif kind=='metadata':
                    data=bu.yaml.safe_load((extract/'source.yaml').read_text());data['origin']='changed'
                    for name in ('source.yaml','bibliography-history/'+plan['transaction_id']+'/accepted.yaml'):
                        (extract/name).write_text(bu.yaml.safe_dump(data))
                        for item in manifest['raw_artifacts']:
                            if item['path']==name:
                                item.update(sha256=bu.digest(extract/name),bytes=(extract/name).stat().st_size)
                    plan['source_yaml_sha256']=bu.digest(extract/'source.yaml')
                elif kind=='extra':
                    (extract/'unrelated.md').write_text('injected')
                    manifest['raw_artifacts'].append({'path':'unrelated.md','sha256':bu.digest(extract/'unrelated.md'),'bytes':8,'role':'source'})
                elif kind=='evidence': plan['graph_evidence']='academic/raw/references/example/paper.md'
                else: plan['graph_after']=copy.deepcopy(plan['graph_before'])
                bu.write_json(extract/'manifest.json',manifest)
                plan['manifest_sha256']=bu.digest(extract/'manifest.json')
                bu.write_json(extract/'update-plan.json',plan)
                with self.assertRaises(ValueError):bu.apply(self.root,prepared['plan'])
                self.assertEqual(self.wiki.read_text(),WIKI)
                for path,data in originals.items():path.write_bytes(data)

    def test_missing_previous_provenance_can_be_added(self):
        (self.raw/'source.yaml').unlink()
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        self.assertFalse((self.raw/'bibliography-history'/result['transaction_id']/'source.yaml').exists())
        self.assertEqual(bu.yaml.safe_load((self.raw/'source.yaml').read_text())['bibliographic']['year'],'2022')

    def test_staged_symlink_and_raw_drift_rejected(self):
        prepared=self.prepare();extract=self.root/Path(prepared['plan']).parent
        original=(extract/'wiki.md').read_bytes()
        (extract/'wiki.md').unlink();(extract/'wiki.md').symlink_to(self.wiki)
        with self.assertRaises(ValueError):bu.apply(self.root,prepared['plan'])
        (extract/'wiki.md').unlink();(extract/'wiki.md').write_bytes(original)
        (self.raw/'paper.md').write_text(MD+'changed')
        with self.assertRaisesRegex(ValueError,'Raw changed'):bu.apply(self.root,prepared['plan'])
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_other_document_duplicate_is_outside_bibliography_recovery(self):
        self.assertIsNone(bu.recover_pending(self.root,'admin/raw/documents/example/document.pdf'))

    def test_log_drift_blocks_recovery(self):
        prepared=self.prepare();bu.apply(self.root,prepared['plan'])
        (self.root/'academic/wiki/log.md').write_text('lost log')
        with self.assertRaisesRegex(ValueError,'log entry'):bu.apply(self.root,prepared['plan'])

    def test_run_recovers_after_graph_commit_before_result_checkpoint(self):
        real=bu.write_json
        def fail(path,value):
            if path.name=='update-result.json': raise OSError('checkpoint unavailable')
            return real(path,value)
        with patch.object(bu,'write_json',side_effect=fail):
            pending=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(pending['status'],'failed')
        self.assertIn('--apply',pending['apply_command'])
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['transaction_id'],pending['transaction_id'])

    def test_unrelated_corrupt_plan_is_reported_without_blocking(self):
        bad=self.root/'temp/inbox-extract/bib-update-unrelated/update-plan.json'
        bad.parent.mkdir();bad.write_text('{broken')
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['plan_scan_warning_count'],1)
        self.assertEqual(bad.read_text(),'{broken')

    def test_current_corrupt_plan_is_not_overwritten_or_ignored(self):
        prepared=self.prepare();path=self.root/prepared['plan'];path.write_text('{broken')
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'bibliographic_review_required')
        self.assertEqual(path.read_text(),'{broken')
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_transient_graph_commit_retries_same_plan_after_rollback(self):
        real=finalizer.finalize;attempts=[]
        def flaky(*args,**kwargs):
            attempts.append(args[1])
            if len(attempts)==1:
                def fail():raise OSError(errno.EAGAIN,'temporary write contention')
                kwargs['commit_hook']=fail
            return real(*args,**kwargs)
        with patch.object(finalizer,'finalize',side_effect=flaky), \
             patch.object(bs,'resolve_local',wraps=bs.resolve_local) as resolve:
            result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['apply_attempts'],2)
        self.assertEqual(len(set(attempts)),1)
        self.assertEqual(resolve.call_count,1)
        self.assertEqual((self.root/'academic/wiki/log.md').read_text().count('书目增补'),1)
        self.assertEqual(self.graph_state()['node'][1],'2022')

    def test_persistent_sqlite_contention_is_bounded_and_retryable(self):
        error=sqlite3.OperationalError('fixture busy');error.sqlite_errorcode=sqlite3.SQLITE_BUSY
        with patch.object(bu,'apply',side_effect=error) as apply:
            result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'failed')
        self.assertTrue(result['retryable'])
        self.assertEqual(apply.call_count,2)
        self.assertEqual(result['apply_attempts'],2)
        self.assertIn('--apply',result['apply_command'])
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_permanent_storage_failure_does_not_retry_or_request_bibliography_review(self):
        for code in (errno.ENOSPC,errno.EACCES):
            with self.subTest(code=code),patch.object(bu,'apply',side_effect=OSError(code,'fixture storage failure')) as apply:
                result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
            self.assertEqual(result['status'],'failed')
            self.assertFalse(result['retryable'])
            self.assertEqual(result['failure_domain'],'storage')
            self.assertEqual(apply.call_count,1)
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_pending_recovery_failure_retains_apply_context(self):
        prepared=self.prepare();bu.apply(self.root,prepared['plan'])
        (self.root/prepared['plan']).with_name('update-result.json').unlink()
        with patch.object(bu,'apply',side_effect=OSError(errno.ENOSPC,'fixture disk full')):
            result=bu.recover_pending(self.root,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['transaction_id'],prepared['transaction_id'])
        self.assertIn(prepared['plan'],result['apply_command'])

    def test_inbox_named_bibliography_ignores_and_preserves_shared_library(self):
        library=self.inbox/'library.bib';library.write_text('@article{broken,')
        self.run_inbox_supplement()
        self.assertEqual(library.read_text(),'@article{broken,')
        source=bu.yaml.safe_load((self.raw/'source.yaml').read_text())
        self.assertEqual([f['original_name'] for f in source['bibliographic']['external_receipt']['files']],['example.bib'])

    def test_update_named_no_match_uses_shared_library(self):
        self.pdf.with_suffix('.bib').write_text(BIB.replace('Reliable learning with {quantum} circuits','Other paper'))
        (self.inbox/'library.ris').write_text(RIS)
        result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'completed',result)
        source=bu.yaml.safe_load((self.raw/'source.yaml').read_text())
        self.assertEqual(source['bibliographic']['external_receipt']['discovery_scope'],'directory')
        self.assertEqual(len(source['bibliographic']['external_receipt']['files']),2)

    def test_new_named_file_after_preparation_blocks_apply(self):
        prepared=self.prepare()
        self.pdf.with_suffix('.ris').write_text(RIS.replace('2022','2023'))
        with self.assertRaisesRegex(ValueError,'file set changed'):
            bu.apply(self.root,prepared['plan'])
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_unselected_library_change_does_not_invalidate_named_plan(self):
        shared=self.inbox/'library.bib';shared.write_text('@article{broken,')
        prepared=self.prepare()
        shared.write_text('unrelated change')
        result=bu.apply(self.root,prepared['plan'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(shared.read_text(),'unrelated change')

    def test_selected_input_changes_after_resolution_are_rejected(self):
        real=bs.resolve_local
        def mutate(*args,**kwargs):
            local=real(*args,**kwargs)
            self.pdf.with_suffix('.bib').write_text(BIB+'changed')
            return local
        with patch.object(bs,'resolve_local',side_effect=mutate):
            result=bu.run(self.root,self.pdf,self.raw/'paper.pdf')
        self.assertEqual(result['status'],'bibliographic_review_required')
        self.assertEqual(self.wiki.read_text(),WIKI)

    def test_inbox_auto_supplement_without_semantic_dispatch(self):
        self.run_inbox_supplement()

    def test_inbox_resumes_pending_graph_before_duplicate_cleanup(self):
        prepared=self.prepare();real=finalizer.finalize
        def file_only(*args,**kwargs):
            kwargs.pop('commit_hook');real(*args,**kwargs)
            raise SystemExit('file phase complete')
        with patch.object(finalizer,'finalize',side_effect=file_only):
            with self.assertRaises(SystemExit):bu.apply(self.root,prepared['plan'])
        self.run_inbox_supplement()
        self.assertEqual(self.graph_state()['node'][1],'2022')

    def invoke_inbox(self):
        output=io.StringIO()
        match={'raw_path':str((self.raw/'paper.pdf').relative_to(self.root)), 'binary_sha256':bu.digest(self.pdf)}
        with patch.object(ingest_inbox,'REPO',self.root), patch.object(ingest_inbox,'INBOX',self.inbox), \
             patch.object(ingest_inbox.agent_task,'ingest_backend',return_value='agent'), \
             patch.object(ingest_inbox.sf,'ensure_index'), \
             patch.object(ingest_inbox.sf,'lookup_exact',return_value=match), \
             patch.object(ingest_inbox,'classify_file_details',return_value={'file_type':'paper','needs_api_review':False}), \
             patch.object(ingest_inbox.subprocess,'run',side_effect=AssertionError('no semantic dispatch')), \
             patch.object(ingest_inbox.trash_util,'trash_path',side_effect=lambda p:p.unlink()), \
             patch.object(sys,'argv',['ingest_inbox.py','--run','--file','inbox/example.pdf']), \
             contextlib.redirect_stdout(output):
            try:
                ingest_inbox.main()
            except SystemExit as exc:
                self.assertEqual(exc.code,1)
        result=ingest_inbox._extract_last_json(output.getvalue())
        return result

    def run_inbox_supplement(self):
        result=self.invoke_inbox()
        self.assertEqual(result['status'],'duplicate_found',result)
        self.assertEqual(result['files'][0]['bibliography_update']['status'],'completed')
        self.assertFalse(self.pdf.exists());self.assertTrue(self.pdf.with_suffix('.bib').exists())
        self.assertEqual(self.wiki.read_text().split('## Navigation',1)[1],WIKI.split('## Navigation',1)[1])

    def test_inbox_preserves_storage_failure_status_and_input(self):
        with patch.object(bu,'apply',side_effect=OSError(errno.EACCES,'fixture access denied')):
            result=self.invoke_inbox()
        self.assertEqual(result['status'],'failed',result)
        item=result['files'][0]
        self.assertFalse(item['retryable'])
        self.assertEqual(item['failure_domain'],'storage')
        self.assertEqual(item['failure_disposition']['owner'],'configuration')
        self.assertFalse(item['failure_disposition']['retryable'])
        self.assertIn('--apply',item['apply_command'])
        self.assertNotIn('state_ref',item)
        self.assertNotIn('runtime_status_ref',item)
        self.assertTrue((self.root/item['artifact_refs']['bibliography_plan']).is_file())
        self.assertEqual(item['next_actions']['resume'],item['apply_command'])
        self.assertTrue(self.pdf.exists())

    def test_inbox_retains_program_retry_routing_after_bounded_contention(self):
        with patch.object(bu,'apply',side_effect=OSError(errno.EAGAIN,'fixture contention')) as apply:
            result=self.invoke_inbox()
        item=result['files'][0]
        self.assertEqual(result['status'],'failed')
        self.assertTrue(item['retryable'])
        self.assertTrue(item['failure_disposition']['retryable'])
        self.assertEqual(item['failure_disposition']['owner'],'program')
        self.assertEqual(apply.call_count,2)
        self.assertTrue(self.pdf.exists())

    def test_apply_cli_returns_failure_exit_code_and_recovery_context(self):
        prepared=self.prepare();output=io.StringIO()
        with patch.object(bu,'REPO',self.root), \
             patch.object(sys,'argv',['bibliography_update.py','--apply',prepared['plan']]), \
             patch.object(bu,'apply',side_effect=OSError(errno.EACCES,'fixture denied')) as apply, \
             contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:bu.main()
        self.assertEqual(stopped.exception.code,1)
        result=json.loads(output.getvalue())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['plan'],prepared['plan'])
        self.assertEqual(apply.call_count,1)

    def test_inbox_recovers_old_graph_then_processes_new_incoming_bibliography(self):
        prepared=self.prepare();real=finalizer.finalize
        def file_only(*args,**kwargs):
            kwargs.pop('commit_hook');real(*args,**kwargs)
            raise SystemExit('file phase complete')
        with patch.object(finalizer,'finalize',side_effect=file_only):
            with self.assertRaises(SystemExit):bu.apply(self.root,prepared['plan'])
        self.pdf.with_suffix('.bib').write_text(BIB.replace('2022','2023'))
        self.run_inbox_supplement()
        self.assertEqual(self.graph_state()['node'][1],'2023')

    def test_provenance_replacement_requires_exact_old_archive(self):
        prepared=self.prepare();extract=self.root/Path(prepared['plan']).parent
        plan=json.loads((extract/'update-plan.json').read_text())
        manifest=json.loads((extract/'manifest.json').read_text())
        manifest['raw_artifacts']=[e for e in manifest['raw_artifacts'] if e['path']!=plan['provenance_update']['archive_path']]
        bu.write_json(extract/'manifest.json',manifest)
        with self.assertRaisesRegex(ValueError,'old snapshot'):
            finalizer.finalize(self.root,'invalid',self.raw,self.wiki,extract,extract/'manifest.json',False,True,
                expected_wiki_sha256=bu.digest(self.wiki),provenance_update=plan['provenance_update'])
        self.assertEqual((self.raw/'source.yaml').read_bytes(),self.before['source.yaml'])


if __name__=='__main__':
    unittest.main()

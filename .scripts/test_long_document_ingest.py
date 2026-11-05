#!/usr/bin/env python3
"""Offline, isolated integration tests for long-document ingestion."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ingest_document as doc
import inbox_finalize as finalizer
import long_document_ingest as ld
import long_document_plan as lp


class LongDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.extract = self.repo / 'temp/inbox-extract/book'
        self.extract.mkdir(parents=True)
        (self.repo / 'inbox').mkdir()
        self.text = '# 测试教材\n\n' + '\n'.join(
            f'## 第{i}章 概念{i}\n' + ('定义及适用条件必须一并保留。\n' * 220) for i in range(1, 4))
        (self.repo / 'inbox/book.md').write_text(self.text)
        (self.extract / 'doc.md').write_text(self.text)
        self.state = {'transaction_id': 'long-test', 'pipeline_script': 'ingest_document.py',
                      'status': 'write_wiki', 'extract_dir': 'temp/inbox-extract/book',
                      'source': 'inbox/book.md', 'source_filename': 'book.md',
                      'locator_source_filename': 'book.md', 'raw_locator_kind': 'section-line',
                      'source_kind': 'ordinary', 'subproject': 'academic',
                      'document_type': 'academic-reference', 'date_str': ''}
        self.addCleanup(patch.stopall)
        patch.object(doc, 'REPO', self.repo).start()
        patch.object(doc.ic, 'load_raw_abbr_map', return_value={}).start()

    def result(self, packet, protocol, publish=True):
        if protocol['stage'] == 'evidence':
            if 'source' in packet:
                line, quote = next((int(n), q) for n, q in re.findall(r'<RAW#L(\d+)> ([^\n]*)', packet['source']) if q.strip())
            else:
                line, quote = packet['notes'][0]['evidence'][0].values()
            return {'summary': f'{quote} <RAW#L{line}>', 'evidence': [{'line': line, 'quote': quote}]}
        evidence = packet['notes'][0]['evidence'][0]
        return {'publish': True if protocol['stage'] == 'overview' else publish,
                'rationale': '具有独立问题和定位证据，便于按章查询。',
                'wiki': '---\ntitle: 测试教材\ntype: academic-reference\nstatus: current\n---\n'
                        '# 测试教材\n\n## Navigation\n\n说明。\n\n## Content\n\n'
                        + evidence['quote'] + f" <RAW#L{evidence['line']}>\n",
                'slots': '三元组:\n本文档 | 涉及 | 信息论\n'}

    def run_api(self, publish=True):
        calls = []
        def generate(packet, protocol, state):
            calls.append(protocol['stage'])
            return {'ok': True, 'parsed': self.result(packet, protocol, publish)}
        with patch.object(doc, 'ingest_mode', return_value='api'), patch.object(ld, 'api_generate', side_effect=generate):
            ok, message = doc.step_write_wiki(self.state)
        self.assertTrue(ok, message + str(self.state.get('agent_task', {}).get('issues')))
        return calls

    def test_short_chaptered_document_uses_full_context_but_pending_long_work_resumes(self):
        text = '\n'.join(f'## 第{i}章\n' + '完整事实与适用条件。' * 45 for i in range(4))
        (self.extract / 'doc.md').write_text(text)
        (self.repo / 'inbox/book.md').write_text(text)
        with patch.object(doc, 'ingest_mode', return_value='api'), \
             patch.object(ld, 'write_wiki', side_effect=AssertionError('short source split')), \
             patch.object(doc, 'call_text', return_value={'ok': False, 'error': 'stop before compilation'}) as api:
            ok, message = doc.step_write_wiki(self.state)
        self.assertFalse(ok)
        self.assertIn('stop before compilation', message)
        self.assertEqual(api.call_count, 1)
        prompt = api.call_args.args[0]
        self.assertIn('第0章', prompt)
        self.assertIn('第3章', prompt)
        self.assertNotIn('long_document', self.state)
        self.state['long_document'] = {'binding': {'protocol': ld.PROTOCOL}}
        with patch.object(ld, 'write_wiki', return_value=(False, 'original workflow')) as resume, \
             patch.object(doc, 'call_text', side_effect=AssertionError('pending work reset')):
            self.assertEqual(doc.step_write_wiki(self.state), (False, 'original workflow'))
        self.assertEqual(resume.call_count, 1)
        self.assertEqual(resume.call_args.args[-1], text)

    def test_complete_windows_preserve_long_lines(self):
        text = '# Title\n' + '甲' * 30000 + '\nend\n'
        windows = lp.reading_windows(text)
        pieces = [p for w in windows for p in w['pieces']]
        for number, original in enumerate(text.splitlines(), 1):
            self.assertEqual(''.join(p['text'] for p in pieces if p['line'] == number), original)
        self.assertTrue(all(len(lp.render_window(w)) <= lp.WINDOW_CHARS for w in windows))
        self.assertEqual({p['line'] for p in pieces}, {1, 2, 3})

    def test_formula_rich_native_pdf_prefers_layout_parser_and_reports_fallback(self):
        import subprocess
        source = self.repo / 'inbox/math.pdf'
        source.write_bytes(b'fixture')
        native_map = {'warnings': [{'page': 1, 'unmapped_controls': 1}]}
        with patch.object(lp, 'pdf_profile', return_value={'book_structure': True}), \
             patch.object(lp, 'extract_native_pdf', return_value=('native with [unmapped]', native_map)), \
             patch.object(doc, '_is_scanned_pdf', return_value=False), \
             patch.object(doc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'offline')):
            text = doc.extract_doc_text(source, self.extract)
            self.assertEqual(text, 'native with [unmapped]')
            mapping = json.loads((self.extract / 'pdf-map.json').read_text())
            self.assertEqual(mapping['selection'], 'native_fallback_after_parser_failure')
            parsed = self.extract / 'extern/paper.md'
            parsed.parent.mkdir()
            parsed.write_text('layout-aware extraction')
            self.assertEqual(doc.extract_doc_text(source, self.extract), 'layout-aware extraction')

    def test_large_unstructured_source_uses_bounded_reduction_without_chapter_pages(self):
        self.text = '# Long source\n' + 'Supported fact with conditions.\n' * 6000
        (self.repo / 'inbox/book.md').write_text(self.text)
        (self.extract / 'doc.md').write_text(self.text)
        self.run_api()
        record = self.state['long_document']
        self.assertGreater(record['windows_total'], ld.FAN_IN)
        self.assertEqual(record['windows_validated'], record['windows_total'])
        self.assertEqual(self.state['long_document_pages'], [])
        self.assertTrue(list((self.extract / 'long-document').glob('overview-merge-*')))

    def test_native_pdf_uses_bookmarks_and_preserves_page_map(self):
        import fitz
        pdf = fitz.open()
        for title in ['Chapter 1 Introduction', 'Chapter 2 Results']:
            page = pdf.new_page()
            page.insert_text((72, 72), title + '\n# numerator\nSupported evidence. ' * 5)
        pdf.set_toc([[1, 'Chapter 1 Introduction', 1], [1, 'Chapter 2 Results', 2]])
        source = self.repo / 'inbox/native.pdf'
        pdf.save(source)
        pdf.close()
        self.assertTrue(lp.pdf_profile(source)['book_structure'])
        text, mapping = lp.extract_native_pdf(source)
        self.assertEqual(len(mapping['pages']), 2)
        self.assertEqual(len(lp.parse_sections(text)), 2)
        self.assertNotIn('\n# numerator', text)
        self.assertEqual(mapping['text_sha256'], lp.digest(text))
        self.assertEqual(mapping['pages'][0]['end_line'] + 1, mapping['pages'][1]['line'])

    def test_native_pdf_rejects_visible_page_without_text(self):
        import fitz
        pdf = fitz.open()
        page = pdf.new_page()
        page.draw_rect(fitz.Rect(10, 10, 100, 100))
        source = self.repo / 'inbox/scan.pdf'
        pdf.save(source)
        pdf.close()
        with self.assertRaisesRegex(ValueError, 'no text layer'):
            lp.extract_native_pdf(source)

    def test_heading_levels_and_toc_do_not_duplicate_chapters(self):
        text = '第一章 概论 .... 1\n# Title\n## 第一章 概论\n' + '原文\n' * 250 + '## 第二章 结果\n' + '原文\n' * 250
        self.assertEqual(len(lp.parse_sections(text)), 2)
        self.assertEqual(len(lp.parse_sections('# 第一章\n内容\n# 第二章\n内容')), 2)
        self.assertEqual(len(lp.parse_sections('第一章 导论\n内容\n第二章 结果\n内容')), 2)

    def test_agent_tasks_never_call_api_and_reuse_completed_windows(self):
        patch.object(doc, 'ingest_mode', return_value='agent').start()
        with patch.object(ld, 'api_generate', side_effect=AssertionError('Agent must never call API')):
            ok, _ = doc.step_write_wiki(self.state)
            self.assertFalse(ok)
            completed = []
            for _ in range(30):
                task = self.state['agent_task']
                self.assertEqual(task['schema'], 'agent-task-v1')
                packet = json.loads((self.repo / task['inputs'][0]['path']).read_text())
                output = self.repo / task['outputs'][0]['path']
                self.assertNotIn(str(output), completed)
                ld.write_json(output, {'input_sha256': packet['input_sha256'],
                                       'result': self.result(packet['data'], task['protocol'])})
                completed.append(str(output))
                self.state['status'] = 'write_wiki'
                ok, message = doc.step_write_wiki(self.state)
                if ok:
                    break
            self.assertTrue(ok, message + str(self.state.get('agent_task', {}).get('issues')))
            self.assertEqual(self.state['long_document']['windows_validated'], self.state['long_document']['windows_total'])
            self.assertEqual(len(self.state['long_document_pages']), 3)

    def test_api_multpage_reuse_and_raw_citations(self):
        calls = self.run_api()
        self.assertTrue(calls)
        self.assertEqual(len(self.state['wiki_artifacts']), 3)
        self.assertIn('章节地图', self.state['wiki_content'])
        for page in [self.state] + self.state['long_document_pages']:
            self.assertEqual(doc.step_validate_wiki(page), [])
            self.assertIn('academic/raw/reference-documents/book.md', page['wiki_content'])
            self.assertNotIn('<RAW#', page['wiki_content'])
            self.assertNotIn('temp/', page['wiki_content'])
        with patch.object(doc, 'ingest_mode', return_value='api'), patch.object(ld, 'api_generate', side_effect=AssertionError('cache lost')):
            ok, message = doc.step_write_wiki(self.state)
        self.assertTrue(ok, message)

    def test_blank_front_matter_needs_no_impossible_evidence_task(self):
        self.text = '\n' * 2000 + self.text
        (self.repo / 'inbox/book.md').write_text(self.text)
        (self.extract / 'doc.md').write_text(self.text)
        self.run_api()
        self.assertEqual(self.state['long_document']['windows_validated'], self.state['long_document']['windows_total'])

    def test_semantic_rejection_preserves_single_overview(self):
        self.run_api(publish=False)
        self.assertEqual(self.state['long_document_pages'], [])
        self.assertEqual(self.state['long_document']['structure'], 'single-source-page')
        self.assertIn('第3章', self.state['wiki_content'])

    def test_changed_source_rejects_cached_evidence(self):
        self.run_api()
        (self.repo / 'inbox/book.md').write_text(self.text + 'changed')
        with patch.object(doc, 'ingest_mode', return_value='api'), patch.object(ld, 'api_generate', side_effect=AssertionError('unexpected call')):
            ok, message = doc.step_write_wiki(self.state)
        self.assertFalse(ok)
        self.assertIn('changed', message)

    def test_forged_evidence_and_cross_chapter_citations_rejected(self):
        value = {'summary': '结论 <RAW#L2>', 'evidence': [{'line': 2, 'quote': '伪造'}]}
        self.assertTrue(ld.note_errors(value, ['one', 'two'], {2}))
        self.assertTrue(ld.note_errors({'summary': 'one <RAW#L1>', 'evidence': [{'line': 1, 'quote': 'one'}]}, ['one'], {2}))
        self.assertTrue(ld.page_errors({'publish': True, 'rationale': 'reason', 'wiki': '<RAW#L99>', 'slots': '三元组:'}, {1}, False))

    def test_api_failure_budget_persists_across_resume(self):
        patch.object(doc, 'ingest_mode', return_value='api').start()
        with patch.object(ld, 'api_generate', return_value={'ok': False, 'error': 'offline'}) as api:
            ok, _ = doc.step_write_wiki(self.state)
            self.assertFalse(ok)
            self.assertEqual(api.call_count, 1)
            self.state['status'] = 'write_wiki'
            doc.step_write_wiki(self.state)
            self.assertEqual(api.call_count, 1)
            self.assertIn('offline', str(self.state.get('agent_task')))

    def test_second_content_attempt_receives_failed_candidate_and_remains_bounded(self):
        patch.object(doc, 'ingest_mode', return_value='api').start()
        compiler = ld.Compiler(self.state, doc, self.text)
        packet = {'source': '<RAW#L1> # 测试教材'}
        invalid = {'summary': 'wrong <RAW#L1>', 'evidence': [{'line': 1, 'quote': 'fabricated'}]}
        seen = []
        def generate(packet, protocol, state):
            seen.append(protocol)
            return {'ok': True, 'parsed': invalid}
        with patch.object(ld, 'api_generate', side_effect=generate):
            with self.assertRaises(ld.Pending):
                compiler.note('bounded-failure', packet, {1})
            with self.assertRaises(ld.Pending):
                compiler.note('bounded-failure', packet, {1})
        self.assertEqual(len(seen), 2)
        self.assertEqual([p['revision'] for p in seen], [0, 1])
        self.assertNotIn('previous_candidate', seen[0])
        self.assertEqual(seen[1]['previous_candidate'], invalid)
        self.assertTrue(seen[1]['issues'])

    def test_note_reports_shape_and_evidence_failures_together(self):
        value = {'summary': 'Claim <RAW#L2>', 'evidence': [{'line': 2, 'quote': 'rewritten'}], 'issues': []}
        errors = ld.note_errors(value, ['title', 'actual original text'], {2})
        self.assertIn('note requires exactly summary and evidence', errors)
        self.assertTrue(any('not verbatim at L2' in e for e in errors))
        self.assertTrue(any('missing evidence: L2' in e for e in errors))
        # Wrong field types must report errors without crashing independent checks.
        for invalid in (None, {}, {'summary': 3, 'evidence': [None]},
                        {'summary': 'text', 'evidence': [{'line': [], 'quote': 2}]},
                        {'summary': 'text', 'evidence': 'wrong'}):
            self.assertTrue(ld.note_errors(invalid, ['text'], {1}))
        valid = {'summary': 'Claim <RAW#L2>', 'evidence': [{'line': 2, 'quote': 'original text'}]}
        self.assertEqual(ld.note_errors(valid, ['title', 'actual original text'], {2}), [])

    def test_specialist_retry_routes_without_hidden_model_fallback(self):
        import llm_structured
        with patch.object(llm_structured, 'call_json', return_value={'ok': True}) as api:
            ld.api_generate({}, {'revision': 0}, self.state)
            ld.api_generate({}, {'revision': 1}, self.state)
        initial, recovery = [c.kwargs for c in api.call_args_list]
        self.assertEqual(initial['operation'], 'ingest_long_document')
        self.assertEqual(recovery['operation'], 'ingest_long_document_repair')
        self.assertFalse(recovery['allow_profile_fallback'])

    def test_multpage_manifest_conflict_tamper_and_rollback(self):
        self.run_api()
        (self.extract / 'book.md').write_text(self.text)
        manifest = self.extract / 'manifest.json'
        ld.write_json(manifest, {'raw_files': ['book.md'], 'wiki_file': 'wiki.md',
                                 'wiki_artifacts': self.state['wiki_artifacts']})
        raw = self.repo / self.state['raw_dir']
        wiki = self.repo / (self.state['wiki_path'] + '.md')
        child = self.repo / self.state['wiki_artifacts'][0]['destination']
        staged_child = self.repo / self.state['long_document_pages'][0]['wiki_file']
        original = staged_child.read_bytes()
        staged_child.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, 'size changed'):
            finalizer.finalize(self.repo, 'book', raw, wiki, self.extract, manifest, False, True)
        self.assertFalse(raw.exists())
        staged_child.write_bytes(original)
        child.parent.mkdir(parents=True)
        child.write_text('preexisting')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            finalizer.finalize(self.repo, 'book', raw, wiki, self.extract, manifest, False, True)
        self.assertFalse(raw.exists())
        self.assertEqual(child.read_text(), 'preexisting')
        child.unlink()
        original_link = finalizer.os.link
        def fail_second(source, destination):
            if Path(destination) == child:
                raise OSError('injected chapter landing failure')
            return original_link(source, destination)
        with patch.object(finalizer.os, 'link', side_effect=fail_second), self.assertRaises(OSError):
            finalizer.finalize(self.repo, 'book', raw, wiki, self.extract, manifest, False, True)
        self.assertFalse(raw.exists())
        self.assertFalse(wiki.exists())
        self.assertFalse(child.exists())
        receipt = finalizer.finalize(self.repo, 'book', raw, wiki, self.extract, manifest, False, True)
        data = json.loads(receipt.read_text())
        self.assertEqual(len(data['wiki_artifacts']), 3)
        self.assertTrue(child.exists())
        self.assertEqual(finalizer.finalize(self.repo, 'book', raw, wiki, self.extract, manifest, False, True), receipt)
        import inbox_state
        self.state['status'] = 'completed'
        receipt_before = inbox_state.build_verification_receipt(self.state, self.repo)
        self.assertIn('chapter_3', receipt_before['artifacts'])
        child.write_text('tampered')
        receipt_after = inbox_state.build_verification_receipt(self.state, self.repo)
        self.assertNotEqual(receipt_before['receipt_hash'], receipt_after['receipt_hash'])

    def test_real_graph_bundle_and_failure_preserve_existing_graph(self):
        import graph_ingest as gi
        import graph_lib as gl
        import sync_keyword_aliases
        self.run_api()
        for page in [self.state] + self.state['long_document_pages']:
            target = self.repo / (page['wiki_path'] + '.md')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page['wiki_content'])
        raw = self.repo / self.state['raw_dir']
        raw.mkdir(parents=True)
        (raw / 'book.md').write_text(self.text)
        database = self.repo / 'cross-domain/graph.db'
        database.parent.mkdir()
        for obj, name, value in [(gl, 'REPO', self.repo), (gl, 'GRAPH_DB', database),
                                 (gi.ingest_check, 'REPO', self.repo),
                                 (gi.wl, 'REPO', self.repo), (gi.wl.raw_locator, 'REPO', self.repo)]:
            patch.object(obj, name, value).start()
        patch.object(gi.hs, 'refresh_after_ingest', return_value={}).start()
        patch.object(sync_keyword_aliases, 'sync', return_value=(0, 0, 0)).start()
        patch.object(sync_keyword_aliases, 'resolve_abbreviation_todo', return_value=([], 0)).start()
        conn = gl.connect(database)
        gl.init_schema(conn)
        gl.ensure_node(conn, 'existing', 'Existing unrelated node', 'entity')
        conn.commit()
        before = list(conn.iterdump())
        conn.close()
        original = gi._cmd_ingest_locked
        count = 0
        def fail_second(args):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError('injected graph failure')
            return original(args)
        with patch.object(gi, '_cmd_ingest_locked', side_effect=fail_second):
            ok, message = doc.step_update_graph(self.state)
        self.assertFalse(ok)
        self.assertIn('injected graph failure', message)
        conn = gl.connect(database)
        self.assertEqual(list(conn.iterdump()), before)
        conn.close()
        count = 0
        with patch.object(gi, '_cmd_ingest_locked', side_effect=fail_second), \
             patch.object(gl, 'restore_graph', side_effect=OSError('storage unavailable')):
            ok, message = doc.step_update_graph(self.state)
        self.assertFalse(ok)
        self.assertTrue(self.state['long_graph_recovery_required'])
        doc.rollback_committed(self.state)
        self.assertTrue((raw / 'book.md').exists())
        self.assertTrue((self.repo / (self.state['wiki_path'] + '.md')).exists())
        ok, message = doc.step_update_graph(self.state)
        self.assertTrue(ok, message)
        conn = gl.connect(database)
        for page in [self.state] + self.state['long_document_pages']:
            self.assertIsNotNone(conn.execute('SELECT path FROM nodes WHERE path=?', (page['wiki_path'],)).fetchone())
            self.assertFalse(page['graph_report']['page_validation']['errors'])
        self.assertIsNotNone(conn.execute("SELECT path FROM nodes WHERE path='existing'").fetchone())
        conn.close()

    def test_graph_failure_rolls_back_every_page_and_restores_snapshot(self):
        self.run_api()
        for page in [self.state] + self.state['long_document_pages']:
            target = self.repo / (page['wiki_path'] + '.md')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page['wiki_content'])
        raw = self.repo / self.state['raw_dir']
        raw.mkdir(parents=True)
        (raw / 'book.md').write_text(self.text)
        (self.extract / 'manifest.json').write_text(json.dumps({'raw_files': ['book.md']}))
        snapshot = self.extract / 'graph-before.sqlite'
        snapshot.write_bytes(b'isolated snapshot fixture')
        self.state.update(graph_snapshot=str(snapshot.relative_to(self.repo)), graph_db_path=str(self.repo / 'cross-domain/graph.db'))
        with patch.object(doc, 'restore_sqlite_database') as restore, patch.object(doc.trash_util, 'trash_path', side_effect=lambda p: p.unlink()):
            rolled = doc.rollback_committed(self.state)
            restore.assert_called_once()
        self.assertTrue(rolled)
        for page in [self.state] + self.state['long_document_pages']:
            self.assertFalse((self.repo / (page['wiki_path'] + '.md')).exists())
        self.assertFalse((raw / 'book.md').exists())


if __name__ == '__main__':
    unittest.main()

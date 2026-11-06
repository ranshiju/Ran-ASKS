#!/usr/bin/env python3
"""Offline behavioral regression for selective PPT ingestion and library reuse."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

import pptx_document as doc
import pptx_intake as intake
import pptx_structure as st
import pptx_visual as vision
import ingest_document as ingest
import slide_library as lib
import slide_reuse as reuse
from test_slide_reuse import fixture, profile
from test_pptx_academic import result


def make_deck(path):
    fixture(path)
    with zipfile.ZipFile(path) as z:
        data = {n: z.read(n) for n in z.namelist()}
    pres = ET.fromstring(data['ppt/presentation.xml'])
    ET.SubElement(pres.find('p:sldIdLst', st.NS), '{'+st.NS['p']+'}sldId',
                  {'id': '257', '{'+st.NS['r']+'}id': 'second'})
    data['ppt/presentation.xml'] = ET.tostring(pres)
    rels = ET.fromstring(data['ppt/_rels/presentation.xml.rels'])
    ET.SubElement(rels, '{'+st.REL+'}Relationship',
                  {'Id': 'second', 'Type': st.NS['r']+'/slide', 'Target': 'slides/slide2.xml'})
    data['ppt/_rels/presentation.xml.rels'] = ET.tostring(rels)
    second = ET.fromstring(data['ppt/slides/slide1.xml'])
    tree = second.find('p:cSld/p:spTree', st.NS)
    tree.remove(tree.find('p:pic', st.NS))
    data['ppt/slides/slide2.xml'] = ET.tostring(second)
    with zipfile.ZipFile(path, 'w') as z:
        for n, b in data.items():
            z.writestr(n, b)


def plan_for(source, manifest, visual=False, templates=False):
    data = st.extract(source)
    return {'schema': intake.PLAN_SCHEMA, 'source_sha256': data['source_sha256'],
        'native_sha256': manifest['native_sha256'], 'structure_sha256': st.stable_hash(data),
        'pages': [{'number': p['number'], 'mode': 'visual' if visual and p['number'] == 1 else 'native',
            'reason': '图片承载主要图意，原生标题不足' if visual and p['number'] == 1 else '原生文字表达主要内容，图片仅作配图',
            'navigation': 'TEST-ONLY 页面主题和用途', 'visual_object_ids': ['3'] if visual and p['number'] == 1 else []}
            for p in data['pages']],
        'library_reason': 'TEST-ONLY 对比库后选择一个代表版式' if templates else '既有库已覆盖，无需重复收藏',
        'templates': [{'page': 1, 'name': '测试-图文', 'reason': 'TEST-ONLY 新增可替换图文布局',
                       'profile': profile()}] if templates else []}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.source = self.repo/'inbox/source.pptx'; self.source.parent.mkdir()
        make_deck(self.source)
        self.directory = self.repo/'temp/intake'
        for mod in (doc, vision, ingest, lib):
            p = patch.object(mod, 'REPO', self.repo); p.start(); self.addCleanup(p.stop)
        self.manifest = doc.prepare(self.source, self.directory, render=False)
        self.plan = plan_for(self.source, self.manifest)

    def renderer(self, source, directory):
        p = directory/'source-slides.pdf'; p.write_bytes(b'TEST-ONLY PDF')
        return 'slides', p, len(st.extract(source)['pages'])

    def render_page(self, kind, source, number, target, dpi):
        from PIL import Image
        Image.new('RGB', (32, 32), 'white').save(target)

    def test_native_with_decorative_picture_needs_no_renderer_or_api(self):
        with (patch.object(vision, 'analyze', side_effect=AssertionError('no vision')),
              patch.object(lib.qa, '_prepare_source', side_effect=AssertionError('no rendering'))):
            intake.prepare_review(self.source, self.directory, self.plan, planner='agent')
            text, receipt, warnings = doc.validated(self.source, self.directory)
        self.assertIn('Source title', text)
        self.assertEqual(receipt['visual_pages'], [])
        self.assertEqual(receipt['review_status'], 'native_extracted')
        self.assertTrue(all(p['visual_status'] == 'not_required' for p in receipt['pages']))
        self.assertFalse(warnings)

    def test_mixed_only_uploads_image_main_page_and_preserves_native(self):
        self.plan = plan_for(self.source, self.manifest, visual=True)
        call = Mock(return_value={'result': result(), 'usage': {}})
        env = {'VISUAL_QA_API_BASE': 'https://test.invalid/v1', 'VISUAL_QA_API_KEY': 'TEST',
               'VISUAL_QA_MODEL': 'TEST', 'VISUAL_QA_FALLBACK_MODEL': ''}
        with patch.object(lib.qa, '_prepare_source', side_effect=self.renderer), \
             patch.object(lib.qa, 'render_page', side_effect=self.render_page), \
             patch.object(lib.qa, 'load_visual_env', return_value=env), \
             patch.object(lib.qa, 'call_json_vision', call):
            intake.prepare_review(self.source, self.directory, self.plan, planner='agent', allow_remote=True)
            # Resume reuses successful receipt, without uploading the text page or repeating paid calls.
            intake.prepare_review(self.source, self.directory, self.plan, planner='agent', allow_remote=True)
            text, receipt, _ = doc.validated(self.source, self.directory)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(receipt['visual_pages'], [1])
        self.assertEqual(receipt['pages'][1]['visual_status'], 'not_required')
        self.assertIn('非完整转录', text)
        self.assertIn('Original talk notes', text)
        self.assertIn('图片文字', text)
        record = json.loads((self.directory/'pptx-review.json').read_text())
        record['pages'][0]['result']['supplement'] = 'tampered'
        (self.directory/'pptx-review.json').write_text(json.dumps(record))
        with self.assertRaises(doc.PPTXError):
            doc.validated(self.source, self.directory)

    def test_sparse_pass_adds_advisory_without_changing_sealed_api_receipt(self):
        with zipfile.ZipFile(self.source) as archive:
            package = {name: archive.read(name) for name in archive.namelist()}
        page = ET.fromstring(package['ppt/slides/slide1.xml'])
        extent = page.find('.//p:pic/p:spPr/a:xfrm/a:ext', st.NS)
        extent.set('cx', '800'); extent.set('cy', '400')
        package['ppt/slides/slide1.xml'] = ET.tostring(page)
        with zipfile.ZipFile(self.source, 'w') as archive:
            for name, data in package.items():
                archive.writestr(name, data)
        self.directory = self.repo / 'temp/sparse'
        self.manifest = doc.prepare(self.source, self.directory, render=False)
        plan = plan_for(self.source, self.manifest, visual=True)
        response = result(); response['supplement'] = ''
        call = Mock(return_value={'result': response, 'usage': {}})
        env = {'VISUAL_QA_API_BASE': 'https://test.invalid/v4', 'VISUAL_QA_API_KEY': 'TEST',
               'VISUAL_QA_API_PATH': '/chat/completions', 'VISUAL_QA_MODEL': 'TEST', 'VISUAL_QA_FALLBACK_MODEL': ''}
        with patch.object(lib.qa, '_prepare_source', side_effect=self.renderer), \
             patch.object(lib.qa, 'render_page', side_effect=self.render_page), \
             patch.object(lib.qa, 'load_visual_env', return_value=env), \
             patch.object(lib.qa, 'call_json_vision', call):
            intake.prepare_review(self.source, self.directory, plan, planner='agent', allow_remote=True)
            before = (self.directory / 'pptx-review.json').read_bytes()
            text, receipt, warnings = doc.validated(self.source, self.directory)
            self.assertEqual((self.directory / 'pptx-review.json').read_bytes(), before)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[-1].api_path, '/chat/completions')
        self.assertEqual(receipt['pages'][0]['api']['result']['verdict'], 'pass')
        self.assertEqual(receipt['review_status'], 'reviewed_with_limits')
        self.assertIn('程序检查，非原文', text)
        self.assertIn('pptx_sparse_content', [item['issue'] for item in warnings])

    def test_plan_rejects_stale_missing_and_false_visual_addresses(self):
        cases = [lambda p: p.update(source_sha256='wrong'), lambda p: p['pages'].pop(),
                 lambda p: p['pages'][0].update(mode='visual', visual_object_ids=['1']),
                 lambda p: p['pages'][0].update(visual_object_ids=['3']),
                 lambda p: p['pages'][0].update(reason=''),
                 lambda p: p['pages'].reverse()]
        for change in cases:
            p = copy.deepcopy(self.plan); change(p)
            with self.subTest(change=change), self.assertRaises(ValueError):
                intake.validate_plan(p, self.source, self.manifest)

    def test_missing_failed_or_critical_selected_review_cannot_commit(self):
        self.plan = plan_for(self.source, self.manifest, visual=True)
        missing = {'schema': intake.REVIEW_SCHEMA, 'source_sha256': self.manifest['source_sha256'],
                   'native_sha256': self.manifest['native_sha256'], 'planner': 'agent',
                   'plan': self.plan, 'plan_sha256': st.stable_hash(self.plan), 'pages': []}
        (self.directory/'pptx-review.json').write_text(json.dumps(missing))
        with self.assertRaisesRegex(doc.PPTXError, 'coverage'):
            doc.validated(self.source, self.directory)

    def test_selected_page_without_permission_is_pending_and_critical_result_blocks(self):
        self.plan = plan_for(self.source, self.manifest, visual=True)
        env = {'VISUAL_QA_API_BASE': 'https://test.invalid/v1', 'VISUAL_QA_API_KEY': 'TEST',
               'VISUAL_QA_MODEL': 'TEST', 'VISUAL_QA_FALLBACK_MODEL': ''}
        r = result(); r.update(verdict='warn', limitations=[{'detail': '主要图意无法理解', 'critical': True}])
        with patch.object(lib.qa, '_prepare_source', side_effect=self.renderer), \
             patch.object(lib.qa, 'render_page', side_effect=self.render_page), \
             patch.object(lib.qa, 'load_visual_env', return_value=env), \
             patch.object(lib.qa, 'call_json_vision', return_value={'result': r, 'usage': {}}) as api:
            pending = intake.prepare_review(self.source, self.directory, self.plan, planner='agent')
            self.assertEqual(pending['status'], 'partial')
            self.assertEqual(pending['selected_pages'], [1])
            api.assert_not_called()
            intake.prepare_review(self.source, self.directory, self.plan, planner='agent', allow_remote=True)
            with self.assertRaisesRegex(doc.PPTXError, '关键未决'):
                doc.validated(self.source, self.directory)

    def test_agent_plan_handoff_is_text_only_and_never_calls_api(self):
        state = {'transaction_id': 'test', 'source': 'inbox/source.pptx', 'source_filename': 'source.pptx',
                 'extract_dir': 'temp/intake', 'semantic_backend': 'agent', 'subproject': 'admin'}
        with patch.object(ingest, 'call_text', side_effect=AssertionError('no hidden API')):
            ok, _ = ingest.prepare_pptx_review(state, self.source, self.directory)
            self.assertFalse(ok)
            self.assertEqual(state['agent_task']['kind'], 'pptx_intake')
            self.assertFalse(any(x['path'].endswith('.png') for x in state['agent_task']['inputs']))
            (self.directory/'pptx-plan.json').write_text(json.dumps(self.plan))
            ok, msg = ingest.prepare_pptx_review(state, self.source, self.directory)
            self.assertTrue(ok, msg)
        self.assertEqual(state['pptx']['planner'], 'agent')
        self.assertEqual(state['semantic_backend'], 'agent')

    def test_api_backend_uses_text_planning_and_no_vision_for_native(self):
        state = {'transaction_id': 'test-api', 'source': 'inbox/source.pptx', 'source_filename': 'source.pptx',
                 'extract_dir': 'temp/intake', 'semantic_backend': 'api', 'subproject': 'admin'}
        with patch.object(ingest, 'call_text', return_value={'ok': True, 'text': json.dumps(self.plan)}) as call, \
             patch.object(vision, 'analyze', side_effect=AssertionError('no visual')):
            ok, msg = ingest.prepare_pptx_review(state, self.source, self.directory)
        self.assertTrue(ok, msg)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(state['pptx']['planner'], 'api')

    def test_library_collects_typed_native_template_and_resume_deduplicates(self):
        target = self.repo/'admin/raw/policies/source.pptx'; target.parent.mkdir(parents=True)
        target.write_bytes(self.source.read_bytes())
        self.plan = plan_for(target, self.manifest, templates=True)
        before = target.read_bytes()
        with patch.object(lib.qa, '_prepare_source', side_effect=self.renderer), \
             patch.object(lib.qa, 'render_page', side_effect=self.render_page), \
             patch.object(lib.qa, 'call_json_vision', side_effect=AssertionError('library is offline')):
            one = intake.enrich(target, self.plan, self.repo/'temp/enrich', instruction='TEST-ONLY enrich selected intake')
            two = intake.enrich(target, self.plan, self.repo/'temp/enrich', instruction='TEST-ONLY resume')
        self.assertEqual(one['results'][0]['status'], 'collected')
        self.assertEqual(two['results'][0]['status'], 'already_represented')
        folder = Path(one['results'][0]['path'])
        self.assertEqual(lib.verify_bundle(folder)['reuse_kind'], 'adaptable')
        native = st.extract(folder/'component.pptx')
        self.assertIn('Your title', native['pages'][0]['objects'][0]['text'])
        self.assertEqual(target.read_bytes(), before)

    def test_library_failure_does_not_fail_ingest_tail(self):
        state = {'pptx': {'intake_plan': self.plan}, 'raw_dir': 'admin/raw/policies',
                 'source_filename': 'source.pptx', 'transaction_id': 'test'}
        with (patch.object(ingest.ic, 'step_finalize_tail', return_value=(True, '')),
             patch.object(ingest.ic, 'validate_completion', return_value=[]),
             patch.object(intake, 'enrich', side_effect=ValueError('TEST-ONLY export unavailable'))):
            ok, _ = ingest.step_finalize_tail(state)
        self.assertTrue(ok)
        self.assertEqual(state['pptx_library']['status'], 'warning')
        self.assertEqual(state['quality_warnings'][0]['issue'], 'pptx_library_enrichment')

    def test_duplicate_detection_preserves_distinct_direct_content(self):
        data = st.extract(self.source)
        changed = copy.deepcopy(data)
        changed['pages'][0]['objects'][0]['text'] = 'Different content'
        self.assertNotEqual(intake.template_signature(data, profile('direct')),
                            intake.template_signature(changed, profile('direct')))
        self.assertEqual(intake.template_signature(data, profile()),
                         intake.template_signature(changed, profile()))


if __name__ == '__main__':
    unittest.main()

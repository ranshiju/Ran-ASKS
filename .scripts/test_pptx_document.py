#!/usr/bin/env python3
"""Native PPTX evidence, host handoff, resume and precommit integrity regression."""
import copy
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pptx_document as ppt
import ingest_document as ingest
import visual_qa

P, A, R = ppt.NS['p'], ppt.NS['a'], ppt.NS['r']
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'


def fixture(path):
    def doc(inner, kind='sld', attrs=''):
        return f'<p:{kind} xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" {attrs}>{inner}</p:{kind}>'
    def text(value, sid='1'):
        return f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}"/></p:nvSpPr><p:txBody><a:p><a:pPr lvl="1"/><a:r><a:t>{value}</a:t></a:r><a:br/><a:r><a:t>next</a:t></a:r></a:p></p:txBody></p:sp>'
    table = '<p:graphicFrame><a:tbl><a:tblGrid><a:gridCol w="10"/><a:gridCol w="20"/></a:tblGrid><a:tr><a:tc gridSpan="2"><a:txBody><a:p><a:r><a:t>设备</a:t></a:r></a:p></a:txBody></a:tc><a:tc hMerge="1"><a:txBody><a:p/></a:txBody></a:tc></a:tr></a:tbl></p:graphicFrame>'
    slide1 = doc('<p:cSld><p:spTree>' + text('SECOND') + '</p:spTree></p:cSld>', attrs='show="0"')
    slide2 = doc('<p:cSld><p:spTree><p:grpSp>' + text('FIRST', '2') + '</p:grpSp>' + table + '<p:pic/><p:graphicFrame/></p:spTree></p:cSld>')
    presentation = doc('<p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst><p:sldSz cx="900" cy="600"/>', 'presentation')
    rels = f'<Relationships xmlns="{REL}"><Relationship Id="rId1" Target="slides/slide1.xml" Type="{R}/slide"/><Relationship Id="rId2" Target="slides/slide2.xml" Type="{R}/slide"/></Relationships>'
    notes = doc('<p:cSld><p:spTree><p:sp><p:nvSpPr><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:p><a:r><a:t>NOTES ONLY</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>', 'notes')
    with zipfile.ZipFile(path, 'w') as z:
        for name, value in {'ppt/presentation.xml': presentation, 'ppt/_rels/presentation.xml.rels': rels,
                            'ppt/slides/slide1.xml': slide1, 'ppt/slides/slide2.xml': slide2,
                            'ppt/slides/_rels/slide2.xml.rels': f'<Relationships xmlns="{REL}"><Relationship Id="n" Target="../notesSlides/notesSlide1.xml" Type="{R}/notesSlide"/></Relationships>',
                            'ppt/notesSlides/notesSlide1.xml': notes}.items():
            z.writestr(name, value)


def render_source(source, directory):
    pdf = directory / 'source-slides.pdf'
    if not pdf.exists():
        pdf.write_bytes(b'fake local PDF')
    return 'slides', pdf, 2


def render_page(kind, source, number, target, dpi):
    if not target.exists():
        from PIL import Image
        Image.new('RGB', (32, 32), (number, 0, 0)).save(target)


def reviewed(manifest):
    protocol = ppt.review_protocol(manifest)
    return {'schema': ppt.REVIEW_SCHEMA, 'source_sha256': protocol['source_sha256'],
            'native_sha256': protocol['native_sha256'],
            'reviewer': {'kind': 'agent', 'name': 'test-host', 'reviewed_at': '2026-09-11T10:00:00+08:00'},
            'pages': [{**p, 'note': '核对原生文字、表格空格及布局', 'supplement': '', 'limitations': [], 'ocr_receipt': ''}
                      for p in protocol['pages']]}


class PPTXTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.source = self.repo / 'inbox/20260911-test.pptx'
        self.source.parent.mkdir()
        fixture(self.source)
        self.directory = self.repo / 'temp/inbox-extract/test'
        for patcher in (patch.object(ppt, 'REPO', self.repo), patch.object(ingest, 'REPO', self.repo),
                        patch.object(visual_qa, '_prepare_source', side_effect=render_source),
                        patch.object(visual_qa, 'render_page', side_effect=render_page)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def stage(self):
        self.manifest = ppt.prepare(self.source, self.directory)
        self.review = reviewed(self.manifest)
        self.write_review()
        return self.manifest

    def write_review(self):
        (self.directory / 'pptx-review.json').write_text(json.dumps(self.review, ensure_ascii=False))

    def test_native_order_notes_tables_groups_hidden(self):
        before = self.source.read_bytes()
        text, manifest = ppt.extract(self.source)
        self.assertLess(text.index('FIRST'), text.index('SECOND'))
        self.assertIn('  FIRST\nnext', text)
        self.assertIn('R1: ["设备", ""]', text)
        self.assertIn('"gridSpan": "2"', text)
        self.assertIn('"hMerge": "1"', text)
        self.assertIn('### 演讲者备注', text)
        self.assertIn('NOTES ONLY', text)
        self.assertTrue(manifest['pages'][1]['hidden'])
        self.assertEqual(manifest['pages'][0]['objects']['groups'], 1)
        self.assertEqual(manifest['pages'][0]['objects']['unsupported'], 1)
        self.assertEqual(self.source.read_bytes(), before)

    def test_review_required_fields_and_integrity(self):
        self.stage()
        ppt.validated(self.source, self.directory)
        original = copy.deepcopy(self.review)
        changes = [lambda r: r['pages'].pop(),
                   lambda r: r['pages'][0]['checks'].update(table='unresolved'),
                   lambda r: r['pages'][0]['checks'].update(critical_values='unresolved'),
                   lambda r: r['pages'][0]['checks'].update(visual_information='unresolved'),
                   lambda r: r['pages'][0].update(image_sha256='wrong'),
                   lambda r: r['pages'][0].update(limitations=[{'critical': True, 'detail': '金额未核对'}]),
                   lambda r: r.update(source_sha256='wrong'),
                   lambda r: r['reviewer'].update(kind='human')]
        for change in changes:
            with self.subTest(change=change):
                self.review = copy.deepcopy(original)
                change(self.review)
                self.write_review()
                with self.assertRaises(ppt.PPTXError):
                    ppt.validated(self.source, self.directory)

    def test_source_native_and_render_tampering(self):
        self.stage()
        for file in [self.source, self.directory / 'pptx-native.md',
                     self.directory / 'pptx-renders/page-0001.png', self.directory / 'pptx-renders/source-slides.pdf']:
            with self.subTest(file=file):
                original = file.read_bytes()
                file.write_bytes(original + b'changed')
                with self.assertRaises(ppt.PPTXError):
                    ppt.validated(self.source, self.directory)
                file.write_bytes(original)

    def test_render_failure_is_structured(self):
        with patch.object(visual_qa, '_prepare_source', side_effect=visual_qa.VisualQAError('missing renderer')):
            with self.assertRaisesRegex(ppt.PPTXError, 'missing renderer'):
                ppt.prepare(self.source, self.directory)

    def test_page_count_mismatch_blocks(self):
        with patch.object(visual_qa, '_prepare_source', return_value=('slides', self.source, 1)):
            with self.assertRaisesRegex(ppt.PPTXError, '页数'):
                ppt.prepare(self.source, self.directory)

    def test_limits_and_supplement_are_explicit(self):
        self.stage()
        self.review['pages'][0].update(supplement='图片原文', limitations=[{'critical': False, 'detail': '校徽小字未转写'}])
        self.write_review()
        text, receipt, warnings = ppt.validated(self.source, self.directory)
        self.assertIn('> 图片原文', text)
        self.assertEqual(receipt['review_status'], 'reviewed_with_limits')
        self.assertEqual(len(warnings), 1)
        self.assertIn('> 校徽小字未转写', text)

    def test_agent_and_api_handoff_and_manifest_commit_guard(self):
        for backend in ['agent', 'api']:
            with self.subTest(backend=backend):
                directory = self.repo / ('temp/inbox-extract/' + backend)
                state = {'source': str(self.source.relative_to(self.repo)), 'source_filename': self.source.name,
                         'extract_dir': str(directory.relative_to(self.repo)), 'transaction_id': backend,
                         'subproject': 'admin', 'semantic_backend': backend}
                with patch.object(ingest, 'call_text', side_effect=AssertionError('no API before review')):
                    ok, _ = ingest.step_preprocess(state)
                self.assertFalse(ok)
                self.assertEqual(state['agent_task']['kind'], 'pptx_review')
                self.assertEqual(state['pre_handoff_status'], 'preprocess')
                self.assertEqual(state['semantic_backend'], backend)
                manifest = json.loads((directory / 'pptx-manifest.json').read_text())
                (directory / 'pptx-review.json').write_text(json.dumps(reviewed(manifest)))
                ok, message = ingest.step_preprocess(state)
                self.assertTrue(ok, message)
                self.assertEqual(ingest._manifest_raw_files(state), [self.source.name, self.source.stem + '.md', self.source.name + '.source.json'])
                with patch.object(ingest.ic, 'step_finalize', return_value=(True, 'committed')) as commit:
                    ok, message = ingest.step_finalize(state)
                    self.assertTrue(ok, message)
                    commit.assert_called_once()
                for name in [self.source.stem + '.md', self.source.name + '.source.json', self.source.name]:
                    file = directory / name
                    original = file.read_bytes()
                    file.write_bytes(original + b'changed')
                    with patch.object(ingest.ic, 'step_finalize') as commit:
                        ok, message = ingest.step_finalize(state)
                        self.assertFalse(ok, message)
                        commit.assert_not_called()
                    file.write_bytes(original)

    def test_pptx_wiki_identity_uses_source_not_extractor_heading(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'doc.md').write_text('# PPTX 逐页原生提取\n\n# Slide title')
        state = {'source_filename': '20260911-设备更新答辩.PPTX',
                 'extract_dir': str(self.directory.relative_to(self.repo)),
                 'date_str': '2026-09-11', 'subproject': 'admin',
                 'document_type': 'application'}
        # Stop at the semantic adapter boundary after identity is selected.
        with patch.object(ingest, 'ensure_unique_admin_id', side_effect=lambda base, *args: base), \
             patch.object(ingest, 'ingest_mode', side_effect=RuntimeError('adapter boundary')):
            with self.assertRaisesRegex(RuntimeError, 'adapter boundary'):
                ingest.step_write_wiki(state)
        self.assertEqual(state['_pending_title'], '20260911-设备更新答辩')
        self.assertNotIn('逐页原生提取', state['admin_id'])
        self.assertIn('设备更新答辩', state['admin_id'])

    def test_reused_ocr_requires_exact_page_image(self):
        import image_ocr
        self.stage()
        info, _ = image_ocr.read_image(self.directory / 'pptx-renders/page-0002.png')
        receipt = image_ocr.make_receipt(info, 'wrong page OCR', backend='agent')
        path = self.directory / 'wrong-ocr.json'
        path.write_text(json.dumps(receipt))
        self.review['pages'][0]['ocr_receipt'] = str(path)
        self.write_review()
        with self.assertRaises(image_ocr.ImageOCRError):
            ppt.validated(self.source, self.directory)


if __name__ == '__main__':
    unittest.main()

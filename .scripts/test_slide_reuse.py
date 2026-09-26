#!/usr/bin/env python3
"""Offline tests of typed reuse, native replacements and immutable source bundles."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from PIL import Image
import pptx_structure as st
import slide_library as lib
import slide_reuse as reuse


def fixture(path):
    p, a, r = st.NS['p'], st.NS['a'], st.NS['r']
    def shape(sid, text):
        return f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="shape"/></p:nvSpPr><p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="400" cy="100"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2400" b="1"><a:latin typeface="Arial"/></a:rPr><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>'
    picture = '<p:pic><p:nvPicPr><p:cNvPr id="3" name="picture"/></p:nvPicPr><p:blipFill><a:blip r:embed="image"/><a:srcRect l="1000"/><a:stretch><a:fillRect/></a:stretch></p:blipFill><p:spPr><a:xfrm><a:off x="0" y="100"/><a:ext cx="400" cy="200"/></a:xfrm></p:spPr></p:pic>'
    slide = f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{r}"><p:cSld><p:spTree>{shape(1,"Source title")}{shape(2,"Fixed label")}{picture}</p:spTree></p:cSld></p:sld>'
    notes = f'<p:notes xmlns:p="{p}" xmlns:a="{a}"><p:cSld><p:spTree>{shape(1,"Original talk notes")}</p:spTree></p:cSld></p:notes>'
    image = io.BytesIO(); Image.new('RGB', (10, 10), 'red').save(image, 'PNG')
    entries = {
        'ppt/presentation.xml': f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId id="256" r:id="slide"/></p:sldIdLst><p:sldSz cx="900" cy="600"/></p:presentation>',
        'ppt/_rels/presentation.xml.rels': f'<Relationships xmlns="{st.REL}"><Relationship Id="slide" Type="{r}/slide" Target="slides/slide1.xml"/></Relationships>',
        '_rels/.rels': f'<Relationships xmlns="{st.REL}"><Relationship Id="office" Type="{r}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        '[Content_Types].xml': '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="png" ContentType="image/png"/></Types>',
        'ppt/slides/slide1.xml': slide, 'ppt/notesSlides/notesSlide1.xml': notes,
        'ppt/slides/_rels/slide1.xml.rels': f'<Relationships xmlns="{st.REL}"><Relationship Id="image" Type="{r}/image" Target="../media/original.png"/><Relationship Id="notes" Type="{r}/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>',
        'ppt/media/original.png': image.getvalue()}
    with zipfile.ZipFile(path, 'w') as z:
        for name, value in entries.items():
            z.writestr(name, value)


def profile(kind='adaptable'):
    return {'schema': reuse.PROFILE_SCHEMA, 'kind': kind,
            'use_when': ['TEST-ONLY method comparison'], 'constraints': ['Check source and actual display'],
            'slots': [] if kind == 'direct' else [
                {'id': 'title', 'page': 1, 'object_id': '1', 'type': 'text', 'label': 'Title',
                 'guidance': 'Short title', 'placeholder': 'Your title', 'max_chars': 40, 'max_lines': 2, 'max_chars_per_line': 25},
                {'id': 'figure', 'page': 1, 'object_id': '3', 'type': 'image', 'label': 'Figure',
                 'guidance': 'PNG/JPEG', 'placeholder': 'Your figure'}],
            'fixed_objects': [] if kind == 'direct' else [{'page': 1, 'object_id': '2', 'reason': 'Fixed separator'}]}


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.source = self.repo/'academic/raw/reference-documents/test.pptx'
        self.source.parent.mkdir(parents=True); fixture(self.source)
        p = patch.object(lib, 'REPO', self.repo); p.start(); self.addCleanup(p.stop)
        def render(source, directory):
            pdf = directory/'source-slides.pdf'; pdf.write_bytes(b'TEST-ONLY')
            return 'slides', pdf, 1
        def page(kind, source, number, target, dpi):
            Image.new('RGB', (32, 32), 'white').save(target)
        for p in (patch.object(lib.qa, '_prepare_source', side_effect=render), patch.object(lib.qa, 'render_page', side_effect=page)):
            p.start(); self.addCleanup(p.stop)
        staged = self.repo/'temp/base'
        lib.export(self.source, '1', staged, 'source')
        self.base = Path(lib.collect(staged, 'source', 'TEST-ONLY selection')['path'])

    def typed(self, kind='adaptable', name=None):
        name = name or kind
        target = self.repo/'temp'/name
        reuse.prepare(self.base, profile(kind), target, name)
        return Path(lib.collect(target, name, 'TEST-ONLY typed selection')['path'])

    def test_direct_is_byte_identical_and_has_usage(self):
        direct = self.typed('direct')
        self.assertEqual((direct/'component.pptx').read_bytes(), (self.base/'component.pptx').read_bytes())
        self.assertIn('直接使用型', (direct/'component.md').read_text())
        self.assertEqual(lib.verify_bundle(direct)['reuse_kind'], 'direct')

    def test_both_types_coexist_and_deduplicate_by_kind_and_profile(self):
        self.typed('direct'); self.typed()
        again = lib.collect(self.repo/'temp/adaptable', 'another', 'TEST-ONLY')
        self.assertEqual(again['status'], 'already_collected')
        self.assertEqual(len(lib.search('', 'direct')), 1)
        self.assertEqual(len(lib.search('', 'adaptable')), 1)
        self.assertEqual(len(lib.search('', 'unclassified')), 1)
        index = (self.repo/'slide-library/_index.md').read_text()
        self.assertIn('直接使用型', index); self.assertIn('套用填充型', index)

    def test_native_slots_replace_text_image_and_notes_preserve_style_and_fixed_object(self):
        before = (self.base/'component.pptx').read_bytes()
        adaptable = self.typed()
        data = st.extract(adaptable/'component.pptx')['pages'][0]
        self.assertIn('Your title', data['objects'][0]['text'])
        photo = self.repo/'temp/new.png'; Image.new('RGB', (100, 200), 'blue').save(photo)
        output = self.repo/'temp/filled'
        reuse.fill(adaptable, {'title': 'New title\nSecond line', 'figure': str(photo)}, output)
        page = st.extract(output/'component.pptx')['pages'][0]
        self.assertEqual(page['objects'][0]['text'], 'New title\nSecond line')
        self.assertEqual(page['objects'][0]['styles'][0]['font_size_pt'], 24)
        self.assertEqual(page['objects'][1]['text'], 'Fixed label')
        self.assertNotIn('Original talk notes', page['notes'])
        self.assertEqual((self.base/'component.pptx').read_bytes(), before)
        with zipfile.ZipFile(output/'component.pptx') as z:
            im = Image.open(io.BytesIO(z.read('ppt/media/reuse-1-figure.png')))
            self.assertEqual(im.size, (1000, 500))
            self.assertEqual(im.getpixel((500, 250)), (0, 0, 255))
            self.assertNotEqual(im.getpixel((0, 0)), (0, 0, 255))
        receipt = reuse.read_json(output/'instance.json')
        self.assertEqual(receipt['visual_review'], 'not_executed')
        self.assertEqual(receipt['user_approval'], 'not_requested')

    def test_invalid_profiles_reject_missing_duplicate_or_unsupported_slots(self):
        data = st.extract(self.base/'component.pptx')
        variants = []
        p = profile(); p['fixed_objects'] = []; variants.append(p)
        p = profile(); p['slots'][0]['object_id'] = '999'; variants.append(p)
        p = profile(); p['slots'].append(copy.deepcopy(p['slots'][0])); variants.append(p)
        p = profile(); p['slots'][0]['type'] = 'table'; variants.append(p)
        p = profile(); p['slots'][0]['max_chars'] = 0; variants.append(p)
        for p in variants:
            with self.assertRaises(ValueError): reuse.validate_profile(p, data)

    def test_missing_extra_placeholder_and_oversized_values_do_not_write(self):
        adaptable = self.typed()
        photo = self.repo/'temp/photo.png'; Image.new('RGB', (10, 10)).save(photo)
        cases = [{'title': 'Title'}, {'title': 'Title', 'figure': str(photo), 'unknown': 'x'},
                 {'title': 'Your title', 'figure': str(photo)}, {'title': 'A'*26, 'figure': str(photo)},
                 {'title': 'A\nB\nC', 'figure': str(photo)}]
        for values in cases:
            with self.assertRaises(ValueError): reuse.fill(adaptable, values, self.repo/'temp/rejected')
            self.assertFalse((self.repo/'temp/rejected').exists())
        with self.assertRaises(ValueError): reuse.fill(self.base, {}, self.repo/'temp/not-adaptable')

    def test_integrity_private_symlinks_and_output_scope(self):
        adaptable = self.typed()
        private = self.repo/'private/raw/photo.png'; private.parent.mkdir(parents=True); Image.new('RGB',(10,10)).save(private)
        with self.assertRaises(ValueError): reuse.fill(adaptable, {'title': 'Hello', 'figure': str(private)}, self.repo/'temp/no')
        with self.assertRaises(ValueError): reuse.prepare(self.base, profile('direct'), self.repo/'academic/raw/no', 'no')
        link = self.repo/'temp/link'; link.symlink_to(private)
        with self.assertRaises(ValueError): reuse.safe_input(link)
        (adaptable/'reuse.json').write_text('{}')
        with self.assertRaises(ValueError): lib.verify_bundle(adaptable)

    def test_render_failure_is_atomic_and_source_unchanged(self):
        before = (self.base/'component.pptx').read_bytes()
        with patch.object(lib.qa, '_prepare_source', side_effect=ValueError('TEST-ONLY render failure')):
            with self.assertRaises(ValueError): reuse.prepare(self.base, profile(), self.repo/'temp/failed', 'failed')
        self.assertFalse((self.repo/'temp/failed').exists())
        self.assertEqual((self.base/'component.pptx').read_bytes(), before)
        self.assertEqual(list((self.repo/'temp').glob('.reuse-*')), [])

    def test_layout_collection_binds_derivative_and_preserves_api_identity(self):
        staged = self.repo/'temp/reviewed'
        reuse.prepare(self.base, profile(), staged, 'reviewed')
        report_dir = self.repo/'temp/layout'; (report_dir/'pptx-renders').mkdir(parents=True)
        image = report_dir/'pptx-renders/page.png'; Image.new('RGB', (32, 32)).save(image)
        data = st.extract(staged/'component.pptx')
        receipt = {'schema': 'pptx-visual-v1', 'mode': 'layout', 'page': 1,
                   'source_sha256': data['source_sha256'], 'structure_sha256': st.stable_hash(data['pages'][0]),
                   'image_sha256': lib.vision.digest(image), 'reference_sha256': None,
                   'status': 'complete', 'backend': 'api', 'model': 'TEST-ONLY',
                   'result': {'verdict': 'pass', 'summary': 'TEST-ONLY layout', 'supplement': '',
                              'issues': [], 'limitations': [], 'regions': []}}
        receipt['receipt_sha256'] = st.stable_hash(receipt)
        report = {'schema': 'pptx-analysis-v1', 'mode': 'layout', 'status': 'complete',
                  'source_sha256': data['source_sha256'], 'pages': [receipt]}
        lib.vision.write(report_dir/'pptx-manifest.json', {'pages': [{'image': 'page.png'}]})
        lib.vision.write(report_dir/'layout-analysis.json', report)
        collected = Path(lib.collect(staged, 'reviewed', 'TEST-ONLY', report_dir/'layout-analysis.json')['path'])
        self.assertEqual(lib.verify_bundle(collected)['layout_analysis'], 'api')
        self.assertIn('TEST-ONLY layout', (collected/'component.md').read_text())
        report['source_sha256'] = 'changed'
        lib.vision.write(report_dir/'layout-analysis.json', report)
        with self.assertRaises(ValueError): reuse.validate_layout(staged, report_dir/'layout-analysis.json')


if __name__ == '__main__':
    unittest.main()

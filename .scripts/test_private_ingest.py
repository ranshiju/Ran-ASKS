#!/usr/bin/env python3
"""Synthetic source transactions; no personal data and no network calls."""
import datetime
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import graph_lib as gl
import image_ocr
import embed_helper
import private_ingest as pi
import private_reingest as pr
import test_private_reingest as fixtures
from test_private_reingest import SEMANTIC, digest


class PrivateCreateTests(unittest.TestCase):
    def setUp(self):
        fixtures.PrivateReingestTests.setUp(self)
        self.input = self.root / 'private/outputs/input.md'
        self.input.parent.mkdir(parents=True)
        self.input.write_text('# Source\nSynthetic observation.\n')
        self.new_page = 'private/wiki/health/new-record'
        self.target = self.root / (self.new_page + '.md')

    def prepare(self, **kwargs):
        result = pi.prepare(self.new_page, 'New record', 'health-record', '2026-09-23', self.input, **kwargs)
        work = Path(result['workspace'])
        source = result['agent_task']['protocol']['frontmatter']['sources'][0]
        candidate = work / 'candidate.md'
        candidate.write_text(candidate.read_text().replace('## Navigation\n', '## Navigation\n\nA private record.\n')
                             .replace('## Content\n', '## Content\n\nSynthetic observation.[^r]\n')
                             + f'\n[^r]: {source}#L2\n')
        (work / 'semantic.txt').write_text(SEMANTIC)
        return result['transaction_id'], work

    def test_commit_integrity_isolation_and_repeat(self):
        txn, work = self.prepare()
        public = digest(self.public)
        old_raw = digest(self.raw)
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')) as network:
            result = pi.commit(txn)
        network.assert_not_called()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(digest(self.public), public)
        self.assertEqual(digest(self.raw), old_raw)
        for entry in result['files']:
            self.assertEqual(digest(self.root / entry['destination']), entry['sha256'])
        self.assertFalse((self.root / 'temp').exists())
        self.assertTrue((self.root / 'private/outputs/graph-writer').is_dir())
        self.assertTrue((self.root / 'private/source-fingerprints.db').is_file())
        self.assertGreaterEqual(result['source_fingerprints']['indexed'], 1)
        self.assertEqual(pi.commit(txn), result)
        self.assertTrue(self.target.is_file())

    def test_fingerprint_cache_failure_is_receipted_without_rolling_back_facts(self):
        txn, _work = self.prepare()
        with patch.object(pi.sf, 'reconcile', side_effect=OSError('private index unavailable')):
            result = pi.commit(txn)
        self.assertEqual(result['status'], 'completed')
        self.assertIsNone(result['source_fingerprints'])
        self.assertIn({
            'issue': 'fingerprint_register_failed',
            'detail': 'private index unavailable',
        }, result['warnings'])
        self.assertTrue(self.target.is_file())

    def test_source_hash_changes_rejected_before_writes(self):
        txn, work = self.prepare()
        (work / 'bundle/input.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            pi.commit(txn)
        self.assertFalse(self.target.exists())

    def test_web_provenance_survives_commit(self):
        txn, work = self.prepare(source_type='web')
        public = digest(self.public)
        result = pi.commit(txn)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(gl.read_frontmatter(self.target)['source_type'], 'web')
        self.assertEqual(gl.read_frontmatter(self.target)['confidence'], 'medium')
        self.assertEqual(digest(self.public), public)
        raw = self.root / result['files'][0]['destination']
        self.assertEqual(raw.read_bytes(), self.input.read_bytes())

    def test_source_provenance_cannot_change_after_prepare(self):
        txn, work = self.prepare(source_type='web')
        candidate = work / 'candidate.md'
        candidate.write_text(candidate.read_text().replace('source_type: web', 'source_type: user-assertion'))
        with self.assertRaisesRegex(ValueError, 'source_type changed'):
            pi.commit(txn)
        self.assertFalse(self.target.exists())

    def test_text_layer_pdf_archives_companion_with_page_bound_evidence(self):
        import fitz
        source = self.root / 'private/outputs/report.pdf'
        document = fitz.open()
        page = document.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 520, 700),
                            'Synthetic official health report.\n' * 12)
        document.save(source)
        document.close()
        result = pi.prepare(self.new_page, 'New record', 'health-record', '2026-09-23',
                            None, document=source)
        work = Path(result['workspace'])
        raw_source = result['agent_task']['protocol']['frontmatter']['sources'][0]
        self.assertEqual(result['agent_task']['protocol']['frontmatter']['source_type'], 'official-doc')
        self.assertIn('## Page 1', (work / 'extracted.md').read_text())
        state = json.loads((work / 'state.json').read_text())
        self.assertEqual(state['document_companion'], 'report.md')
        self.assertEqual(len(state['files']), 3)
        self.assertEqual((work / 'bundle/report.md').read_text(), (work / 'extracted.md').read_text())
        context = json.loads((work / 'bundle/report.pdf.source.json').read_text())
        self.assertEqual(context['companion']['schema'], 'raw-companion-v1')
        self.assertEqual(context['companion']['locator_scheme'], 'page-heading')
        candidate = work / 'candidate.md'
        candidate.write_text(candidate.read_text().replace(
            '## Navigation\n', '## Navigation\n\nA private PDF record.[^r]\n').replace(
            '## Content\n', '## Content\n\nSynthetic official health report.[^r]\n')
            + f'\n[^r]: {raw_source}#page-1\n')
        (work / 'semantic.txt').write_text(SEMANTIC)
        receipt = pi.commit(result['transaction_id'])
        self.assertEqual(receipt['status'], 'completed')
        archived = self.root / next(item['destination'] for item in receipt['files']
                                    if item['destination'].endswith('.pdf'))
        companion = archived.with_suffix('.md')
        self.assertEqual(archived.read_bytes(), source.read_bytes())
        self.assertEqual(companion.read_text(), (work / 'extracted.md').read_text())
        self.assertEqual(gl.read_frontmatter(self.target)['sources'], [raw_source])
        source_target, read_target = pi.sl.evidence_targets(archived, 'page-1')
        self.assertEqual(source_target, archived)
        self.assertEqual(read_target, companion)
        self.assertIn('Synthetic official health report', pi.sl.read_locator_text(read_target, 'page-1'))

    def test_pdf_companion_change_is_rejected_before_writes(self):
        import fitz
        source = self.root / 'private/outputs/report.pdf'
        document = fitz.open()
        document.new_page().insert_textbox(
            fitz.Rect(72, 72, 520, 700), 'Synthetic official health report.\n' * 12)
        document.save(source)
        document.close()
        result = pi.prepare(self.new_page, 'New record', 'health-record', '2026-09-23',
                            None, document=source)
        work = Path(result['workspace'])
        (work / 'bundle/report.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'hash mismatch|companion mismatch'):
            pi.commit(result['transaction_id'])
        self.assertFalse(self.target.exists())

    def test_completed_legacy_pdf_transaction_backfills_companion(self):
        import fitz
        source = self.root / 'private/outputs/report.pdf'
        document = fitz.open()
        document.new_page().insert_textbox(
            fitz.Rect(72, 72, 520, 700), 'Synthetic official health report.\n' * 12)
        document.save(source)
        document.close()
        result = pi.prepare(self.new_page, 'New record', 'health-record', '2026-09-23',
                            None, document=source)
        work = Path(result['workspace'])
        raw_source = result['agent_task']['protocol']['frontmatter']['sources'][0]
        candidate = work / 'candidate.md'
        candidate.write_text(candidate.read_text().replace(
            '## Navigation\n', '## Navigation\n\nA private PDF record.[^r]\n').replace(
            '## Content\n', '## Content\n\nSynthetic official health report.[^r]\n')
            + f'\n[^r]: {raw_source}#page-1\n')
        (work / 'semantic.txt').write_text(SEMANTIC)
        receipt = pi.commit(result['transaction_id'])
        companion_item = next(item for item in receipt['files']
                              if item['destination'].endswith('.md'))
        companion = self.root / companion_item['destination']
        sidecar_item = next(item for item in receipt['files']
                            if item['destination'].endswith('.source.json'))
        sidecar = self.root / sidecar_item['destination']
        companion.unlink()
        sidecar.unlink()
        state = json.loads((work / 'state.json').read_text())
        state['files'] = [item for item in state['files']
                          if item['destination'] not in {
                              companion_item['destination'], sidecar_item['destination']}]
        state.pop('document_companion')
        state.pop('document_sidecar')
        state.pop('companion')
        state['receipt']['files'] = [item for item in state['receipt']['files']
                                     if item['destination'] not in {
                                         companion_item['destination'], sidecar_item['destination']}]
        (work / 'state.json').write_text(json.dumps(state))
        (work / 'receipt.json').write_text(json.dumps(state['receipt']))

        upgraded = pi.commit(result['transaction_id'])
        self.assertTrue(companion.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertEqual(upgraded['companion_backfill']['destination'],
                         companion_item['destination'])
        self.assertEqual(pi.sl.companion_binding_status(
            self.root / raw_source, companion)['status'], 'valid')
        self.assertEqual(pi.commit(result['transaction_id']), upgraded)

    def test_pdf_requires_text_layer_and_separate_source_mode(self):
        import fitz
        source = self.root / 'private/outputs/blank.pdf'
        document = fitz.open()
        document.new_page()
        document.save(source)
        document.close()
        with self.assertRaisesRegex(ValueError, 'text layer'):
            pi.prepare(self.new_page, 'New', 'health-record', '2026-09-23', None,
                       document=source)
        with self.assertRaisesRegex(ValueError, 'separate source modes'):
            pi.prepare(self.new_page, 'New', 'health-record', '2026-09-23', self.input,
                       document=source)

    def test_invalid_text_provenance_and_image_override_rejected(self):
        with self.assertRaisesRegex(ValueError, 'invalid text source type'):
            self.prepare(source_type='unknown')
        with self.assertRaisesRegex(ValueError, 'fixed to ocr'):
            self.prepare(source_type='web', image=self.input)

    def test_existing_raw_never_overwritten(self):
        txn, work = self.prepare()
        state = json.loads((work / 'state.json').read_text())
        destination = self.root / state['files'][0]['destination']
        destination.parent.mkdir(parents=True)
        destination.write_text('Existing source')
        with self.assertRaises(FileExistsError):
            pi.commit(txn)
        self.assertEqual(destination.read_text(), 'Existing source')

    def test_post_graph_failure_restores_all_layers(self):
        txn, work = self.prepare()
        with gl.connect(self.private, read_only=True) as conn:
            old_graph = pr._graph_signature(conn, self.new_page)
        with patch.object(pr, '_graph_validation', return_value=(['injected failure'], [])):
            with self.assertRaisesRegex(ValueError, 'injected failure'):
                pi.commit(txn)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / 'private/raw/health/new-record-2026-09-23/input.md').exists())
        with gl.connect(self.private, read_only=True) as conn:
            self.assertEqual(pr._graph_signature(conn, self.new_page), old_graph)
        self.assertFalse((self.root / 'private/wiki/log.md').exists())
        self.assertEqual(json.loads((work / 'state.json').read_text())['status'], 'rolled_back')

    def test_bad_citation_rolls_back_sources(self):
        txn, work = self.prepare()
        candidate = work / 'candidate.md'
        candidate.write_text(candidate.read_text().replace('#L2', '#L9999'))
        with self.assertRaisesRegex(ValueError, 'validation failed'):
            pi.commit(txn)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / 'private/raw/health/new-record-2026-09-23/input.md').exists())

    def test_public_input_and_symlink_rejected(self):
        public_input = self.root / 'public.md'
        public_input.write_text('Synthetic')
        with self.assertRaises(ValueError):
            pi.prepare(self.new_page, 'New', 'health-record', '2026-09-23', public_input)
        link = self.root / 'private/outputs/link.md'
        link.symlink_to(public_input)
        with self.assertRaises(ValueError):
            pi.prepare(self.new_page, 'New', 'health-record', '2026-09-23', link)

    def test_image_bundle_and_review_gate(self):
        from PIL import Image
        source = self.root / 'private/outputs/chart.png'
        Image.new('RGB', (20, 20), 'white').save(source)
        info, _ = image_ocr.read_image(source)
        receipt = image_ocr.make_receipt(info, 'Synthetic label\n', backend='agent')
        ocr = source.with_suffix('.json')
        ocr.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'review'):
            self.prepare(image=source, ocr=ocr)
        receipt['review'] = {'schema': 'image-ocr-review-v1', 'source_sha256': info['sha256'],
            'text_sha256': receipt['text_sha256'], 'reviewer_kind': 'agent', 'reviewer': 'test fixture',
            'reviewed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'risk': 'ordinary',
            'checks': [{'field': 'synthetic', 'locator': 'L1', 'critical': True,
                        'status': 'verified', 'note': 'synthetic fixture only'}], 'limitations': []}
        ocr.write_text(json.dumps(receipt))
        txn, work = self.prepare(image=source, ocr=ocr)
        result = pi.commit(txn)
        self.assertEqual(len(result['files']), 4)
        raw_dir = (self.root / result['files'][0]['destination']).parent
        self.assertEqual((raw_dir / 'chart.png').read_bytes(), source.read_bytes())
        self.assertEqual((raw_dir / 'chart.md').read_text(), receipt['markdown'])
        self.assertEqual(json.loads((raw_dir / 'chart.png.source.json').read_text())['ocr']['review_status'], 'agent-reviewed')

    def test_offline_blocks_all_embedding_entrypoints(self):
        with patch('urllib.request.urlopen') as network, embed_helper.offline():
            for call in (lambda: embed_helper.embed('secret'), lambda: embed_helper.embed_batch(['secret']),
                         lambda: embed_helper.embed_cached_batch(['secret'])):
                with self.assertRaisesRegex(RuntimeError, 'local-only'):
                    call()
        network.assert_not_called()
        self.assertFalse((self.root / 'cross-domain/embeddings.db').exists())


if __name__ == '__main__':
    unittest.main()

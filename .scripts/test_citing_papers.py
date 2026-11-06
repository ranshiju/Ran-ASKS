#!/usr/bin/env python3
"""Independent citation collections, offline reuse and backwards compatibility."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import citing_papers as cp


class CitingPapersTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline regression')))
        self.target = {'doi': '10.1234/target', 'title': 'Target paper'}
        self.payload = {'schema': cp.IMPORT_SCHEMA, 'target': self.target,
                        'provider': 'synthetic export', 'retrieved_at': '2020-01-01T00:00:00Z',
                        'scope': 'synthetic fixture', 'complete': True,
                        'records': [{'doi': '10.1234/citing', 'title': 'Citing paper', 'provider_id': 'https://openalex.org/W2'}]}
        cp.initialize(self.root, 'collection', self.target)

    def state(self, collection='collection'):
        return cp.load(self.root, collection)

    def import_(self, payload=None):
        return cp.import_collection(self.root, 'collection', payload or self.payload)

    def provider(self, *, complete=True):
        target = {'id': 'https://openalex.org/W1', 'doi': '10.1234/target', 'display_name': 'Target paper'}
        work = {'id': 'https://openalex.org/W2', 'doi': '10.1234/citing', 'display_name': 'Citing paper',
                'referenced_works': ['https://openalex.org/W1']}
        responses = iter([target, {'results': [work], 'meta': {'count': 1, 'next_cursor': None if complete else 'next'}}])
        return lambda _: next(responses)

    def test_standalone_fetch_persists_without_impact_or_knowledge(self):
        result = cp.collect(self.root, 'collection', fetcher=self.provider())
        self.assertTrue(result['ok'])
        self.assertEqual(result['observed'], 1)
        self.assertEqual(result['status'], 'complete')
        state = self.state()
        self.assertEqual(state['schema'], cp.SCHEMA)
        self.assertNotIn('config', state)
        self.assertNotIn('reviews', state)
        self.assertEqual(len(state['batches'][0]['responses']), 2)
        for path in ('projects/paper-impact', 'academic/raw', 'cross-domain/graph.db', 'temp'):
            self.assertFalse((self.root / path).exists())

    def test_bounded_pagination_is_explicitly_partial(self):
        result = cp.collect(self.root, 'collection', max_pages=1, fetcher=self.provider(complete=False))
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['complete'])
        self.assertEqual(result['next_cursor'], 'next')

    def test_failed_fetch_is_archived_and_returns_failure(self):
        result = cp.collect(self.root, 'collection', fetcher=lambda _: (_ for _ in ()).throw(ValueError('quota unavailable')))
        self.assertFalse(result['ok'])
        self.assertTrue(result['saved'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('quota unavailable', self.state()['batches'][0]['errors'])
        exported = cp.export_payload(self.state())
        self.assertEqual(exported['retrieval_status'], 'failed')
        cp.initialize(self.root, 'copy', self.target)
        self.assertFalse(cp.import_collection(self.root, 'copy', exported)['ok'])

    def test_complete_empty_result_differs_from_failed_result(self):
        responses = iter([{'id': 'https://openalex.org/W1', 'doi': '10.1234/target', 'display_name': 'Target paper'},
                          {'results': [], 'meta': {'count': 0, 'next_cursor': None}}])
        result = cp.collect(self.root, 'collection', fetcher=lambda _: next(responses))
        self.assertTrue(result['ok'])
        self.assertEqual(result['observed'], 0)
        self.assertTrue(result['complete'])
        self.assertEqual(cp.export_payload(self.state())['provenance'][0]['status'], 'complete')
        self.assertEqual(cp.export_payload(self.state())['retrieval_status'], 'partial')

    def test_update_dedup_and_new_only_listing(self):
        first = self.import_()
        first_seen = next(iter(self.state()['candidates'].values()))['first_seen_at']
        payload = deepcopy(self.payload)
        payload['records'].append({'doi': '10.1234/second', 'title': 'Second paper'})
        second = self.import_(payload)
        self.assertEqual(second['new'], 1)
        state = self.state()
        self.assertEqual(state['candidates'][first['new_candidate_ids'][0]]['first_seen_at'], first_seen)
        rows = cp.list_records(state, new_only=True)['records']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['record']['title'], 'Second paper')
        self.assertEqual(len(cp.list_records(state, batch_id=first['batch_id'])['records']), 1)
        page = cp.list_records(state, limit=1)
        self.assertEqual(page['total'], 2)
        self.assertTrue(page['truncated'])
        self.assertFalse(cp.list_records(state, limit=1, offset=1)['truncated'])

    def test_export_roundtrip_keeps_sources_and_per_record_times(self):
        self.import_()
        payload = deepcopy(self.payload)
        payload['retrieved_at'] = '2021-01-01T00:00:00Z'
        payload['records'] = [{'doi': '10.1234/new', 'title': 'New paper'}]
        self.import_(payload)
        export = cp.export_collection(self.root, 'collection')
        source = json.loads(Path(export['path']).read_text())
        self.assertEqual(source['schema'], cp.IMPORT_SCHEMA)
        self.assertFalse(source['complete'])
        self.assertEqual(len(source['provenance']), 2)
        self.assertEqual(source['source_collection']['sha256'], cp.fingerprint(self.state()))
        cp.initialize(self.root, 'copy', self.target)
        cp.import_collection(self.root, 'copy', source)
        copied = self.state('copy')
        dates = {c['record']['doi']: c['last_seen_at'] for c in copied['candidates'].values()}
        self.assertEqual(dates['10.1234/citing'], '2020-01-01T00:00:00+00:00')
        self.assertEqual(dates['10.1234/new'], '2021-01-01T00:00:00+00:00')
        self.assertEqual(copied['batches'][0]['origin_observations'], source['observations'])
        self.assertEqual(copied['batches'][0]['provenance'], source['provenance'])
        again = cp.export_payload(copied)
        self.assertTrue(all(p in again['provenance'] for p in source['provenance']))

    def test_deterministic_export_does_not_fetch_or_mutate_state(self):
        self.import_(); before = self.state()
        with patch.object(cp, 'fetch', side_effect=AssertionError('must not query')):
            a = cp.export_collection(self.root, 'collection')
            b = cp.export_collection(self.root, 'collection')
        self.assertEqual(a['sha256'], b['sha256'])
        self.assertEqual(self.state(), before)

    def test_impact_consumes_generic_export_offline(self):
        import paper_impact as pi
        self.import_()
        exported = cp.export_collection(self.root, 'collection')
        pi.initialize(self.root, 'award', self.target['doi'], self.target['title'])
        with patch.object(cp, 'fetch', side_effect=AssertionError('no redundant network')):
            result = pi.execute(pi.parser().parse_args(['import', 'award', '--file', exported['path']]), self.root)
        self.assertEqual(result['new'], 1)
        state = pi.load(self.root, 'award')
        self.assertEqual(state['batches'][0]['provenance'][0]['provider'], 'synthetic export')
        self.assertFalse(state['reviews'])
        self.assertFalse(state['config']['authors'])
        self.assertFalse((self.root / 'academic/raw').exists())

    def test_legacy_import_and_module_are_compatible(self):
        import paper_impact_discovery as legacy
        self.assertIs(legacy.discover, cp.discover)
        self.assertIs(legacy.imported, cp.imported)
        payload = deepcopy(self.payload); payload['schema'] = 'paper-impact-import-v1'
        self.assertEqual(self.import_(payload)['new'], 1)
        self.assertEqual(self.import_()['new'], 0)

    def test_shared_merge_used_by_impact(self):
        import paper_impact as pi
        pi.initialize(self.root, 'award', self.target['doi'])
        with patch.object(cp, 'merge_batch', wraps=cp.merge_batch) as merge:
            pi.add_batch(self.root, pi.load(self.root, 'award'), cp.imported(self.payload, self.target))
        merge.assert_called_once()

    def test_bad_target_and_unknown_collection_do_not_write(self):
        with self.assertRaisesRegex(ValueError, 'invalid target DOI'):
            cp.initialize(self.root, 'invalid', {'doi': 'not-a-doi'})
        self.assertFalse((self.root / 'projects/citing-papers/invalid').exists())
        with self.assertRaisesRegex(ValueError, 'does not exist'):
            cp.collect(self.root, 'missing', fetcher=self.provider())
        self.assertFalse((self.root / 'projects/citing-papers/missing').exists())

    def test_doi_url_is_normalized_for_python_and_cli(self):
        target = cp.normalized_target({'doi': 'https://doi.org/10.1234/TARGET'})
        self.assertEqual(target['doi'], self.target['doi'])
        result = cp.discover(target, fetcher=self.provider())
        self.assertEqual(result['status'], 'complete')

    def test_wrong_target_import_retains_previous_collection(self):
        self.import_(); before = self.state()
        payload = deepcopy(self.payload); payload['target']['doi'] = '10.1234/other'
        with self.assertRaisesRegex(ValueError, 'another DOI'):
            self.import_(payload)
        self.assertEqual(self.state(), before)

    def test_invalid_observation_and_false_completeness_rejected(self):
        self.import_(); payload = cp.export_payload(self.state())
        payload['observations'][0]['aliases'] = ['doi:10.1234/other']
        with self.assertRaisesRegex(ValueError, 'one exported paper'):
            cp.imported(payload, self.target)
        payload = cp.export_payload(self.state()); payload['complete'] = True
        with self.assertRaisesRegex(ValueError, 'contradicts'):
            cp.imported(payload, self.target)

    def test_all_failed_and_unknown_empty_export_not_claimed_complete(self):
        with self.assertRaisesRegex(ValueError, 'nothing to export'):
            cp.export_collection(self.root, 'collection')
        payload = deepcopy(self.payload); payload.update(complete=False, records=[])
        self.import_(payload)
        exported = cp.export_payload(self.state())
        self.assertEqual(exported['retrieval_status'], 'partial')
        self.assertFalse(exported['complete'])

    def test_private_input_and_symlink_output_are_rejected(self):
        private = self.root / 'private'; private.mkdir()
        source = private / 'citations.json'; source.write_text(json.dumps(self.payload))
        args = cp.parser().parse_args(['import', 'collection', '--file', str(source)])
        with self.assertRaisesRegex(ValueError, 'private'):
            cp.execute(args, self.root)
        outside = self.root / 'outside'; outside.mkdir()
        (self.root / 'projects/citing-papers/escape').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            cp.initialize(self.root, 'escape', self.target)
        self.assertFalse(list(outside.iterdir()))

    def test_concurrent_write_and_atomic_failure_preserve_state(self):
        with cp.collection_lock(self.root, 'collection'):
            with self.assertRaisesRegex(ValueError, 'busy'):
                self.import_()
        before = self.state()
        with patch.object(cp.si, 'atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.import_()
        self.assertEqual(self.state(), before)

    def test_wg_failure_and_args(self):
        import wg
        args = wg.build_parser().parse_args(['citing-papers', 'fetch', 'collection', '--max-pages', '1'])
        self.assertEqual(args.citing_args, ['fetch', 'collection', '--max-pages', '1'])
        response = {'ok': False, 'status': 'failed', 'saved': True, 'error': 'quota'}
        with patch.object(wg, 'run_script', return_value=(1, json.dumps(response), '')), redirect_stdout(io.StringIO()) as out:
            args.func(args)
        envelope = json.loads(out.getvalue())
        self.assertFalse(envelope['ok'])
        self.assertTrue(envelope['result']['saved'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

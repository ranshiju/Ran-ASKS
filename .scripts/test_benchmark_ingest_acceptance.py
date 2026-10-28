#!/usr/bin/env python3
"""Acceptance regressions: partial/failed work must never become a speedup."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import benchmark_ingest_acceptance as b


def good():
    hashes = {k: b.digest(k.encode()) for k in b.ARTIFACTS}
    context = {k: b.digest(k.encode()) for k in b.MATCH}
    hashes['input'] = b.digest(b'input')
    context.update(input_sha256=hashes['input'], checklist_sha256=hashes['checklist'], measurement_boundary=b.BOUNDARY)
    run = {'id': 'r1', 'case': 'sample', 'variant': 'baseline', 'scope': 'full_ingestion',
           'event_coverage': 'complete', 'context': context}
    data = {'pipeline': {'transaction_id': 't1', 'status': 'completed', 'quality_status': 'complete', 'cleanup_pending': False,
                           'verification_receipt': {'status': 'PASS', 'committed': True, 'transaction_id': 't1',
                                                    'artifacts': {'wiki_path': {'sha256': hashes['wiki']}}},
                           'frontier_capture': {'backend': 'api', 'answer_policy': 'api', 'count': 0, 'answer_attempts': []}},
            'maintenance': {'status': 'completed', 'publication': {'status': 'completed'},
                            'trigger_files': [{'transaction_id': 't1'}],
                            'components': {k: {'status': 'no_action'} for k in ('abbreviations', 'people', 'hubs')}},
            'audit': {'scientific_pass': True, 'transaction_id': 't1', 'review_end_epoch': 1099, 'review_mode': 'runtime',
                      'checks': {k: 'pass' for k in b.CHECKS}, 'artifact_sha256': hashes.copy()},
            'timing': {'schema': b.TIMING, 'boundary': b.BOUNDARY, 'transaction_id': 't1',
                       'wall_start': 1000, 'wall_end': 1100, 'elapsed_sec': 100,
                       'complete': True, 'deducted_sec': 0,
                       'includes': ['entry', 'extraction', 'generation', 'repairs', 'graph', 'frontier',
                                    'maintenance', 'publication', 'runtime_quality_checks', 'waiting']},
            'events': [{'event_kind': 'llm_api_call', 'event_id': 'e1', 'call_id': 'c1', 'transaction_id': 't1',
                        'attempt': 1, 'status': 'ok', 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}]}
    return run, data, hashes


class AcceptanceTest(unittest.TestCase):
    def test_qualified_requires_all_evidence(self):
        run, data, hashes = good()
        result = b.assess(run, data, hashes)
        self.assertTrue(result['qualified'], result['reasons'])
        self.assertEqual(result['qualified_elapsed_sec'], 100)

    def test_completed_does_not_override_scientific_failure(self):
        r, d, h = good(); d['audit']['scientific_pass'] = False
        result = b.assess(r, d, h)
        self.assertFalse(result['qualified']); self.assertIsNone(result['qualified_elapsed_sec'])
        self.assertEqual(result['observed_elapsed_sec'], 100)

    def test_maintenance_handoff_even_with_completed_envelope(self):
        r, d, h = good(); d['maintenance']['components']['hubs']['status'] = 'agent_required'
        self.assertIn('maintenance_component:hubs', b.assess(r, d, h)['reasons'])

    def test_publication_and_transaction_bindings_required(self):
        for role in ('maintenance', 'audit', 'timing'):
            r, d, h = good()
            if role == 'maintenance': d[role]['trigger_files'] = [{'transaction_id': 'unrelated'}]
            else: d[role]['transaction_id'] = 'unrelated'
            self.assertFalse(b.assess(r, d, h)['qualified'])
        r, d, h = good(); d['maintenance']['publication']['status'] = 'deferred'
        self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_quality_degradation_or_handoff_blocks(self):
        for key, value in [('quality_status', 'degraded'), ('agent_required', True), ('quality_warnings', ['missing_condition'])]:
            r, d, h = good(); d['pipeline'][key] = value
            self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_external_readonly_evaluation_does_not_add_researcher_scheduling_latency(self):
        r, d, h = good(); d['audit'].update(review_mode='external_read_only', artifacts_modified=False, review_end_epoch=9000)
        self.assertTrue(b.assess(r, d, h)['qualified'])
        d['audit']['artifacts_modified'] = True
        self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_old_audit_cannot_cover_changed_artifacts(self):
        for name in b.ARTIFACTS:
            r, d, h = good(); h[name] = 'new-hash'
            self.assertIn('audit_unbound:' + name, b.assess(r, d, h)['reasons'])

    def test_unknown_or_incomplete_checks_block(self):
        for check in b.CHECKS:
            r, d, h = good(); del d['audit']['checks'][check]
            self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_legacy_timer_not_upgraded(self):
        r, d, h = good(); d['timing'].pop('schema')
        self.assertIn('measurement_boundary_incomplete', b.assess(r, d, h)['reasons'])

    def test_review_after_timer_and_time_deductions_block(self):
        r, d, h = good(); d['audit']['review_end_epoch'] = 1101
        self.assertFalse(b.assess(r, d, h)['qualified'])
        r, d, h = good(); d['timing']['deducted_sec'] = 10
        self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_clock_and_nonfinite_times_rejected(self):
        for elapsed in (True, -1, float('nan'), float('inf'), 0, 99 - 0.01):
            r, d, h = good(); d['timing']['elapsed_sec'] = elapsed
            self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_nested_spans_do_not_replace_wall_time(self):
        r, d, h = good(); d['timing']['spans'] = [{'duration_ms': 900000}] * 3
        self.assertEqual(b.assess(r, d, h)['qualified_elapsed_sec'], 100)

    def test_partial_generation_cannot_be_full_ingestion(self):
        r, d, h = good(); r['scope'] = 'generation_only'
        self.assertFalse(b.assess(r, d, h)['qualified'])

    def test_failures_missing_usage_retries_retained(self):
        _, d, _ = good(); e = d['events']
        e.append(dict(e[0], event_id='e2', attempt=2, status='error', usage=None))
        e.append(dict(e[0], event_id='e3', attempt=3))
        ledger = b.request_ledger(e)
        self.assertEqual((ledger['attempt_count'], ledger['failed_attempts'], ledger['retry_attempts']), (3, 1, 2))
        self.assertEqual(ledger['known_tokens']['total_tokens'], 30)
        self.assertEqual(ledger['missing_usage_attempts'], 1)
        self.assertIsNone(ledger['currency_cost'])

    def test_duplicate_events_not_double_charged_and_conflicts_fail(self):
        _, d, _ = good(); e = d['events']
        self.assertEqual(b.request_ledger(e + copy.deepcopy(e))['attempt_count'], 1)
        other = dict(e[0], status='error')
        self.assertIn('conflicting_duplicate_event', b.request_ledger(e + [other])['errors'])

    def test_unrelated_transaction_and_missing_event_log_block(self):
        r, d, h = good(); d['events'][0]['transaction_id'] = 'elsewhere'
        self.assertFalse(b.assess(r, d, h)['qualified'])
        d['events'] = []
        self.assertIn('request_log_missing', b.assess(r, d, h)['reasons'])

    def test_paired_threshold_inclusive(self):
        r, d, h = good(); base = b.assess(r, d, h)
        candidate = copy.deepcopy(base); candidate.update(id='r2', variant='candidate', qualified_elapsed_sec=70)
        self.assertTrue(b.compare([base, candidate], ['sample'])['target_accepted_for_declared_cases'])
        candidate['qualified_elapsed_sec'] = 70.01
        self.assertFalse(b.compare([base, candidate], ['sample'])['target_accepted_for_declared_cases'])

    def test_unmatched_inputs_or_configuration_no_ratio(self):
        r, d, h = good(); base = b.assess(r, d, h)
        for key in b.MATCH:
            candidate = copy.deepcopy(base); candidate.update(id='r2', variant='candidate')
            candidate['context'][key] = 'different'
            self.assertEqual(b.compare([base, candidate], ['sample'])['pairs'], [])

    def test_failed_fast_candidate_and_missing_cases_not_cherry_picked(self):
        r, d, h = good(); base = b.assess(r, d, h)
        c = copy.deepcopy(base); c.update(id='r2', variant='candidate', qualified=False, qualified_elapsed_sec=None, observed_elapsed_sec=1)
        out = b.compare([base, c], ['sample', 'missing'])
        self.assertFalse(out['target_accepted_for_declared_cases']); self.assertEqual(out['pairs'], [])
        self.assertIn('missing:missing_or_multiple_pair', out['reasons'])

    def test_hashes_and_path_escape_checked_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); p = root/'data.json'; p.write_text('{"status":"completed"}')
            _, _, errs = b.read_evidence(root, {'pipeline': {'path': p.name, 'sha256': 'wrong'}})
            self.assertEqual(errs, ['hash_mismatch:pipeline'])
            for path in ('../data.json', str(p)):
                _, _, errs = b.read_evidence(root, {'pipeline': {'path': path, 'sha256': b.digest(p.read_bytes())}})
                self.assertEqual(errs, ['invalid_evidence:pipeline'])
            link = root/'link.json'; link.symlink_to(p)
            _, _, errs = b.read_evidence(root, {'pipeline': {'path': link.name, 'sha256': b.digest(p.read_bytes())}})
            self.assertEqual(errs, ['invalid_evidence:pipeline'])

    def test_frontier_failure_and_missing_commit_receipt_block(self):
        r, d, h = good(); d['pipeline']['frontier_capture']['count'] = 1
        self.assertIn('frontier_not_completed', b.assess(r, d, h)['reasons'])
        d['pipeline']['frontier_capture']['answer_attempts'] = [{'status': 'failed'}]
        self.assertIn('frontier_not_completed', b.assess(r, d, h)['reasons'])
        d['pipeline'].pop('verification_receipt')
        self.assertIn('commit_receipt_not_passed', b.assess(r, d, h)['reasons'])

    def test_unknown_attempt_identity_not_silently_deduplicated(self):
        event = {'event_kind': 'llm_api_call', 'status': 'error'}
        ledger = b.request_ledger([event, event.copy()])
        self.assertEqual(ledger['attempt_count'], 2)
        self.assertIn('missing_attempt_identity', ledger['errors'])

    def test_same_provider_attempt_cannot_be_reused_as_an_independent_pair(self):
        r, d, h = good(); r['files'] = {}
        c = copy.deepcopy(r); c.update(id='r2', variant='candidate')
        manifest = {'schema': b.SCHEMA, 'expected_cases': ['sample'], 'runs': [r, c]}
        with patch.object(b, 'read_evidence', return_value=(d, h, [])):
            report = b.evaluate(manifest, Path('.'))
        self.assertEqual(report['qualified_count'], 0)
        self.assertFalse(report['aggregate_cost_verified'])
        self.assertFalse(report['comparison']['target_accepted_for_declared_cases'])
        self.assertTrue(all('attempt_reused_across_runs' in row['reasons'] for row in report['runs']))

    def test_manifest_required_cases_and_duplicates(self):
        with self.assertRaises(ValueError): b.evaluate({'schema': b.SCHEMA, 'expected_cases': []}, Path('.'))
        manifest = {'schema': b.SCHEMA, 'expected_cases': ['sample'], 'runs': []}
        result = b.evaluate(manifest, Path('.'))
        self.assertFalse(result['comparison']['target_accepted_for_declared_cases'])


if __name__ == '__main__': unittest.main()

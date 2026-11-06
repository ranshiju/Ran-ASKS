#!/usr/bin/env python3
"""Offline isolated discovery, ranking, attribution and publication regressions."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import paper_impact as pi
import paper_impact_discovery as pd
import source_ingest as si
import source_locator as sl


class PaperImpactTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(sl, 'REPO', self.root))
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline tests')))
        pi.initialize(self.root, 'sample', '10.1234/target', 'Target Contribution')
        self.record = {'doi': '10.1234/citing', 'title': 'Citing Study', 'provider_id': 'https://openalex.org/W2',
                       'authors': [{'name': 'Expert A', 'id': 'https://openalex.org/A1', 'position': 'last'}],
                       'journal': {'name': 'Top Journal', 'issns': ['1234-5678']}, 'type': 'review'}
        self.payload = {'schema': 'paper-impact-import-v1', 'target': {'doi': '10.1234/target'},
                        'provider': 'synthetic export', 'retrieved_at': '2020-01-01T00:00:00Z',
                        'scope': 'synthetic fixture only', 'complete': True, 'records': [self.record]}

    def state(self):
        return pi.load(self.root, 'sample')

    def ingest_list(self, payload=None):
        state = self.state()
        return pi.add_batch(self.root, state, pd.imported(payload or self.payload, state['target']))

    def paper(self, text=None, folder='citing'):
        path = self.root / f'academic/raw/works/papers/{folder}/paper.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or '# Citing Study\nDOI: 10.1234/citing\n## Results\nThe method [12] provides an effective solution.\n## References\n[12] Target Contribution. DOI: 10.1234/target\n')
        return path

    def configure(self):
        state = self.state()
        config = {'contributions': [{'id': 'K1', 'text': 'A useful method', 'status': 'proposed'}],
                  'authors': [{'name': 'Expert A', 'id': 'https://openalex.org/A1', 'level': 3,
                               'basis': 'user_preference', 'reason': 'Priority researcher', 'as_of': '2026-11-05'}],
                  'journals': [{'name': 'Top Journal', 'issn': '1234-5678', 'level': 2,
                                'basis': 'user_preference', 'reason': 'Priority venue', 'as_of': '2026-11-05'}],
                  'weights': {'author': 2, 'journal': 2, 'type': 1}}
        state['config'] = pi.validate_config(self.root, config)
        pi.save(self.root, state)

    def setup_evidence(self):
        self.ingest_list()
        self.paper()
        self.configure()
        state = self.state()
        cid = next(iter(state['candidates']))
        state['selection'] = [cid]
        pi.save(self.root, state)
        task = pi.prepare(self.root, state, 'evidence')
        output = self.analysis(cid)
        path = pi.task_dir(self.root, task['task_id']) / 'analysis.json'
        si.atomic_json(path, output)
        return cid, task['task_id'], output, path

    def analysis(self, cid):
        base = 'academic/raw/works/papers/citing/paper.md#'
        return {'reviews': [{'candidate_id': cid, 'coverage': 'complete', 'coverage_note': 'All six synthetic lines read',
                'identity': {'locator': base+'L1-L2', 'quote': 'Citing Study'},
                'reference': {'locator': base+'L6-L6', 'quote': '[12] Target Contribution. DOI: 10.1234/target'},
                'reference_marker': '12', 'independence': 'unknown', 'independence_reason': 'No collaboration data',
                'occurrences': [{'kind': 'positive', 'attribution': 'target', 'statement_mode': 'synthesis',
                    'evidence': {'locator': base+'L4-L4', 'quote': 'The method [12] provides an effective solution.'},
                    'citation_marker': '[12]', 'reference_mapping_reason': '12 maps to target DOI',
                    'translation': '该方法提供有效方案。', 'analysis': 'Explicit effectiveness assessment',
                    'contribution_ids': ['K1'], 'supported_statement': 'The authors recognized effectiveness.',
                    'limitations': [], 'unsupported_claims': ['Not a claim of world leadership']}]}]}

    def reject_analysis(self, mutate, expected):
        _, task, output, path = self.setup_evidence()
        mutate(output['reviews'][0])
        si.atomic_json(path, output)
        with self.assertRaisesRegex(ValueError, expected):
            pi.checked(self.root, self.state(), task)

    def test_import_dedup_and_incremental_history(self):
        first = self.ingest_list()
        state = self.state()
        cid = first['new_candidate_ids'][0]
        initial = state['candidates'][cid]['first_seen_at']
        other = deepcopy(self.payload)
        other['retrieved_at'] = '2021-01-01T00:00:00Z'
        other['records'].append(deepcopy(self.record))
        second = self.ingest_list(other)
        self.assertEqual(second['new'], 0)
        state = self.state()
        self.assertEqual(len(state['candidates']), 1)
        self.assertEqual(len(state['batches']), 2)
        self.assertEqual(state['candidates'][cid]['first_seen_at'], initial)
        self.assertEqual(state['candidates'][cid]['last_seen_at'], '2021-01-01T00:00:00+00:00')
        self.assertEqual(len(state['candidates'][cid]['batch_ids']), 2)
        self.assertFalse((self.root / 'academic/raw').exists())

    def test_conflicting_provider_doi_does_not_publish(self):
        self.ingest_list()
        previous = self.state()
        changed = deepcopy(self.payload)
        changed['records'][0]['doi'] = '10.1234/another'
        with self.assertRaisesRegex(ValueError, 'conflicting DOIs'):
            self.ingest_list(changed)
        self.assertEqual(self.state(), previous)

    def test_older_import_keeps_newer_metadata_and_normalizes_timezone(self):
        latest = deepcopy(self.payload)
        latest['retrieved_at'] = '2021-01-01T08:00:00+08:00'
        latest['records'][0]['title'] = 'Newer Metadata Title'
        self.ingest_list(latest)
        self.ingest_list()
        candidate = next(iter(self.state()['candidates'].values()))
        self.assertEqual(candidate['record']['title'], 'Newer Metadata Title')
        self.assertEqual(candidate['last_seen_at'], '2021-01-01T00:00:00+00:00')
        self.assertEqual(len(candidate['batch_ids']), 2)

    def test_different_version_dois_not_merged_by_title(self):
        changed = deepcopy(self.payload)
        other = deepcopy(self.record)
        other.update(doi='10.1234/preprint', provider_id='https://openalex.org/W3')
        changed['records'].append(other)
        self.assertEqual(self.ingest_list(changed)['new'], 2)

    def test_import_wrong_target_and_missing_provenance(self):
        for key, value in [('target', {'doi': '10.1234/other'}), ('scope', ''), ('complete', 'yes'), ('retrieved_at', '2099-01-01T00:00:00Z')]:
            changed = deepcopy(self.payload); changed[key] = value
            with self.assertRaises(ValueError):
                pd.imported(changed, self.state()['target'])

    def test_provider_pagination_complete_and_bounded(self):
        target = {'id': 'https://openalex.org/W1', 'doi': 'https://doi.org/10.1234/target', 'display_name': 'Target Contribution'}
        cited = {'id': 'https://openalex.org/W2', 'doi': 'https://doi.org/10.1234/citing', 'display_name': 'Citing Study', 'referenced_works': ['https://openalex.org/W1']}
        responses = [target, {'results': [cited], 'meta': {'count': 1, 'next_cursor': 'next'}}, {'results': [], 'meta': {'count': 1, 'next_cursor': None}}]
        queries = []
        def fetch(url):
            queries.append(url)
            return responses[len(queries)-1]
        result = pd.discover(self.state()['target'], max_pages=1, fetcher=fetch)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(queries), 2)
        queries.clear()
        result = pd.discover(self.state()['target'], max_pages=2, fetcher=fetch)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(len(result['responses']), 3)

    def test_provider_failure_is_saved_not_empty_success(self):
        result = pd.discover(self.state()['target'], fetcher=lambda _: (_ for _ in ()).throw(ValueError('quota unavailable')))
        self.assertEqual(result['status'], 'failed')
        pi.add_batch(self.root, self.state(), result)
        self.assertEqual(self.state()['batches'][0]['errors'], ['quota unavailable'])
        self.assertFalse(self.state()['candidates'])

    def test_provider_wrong_target_reference_rejected(self):
        responses = iter([{'id': 'https://openalex.org/W1', 'doi': '10.1234/target', 'display_name': 'Target Contribution'},
                          {'results': [{'id': 'https://openalex.org/W2', 'display_name': 'Other', 'referenced_works': []}], 'meta': {'next_cursor': None}}])
        result = pd.discover(self.state()['target'], fetcher=lambda _: next(responses))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('does not reference', result['errors'][0])

    def test_title_ambiguity_not_guessed(self):
        items = [{'id': f'https://openalex.org/W{i}', 'display_name': 'Same Title'} for i in (1, 2)]
        result = pd.discover({'title': 'Same Title'}, fetcher=lambda _: {'results': items})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['target_candidates']), 2)

    def test_fetch_fixed_host_and_no_secret_in_error(self):
        with self.assertRaisesRegex(ValueError, 'unapproved'):
            pd.fetch('https://example.com/works')
        with patch.dict('os.environ', {'OPENALEX_API_KEY': 'secret-value'}), patch.object(pd.urllib.request, 'build_opener', side_effect=ValueError('secret-value')):
            with self.assertRaises(ValueError) as error:
                pd.fetch('https://api.openalex.org/works')
        self.assertNotIn('secret-value', str(error.exception))

    def test_two_channels_and_namesake_disambiguation(self):
        self.ingest_list(); self.configure()
        state = self.state()
        rows = pi.ranking(self.root, state)['rows']
        self.assertEqual(rows[0]['channels'], ['author', 'journal'])
        self.assertEqual(rows[0]['score'], 11)
        candidate = next(iter(state['candidates'].values()))
        candidate['record']['authors'][0]['id'] = 'https://openalex.org/A99'
        row = pi.ranking(self.root, state)['rows'][0]
        self.assertEqual(row['components']['author'], 0)
        self.assertIn('同名', row['uncertainties'][0])

    def test_parent_and_child_journal_names_do_not_match(self):
        self.ingest_list(); self.configure()
        state = self.state()
        state['config']['journals'][0].pop('issn')
        next(iter(state['candidates'].values()))['record']['journal']['name'] = 'Top Journal Communications'
        self.assertEqual(pi.ranking(self.root, state)['rows'][0]['components']['journal'], 0)

    def test_name_preference_not_promoted_to_verified_identity(self):
        self.ingest_list(); self.configure()
        state = self.state(); state['config']['authors'][0].pop('id')
        row = pi.ranking(self.root, state)['rows'][0]
        self.assertEqual(row['reasons'][0]['identity'], 'name_preference_unverified')
        self.assertEqual(row['independence'], 'unknown')

    def test_reference_doi_does_not_identify_this_paper(self):
        self.ingest_list()
        self.paper('# Different Paper\n## Results\nSome text\n## References\nCiting Study, DOI: 10.1234/citing\n')
        self.assertEqual(pi.ranking(self.root, self.state())['rows'][0]['kb']['status'], 'not_found')

    def test_matching_title_does_not_override_conflicting_doi(self):
        self.ingest_list()
        self.paper('# Citing Study\nDOI: 10.1234/preprint\n## References\nTarget Contribution\n')
        self.assertEqual(pi.ranking(self.root, self.state())['rows'][0]['kb']['status'], 'not_found')

    def test_abstract_is_partial_not_fulltext(self):
        self.ingest_list(); self.paper('# Citing Study\nDOI: 10.1234/citing\n## Abstract\nA summary only.\n')
        state = self.state(); cid = next(iter(state['candidates']))
        self.assertEqual(pi.ranking(self.root, state)['rows'][0]['kb']['status'], 'partial_text')
        with self.assertRaisesRegex(ValueError, 'partial_text'):
            pi.prepare(self.root, state, 'evidence', [cid])

    def test_multiple_editions_are_ambiguous(self):
        self.ingest_list(); self.paper(); self.paper(folder='second')
        self.assertEqual(pi.ranking(self.root, self.state())['rows'][0]['kb']['status'], 'ambiguous')

    def test_source_first_fulltext_and_tamper_guard(self):
        self.ingest_list()
        input_path = self.root / 'source.md'
        input_path.write_text('# Citing Study\nDOI: 10.1234/citing\n## References\n[12] Target Contribution\n')
        result = si.ingest(input_path, repo=self.root)
        row = pi.ranking(self.root, self.state())['rows'][0]
        self.assertEqual(row['kb']['status'], 'fulltext_candidate')
        receipt = si.load_receipt(self.root, result['source_id'])
        (self.root / si.selected_parse(receipt)['text_path']).write_text('changed')
        result = pi.ranking(self.root, self.state())
        self.assertEqual(result['rows'][0]['kb']['status'], 'not_found')
        self.assertTrue(result['catalog_issues'])

    def test_complete_workflow_and_idempotent_commit(self):
        cid, task, output, path = self.setup_evidence()
        pi.checked(self.root, self.state(), task)
        result = pi.commit(self.root, self.state(), task)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(pi.commit(self.root, self.state(), task)['idempotent'])
        result = pi.report(self.root, self.state())
        self.assertEqual(result['usable_evidence'], 1)
        content = Path(result['report']).read_text()
        self.assertIn('E1', content)
        self.assertIn('Not a claim of world leadership', content)
        data = json.loads(Path(result['data']).read_text())
        self.assertEqual(data['contributions'][0]['evidence_numbers'], [1])
        self.assertFalse((self.root / 'cross-domain/graph.db').exists())

    def test_wrong_reference_target_rejected(self):
        self.reject_analysis(lambda r: r['reference'].update(quote='[12]'), 'reference must identify target')

    def presentation(self):
        return {'grant_text': 'Expert A and colleagues recognized the method in Citing Study.',
                'ppt_text': 'Citing Study: effectiveness recognized.',
                'occurrence_indices': [0], 'context_facts': []}

    def people_config(self):
        self.ingest_list()
        self.configure()
        raw = self.paper('Expert A, Example University.\nAward A was granted in 2020.\nAcademy A elected Expert A.\nSociety A named Expert A a Fellow.\nExpert A is Professor of Physics.\n', folder='profile')
        base = raw.relative_to(self.root).as_posix()
        lines = raw.read_text().splitlines()
        credentials = []
        for index, kind in enumerate(('affiliation', 'award', 'academy', 'fellowship', 'appointment')):
            credentials.append({'kind': kind, 'name': kind+' title', 'organization': 'Example organization',
                'period': 'as of 2026', 'as_of': '2026-11-06', 'basis_reason': 'Synthetic direct statement',
                'source_url': 'https://example.org/profile',
                'evidence': {'locator': base+f'#L{index+1}-L{index+1}', 'quote': lines[index]}})
        data = {'title': 'Expert A', 'type': 'people', 'sources': [base],
                'impact_profile': {'schema': 'paper-impact-person-v1', 'identifiers': ['https://openalex.org/A1'],
                    'identity_evidence': {'locator': base+'#L1-L1', 'quote': lines[0]}, 'credentials': credentials}}
        page = self.root/'academic/wiki/authors/expert-a.md'
        page.parent.mkdir(parents=True)
        page.write_text('---\n'+pi.yaml.safe_dump(data)+'---\n\n# Expert A\n')
        config = deepcopy(self.state()['config'])
        config['authors'][0].update(basis='source_bound', profile_page=page.relative_to(self.root).as_posix())
        cid = next(iter(self.state()['candidates']))
        config['screening'] = [{'candidate_id': cid, 'title_relevance': 'direct',
             'title_reason': 'Title addresses the target method', 'authority_reason': 'Verified person profile',
             'recommendation': 'read', 'decision_reason': 'Relevant title and documented credentials'}]
        return config, page, raw

    def test_people_profile_reuse_and_screening_do_not_select(self):
        config, page, raw = self.people_config()
        state = self.state()
        state['config'] = pi.validate_config(self.root, config, candidates=state['candidates'])
        pi.save(self.root, state)
        result = pi.report(self.root, self.state())
        data = json.loads(Path(result['data']).read_text())
        row = data['reading']['rows'][0]
        self.assertEqual(row['screening']['status'], 'current')
        self.assertFalse(row['selected'])
        person = row['reasons'][0]['person_profile']
        self.assertEqual({x['kind'] for x in person['credentials']}, pi.CREDENTIAL_KINDS)
        self.assertIn('学会 Fellow', Path(result['report']).read_text())
        self.assertEqual(data['application_summary'], [])

    def test_people_profile_identity_requires_stable_match(self):
        config, _, _ = self.people_config()
        for identifiers in ({'id': 'https://openalex.org/A99'}, {'id': '', 'orcid': ''}):
            bad = deepcopy(config)
            bad['authors'][0].update(identifiers)
            with self.assertRaisesRegex(ValueError, 'identity must match'):
                pi.validate_config(self.root, bad, candidates=self.state()['candidates'])

    def test_people_page_and_raw_drift_disable_credentials(self):
        config, page, raw = self.people_config()
        state = self.state()
        state['config'] = pi.validate_config(self.root, config, candidates=state['candidates'])
        original = page.read_text()
        page.write_text(original+'Changed page\n')
        row = pi.ranking(self.root, state, entries=[])['rows'][0]
        self.assertEqual(row['components']['author'], 0)
        self.assertEqual(row['screening']['status'], 'stale')
        self.assertFalse(any('person_profile' in r for r in row['reasons']))
        page.write_text(original)
        raw.write_text(raw.read_text()+'Changed source\n')
        self.assertFalse(pi.profile_current(self.root, state['config']['authors'][0]))

    def test_people_profile_requires_declared_raw_and_credential_kind(self):
        config, page, _ = self.people_config()
        original = page.read_text()
        for text, error in [(original.replace('kind: award', 'kind: famous'), 'credential kind'),
                            (original.replace('sources:', 'old_sources:'), 'people sources'),
                            (original.replace('type: people', 'type: paper-summary'), 'current people page')]:
            page.write_text(text)
            with self.assertRaisesRegex(ValueError, error):
                pi.validate_config(self.root, deepcopy(config), candidates=self.state()['candidates'])

    def test_people_profile_path_cannot_escape_authors(self):
        config, _, _ = self.people_config()
        for path in ('private/profile.md', 'academic/wiki/authors/../../../private/profile.md'):
            config['authors'][0]['profile_page'] = path
            with self.assertRaisesRegex(ValueError, 'academic people page'):
                pi.validate_config(self.root, deepcopy(config), candidates=self.state()['candidates'])

    def test_screening_metadata_drift_and_invalid_candidates(self):
        config, _, _ = self.people_config()
        state = self.state()
        state['config'] = pi.validate_config(self.root, deepcopy(config), candidates=state['candidates'])
        cid = next(iter(state['candidates']))
        state['candidates'][cid]['record']['title'] = 'Changed title'
        self.assertEqual(pi.ranking(self.root, state, entries=[])['rows'][0]['screening']['status'], 'stale')
        config['screening'][0]['candidate_id'] = 'missing'
        with self.assertRaisesRegex(ValueError, 'screening candidate'):
            pi.validate_config(self.root, config, candidates=state['candidates'])

    def test_presentation_opens_report_and_groups_same_paper(self):
        cid, task, output, path = self.setup_evidence()
        review = output['reviews'][0]
        review['occurrences'].append(deepcopy(review['occurrences'][0]))
        review['occurrences'][1]['kind'] = 'development'
        review['presentation'] = self.presentation()
        review['presentation']['occurrence_indices'] = [0, 1]
        si.atomic_json(path, output)
        pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        data = json.loads(Path(result['data']).read_text())
        self.assertEqual(len(data['application_summary']), 1)
        summary = data['application_summary'][0]
        self.assertEqual(summary['evidence_numbers'], [1, 2])
        self.assertEqual(summary['status'], 'ready')
        content = Path(result['report']).read_text()
        self.assertLess(content.index(review['presentation']['grant_text']), content.index('## 检索范围'))
        self.assertLess(content.index(review['presentation']['ppt_text']), content.index('## 检索范围'))
        self.assertIn('同一篇施引论文', content)

    def test_legacy_copy_is_retained_without_invented_credentials(self):
        cid, task, output, path = self.setup_evidence()
        pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        summary = json.loads(Path(result['data']).read_text())['application_summary'][0]
        self.assertEqual(summary['grant_text'], output['reviews'][0]['occurrences'][0]['supported_statement'])
        self.assertEqual(summary['context_facts'], [])
        self.assertEqual(summary['ppt_text'], '')
        self.assertEqual(summary['status'], 'existing_statements')

    def test_presentation_rejects_noneligible_evidence(self):
        cid, task, output, path = self.setup_evidence()
        original = deepcopy(output)
        for change in ('background', 'critical', 'partial', 'uncertain'):
            with self.subTest(change=change):
                output = deepcopy(original)
                review = output['reviews'][0]
                review['presentation'] = self.presentation()
                review['occurrences'][0]['limitations'] = ['A necessary scope limit']
                if change == 'partial':
                    review['coverage'] = change
                elif change == 'uncertain':
                    review['occurrences'][0]['attribution'] = change
                else:
                    review['occurrences'][0]['kind'] = change
                si.atomic_json(path, output)
                with self.assertRaisesRegex(ValueError, 'presentation requires complete'):
                    pi.checked(self.root, self.state(), task)

    def test_presentation_indices_are_stable_and_valid(self):
        cid, task, output, path = self.setup_evidence()
        review = output['reviews'][0]
        review['presentation'] = self.presentation()
        for indices in ([], [True], [-1], [1], [0, 0], ['E1']):
            with self.subTest(indices=indices):
                review['presentation']['occurrence_indices'] = indices
                si.atomic_json(path, output)
                with self.assertRaisesRegex(ValueError, 'presentation.*indices'):
                    pi.checked(self.root, self.state(), task)

    def test_context_facts_bind_author_source_and_invalidate_on_drift(self):
        cid, task, output, path = self.setup_evidence()
        source = self.paper('Expert A was affiliated with Example University in 2020.\n', folder='profile')
        fact = {'kind': 'affiliation', 'text': 'Expert A, Example University (2020)',
                'author_id': 'https://openalex.org/A1', 'as_of': '2020-01-01',
                'basis_reason': 'Profile names the same author; historical affiliation only.',
                'evidence': {'locator': source.relative_to(self.root).as_posix()+'#L1-L1',
                             'quote': source.read_text().strip()}}
        output['reviews'][0]['presentation'] = self.presentation()
        output['reviews'][0]['presentation']['context_facts'] = [fact]
        for mutation, error in [({'author_id': 'https://openalex.org/A99'}, 'author must match'),
                                ({'as_of': '2020'}, 'date must be'),
                                ({'evidence': {'locator': 'temp/profile.md#L1-L1', 'quote': 'X'}}, 'academic Raw')]:
            bad = deepcopy(output)
            bad['reviews'][0]['presentation']['context_facts'][0].update(mutation)
            si.atomic_json(path, bad)
            with self.assertRaisesRegex(ValueError, error):
                pi.checked(self.root, self.state(), task)
        si.atomic_json(path, output)
        pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        self.assertIn(fact['evidence']['locator'], Path(result['report']).read_text())
        source.write_text(source.read_text()+'Updated profile.\n')
        result = pi.report(self.root, self.state())
        data = json.loads(Path(result['data']).read_text())
        self.assertEqual(data['application_summary'], [])
        self.assertEqual(result['usable_evidence'], 0)
        self.assertEqual(len(result['invalid_reviews']), 1)

    def test_group_presentation_preserves_scope_and_evidence_link(self):
        cid, task, output, path = self.setup_evidence()
        review = output['reviews'][0]
        other = deepcopy(review['occurrences'][0])
        other.update(attribution='group', limitations=['This assesses several studies jointly.'])
        review['occurrences'].append(other)
        review['presentation'] = self.presentation()
        review['presentation']['occurrence_indices'] = [1]
        si.atomic_json(path, output)
        pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        summary = json.loads(Path(result['data']).read_text())['application_summary'][0]
        self.assertEqual(summary['evidence_numbers'], [2])
        self.assertEqual(summary['limitations'], other['limitations'])

    def test_quote_must_be_verbatim(self):
        self.reject_analysis(lambda r: r['occurrences'][0]['evidence'].update(quote='A world-leading result'), 'not present verbatim')

    def test_citation_marker_must_map_to_target(self):
        cid, task, output, path = self.setup_evidence()
        paper = self.paper()
        paper.write_text(paper.read_text().replace('[12] provides', '[13] provides'))
        task = pi.prepare(self.root, self.state(), 'evidence')['task_id']
        output['reviews'][0]['occurrences'][0]['evidence']['quote'] = 'The method [13] provides an effective solution.'
        output['reviews'][0]['occurrences'][0]['citation_marker'] = '[13]'
        si.atomic_json(pi.task_dir(self.root, task) / 'analysis.json', output)
        with self.assertRaisesRegex(ValueError, 'does not include target'):
            pi.checked(self.root, self.state(), task)

    def test_numeric_ranges(self):
        self.assertTrue(pi.marker_matches('12', '[10–15]'))
        self.assertTrue(pi.marker_matches('12', '[9,12,18]'))
        self.assertFalse(pi.marker_matches('12', '[112]'))
        self.assertFalse(pi.marker_matches('12', '[15-10]'))

    def test_group_requires_limitation(self):
        self.reject_analysis(lambda r: r['occurrences'][0].update(attribution='group'), 'limitations must not be empty')

    def test_independence_requires_raw_basis(self):
        self.reject_analysis(lambda r: r.update(independence='independent'), 'evidence reference')

    def test_temp_not_accepted_as_fact_source(self):
        self.reject_analysis(lambda r: r['identity'].update(locator='temp/note.md#L1-L2'), 'academic Raw')

    def test_cross_paper_evidence_rejected(self):
        cid, task, output, path = self.setup_evidence()
        self.paper(folder='other')
        output['reviews'][0]['occurrences'][0]['evidence']['locator'] = 'academic/raw/works/papers/other/paper.md#L4-L4'
        si.atomic_json(path, output)
        with self.assertRaisesRegex(ValueError, 'another paper'):
            pi.checked(self.root, self.state(), task)

    def test_unknown_contribution_and_missing_boundary(self):
        self.reject_analysis(lambda r: r['occurrences'][0].update(contribution_ids=['K404']), 'unknown contribution')

    def test_missing_unsupported_claims(self):
        self.reject_analysis(lambda r: r['occurrences'][0].update(unsupported_claims=[]), 'unsupported_claims must not be empty')

    def test_source_change_invalidates_pending_and_published_evidence(self):
        cid, task, output, path = self.setup_evidence()
        pi.commit(self.root, self.state(), task)
        pending = pi.prepare(self.root, self.state(), 'evidence')['task_id']
        si.atomic_json(pi.task_dir(self.root, pending) / 'analysis.json', output)
        paper = self.paper(); paper.write_text(paper.read_text()+'Unrelated footer change.\n')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            pi.checked(self.root, self.state(), pending)
        report = pi.report(self.root, self.state())
        self.assertEqual(report['usable_evidence'], 0)
        self.assertEqual(len(report['invalid_reviews']), 1)

    def test_configuration_change_invalidates_task(self):
        cid, task, output, path = self.setup_evidence()
        self.configure()
        with self.assertRaisesRegex(ValueError, 'stale task'):
            pi.checked(self.root, self.state(), task)

    def test_changed_task_packet_rejected(self):
        cid, task, output, path = self.setup_evidence()
        packet_path = pi.task_dir(self.root, task) / 'input.json'
        packet = json.loads(packet_path.read_text()); packet['allowed_paths'][cid].append('temp/x.md')
        si.atomic_json(packet_path, packet)
        with self.assertRaisesRegex(ValueError, 'task input changed'):
            pi.checked(self.root, self.state(), task)

    def test_changed_committed_output_rejected(self):
        cid, task, output, path = self.setup_evidence()
        pi.commit(self.root, self.state(), task)
        output['reviews'][0]['coverage_note'] = 'changed'; si.atomic_json(path, output)
        with self.assertRaisesRegex(ValueError, 'output was changed'):
            pi.commit(self.root, self.state(), task)

    def test_critical_and_partial_observations_not_award_evidence(self):
        cid, task, output, path = self.setup_evidence()
        output['reviews'][0]['occurrences'][0]['kind'] = 'critical'
        si.atomic_json(path, output); pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        data = json.loads(Path(result['data']).read_text())
        self.assertEqual(result['usable_evidence'], 0)
        self.assertEqual(len(data['other_observations']), 1)

    def test_empty_findings_preserve_reason(self):
        cid, task, output, path = self.setup_evidence()
        output['reviews'][0].update(occurrences=[], no_evidence_reason='Only a reference-list mention')
        si.atomic_json(path, output); pi.commit(self.root, self.state(), task)
        result = pi.report(self.root, self.state())
        self.assertIn('Only a reference-list mention', Path(result['report']).read_text())

    def test_prepare_configure_has_valid_agent_manifest(self):
        task = pi.prepare(self.root, self.state(), 'configure')
        manifest = task['agent_task']
        self.assertEqual(manifest['schema'], 'agent-task-v1')
        self.assertEqual(set(manifest['commands']), {'check', 'commit'})
        si.atomic_json(pi.task_dir(self.root, task['task_id']) / 'analysis.json', self.state()['config'])
        pi.commit(self.root, self.state(), task['task_id'])

    def test_symlink_and_traversal_do_not_escape(self):
        with self.assertRaisesRegex(ValueError, 'invalid paper-impact project'):
            pi.initialize(self.root, '../../raw', '10.1234/target')
        outside = self.root / 'outside'; outside.mkdir()
        path = self.root / 'projects/paper-impact/escape'; path.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            pi.initialize(self.root, 'escape', '10.1234/target')
        self.assertFalse(list(outside.iterdir()))

    def test_concurrent_writer_fails_without_waiting(self):
        with pi.locked(self.root, 'sample'):
            with self.assertRaisesRegex(ValueError, 'busy'):
                with pi.locked(self.root, 'sample'):
                    self.fail('second writer obtained lock')

    def test_explicit_source_choice_resolves_multiple_editions(self):
        cid, task, output, path = self.setup_evidence()
        self.paper(folder='second')
        selected = 'academic/raw/works/papers/citing/paper.md'
        task = pi.prepare(self.root, self.state(), 'evidence', [cid], [cid+'='+selected])['task_id']
        si.atomic_json(pi.task_dir(self.root, task) / 'analysis.json', output)
        pi.commit(self.root, self.state(), task)
        kb = pi.ranking(self.root, self.state())['rows'][0]['kb']
        self.assertEqual(kb['selection_basis'], 'host_fulltext_review')
        self.assertEqual(len(kb['matches']), 1)
        self.assertEqual(len(kb['alternatives']), 2)
        with self.assertRaisesRegex(ValueError, 'own bibliographic identity'):
            pi.prepare(self.root, self.state(), 'evidence', [cid], [cid+'=temp/unrelated.md'])

    def test_partial_source_can_be_read_but_not_promoted_by_default(self):
        cid, task, output, path = self.setup_evidence()
        output['reviews'][0]['coverage'] = 'partial'
        output['reviews'][0]['occurrences'][0]['limitations'] = ['Supplement not available']
        si.atomic_json(path, output)
        pi.commit(self.root, self.state(), task)
        report = pi.report(self.root, self.state())
        self.assertEqual(report['usable_evidence'], 0)
        self.assertEqual(len(json.loads(Path(report['data']).read_text())['other_observations']), 1)

    def test_wiki_metadata_without_raw_is_not_ingested_fulltext(self):
        self.ingest_list()
        wiki = self.root / 'academic/wiki/papers/citing.md'
        wiki.parent.mkdir(parents=True)
        wiki.write_text('---\ntitle: Citing Study\ndoi: 10.1234/citing\nsources: []\n---\nSummary only.\n')
        row = pi.ranking(self.root, self.state())['rows'][0]
        self.assertEqual(row['kb']['status'], 'metadata_only')

    def test_contribution_redefinition_invalidates_published_analysis(self):
        cid, task, output, path = self.setup_evidence()
        pi.commit(self.root, self.state(), task)
        state = self.state()
        state['config']['contributions'][0]['text'] = 'Completely different contribution'
        pi.save(self.root, state)
        result = pi.report(self.root, self.state())
        self.assertEqual(result['usable_evidence'], 0)
        self.assertIn('contribution definition changed', result['invalid_reviews'][0]['reason'])

    def test_doi_prefix_does_not_prove_identity(self):
        self.assertFalse(pi.identity_quote({'doi': '10.1234/target', 'title': 'Target Contribution'}, 'Target Contribution, 10.1234/targeted'))
        self.assertTrue(pi.identity_quote({'doi': '10.1234/target'}, 'doi: 10.1234/target).'))

    def test_reference_12_does_not_match_entry_112(self):
        cid, task, output, path = self.setup_evidence()
        paper = self.paper()
        paper.write_text(paper.read_text().replace('[12] Target', '[112] Target'))
        task = pi.prepare(self.root, self.state(), 'evidence')['task_id']
        output['reviews'][0]['reference']['quote'] = '[112] Target Contribution. DOI: 10.1234/target'
        si.atomic_json(pi.task_dir(self.root, task) / 'analysis.json', output)
        with self.assertRaisesRegex(ValueError, 'reference marker missing'):
            pi.checked(self.root, self.state(), task)

    def test_source_bound_contribution_must_belong_to_target(self):
        self.paper()
        config = self.state()['config']
        config['contributions'] = [{'id': 'K1', 'text': 'A method', 'status': 'source_bound',
                                  'evidence': {'locator': 'academic/raw/works/papers/citing/paper.md#L4-L4',
                                               'quote': 'The method [12] provides an effective solution.'}}]
        with self.assertRaisesRegex(ValueError, "target paper's own Raw"):
            pi.validate_config(self.root, config, self.state()['target'])

    def test_independence_rejected_when_author_ids_overlap(self):
        cid, task, output, path = self.setup_evidence()
        state = self.state()
        state['target']['authors'] = [{'name': 'Expert A', 'id': 'https://openalex.org/A1'}]
        pi.save(self.root, state)
        task = pi.prepare(self.root, self.state(), 'evidence')['task_id']
        output['reviews'][0]['independence'] = 'independent'
        si.atomic_json(pi.task_dir(self.root, task) / 'analysis.json', output)
        with self.assertRaisesRegex(ValueError, 'shared author'):
            pi.checked(self.root, self.state(), task)

    def test_rank_paginates_without_discarding_stored_candidates(self):
        payload = deepcopy(self.payload)
        second = deepcopy(self.record); second.update(doi='10.1234/second', provider_id='other:2')
        payload['records'].append(second); self.ingest_list(payload)
        result = pi.execute(pi.parser().parse_args(['rank', 'sample', '--limit', '1']), self.root)
        self.assertEqual(result['total'], 2)
        self.assertTrue(result['truncated'])
        other = pi.execute(pi.parser().parse_args(['rank', 'sample', '--limit', '1', '--offset', '1']), self.root)
        self.assertFalse(other['truncated'])
        self.assertNotEqual(result['rows'][0]['id'], other['rows'][0]['id'])

    def test_failed_atomic_publication_leaves_previous_state(self):
        previous = self.state()
        with patch.object(si, 'atomic_json', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.ingest_list()
        self.assertEqual(self.state(), previous)

    def test_wg_entry_passes_args_and_failure(self):
        import wg
        args = wg.build_parser().parse_args(['paper-impact', 'status', 'sample'])
        self.assertEqual(args.impact_args, ['status', 'sample'])
        import io
        from contextlib import redirect_stdout
        with patch.object(wg, 'run_script', return_value=(1, '{"ok":false,"error":"missing"}', '')), redirect_stdout(io.StringIO()) as output:
            rc = args.func(args)
        self.assertEqual(rc, 0)  # WG's existing envelope signals failure in JSON.
        self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Offline parser, intake, dual-backend and managed archive regression tests."""
import copy
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bibliography_sidecar as bs
import external_bibliography as eb
import ingest_paper as ip
import ingest_check as check
import inbox_plan
import ingest_inbox
import inbox_finalize
import re_ingest

TITLE = 'Reliable learning with quantum circuits'
MD = f'# {TITLE}\n\nAlice Smith, Bob Jones\n\n## Abstract\nResearch.\n## References\n[1] Other paper\n'
BIB = '''@article{smith,
 title={Reliable learning with {quantum} circuits},
 author={Smith, Alice and Jones, Bob},
 journal={Journal of Quantum Research}, year={2022}, doi={10.1000/example}
}'''
RIS = f'''TY  - JOUR
TI  - {TITLE}
AU  - Smith, Alice
AU  - Jones, Bob
JF  - Journal of Quantum Research
PY  - 2022/01/01
DO  - 10.1000/example
ER  -
'''


class SidecarTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir()
        self.pdf = self.inbox / 'paper.pdf'
        self.pdf.write_bytes(b'fixture PDF')
        self.extract = self.root / 'extract'
        self.extract.mkdir()
        (self.extract / 'paper.md').write_text(MD)
        (self.extract / 'paper.pdf').write_bytes(b'fixture PDF')

    def resolve(self, bib=BIB, md=MD, suffix='.bib'):
        self.pdf.with_suffix(suffix).write_text(bib, encoding='utf-8')
        return bs.stage_and_resolve(self.pdf, self.extract, md)

    def test_ris_and_bib_same_result(self):
        self.resolve(RIS, suffix='.ris')
        result = self.resolve()
        self.assertEqual(result['status'], 'verified')
        b = result['bibliographic']
        self.assertEqual(b['authors'], ['Alice Smith', 'Bob Jones'])
        self.assertEqual(b['year'], '2022')
        self.assertEqual(len(b['record_evidence']), 2)
        self.assertFalse(b['origin_verified'])
        self.assertEqual(b['publication_status_basis'], 'sidecar_declared')

    def test_ris_continuation_and_bom(self):
        ris = '\ufeff' + RIS.replace(TITLE, 'Reliable learning with\n  quantum circuits')
        self.assertEqual(self.resolve(ris, suffix='.ris')['status'], 'verified')

    def test_bib_strings_quotes_crossref_and_ignored_links(self):
        bib = '''@string{j = "Journal of " # {Quantum Research}}
@proceedings{parent, title=j, year=2022, doi={10.1000/parent}}
@inproceedings{child, title={Reliable learning with {quantum} circuits},
author="Smith, Alice and Jones, Bob", crossref={parent},
url={https://untrusted.invalid}, file={/etc/passwd}, note={\\input{untrusted}}}'''
        result = self.resolve(bib)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['bibliographic']['doi'], '')
        self.assertEqual(result['bibliographic']['publication_type'], 'proceedings-article')

    def test_latex_accents_and_corporate_author(self):
        self.assertEqual(bs.latex_text(r'{G\"{o}del} \& {M\"uller}'), 'Gödel & Müller')
        self.assertEqual(bs._bib_authors('{Research and Development} and Smith, Alice'),
                         ['{Research and Development}', 'Smith, Alice'])
        self.assertEqual(bs._person('{Research and Development}', True),
                         ('Research and Development', 'Research and Development'))
        self.assertEqual(bs._person('Smith, Jr., Alice', True), ('Alice Smith Jr.', 'Smith'))

    def test_preprint_does_not_override_formal(self):
        preprint = BIB.replace('smith,', 'preprint,').replace('Journal of Quantum Research', 'arXiv')
        self.assertEqual(self.resolve(preprint + '\n' + BIB)['status'], 'verified')
        self.assertEqual(self.resolve(preprint)['status'], 'no_match')

    def test_formal_conflicts_and_doi_contradiction(self):
        conflict = BIB.replace('smith,', 'other,').replace('2022', '2023')
        self.assertEqual(self.resolve(BIB + '\n' + conflict)['status'], 'conflict')
        self.assertEqual(self.resolve(md=MD.replace('## Abstract', 'DOI:10.1000/wrong\n## Abstract'))['status'], 'conflict')

    def test_source_doi_can_complete_formal_record(self):
        md = MD.replace('## Abstract', 'DOI:10.1000/example\n## Abstract')
        b = self.resolve(BIB.replace(', doi={10.1000/example}', ''), md)['bibliographic']
        self.assertEqual(b['doi'], '10.1000/example')
        self.assertEqual(b['doi_source'], 'paper.md')

    def test_complementary_ris_bib_fields_merge_with_provenance(self):
        self.pdf.with_suffix('.ris').write_text(RIS.replace('DO  - 10.1000/example\n', '')
                                               .replace('ER  -', 'SP  - 10\nEP  - 20\nER  -'))
        result = self.resolve(BIB.replace('year={2022}', 'year={2022}, volume={4}'))
        self.assertEqual(result['status'], 'verified')
        b = result['bibliographic']
        self.assertEqual((b['doi'], b['volume'], b['pages']), ('10.1000/example', '4', '10-20'))
        self.assertEqual(len(b['record_evidence']), 2)
        origins = b['field_record_evidence']
        self.assertTrue(origins['doi'][0]['path'].endswith('.bib'))
        self.assertTrue(origins['pages'][0]['path'].endswith('.ris'))
        self.assertEqual(bs.replay(result['receipt'], MD, self.extract)['field_record_evidence'], origins)

    def test_optional_merge_is_order_independent(self):
        incomplete = BIB.replace('smith,', 'incomplete,').replace(', doi={10.1000/example}', '')
        complete = BIB.replace('year={2022}', 'year={2022}, volume={4}, number={2}, pages={10--20}')
        values = []
        for body in (incomplete + complete, complete + incomplete):
            b = self.resolve(body)['bibliographic']
            values.append(tuple(b[key] for key in ('doi', 'volume', 'issue', 'pages')))
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[0], ('10.1000/example', '4', '2', '10--20'))

    def test_conflicting_optional_fields_block_even_with_empty_third_record(self):
        for key, first, second in (('doi', '10.1000/a-b', '10.1000/a.b'),
                                   ('volume', '1.2', '1-2'), ('number', '1', '2'),
                                   ('pages', '10-20', '10.20')):
            with self.subTest(field=key):
                base = BIB.replace(', doi={10.1000/example}', '')
                left = base.replace('year={2022}', 'year={2022}, ' + key + '={' + first + '}')
                right = left.replace('smith,', 'other,').replace(first, second)
                empty = base.replace('smith,', 'empty,')
                result = self.resolve(left + empty + right)
                self.assertEqual(result['status'], 'conflict')
                self.assertIn('issue' if key == 'number' else key,
                              result['receipt']['diagnostics'][-1]['conflicting_fields'])

    def test_equivalent_doi_url_case_and_page_typography_merge(self):
        self.pdf.with_suffix('.ris').write_text(RIS.replace('10.1000/example', 'https://doi.org/10.1000/EXAMPLE')
                                               .replace('ER  -', 'SP  - 10\nEP  - 20\nER  -'))
        self.assertEqual(self.resolve(BIB.replace('year={2022}', 'year={2022}, pages={10--20}'))['status'],
                         'verified')

    def test_distinct_core_fields_and_publication_types_never_merge(self):
        variants = (BIB.replace('author={Smith, Alice and Jones, Bob}', 'author={Jones, Bob and Smith, Alice}'),
                    BIB.replace('2022', '2023'),
                    BIB.replace('Journal of Quantum Research', 'Another Journal'),
                    BIB.replace('@article', '@inproceedings').replace('journal=', 'booktitle='))
        for other in variants:
            with self.subTest(other=other):
                self.assertEqual(self.resolve(BIB + other.replace('smith,', 'other,'))['status'], 'conflict')

    def test_unrelated_author_or_venue_commands_do_not_disable_library(self):
        for field in ('Smith, Alice', 'Journal of Quantum Research'):
            other = BIB.replace('smith,', 'other,').replace('Reliable learning with {quantum} circuits',
                                                        'An unrelated catalogue title')
            other = other.replace(field, r'\foobar{unsupported}')
            with self.subTest(field=field):
                self.assertEqual(self.resolve(BIB + other)['status'], 'verified')

    def test_unknown_titles_and_matching_record_commands_remain_unresolved(self):
        for field in ('Reliable learning with {quantum} circuits', 'Smith, Alice',
                      'Journal of Quantum Research'):
            other = BIB.replace('smith,', 'other,').replace(field, r'\foobar{unsupported}')
            with self.subTest(field=field):
                self.assertEqual(self.resolve(BIB + other)['status'], 'parse_error')

    def test_unrelated_structural_errors_are_not_skipped(self):
        other = BIB.replace('smith,', 'other,').replace('Reliable learning with {quantum} circuits',
                                                    'An unrelated catalogue title')
        self.assertEqual(self.resolve(BIB + other[:-1])['status'], 'parse_error')
        self.assertEqual(self.resolve(BIB + other.replace('{2022}', 'undefined'))['status'], 'parse_error')

    def test_letter_accent_command_boundaries(self):
        for command in (r'\unknown{Alice}', r'\url{https://example.test}', r'\cite{other}',
                        r'\bar{x}', r'\usepackage{shellesc}'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                bs.latex_text(command)
        self.assertEqual(bs.latex_text(r'\u{a} \v c \c{c} \'e'), 'ă č ç é')

    def test_pdf_completed_doi_is_not_attributed_to_empty_export(self):
        md = MD.replace('## Abstract', 'DOI:10.1000/example\n## Abstract')
        b = self.resolve(BIB.replace(', doi={10.1000/example}', ''), md)['bibliographic']
        self.assertEqual(b['field_record_evidence']['doi'], [{'path': 'paper.md'}])

    def test_old_accepted_flag_cannot_hide_doi_or_type_conflict(self):
        for body in (BIB.replace('10.1000/example', '10.1000/a-b') +
                     BIB.replace('smith,', 'other,').replace('10.1000/example', '10.1000/a.b'),
                     BIB + BIB.replace('@article{smith,', '@inproceedings{other,').replace('journal=', 'booktitle=')):
            receipt = {'protocol_version': bs.PROTOCOL, 'source_sha256': eb.digest(MD), 'status': 'verified',
                       'files': [{'path': 'bibliography/old.bib', 'format': 'bib', 'body': body,
                                  'sha256': bs.sha(body.encode())}]}
            self.assertIsNone(eb.replay(receipt, MD))

    def test_archive_checks_optional_fields_and_provenance(self):
        self.pdf.with_suffix('.ris').write_text(RIS.replace('DO  - 10.1000/example\n', '')
                                               .replace('ER  -', 'SP  - 10\nEP  - 20\nER  -'))
        bib = self.resolve()['bibliographic']
        fm = {'type': 'paper-summary', 'sources': ['extract/paper.md'], 'date': 2022, 'venue': bib['venue']}
        for field, value in (('pages', '99'), ('volume', '99'), ('issue', '99'),
                             ('publication_type', 'proceedings-article'), ('field_record_evidence', {})):
            with self.subTest(field=field), patch.object(check, 'REPO', self.root):
                ip.persist_bibliographic_metadata(self.extract, {**bib, field: value})
                self.assertTrue(check.check_bibliographic_consistency('academic/wiki/papers/test.md', fm))
        legacy = {k: v for k, v in bib.items() if k != 'field_record_evidence'}
        with patch.object(check, 'REPO', self.root):
            ip.persist_bibliographic_metadata(self.extract, legacy)
            self.assertFalse(check.check_bibliographic_consistency('academic/wiki/papers/test.md', fm))

    def test_merged_library_success_skips_network_and_llm_both_backends(self):
        self.pdf.with_suffix('.ris').write_text(RIS.replace('DO  - 10.1000/example\n', ''))
        self.pdf.with_suffix('.bib').write_text(BIB)
        for backend in ('api', 'agent'):
            state = {'source': 'inbox/paper.pdf', 'extract_dir': 'extract',
                     'bibliography_source_policy': 'free_external_first'}
            with patch.object(ip, 'REPO', self.root), patch.object(ip, 'ingest_mode', return_value=backend), \
                    patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')), \
                    patch.object(ip, 'call_json', side_effect=AssertionError('no LLM')):
                ip.prepare_external_bibliography(state, MD)
                result = ip.review_bibliographic_metadata(state['bibliographic_meta'], MD, agent_workspace=True)
                self.assertTrue(result['ok'])
                self.assertFalse(result['worker']['api_called'])

    def test_exact_identity_and_reference_boundary(self):
        unrelated = BIB.replace('smith,', 'other,').replace(TITLE, 'Different paper')
        # Nested brace title in BIB is replaced separately.
        unrelated = unrelated.replace('Reliable learning with {quantum} circuits', 'Different paper')
        self.assertEqual(self.resolve(unrelated)['status'], 'no_match')
        self.assertEqual(self.resolve(md=MD.replace('Alice Smith', 'Charlie Brown'))['status'], 'no_match')
        self.assertEqual(self.resolve(md='# Other paper\n## References\n' + MD)['status'], 'no_match')
        self.assertEqual(self.resolve(unrelated + '\n' + BIB)['status'], 'verified')

    def test_unsupported_and_incomplete_syntax_fail_closed(self):
        for bib in (BIB[:-1], BIB.replace('{2022}', 'unknown_macro'),
                    BIB.replace('{quantum}', r'\input{secret}'),
                    BIB + BIB, '@article{x, crossref={missing}}'):
            with self.subTest(bib=bib):
                self.assertEqual(self.resolve(bib)['status'], 'parse_error')
        self.assertEqual(self.resolve(RIS.replace('ER  -', ''), suffix='.ris')['status'], 'parse_error')

    def test_parser_bounds(self):
        with patch.object(bs, 'MAX_RECORDS', 1):
            with self.assertRaises(ValueError):
                bs.parse_bib(BIB + BIB.replace('smith,', 'other,'))
        with patch.object(bs, 'MAX_DEPTH', 2):
            with self.assertRaises(ValueError):
                bs.parse_bib('@article{x,title={{{{{too deep}}}}}}')
        with patch.object(bs, 'MAX_FILE_BYTES', 16):
            with self.assertRaises(ValueError):
                bs.parse_bib('@string{a={123456789}} @string{b=a # a}')
            self.assertEqual(self.resolve()['status'], 'parse_error')
        with patch.object(bs, 'MAX_FILES', 0):
            self.assertEqual(self.resolve()['status'], 'parse_error')

    def test_named_success_ignores_broken_conflicting_and_overlimit_shared_catalogs(self):
        self.pdf.with_suffix('.bib').write_text(BIB)
        (self.inbox/'library.bib').write_text('@article{broken,')
        for i in range(bs.MAX_FILES + 1):
            (self.inbox/f'library-{i}.ris').write_text(RIS.replace('2022','2023'))
        result=bs.stage_and_resolve(self.pdf,self.extract,MD)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['receipt']['discovery_scope'],'same-name')
        self.assertEqual([f['original_name'] for f in result['receipt']['files']],['paper.bib'])
        self.assertEqual(bs.replay(result['receipt'],MD,self.extract),result['bibliographic'])

    def test_named_no_match_falls_back_to_directory(self):
        self.pdf.with_suffix('.bib').write_text(BIB.replace('Reliable learning with {quantum} circuits','Unrelated paper'))
        (self.inbox/'library.ris').write_text(RIS)
        result=bs.stage_and_resolve(self.pdf,self.extract,MD)
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['receipt']['discovery_scope'],'directory')
        self.assertEqual(len(result['receipt']['files']),2)
        self.assertEqual(bs.replay(result['receipt'],MD,self.extract),result['bibliographic'])

    def test_named_conflict_or_parse_error_does_not_hide_behind_shared_record(self):
        (self.inbox/'library.ris').write_text(RIS)
        for body,status in ((BIB+BIB.replace('smith,','other,').replace('2022','2023'),'conflict'),
                            ('@article{broken,','parse_error')):
            with self.subTest(status=status):
                self.pdf.with_suffix('.bib').write_text(body)
                result=bs.stage_and_resolve(self.pdf,self.extract,MD)
                self.assertEqual(result['status'],status)
                self.assertEqual([f['original_name'] for f in result['receipt']['files']],['paper.bib'])

    def test_named_success_evaluated_once_but_replay_remains_independent(self):
        with patch.object(bs,'evaluate',wraps=bs.evaluate) as evaluate:
            result=self.resolve()
            self.assertEqual(evaluate.call_count,1)
            self.assertEqual(bs.replay(result['receipt'],MD,self.extract),result['bibliographic'])
            self.assertEqual(evaluate.call_count,2)
        artifact=self.extract/result['receipt']['files'][0]['path']
        artifact.write_text('corrupted')
        self.assertIsNone(bs.replay(result['receipt'],MD,self.extract))

    def test_stage_snapshot_is_rechecked_after_write_without_semantic_reparse(self):
        self.pdf.with_suffix('.bib').write_text(BIB)
        local=bs.resolve_local(self.pdf,MD)
        real=Path.write_bytes
        def corrupt(path,data):
            return real(path,data+b'corrupted' if path.suffix=='.bib' else data)
        with patch.object(Path,'write_bytes',corrupt):
            with self.assertRaisesRegex(ValueError,'integrity'):
                bs.stage_resolved(local,self.extract,MD)

    def test_discovery_shared_catalog_and_other_paper(self):
        (self.inbox / 'paper.bib').write_text(BIB)
        (self.inbox / 'library.ris').write_text(RIS)
        (self.inbox / 'other.pdf').write_bytes(b'pdf')
        (self.inbox / 'other.bib').write_text(BIB)
        self.assertEqual([p.name for p in bs.discover(self.pdf)], ['paper.bib', 'library.ris'])
        self.assertEqual([p.name for p in bs.discover(self.pdf, same_name_only=True)], ['paper.bib'])

    def test_symlink_rejected_and_paths_bound(self):
        outside = self.root / 'outside.bib'
        outside.write_text(BIB)
        self.pdf.with_suffix('.bib').symlink_to(outside)
        self.assertEqual(bs.stage_and_resolve(self.pdf, self.extract, MD)['status'], 'parse_error')
        self.pdf.with_suffix('.bib').unlink()
        result = self.resolve()
        for path in ('../escape.bib', 'bibliography/../escape.bib', 'bibliography/file.txt'):
            receipt = copy.deepcopy(result['receipt'])
            receipt['files'][0]['path'] = path
            self.assertIsNone(bs.replay(receipt, MD, self.extract))

    def test_hash_and_source_drift_rejected(self):
        receipt = self.resolve()['receipt']
        self.assertIsNotNone(eb.replay(receipt, MD, self.extract))
        self.assertIsNone(eb.replay(receipt, MD + 'changed', self.extract))
        changed = copy.deepcopy(receipt)
        changed['files'][0]['body'] += ' '
        self.assertIsNone(eb.replay(changed, MD))
        (self.extract / receipt['files'][0]['path']).write_text('changed')
        self.assertIsNone(eb.replay(receipt, MD, self.extract))

    def test_local_success_zero_network_and_bibliographic_llm_both_backends(self):
        self.pdf.with_suffix('.bib').write_text(BIB)
        (self.inbox/'library.bib').write_text('@article{broken,')
        for backend in ('agent', 'api'):
            state = {'source': 'inbox/paper.pdf', 'extract_dir': 'extract',
                     'bibliography_source_policy': 'free_external_first'}
            with patch.object(ip, 'REPO', self.root), patch.object(ip, 'ingest_mode', return_value=backend), \
                    patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')), \
                    patch.object(ip, 'call_json', side_effect=AssertionError('no bibliography LLM')):
                ip.prepare_external_bibliography(state, MD)
                result = ip.review_bibliographic_metadata(state['bibliographic_meta'], MD, agent_workspace=True)
                self.assertTrue(result['ok'])
                self.assertEqual(result['worker']['skip_reason'], 'verified_local_bibliography')
                ip.prepare_external_bibliography(state, MD)
                self.assertTrue(state['telemetry']['external_bibliography']['cache_hit'])
                self.assertEqual(state['telemetry']['external_bibliography']['request_count'], 0)

    def test_conflict_blocks_without_network_or_llm(self):
        self.pdf.with_suffix('.bib').write_text(BIB + BIB.replace('smith,', 'other,').replace('2022', '2023'))
        state = {'source': 'inbox/paper.pdf', 'extract_dir': 'extract',
                 'bibliography_source_policy': 'free_external_first'}
        with patch.object(ip, 'REPO', self.root), patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')), \
                patch.object(ip, 'call_json', side_effect=AssertionError('no LLM')):
            ip.prepare_external_bibliography(state, MD)
            self.assertEqual(ip.review_bibliographic_metadata(state['bibliographic_meta'], MD)['status'],
                             'bibliographic_review_required')

    def test_staged_catalog_remains_out_of_queue_after_pdf_cleanup(self):
        self.pdf.with_suffix('.bib').write_text(BIB)
        state = {'source': 'inbox/paper.pdf', 'extract_dir': 'extract',
                 'bibliography_source_policy': 'free_external_first'}
        with patch.object(ip, 'REPO', self.root), patch.object(inbox_plan, 'REPO', self.root), \
                patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox):
            ip.prepare_external_bibliography(state, MD)
            self.pdf.unlink()
            self.assertFalse(ingest_inbox.scan_inbox())
            plan = inbox_plan.build_plan(self.inbox)
            self.assertFalse(plan['items'])
            self.assertEqual(plan['retained_sources'], ['inbox/paper.bib'])

    def test_chat_pdf_imports_same_name_companions_and_preserves_originals(self):
        source = self.root / 'outside.pdf'
        source.write_bytes(b'pdf')
        source.with_suffix('.bib').write_text(BIB)
        with patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox):
            target, receipt = ingest_inbox.stage_chat_input(source=str(source))
        self.assertEqual(target.with_suffix('.bib').read_text(), BIB)
        self.assertEqual(source.with_suffix('.bib').read_text(), BIB)
        self.assertEqual(len(receipt['bibliography_sidecars']), 1)

    def test_chat_receipt_failure_removes_only_new_copies(self):
        source = self.root / 'outside.pdf'
        source.write_bytes(b'pdf')
        source.with_suffix('.bib').write_text(BIB)
        with patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox), \
                patch.object(ingest_inbox, '_write_json_atomic', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                ingest_inbox.stage_chat_input(source=str(source))
        self.assertEqual([p.name for p in self.inbox.iterdir()], ['paper.pdf'])
        self.assertEqual(source.with_suffix('.bib').read_text(), BIB)

    def test_malformed_local_uses_existing_external_fallback(self):
        self.pdf.with_suffix('.bib').write_text('bad bib')
        state = {'source': 'inbox/paper.pdf', 'extract_dir': 'extract',
                 'bibliography_source_policy': 'free_external_first'}
        result = {'receipt': {'status': 'unresolved'}, 'bibliographic': None,
                  'execution': {'request_count': 0}, 'cache_hit': False}
        with patch.object(ip, 'REPO', self.root), patch.object(eb, 'resolve', return_value=result) as resolve:
            ip.prepare_external_bibliography(state, MD)
        resolve.assert_called_once()
        self.assertEqual(state['bibliographic_meta']['local_lookup_receipt']['status'], 'parse_error')

    def test_inbox_grouping_and_orphan_visibility(self):
        self.pdf.with_suffix('.bib').write_text(BIB)
        for name in ('second.pdf', 'third.pdf'):
            (self.inbox / name).write_bytes(b'pdf')
        with patch.object(inbox_plan, 'REPO', self.root), patch.object(ingest_inbox, 'REPO', self.root), \
                patch.object(ingest_inbox, 'INBOX', self.inbox):
            plan = inbox_plan.build_plan(self.inbox)
            self.assertEqual(len(plan['items']), 3)
            self.assertTrue(plan['routing']['batch_eligible'])
            self.assertEqual(len(ingest_inbox.scan_inbox()), 3)
            for pdf in self.inbox.glob('*.pdf'):
                pdf.unlink()
            self.assertEqual(len(inbox_plan.build_plan(self.inbox)['items']), 1)
            self.assertEqual(ingest_inbox.scan_inbox()[0].suffix, '.bib')

    def test_external_import_names_and_collision(self):
        source = self.root / 'outside.pdf'
        source.write_bytes(b'new pdf')
        source.with_suffix('.ris').write_text(RIS)
        (self.root / 'library.bib').write_text(BIB)
        with patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox):
            target, receipt = ingest_inbox.stage_external_file(str(source), 'paper.pdf')
        self.assertNotEqual(target.name, 'paper.pdf')
        self.assertEqual(target.with_suffix('.ris').read_text(), RIS)
        self.assertEqual(len(receipt['bibliography_sidecars']), 1)
        self.assertFalse((self.inbox / 'library.bib').exists())
        target.with_suffix('.ris').write_text('conflict')
        with self.assertRaises(ValueError):
            bs.import_companions(source, target)
        self.assertEqual(target.with_suffix('.ris').read_text(), 'conflict')

    def test_external_receipt_failure_rolls_back_only_owned_copies(self):
        source = self.root / 'external.pdf'
        source.write_bytes(b'new PDF')
        source.with_suffix('.bib').write_text(BIB)
        for reuse_pdf, reuse_bib in ((False, False), (True, False), (True, True), (False, True)):
            target = self.inbox / 'external.pdf'
            target.unlink(missing_ok=True)
            target.with_suffix('.bib').unlink(missing_ok=True)
            if reuse_pdf:
                target.write_bytes(source.read_bytes())
            if reuse_bib:
                target.with_suffix('.bib').write_text(BIB)
            with self.subTest(pdf=reuse_pdf, bib=reuse_bib), patch.object(ingest_inbox, 'REPO', self.root), \
                    patch.object(ingest_inbox, 'INBOX', self.inbox), \
                    patch.object(ingest_inbox, '_write_json_atomic', side_effect=OSError('receipt failure')):
                with self.assertRaises(OSError):
                    ingest_inbox.stage_external_file(str(source))
            self.assertEqual(target.exists(), reuse_pdf)
            self.assertEqual(target.with_suffix('.bib').exists(), reuse_bib)
            self.assertEqual(source.read_bytes(), b'new PDF')
            self.assertEqual(source.with_suffix('.bib').read_text(), BIB)

    def archived_local_pdf(self):
        bib = self.resolve()['bibliographic']
        ip.persist_bibliographic_metadata(self.extract, bib)
        (self.extract / 'wiki.md').write_text('---\ntitle: Fixture\n---\n# Fixture')
        managed = self.root / 'temp/inbox-extract/fixture'
        shutil.copytree(self.extract, managed)
        ip._write_paper_artifact_manifest({'source': 'inbox/paper.pdf'}, managed)
        raw = self.root / 'academic/raw/references/fixture'
        inbox_finalize.finalize(self.root, 'fixture', raw, self.root / 'academic/wiki/papers/fixture.md',
                                managed, managed / 'manifest.json', False)
        return raw / 'paper.pdf'

    def duplicate_match(self, raw):
        return {'raw_path': str(raw.relative_to(self.root)), 'binary_sha256': bs.sha(self.pdf.read_bytes())}

    def test_duplicate_pdf_with_new_bibliography_is_retained(self):
        raw = self.archived_local_pdf()
        self.pdf.with_suffix('.bib').write_text(BIB.replace('2022', '2023'))
        with patch.object(ingest_inbox, 'REPO', self.root), \
                patch.object(ingest_inbox.trash_util, 'trash_path', side_effect=AssertionError('must retain')):
            result = ingest_inbox.cleanup_exact_duplicate(self.pdf, self.duplicate_match(raw))
        self.assertEqual(result['status'], 'retained_bibliography')
        self.assertTrue(self.pdf.exists())
        self.assertIn('2023', self.pdf.with_suffix('.bib').read_text())
        self.assertEqual(json.loads((self.root / result['receipt_path']).read_text())['status'], 'retained_bibliography')

    def test_duplicate_pdf_with_verified_archived_bibliography_can_cleanup(self):
        raw = self.archived_local_pdf()
        with patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox), \
                patch.object(ingest_inbox.trash_util, 'trash_path', side_effect=lambda p: p.unlink()):
            result = ingest_inbox.cleanup_exact_duplicate(self.pdf, self.duplicate_match(raw))
            self.assertFalse(ingest_inbox.scan_inbox())
        self.assertEqual(result['status'], 'trashed')
        self.assertTrue(self.pdf.with_suffix('.bib').exists())
        self.assertTrue(raw.exists())

    def test_duplicate_guard_rejects_missing_or_damaged_archived_evidence(self):
        raw = self.archived_local_pdf()
        receipt = ip.yaml.safe_load(raw.with_name('source.yaml').read_text())['bibliographic']['external_receipt']
        artifact = raw.parent / receipt['files'][0]['path']
        artifact.unlink()
        self.assertEqual(bs.duplicate_companion_status(self.pdf, raw)['status'], 'unarchived')
        raw.with_name('source.yaml').write_text('invalid: [')  # Temporary archive corruption injection.
        self.assertEqual(bs.duplicate_companion_status(self.pdf, raw)['status'], 'unarchived')

    def test_named_receipt_ignores_shared_catalog_but_rejects_unsafe_named_file(self):
        raw = self.archived_local_pdf()
        (self.inbox / 'library.ris').write_text(RIS)
        self.assertEqual(bs.duplicate_companion_status(self.pdf, raw)['status'], 'archived')
        (self.inbox / 'library.ris').unlink()
        self.pdf.with_suffix('.bib').unlink()
        self.pdf.with_suffix('.bib').symlink_to(raw / 'missing')
        self.assertEqual(bs.duplicate_companion_status(self.pdf, raw)['status'], 'unarchived')

    def test_legacy_receipt_keeps_directory_cleanup_scope(self):
        raw=self.archived_local_pdf()
        source=ip.yaml.safe_load(raw.with_name('source.yaml').read_text())
        source['bibliographic']['external_receipt'].pop('discovery_scope',None)
        raw.with_name('source.yaml').write_text(ip.yaml.safe_dump(source))
        (self.inbox/'library.ris').write_text(RIS)
        self.assertEqual(bs.duplicate_companion_status(self.pdf,raw)['status'],'unarchived')
        receipt=source['bibliographic']['external_receipt']
        self.assertIsNotNone(bs.replay(receipt,MD,raw.parent))

    def test_duplicate_guard_rechecks_after_receipt_write(self):
        raw = self.archived_local_pdf()
        write = ingest_inbox._write_json_atomic
        def mutate(path, data):
            write(path, data)
            self.pdf.with_suffix('.bib').write_text('changed during cleanup')
        with patch.object(ingest_inbox, 'REPO', self.root), \
                patch.object(ingest_inbox, '_write_json_atomic', side_effect=mutate), \
                patch.object(ingest_inbox.trash_util, 'trash_path', side_effect=AssertionError('must retain')):
            with self.assertRaisesRegex(RuntimeError, '书目文件已变化'):
                ingest_inbox.cleanup_exact_duplicate(self.pdf, self.duplicate_match(raw))
        self.assertTrue(self.pdf.exists())

    def test_unarchived_duplicate_reaches_pending_report_without_agent_dispatch(self):
        raw = self.archived_local_pdf()
        self.pdf.with_suffix('.bib').write_text(BIB.replace('2022', '2023'))
        output = io.StringIO()
        with patch.object(ingest_inbox, 'REPO', self.root), patch.object(ingest_inbox, 'INBOX', self.inbox), \
                patch.object(ingest_inbox.agent_task, 'ingest_backend', return_value='agent'), \
                patch.object(ingest_inbox.sf, 'ensure_index'), \
                patch.object(ingest_inbox.sf, 'lookup_exact', return_value=self.duplicate_match(raw)), \
                patch.object(ingest_inbox, 'classify_file_details', return_value={'file_type': 'paper', 'needs_api_review': False}), \
                patch.object(ingest_inbox.subprocess, 'run', side_effect=AssertionError('no agent dispatch')), \
                patch.object(ingest_inbox.trash_util, 'trash_path', side_effect=AssertionError('must retain')), \
                patch.object(sys, 'argv', ['ingest_inbox.py', '--run', '--file', 'inbox/paper.pdf']), \
                contextlib.redirect_stdout(output):
            ingest_inbox.main()
        result = ingest_inbox._extract_last_json(output.getvalue())
        self.assertEqual(result['status'], 'bibliographic_review_required')
        self.assertEqual(result['files'][0]['cleanup']['status'], 'retained_bibliography')
        self.assertFalse(result['files'][0]['retryable'])
        self.assertTrue(self.pdf.exists())

    def test_duplicate_cleanup_rejects_new_sidecar_arriving_after_receipt(self):
        raw = self.archived_local_pdf()
        write = ingest_inbox._write_json_atomic
        def add(path, data):
            write(path, data)
            self.pdf.with_suffix('.ris').write_text(RIS)
        with patch.object(ingest_inbox, 'REPO', self.root), \
                patch.object(ingest_inbox, '_write_json_atomic', side_effect=add), \
                patch.object(ingest_inbox.trash_util, 'trash_path', side_effect=AssertionError('must retain')):
            with self.assertRaisesRegex(RuntimeError, '集合已变化'):
                ingest_inbox.cleanup_exact_duplicate(self.pdf, self.duplicate_match(raw))
        self.assertTrue(self.pdf.exists())

    def conflict_state(self):
        self.pdf.with_suffix('.bib').write_text(BIB + BIB.replace('smith,', 'other,').replace('2022', '2023'))
        state = {'transaction_id': 'extract', 'status': 'bibliographic_review_required',
                 'source': 'inbox/paper.pdf', 'extract_dir': 'extract', 'errors': ['old conflict'],
                 'bibliography_source_policy': 'free_external_first', 'bibliographic_review_required': True,
                 'next_action': 'repair_bibliographic_review_then_resume', 'retryable': False}
        with patch.object(ip, 'REPO', self.root):
            ip.prepare_external_bibliography(state, MD)
        ip._write_paper_artifact_manifest(state, self.extract)
        return state

    def test_corrected_local_conflict_resumes_original_transaction_without_ocr(self):
        state = self.conflict_state()
        self.pdf.with_suffix('.bib').write_text(BIB)
        def stop_at_content(s):
            self.assertEqual(s['status'], 'write_wiki')
            self.assertEqual(s['bibliographic_meta']['year'], '2022')
            raise StopIteration('content stage reached')
        with patch.object(ip, 'REPO', self.root), patch.object(ip, 'TEMP_EXTRACT', self.root), \
                patch.object(ip.inbox_state, 'save'), patch.object(ip, 'ingest_mode', return_value='agent'), \
                patch.object(ip, 'run', side_effect=AssertionError('no OCR')), \
                patch.object(eb, 'resolve', side_effect=AssertionError('no network')), \
                patch.object(ip, 'call_json', side_effect=AssertionError('no bibliography LLM')), \
                patch.object(ip, '_post_extract_duplicate', return_value=None), \
                patch.object(ip, '_review_pending_relationship_after_bibliography', return_value=(True, '')), \
                patch.object(ip, 'step_write_wiki', side_effect=stop_at_content):
            with self.assertRaisesRegex(StopIteration, 'content stage reached'):
                ip.run_prepare(state)
        self.assertFalse(state['bibliographic_review_required'])
        self.assertNotIn('local_bibliography_issue', state['bibliographic_meta'])
        self.assertNotIn('next_action', state)
        self.assertEqual(state['errors'], [])
        self.assertEqual(state['transaction_id'], 'extract')

    def test_unchanged_conflict_resume_stays_pending_without_calls(self):
        state = self.conflict_state()
        with patch.object(ip, 'REPO', self.root), patch.object(ip, 'TEMP_EXTRACT', self.root), \
                patch.object(ip.inbox_state, 'save'), patch.object(ip, 'run', side_effect=AssertionError('no OCR')), \
                patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')), \
                patch.object(ip, 'step_write_wiki', side_effect=AssertionError('no content')):
            result = ip.run_prepare(state)
        self.assertEqual(result['status'], 'bibliographic_review_required')

    def test_corrected_local_conflict_api_resumes_at_content_without_bibliography_call(self):
        state = self.conflict_state()
        self.pdf.with_suffix('.bib').write_text(BIB)
        def stop_at_content(s, review, paper):
            self.assertTrue(review['ok'])
            self.assertFalse(review['worker']['api_called'])
            self.assertEqual(review['bibliographic']['year'], '2022')
            raise StopIteration('API content stage reached')
        with patch.object(ip, 'REPO', self.root), patch.object(ip, 'TEMP_EXTRACT', self.root), \
                patch.object(ip.inbox_state, 'save'), patch.object(ip, 'ingest_mode', return_value='api'), \
                patch.object(ip, 'run', side_effect=AssertionError('no OCR')), \
                patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')), \
                patch.object(ip, 'call_json', side_effect=AssertionError('no bibliography LLM')), \
                patch.object(ip, 'execute_api_paper_workspace', side_effect=stop_at_content):
            with self.assertRaisesRegex(StopIteration, 'API content stage reached'):
                ip.run_prepare(state)
        self.assertFalse(state['bibliographic_review_required'])

    def test_local_conflict_resume_does_not_reopen_frozen_external_evidence(self):
        state = self.conflict_state()
        state['bibliographic_meta']['external_lookup_receipt'] = {'status': 'unresolved'}
        self.pdf.with_suffix('.bib').write_text(BIB)
        with patch.object(ip, 'REPO', self.root), patch.object(ip.inbox_state, 'save'), \
                patch.object(ip, 'step_extract', side_effect=AssertionError('frozen receipt')):
            result = ip.run_prepare(state)
        self.assertEqual(result['status'], 'bibliographic_review_required')

    def test_local_conflict_resume_rejects_text_bundle_and_pdf_drift(self):
        for change in ('paper.md', 'staged_bibliography', 'input_pdf'):
            with self.subTest(change=change):
                state = self.conflict_state()
                original_md = (self.extract / 'paper.md').read_bytes()
                original_pdf = self.pdf.read_bytes()
                path = (self.pdf if change == 'input_pdf' else self.extract / 'paper.md' if change == 'paper.md'
                        else self.extract / state['bibliographic_meta']['local_lookup_receipt']['files'][0]['path'])
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with patch.object(ip, 'REPO', self.root), patch.object(ip, 'TEMP_EXTRACT', self.root), \
                        patch.object(ip.inbox_state, 'save'), patch.object(ip, 'run', side_effect=AssertionError('no OCR')), \
                        patch.object(eb, 'resolve', side_effect=AssertionError('no HTTP')):
                    result = ip.run_prepare(state)
                self.assertEqual(result['status'], 'bibliographic_review_required')
                self.assertTrue(result['errors'])
                path.write_bytes(original)
                (self.extract / 'paper.md').write_bytes(original_md)
                self.pdf.write_bytes(original_pdf)

    def test_managed_archive_and_validator_reject_actual_file_drift(self):
        self.pdf.with_suffix('.ris').write_text(RIS.replace('DO  - 10.1000/example\n', '')
                                               .replace('ER  -', 'SP  - 10\nEP  - 20\nER  -'))
        bib = self.resolve()['bibliographic']
        managed = self.root / 'temp/inbox-extract/fixture'
        shutil.copytree(self.extract, managed)
        self.extract = managed
        ip.persist_bibliographic_metadata(self.extract, bib)
        (self.extract / 'wiki.md').write_text('---\ntitle: Fixture\n---\n# Fixture')
        state = {'source': 'inbox/paper.pdf'}
        ip._write_paper_artifact_manifest(state, self.extract)
        manifest = json.loads((self.extract / 'manifest.json').read_text())
        self.assertTrue(any(x['role'] == 'bibliographic-source' for x in manifest['raw_artifacts']))
        raw = self.root / 'academic/raw/references/fixture'
        wiki = self.root / 'academic/wiki/papers/fixture.md'
        staged_sidecar = self.extract / bib['external_receipt']['files'][0]['path']
        staged_sidecar.write_text('changed')
        with self.assertRaises(ValueError):
            inbox_finalize.finalize(self.root, 'fixture', raw, wiki, self.extract,
                                    self.extract / 'manifest.json', False)
        self.assertFalse(raw.exists())
        staged_sidecar.write_text(BIB)
        inbox_finalize.finalize(self.root, 'fixture', raw, wiki, self.extract,
                                self.extract / 'manifest.json', False)
        sidecar = raw / bib['external_receipt']['files'][0]['path']
        self.assertEqual(sidecar.read_text(), BIB)
        fm = {'type': 'paper-summary', 'sources': [str((raw / 'paper.md').relative_to(self.root))],
              'date': 2022, 'venue': bib['venue']}
        with patch.object(check, 'REPO', self.root):
            self.assertFalse(check.check_bibliographic_consistency(str(wiki.relative_to(self.root)), fm))
            sidecar.write_text('changed')
            self.assertTrue(check.check_bibliographic_consistency(str(wiki.relative_to(self.root)), fm))

    def test_reingest_reuses_archived_sidecars_without_network(self):
        bib = self.resolve()['bibliographic']
        ip.persist_bibliographic_metadata(self.extract, bib)
        with patch.object(ip, 'REPO', self.root), patch.object(re_ingest, 'REPO', self.root), \
                patch.object(re_ingest, 'TEMP_REINGEST', self.root / 'reingest'), \
                patch.object(eb, 'fetch', side_effect=AssertionError('no network')):
            state = re_ingest.new_state_for_reingest('fixture', 'extract/paper.md')
            staged = self.root / state['extract_dir']
            self.assertIsNotNone(eb.replay(state['bibliographic_meta']['external_receipt'], MD, staged))
            (self.extract / bib['external_receipt']['files'][0]['path']).write_text('changed')
            with self.assertRaises(ValueError):
                re_ingest.new_state_for_reingest('fixture', 'extract/paper.md')

    def test_api_workspace_locks_bibliography_and_manifest(self):
        bib = self.resolve()['bibliographic']
        review = ip.review_bibliographic_metadata(bib, MD, agent_workspace=True)
        skeleton = self.extract / 'skeleton.md'
        skeleton.write_text('---\ntitle: Locked\n---\n# Locked\n')
        state = {'transaction_id': 'test', 'status': 'extract', 'extract_dir': 'extract',
                 'source': 'inbox/paper.pdf', 'bibliographic_meta': bib, 'errors': []}
        generated = '<<<BIBLIOGRAPHIC>>>\n{"year":1999}\n<<<WIKI>>>\n# Content\n<<<SLOTS>>>\n三元组:\n本论文 | 核心方法 | Test\n'
        with patch.object(ip, 'REPO', self.root), patch.object(ip, 'ingest_mode', return_value='api'), \
                patch.object(ip.inbox_state, 'save'), \
                patch.object(ip, 'call_text', return_value={'ok': True, 'text': generated, 'history': []}) as call:
            ok, message = ip.execute_api_paper_workspace(state, review, self.extract / 'paper.md',
                skeleton_bundle=(skeleton, skeleton.read_text(), ''))
        self.assertTrue(ok, message)
        self.assertEqual(call.call_count, 1)  # Wiki/content only; no bibliographic generation.
        self.assertEqual(state['bibliographic_meta']['year'], '2022')
        self.assertEqual(state['agent_workspace']['artifact_bundle_sha256'], state['artifact_bundle_sha256'])


if __name__ == '__main__':
    unittest.main()

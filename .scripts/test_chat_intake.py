#!/usr/bin/env python3
"""Isolated regressions: chat input uses inbox transactions but never owns the original."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import inbox_plan
import inbox_source_policy as policy
import ingest_inbox as intake
import ingest_pipeline as pipeline
import wg


class ChatIntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.inbox = self.repo / 'inbox'
        self.inbox.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module in (intake, pipeline, inbox_plan, wg):
            self.stack.enter_context(patch.object(module, 'REPO', self.repo))
        self.stack.enter_context(patch.object(intake, 'INBOX', self.inbox))
        self.stack.enter_context(patch.object(intake.agent_task, 'ingest_backend', return_value='agent'))
        self.stack.enter_context(patch.object(intake.sf, 'ensure_index', return_value=None))
        self.stack.enter_context(patch.object(intake.sf, 'lookup_exact', return_value=None))

    def original(self, *, in_inbox=False, text='Exact user document.\r\n  Keep spacing.\n'):
        path = (self.inbox if in_inbox else self.repo) / 'document.txt'
        path.write_bytes(text.encode('utf-8'))
        return path

    def cleanup(self, source):
        extraction = self.repo / 'temp' / 'test-extraction'
        extraction.mkdir(parents=True, exist_ok=True)
        def trash(path):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        with patch.object(pipeline.trash_util, 'trash_path', side_effect=trash):
            pipeline._cleanup_sources({'source': str(source.relative_to(self.repo)),
                                       'extract_dir': str(extraction.relative_to(self.repo))})
        self.assertFalse(extraction.exists())

    def test_external_original_survives_success_cleanup(self):
        original = self.original()
        before = original.read_bytes()
        staged, receipt = intake.stage_chat_input(source=str(original))
        self.assertEqual(staged.read_bytes(), before)
        self.assertEqual(receipt['source_policy'], 'retain_original')
        self.assertEqual(receipt['cleanup_scope'], 'staged_copy_only')
        self.cleanup(staged)
        self.assertFalse(staged.exists())
        self.assertEqual(original.read_bytes(), before)

    def test_inbox_original_excluded_from_scan_and_plan_even_after_edit(self):
        original = self.original(in_inbox=True)
        staged, _ = intake.stage_chat_input(source=str(original))
        self.assertNotEqual(staged, original)
        self.assertTrue(policy.is_retained(self.repo, original))
        self.assertEqual(intake.scan_inbox(), [staged])
        plan = inbox_plan.build_plan(self.inbox)
        self.assertEqual(plan['retained_sources'], ['inbox/document.txt'])
        self.assertEqual(len(plan['items']), 1)
        self.cleanup(staged)
        original.write_text('User edited the original after ingestion.', encoding='utf-8')
        self.assertEqual(intake.scan_inbox(), [])
        self.assertEqual(inbox_plan.build_plan(self.inbox)['items'], [])
        self.assertFalse(inbox_plan.build_plan(self.inbox)['routing']['batch_eligible'])

    def test_retention_guard_covers_existing_transaction_cleanup(self):
        original = self.original(in_inbox=True)
        before = original.read_bytes()
        policy.retain_original(self.repo, original)
        self.cleanup(original)
        self.assertEqual(original.read_bytes(), before)

    def test_ordinary_inbox_cleanup_unchanged(self):
        original = self.original(in_inbox=True)
        self.cleanup(original)
        self.assertFalse(original.exists())

    def test_verbatim_text_preserves_utf8_bom_crlf_and_trailing_whitespace(self):
        content = '\ufeff# 原文\r\n\r\n甲  乙\t \r\n\n'.encode('utf-8')
        staged, receipt = intake.stage_chat_input(content=content, import_name='原文.md')
        self.assertEqual(staged.read_bytes(), content)
        self.assertEqual(receipt['binary_sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(receipt['origin'], 'chat_text')
        self.assertIsNone(receipt['source_path'])
        self.assertEqual(receipt['action'], 'saved_verbatim')

    def test_empty_invalid_text_and_unsafe_names_never_stage(self):
        for content, name in [(b'', 'a.txt'), (b' \r\n', 'a.txt'), (b'\xff', 'a.txt'),
                              (b'a', '../escape.txt'), (b'a', '.hidden.md'),
                              (b'a', 'facts-pending.md'), (b'a', 'a.pdf')]:
            with self.subTest(content=content, name=name), self.assertRaises(ValueError):
                intake.stage_chat_input(content=content, import_name=name)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_copies_never_reuse_or_overwrite_same_named_original(self):
        original = self.original(in_inbox=True)
        first, _ = intake.stage_chat_input(source=str(original))
        second, _ = intake.stage_chat_input(source=str(original))
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, original)
        self.assertEqual(first.read_bytes(), original.read_bytes())
        self.assertEqual(second.read_bytes(), original.read_bytes())

    def test_exact_duplicate_only_cleans_owned_copy(self):
        original = self.original(in_inbox=True)
        staged, _ = intake.stage_chat_input(source=str(original))
        raw = self.repo / 'academic/raw/fixture.txt'
        raw.parent.mkdir(parents=True)
        raw.write_bytes(original.read_bytes())  # Synthetic temporary repository only.
        match = {'raw_path': 'academic/raw/fixture.txt',
                 'binary_sha256': hashlib.sha256(original.read_bytes()).hexdigest()}
        with patch.object(intake.trash_util, 'trash_path', side_effect=lambda p: p.unlink()):
            receipt = intake.cleanup_exact_duplicate(staged, match)
            self.assertEqual(receipt['status'], 'trashed')
            with self.assertRaisesRegex(ValueError, '原件'):
                intake.cleanup_exact_duplicate(original, match)
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), raw.read_bytes())

    def test_protected_original_cannot_enter_normal_file_path(self):
        original = self.original(in_inbox=True)
        policy.retain_original(self.repo, original)
        with self.assertRaisesRegex(ValueError, '--keep-source'):
            intake._resolve_inbox_file(str(original))

    def test_retention_survives_original_path_becoming_a_symlink(self):
        original = self.original(in_inbox=True)
        policy.retain_original(self.repo, original)
        other = self.repo / 'other.txt'
        other.write_text('Other user file.')
        original.unlink()
        original.symlink_to(other)
        self.assertTrue(policy.is_retained(self.repo, original))
        self.assertEqual(intake.scan_inbox(), [])
        self.cleanup(original)
        self.assertTrue(original.is_symlink())
        self.assertEqual(other.read_text(), 'Other user file.')

    def test_symlink_attachment_is_rejected_without_staging(self):
        original = self.original()
        link = self.inbox / 'linked.txt'
        link.symlink_to(original)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            intake.stage_chat_input(source=str(link))
        self.assertEqual(list(self.inbox.glob('*-chat-*')), [])
        self.assertTrue(original.exists())

    def test_corrupt_retention_policy_fails_closed(self):
        original = self.original(in_inbox=True)
        policy.retain_original(self.repo, original)
        next((self.inbox / '.source-retention').glob('*.json')).write_text('{}')
        with self.assertRaises(ValueError):
            intake.scan_inbox()
        with self.assertRaises(ValueError):
            self.cleanup(original)
        self.assertTrue(original.exists())

    def test_retention_directory_symlink_rejected_before_copy(self):
        original = self.original(in_inbox=True)
        other = self.repo / 'other'
        other.mkdir()
        (self.inbox / '.source-retention').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            intake.stage_chat_input(source=str(original))
        self.assertEqual(list(other.iterdir()), [])
        self.assertTrue(original.exists())

    def test_classification_resume_binds_staged_text_not_stdin_or_full_inbox(self):
        (self.inbox / 'unrelated.txt').write_text('Unrelated item.')
        stdin = types.SimpleNamespace(buffer=io.BytesIO('会议安排\r\n'.encode('utf-8')))
        stdout = io.StringIO()
        with patch.object(sys, 'argv', ['ingest_inbox.py', '--stdin', '--run', '--subproject', 'admin']), \
                patch.object(sys, 'stdin', stdin), contextlib.redirect_stdout(stdout), \
                patch.object(intake.subprocess, 'run', side_effect=AssertionError('must classify first')):
            intake.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload['status'], 'prepared')
        self.assertEqual(payload['total'], 1)
        command = payload['agent_task']['commands']['resume']
        self.assertIn('--file', command)
        self.assertNotIn('--stdin', command)
        self.assertNotIn('--import-file', command)
        self.assertNotIn('unrelated.txt', command)
        staged = [p for p in self.inbox.glob('*-chat-*.txt')][0]
        self.assertIn(staged.name, command)
        self.assertEqual(staged.read_bytes(), '会议安排\r\n'.encode('utf-8'))

    def test_copy_failure_never_leaves_partial_input_or_modifies_original(self):
        original = self.original(in_inbox=True)
        before = original.read_bytes()
        def partial_copy(source, target):
            target.write(b'partial')
            raise OSError('injected copy failure')
        with patch.object(intake.shutil, 'copyfileobj', side_effect=partial_copy):
            with self.assertRaises(OSError):
                intake.stage_chat_input(source=str(original))
        self.assertEqual(list(self.inbox.glob('*-chat-*')), [])
        self.assertEqual(original.read_bytes(), before)

    def test_receipt_failure_removes_only_owned_copy(self):
        original = self.original(in_inbox=True)
        before = original.read_bytes()
        with patch.object(intake, '_write_json_atomic', side_effect=OSError('injected receipt failure')):
            with self.assertRaises(OSError):
                intake.stage_chat_input(source=str(original))
        self.assertEqual(list(self.inbox.glob('*-chat-*')), [])
        self.assertEqual(original.read_bytes(), before)

    def test_failed_or_prepared_dispatch_keeps_original_and_recoverable_copy(self):
        original = self.original(in_inbox=True)
        before = original.read_bytes()
        for status in ('failed', 'prepared'):
            with self.subTest(status=status):
                stdout = io.StringIO()
                process = types.SimpleNamespace(returncode=1 if status == 'failed' else 0,
                    stdout=json.dumps({'status': status, 'transaction_id': 'chat-fixture-123'}), stderr='')
                with patch.object(sys, 'argv', ['ingest_inbox.py', '--run', '--file',
                        str(original), '--keep-source', '--subproject', 'admin']), \
                        patch.object(intake.subprocess, 'run', return_value=process) as run, \
                        patch.object(intake, 'run_post_ingest_maintenance', return_value={'status': 'no_action'}), \
                        patch.object(intake.inbox_state, 'load', return_value=None), \
                        contextlib.redirect_stdout(stdout):
                    if status == 'failed':
                        with self.assertRaises(SystemExit):
                            intake.main()
                    else:
                        intake.main()
                payload = json.loads(stdout.getvalue())
                self.assertEqual(payload['status'], status)
                self.assertEqual(payload['intake']['source_policy'], 'retain_original')
                staged = self.repo / payload['intake']['staged_path']
                self.assertTrue(staged.exists())
                self.assertEqual(staged.read_bytes(), before)
                self.assertEqual(original.read_bytes(), before)
                command = run.call_args.args[0]
                self.assertIn(str(staged.relative_to(self.repo)), command)
                self.assertNotIn(str(original), command)

    def test_wrapper_preserves_inbox_attachment(self):
        original = self.original(in_inbox=True)
        args = types.SimpleNamespace(file=str(original), resume='', stdin=False,
                                     keep_source=True, name='', subproject='admin',
                                     document_type='', ocr_result='', allow_remote_ocr=False)
        with patch.object(wg, 'run_script', return_value=(0, '{"status":"prepared"}', '')) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            wg.cmd_ingest(args)
        command = run.call_args.args[0]
        self.assertIn('--keep-source', command)
        self.assertIn('--import-file', command)
        self.assertNotIn('--file', command)

    def test_wrapper_passes_stdin_bytes_without_newline_conversion(self):
        content = '一\r\n二  \n'.encode('utf-8')
        args = types.SimpleNamespace(file=None, resume='', stdin=True, keep_source=False,
                                     name='原文.md', subproject='admin', document_type='',
                                     ocr_result='', allow_remote_ocr=False)
        with patch.object(wg, 'run_script', return_value=(0, '{"status":"prepared"}', '')) as run, \
                patch.object(sys, 'stdin', types.SimpleNamespace(buffer=io.BytesIO(content))), \
                contextlib.redirect_stdout(io.StringIO()):
            wg.cmd_ingest(args)
        self.assertEqual(run.call_args.kwargs['input'], content)
        self.assertIn('--stdin', run.call_args.args[0])

    def test_run_script_supports_byte_exact_stdin(self):
        content = '原文\r\n \n'.encode('utf-8')
        rc, out, err = wg.run_script([sys.executable, '-c',
            'import sys; print(sys.stdin.buffer.read().hex())'], input=content)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip(), content.hex())


if __name__ == '__main__':
    unittest.main(verbosity=2)

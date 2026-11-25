#!/usr/bin/env python3
"""Real archive/restore and failure safety in isolated disposable fixtures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import temp_archive as ta
import workspace_checkpoint as cp


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'workspace'
        self.source = self.root / 'temp/closed'
        self.evidence = 'projects/demo/report.md'
        for rel, data in {
            'temp/closed/result.json': b'{"done":true}',
            'temp/closed/nested/referenced.bin': bytes(range(256)),
            'temp/closed/nested/other.txt': b'reproducible evidence',
            'temp/closed/run.py': b'# temp/closed/nested/other.txt',
            self.evidence: b'Completed: `temp/closed/nested/referenced.bin`',
            'temp/active/transaction.json': b'{"stage":2}',
            '.scripts/entry.py': b'pass',
        }.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        os.chmod(self.source / 'run.py', 0o750)
        os.symlink('/missing/external', self.source / 'external')
        self.quarantine = self.base / 'pending/closed'
        self.packed = self.root / 'archives/temporary/closed.zip'

    def archive(self):
        return ta.archive(self.root, 'closed', self.evidence, 'Completed experiment', self.quarantine)

    def test_roundtrip_preserves_links_modes_and_report_references(self):
        result = self.archive()
        self.assertEqual(result['original_files'], 4)
        self.assertTrue((self.source / 'nested/referenced.bin').is_file())
        self.assertFalse((self.source / 'nested/other.txt').exists())
        self.assertTrue((self.source / 'result.json').exists())
        target = self.base / 'recovered'
        ta.restore(self.packed, target, result['archive_sha256'])
        for p in self.quarantine.rglob('*'):
            rel = p.relative_to(self.quarantine)
            if p.is_symlink():
                self.assertEqual(os.readlink(p), os.readlink(target / rel))
            elif p.is_file():
                self.assertEqual(p.read_bytes(), (target / rel).read_bytes())
                self.assertEqual(p.stat().st_mode, (target / rel).stat().st_mode)
        with self.assertRaises(FileExistsError):
            ta.restore(self.packed, target)

    def test_reference_scan_excludes_self_environment_and_similar_names(self):
        for rel in ['.venv/lib/consumer.py', 'archives/consumer.py', 'temp/closed/consumer.py']:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('temp/closed/nested/other.txt')
        (self.root / '.scripts/entry.py').write_text('temp/closed-other/nested/other.txt')
        self.assertEqual(ta.references(self.root, 'closed')[0], ['nested/referenced.bin'])

    def test_runtime_reference_rejected(self):
        (self.root / '.scripts/entry.py').write_text('temp/closed/nested/other.txt')
        with self.assertRaisesRegex(ValueError, 'Code still references'):
            self.archive()
        self.assertFalse(self.quarantine.exists())

    def archive_upstream(self):
        upstream = self.root / 'temp/upstream'
        upstream.mkdir()
        (upstream / 'run.py').write_text("dependency = 'temp/closed/nested/referenced.bin'\n")
        report = self.root / 'projects/demo/upstream.md'
        report.write_text('Upstream experiment completed.')
        ta.archive(self.root, 'upstream', 'projects/demo/upstream.md',
                   'Closed upstream experiment', self.base / 'pending/upstream')
        return upstream

    def test_verified_archived_script_allows_dependency_chain(self):
        upstream = self.archive_upstream()
        result = self.archive()
        self.assertIn('temp/upstream/run.py', result['reference_documents'])
        self.assertIn('temp/upstream/run.py', (self.source / 'ARCHIVED.md').read_text())
        ta.restore(self.root / 'archives/temporary/upstream.zip', self.base / 'restored-upstream')
        self.assertEqual((upstream / 'run.py').read_bytes(),
                         (self.base / 'restored-upstream/run.py').read_bytes())

    def test_modified_archived_script_receipt_or_pack_blocks_retirement(self):
        upstream = self.archive_upstream()
        for path in [upstream / 'run.py', upstream / 'ARCHIVED.json',
                     self.root / 'archives/temporary/upstream.zip']:
            original = path.read_bytes()
            with self.subTest(path=path.name):
                if path.suffix == '.json':
                    doc = json.loads(original); doc['reason'] = 'changed'
                    path.write_text(json.dumps(doc))
                else:
                    path.write_bytes(original + b'\n# modified\n')
                with self.assertRaisesRegex(ValueError, 'Historical script'):
                    self.archive()
                self.assertTrue((self.source / 'nested/other.txt').is_file())
                self.assertFalse(self.quarantine.exists())
                self.assertFalse(self.packed.exists())
                path.write_bytes(original)

    def test_unverified_archive_marker_does_not_bypass_code_dependency(self):
        upstream = self.root / 'temp/upstream'
        upstream.mkdir()
        (upstream / 'run.py').write_text("'temp/closed/nested/other.txt'")
        (upstream / 'ARCHIVED.json').write_text('{}')
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.archive()
        self.assertFalse(self.quarantine.exists())

    def test_changes_during_validation_do_not_retire_source(self):
        original = ta.restore
        for changed in ['temp/closed/nested/other.txt', self.evidence, 'projects/demo/new.md']:
            with self.subTest(changed=changed):
                def mutate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    (self.root / changed).write_text('changed temp/closed/nested/other.txt')
                    return result
                with patch.object(ta, 'restore', mutate), self.assertRaisesRegex(ValueError, 'changed during'):
                    self.archive()
                self.assertTrue(self.source.is_dir())
                self.assertFalse(self.quarantine.exists())
                self.assertFalse(self.packed.exists())

    def test_stub_install_failure_rolls_back_original(self):
        rename = os.rename
        def fail_stub(src, dst, *args, **kwargs):
            if Path(src).name == 'stub':
                raise OSError('simulated install failure')
            return rename(src, dst, *args, **kwargs)
        with patch.object(ta.os, 'rename', fail_stub), self.assertRaisesRegex(OSError, 'install failure'):
            self.archive()
        self.assertTrue((self.source / 'nested/other.txt').is_file())
        self.assertFalse(self.quarantine.exists())
        ta.check_archive(self.packed)

    def test_paths_collisions_and_git_tracking_refused(self):
        for name in ['../escape', '/absolute', '.', '..', 'one/two']:
            with self.subTest(name=name), self.assertRaises(ValueError):
                ta.paths(self.root, name)
        self.quarantine.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            self.archive()
        self.quarantine.rmdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', 'temp/closed/result.json'], check=True)
        with self.assertRaisesRegex(ValueError, 'Tracked'):
            self.archive()

    def test_corrupt_and_unsafe_archives_rejected_before_extract(self):
        result = self.archive()
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            ta.restore(self.packed, self.base / 'bad-hash', '0' * 64)
        doc = ta.check_archive(self.packed)
        tampered = self.base / 'payload.zip'
        with zipfile.ZipFile(self.packed) as original, zipfile.ZipFile(tampered, 'w') as changed:
            for member in original.namelist():
                changed.writestr(member, b'changed' if member == 'result.json' else original.read(member))
        with self.assertRaisesRegex(ValueError, 'payload mismatch'):
            ta.restore(tampered, self.base / 'payload-target')
        for name, entries, extras in [
            ('escape', {'../escape': {'kind': 'dir'}}, []),
            ('root-entry', {'.': {'kind': 'dir'}}, []),
            ('link-parent', {'link': {'kind': 'link', 'target': '/tmp'}, 'link/child': {'kind': 'dir'}}, []),
            ('unexpected', {}, [('extra', b'bad')]),
            ('duplicate', {}, [(ta.META, b'{}')]),
            ('tampered', doc['entries'], [('result.json', b'changed')]),
        ]:
            p = self.base / (name + '.zip')
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr(ta.META, json.dumps({'schema': ta.SCHEMA, 'entries': entries}))
                for key, content in extras:
                    z.writestr(key, content)
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)):
                ta.restore(p, self.base / (name + '-target'))
            self.assertFalse((self.base / (name + '-target')).exists())

    def test_checkpoint_contains_archive_and_live_transaction(self):
        self.archive()
        bundle = self.base / 'checkpoint'
        cp.capture(self.root, bundle, quiescent=True)
        copied = bundle / 'workspace'
        self.assertEqual(cp.digest(self.packed), cp.digest(copied / self.packed.relative_to(self.root)))
        self.assertEqual((copied / 'temp/active/transaction.json').read_bytes(), b'{"stage":2}')
        self.assertTrue((copied / 'temp/closed/ARCHIVED.json').is_file())
        self.assertFalse((copied / 'temp/closed/nested/other.txt').exists())
        self.assertNotIn('pending', [p.name for p in copied.iterdir()])


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""Synthetic installation, discovery and execution boundary regressions."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import local_extension as ext


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.root = self.repo / 'private/extensions/example'
        self.root.mkdir(parents=True)
        self.data = {'schema': 'local-extension-v1', 'id': 'example', 'title': 'Example',
                     'summary': 'Synthetic local capability', 'version': '1.0.0',
                     'entry': 'main.py', 'guide': 'GUIDE.md', 'backend': 'agent', 'visibility': 'private'}
        (self.root / 'GUIDE.md').write_text('Synthetic guide')
        (self.root / 'main.py').write_text('raise RuntimeError("must not import during discovery")')
        self.save()

    def save(self):
        (self.root / 'extension.json').write_text(json.dumps(self.data))

    def test_empty_installation(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(ext.discover(Path(empty)), [])
            self.assertFalse((Path(empty) / 'private').exists())

    def test_discovery_does_not_execute(self):
        with patch.object(subprocess, 'run', side_effect=AssertionError('unexpected execution')):
            self.assertEqual(ext.discover(self.repo)[0]['id'], 'example')
            self.assertEqual(ext.package('example', self.repo)['version'], '1.0.0')

    def test_reject_escape_and_invalid_ids(self):
        for value in ('../main.py', '/tmp/main.py'):
            self.data['entry'] = value; self.save()
            with self.assertRaises(ValueError): ext.package('example', self.repo)
        for value in ('../example', 'a/b', 'Example', ''):
            with self.assertRaises(ValueError): ext.package(value, self.repo)

    def test_reject_file_and_directory_symlinks(self):
        (self.root / 'main.py').unlink()
        outside = self.repo / 'outside.py'; outside.write_text('print(1)')
        (self.root / 'main.py').symlink_to(outside)
        with self.assertRaises(ValueError): ext.package('example', self.repo)
        (self.repo / 'private/extensions/link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): ext.package('link', self.repo)

    def test_reject_unknown_schema_and_backend(self):
        for key, value in [('schema', 'other'), ('backend', 'api'), ('visibility', 'public'), ('id', 'different')]:
            original = self.data[key]; self.data[key] = value; self.save()
            with self.assertRaises(ValueError): ext.package('example', self.repo)
            self.data[key] = original

    def test_explicit_argv_not_shell(self):
        dangerous = '$(touch SHOULD_NOT_EXIST); `echo x`'
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(ext.dispatch('example', ['--', dangerous], self.repo), 7)
        self.assertEqual(run.call_args.args[0], [sys.executable, str(self.root / 'main.py'), dangerous])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_real_process_execution(self):
        (self.root / 'main.py').write_text('from pathlib import Path\nPath("executed").write_text("ok")\n')
        self.assertEqual(ext.dispatch('example', [], self.repo), 0)
        self.assertEqual((self.repo / 'executed').read_text(), 'ok')


if __name__ == '__main__': unittest.main()

#!/usr/bin/env python3
"""Real macOS virtual environments, atomic placement and portable recovery."""
import json
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import local_environments as le
import workspace_checkpoint as cp


@unittest.skipUnless(sys.platform == 'darwin', 'macOS atomic swap contract')
class LocalEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'workspace'
        (self.root / '.scripts').mkdir(parents=True)
        self.env = self.root / '.venv'
        self.store = self.base / 'runtimes'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(self.env)], check=True)
        self.script = self.env / 'bin/old-console'
        self.script.write_text('#!' + str(self.env / 'bin/python') + '\nprint("old entry works")\n')
        self.script.chmod(0o755)
        (self.env / 'binary-data').write_bytes(bytes(range(256)))

    def move(self):
        return le.externalize(self.root, '.venv', self.store)

    def test_atomic_move_and_reverse_preserve_bytes_and_original_shebang(self):
        before = cp.inventory(self.env, filter_runtime=False)[0]
        result = self.move()
        self.assertTrue(self.env.is_symlink())
        target = Path(result['external'])
        self.assertEqual(before, cp.inventory(target, filter_runtime=False)[0])
        self.assertEqual(subprocess.check_output([str(self.script)], text=True).strip(), 'old entry works')
        le.restore_in_tree(self.root, '.venv')
        self.assertFalse(self.env.is_symlink())
        self.assertFalse(os.path.lexists(target))
        self.assertEqual(before, cp.inventory(self.env, filter_runtime=False)[0])
        self.assertEqual(subprocess.check_output([str(self.script)], text=True).strip(), 'old entry works')

    def test_failed_swap_leaves_original_and_clears_only_reserved_link(self):
        _, _, target = le.locations(self.root, '.venv', self.store)
        with patch.object(le, 'swap', side_effect=OSError('simulated failure')):
            with self.assertRaisesRegex(OSError, 'simulated failure'):
                self.move()
        self.assertTrue(self.env.is_dir())
        self.assertFalse(self.env.is_symlink())
        self.assertFalse(os.path.lexists(target))
        self.assertEqual((self.env / 'binary-data').read_bytes(), bytes(range(256)))

    def test_failed_post_move_check_reverses_swap(self):
        original = le.smoke
        calls = 0
        def failing(path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('simulated post-move failure')
            return original(path)
        with patch.object(le, 'smoke', failing), self.assertRaisesRegex(RuntimeError, 'post-move'):
            self.move()
        self.assertFalse(self.env.is_symlink())
        self.assertEqual(subprocess.check_output([str(self.script)], text=True).strip(), 'old entry works')

    def test_dependency_capture_drift_leaves_runtime_in_tree(self):
        original = cp.environment_inventory
        def drift(*args):
            result = original(*args)
            (self.env / 'binary-data').write_bytes(b'concurrent package change')
            return result
        with patch.object(cp, 'environment_inventory', drift):
            with self.assertRaisesRegex(ValueError, 'changed while recording'):
                self.move()
        self.assertFalse(self.env.is_symlink())
        self.assertEqual((self.env / 'binary-data').read_bytes(), b'concurrent package change')

    def test_collision_inside_store_and_tracked_files_are_refused(self):
        with self.assertRaises(ValueError):
            le.externalize(self.root, '.venv', self.root / 'inside')
        _, _, target = le.locations(self.root, '.venv', self.store)
        target.mkdir(parents=True)
        (target / 'owned').write_text('keep')
        with self.assertRaises(FileExistsError):
            self.move()
        self.assertEqual((target / 'owned').read_text(), 'keep')
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.venv/binary-data'], check=True)
        with self.assertRaisesRegex(ValueError, 'Tracked'):
            le.externalize(self.root, '.venv', self.base / 'other-store')

    def test_checkpoint_omits_runtime_link_without_following_it(self):
        result = self.move()
        saved = le.descriptors(self.root)['.venv']
        # Move the real environment again, making the link broken: capture must
        # use the in-workspace metadata, not inspect the external target.
        Path(result['external']).rename(self.base / 'offline-runtime')
        (self.root / 'academic/raw').mkdir(parents=True)
        os.symlink('/unavailable/raw-source', self.root / 'academic/raw/alias')
        os.symlink('/unavailable/ordinary', self.root / 'ordinary-link')
        bundle = self.base / 'bundle'
        doc = cp.capture(self.root, bundle, quiescent=True)
        self.assertFalse(os.path.lexists(bundle / 'workspace/.venv'))
        self.assertIn('.venv', doc['excluded'])
        self.assertEqual(doc['environments']['.venv'], saved)
        self.assertTrue((bundle / 'workspace' / le.MANIFEST).is_file())
        self.assertTrue((bundle / 'workspace/academic/raw/alias').is_symlink())
        self.assertTrue((bundle / 'workspace/ordinary-link').is_symlink())
        cp.verify_bundle(bundle)

    @unittest.skipUnless(shutil.which('uv'), 'uv required for real rebuild')
    def test_other_workspace_rebuild_and_no_overwrite(self):
        self.move()
        other = self.base / 'other-machine'
        (other / '.scripts').mkdir(parents=True)
        (other / '.project').mkdir()
        shutil.copy2(self.root / le.MANIFEST, other / le.MANIFEST)
        result = le.prepare(other, '.venv', self.store, python=sys.executable)
        self.assertTrue((other / '.venv').is_symlink())
        self.assertTrue(Path(result['external']).is_dir())
        with self.assertRaises(FileExistsError):
            le.prepare(other, '.venv', self.store, python=sys.executable)
        with self.assertRaisesRegex(ValueError, 'reverse swap'):
            le.restore_in_tree(other, '.venv')

    def test_bad_or_linked_manifest_is_rejected(self):
        self.move()
        p = self.root / le.MANIFEST
        d = json.loads(p.read_text())
        d['environments']['.venv']['packages'] = ['--index-url=https://invalid']
        p.write_text(json.dumps(d))
        with self.assertRaises(ValueError):
            cp.inventory(self.root)
        p.unlink()
        os.symlink('/missing/manifest', p)
        with self.assertRaises(ValueError):
            le.descriptors(self.root)

    def test_checkpoint_entry_externalizes_and_rolls_back_without_backup_config(self):
        output = io.StringIO()
        with patch.object(cp, 'ROOT', self.root), contextlib.redirect_stdout(output):
            self.assertEqual(cp.main(['--config', str(self.base / 'no-config'),
                                     'externalize-env', '.venv', '--store', str(self.store)]), 0)
            self.assertTrue(self.env.is_symlink())
            self.assertEqual(cp.main(['restore-env', '.venv']), 0)
        self.assertFalse(self.env.is_symlink())


if __name__ == '__main__':
    unittest.main()

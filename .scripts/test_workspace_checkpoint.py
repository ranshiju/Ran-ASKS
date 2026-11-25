#!/usr/bin/env python3
"""Destructive cases stay in isolated fixtures; real restic roundtrips when installed."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import workspace_checkpoint as cp


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        for path, text in {
            ".scripts/entry.py": "print('entry')\n", "academic/raw/original.txt": "原始事实\n",
            "academic/wiki/page.md": "[[raw/original]]\n", "private/raw/secret.txt": "private fixture\n",
            "temp/ingest/transaction.json": '{"stage": 2}', "projects/demo/notes/status.md": "resume here",
            ".env": "DUMMY_CREDENTIAL=fixture", ".venv/pyvenv.cfg": "home = /fixture",
            ".venv/bin/stub": "machine-specific", "temp/cache/__pycache__/x.pyc": "cache",
            "academic/raw/__pycache__/fact.bin": "keep even with cache name",
        }.items():
            p = self.source / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        os.symlink("original.txt", self.source / "academic/raw/alias.txt")
        self.git("init", "-q")
        self.git("add", ".scripts/entry.py", "academic/raw/original.txt")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        (self.source / ".scripts/entry.py").write_text("staged version\n")
        self.git("add", ".scripts/entry.py")
        (self.source / ".scripts/entry.py").write_text("unstaged version\n")
        self.connections = []
        for rel in ("cross-domain/graph.db", "private/graph.db", "projects/demo/.workspace/index.sqlite"):
            p = self.source / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(p)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE data (value TEXT)")
            conn.execute("INSERT INTO data VALUES ('committed WAL')")
            conn.commit()
            self.connections.append(conn)
        self.addCleanup(lambda: [c.close() for c in self.connections])

    def git(self, *args, source=None):
        return subprocess.check_output(["git", "-C", str(source or self.source), *args],
                                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})

    def capture(self, name="bundle"):
        bundle = self.root / name
        return bundle, cp.capture(self.source, bundle, quiescent=True)

    def test_capture_preserves_raw_git_private_and_wal(self):
        bundle, doc = self.capture()
        restored = bundle / "workspace"
        cp.verify_bundle(bundle)
        self.assertEqual(cp.digest(restored / "academic/raw/original.txt"), cp.digest(self.source / "academic/raw/original.txt"))
        for args in (("diff",), ("diff", "--cached"), ("status", "--porcelain", "--untracked-files=no")):
            self.assertEqual(self.git(*args), self.git(*args, source=restored))
        self.assertEqual((restored / "temp/ingest/transaction.json").read_text(), '{"stage": 2}')
        self.assertTrue((restored / "private/raw/secret.txt").is_file())
        self.assertFalse((restored / ".env").exists())
        self.assertFalse((restored / ".venv").exists())
        self.assertTrue((restored / "academic/raw/__pycache__/fact.bin").exists())
        for rel in doc["databases"]:
            self.assertEqual(cp.digest(self.source / rel), cp.digest(restored / rel))
            conn = cp.graph_lib.connect(restored / rel, read_only=True)
            self.assertEqual(conn.execute("SELECT value FROM data").fetchone()[0], "committed WAL")
            conn.close()

    def test_requires_quiescence_and_new_disjoint_target(self):
        with self.assertRaises(ValueError):
            cp.capture(self.source, self.root / "no")
        with self.assertRaises(ValueError):
            cp.capture(self.source, self.source / "nested", quiescent=True)
        bundle, _ = self.capture()
        with self.assertRaises(FileExistsError):
            cp.capture(self.source, bundle, quiescent=True)

    def test_drift_rejected(self):
        original = cp.shutil.copy2
        changed = False
        def mutate(src, dst, **kwargs):
            nonlocal changed
            result = original(src, dst, **kwargs)
            if not changed:
                (self.source / "academic/raw/original.txt").write_text("changed during capture")
                changed = True
            return result
        with patch.object(cp.shutil, "copy2", mutate), self.assertRaisesRegex(ValueError, "changed during"):
            self.capture()
        self.assertFalse((self.root / "bundle/checkpoint.json").exists())

    def test_tampering_and_extra_files_rejected(self):
        bundle, _ = self.capture()
        p = bundle / "workspace/academic/raw/original.txt"
        p.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "Hash mismatch"):
            cp.verify_bundle(bundle)

    def test_database_changes_during_snapshot_start_are_rejected(self):
        original = cp.graph_lib.connect
        changed = False
        def racing_connect(path, **kwargs):
            nonlocal changed
            conn = original(path, **kwargs)
            if not changed:
                self.connections[0].execute("INSERT INTO data VALUES ('concurrent writer')")
                self.connections[0].commit()
                changed = True
            return conn
        with patch.object(cp.graph_lib, "connect", racing_connect):
            with self.assertRaisesRegex(ValueError, "establishing database snapshots"):
                self.capture()

    def test_external_symlink_not_followed(self):
        outside = self.root / "external-secret"
        outside.write_text("external bytes must not be copied")
        os.symlink(str(outside), self.source / "private/external")
        bundle, doc = self.capture()
        self.assertEqual(doc["files"]["private/external"]["kind"], "link")
        self.assertEqual(doc["warnings"][0]["path"], "private/external")
        self.assertTrue((bundle / "workspace/private/external").is_symlink())

    def test_git_lock_and_linked_worktree_rejected(self):
        (self.source / ".git/index.lock").write_text("")
        with self.assertRaisesRegex(ValueError, "Git lock"):
            self.capture()
        (self.source / ".git/index.lock").unlink()
        shutil.rmtree(self.source / ".git")
        (self.source / ".git").write_text("gitdir: /external")
        with self.assertRaisesRegex(ValueError, "Linked Git"):
            self.capture()

    @unittest.skipUnless(shutil.which("restic"), "restic required for complete acceptance")
    def test_real_restic_incremental_roundtrip_and_lineage(self):
        config = self.root / "machine/config.json"
        password = self.root / "password"
        password.write_text("fixture only")
        password.chmod(0o600)
        cp.write_json(config, {"repository": str(self.root / "restic-repo"),
                              "password_file": str(password), "workspace_id": "fixture"})
        store = cp.Store(config)
        store.run("init")
        first = store.save(self.source, True)
        second = store.save(self.source, True)
        self.assertEqual(second["parent"], first["snapshot"])
        self.assertLess(second["summary"]["data_added"], first["summary"]["data_added"])
        target = self.root / "other-computer"
        receipt = store.restore("latest", target)
        self.assertEqual(receipt["snapshot"], second["snapshot"])
        self.assertEqual(self.git("diff", "--cached"), self.git("diff", "--cached", source=target))
        self.assertEqual(self.git("diff"), self.git("diff", source=target))
        with self.assertRaises(FileExistsError):
            store.restore("latest", target)
        with patch.object(cp, "verify_bundle", side_effect=ValueError("injected verification failure")):
            with self.assertRaisesRegex(RuntimeError, "retained staging"):
                store.restore("latest", self.root / "failed-restore")
        self.assertFalse((self.root / "failed-restore").exists())
        self.assertNotIn(str(self.root / "failed-restore"), store.state())
        real_run = store.run
        def interrupt_backup(*args, **kwargs):
            if args[0] == "backup":
                raise RuntimeError("injected transport interruption")
            return real_run(*args, **kwargs)
        with patch.object(store, "run", interrupt_backup):
            with self.assertRaisesRegex(RuntimeError, "transport interruption"):
                store.save(self.source, True)
        self.assertEqual(store.state()[str(self.source)], second["snapshot"])
        (target / "projects/demo/notes/status.md").write_text("worked on second computer")
        third = store.save(target, True)
        self.assertEqual(third["parent"], second["snapshot"])
        with self.assertRaisesRegex(ValueError, "Stale/unregistered"):
            store.save(self.source, True)
        older = self.root / "older-checkpoint"
        store.restore(first["snapshot"], older)
        with self.assertRaisesRegex(ValueError, "Stale/unregistered"):
            store.save(older, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

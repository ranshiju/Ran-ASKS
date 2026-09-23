#!/usr/bin/env python3
"""Synthetic private re-ingest transaction, isolation, rollback, and idempotence tests."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import ingest_check as ic
import wiki_locator as wl
import source_locator as sl
import private_reingest as pr

CANDIDATE = '''---
title: Synthetic private record
type: health-record
sources:
  - private/raw/health/synthetic.md
source_type: user-assertion
date: 2026-01-01
status: active
confidence: high
created: 2026-01-01
updated: 2026-09-17
---

## Navigation

Synthetic record navigation.

## Content

Synthetic private observation.[^r1]

[^r1]: private/raw/health/synthetic.md#L2
'''

SEMANTIC = '''三元组:
  本文件|涉及|合成主题

概念说明:
  合成主题|来自合成 Raw 的主题
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrivateReingestTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.public = self.root / 'cross-domain/graph.db'
        self.private = self.root / 'private/graph.db'
        for module, attr, value in [
            (gl, 'REPO', self.root), (gl, 'GRAPH_DB', self.public),
            (gl, 'PRIVATE_GRAPH_DB', self.private), (gl, 'PRIVATE_DIR', self.private.parent),
            (ic, 'REPO', self.root), (wl, 'REPO', self.root), (sl, 'REPO', self.root),
        ]:
            self.stack.enter_context(patch.object(module, attr, value))
        for db in (self.public, self.private):
            db.parent.mkdir(parents=True)
            conn = sqlite3.connect(db)
            gl.init_schema(conn)
            conn.close()
        self.page = 'private/wiki/health/synthetic'
        self.wiki = self.root / (self.page + '.md')
        self.raw = self.root / 'private/raw/health/synthetic.md'
        self.wiki.parent.mkdir(parents=True)
        self.raw.parent.mkdir(parents=True)
        self.raw.write_text('# Synthetic source\nSynthetic private observation.\n', encoding='utf-8')
        self.wiki.write_text(CANDIDATE.replace('updated: 2026-09-17', 'updated: 2026-01-01'), encoding='utf-8')

    def prepare_candidate(self, batch):
        result = pr.prepare(self.page, batch)
        workspace = self.root / result['workspace']
        (workspace / 'candidate.md').write_text(CANDIDATE, encoding='utf-8')
        (workspace / 'semantic.txt').write_text(SEMANTIC, encoding='utf-8')
        return result['transaction_id'], workspace

    def test_prepare_validate_commit_verify_and_isolation(self):
        txn, workspace = self.prepare_candidate('pilot')
        self.assertTrue(workspace.resolve().is_relative_to(self.root / 'private'))
        validated = pr.validate(txn)
        self.assertEqual(validated['status'], 'ready_to_commit')
        before = {path: digest(path) for path in (self.raw, self.public)}
        committed = pr.commit(txn)
        self.assertEqual(committed['status'], 'completed')
        self.assertEqual(digest(self.raw), before[self.raw])
        self.assertEqual(digest(self.public), before[self.public])
        with gl.connect(self.private, read_only=True) as conn:
            self.assertIsNotNone(conn.execute('SELECT 1 FROM nodes WHERE path=?', (self.page,)).fetchone())
            self.assertGreaterEqual(conn.execute(
                'SELECT COUNT(*) FROM edges WHERE subject=? OR object=?', (self.page, self.page)
            ).fetchone()[0], 1)
        report = pr.verify(txn)
        self.assertEqual(report['status'], 'verified')
        self.assertTrue(report['idempotent'])
        self.assertTrue(report['raw_unchanged'])
        self.assertTrue(report['public_unchanged'])
        backup_dir = (self.root / committed['receipt']).parent
        self.assertTrue((backup_dir / 'graph.db').exists())
        self.assertFalse((self.root / 'cross-domain/embeddings.db').exists())

    def test_commit_rejects_page_self_edge_and_rolls_back(self):
        result = pr.prepare(self.page, "self-edge")
        workspace = self.root / result["workspace"]
        (workspace / "candidate.md").write_text(CANDIDATE, encoding="utf-8")
        (workspace / "semantic.txt").write_text(
            "三元组:\n  本文件|来源|Synthetic private record\n", encoding="utf-8")
        pr.validate(result["transaction_id"])
        before_wiki = digest(self.wiki)
        with self.assertRaises(ValueError):
            pr.commit(result["transaction_id"])
        self.assertEqual(digest(self.wiki), before_wiki)
        with gl.connect(self.private, read_only=True) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM edges WHERE subject=? AND object=?",
                                           (self.page, self.page)).fetchone())

    def test_unicode_page_names_get_safe_private_workspaces(self):
        page = "private/wiki/metaphysics/貔貅部.md"
        raw = self.root / "private/raw/metaphysics/貔貅部.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("# Synthetic\n", encoding="utf-8")
        target = self.root / page
        target.parent.mkdir(parents=True)
        target.write_text(
            CANDIDATE.replace("type: health-record", "type: metaphysics-knowledge")
                     .replace("private/raw/health/synthetic.md", "private/raw/metaphysics/貔貅部.md"),
            encoding="utf-8")
        result = pr.prepare(page, "unicode")
        workspace = self.root / result["workspace"]
        self.assertEqual(workspace.name, "metaphysics-貔貅部")
        self.assertTrue(workspace.resolve().is_relative_to(self.root / "private"))

    def test_validation_rejects_source_template_and_domain_changes(self):
        txn, workspace = self.prepare_candidate('validation')
        candidate = workspace / 'candidate.md'
        original = candidate.read_text(encoding='utf-8')
        bad = original.replace('type: health-record', 'type: health-knowledge')
        candidate.write_text(bad, encoding='utf-8')
        with self.assertRaises(ValueError):
            pr.validate(txn)
        candidate.write_text(original.replace('private/raw/health/synthetic.md', 'academic/raw/synthetic.md'), encoding='utf-8')
        with self.assertRaises(ValueError):
            pr.validate(txn)
        candidate.write_text(original + '\n\nhub_subtype: research-direction\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            pr.validate(txn)

    def test_commit_rolls_back_wiki_graph_log_and_raw_on_postcommit_failure(self):
        txn, _workspace = self.prepare_candidate('rollback')
        pr.validate(txn)
        with gl.connect(self.private, read_only=True) as conn:
            before_counts = (conn.execute('SELECT COUNT(*) FROM nodes').fetchone()[0],
                             conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0])
        before_wiki = digest(self.wiki)
        log = self.root / 'private/wiki/log.md'
        log.write_text('# private operation log\n', encoding='utf-8')
        before_log = digest(log)
        with patch.object(pr, '_append_log', side_effect=OSError('injected log failure')):
            with self.assertRaises(OSError):
                pr.commit(txn)
        with gl.connect(self.private, read_only=True) as conn:
            after_counts = (conn.execute('SELECT COUNT(*) FROM nodes').fetchone()[0],
                            conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0])
            self.assertIsNone(conn.execute('SELECT 1 FROM nodes WHERE path=?', (self.page,)).fetchone())
        self.assertEqual(after_counts, before_counts)
        self.assertEqual(digest(self.wiki), before_wiki)
        self.assertEqual(digest(log), before_log)
        self.assertEqual(digest(self.raw), digest(self.raw))

    def test_changed_raw_or_public_storage_blocks_commit(self):
        txn, _workspace = self.prepare_candidate('concurrency')
        pr.validate(txn)
        public_cache = self.root / 'cross-domain/embeddings.db'
        public_cache.write_bytes(b'unexpected')
        with self.assertRaises(ValueError):
            pr.commit(txn)
        public_cache.unlink()
        pr.validate(txn)
        self.raw.write_text('# Synthetic source\nChanged\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            pr.commit(txn)


if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Synthetic-only private boundary tests: no real private input or provider requests."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
import graph_lib as gl
import graph_ingest as gi
import embed_helper as eh
import node_semantics as ns
import hub_semantics as hs
import private_maintenance as pm
import sync_keyword_aliases as ska
import source_locator as sl
import wiki_locator as wl


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrivateIsolationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.public = self.root / 'cross-domain/graph.db'
        self.private = self.root / 'private/graph.db'
        for module, attr, value in [
            (gl, 'REPO', self.root), (gl, 'GRAPH_DB', self.public),
            (gl, 'PRIVATE_GRAPH_DB', self.private), (gl, 'PRIVATE_DIR', self.private.parent),
            (hs, 'REPO', self.root), (wl, 'REPO', self.root), (sl, 'REPO', self.root),
            (eh, 'EMBED_DB', None),
        ]:
            self.stack.enter_context(patch.object(module, attr, value))
        # Any unintended external access is a test failure, not a fallback API call.
        self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(eh, 'embed_batch', side_effect=lambda texts: np.array([[1., .5] for _ in texts], dtype=np.float32)))
        for db in (self.public, self.private):
            db.parent.mkdir(parents=True)
            conn = sqlite3.connect(db)
            gl.init_schema(conn)
            conn.close()
        self.page = 'private/wiki/health/synthetic'

    def connect(self, private=True):
        conn = gl.connect(self.private if private else self.public)
        self.addCleanup(conn.close)
        return conn

    def test_db_mismatch_rejected_before_lock_or_connect(self):
        for page, db in [(self.page, self.public), ('academic/wiki/papers/synthetic', self.private),
                         ('private/../academic/wiki/test', self.private)]:
            with self.subTest(page=page), patch.object(gl, 'graph_writer_lock') as lock, patch.object(gl, 'connect') as connect:
                with self.assertRaises(ValueError):
                    gi.cmd_ingest(Namespace(page=page, db=str(db)))
                lock.assert_not_called()
                connect.assert_not_called()
        self.assertEqual(gi._graph_db_path_for(Namespace(page=self.page, db=None)), self.private)
        with self.assertRaises(ValueError):
            gi._graph_db_path_for(Namespace(page=self.page, db=None, graph_plan_out=str(self.root / 'temp/plan.json')))

    def test_staged_input_files_must_share_the_graph_domain(self):
        for field in ('page_file', 'raw_source_override', 'knowledge_ir', 'semantic', 'citations', 'triples'):
            for page, db, staged in [(self.page, self.private, self.root / 'temp/input.json'),
                                      ('academic/wiki/papers/test', self.public, self.private.parent / 'input.json')]:
                with self.subTest(field=field, page=page), self.assertRaises(ValueError):
                    gi._graph_db_path_for(Namespace(page=page, db=db, **{field: str(staged)}))

    def test_symlink_database_and_source_escape(self):
        link = self.private.parent / 'outside.db'
        link.symlink_to(self.public)
        with self.assertRaises(ValueError):
            gi._graph_db_path_for(Namespace(page=self.page, db=link))
        self.root.joinpath('academic/raw').mkdir(parents=True)
        self.root.joinpath('private/raw').symlink_to(self.root / 'academic/raw', target_is_directory=True)
        with self.assertRaises(ValueError):
            gl.validate_graph_target('private/raw/synthetic.md', self.private)

    def test_direct_node_writer_rejects_other_domain(self):
        conn = self.connect()
        with self.assertRaises(ValueError):
            gl.ensure_node(conn, 'academic/raw/synthetic', 'Synthetic', 'raw')
        public = self.connect(False)
        with self.assertRaises(ValueError):
            gl.ensure_node(public, self.page, 'Synthetic', 'page')

    def test_private_source_cannot_reference_public_raw(self):
        target = self.root / (self.page + '.md')
        target.parent.mkdir(parents=True)
        target.write_text('---\ntitle: Synthetic\ntype: health-record\nsources:\n  - academic/raw/synthetic.md\n---\n')
        with patch.object(gi, '_connect_for') as connect:
            with self.assertRaises(ValueError):
                gi.cmd_ingest(Namespace(page=self.page, db=None))
            connect.assert_not_called()

    def test_embedding_cache_isolation_and_scope_restoration(self):
        public = self.connect(False)
        private = self.connect()
        with gl.graph_scope(public):
            eh.embed_cached_batch(['SYNTHETIC PUBLIC'])
        public_cache = self.root / 'cross-domain/embeddings.db'
        before = digest(public_cache)
        with gl.graph_scope(private):
            eh.embed_cached_batch(['SYNTHETIC PRIVATE'])
            self.assertEqual(set(ns._cached_vectors(['SYNTHETIC PRIVATE', 'SYNTHETIC PUBLIC'])), {'SYNTHETIC PRIVATE'})
            with gl.graph_scope(public):
                self.assertEqual(eh._get_embed_db(), public_cache)
            self.assertEqual(eh._get_embed_db(), self.private.parent / 'embeddings.db')
            with patch.object(eh, 'EMBED_DB', public_cache):
                with self.assertRaises(ValueError):
                    eh.embed_cached_batch(['SHOULD NOT BE WRITTEN'])
        self.assertEqual(digest(public_cache), before)
        self.assertEqual(eh._get_embed_db(), public_cache)
        with self.assertRaises(RuntimeError):
            with gl.graph_scope(private):
                raise RuntimeError('injected')
        self.assertIsNone(gl.scoped_graph_path())
        with sqlite3.connect(public_cache) as c:
            self.assertEqual(c.execute('SELECT text FROM embeddings').fetchall(), [('SYNTHETIC PUBLIC',)])

    def test_derived_file_symlinks_cannot_cross_storage(self):
        public_cache = self.public.parent / 'embeddings.db'
        public_cache.write_bytes(b'synthetic sentinel')
        private_cache = self.private.parent / 'embeddings.db'
        private_cache.symlink_to(public_cache)
        with gl.graph_scope(self.private), self.assertRaises(ValueError):
            eh._get_embed_db()
        private_cache.unlink()
        with patch.object(eh, 'EMBED_DB', private_cache), self.assertRaises(ValueError):
            eh._get_embed_db()
        public_todo = self.public.parent / 'abbreviation-todo.jsonl'
        public_todo.write_text('{}\n')
        (self.private.parent / public_todo.name).symlink_to(public_todo)
        with self.assertRaises(ValueError):
            ska.resolve_abbreviation_todo(self.connect())
        self.assertEqual(public_cache.read_bytes(), b'synthetic sentinel')
        self.assertEqual(public_todo.read_text(), '{}\n')

    def test_graph_scope_is_concurrent_task_local(self):
        import asyncio
        async def worker(db):
            with gl.graph_scope(db):
                await asyncio.sleep(0)
                return eh._get_embed_db()
        async def run():
            return await asyncio.gather(worker(self.private), worker(self.public))
        self.assertEqual(asyncio.run(run()), [self.private.parent / 'embeddings.db',
                                              self.public.parent / 'embeddings.db'])
        self.assertIsNone(gl.scoped_graph_path())

    def test_retirement_rejects_backup_or_log_symlink_before_writing(self):
        hub, _, _ = self.make_legacy_hub()
        plan = pm.retire(hub)
        (self.private.parent / 'outputs').symlink_to(self.public.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            pm.retire(hub, apply=True, expected_hash=plan['plan_hash'])
        self.assertFalse((self.public.parent / 'maintenance').exists())
        (self.private.parent / 'outputs').unlink()
        log = self.private.parent / 'wiki/log.md'
        log.unlink()
        log.symlink_to(self.public)
        before = digest(self.public)
        with self.assertRaises(ValueError):
            pm.retire(hub, apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(digest(self.public), before)

    def test_node_semantics_direct_calls_inherit_private_graph(self):
        conn = self.connect()
        gl.ensure_node(conn, 'synthetic-entity', 'Synthetic entity', 'entity')
        conn.commit()
        observed = []
        def fake(texts):
            observed.append(eh._get_embed_db())
            return None
        with patch.object(ns, '_embed_queries', side_effect=fake):
            ns.semantic_search(conn, 'synthetic query')
        self.assertTrue(observed)
        self.assertEqual(set(observed), {self.private.parent / 'embeddings.db'})
        self.assertIsNone(gl.scoped_graph_path())

    def test_private_abbreviation_resolution_leaves_public_todo_unchanged(self):
        conn = self.connect()
        gl.ensure_node(conn, 'Synthetic Private(SPR)', 'Synthetic', 'entity')
        conn.commit()
        public_todo = self.public.parent / 'abbreviation-todo.jsonl'
        private_todo = self.private.parent / 'abbreviation-todo.jsonl'
        data = json.dumps({'object': 'SPR'}) + '\n'
        public_todo.write_text(data)
        private_todo.write_text(data)
        before = digest(public_todo)
        self.assertEqual(ska.resolve_abbreviation_todo(conn), (1, 0))
        self.assertEqual(digest(public_todo), before)
        self.assertEqual(private_todo.read_text(), '')

    def test_private_arxiv_and_catch_all_rejected(self):
        conn = self.connect()
        with patch.object(gi, 'load_arxiv_direction_scopes') as scopes, patch.object(gi, 'append_catch_all_keywords') as catch:
            for function, args in [
                (gi.ensure_research_hub, (conn, 'Synthetic Physics', self.page)),
                (gi.assign_keyword_hubs, (conn, ['Synthetic'], [], self.page)),
                (gi.assign_keyword_hubs_meeting_admin, (conn, ['Synthetic'], self.page)),
            ]:
                with self.assertRaises(ValueError):
                    function(*args)
            scopes.assert_not_called()
            catch.assert_not_called()
        self.assertFalse((self.private.parent / 'wiki/hubs').exists())

    def make_legacy_hub(self):
        hub = 'private/wiki/hubs/synthetic-legacy'
        file = self.root / (hub + '.md')
        file.parent.mkdir(parents=True)
        file.write_text('---\ntitle: Synthetic Legacy\ntype: topic-hub\nhub_subtype: research-direction\n---\n\n'
                        '# Synthetic Legacy\n\n> arXiv 物理分类研究方向\n\n## 关键词\n\n- Synthetic concept\n')
        (self.private.parent / 'wiki/log.md').write_text('# Synthetic log\n')
        raw = self.private.parent / 'raw/synthetic.md'
        raw.parent.mkdir()
        raw.write_text('Synthetic immutable raw')
        conn = self.connect()
        gl.ensure_node(conn, hub, 'Synthetic Legacy', 'hub')
        conn.commit()
        conn.close()
        return hub, file, raw

    def test_retirement_is_readonly_then_hash_bound_with_private_backup(self):
        hub, file, raw = self.make_legacy_hub()
        before = digest(self.private)
        raw_before = digest(raw)
        plan = pm.retire(hub)
        self.assertTrue(plan['eligible'])
        self.assertEqual(digest(self.private), before)
        with self.assertRaises(ValueError):
            pm.retire(hub, apply=True, expected_hash='stale')
        result = pm.retire(hub, apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(result['status'], 'completed')
        self.assertFalse(file.exists())
        self.assertEqual(digest(raw), raw_before)
        receipt = self.root / result['receipt']
        self.assertTrue(receipt.resolve().is_relative_to(self.private.parent))
        self.assertTrue((receipt.parent / 'graph.db').exists())
        with gl.connect(self.private, read_only=True) as conn:
            self.assertIsNone(conn.execute('SELECT path FROM nodes WHERE path=?', (hub,)).fetchone())

    def test_retirement_rejects_referenced_or_canonical_hub(self):
        hub, file, _ = self.make_legacy_hub()
        consumer = self.private.parent / 'wiki/consumer.md'
        consumer.write_text('[[hubs/synthetic-legacy]]')
        self.assertFalse(pm.retire(hub)['eligible'])
        consumer.unlink()
        file.write_text(file.read_text() + '\n## Scope\n\nSynthetic scope\n')
        self.assertFalse(pm.retire(hub)['eligible'])

    def test_retirement_rollback_restores_graph_wiki_log_and_raw(self):
        hub, file, raw = self.make_legacy_hub()
        plan = pm.retire(hub)
        log = self.private.parent / 'wiki/log.md'
        before = {p: p.read_bytes() for p in (file, log, raw, self.public)}
        with patch.object(pm, '_append_log', side_effect=OSError('injected log failure')):
            with self.assertRaises(OSError):
                pm.retire(hub, apply=True, expected_hash=plan['plan_hash'])
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)
        with gl.connect(self.private, read_only=True) as conn:
            self.assertIsNotNone(conn.execute('SELECT path FROM nodes WHERE path=?', (hub,)).fetchone())
        receipts = list((self.private.parent / 'outputs/maintenance').glob('*/receipt.json'))
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'rolled_back')

    def test_readonly_connection_never_migrates_or_creates_missing_db(self):
        old = self.private.parent / 'old.db'
        with sqlite3.connect(old) as conn:
            conn.execute('CREATE TABLE nodes(path TEXT PRIMARY KEY, type TEXT)')
        before = digest(old)
        with gl.connect(old, read_only=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM nodes').fetchone()[0], 0)
        self.assertEqual(digest(old), before)
        missing = self.private.parent / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            gl.connect(missing, read_only=True)
        self.assertFalse(missing.exists())

    def make_mixed_cache(self, *, wal=False):
        target = self.public.parent / 'embeddings.db'
        conn = sqlite3.connect(target)
        if wal:
            conn.execute('PRAGMA journal_mode=WAL')
        eh._ensure_cache_schema(conn)
        conn.execute('INSERT INTO embeddings VALUES (?,?,?,?)',
                     ('SYNTHETIC PRIVATE CACHE MARKER', b'synthetic-vector', None, 1))
        conn.execute('INSERT INTO node_texts VALUES (?,?,?)',
                     ('private/wiki/synthetic', 'SYNTHETIC PRIVATE CACHE MARKER', 1))
        conn.commit()
        conn.close()
        return target

    def test_mixed_cache_quarantine_is_hash_gated_and_clears_both_tables(self):
        cache = self.make_mixed_cache()
        before = digest(cache)
        graphs = {p: digest(p) for p in (self.public, self.private)}
        plan = pm.quarantine_public_cache()
        self.assertEqual(digest(cache), before)
        with self.assertRaises(ValueError):
            pm.quarantine_public_cache(apply=True, expected_hash='stale')
        self.assertEqual(digest(cache), before)
        result = pm.quarantine_public_cache(apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(result['remaining'], {'embeddings': 0, 'node_texts': 0})
        backup = (self.root / result['receipt']).parent / 'embeddings.db'
        self.assertTrue(backup.resolve().is_relative_to(self.private.parent))
        with gl.connect(backup, read_only=True) as conn:
            self.assertEqual(pm._cache_plan(conn)['plan_hash'], plan['plan_hash'])
        self.assertNotIn(b'SYNTHETIC PRIVATE CACHE MARKER', cache.read_bytes())
        for p, expected in graphs.items():
            self.assertEqual(digest(p), expected)

    def test_mixed_cache_quarantine_rolls_back_precommit_failure(self):
        cache = self.make_mixed_cache()
        plan = pm.quarantine_public_cache()
        def fail(conn, tables):
            conn.execute('DELETE FROM embeddings')
            raise OSError('injected cache write failure')
        with patch.object(pm, '_clear_cache_tables', side_effect=fail), self.assertRaises(OSError):
            pm.quarantine_public_cache(apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(pm.quarantine_public_cache()['plan_hash'], plan['plan_hash'])
        receipts = list((self.private.parent / 'outputs/maintenance').glob('cache-*/receipt.json'))
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'rolled_back')

    def test_mixed_cache_quarantine_truncates_wal(self):
        cache = self.make_mixed_cache(wal=True)
        plan = pm.quarantine_public_cache()
        result = pm.quarantine_public_cache(apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(result['remaining'], {'embeddings': 0, 'node_texts': 0})
        wal = Path(str(cache) + '-wal')
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)
        self.assertNotIn(b'SYNTHETIC PRIVATE CACHE MARKER', cache.read_bytes())

    def test_mixed_cache_quarantine_rejects_backup_escape(self):
        cache = self.make_mixed_cache()
        plan = pm.quarantine_public_cache()
        (self.private.parent / 'outputs').symlink_to(self.public.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            pm.quarantine_public_cache(apply=True, expected_hash=plan['plan_hash'])
        self.assertEqual(pm.quarantine_public_cache()['plan_hash'], plan['plan_hash'])
        self.assertFalse((self.public.parent / 'maintenance').exists())

    def test_synthetic_private_ingest_keeps_public_files_byte_identical(self):
        file = self.root / (self.page + '.md')
        raw = self.private.parent / 'raw/health/synthetic.md'
        file.parent.mkdir(parents=True)
        raw.parent.mkdir(parents=True)
        raw.write_text('# Synthetic source\n\nSynthetic private observation.\n')
        file.write_text('---\ntitle: Synthetic record\ntype: health-record\nsource_type: user-assertion\n'
                        'date: 2026-01-01\nstatus: active\nsources:\n  - private/raw/health/synthetic.md\n---\n\n'
                        '## Navigation\n\nSynthetic private record.\n\n## Content\n\nSynthetic private observation.[^r1]\n\n'
                        '[^r1]: private/raw/health/synthetic.md#L3\n')
        todo = self.public.parent / 'abbreviation-todo.jsonl'
        todo.write_text('{"object":"SPR"}\n')
        before = {p: p.read_bytes() for p in (self.public, todo, raw)}
        args = Namespace(page=self.page, db=str(self.private), clean=True, citations=None, triples=None, triples_json=None)
        with redirect_stdout(io.StringIO()) as output:
            gi.cmd_ingest(args)
        result = json.loads(output.getvalue())
        self.assertEqual(result['page'], self.page)
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data)
        self.assertFalse((self.public.parent / 'embeddings.db').exists())
        self.assertFalse((self.private.parent / 'wiki/hubs').exists())
        with gl.connect(self.private, read_only=True) as conn:
            first = conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0]
        with redirect_stdout(io.StringIO()):
            gi.cmd_ingest(args)
        with gl.connect(self.private, read_only=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edges').fetchone()[0], first)


if __name__ == '__main__':
    unittest.main(verbosity=2)

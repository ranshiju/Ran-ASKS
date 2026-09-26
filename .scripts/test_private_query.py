#!/usr/bin/env python3
"""Synthetic regressions for private scope through the shared query interfaces."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import graph_lib as gl
import query_actions as qa
import query_graph as qg
import source_locator as sl
import wiki_locator as wl
import wg


class PrivateQueryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.pub = self.root / 'cross-domain/graph.db'
        self.priv = self.root / 'private/graph.db'
        for module, name, value in [
            (qa, '_REPO', self.root), (gl, 'REPO', self.root), (gl, 'GRAPH_DB', self.pub),
            (gl, 'PRIVATE_GRAPH_DB', self.priv), (gl, 'PRIVATE_DIR', self.priv.parent),
            (sl, 'REPO', self.root), (wl, 'REPO', self.root), (wg, 'REPO', self.root),
        ]:
            self.stack.enter_context(patch.object(module, name, value))
        for domain, db, token in [('academic', self.pub, 'PUBLIC_ONLY'), ('private', self.priv, 'PRIVATE_ONLY')]:
            db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(db)
            gl.init_schema(conn)
            page = f'{domain}/wiki/records/example'
            raw = f'{domain}/raw/example.md'
            (self.root / raw).parent.mkdir(parents=True, exist_ok=True)
            (self.root / raw).write_text(f'# Source\n{token} source.\n')
            wiki = self.root / (page + '.md')
            wiki.parent.mkdir(parents=True, exist_ok=True)
            wiki.write_text(f'---\ntitle: {token}\ntype: health-record\nstatus: active\nsources:\n  - {raw}\n---\n\n## Navigation\n\nShared subject {token}.\n\n## Content\n\n{token} observation.[^r]\n\n[^r]: {raw}#L2\n')
            gl.ensure_node(conn, page, token, 'page')
            gl.ensure_node(conn, 'shared-subject', 'Shared subject', 'entity')
            conn.execute('INSERT INTO edges(subject,predicate,object,confidence) VALUES (?,?,?,?)',
                         (page, '涉及', 'shared-subject', '可追溯'))
            conn.commit()
            conn.close()

    def action(self, action, inputs, subproject='private'):
        result = qa.execute(action, inputs, subproject=subproject)
        self.assertTrue(result['ok'], result)
        return result['text']

    def test_same_recall_algorithm_stays_in_selected_scope_offline(self):
        before = {db: hashlib.sha256(db.read_bytes()).hexdigest() for db in (self.pub, self.priv)}
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')) as net:
            text = self.action('hybrid_recall', {'query': 'Shared subject', 'intent': 'relation'})
            self.assertIn('PRIVATE_ONLY', text)
            self.assertNotIn('PUBLIC_ONLY', text)
            self.assertEqual(json.loads(text)['domain'], 'private')
            text = self.action('wiki_recall', {'query': 'Shared subject'}, 'public')
            self.assertIn('PUBLIC_ONLY', text)
            self.assertNotIn('PRIVATE_ONLY', text)
        net.assert_not_called()
        for db, digest in before.items():
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), digest)
        self.assertFalse((self.root / 'private/embeddings.db').exists())
        self.assertFalse((self.root / 'cross-domain/embeddings.db').exists())

    def test_graph_command_receives_private_db(self):
        captured = []
        def fake_run(command, **kwargs):
            captured.append(command)
            from types import SimpleNamespace
            return SimpleNamespace(stdout='{}', stderr='', returncode=0)
        with patch.object(qa.subprocess, 'run', side_effect=fake_run):
            self.action('graph_search', {'term': 'Shared subject'})
        self.assertEqual(captured[0][-2:], ['--db', str(self.priv)])

    def test_raw_wiki_context_and_identity_share_private_scope(self):
        page = 'private/wiki/records/example.md'
        text = self.action('read_section', {'page': page, 'section': 'content'})
        self.assertIn('PRIVATE_ONLY', text)
        text = self.action('read_raw', {'locator': 'private/raw/example.md#L2'})
        self.assertIn('PRIVATE_ONLY', text)
        text = self.action('wiki_context', {'page': page, 'section': 'content'})
        self.assertIn('PRIVATE_ONLY', text)
        text = self.action('node_resolve', {'name': 'Shared subject'})
        self.assertIn('shared-subject', text)

    def test_explicit_scope_blocks_other_domain_and_nested_switch(self):
        for scope, other in [('private', 'academic'), ('public', 'private')]:
            result = qa.execute('read_raw', {'locator': f'{other}/raw/example.md#L2'}, subproject=scope)
            self.assertFalse(result['ok'])
            self.assertNotIn('source.', result['text'])
        with qa.query_scope('public'):
            result = qa.execute('hybrid_recall', {'query': 'Shared subject', 'domain': 'private'})
            self.assertFalse(result['ok'])
        with qa.query_scope('private'):
            result = qa.execute('wiki_recall', {'query': 'Shared subject', 'domain': 'academic'})
            self.assertFalse(result['ok'])
        self.assertIsNone(qa._QUERY_SCOPE.get())

    def test_symlink_and_traversal_rejected_in_both_directions(self):
        for domain, other, scope in [('private', 'academic', 'private'), ('academic', 'private', 'public')]:
            link = self.root / domain / 'raw/escape.md'
            link.symlink_to(self.root / other / 'raw/example.md')
            result = qa.execute('read_raw', {'locator': f'{domain}/raw/escape.md#L2'}, subproject=scope)
            self.assertFalse(result['ok'])
        result = qa.execute('read_raw', {'locator': 'private/../academic/raw/example.md#L2'}, subproject='private')
        self.assertFalse(result['ok'])

    def test_image_companion_cannot_cross_scope(self):
        image = self.root / 'private/raw/scan.jpg'
        image.write_bytes(b'synthetic not decoded')
        image.with_suffix('.md').symlink_to(self.root / 'academic/raw/example.md')
        result = qa.execute('read_raw', {'locator': 'private/raw/scan.jpg#L2'}, subproject='private')
        self.assertFalse(result['ok'])

    def test_extensionless_wiki_symlink_is_rejected_before_read(self):
        link = self.root / 'private/wiki/records/escape.md'
        link.symlink_to(self.root / 'academic/wiki/records/example.md')
        with patch.object(wl, 'read_wiki_locator', side_effect=AssertionError('must not read')) as reader:
            result = qa.execute('read_section', {'page': 'private/wiki/records/escape', 'section': 'content'}, subproject='private')
        self.assertFalse(result['ok'])
        self.assertIn('mismatch', result['error'])
        reader.assert_not_called()

    def test_private_db_symlink_to_public_is_rejected(self):
        self.priv.unlink()
        self.priv.symlink_to(self.pub)
        result = qa.execute('wiki_recall', {'query': 'Shared subject'}, subproject='private')
        self.assertFalse(result['ok'])
        self.assertNotIn('PUBLIC_ONLY', result['text'])

    def test_private_named_entity_with_slash_is_not_a_public_filepath(self):
        with patch.object(qa.ns, 'resolve_node', return_value={'decision': 'unmatched'}) as resolver:
            self.action('node_resolve', {'name': '概念甲/乙'})
        resolver.assert_called_once()

    def test_missing_db_is_not_created(self):
        self.priv.unlink()
        result = qa.execute('wiki_recall', {'query': 'Shared subject'}, subproject='private')
        self.assertFalse(result['ok'])
        self.assertFalse(self.priv.exists())
        with self.assertRaises(SystemExit):
            qg.connect(self.priv)
        self.assertFalse(self.priv.exists())

    def test_graph_query_connection_is_read_only(self):
        conn = qg.connect(self.priv)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute('DELETE FROM nodes')
        finally:
            conn.close()

    def test_wg_explicit_scope_and_private_domain_cli(self):
        for argv in [
            ['hybrid-recall', 'Shared subject', '--domain', 'private'],
            ['read-section', 'private/wiki/records/example.md#content', '--subproject', 'private'],
            ['read-raw', 'private/raw/example.md#L2', '--subproject', 'private'],
        ]:
            output = io.StringIO()
            with redirect_stdout(output):
                wg.main(argv)
            data = json.loads(output.getvalue())
            self.assertTrue(data['ok'], data)
            self.assertIn('PRIVATE_ONLY', output.getvalue())
            self.assertNotIn('PUBLIC_ONLY', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            wg.main(['read-raw', 'academic/raw/example.md#L2', '--subproject', 'private'])
        self.assertFalse(json.loads(output.getvalue())['ok'])

    def test_public_api_session_cannot_opt_into_private(self):
        import query_orchestrate as qo
        session = qo.QuerySession(query='Synthetic', mode='api', stage='evidence')
        session.record_strategy({'status': 'skipped', 'reason': 'synthetic test'})
        out = qo.execute_plan(session, [{'action': 'read_raw', 'input': {'locator': 'private/raw/example.md#L2'}}])
        self.assertTrue(out['results'])
        self.assertFalse(out['results'][0]['ok'])
        self.assertEqual(session.read_sources, [])
        self.assertNotIn('PRIVATE_ONLY', json.dumps(session.steps))

    def test_dsh_api_tools_cannot_read_private(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from dsh.tools import _qa_call
        text = _qa_call('read_raw', {'locator': 'private/raw/example.md#L2'})
        self.assertIn('mismatch', text)
        self.assertNotIn('PRIVATE_ONLY', text)


if __name__ == '__main__':
    unittest.main()

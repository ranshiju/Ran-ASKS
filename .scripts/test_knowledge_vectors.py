#!/usr/bin/env python3
"""Offline end-to-end KU discovery, projection integrity and recovery tests."""
import io
import json
from pathlib import Path
import socket
import sys
import unittest
import tempfile
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed_helper as eh
import graph_lib as gl
import knowledge_store as ks
import knowledge_vectors as kv
import query_actions as qa
import query_orchestrate as qo
import source_ingest as si
import source_locator as sl
import source_vectors as sv
import vector_windows as vw
import wg


class Response(io.BytesIO):
    status = 200


class KnowledgeVectorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(socket.socket, 'connect', side_effect=AssertionError('offline')))
        for mod, attr in ((qa, '_REPO'), (sl, 'REPO'), (wg, 'REPO')):
            self.stack.enter_context(patch.object(mod, attr, self.root))
        for attr, value in (('EMBED_DB', self.root/'cache.sqlite'), ('_API_BASE', 'https://example.invalid'), ('_MODEL', 'test')):
            self.stack.enter_context(patch.object(eh, attr, value))
        self.calls = []
        self.stack.enter_context(patch.object(eh.urllib.request, 'urlopen', side_effect=self.respond))
        path = self.root/'source.md'; path.write_text('# Target\nUnder condition A, method X may work.\n')
        self.source = si.ingest(path, repo=self.root)
        self.ref, _ = ks.evidence_ref(self.root, self.source['source_id'], self.source['parse_id'], 1, 2)

    def respond(self, request, **kw):
        data = json.loads(request.data); self.calls.append(data)
        vecs = [{'index': i, 'embedding': [1., 0.] if ('目标' in t or 'target' in t) else [0., 1.]}
                for i, t in enumerate(data['input'])]
        return Response(json.dumps({'data': vecs, 'usage': {'total_tokens': 10}}).encode())

    def create(self, title='目标', statement='Only under condition A; uncertainty remains.'):
        result = ks.commit(repo=self.root, operation_id=title, title=title, statement=statement,
                           evidence=[dict(self.ref, relation='limits')], reason='fixture',
                           unresolved=['Generalization unknown'], claim_mode='conjecture')
        return result['id']

    def search(self, query='target', **kw):
        return ks.search(query, repo=self.root, retrieval='hybrid', **kw)

    def test_cross_language_navigation_metadata_and_no_fact_mutation(self):
        uid = self.create(); other = self.create('irrelevant', 'Completely different concept.')
        paths = [p for p in self.root.rglob('*') if p.is_file() and ('academic' in p.parts)]
        frozen = {p: si.digest(p) for p in paths}
        baseline = ks.search('target', repo=self.root)
        self.assertEqual(baseline['candidates'], [])
        kv.build([uid, other], repo=self.root)
        result = self.search()
        hit = result['candidates'][0]
        self.assertEqual(hit['id'], uid)
        self.assertEqual(hit['status'], 'draft'); self.assertEqual(hit['claim_mode'], 'conjecture')
        self.assertEqual(hit['unresolved'], ['Generalization unknown'])
        self.assertEqual(hit['evidence_preview'][0]['relation'], 'limits')
        self.assertEqual(result['vector_channel']['status'], 'ready')
        self.assertEqual(ks.search('target', repo=self.root), baseline)
        self.assertEqual({p: si.digest(p) for p in frozen}, frozen)

    def test_stale_revision_same_revision_edit_deletion_and_namespace(self):
        uid = self.create(); kv.build([uid], repo=self.root); count = len(self.calls)
        path = ks.unit_path(self.root, uid); original = path.read_text()
        for changed in (original.replace('condition A', 'condition B'), original.replace('"revision": 1', '"revision": 2')):
            path.write_text(changed)
            result = self.search()
            self.assertEqual(result['vector_channel']['status'], 'no_current_vectors')
            self.assertTrue(result['vector_channel']['invalid_units'])
        path.write_text(original)
        with patch.object(eh, '_MODEL', 'different'):
            self.assertEqual(self.search()['vector_channel']['missing_units'], [uid])
        path.unlink()
        self.assertEqual(self.search()['candidates'], [])
        self.assertEqual(len(self.calls), count)

    def test_corruption_and_missing_index_disclosed_without_network(self):
        uid = self.create()
        self.assertEqual(self.search()['vector_channel']['status'], 'index_missing')
        kv.build([uid], repo=self.root); count = len(self.calls)
        for sql in ("UPDATE vectors SET input_sha256='bad'", "UPDATE manifests SET dimension=17",
                    "UPDATE vectors SET vector_sha256='bad'", "DELETE FROM vectors"):
            kv.build([uid], repo=self.root)
            db = kv._connection(self.root, write=True)
            with db: db.execute(sql)
            db.close()
            self.assertEqual(self.search()['vector_channel']['status'], 'no_current_vectors')
        (self.root/'academic/knowledge/vectors.sqlite').write_bytes(b'corrupt database')
        self.assertEqual(self.search()['vector_channel']['status'], 'index_unavailable')
        self.assertEqual(len(self.calls), count)

    def test_successful_batches_survive_later_failure(self):
        ids = [self.create(f'unit-{i}') for i in range(18)]
        attempts = 0
        def fail_second(request, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 2: raise OSError('interrupted')
            return self.respond(request, **kw)
        with patch.object(eh.urllib.request, 'urlopen', side_effect=fail_second):
            with self.assertRaises(eh.EmbeddingAPIError): kv.build(ids, repo=self.root)
        self.assertEqual(attempts, 2)
        self.assertFalse((self.root/'academic/knowledge/vectors.sqlite').exists())
        self.calls.clear()
        kv.build(ids, repo=self.root)
        self.assertEqual([len(c['input']) for c in self.calls], [2])
        with patch.object(eh.urllib.request, 'urlopen', side_effect=AssertionError('cached replay')):
            self.assertEqual(len(kv.build(ids, repo=self.root)['units']), 18)

    def test_changed_during_build_preserves_old_projection(self):
        uid = self.create(); kv.build([uid], repo=self.root)
        path = ks.unit_path(self.root, uid)
        before = (self.root/'academic/knowledge/vectors.sqlite').read_bytes()
        original = eh.embed_cached_batch
        def edit(*a, **kw):
            values = original(*a, **kw)
            path.write_text(path.read_text().replace('condition A', 'condition B'))
            return values
        with patch.object(eh, 'embed_cached_batch', side_effect=edit):
            with self.assertRaisesRegex(ValueError, 'changed'): kv.build([uid], repo=self.root)
        self.assertEqual((self.root/'academic/knowledge/vectors.sqlite').read_bytes(), before)

    def test_service_failure_dimensions_and_query_race_return_lexical(self):
        uid = self.create(); kv.build([uid], repo=self.root)
        baseline = ks.search('目标', repo=self.root)
        with patch.object(eh.urllib.request, 'urlopen', side_effect=OSError('unavailable')) as provider:
            result = self.search('目标')
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(result['vector_channel']['status'], 'query_failed')
            self.assertEqual(result['candidates'], baseline['candidates'])
        with patch.object(eh, 'embed_cached_batch', return_value=eh.require_numpy().array([[1., 0., 0.]])):
            self.assertEqual(self.search()['vector_channel']['status'], 'query_failed')
        original = eh.embed_cached_batch
        def edit(*a, **kw):
            values = original(*a, **kw)
            path = ks.unit_path(self.root, uid)
            path.write_text(path.read_text().replace('condition A', 'condition B'))
            return values
        with patch.object(eh, 'embed_cached_batch', side_effect=edit):
            self.assertEqual(self.search()['vector_channel']['status'], 'query_failed')

    def test_question_is_lexical_and_not_vector_coverage(self):
        uid = self.create(); kv.build([uid], repo=self.root)
        path = self.root/'academic/frontier/questions/Q-test.md'; path.parent.mkdir(parents=True)
        path.write_text(ks.render({'id': 'Q-test', 'kind': 'question', 'title': 'target question',
                                 'question': 'target remains unknown', 'status': 'open', 'statement': 'target remains unknown'}))
        before = path.read_bytes(); count = len(self.calls)
        result = self.search(kind='question')
        self.assertEqual(result['vector_channel']['status'], 'not_applicable')
        self.assertEqual(result['candidates'][0]['id'], 'Q-test')
        self.assertEqual(len(self.calls), count)
        result = self.search()
        question = next(c for c in result['candidates'] if c['id'] == 'Q-test')
        self.assertEqual(question['retrieval_ranks'], {'lexical': 1})
        self.assertNotIn('vector_match', question)
        self.assertEqual(result['vector_channel']['total_units'], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_private_invalid_ids_empty_and_limits_never_call_provider(self):
        uid = self.create(); kv.build([uid], repo=self.root); count = len(self.calls)
        for q, expected in (('', 'empty_query'), ('甲'*801, 'query_over_limit')):
            self.assertEqual(self.search(q)['vector_channel']['status'], expected)
        with self.assertRaises(ValueError): kv.build(['../../private/secret'], repo=self.root)
        with self.assertRaises(ValueError): self.search(kind='invalid')
        with self.assertRaises(ValueError): ks.search('x', repo=self.root, retrieval='invalid')
        self.assertFalse(qa.execute('knowledge_search', {'query': 'secret', 'retrieval': 'hybrid'}, subproject='private')['ok'])
        with patch.object(gl, 'private_graph_path', return_value=True):
            with self.assertRaises(ValueError): kv.build([uid], repo=self.root)
            with self.assertRaises(ValueError): self.search('secret')
        self.assertEqual(len(self.calls), count)

    def test_window_compatibility_long_units_one_vote_each(self):
        self.assertIs(sv.windows, vw.windows)
        body = '定义🙂条件\n' * 900
        chunks = list(vw.windows(body))
        self.assertEqual(''.join(t for _, _, t in chunks), body)
        self.assertTrue(all(len(t.encode()) <= 2400 and t == body[a:b] for a,b,t in chunks))
        uid = self.create(statement=body); other = self.create('another')
        kv.build([uid, other], repo=self.root)
        result = self.search()
        ids = [c['id'] for c in result['candidates']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {uid, other})

    def test_cli_api_dsh_and_actual_ku_then_raw_read(self):
        from dsh.tools import build_tools
        uid = self.create(); kv.build([uid], repo=self.root)
        kwargs = {'query': 'target', 'retrieval': 'hybrid'}
        self.assertNotIn('retrieval', qo._TOOL_SCHEMAS['knowledge_search']['required'])
        self.assertEqual(qo._TOOL_SCHEMAS['knowledge_search']['properties']['retrieval']['enum'], ['lexical', 'hybrid'])
        session = qo.QuerySession(query='target', mode='api')
        qo.execute_plan(session, [{'action': 'knowledge_search', 'input': kwargs}])
        self.assertFalse(session.read_sources)
        session.stage = 'evidence'
        ref = next(k for k,v in session.read_options.items() if v['action'] == 'knowledge_read')
        qo.execute_plan(session, [{'action': 'read_ref', 'input': {'ref': ref}}])
        self.assertFalse(session.read_sources)
        ref = next(k for k,v in session.read_options.items() if v['action'] == 'read_raw')
        qo.execute_plan(session, [{'action': 'read_ref', 'input': {'ref': ref}}])
        self.assertTrue(session.read_sources)
        args = wg.build_parser().parse_args(['ku-search', 'target', '--retrieval', 'hybrid'])
        stream = io.StringIO()
        with redirect_stdout(stream): self.assertEqual(wg.cmd_knowledge_query(args), 0)
        self.assertEqual(json.loads(stream.getvalue())['result']['candidates'][0]['id'], uid)
        tool = next(t for t in build_tools() if t.name == 'knowledge_search')
        self.assertEqual(tool.input_schema['properties']['retrieval']['default'], 'lexical')
        with patch('dsh.tools._qa_call', return_value='ok') as call:
            tool.execute_fn(kwargs)
            self.assertEqual(call.call_args.args[1]['retrieval'], 'hybrid')


if __name__ == '__main__':
    unittest.main()

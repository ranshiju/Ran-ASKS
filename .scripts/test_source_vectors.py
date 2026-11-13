#!/usr/bin/env python3
"""Offline source scope, vector integrity and actual reader/adapter regression."""
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embed_helper as eh
import graph_lib as gl
import query_actions as qa
import query_orchestrate as qo
import source_ingest as si
import source_locator as sl
import source_vectors as sv
import wg


class Response(io.BytesIO):
    status = 200


class SourceVectorTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("offline")))
        for mod, attr in ((qa, "_REPO"), (sl, "REPO"), (wg, "REPO")):
            self.stack.enter_context(patch.object(mod, attr, self.root))
        self.stack.enter_context(patch.object(eh, "EMBED_DB", self.root / 'cache.sqlite'))
        self.stack.enter_context(patch.object(eh, "_API_BASE", 'https://example.invalid'))
        self.stack.enter_context(patch.object(eh, "_MODEL", 'test'))
        self.calls = []
        self.provider = self.stack.enter_context(patch.object(eh.urllib.request, "urlopen", side_effect=self.respond))

    def respond(self, request, **kwargs):
        data = json.loads(request.data)
        self.calls.append(data)
        vectors = [{'index': i, 'embedding': [1., 0.] if ('target' in t or '目标' in t) else [0., 1.]}
                   for i,t in enumerate(data['input'])]
        return Response(json.dumps({'data': list(reversed(vectors)), 'usage': {'total_tokens': 10}}).encode())

    def ingest(self, name, text):
        path = self.root / name
        path.write_text(text)
        return si.ingest(path, repo=self.root)['source_id']

    def fixture(self):
        return self.ingest('paper.md', '# Paper\n' + ('unrelated filler\n' * 31) +
                           '\nThe target state is independent of the optimized parameters.\n')

    def test_scope_filters_before_limit_and_empty_preserves_legacy(self):
        sid = self.ingest('long.md', '# Long\nneedle ' + 'irrelevant ' * 350)
        for n in range(8):
            self.ingest(f'short{n}.md', f'# Distractor {n}\nneedle\n')
        broad = si.search('needle', repo=self.root, topk=1)
        self.assertNotEqual(broad['candidates'][0]['source_id'], sid)
        narrow = si.search('needle', repo=self.root, topk=1, source_id=sid)
        self.assertEqual(narrow['candidates'][0]['source_id'], sid)
        self.assertEqual(broad, si.search('needle', repo=self.root, topk=1, source_id=''))
        self.assertEqual(self.calls, [])

    def test_scope_rejects_invalid_stale_even_no_hit(self):
        sid = self.fixture()
        with self.assertRaises(ValueError):
            si.search('nohit', repo=self.root, source_id='../private')
        conn = si.connect_index(self.root, write=True)
        with conn:
            conn.execute('DELETE FROM sources WHERE source_id=?', (sid,))
        conn.close()
        with self.assertRaisesRegex(ValueError, 'missing/stale'):
            si.search('nohit', repo=self.root, source_id=sid)

    def test_missing_index_and_valid_no_hit_distinct(self):
        sid = self.fixture()
        valid = si.search('nohit', repo=self.root, source_id=sid)
        self.assertFalse(valid['issues'])
        si.index_path(self.root).unlink()
        missing = si.search('nohit', repo=self.root, source_id=sid)
        self.assertFalse(missing['index_available'])
        self.assertTrue(missing['issues'])

    def test_windows_preserve_multibyte_text_without_truncation(self):
        body = '甲乙丙🙂' * 3000
        parts = list(sv.windows(body))
        self.assertEqual(''.join(t for _,_,t in parts), body)
        for a,b,t in parts:
            self.assertEqual(body[a:b], t)
            self.assertLessEqual(len(t.encode()), sv.MAX_BYTES)

    def test_hybrid_cross_language_reads_bound_raw_and_reuses_cache(self):
        sid = self.fixture()
        before = {p:p.read_bytes() for p in (self.root/'academic/raw').rglob('*') if p.is_file()}
        built = sv.build([sid], repo=self.root)
        self.assertTrue(any(e.get('usage') for e in built['events']))
        self.assertFalse(si.search('目标的条件', repo=self.root, source_id=sid)['candidates'])
        result = si.search('目标的条件', repo=self.root, source_id=sid, retrieval='hybrid', topk=1)
        self.assertEqual(result['vector_channel']['status'], 'ready')
        hit = result['candidates'][0]
        self.assertIn('target', qa.execute('read_raw', {'locator': hit['locator']})['text'])
        count = len(self.calls)
        sv.build([sid], repo=self.root)
        replay = si.search('目标的条件', repo=self.root, source_id=sid, retrieval='hybrid', topk=1)
        self.assertEqual(replay['candidates'], result['candidates'])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(before, {p:p.read_bytes() for p in (self.root/'academic/raw').rglob('*') if p.is_file()})

    def test_partial_coverage_does_not_build_missing_sources(self):
        sid = self.fixture()
        missing = self.ingest('other.md', '# Other\nother subject\n')
        sv.build([sid], repo=self.root)
        result = si.search('目标', repo=self.root, retrieval='hybrid')
        self.assertEqual(result['vector_channel']['status'], 'partial')
        self.assertEqual(result['vector_channel']['missing_sources'], [missing])
        self.assertLessEqual(sum(c['source_id'] == sid for c in result['candidates']), 2)

    def test_projection_corruption_model_change_and_missing_are_disclosed_without_calls(self):
        sid = self.fixture()
        self.assertEqual(si.search('目标', repo=self.root, retrieval='hybrid')['vector_channel']['status'], 'index_missing')
        sv.build([sid], repo=self.root)
        count = len(self.calls)
        db = sv._connection(self.root, write=True)
        with db:
            db.execute("UPDATE vectors SET input_sha256='wrong'")
        db.close()
        bad = si.search('目标', repo=self.root, retrieval='hybrid')
        self.assertEqual(bad['vector_channel']['status'], 'no_current_vectors')
        self.assertTrue(bad['vector_channel']['invalid_sources'])
        with patch.object(eh, '_MODEL', 'changed'):
            other = si.search('目标', repo=self.root, retrieval='hybrid')
            self.assertTrue(other['vector_channel']['missing_sources'])
        self.assertEqual(len(self.calls), count)

    def test_failed_build_preserves_published_projection_and_stops_auth(self):
        sid = self.fixture()
        sv.build([sid], repo=self.root)
        vector_path = self.root/'academic/sources/vectors.sqlite'
        before = vector_path.read_bytes()
        other = self.ingest('another.md', '# New\nnever cached input\n')
        error = urllib.error.HTTPError('https://example.invalid', 401, 'denied', {}, io.BytesIO(b'no'))
        events = []
        with patch.object(eh.urllib.request, 'urlopen', side_effect=error) as request:
            with self.assertRaises(eh.EmbeddingAPIError):
                sv.build([other], repo=self.root, events=events)
            self.assertEqual(request.call_count, 1)
        self.assertEqual(vector_path.read_bytes(), before)
        self.assertEqual(events[-1]['http_status'], 401)
        self.assertEqual(events[-1]['status'], 'error')

    def test_query_failure_returns_lexical_and_zero_retries(self):
        sid = self.fixture()
        sv.build([sid], repo=self.root)
        with patch.object(eh.urllib.request, 'urlopen', side_effect=OSError('unavailable')) as provider:
            result = si.search('target', repo=self.root, source_id=sid, retrieval='hybrid')
            self.assertTrue(result['candidates'])
            self.assertEqual(result['vector_channel']['status'], 'query_failed')
            self.assertEqual(provider.call_count, 1)

    def test_response_indices_and_count_are_validated(self):
        vectors = eh.embed_batch(['other', 'target'], retries=0)
        self.assertEqual(vectors.tolist(), [[0.,1.], [1.,0.]])
        for data in ([{'index': 0, 'embedding':[1,0]}], [{'index':0,'embedding':[1,0]}, {'index':0,'embedding':[0,1]}]):
            with patch.object(eh.urllib.request, 'urlopen', return_value=Response(json.dumps({'data':data}).encode())):
                with self.assertRaises(eh.EmbeddingAPIError):
                    eh.embed_batch(['a','b'], retries=0)

    def test_private_overlong_and_empty_never_send_queries(self):
        sid = self.fixture()
        sv.build([sid], repo=self.root)
        count = len(self.calls)
        self.assertEqual(si.search('甲'*2401, repo=self.root, retrieval='hybrid')['vector_channel']['status'], 'query_over_limit')
        self.assertEqual(si.search('', repo=self.root, retrieval='hybrid')['vector_channel']['status'], 'empty_query')
        result = qa.execute('source_search', {'query':'secret', 'retrieval':'hybrid'}, subproject='private')
        self.assertFalse(result['ok'])
        with patch.object(gl, 'private_graph_path', return_value=True):
            with self.assertRaises(ValueError):
                sv.build([sid], repo=self.root)
        self.assertEqual(len(self.calls), count)

    def test_cli_api_dsh_optional_scope_and_read_ref(self):
        from dsh.tools import build_tools
        sid = self.fixture()
        sv.build([sid], repo=self.root)
        kwargs = {'query':'目标', 'source_id':sid, 'retrieval':'hybrid'}
        self.assertNotIn('source_id', qo._TOOL_SCHEMAS['source_search']['required'])
        session = qo.QuerySession(query='target', mode='api')
        qo.execute_plan(session, [{'action':'source_search', 'input':kwargs}])
        self.assertFalse(session.read_sources)
        self.assertTrue(session.read_options)
        session.stage = 'evidence'
        qo.execute_plan(session, [{'action':'read_ref', 'input':{'ref':next(iter(session.read_options))}}])
        self.assertTrue(session.read_sources)
        args = wg.build_parser().parse_args(['source-search','目标','--source-id',sid,'--retrieval','hybrid'])
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(wg.cmd_source_search(args), 0)
        self.assertEqual(json.loads(stream.getvalue())['result']['search_scope']['source_id'], sid)
        tool = next(t for t in build_tools() if t.name == 'source_search')
        self.assertNotIn('source_id', tool.input_schema['required'])
        with patch('dsh.tools._qa_call', return_value='ok') as call:
            tool.execute_fn(kwargs)
            self.assertEqual(call.call_args.args[1]['source_id'], sid)
            self.assertEqual(call.call_args.args[1]['retrieval'], 'hybrid')


if __name__ == '__main__':
    unittest.main()

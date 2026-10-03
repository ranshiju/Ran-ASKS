#!/usr/bin/env python3
"""Read-only graph, bounded selection, provenance and isolation regression."""
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import graph_lib as gl
import graph_visualize as gv
import wg


class GraphVisualizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.db = self.repo/'graph.db'
        conn = gl.connect(self.db); gl.init_schema(conn)
        nodes = [('academic/wiki/a','Topic A','page'),('academic/wiki/b','Topic B','hub'),
                 ('academic/wiki/c','Other C','page'),('academic/wiki/d','Isolated D','page')]
        conn.executemany('INSERT INTO nodes(path,title,type) VALUES (?,?,?)',nodes)
        a,b,c,d = [r[0] for r in nodes]; self.a,self.b,self.c,self.d = a,b,c,d
        for src,pred,dst in [(a,'supports',b),(a,'contrasts',b),(b,'uses',a),(b,'contains',c),(a,'相似',d),(a,'self',a)]:
            cur = conn.execute('INSERT INTO edges(subject,predicate,object,source) VALUES (?,?,?,?)',
                               (src,pred,dst,'academic/raw/proof.md#result'))
            conn.execute('INSERT INTO edge_evidence(edge_id,source) VALUES (?,?)',
                         (cur.lastrowid,'academic/raw/extra.md'))
        conn.commit();conn.close()
        for path in ('academic/wiki/a.md','academic/raw/proof.md','academic/raw/extra.md','private/raw/secret.md'):
            target = self.repo/path; target.parent.mkdir(parents=True,exist_ok=True);target.write_text('TEST ONLY')
        for obj,name,value in [(gv,'REPO',self.repo),(gl,'GRAPH_DB',self.db),(gl,'PRIVATE_GRAPH_DB',self.db)]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)

    def graph(self,*args):
        conn=gl.connect(self.db,read_only=True)
        try:return gv.build_graph(conn,gv.parser().parse_args(args))
        finally:conn.close()

    def test_direction_multiedges_sources_and_radius(self):
        graph=self.graph('--node',self.a,'--radius','1')
        self.assertEqual(set(graph),{self.a,self.b})
        self.assertEqual(graph.number_of_edges(self.a,self.b),2)
        self.assertEqual(graph.number_of_edges(self.b,self.a),1)
        self.assertEqual(graph.number_of_edges(self.a,self.a),1)
        self.assertEqual(len(next(iter(graph.edges(data=True)))[2]['sources']),2)
        self.assertIn(self.c,self.graph('--node',self.a,'--radius','2'))
        self.assertIn(self.d,self.graph('--node',self.a,'--sim'))

    def test_filters_applied_before_traversal_and_isolates_kept(self):
        graph=self.graph('--node',self.a,'--predicates','supports','--radius','2')
        self.assertEqual(graph.number_of_edges(),1)
        self.assertNotIn(self.c,graph)
        self.assertEqual(set(self.graph('--node',self.d)),{self.d})
        self.assertEqual(set(self.graph('--types','hub')),{self.b})

    def test_query_caps_and_invalid_input(self):
        graph=self.graph('--query','topic','--max-nodes','1','--max-edges','1')
        meta=graph.graph['selection']
        self.assertTrue(meta['truncated']);self.assertEqual(meta['omitted_seeds'],1)
        graph=self.graph('--node',self.a,'--max-edges','1')
        self.assertEqual(graph.number_of_edges(),1)
        self.assertEqual(graph.graph['selection']['omitted_edges_between_shown_nodes'],3)
        for args in [('--query',''),('--query','missing'),('--radius','5'),('--max-nodes','301')]:
            with self.assertRaises(ValueError):self.graph(*args)

    def test_link_resolution_respects_physical_domains(self):
        out=self.repo/'temp/graph.html'
        self.assertTrue(gv.source_link(self.a,out,'main')['href'].endswith('a.md'))
        self.assertIn('#result',gv.source_link('academic/raw/proof.md#result',out,'main')['href'])
        self.assertIsNone(gv.source_link('private/raw/secret.md',out,'main')['href'])
        self.assertIsNone(gv.source_link('academic/raw/proof.md',out,'private')['href'])
        link=self.repo/'academic/raw/escape.md';link.symlink_to(self.repo/'private/raw/secret.md')
        self.assertIsNone(gv.source_link('academic/raw/escape.md',out,'main')['href'])
        self.assertIsNone(gv.source_link('/etc/passwd',out,'main')['href'])

    def test_html_manifest_readonly_and_no_overwrite(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        args=gv.parser().parse_args(['--html','--node',self.a,'--output',str(self.repo/'temp/graph.html')])
        result=gv.run(args);data=json.loads(Path(result['manifest']).read_text())
        self.assertEqual(data['schema'],'graph-visualization-v1')
        self.assertEqual(len(data['edges']),4)
        html=Path(result['artifact']).read_text()
        self.assertIn('marker-end',html);self.assertIn('academic/raw/proof.md#result',html)
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):gv.run(args)
        self.assertEqual(Path(result['artifact']).read_text(),html)

    def test_private_output_and_symlink_guard(self):
        for target,scope in [('temp/private.html','private'),('academic/raw/g.html','main')]:
            args=gv.parser().parse_args(['--html','--graph',scope,'--output',str(self.repo/target)])
            with self.assertRaises(ValueError):gv.run(args)
        result=gv.run(gv.parser().parse_args(['--html','--graph','private']))
        self.assertTrue(Path(result['artifact']).is_relative_to(self.repo/'private/temp'))
        link=self.repo/'temp-link';link.symlink_to(self.repo/'private/temp')
        with self.assertRaises(ValueError):gv.output_path(gv.parser().parse_args(['--html','--output',str(link/'new.html')]))

    def test_missing_database_is_not_created(self):
        missing=self.repo/'missing.db'
        with patch.object(gl,'GRAPH_DB',missing):
            with self.assertRaises(FileNotFoundError):gv.run(gv.parser().parse_args(['--html']))
        self.assertFalse(missing.exists())

    def test_render_failure_cleans_own_output(self):
        out=self.repo/'temp/fail.png'
        with patch.object(gv,'render_png',side_effect=ValueError('TEST ONLY')):
            with self.assertRaises(ValueError):gv.run(gv.parser().parse_args(['--output',str(out)]))
        self.assertFalse(out.exists());self.assertFalse(out.with_suffix('.json').exists())

    def test_concurrent_artifact_is_preserved(self):
        out=self.repo/'temp/race.html';original=gv.payload
        def race(*args):
            out.parent.mkdir(parents=True,exist_ok=True);out.write_text('other process')
            return original(*args)
        with patch.object(gv,'payload',side_effect=race):
            with self.assertRaises(FileExistsError):gv.run(gv.parser().parse_args(['--html','--output',str(out)]))
        self.assertEqual(out.read_text(),'other process')

    def test_png_and_wg_forwarding(self):
        result=gv.run(gv.parser().parse_args(['--node',self.a,'--output',str(self.repo/'temp/test.png')]))
        self.assertTrue(Path(result['artifact']).read_bytes().startswith(b'\x89PNG'))
        buffer=io.StringIO()
        with patch.object(wg,'run_script',return_value=(0,json.dumps(result),'')) as run,redirect_stdout(buffer):
            rc=wg.main(['graph-visualize','--query','topic','--max-nodes','12','--html'])
        self.assertEqual(rc,0);self.assertTrue(json.loads(buffer.getvalue())['ok'])
        self.assertIn('--max-nodes',run.call_args.args[0]);self.assertIn('12',run.call_args.args[0])
        self.assertIn('--html',run.call_args.args[0])


if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""Offline isolated regression: author discovery is not a publication count."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent))
import author_works as aw
import graph_lib as gl

class AuthorWorksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        (self.root/'cross-domain').mkdir()
        self.conn = sqlite3.connect(self.root/'cross-domain/graph.db'); self.conn.row_factory = sqlite3.Row
        gl.init_schema(self.conn)
        self.person = 'academic/wiki/authors/ada'
        gl.ensure_node(self.conn, self.person, 'Ada Lovelace', 'people')
        self.conn.execute('INSERT INTO aliases(node_path,alias) VALUES (?,?)',(self.person,'A. Lovelace'))
        self.conn.commit()
    def tearDown(self):
        self.conn.close(); self.temp.cleanup()
    def paper(self, name, authors=None, text=None, status='current', source=None):
        page='academic/wiki/papers/'+name
        src=source or 'academic/raw/references/'+name+'/paper.md'
        p=self.root/(page+'.md');p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text('---\n'+yaml.safe_dump({'title':name,'authors':authors or [],'sources':[src],'status':status})+'---\n\n## Navigation\n')
        raw=self.root/src
        if text is not None:
            raw.parent.mkdir(parents=True,exist_ok=True);raw.write_text(text)
        gl.ensure_node(self.conn,page,name,'page',status=status);self.conn.commit()
        return page,src
    def result(self,**kwargs):return aw.inventory('A. Lovelace',repo=self.root,**kwargs)
    def test_metadata_without_edge_and_explicit_raw_gap(self):
        self.paper('one',['Ada Lovelace'],'# One\nAda Lovelace\n## Abstract\n')
        self.paper('two',[], '# Two\nA. Lovelace\n## Abstract\n')
        r=self.result(extra_sources=['academic/raw/references/two/paper.md']);self.assertEqual(r['counts']['identity_groups'],2)
        self.assertEqual(r['coverage']['metadata_matched_pages'],1);self.assertIsNone(r['publication_count'])
    def test_references_not_author_and_similar_name(self):
        self.paper('references',[], '# Other\nBob Smith\n## Abstract\nText\n## References\nAda Lovelace\n')
        self.paper('similar',[], '# Other\nAda Lovelacex\n## Abstract\n')
        self.assertEqual(self.result()['counts']['source_records'],0)
    def test_bidirectional_authorship_and_missing_text(self):
        p,_=self.paper('one',[])
        self.conn.execute('INSERT INTO edges(subject,predicate,object,confidence) VALUES (?,?,?,?)',(p,'作者',self.person,'可追溯'));self.conn.commit()
        r=self.result();self.assertEqual(r['counts']['source_records'],1)
        self.assertFalse(r['coverage']['enumeration_complete'])
    def test_doi_duplicate_and_conflicting_doi(self):
        for name,doi in [('a','10.1234/a'),('b','10.1234/a'),('c','10.1234/c')]:
            self.paper(name,['Ada Lovelace'],f'# Same title\nAda Lovelace\nDOI: {doi}\n## Abstract\n')
        r=self.result();self.assertEqual(r['counts']['identity_groups'],2)
        self.assertTrue(any(g['same_title_review_candidates'] for g in r['groups']))
    def test_deprecated_pages_not_default(self):
        self.paper('old',['Ada Lovelace'],'# Old\nAda Lovelace\n',status='deprecated')
        self.assertEqual(self.result()['counts']['source_records'],0)
    def test_budget_and_missing_db(self):
        self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\n')
        self.paper('b',['Ada Lovelace'],'# B\nAda Lovelace\n')
        self.assertTrue(self.result(max_pages=1)['coverage']['truncated'])
        with self.assertRaises(ValueError):aw.inventory('Ada',repo=self.root/'missing')
        self.assertFalse((self.root/'missing/cross-domain/graph.db').exists())
    def test_ambiguous_alias_rejected(self):
        gl.ensure_node(self.conn,'academic/wiki/authors/ada2','Ada Lovelace','people');self.conn.commit()
        with self.assertRaises(ValueError):aw.inventory('Ada Lovelace',repo=self.root)
    def test_private_and_escape_sources_rejected(self):
        self.paper('escape',['Ada Lovelace'],source='academic/raw/../../../outside.md')
        r=self.result();self.assertEqual(r['counts']['source_records'],0);self.assertTrue(r['coverage']['issues'])
    def test_supplement_and_own_doi_only(self):
        self.paper('supplement',['Ada Lovelace'],'# Supplemental material\nAda Lovelace\n## Abstract\nDOI: 10.1234/ref\n')
        r=self.result();self.assertEqual(r['records'][0]['category_hint'],'supplement');self.assertEqual(r['records'][0]['doi'],'')
    def test_cli_and_read_only(self):
        self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\n')
        db=self.root/'cross-domain/graph.db';before=db.read_bytes()
        p=subprocess.run([sys.executable,str(Path(aw.__file__)),'Ada Lovelace','--repo',str(self.root),'--compact'],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(json.loads(p.stdout)['counts']['identity_groups'],1)
        self.assertEqual(before,db.read_bytes())
    def test_supplement_not_merged_by_parent_doi(self):
        self.paper('main',['Ada Lovelace'],'# Main\nAda Lovelace\nDOI: 10.1234/a\n## Abstract\n')
        self.paper('supplement',['Ada Lovelace'],'# Supplementary Material\nAda Lovelace\nDOI: 10.1234/a\n## Abstract\n')
        self.assertEqual(self.result()['counts']['identity_groups'],2)
    def test_review_counts_exact_evidence_and_staleness(self):
        _,src=self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\nDOI: 10.1234/a\n## Abstract\n')
        r=self.result();review={'protocol':'author-works-review-v1','inventory_digest':r['inventory_digest'],
            'decisions':[{'id':r['records'][0]['id'],'category':'journal_research',
                'evidence':[{'locator':src+'#L2','quote':'Ada Lovelace'}]}]}
        self.assertEqual(aw.reviewed_counts(r,review,repo=self.root)['journal_articles'],1)
        review['decisions'][0]['evidence'][0]['quote']='Absent'
        with self.assertRaises(ValueError):aw.reviewed_counts(r,review,repo=self.root)
        review['inventory_digest']='old'
        with self.assertRaises(ValueError):aw.reviewed_counts(r,review,repo=self.root)
    def test_malformed_metadata_surfaces_issue(self):
        p,_=self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\n')
        (self.root/(p+'.md')).write_text('---\nauthors: [Ada Lovelace\n---\n')
        r=self.result();self.assertTrue(r['coverage']['issues']);self.assertFalse(r['coverage']['enumeration_complete'])

    def test_invalid_source_and_companion_binding_excluded(self):
        self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\n')
        for owner, name in [(aw.si, 'binding_for_target'), (aw.sl, 'companion_binding_for_target')]:
            with self.subTest(binding=name), patch.object(owner, name, return_value={'status':'invalid'}):
                r=self.result()
                self.assertEqual(r['counts']['source_records'],0)
                self.assertFalse(r['coverage']['enumeration_complete'])

    def test_malformed_sources_and_review_are_rejected(self):
        page,_=self.paper('a',['Ada Lovelace'],'# A\nAda Lovelace\n')
        r=self.result()
        for decisions in [[None], [{'id':[], 'category':'unknown'}],
                          [{'id':r['records'][0]['id'], 'category':'unknown', 'evidence':'bad'}]]:
            review={'protocol':'author-works-review-v1','inventory_digest':r['inventory_digest'],'decisions':decisions}
            with self.assertRaises(ValueError):aw.reviewed_counts(r,review,repo=self.root)
        (self.root/(page+'.md')).write_text('---\nauthors: [Ada Lovelace]\nsources: wrong\n---\n')
        self.assertTrue(self.result()['coverage']['issues'])

if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""Public analytics windows, path boundaries and unavailable-data semantics."""
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import gallery_metrics as metrics


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.catalog={'site_url':'https://example.org/gallery/','works':['one','two']}
        self.now=datetime(2026,11,12,12,tzinfo=timezone.utc)

    def row(self,date,path,views=2,visits=1):
        return {'dimensions':{'date':date,'requestPath':path},'count':views,'sum':{'visits':visits}}

    def test_utc_windows_and_only_approved_paths(self):
        result=metrics.aggregate([
            self.row('2026-11-12','/gallery/works/one/'),
            self.row('2026-11-06','/gallery/works/one/index.html',3,2),
            self.row('2026-11-05','/gallery/index.html',7,4),
            self.row('2026-10-14','/gallery/works/two/index.html',5,3),
            self.row('2026-10-13','/gallery/index.html',100,80),
            self.row('2026-11-12','/other/private'),
            self.row('2026-11-12','/gallery/index.html?q=private'),
            self.row('2026-11-12','/gallery/works/missing/index.html')],self.catalog,self.now)
        self.assertEqual(result['windows']['7'],{'page_views':5,'visits':3})
        self.assertEqual(result['windows']['30'],{'page_views':17,'visits':10})
        self.assertEqual([x['id'] for x in result['popular']],['one','two'])

    def test_failure_preserves_previous_time_and_sanitizes_private_fields(self):
        previous=metrics.aggregate([self.row('2026-11-12','/gallery/')],self.catalog,self.now)
        previous.update({'token':'PRIVATE','visitor_ip':'PRIVATE'})
        result=metrics.unavailable(previous,set(self.catalog['works']))
        self.assertEqual(result['status'],'stale')
        self.assertEqual(result['generated_at'],previous['generated_at'])
        self.assertNotIn('PRIVATE',str(result))
        self.assertEqual(metrics.unavailable(None,{'one'}),{'schema':metrics.SCHEMA,'status':'unavailable'})

    def test_zero_only_after_valid_empty_response_and_invalid_counts_rejected(self):
        empty=metrics.aggregate([],self.catalog,self.now)
        self.assertEqual(empty['status'],'empty')
        self.assertEqual(empty['windows']['30']['page_views'],0)
        for value in [-1,float('nan'),True,'7']:
            with self.assertRaises(ValueError):metrics.count(value)
        with self.assertRaises(ValueError):metrics.aggregate([{}]*metrics.LIMIT,self.catalog,self.now)

    def test_cumulative_rollover_overlap_correction_and_idempotence(self):
        old=metrics.aggregate([self.row('2026-10-14','/gallery/',5,3),
                               self.row('2026-11-12','/gallery/',2,1)],self.catalog,self.now)
        tomorrow=self.now+timedelta(days=1)
        fresh=metrics.aggregate([self.row('2026-11-12','/gallery/',4,2),
                                 self.row('2026-11-13','/gallery/',3,2)],self.catalog,tomorrow)
        merged=metrics.merge_history(fresh,old,self.catalog)
        self.assertEqual(merged['cumulative']['page_views'],12)
        self.assertEqual(merged['cumulative']['visits'],7)
        self.assertEqual(merged['windows']['30']['page_views'],7)
        self.assertEqual(merged['cumulative']['since'],'2026-10-14')
        self.assertEqual(merged['cumulative']['missing_days'],0)
        self.assertEqual(metrics.merge_history(fresh,merged,self.catalog),merged)
        # A provider revision to zero replaces the overlapping day's prior value.
        zero=metrics.aggregate([],self.catalog,tomorrow)
        revised=metrics.merge_history(zero,merged,self.catalog)
        self.assertEqual(revised['cumulative']['page_views'],5)

    def test_gaps_and_legacy_snapshots_do_not_invent_history(self):
        old=metrics.aggregate([self.row('2026-11-12','/gallery/',2,1)],self.catalog,self.now)
        later=self.now+timedelta(days=40)
        fresh=metrics.aggregate([self.row(later.date().isoformat(),'/gallery/',3,2)],self.catalog,later)
        merged=metrics.merge_history(fresh,old,self.catalog)
        self.assertEqual(merged['cumulative']['page_views'],5)
        self.assertEqual(merged['cumulative']['missing_days'],10)
        legacy={k:v for k,v in old.items() if k not in {'history','cumulative'}}
        migrated=metrics.merge_history(fresh,legacy,self.catalog)
        self.assertEqual(migrated,fresh)
        self.assertNotEqual(migrated['cumulative']['since'],old['cumulative']['since'])

    def test_history_is_sanitized_bound_and_preserved_on_failure(self):
        saved=metrics.aggregate([self.row('2026-11-12','/gallery/',7,3)],self.catalog,self.now)
        saved['history']['days'][-1]['visitor_ip']='PRIVATE'
        saved['cumulative']['page_views']=9999
        clean=metrics.clean_snapshot(saved,{'one','two'})
        self.assertEqual(clean['cumulative']['page_views'],7)
        self.assertNotIn('PRIVATE',str(clean))
        stale=metrics.unavailable(clean,{'one','two'})
        self.assertEqual(stale['cumulative'],clean['cumulative'])
        self.assertEqual(stale['history'],clean['history'])
        bad=json.loads(json.dumps(clean))
        bad['history']['days'].append(bad['history']['days'][0])
        with self.assertRaises(ValueError):metrics.clean_snapshot(bad,{'one','two'})
        bad=json.loads(json.dumps(clean));bad['history']['site_url']='https://other.example/'
        with self.assertRaisesRegex(ValueError,'different site'):
            metrics.merge_history(clean,bad,self.catalog)
        removed=metrics.aggregate([self.row('2026-11-12','/gallery/works/two/',9,4)],self.catalog,self.now)
        reduced={**self.catalog,'works':['one']}
        fresh=metrics.aggregate([self.row('2026-11-12','/gallery/works/one/',2,1)],reduced,self.now+timedelta(days=31))
        merged=metrics.merge_history(fresh,removed,reduced)
        self.assertEqual(merged['cumulative']['page_views'],9)
        self.assertNotIn('two',str(merged['popular']))

    def test_cli_reads_prior_on_success_and_stops_if_prior_cannot_be_read(self):
        previous=metrics.aggregate([self.row('2026-10-14','/gallery/',5,3)],self.catalog,self.now)
        new=metrics.aggregate([],self.catalog,self.now+timedelta(days=1))
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'gallery').mkdir()
            (root/'gallery/catalog.json').write_text(json.dumps(self.catalog))
            output=root/'metrics.json'
            with patch.object(metrics,'REPO',root),patch('sys.argv',['metrics','--reuse-published','--output',str(output)]):
                with patch.object(metrics,'urlopen',return_value=io.BytesIO(json.dumps(previous).encode())), \
                        patch.object(metrics,'fetch',return_value=new):
                    self.assertEqual(metrics.main(),0)
                self.assertEqual(json.loads(output.read_text())['cumulative']['page_views'],5)
                before=output.read_bytes()
                with patch.object(metrics,'urlopen',side_effect=OSError('offline')), \
                        patch.object(metrics,'fetch',side_effect=AssertionError('stop before new collection')):
                    self.assertEqual(metrics.main(),1)
                self.assertEqual(output.read_bytes(),before)


if __name__=='__main__':unittest.main()

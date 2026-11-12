#!/usr/bin/env python3
"""Public analytics windows, path boundaries and unavailable-data semantics."""
from datetime import datetime, timezone
import unittest
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


if __name__=='__main__':unittest.main()

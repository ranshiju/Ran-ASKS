#!/usr/bin/env python3
"""Offline regression for academic PPT structures, API receipts and library transactions."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch,Mock
from xml.etree import ElementTree as ET
import pptx_structure as st
import pptx_visual as v
import pptx_document as doc
import slide_library as lib
import visual_qa as qa
from test_pptx_document import fixture,render_source,render_page


def full_fixture(path):
    fixture(path)
    with zipfile.ZipFile(path) as z:data={n:z.read(n) for n in z.namelist()}
    P,A,R=st.NS['p'],st.NS['a'],st.NS['r']
    x=f'<p:xfrm xmlns:p="{P}" xmlns:a="{A}"><a:off x="10" y="20"/><a:ext cx="100" cy="60"/></p:xfrm>'
    def shape(sid,rect,text=''):
        a,b,c,d=rect
        return ET.fromstring(f'<p:sp xmlns:p="{P}" xmlns:a="{A}"><p:nvSpPr><p:cNvPr id="{sid}" name="shape"/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{a}" y="{b}"/><a:ext cx="{c}" cy="{d}"/></a:xfrm></p:spPr><p:txBody><a:p><a:pPr><a:defRPr sz="2400"><a:latin typeface="Arial"/></a:defRPr></a:pPr><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')
    slide=ET.fromstring(data['ppt/slides/slide2.xml']);tree=slide.find('p:cSld/p:spTree',st.NS)
    tree.insert(0,shape('b',(0,0,900,600)))
    tree.append(shape('r1',(0,0,450,300),'one'))
    tree.append(shape('r2',(225,0,450,300),'two'))
    table=tree.find('p:graphicFrame',st.NS);table.insert(0,ET.fromstring(x))
    data['ppt/slides/slide2.xml']=ET.tostring(slide)
    data['_rels/.rels']=f'<Relationships xmlns="{st.REL}"><Relationship Id="rId1" Type="{R}/officeDocument" Target="ppt/presentation.xml"/></Relationships>'.encode()
    data['[Content_Types].xml']=b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/></Types>'
    with zipfile.ZipFile(path,'w') as z:
        for n,b in data.items():z.writestr(n,b)


def result(mode='content'):
    return {'summary':'TEST-ONLY: synthetic visual response','verdict':'pass','supplement':'图片文字' if mode=='content' else '', 'regions':[], 'issues':[], 'limitations':[]}


class AcademicPPTTests(unittest.TestCase):
    def test_academic_report_topics_anchor_to_owning_page(self):
        import graph_ingest as graph
        import ingest_document as ingest
        self.assertEqual(graph._DOMAIN_PREDICATES['academic'],
                         (ingest.DOMAIN_CONFIG['academic']['nav_predicates'],
                          ingest.DOMAIN_CONFIG['academic']['kw_predicates']))
        for subdir in ('references', 'editorials'):
            page = f'academic/wiki/{subdir}/sample'
            triples, keywords, direction, _, _, directions = graph.parse_semantic_text(
                '三元组:\n本文档|作者|Example Speaker\n本文档|涉及|Example method\n', page)
            self.assertTrue(all(t['subject'] == page for t in triples))
            self.assertEqual(keywords, ['Example method'])
            self.assertIsNone(direction)
            self.assertEqual(directions, [])
        for subdir in ('papers', 'conferences', 'authors', 'hubs'):
            self.assertIsNone(graph._get_domain_from_path(f'academic/wiki/{subdir}/sample'))

    def test_source_date_does_not_borrow_report_or_advertisement_dates(self):
        import ingest_document as ingest
        text = '2026-07-11\n宣传活动：2026年8月23日\n发布日期：2026-06-22'
        self.assertEqual(ingest.extract_admin_date('inbox/ran.pptx', text), '')
        self.assertEqual(ingest.extract_admin_date('inbox/2026-09-01/ran.pptx', text), '2026-09-01')
        self.assertEqual(ingest.extract_admin_date('inbox/2026-09-02-ran.PPTX', text), '2026-09-02')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.repo=Path(self.tmp.name).resolve();self.source=self.repo/'inbox/source.pptx';self.source.parent.mkdir();full_fixture(self.source)
        self.dir=self.repo/'temp/analysis';self.env={'VISUAL_QA_API_BASE':'https://synthetic.invalid','VISUAL_QA_API_KEY':'TEST-ONLY','VISUAL_QA_MODEL':'TEST-ONLY','VISUAL_QA_FALLBACK_MODEL':''}
        for obj in (v,doc,lib):
            p=patch.object(obj,'REPO',self.repo);p.start();self.addCleanup(p.stop)
        for p in (patch.object(qa,'_prepare_source',side_effect=render_source),patch.object(qa,'render_page',side_effect=render_page),patch.object(qa,'load_visual_env',return_value=self.env)):
            p.start();self.addCleanup(p.stop)
        self.data=st.extract(self.source);self.manifest=doc.prepare(self.source,self.dir)
        self.image=self.dir/'pptx-renders/page-0001.png';self.page=self.data['pages'][0]

    def analyze(self,mode='content',call=None,**kw):
        return v.analyze_image(self.image,mode=mode,source_sha256=self.data['source_sha256'],page=self.page,
            call=call or (lambda *a:{'result':result(mode),'usage':{'total_tokens':10}}),**kw)

    def test_font_geometry_and_union(self):
        p=self.data['pages'][0];o=next(x for x in p['objects'] if x['id']=='r1')
        self.assertEqual(o['styles'][0]['font_size_pt'],24)
        self.assertEqual(o['styles'][0]['provenance']['sz'],'paragraph')
        self.assertEqual(o['styles'][0]['fonts']['latin']['theme_resolved'],'Arial')
        self.assertAlmostEqual(p['coverage']['ratio'],.375)
        self.assertEqual(p['coverage']['including_background_candidates_ratio'],1)
        self.assertEqual(p['coverage']['excluded'][0]['id'],'b')
        self.assertEqual(st.union_area([[0,0,10,10],[5,0,15,10]]),150)

    def test_rotated_group_transform(self):
        root=ET.fromstring(f'<p:grpSp xmlns:p="{st.NS["p"]}" xmlns:a="{st.NS["a"]}"><p:grpSpPr><a:xfrm rot="5400000"><a:off x="100" y="100"/><a:ext cx="200" cy="100"/><a:chOff x="0" y="0"/><a:chExt cx="100" cy="50"/></a:xfrm></p:grpSpPr></p:grpSp>')
        b,m,_=st.transform(root,st.IDENTITY)
        for actual,expected in zip(b,[150,50,250,250]):self.assertAlmostEqual(actual,expected)
        self.assertAlmostEqual(m[0][1],-2)

    def test_no_authorization_zero_network_and_text_only_action(self):
        call=Mock(side_effect=AssertionError('network forbidden'))
        r=self.analyze(call=call)
        self.assertEqual(r['status'],'not_checked');call.assert_not_called()

    def test_three_modes_cache_and_scope_binding(self):
        for mode in v.MODES:
            with self.subTest(mode=mode):
                call=Mock(return_value={'result':result(mode),'usage':{}})
                r=self.analyze(mode,call,allow_remote=True)
                self.assertEqual(r['status'],'complete')
                self.analyze(mode,call,allow_remote=True);self.assertEqual(call.call_count,1)
                v.validate_receipt(r,image=self.image,mode=mode,source_sha256=self.data['source_sha256'],page=self.page)
                with self.assertRaises(v.PPTVisualError):v.validate_receipt(r,image=self.image,mode=mode,source_sha256='changed',page=self.page)
                self.assertNotIn('TEST-ONLY',json.dumps(r['parameters'].get('api_key',None)))

    def test_reference_actually_sent_and_bound(self):
        other=self.dir/'pptx-renders/page-0002.png';call=Mock(return_value={'result':result('form-check'),'usage':{}})
        r=self.analyze('form-check',call,allow_remote=True,reference=other)
        self.assertEqual(call.call_args.args[1],[self.image.resolve(),other.resolve()])
        with self.assertRaises(v.PPTVisualError):v.validate_receipt(r,image=self.image,mode='form-check',source_sha256=self.data['source_sha256'],page=self.page)

    def test_failure_not_passed_and_no_implicit_retry(self):
        call=Mock(side_effect=RuntimeError('TEST_SECRET_PROVIDER_ECHO'))
        r=self.analyze(call=call,allow_remote=True)
        self.assertEqual(r['status'],'error');self.assertNotIn('TEST_SECRET_PROVIDER_ECHO',json.dumps(r))
        self.analyze(call=call,allow_remote=True);self.assertEqual(call.call_count,1)
        self.analyze(call=call,allow_remote=True,retry_failed=True);self.assertEqual(call.call_count,2)

    def test_schema_hallucinated_id_pass_conflict_and_bbox(self):
        for r in [dict(result(),verdict='certified'),dict(result(),limitations=[{'detail':'missing','critical':True}]),dict(result(),regions=[{'object_ids':['nonexistent'],'bbox':None,'description':'test'}])]:
            with self.assertRaises(v.PPTVisualError):v.validate_result(r,'content',{'r1'})
        r=result();r['regions']=[{'object_ids':['r1'],'bbox':[-.01,0,1.02,1],'description':'bleed'}]
        v.validate_result(r,'content',{'r1'});self.assertEqual(r['regions'][0]['bbox'],[0,0,1,1]);self.assertIn('bbox_unclipped',r['regions'][0])

    def test_api_companion_preserves_native_and_quotes_supplements(self):
        with patch.object(qa,'call_json_vision',return_value={'result':result(),'usage':{}}):
            a=doc.prepare_api_review(self.source,self.dir,allow_remote=True)
        self.assertEqual(a['status'],'complete')
        text,receipt,_=doc.validated(self.source,self.dir)
        self.assertIn('FIRST',text);self.assertIn('NOTES ONLY',text)
        self.assertIn('> 图片文字',text);self.assertIn('API视觉识读',text)
        self.assertEqual(receipt['reviewer']['kind'],'api')
        self.assertEqual(receipt['schema'],'pptx-source-v2')
        review=json.loads((self.dir/'pptx-review.json').read_text());review['pages'][0]['result']['supplement']='tampered'
        (self.dir/'pptx-review.json').write_text(json.dumps(review))
        with self.assertRaises(doc.PPTXError):doc.validated(self.source,self.dir)

    def test_critical_api_limit_blocks_commit(self):
        r=result();r.update(verdict='warn',limitations=[{'detail':'Unreadable key value','critical':True}])
        with patch.object(qa,'call_json_vision',return_value={'result':r,'usage':{}}):doc.prepare_api_review(self.source,self.dir,allow_remote=True)
        with self.assertRaisesRegex(doc.PPTXError,'关键未决'):doc.validated(self.source,self.dir)

    def test_template_subset_keeps_notes_and_original_bytes(self):
        target=self.repo/'temp/one.pptx';before=self.source.read_bytes();lib.native_subset(self.source,target,[1])
        extracted=st.extract(target)
        self.assertEqual(len(extracted['pages']),1);self.assertIn('NOTES ONLY',extracted['pages'][0]['notes'])
        self.assertEqual(self.source.read_bytes(),before)
        with zipfile.ZipFile(target) as z:
            self.assertNotIn('ppt/slides/slide1.xml',z.namelist())
            self.assertIn(b'<Relationships xmlns=',z.read('_rels/.rels'))
            self.assertIn(b'<Types xmlns=',z.read('[Content_Types].xml'))

    def test_private_source_isolation(self):
        private=self.repo/'private/raw/private.pptx';private.parent.mkdir(parents=True);private.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(v.PPTVisualError,'Private'):v.analyze(private,allow_remote=True)
        with self.assertRaisesRegex(ValueError,'Private'):lib.export(private,'1',self.repo/'temp/no','test')

    def test_collection_atomic_dedup_reindex_and_source_drift(self):
        source=self.repo/'academic/raw/reference-documents/test.pptx';source.parent.mkdir(parents=True);source.write_bytes(self.source.read_bytes())
        prepared=self.repo/'temp/component'
        def renderer(path,directory):
            p=directory/'source-slides.pdf';p.write_bytes(b'test-only');return 'slides',p,1
        with patch.object(qa,'_prepare_source',side_effect=renderer):lib.export(source,'1',prepared,'test')
        library=self.repo/'slide-library';library.mkdir();(library/'_index.md').write_text('# Existing index\n\nKeep this text\n')
        r=lib.collect(prepared,'test','TEST-ONLY selected page 1')
        self.assertEqual(r['status'],'collected');self.assertIn('Keep this text',(library/'_index.md').read_text())
        self.assertEqual(lib.collect(prepared,'test-other','TEST-ONLY')['status'],'already_collected')
        self.assertEqual(len(lib.search('one')),1)
        (library/'_index.md').write_text('# Existing index\n');lib.rebuild_index(library)
        self.assertIn('test/component.md',(library/'_index.md').read_text())
        source.write_bytes(source.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'Source changed'):lib.collect(prepared,'other','TEST-ONLY')

    def test_no_collect_inbox_and_manifest_tampering(self):
        prepared=self.repo/'temp/component'
        def renderer(path,directory):
            p=directory/'source-slides.pdf';p.write_bytes(b'test-only');return 'slides',p,1
        with patch.object(qa,'_prepare_source',side_effect=renderer):lib.export(self.source,'1',prepared,'test')
        with self.assertRaisesRegex(ValueError,'Raw'):lib.collect(prepared,'test','TEST-ONLY')
        (prepared/'component.md').write_text('modified')
        with self.assertRaisesRegex(ValueError,'hash'):lib.verify_bundle(prepared)

    def test_preview_api_records_identity_without_locking(self):
        from test_presentation_state import StateTests
        t=StateTests();t.setUp();self.addCleanup(t.tearDown)
        t.ready();t.apply('build')
        rev=t.store.read()['session']['revision_id']
        with patch.object(st,'extract',return_value={'pages':[self.page]}), patch.object(qa,'call_json_vision',return_value={'result':result('form-check'),'usage':{}}):
            v.review_state(t.store,'s1',rev,True)
        records=t.store.read()['session']['approval_records']['s1']
        self.assertNotIn('preview',records)
        self.assertEqual(records['visual']['actor'],'API:TEST-ONLY')
        self.assertEqual(records['visual']['api_receipt']['backend'],'api')

    def test_form_api_scope_comparison_and_persistent_report(self):
        from test_presentation_delivery import DeliveryTests
        t=DeliveryTests();t.setUp();self.addCleanup(t.tearDown)
        output=t.exported();_,candidate=t.candidate('form-check',output=output)
        # The isolated state tests use synthetic binary files; this seam supplies
        # the native inventory only. Real OOXML closure is tested separately.
        with patch.object(st,'extract',return_value={'pages':[dict(self.page,number=n) for n in [1,2,3]]}), patch.object(qa,'call_json_vision',return_value={'result':result('form-check'),'usage':{}}) as call:
            completed=t.delivery.form_api(candidate,allow_remote=True)
            self.assertEqual(len(call.call_args.args[1]),2)
            self.assertEqual([i['slide_id'] for i in completed['items']],['s1'])
            report=t.delivery.apply(completed)
            saved,_=__import__('presentation_delivery')._read(t.store.root/'outputs',report['output_id'])
            self.assertEqual(saved['semantic_executor'],'pptx_visual_api')
            self.assertEqual(saved['user_approval'],'not_requested')

    def test_explicit_ppt_permission_propagates_without_changing_backend(self):
        import ingest_inbox
        c=ingest_inbox.dispatch_command('document','inbox/test.pptx','academic','academic-reference',allow_remote_ppt=True)
        self.assertIn('--allow-remote-ppt',c)
        tool,args=ingest_inbox.dsi_tool('document','inbox/test.pptx','academic','academic-reference',allow_remote_ppt=True)
        self.assertTrue(args['allow_remote_ppt']);self.assertEqual(tool,'ingest_document_file')

if __name__=='__main__':unittest.main()

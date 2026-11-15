#!/usr/bin/env python3
"""Offline native asset, isolation, registration and reuse regression. No model calls."""
import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import presentation_asset as asset
import presentation_asset_render as render
import presentation_artwork as art

REPO=Path(__file__).resolve().parent.parent
SEED=json.loads((REPO/'operations/templates/presentation-assets/circular-dual-arrow/asset.json').read_text())


class AssetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name).resolve()
        self.patch=patch.object(asset,'REPO',self.root);self.patch.start();self.addCleanup(self.patch.stop)
        self.a=copy.deepcopy(SEED)
        self.builtin=self.root/'operations/templates/presentation-assets/circular-dual-arrow/asset.json'
        self.builtin.parent.mkdir(parents=True);asset.save(self.builtin,self.a)
        self.candidate=self.root/'temp/candidate.json';self.candidate.parent.mkdir();asset.save(self.candidate,self.a)

    def test_generic_geometry_transparency_native_and_color_variants(self):
        import fitz
        for i,preset in enumerate(('red','blue-gray','mono')):
            result=asset.use('circular-dual-arrow',f'temp/variant-{i}',preset=preset,width=68*(i+1),rotation=45 if i==1 else 0)
            directory=Path(result['output'])
            png=fitz.Pixmap(str(directory/'preview.png'))
            self.assertEqual(png.alpha,1);self.assertEqual(png.samples[3],0)
            with ZipFile(directory/'asset.pptx') as z:
                root=ET.fromstring(z.read('ppt/slides/slide1.xml'))
                self.assertEqual(len(root.findall('.//a:custGeom',render.NS)),2)
                self.assertEqual(len(root.findall('.//p:grpSp',render.NS)),1)
                self.assertEqual(root.findall('.//p:pic',render.NS),[])
            # OpenXML is also consumable by the native object library.
            deck=render.runtime._load_pptx().Presentation(directory/'asset.pptx')
            self.assertEqual(len(deck.slides),1)
            self.assertEqual(len(deck.slides[0].shapes[0].shapes),2)
        self.assertEqual(self.builtin.read_bytes(),asset.canonical(SEED))

    def test_collection_idempotence_version_integrity_and_snapshot(self):
        result=asset.collect(self.candidate,'Explicit collection for testing')
        p=Path(result['asset']);before=p.read_bytes()
        self.assertEqual(asset.collect(self.candidate,'Repeat authorized collection')['status'],'existing')
        self.a['name']='Changed';asset.save(self.candidate,self.a)
        with self.assertRaisesRegex(ValueError,'Immutable'):asset.collect(self.candidate,'Changed but same version')
        self.assertEqual(p.read_bytes(),before)
        self.a['version']=2;asset.save(self.candidate,self.a);asset.collect(self.candidate,'New version')
        self.assertEqual(asset.locate('local:circular-dual-arrow')[1]['version'],2)
        self.assertEqual(asset.locate('local:circular-dual-arrow@1')[1]['name'],SEED['name'])
        asset.use('local:circular-dual-arrow@1','temp/old-instance')
        receipt=(p.parent/'receipt.json').read_bytes();(p.parent/'receipt.json').unlink()
        with self.assertRaisesRegex(ValueError,'receipt missing'):asset.locate('local:circular-dual-arrow@1')
        (p.parent/'receipt.json').write_bytes(receipt)
        (p.parent/'asset.svg').write_text('broken')
        with self.assertRaisesRegex(ValueError,'integrity'):asset.locate('local:circular-dual-arrow@1')
        self.assertEqual(asset.read(self.root/'temp/old-instance/asset.json')['name'],SEED['name'])

    def test_parameter_and_path_boundaries_do_not_accept_code_or_new_v1_primitives(self):
        for values in ({'ring-width':999},{'gap':float('nan')},{'primary':'url(https://example.invalid)'},{'unknown':3}):
            with self.subTest(values=values),self.assertRaises(ValueError):asset.validate(self.a,values)
        artwork,_=asset.validate(self.a)
        with self.assertRaises(ValueError):art.validate_artwork(artwork,{'canvas':artwork['canvas']})
        for update in ({'onclick':'code'},{'commands':[['M',0,0],['L',999,0],['Z']]},{'fill':'url(file:///private)'},{'commands':[['M',0,0],['Q',1,2],['Z']]}):
            obj=copy.deepcopy(artwork);obj['objects'][0].update(update)
            with self.assertRaises(ValueError):art.validate_artwork(obj,{'canvas':obj['canvas']},allow_v2=True)
        self.a['parameters']['bad']={'type':'code','default':'raise SystemExit'}
        with self.assertRaises(ValueError):asset.validate(self.a)

    def test_color_cli_discovery_precedence_and_native_exports(self):
        def cli(*args):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=asset.main(list(args))
            self.assertEqual(code,0,output.getvalue())
            return json.loads(output.getvalue())
        shown=cli('show','--asset','circular-dual-arrow')
        self.assertEqual(shown['color_parameters'],{'primary':'#C60018','secondary':'#E66A72'})
        params=self.root/'temp/values.json';asset.save(params,{'primary':'#ABCDEF','secondary':'#334455'})
        result=cli('use','--asset','circular-dual-arrow','--preset','blue-gray','--values',str(params),
                   '--color','primary=#123ABC','--output','temp/recolored')
        self.assertEqual(result['instance']['parameters']['primary'],'#123ABC')
        self.assertEqual(result['instance']['parameters']['secondary'],'#334455')
        out=Path(result['output'])
        svg=(out/'asset.svg').read_text()
        for color in ['#123ABC','#334455']:self.assertIn(color,svg)
        with ZipFile(out/'asset.pptx') as z:
            slide=ET.fromstring(z.read('ppt/slides/slide1.xml'))
            fills=[e.get('val') for e in slide.findall('.//p:sp/p:spPr/a:solidFill/a:srgbClr',render.NS)]
            self.assertEqual(fills,['123ABC','334455'])
        from PIL import Image
        pixels=set(Image.open(out/'preview.png').getdata())
        self.assertIn((18,58,188,255),pixels);self.assertIn((51,68,85,255),pixels)
        original,_=asset.validate(self.a);resolved=asset.read(out/'resolved-artwork.json')
        for before,after in zip(original['objects'],resolved['objects']):
            self.assertEqual({k:v for k,v in before.items() if k!='fill'},
                             {k:v for k,v in after.items() if k!='fill'})
        self.assertEqual(asset.read(out/'asset.json'),self.a)
        self.assertEqual(asset.read(self.builtin),self.a)

    def test_color_errors_fail_before_output_and_do_not_hide_invalid_values(self):
        for entries in [['primary=red'],['primary=#fff'],['primary=#123456','primary=#654321'],['missing'],['../bad=#123456']]:
            with self.subTest(entries=entries),self.assertRaises(ValueError):asset.parse_colors(entries)
        for colors in [{'missing':'#123456'},{'ring-width':'#123456'},{'primary':'none'},[]]:
            with self.subTest(colors=colors),self.assertRaises(ValueError):
                asset.use('circular-dual-arrow','temp/bad-colors',colors=colors)
            self.assertFalse((self.root/'temp/bad-colors').exists())
        with self.assertRaises(ValueError):
            asset.use('circular-dual-arrow','temp/hidden-invalid',values={'primary':'bad'},colors={'primary':'#123456'})
        self.assertFalse((self.root/'temp/hidden-invalid').exists())

    def test_source_drift_and_raw_boundary(self):
        self.a['source']['kind']='source-derived'
        with self.assertRaises(ValueError):asset.validate(self.a)
        path=self.root/'projects/source.pptx';path.parent.mkdir();path.write_bytes(b'original')
        self.a['source']['files']=[{'path':'projects/source.pptx','sha256':asset.digest(path.read_bytes())}]
        with self.assertRaisesRegex(ValueError,'Raw'):asset.validate(self.a)
        self.a['source']['kind']='generated';asset.save(self.candidate,self.a)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):asset.collect(self.candidate,'Collect generated figure')

    def test_private_search_collection_and_output_isolation(self):
        self.a['sensitivity']='private';asset.save(self.candidate,self.a)
        result=asset.collect(self.candidate,'Keep private figure')
        self.assertIn('/private/presentation-assets/',result['asset'])
        self.assertEqual(len(asset.catalog()),1);self.assertEqual(len(asset.catalog(True)),1)
        with self.assertRaisesRegex(ValueError,'private'):asset.locate(result['asset'])
        with self.assertRaises(ValueError):asset.use(result['asset'],'temp/illegal',private=True)
        asset.use(result['asset'],'private/instances/good',private=True)
        downgraded=self.root/'private/downgraded.json';asset.save(downgraded,SEED)
        with self.assertRaisesRegex(ValueError,'downgraded'):asset.use(str(downgraded),'temp/leaked')
        source=self.root/'private/source.json';asset.save(source,SEED)
        with self.assertRaisesRegex(ValueError,'downgraded'):asset.collect(source,'Do not downgrade')

    def test_symlinks_existing_output_and_rollback(self):
        other=self.root/'other';other.mkdir();(self.root/'temp/link').symlink_to(other,target_is_directory=True)
        with self.assertRaises(ValueError):asset.use('circular-dual-arrow','temp/link/result')
        with self.assertRaises(ValueError):asset.use('circular-dual-arrow','admin/raw/result')
        with self.assertRaises(ValueError):asset.use('circular-dual-arrow','temp/../escape')
        with patch.object(render,'export',side_effect=RuntimeError('render failed')):
            with self.assertRaises(RuntimeError):asset.use('circular-dual-arrow','temp/failure')
        self.assertFalse((self.root/'temp/failure').exists())
        self.assertFalse(list((self.root/'temp').glob('.pending-*')))
        asset.use('circular-dual-arrow','temp/good')
        with self.assertRaisesRegex(ValueError,'exists'):asset.use('circular-dual-arrow','temp/good')

    def test_prepare_is_resumable_data_task_and_has_no_remote_call(self):
        brief=self.root/'temp/brief.json';asset.save(brief,{'id':'circular-dual-arrow','name':'Another arrow','brief':'Design a feedback arrow','sensitivity':'local_only'})
        with patch.object(art.qa,'call_json_vision',side_effect=AssertionError('No API')):
            result=asset.prepare(brief)
        task=result['agent_task'];self.assertEqual(task['schema'],'agent-task-v1')
        self.assertIn('collect',json.dumps(task))
        # A later process can read the manifest and write/validate the declared candidate.
        p=Path(result['run'])/'candidate.json';asset.save(p,self.a)
        with self.assertRaisesRegex(ValueError,'match creation brief'):asset.collect(p,'Reject sensitivity mismatch')
        self.a['sensitivity']='local_only';asset.save(p,self.a)
        asset.validate(asset.read(p));asset.collect(p,'Continue saved task')
        self.assertEqual(asset.locate('local:circular-dual-arrow')[1]['id'],self.a['id'])

    def test_insertion_preserves_other_parts_and_rejects_missing_replacement(self):
        artwork,_=asset.validate(self.a)
        deck=render.native_deck(artwork,'test');deck.slide_width=1280*render.EMU;deck.slide_height=720*render.EMU
        deck.slides.add_slide(deck.slide_layouts[6]);source=self.root/'temp/source.pptx';deck.save(source)
        before=source.read_bytes()
        result=asset.use('circular-dual-arrow','temp/inserted',source=source,page=1,x=200,y=330,width=68,replace=['asset:test'],colors={'primary':'#123ABC'})
        with ZipFile(source) as a,ZipFile(result['pptx']) as b:
            changed=[n for n in a.namelist() if a.read(n)!=b.read(n)]
            self.assertEqual(changed,['ppt/slides/slide1.xml'])
            root=ET.fromstring(b.read(changed[0]));ids=[e.get('id') for e in root.findall('.//p:cNvPr',render.NS)]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertIn('123ABC',[e.get('val') for e in root.findall('.//a:srgbClr',render.NS)])
        self.assertEqual(source.read_bytes(),before)
        self.assertEqual(result['instance']['insertion']['box'],[200,330,68,68])
        with self.assertRaises(ValueError):asset.use('circular-dual-arrow','temp/no-match',source=source,page=1,x=0,y=0,width=68,replace=['absent'])
        self.assertFalse((self.root/'temp/no-match').exists())
        with self.assertRaises(ValueError):asset.use('circular-dual-arrow','temp/outside',source=source,page=2,x=1270,y=0,width=68)

    def test_text_parameters_escape_markup_and_reuse_remains_editable(self):
        a=copy.deepcopy(self.a);a['artwork']['canvas']=[400,200]
        a['parameters']['label']={'type':'text','default':'Feedback','max_length':40}
        a['artwork']['objects'].append({'type':'text','x':80,'y':20,'w':300,'h':40,'text':'@label','size':20,'color':'@primary','align':'left','bold':False})
        asset.save(self.candidate,a)
        result=asset.use(str(self.candidate),'temp/text',values={'label':'<script>data</script>'})
        svg=(Path(result['output'])/'asset.svg').read_text();self.assertIn('&lt;script&gt;',svg)
        with ZipFile(result['pptx']) as z:self.assertIn(b'&lt;script&gt;',z.read('ppt/slides/slide1.xml'))

    def test_safe_source_path_extraction(self):
        artwork,_=asset.validate(self.a)
        deck=render.native_deck(artwork,'extractable');slide=deck.slides[0]
        group=slide.shapes[0]
        # Flatten only these native paths for the bounded extraction fixture.
        children=list(group._element)[2:]
        for el in children:slide.shapes._spTree.append(copy.deepcopy(el))
        slide.shapes._spTree.remove(group._element)
        source=self.root/'temp/generated.pptx';deck.save(source)
        result=asset.extract(source,1,['asset:extractable:1','asset:extractable:2'],'temp/extracted','extracted','Extracted','generated','Select generated paths')
        a=asset.read(result['candidate']);self.assertEqual(len(a['artwork']['objects']),2)
        asset.collect(result['candidate'],'Collect selected paths')
        with self.assertRaisesRegex(ValueError,'Raw'):asset.extract(source,1,['asset:extractable:1'],'temp/bad','bad','Bad','source-derived','No Raw')


class DiscoveryTests(unittest.TestCase):
    def test_registered_routed_and_on_demand_from_ppt(self):
        import function_registry as fr
        data=fr.load_registry();self.assertEqual(fr.validate_runtime(data),[])
        self.assertIn('artifact.presentation_asset',data['functions'])
        for args in (['--capability','presentation-asset','--capability-profile','general'],['--capability','presentation','--capability-profile','create'],['--task','write']):
            p=subprocess.run([sys.executable,str(REPO/'.scripts/route.py'),*args],capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn('presentation-asset',p.stdout)
        p=subprocess.run([sys.executable,str(REPO/'.scripts/wg.py'),'presentation-asset','search','反馈'],capture_output=True,text=True)
        self.assertTrue(json.loads(p.stdout)['ok'])
        p=subprocess.run([sys.executable,str(REPO/'.scripts/wg.py'),'presentation-asset','show','--asset','does-not-exist'],capture_output=True,text=True)
        self.assertFalse(json.loads(p.stdout)['ok'])


if __name__=='__main__':unittest.main()

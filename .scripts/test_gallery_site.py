#!/usr/bin/env python3
"""Synthetic regression coverage for the standalone Gallery publication boundary."""
import copy
import hashlib
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
import gallery_site as site


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.ids=set()
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if 'id' in a:self.ids.add(a['id'])
        for key in ('href','src'):
            if key in a:self.links.append(a[key])


class GalleryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve();self.root=self.base/'source';self.root.mkdir()
        self.out=self.base/'site';self.includes=[]
        self.catalog={'schema':'gallery-catalog-v1','featured':'poster-one','works':['poster-one','figure-two'],
                      'capabilities':[{'id':'poster','label':'海报','description':'Poster capability','doc':'operations/POSTER.md'},
                                      {'id':'figure','label':'图示','description':'Figure capability','doc':'operations/FIGURE.md'}]}
        for name in ('POSTER','FIGURE'):self.write('operations/'+name+'.md','Documentation')
        self.write('gallery/RIGHTS.md','# Rights\n\nSynthetic fixture only.',True)
        for name in ('style.css','app.js'):self.write('gallery/site/'+name, '/* fixture */',True)
        for slug,caps in [('poster-one',['poster','figure']),('figure-two',['figure'])]:
            base='gallery/works/'+slug
            files={'cover.webp':b'fixture image','poster.png':b'fixture image','poster.pdf':b'fixture pdf'}
            for name,data in files.items():self.write(base+'/'+name,data,True)
            metadata={'schema':'gallery-work-v1','id':slug,'title':'Synthetic title','kind':'poster' if slug=='poster-one' else 'figure',
                      'language':'zh-CN','selected_on':'2026-10-29','capabilities':caps,'references':[{'url':'https://doi.org/fixture'}],
                      'files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()},
                      'presentation':{'name':slug,'summary':'Synthetic summary','type_label':'海报' if slug=='poster-one' else '图示',
                      'format_label':'A0','audience':'Students','prompt':'Make a poster','source_note':'Synthetic source',
                      'credit':'Fixture','boundary':'Not scientific data','cover':'cover.webp','preview':'poster.png',
                      'downloads':[{'file':'poster.pdf','label':'Download'}],
                      'capability_uses':[{'id':c,'text':'Contribution '+c} for c in caps]}}
            self.write(base+'/metadata.json',json.dumps(metadata),True)
        self.write('gallery/catalog.json',json.dumps(self.catalog),True)
        self.write('gallery/local/private.txt','PRIVATE_SENTINEL')
        self.manifest={'include':self.includes,'exclude':['gallery/local/**']}
        self.save_manifest()

    def write(self,relative,data,approve=False):
        p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(data if isinstance(data,bytes) else data.encode())
        if approve:self.includes.append(relative)
        return p

    def save_manifest(self):self.write('operations/engineering/open-source-manifest.yaml',yaml.safe_dump(self.manifest))
    def change_metadata(self,edit):
        p=self.root/'gallery/works/poster-one/metadata.json';m=json.loads(p.read_text());edit(m);p.write_text(json.dumps(m))

    def test_multiple_works_capabilities_and_project_relative_links(self):
        result=site.build(self.root,self.out);self.assertEqual(result['works'],2)
        index=(self.out/'index.html').read_text()
        self.assertIn('data-capabilities="poster figure"',index)
        self.assertIn('data-capabilities="figure"',index)
        self.assertIn('<option value="figure">',index)
        self.assertNotIn('PRIVATE_SENTINEL',''.join(p.read_text() for p in self.out.rglob('*.html')))
        self.assertFalse((self.out/'gallery').exists())
        self.assertFalse(any(p.name=='metadata.json' for p in self.out.rglob('*')))
        for page in self.out.rglob('*.html'):
            parser=Links();parser.feed(page.read_text())
            for value in parser.links:
                u=urlsplit(value)
                if u.scheme:continue
                self.assertFalse(u.path.startswith('/'),value)
                target=page.parent/unquote(u.path) if u.path else page
                self.assertTrue(target.resolve().is_relative_to(self.out.resolve()),value)
                self.assertTrue(target.is_file(),value)
                if u.fragment:
                    other=Links();other.feed(target.read_text());self.assertIn(u.fragment,other.ids)

    def test_failed_rebuild_preserves_previous_output(self):
        site.build(self.root,self.out);old=(self.out/'index.html').read_bytes()
        self.write('gallery/works/poster-one/poster.pdf','tampered')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):site.build(self.root,self.out)
        self.assertEqual((self.out/'index.html').read_bytes(),old)

    def test_related_works_use_catalog_titles_and_relative_links(self):
        self.change_metadata(lambda m:m.update({'related_works':['figure-two']}))
        site.build(self.root,self.out)
        page=(self.out/'works/poster-one/index.html').read_text()
        self.assertIn('aria-label="关联作品"',page)
        self.assertIn('href="../figure-two/index.html"',page)
        self.assertIn('<h3>figure-two',page)
        for related in [['missing'],['poster-one'],['figure-two','figure-two'],['../private']]:
            self.change_metadata(lambda m:m.update({'related_works':related}))
            with self.assertRaisesRegex(ValueError,'Related works'):site.build(self.root,self.out)

    def test_multiple_document_previews_and_full_downloads(self):
        image=b'second language preview'; pdf=b'second language full document'
        self.write('gallery/works/poster-one/english.png', image, True)
        self.write('gallery/works/poster-one/english.pdf', pdf, True)
        self.save_manifest()
        def edit(m):
            m['files'].update({'english.png':hashlib.sha256(image).hexdigest(),
                               'english.pdf':hashlib.sha256(pdf).hexdigest()})
            m['presentation']['downloads'].append({'file':'english.pdf','label':'English PDF'})
            m['presentation']['previews']=[{'file':'poster.png','label':'中文首页','document':'poster.pdf'},
                                          {'file':'english.png','label':'English first page','document':'english.pdf'}]
        self.change_metadata(edit)
        site.build(self.root,self.out)
        page=(self.out/'works/poster-one/index.html').read_text()
        self.assertIn('中文首页',page);self.assertIn('English first page',page)
        self.assertEqual(page.count('阅读全文 PDF'),2)
        self.assertNotIn('完整科普展板',page)
        self.assertEqual((self.out/'assets/works/poster-one/english.png').read_bytes(),image)
        self.assertEqual((self.out/'assets/works/poster-one/english.pdf').read_bytes(),pdf)

    def test_extra_preview_must_be_verified_and_document_downloadable(self):
        for preview in [{'file':'../../local/private.png','label':'Private','document':'poster.pdf'},
                        {'file':'poster.png','label':'First page','document':'secret.pdf'}]:
            self.change_metadata(lambda m:m['presentation'].update({'previews':[preview]}))
            with self.assertRaisesRegex(ValueError,'Preview'):site.build(self.root,self.out)

    def test_unapproved_and_excluded_assets_rejected(self):
        for mode in ('unapproved','excluded'):
            with self.subTest(mode=mode):
                manifest=copy.deepcopy(self.manifest);name='gallery/works/poster-one/poster.pdf'
                if mode=='unapproved':manifest['include'].remove(name)
                else:manifest['exclude'].append(name)
                self.write('operations/engineering/open-source-manifest.yaml',yaml.safe_dump(manifest))
                with self.assertRaisesRegex(ValueError,'Not an approved'):site.build(self.root,self.out)

    def test_recursive_gallery_allowlist_rejected(self):
        self.manifest['include'].append('gallery/**');self.save_manifest()
        with self.assertRaisesRegex(ValueError,'exact-file'):site.build(self.root,self.out)

    def test_symlinked_input_rejected(self):
        p=self.root/'gallery/works/poster-one/cover.webp';p.unlink();p.symlink_to(self.root/'gallery/local/private.txt')
        with self.assertRaisesRegex(ValueError,'Symlink'):site.build(self.root,self.out)

    def test_traversal_and_unsafe_links_rejected(self):
        original=(self.root/'gallery/works/poster-one/metadata.json').read_text()
        for edit in [lambda m:m['files'].update({'../../local/private.txt':'fake'}),
                     lambda m:m['references'][0].update({'url':'javascript:alert(1)'}),
                     lambda m:m['presentation']['downloads'][0].update({'file':'secret.txt'})]:
            self.write('gallery/works/poster-one/metadata.json',original);self.change_metadata(edit)
            with self.assertRaises(ValueError):site.build(self.root,self.out)

    def test_metadata_is_escaped_in_html(self):
        payload='<img src=x onerror=alert(1)>'
        self.change_metadata(lambda m:m['presentation'].update({'name':payload,'summary':payload,'prompt':payload}))
        site.build(self.root,self.out)
        for p in [self.out/'index.html',self.out/'works/poster-one/index.html']:
            self.assertNotIn(payload,p.read_text());self.assertIn('&lt;img',p.read_text())

    def test_destination_protection_and_symlink(self):
        untouched=self.base/'existing';untouched.mkdir();(untouched/'note').write_text('keep')
        for target in [untouched,self.root,self.root/'gallery',self.root/'temp/../gallery']:
            with self.subTest(target=target),self.assertRaises(ValueError):site.build(self.root,target)
        alias=self.base/'alias';alias.symlink_to(untouched,target_is_directory=True)
        with self.assertRaises(ValueError):site.build(self.root,alias/'child')
        self.assertEqual((untouched/'note').read_text(),'keep')

    def test_unknown_capability_and_duplicate_work_rejected(self):
        self.change_metadata(lambda m:m.update({'capabilities':['unknown']}))
        with self.assertRaisesRegex(ValueError,'capability'):site.build(self.root,self.out)
        self.catalog['works'].append('poster-one');self.write('gallery/catalog.json',json.dumps(self.catalog))
        with self.assertRaisesRegex(ValueError,'duplicate work'):site.build(self.root,self.out)

    def test_empty_catalog_builds_without_fake_works(self):
        self.catalog.update({'works':[],'featured':None});self.write('gallery/catalog.json',json.dumps(self.catalog))
        site.build(self.root,self.out);s=(self.out/'index.html').read_text()
        self.assertNotIn('data-work ',s);self.assertNotIn('hero-art',s)

    def test_repeat_build_deterministic_and_stale_work_removed(self):
        site.build(self.root,self.out)
        first={str(p.relative_to(self.out)):p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        site.build(self.root,self.out)
        self.assertEqual(first,{str(p.relative_to(self.out)):p.read_bytes() for p in self.out.rglob('*') if p.is_file()})
        self.catalog['works'].remove('figure-two');self.write('gallery/catalog.json',json.dumps(self.catalog))
        site.build(self.root,self.out);self.assertFalse((self.out/'works/figure-two').exists())


if __name__=='__main__':unittest.main(verbosity=2)

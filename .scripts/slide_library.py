#!/usr/bin/env python3
"""Source-preserving native slide extraction and atomic, explicitly scoped collection."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.dom import minidom
import pptx_structure as st
import pptx_visual as vision
import visual_qa as qa

REPO=Path(__file__).resolve().parent.parent
SCHEMA='slide-library-component-v1'
REUSE_SCHEMA='slide-library-component-v2'

def reuse_key(receipt):
    return (receipt['source_sha256'], tuple(receipt['pages']),
            receipt.get('reuse_kind', 'unclassified'), receipt.get('profile_sha256', ''))

def require(ok,message):
    if not ok: raise ValueError(message)

def native_subset(source,target,pages):
    """Retain selected slides and the transitive OPC dependency closure, not raster copies."""
    with zipfile.ZipFile(source) as z:
        require(len(z.namelist())==len(set(z.namelist())),'Duplicate archive entries')
        changes={}
        pres=ET.fromstring(z.read('ppt/presentation.xml'))
        ids=pres.find('p:sldIdLst',st.NS); refs=list(ids)
        keep_ids={refs[n-1].get('{'+st.NS['r']+'}id') for n in pages}
        for child in refs: ids.remove(child)
        for n in pages: ids.append(refs[n-1])
        # Deck-level navigation references discarded slides; it is not a page asset.
        for tag in ('custShowLst','sectionLst','extLst'):
            for el in pres.findall('p:'+tag,st.NS): pres.remove(el)
        # Preserve namespace declarations used by mc:Ignorable and extension values.
        # ElementTree drops unused declarations, producing files Office cannot load.
        document=minidom.parseString(z.read('ppt/presentation.xml'))
        slide_list=document.getElementsByTagNameNS(st.NS['p'],'sldIdLst')[0]
        dom_refs=[n for n in slide_list.childNodes if n.nodeType==n.ELEMENT_NODE]
        for node in list(slide_list.childNodes): slide_list.removeChild(node)
        for n in pages:slide_list.appendChild(dom_refs[n-1])
        for node in list(document.documentElement.childNodes):
            if node.nodeType==node.ELEMENT_NODE and node.localName in ('custShowLst','sectionLst','extLst'):
                document.documentElement.removeChild(node)
        changes['ppt/presentation.xml']=document.toxml(encoding='utf-8')
        rp='ppt/_rels/presentation.xml.rels'; rels=ET.fromstring(z.read(rp))
        for r in list(rels):
            if r.get('Type','').endswith('/slide') and r.get('Id') not in keep_ids: rels.remove(r)
        ET.register_namespace('',st.REL)
        changes[rp]=ET.tostring(rels,encoding='utf-8',xml_declaration=True)
        # Remove original deck thumbnail, which may depict a discarded slide.
        root=ET.fromstring(z.read('_rels/.rels'))
        for r in list(root):
            if r.get('Type','').endswith('/thumbnail'): root.remove(r)
        changes['_rels/.rels']=ET.tostring(root,encoding='utf-8',xml_declaration=True)
        kept=set(); todo=['']
        while todo:
            part=todo.pop()
            if part in kept: continue
            if part: require(part in z.namelist(),'Missing package dependency: '+part)
            kept.add(part)
            rp=posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels') if part else '_rels/.rels'
            if rp not in z.namelist(): continue
            kept.add(rp)
            tree=ET.fromstring(changes.get(rp,z.read(rp)))
            for rel in tree:
                if rel.get('TargetMode')=='External': continue
                target_part=rel.get('Target','')
                target_part=target_part.lstrip('/') if target_part.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part),target_part))
                require(not target_part.startswith('../'),'Escaping package relationship')
                todo.append(target_part)
        allowed_slide_parts={rels_item['part'] for rid,rels_item in st.relationships(z,'ppt/presentation.xml').items() if rid in keep_ids}
        all_slide_parts={r['part'] for r in st.relationships(z,'ppt/presentation.xml').values() if r['kind']=='slide'}
        require(not ((kept & all_slide_parts)-allowed_slide_parts),'Cross-slide dependency includes an unselected slide; select it explicitly or remove the link in a derivative')
        ct=ET.fromstring(z.read('[Content_Types].xml'))
        for el in list(ct):
            if el.tag.endswith('Override') and el.get('PartName','').lstrip('/') not in kept: ct.remove(el)
        ET.register_namespace('','http://schemas.openxmlformats.org/package/2006/content-types')
        changes['[Content_Types].xml']=ET.tostring(ct,encoding='utf-8',xml_declaration=True);kept.add('[Content_Types].xml')
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as out:
            for name in sorted(kept-{''}): out.writestr(name,changes.get(name,z.read(name)))
    # All selected slide XML bytes are unchanged; dependencies retained, no false native rebuild.
    with zipfile.ZipFile(source) as before,zipfile.ZipFile(target) as after:
        data=st.extract(source)
        for n in pages:
            part=data['pages'][n-1]['part']; require(before.read(part)==after.read(part),'Slide changed during extraction')

def design_md(name, data, pages, layout):
    lines=['# '+name,'','> 原样页面组件；设计分析是派生描述，原始研究信息回溯来源 PPT。','',
           '## 来源','',f"原件：{data['source']}；页码：{', '.join(map(str,pages))}",f"原件 SHA-256：{data['source_sha256']}",'',
           '## 复用边界','','保留具体会议、日期、引用与研究内容；未执行通用化或引用脱敏。字体为声明/继承解析值，实际显示可能替代。图片不可当作原生图表数据。','']
    for p in data['pages']:
        sizes=sorted({s['font_size_pt'] for o in p['objects'] for s in o['styles'] if s['font_size_pt'] is not None})
        fonts=sorted({v['theme_resolved'] for o in p['objects'] for s in o['styles'] for v in s['fonts'].values() if v['theme_resolved']})
        kinds={k:sum(o['kind']==k for o in p['objects']) for k in sorted({o['kind'] for o in p['objects']})}
        lines += [f"## 原第 {p['number']} 页",'',f"字体：{', '.join(fonts) or '未解析'}；字号(pt)：{sizes}",
                  f"对象类型：{json.dumps(kinds,ensure_ascii=False)}",f"几何内容覆盖估算：{p['coverage']['ratio']:.1%}；不作为设计评分。",'',
                  '### 页面文字','', '\n'.join(o['text'] for o in p['objects'] if o['text']), '', '### 设计与替换建议','']
        r=layout.get(p['number'])
        if r:
            lines.append(r['summary']); lines.extend('- '+x['description'] for x in r['regions'])
            lines.extend('- 限制：'+x['detail'] for x in r['limitations'])
        else: lines.append('尚未执行 API 版式解析；可用精确对象参数见 structure.json，不冒充设计已评估。')
        lines+=['','保留版式修改文字后，需要重新确认容纳量；图片、图表和引用按实际用途替换。','']
    return '\n'.join(lines)+'\n'

def export(source,pages,target,name,layout_report=None):
    source=Path(source).resolve();target=Path(target).resolve()
    require(target.is_relative_to((REPO/'temp').resolve()),'Extraction output must be in repository temp')
    require(not target.exists(),'Output exists; use a new directory')
    require(not source.is_relative_to((REPO/'private').resolve()),'Private assets must not enter public template library')
    data=st.extract(source);selected=qa.parse_page_selector(pages,len(data['pages']))
    require(selected,'Explicit pages required');layout={}
    if layout_report:
        report=json.loads(Path(layout_report).read_text())
        require(report.get('source_sha256')==data['source_sha256'] and report.get('mode')=='layout','Wrong layout source/mode')
        for r in report.get('pages',[]):
            if r['page'] not in selected: continue
            require(r.get('status')=='complete' and r.get('backend')=='api','Layout API incomplete')
            require(r.get('receipt_sha256')==st.stable_hash({k:v for k,v in r.items() if k!='receipt_sha256'}),'Layout receipt changed')
            p=data['pages'][r['page']-1]
            require(r.get('structure_sha256')==st.stable_hash(p),'Layout source structure changed')
            layout[p['number']]=vision.validate_result(r['result'],'layout',{str(o['id']) for o in p['objects']})
        require(set(layout)==set(selected),'Layout report must cover selected pages')
    target.parent.mkdir(parents=True,exist_ok=True)
    pending=Path(tempfile.mkdtemp(prefix='.slide-export-',dir=target.parent))
    try:
        native_subset(source,pending/'component.pptx',selected)
        data=dict(data,source=str(source.relative_to(REPO)) if source.is_relative_to(REPO) else str(source),pages=[data['pages'][i-1] for i in selected])
        vision.write(pending/'structure.json',data)
        (pending/'component.md').write_text(design_md(name,data,selected,layout),encoding='utf-8')
        if layout_report: shutil.copyfile(layout_report,pending/'layout-analysis.json')
        kind,rendered,count=qa._prepare_source(pending/'component.pptx',pending)
        require(count==len(selected),'Rendered page count mismatch; hidden slides may require explicit handling')
        for i in range(1,count+1): qa.render_page(kind,rendered,i,pending/f'preview-{i}.png',dpi=90)
        files={p.name:vision.digest(p) for p in pending.iterdir() if p.is_file()}
        receipt={'schema':SCHEMA,'name':name,'source':data['source'],'source_sha256':data['source_sha256'],'pages':selected,
                 'layout_analysis':'api' if layout else 'not_executed','files':files,
                 'limitations':['Static native pages only; no guarantee of animation/video playback or cross-application font equivalence.','Deck navigation/custom shows not copied; native per-slide dependencies retained.']}
        vision.write(pending/'receipt.json',receipt)
        require(vision.digest(source)==data['source_sha256'],'Source changed during extraction')
        os.rename(pending,target)
        return {'status':'prepared','directory':str(target),'receipt':receipt}
    except BaseException:
        shutil.rmtree(pending,ignore_errors=True);raise

def verify_bundle(folder):
    require(not folder.is_symlink() and folder.is_dir(),'Invalid bundle path')
    require(not (folder/'receipt.json').is_symlink(),'Receipt symlink not allowed')
    receipt=json.loads((folder/'receipt.json').read_text())
    require(receipt.get('schema') in {SCHEMA, REUSE_SCHEMA},'Unsupported bundle')
    files={p.name for p in folder.iterdir()}
    require(files==set(receipt['files'])|{'receipt.json'},'Bundle file set changed')
    for name,h in receipt['files'].items():
        require(Path(name).name==name and not (folder/name).is_symlink(),'Unsafe bundle entry')
        require(vision.digest(folder/name)==h,'Bundle hash changed: '+name)
    if receipt['schema'] == REUSE_SCHEMA:
        import slide_reuse
        require('reuse.json' in receipt['files'], 'Missing reuse profile')
        profile = slide_reuse.read_json(folder/'reuse.json')
        require(receipt.get('reuse_kind') == profile.get('kind'), 'Reuse kind changed')
        require(receipt.get('profile_sha256') == st.stable_hash(profile), 'Reuse profile changed')
        slide_reuse.validate_profile(profile, st.extract(folder/'component.pptx'))
    return receipt

@contextmanager
def lock(root):
    root.mkdir(parents=True,exist_ok=True)
    require(not root.is_symlink(),'Library symlink not allowed')
    require(not (root/'.collection.lock').is_symlink(),'Lock symlink not allowed')
    require(not (root/'templates').is_symlink(),'Template root symlink not allowed')
    with (root/'.collection.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX);yield

def rebuild_index(root):
    path=root/'_index.md'
    require(not path.is_symlink() and not (root/'._index.pending.md').is_symlink(),'Index symlink not allowed')
    old=path.read_text() if path.exists() else '# Slide Library\n'
    marker='\n<!-- managed-components -->\n'
    old=old.split(marker)[0]
    lines=[marker,'## 受管收藏组件','', '| 名称 | 复用类型 | 原始页码 | 说明 |','|---|---|---|---|']
    for folder in sorted((root/'templates').glob('*/')):
        if folder.name.startswith('.'): continue
        r=verify_bundle(folder)
        rel=folder.relative_to(root).as_posix()
        label=r['name'].replace('|',' ').replace('\n',' ')
        kind={'direct':'直接使用型','adaptable':'套用填充型'}.get(r.get('reuse_kind'),'未分类原样组件')
        lines.append(f"| {label} | {kind} | {r['pages']} | [使用说明]({rel}/component.md) |")
    text=old+'\n'.join(lines)+'\n';tmp=root/'._index.pending.md';tmp.write_text(text,encoding='utf-8');os.replace(tmp,path)

def collect(prepared,name,instruction,layout_report=None):
    require(instruction.strip(),'Actual user selection instruction required')
    require(re.fullmatch(r'[\w\-\u3400-\u9fff]{1,80}',name) is not None,'Use a safe component name')
    prepared=Path(prepared).resolve();require(prepared.is_relative_to((REPO/'temp').resolve()),'Only staged bundles may be committed')
    r=verify_bundle(prepared);source=(REPO/r['source']).resolve()
    parts=source.relative_to(REPO).parts if source.is_relative_to(REPO) else ()
    require(len(parts)>=3 and parts[0] in {'academic','admin','teaching','business'} and parts[1]=='raw','Collect only from managed public-domain Raw; ingest the report first')
    require(vision.digest(source)==r['source_sha256'],'Source changed')
    layout = None
    if layout_report:
        import slide_reuse
        require(r['schema'] == REUSE_SCHEMA, 'Derivative layout reports require a v2 reuse component')
        layout = slide_reuse.validate_layout(prepared, layout_report)
    root=REPO/'slide-library';key=reuse_key(r)
    with lock(root):
        for folder in sorted((root/'templates').glob('*/')):
            if folder.name.startswith('.'): continue
            prior=verify_bundle(folder)
            if reuse_key(prior)==key:
                rebuild_index(root);return {'status':'already_collected','path':str(folder)}
        target=root/'templates'/name;require(not target.exists(),'Name occupied; choose a versioned name')
        target.parent.mkdir(parents=True,exist_ok=True)
        pending=Path(tempfile.mkdtemp(prefix='.pending-',dir=target.parent))
        try:
            for p in prepared.iterdir(): shutil.copyfile(p,pending/p.name)
            r=dict(r,name=name,instruction=instruction)
            if layout:
                vision.write(pending/'layout-analysis.json', layout)
                doc=pending/'component.md'
                notes=['', '## API 设计分析', '', '这是模板制作阶段的版式分析，不是用户最终形式检查或人工批准。', '']
                for page in layout['pages']:
                    result=page['result'];notes.append(f"第 {page['page']} 页（{result['verdict']}）：{result['summary']}")
                    notes.extend('- '+x['description']+'；建议：'+x['suggestion'] for x in result['issues'])
                    notes.extend('- 限制：'+x['detail'] for x in result['limitations'])
                doc.write_text(doc.read_text()+'\n'.join(notes)+'\n',encoding='utf-8')
                r['layout_analysis']='api'
                r['files']={p.name:vision.digest(p) for p in pending.iterdir() if p.is_file() and p.name!='receipt.json'}
            vision.write(pending/'receipt.json',r);verify_bundle(pending)
            for file in pending.iterdir():
                with file.open('rb') as f: os.fsync(f.fileno())
            os.rename(pending,target)
            rebuild_index(root)
        except BaseException:
            if pending.exists(): shutil.rmtree(pending,ignore_errors=True)
            raise
    return {'status':'collected','path':str(target)}

def search(query, kind=None):
    require(kind in {None,'direct','adaptable','unclassified'}, 'Invalid reuse kind')
    tokens=query.lower().split();results=[]
    root=REPO/'slide-library'
    for p in sorted(root.glob('**/*.md')):
        if p.name.startswith('_') or any(part.startswith('.') for part in p.relative_to(root).parts): continue
        receipt=verify_bundle(p.parent) if p.name=='component.md' and (p.parent/'receipt.json').exists() else None
        entry_kind=receipt.get('reuse_kind','unclassified') if receipt else 'unclassified'
        if kind is not None and entry_kind!=kind: continue
        content=p.read_text();score=sum(token in content.lower() for token in tokens) if tokens else 1
        if score: results.append({'path':str(p),'score':score,'title':content.splitlines()[0].lstrip('# '),
                                 'kind':entry_kind,'pptx':str(p.parent/'component.pptx' if receipt else p.with_suffix('.pptx'))})
    return sorted(results,key=lambda r:-r['score'])[:30]

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('extract');p.add_argument('--source',required=True);p.add_argument('--pages',required=True);p.add_argument('--output',required=True);p.add_argument('--name',required=True);p.add_argument('--layout-report')
    p=sub.add_parser('collect');p.add_argument('--prepared',required=True);p.add_argument('--name',required=True);p.add_argument('--instruction',required=True);p.add_argument('--layout-report')
    p=sub.add_parser('search');p.add_argument('query',nargs='?',default='');p.add_argument('--kind',choices=['direct','adaptable','unclassified'])
    p=sub.add_parser('prepare-reuse');p.add_argument('--component',required=True);p.add_argument('--profile',required=True);p.add_argument('--output',required=True);p.add_argument('--name',required=True)
    p=sub.add_parser('fill');p.add_argument('--template',required=True);p.add_argument('--values',required=True);p.add_argument('--output',required=True)
    p=sub.add_parser('reindex')
    a=parser.parse_args()
    try:
        if a.command=='extract':r=export(a.source,a.pages,a.output,a.name,a.layout_report)
        elif a.command=='collect':r=collect(a.prepared,a.name,a.instruction,a.layout_report)
        elif a.command=='search':r=search(a.query,a.kind)
        elif a.command=='prepare-reuse':
            import slide_reuse
            r=slide_reuse.prepare(a.component,slide_reuse.read_json(a.profile),a.output,a.name)
        elif a.command=='fill':
            import slide_reuse
            r=slide_reuse.fill(a.template,slide_reuse.read_json(a.values),a.output)
        else:
            with lock(REPO/'slide-library'):rebuild_index(REPO/'slide-library')
            r={'status':'reindexed'}
        print(json.dumps(r,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError,qa.VisualQAError,zipfile.BadZipFile) as exc:
        print(json.dumps({'status':'error','error':str(exc)},ensure_ascii=False));return 2
if __name__=='__main__':raise SystemExit(main())

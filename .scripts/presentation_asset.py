#!/usr/bin/env python3
"""Agent-authored, data-only PPT assets: prepare, check, collect, search, use, insert."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile
import uuid
from contextlib import contextmanager

import agent_task
import presentation_artwork as art
import presentation_asset_render as render
from platform_compat import fcntl, fsync_path

REPO=Path(__file__).resolve().parent.parent
SCHEMA='presentation-asset-v1'
MAX_BYTES=2_000_000


def canonical(obj):return (json.dumps(obj,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
def digest(data):return hashlib.sha256(data).hexdigest()
def save(path,obj):path.write_bytes(canonical(obj))


def safe(value, *, output=False, private=False):
    p=Path(value);p=p if p.is_absolute() else REPO/p
    if '..' in p.parts or not p.is_relative_to(REPO):raise ValueError('Path must stay in the repository')
    for item in [p,*p.parents]:
        if item==REPO.parent:break
        if item.is_symlink():raise ValueError('Symlink paths are not accepted')
    if output:
        rel=p.relative_to(REPO)
        if not rel.parts or rel.parts[0] not in (('private',) if private else ('temp','projects')) or 'raw' in rel.parts:
            raise ValueError('Outputs require a new temp/projects directory; private assets require private/')
    return p


def read(path):
    path=safe(path)
    if not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Missing or oversized JSON input')
    def pairs(items):
        result={}
        for k,v in items:
            if k in result:raise ValueError('Duplicate JSON key')
            result[k]=v
        return result
    return json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=pairs,
                      parse_constant=lambda v:(_ for _ in ()).throw(ValueError('Non-finite JSON number')))


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',value):raise ValueError('Invalid asset/parameter identifier')
    return value


def string(value,maximum=1000):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum or any(ord(c)<32 and c!='\n' for c in value):raise ValueError('Invalid text field')
    return value


def parameter(spec,value):
    if not isinstance(spec,dict) or spec.get('type') not in ('color','number','text'):raise ValueError('Invalid parameter schema')
    kind=spec['type'];fields={'type','default'}|({'min','max'} if kind=='number' else {'max_length'} if kind=='text' else set())
    if set(spec)!=fields:raise ValueError('Unknown parameter fields')
    if kind=='number':
        lo,hi=art.number(spec['min'],-4096,4096),art.number(spec['max'],-4096,4096)
        return art.number(value,lo,hi)
    if kind=='color':return art.color(value)
    return string(value,int(art.number(spec['max_length'],1,300)))


def validate(asset, values=None):
    fields={'schema','id','version','name','category','purpose','tags','sensitivity','source','parameters','presets','artwork'}
    if not isinstance(asset,dict) or set(asset)!=fields or asset['schema']!=SCHEMA:raise ValueError('Invalid asset schema')
    identifier(asset['id']);string(asset['name'],160);string(asset['category'],120);string(asset['purpose'],3000)
    if type(asset['version'])!=int or not 1<=asset['version']<=10000:raise ValueError('Invalid version')
    if not isinstance(asset['tags'],list) or not 1<=len(asset['tags'])<=30:raise ValueError('Tags required')
    for tag in asset['tags']:string(tag,80)
    if asset['sensitivity'] not in ('public','local_only','private'):raise ValueError('Invalid sensitivity')
    source=asset['source']
    if not isinstance(source,dict) or set(source)!={'kind','description','files'} or source['kind'] not in ('generated','source-derived'):raise ValueError('Invalid source metadata')
    string(source['description'],3000)
    if not isinstance(source['files'],list) or len(source['files'])>20:raise ValueError('Invalid source files')
    if source['kind']=='source-derived' and not source['files']:raise ValueError('Source-derived assets require archived Raw')
    for item in source['files']:
        if not isinstance(item,dict) or set(item)!={'path','sha256'}:raise ValueError('Invalid source file')
        p=safe(item['path']);rel=p.relative_to(REPO)
        if Path(item['path']).is_absolute():raise ValueError('Source metadata uses repository-relative paths')
        if not re.fullmatch('[0-9a-f]{64}',item['sha256']):raise ValueError('Invalid source hash')
        if source['kind']=='source-derived' and (len(rel.parts)<3 or rel.parts[1]!='raw' or rel.parts[0] not in ('academic','admin','teaching','business','private','cross-domain')):raise ValueError('Source-derived assets require archived Raw')
        if rel.parts[0]=='private' and asset['sensitivity']!='private':raise ValueError('Private source sensitivity must be inherited')
    params=asset['parameters']
    if not isinstance(params,dict) or len(params)>24:raise ValueError('Too many parameters')
    resolved={}
    for k,spec in params.items():identifier(k);resolved[k]=parameter(spec,spec.get('default'))
    if not isinstance(asset['presets'],dict) or len(asset['presets'])>20:raise ValueError('Invalid presets')
    for name,preset in asset['presets'].items():
        identifier(name)
        if not isinstance(preset,dict) or set(preset)-set(params):raise ValueError('Invalid preset values')
        for k,v in preset.items():parameter(params[k],v)
    values=values or {}
    if not isinstance(values,dict) or set(values)-set(params):raise ValueError('Unknown asset parameter')
    for k,v in values.items():resolved[k]=parameter(params[k],v)
    def resolve(v):
        if isinstance(v,str) and v.startswith('@'):
            if v[1:] not in resolved:raise ValueError('Unknown parameter reference')
            return resolved[v[1:]]
        if isinstance(v,list):return [resolve(x) for x in v]
        if isinstance(v,dict):return {k:resolve(x) for k,x in v.items()}
        return v
    artwork=resolve(copy.deepcopy(asset['artwork']))
    if not isinstance(artwork,dict) or not isinstance(artwork.get('objects'),list):raise ValueError('Invalid artwork')
    if not 1<=len(artwork['objects'])<=100:raise ValueError('Invalid object count')
    artwork['objects']=[render.arc_arrow(x) if isinstance(x,dict) and x.get('type')=='arc_arrow' else x for x in artwork['objects']]
    art.validate_artwork(artwork,{'canvas':artwork.get('canvas')},allow_v2=True)
    if artwork['schema']!='presentation-artwork-v2':raise ValueError('Asset artwork requires v2')
    return artwork,resolved


def source_check(asset, candidate):
    request=candidate.parent/'request.json'
    if (candidate.parent/'agent-task.json').exists():
        req=read(request);brief=read(candidate.parent/'brief.json')
        if digest(canonical(brief))!=req['brief_sha256']:
            raise ValueError('Creation brief changed; prepare a new task')
        if asset['id']!=brief['id'] or asset['sensitivity']!=brief['sensitivity']:
            raise ValueError('Candidate ID/sensitivity must match creation brief')
    if candidate.relative_to(REPO).parts[0]=='private' and asset['sensitivity']!='private':raise ValueError('Private candidate cannot be downgraded')
    for item in asset['source']['files']:
        p=safe(item['path'])
        if not p.is_file() or digest(p.read_bytes())!=item['sha256']:raise ValueError('Source file missing or changed')


def roots(private=False):
    if private:return [('private',safe(REPO/'private/presentation-assets'))]
    return [('builtin',safe(REPO/'operations/templates/presentation-assets')),('local',safe(REPO/'slide-library/assets'))]


def catalog(private=False):
    records=[]
    for origin,root in roots(private):
        if not root.exists():continue
        for p in sorted([*root.glob('*/asset.json'),*root.glob('*/v*/asset.json')]):
            a=read(p);validate(a)
            if (a['sensitivity']=='private')!=private:raise ValueError('Asset in wrong sensitivity library')
            records.append({'id':a['id'],'version':a['version'],'name':a['name'],'purpose':a['purpose'],'tags':a['tags'],
                            'origin':origin,'path':p.relative_to(REPO).as_posix(),'sha256':digest(canonical(a))})
    return records


def locate(value,private=False):
    if '/' in value or value.endswith('.json'):
        p=safe(value);p=p/'asset.json' if p.is_dir() else p
    else:
        origin=None
        if ':' in value:origin,value=value.split(':',1)
        version=None
        if '@' in value:value,version=value.split('@',1);version=int(version)
        identifier(value)
        items=[x for x in catalog(private) if x['id']==value and (origin is None or x['origin']==origin) and (version is None or x['version']==version)]
        if not items:raise ValueError('Asset not found')
        items.sort(key=lambda x:(x['version'],x['origin']!='builtin'),reverse=True)
        p=safe(items[0]['path'])
    a=read(p);validate(a)
    if p.relative_to(REPO).parts[0]=='private' and a['sensitivity']!='private':raise ValueError('Private candidate cannot be downgraded')
    if a['sensitivity']=='private' and not private:raise ValueError('Use --private for private assets')
    managed=any(p.is_relative_to(root) for origin,root in roots(a['sensitivity']=='private') if origin!='builtin')
    if managed and not (p.parent/'receipt.json').is_file():raise ValueError('Asset package integrity receipt missing')
    if (p.parent/'receipt.json').exists():
        receipt=read(p.parent/'receipt.json')
        if receipt.get('files',{}).get('asset.json')!=digest(p.read_bytes()):raise ValueError('Asset package integrity mismatch')
        for name,sha in receipt['files'].items():
            if Path(name).name!=name:raise ValueError('Invalid receipt member')
            f=safe(p.parent/name)
            if not f.is_file() or digest(f.read_bytes())!=sha:raise ValueError('Asset package integrity mismatch')
    return p,a


@contextmanager
def transaction(target):
    safe(target);target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise ValueError('Output already exists')
    staging=Path(tempfile.mkdtemp(prefix='.pending-',dir=target.parent))
    try:
        yield staging
        for p in staging.iterdir():
            if p.is_file():
                with p.open('rb') as f:os.fsync(f.fileno())
        fsync_path(staging)
        if target.exists():raise ValueError('Concurrent output already exists')
        os.rename(staging,target);fsync_path(target.parent)
    finally:
        if staging.exists():shutil.rmtree(staging)


def receipt(output, data):
    files={p.name:digest(p.read_bytes()) for p in output.iterdir() if p.is_file() and p.name!='receipt.json'}
    save(output/'receipt.json',{'schema':'presentation-asset-receipt-v1',**data,'files':files})


def collect(candidate,instruction):
    string(instruction,3000);p=safe(candidate);a=read(p);artwork,values=validate(a);source_check(a,p)
    root=roots(a['sensitivity']=='private')[-1][1];root.mkdir(parents=True,exist_ok=True)
    lock=safe(root/'.lock')
    with lock.open('a+') as f:
        fcntl.flock(f.fileno(),fcntl.LOCK_EX)
        try:
            dest=root/a['id']/f"v{a['version']}"
            if dest.exists():
                existing,old=locate(str(dest),a['sensitivity']=='private')
                if canonical(old)!=canonical(a):raise ValueError('Immutable version already exists with different content')
                return {'status':'existing','asset':str(existing),'sha256':digest(canonical(a))}
            with transaction(dest) as out:
                save(out/'asset.json',a)
                result=render.export(artwork,a['id'],out)
                receipt(out,{'kind':'collection','asset_sha256':digest(canonical(a)),'instruction':instruction,**result})
        finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)
    return {'status':'collected','asset':str(dest/'asset.json'),'preview':str(dest/'preview.png'),'sha256':digest(canonical(a))}


def color_parameters(asset):
    """Expose authored color roles without guessing semantics from hex values."""
    return {name:spec['default'] for name,spec in asset['parameters'].items() if spec['type']=='color'}


def parse_colors(items):
    """Parse repeated CLI role=#RRGGBB entries; no expressions or implicit aliases."""
    colors={}
    for item in items:
        if not isinstance(item,str) or '=' not in item:raise ValueError('Color requires role=#RRGGBB')
        name,value=item.split('=',1);identifier(name)
        if name in colors:raise ValueError('Duplicate color role: '+name)
        colors[name]=art.color(value)
    return colors


def use(value,output,*,private=False,preset=None,values=None,colors=None,width=None,rotation=0,font='Arial',source=None,page=1,x=0,y=0,replace=()):
    p,a=locate(value,private)
    if preset and preset not in a['presets']:raise ValueError('Unknown preset')
    if colors is None:colors={}
    if not isinstance(colors,dict):raise ValueError('Colors must map declared roles to hex colors')
    for name,color in colors.items():
        if name not in color_parameters(a):raise ValueError('Unknown or non-color role: '+str(name))
        parameter(a['parameters'][name],color)
    # Validate lower-priority inputs too: an overriding color cannot hide bad values.
    validate(a,values)
    overrides={**a['presets'].get(preset,{}),**(values or {}),**colors};artwork,params=validate(a,overrides)
    art.number(rotation,-180,180);string(font,120)
    if width is not None:art.number(width,8,4096)
    private_output=a['sensitivity']=='private'
    if source:
        source=safe(source)
        if source.relative_to(REPO).parts[0]=='private':private_output=True
        if source.suffix.lower()!='.pptx' or not source.is_file():raise ValueError('Expected source PPTX')
        art.number(x,0,4096);art.number(y,0,4096)
        if width is None:raise ValueError('Insertion requires --width')
    target=safe(output,output=True,private=private_output)
    with transaction(target) as out:
        save(out/'asset.json',a);save(out/'resolved-artwork.json',artwork)
        info=render.export(artwork,a['id'],out,rotation=rotation,width=width,font=font)
        instance={'schema':'presentation-asset-instance-v1','id':a['id'],'version':a['version'],'asset_sha256':digest(canonical(a)),
                  'parameters':params,'rotation':rotation,'width':width,'font':font,'sensitivity':'private' if private_output else a['sensitivity'],**info}
        if source:
            instance['insertion']=render.insert(source,out/'asset.pptx',out/'deck.pptx',page,x,y,width,replace)
            instance['source']={'path':source.relative_to(REPO).as_posix(),'sha256':digest(source.read_bytes())}
        save(out/'instance.json',instance);receipt(out,{'kind':'instance','asset_sha256':instance['asset_sha256']})
    return {'status':'created','output':str(target),'pptx':str(target/('deck.pptx' if source else 'asset.pptx')),'preview':str(target/'preview.png'),'instance':instance}


def prepare(brief,private=False):
    p=safe(brief);b=read(p)
    if set(b)!={'id','name','brief','sensitivity'}:raise ValueError('Brief needs id,name,brief,sensitivity')
    identifier(b['id']);string(b['name'],160);string(b['brief'],12000)
    if b['sensitivity'] not in ('public','local_only','private'):raise ValueError('Invalid sensitivity')
    private=private or b['sensitivity']=='private' or p.relative_to(REPO).parts[0]=='private'
    if private:b['sensitivity']='private'
    # Agent task outputs must be temp; private details are not copied into task manifests.
    if private:raise ValueError('Private assets: author candidate directly under private/ and use check/collect -- private sensitivity; prepare is local_only/public')
    target=safe(REPO/'temp/presentation-assets'/uuid.uuid4().hex,output=True)
    with transaction(target) as out:
        save(out/'brief.json',b)
        save(out/'request.json',{'brief_sha256':digest(canonical(b))})
        rel=target.relative_to(REPO).as_posix()
        command='python3 .scripts/wg.py presentation-asset '
        task=agent_task.make_task(kind='presentation-asset',transaction_id=target.name,
             inputs=[{'name':'brief','path':rel+'/brief.json','read':'required'},{'name':'protocol','path':'operations/PRESENTATION_ASSET.md','read':'required'},{'name':'example','path':'operations/templates/presentation-assets/circular-dual-arrow/asset.json','read':'optional'}],
             outputs=[{'name':'candidate','path':rel+'/candidate.json','format':'json'}],
             protocol={'name':SCHEMA,'contract':'operations/PRESENTATION_ASSET.md','source':'generated','sensitivity':b['sensitivity']},
             commands={'check':command+'check --candidate '+shlex.quote(rel+'/candidate.json'),
                       'commit':command+'collect --candidate '+shlex.quote(rel+'/candidate.json')+' --instruction '+shlex.quote(b['brief'])},
             context={'creator':'agent','brief_sha256':digest(canonical(b))})
        save(out/'agent-task.json',task)
    return {'status':'prepared','run':str(target),'agent_task':task}


def extract(source, page, names, output, asset_id, name, origin, instruction, sensitivity='local_only'):
    """Explicit flat native custom paths only. Reject unsupported styles/dependencies."""
    from zipfile import ZipFile
    from lxml import etree as LX
    import posixpath
    identifier(asset_id);string(name,160);string(instruction,3000)
    source=safe(source);rel=source.relative_to(REPO)
    if source.suffix.lower()!='.pptx':raise ValueError('Expected PPTX source')
    if origin=='source-derived' and (len(rel.parts)<3 or rel.parts[1]!='raw'):
        raise ValueError('Source-derived extraction requires archived Raw; generated requires explicit authorship declaration')
    if rel.parts[0]=='private':sensitivity='private'
    if not names or len(names)>30 or len(names)!=len(set(names)):raise ValueError('Select 1..30 distinct object names')
    objects=[];bounds=[];ns=render.NS
    with ZipFile(source) as z:
        if len(z.namelist())!=len(set(z.namelist())):raise ValueError('Duplicate package entries')
        pres=LX.fromstring(z.read('ppt/presentation.xml'));slides=pres.findall('p:sldIdLst/p:sldId',ns)
        if not 1<=page<=len(slides):raise ValueError('Invalid page')
        rid=slides[page-1].get('{'+ns['r']+'}id')
        target=next(r.get('Target') for r in LX.fromstring(z.read('ppt/_rels/presentation.xml.rels')) if r.get('Id')==rid)
        entry=target.lstrip('/') if target.startswith('/') else posixpath.normpath('ppt/'+target)
        tree=LX.fromstring(z.read(entry)).find('p:cSld/p:spTree',ns)
        for selected in names:
            hits=[e for e in tree if e.find('p:nvSpPr/p:cNvPr',ns) is not None and e.find('p:nvSpPr/p:cNvPr',ns).get('name')==selected]
            if len(hits)!=1:raise ValueError('Select unique top-level native custom paths')
            el=hits[0];sp=el.find('p:spPr',ns);xf=sp.find('a:xfrm',ns)
            if xf is None or any(k in xf.attrib for k in ('rot','flipH','flipV')):raise ValueError('Rotated/flipped source path not supported')
            if any(t.text for t in el.findall('.//a:t',ns)) or el.findall('.//a:blip',ns) or len(sp.find('a:effectLst',ns) or []):raise ValueError('Text/media/effects are outside bounded path extraction')
            fill=sp.find('a:solidFill/a:srgbClr',ns)
            if fill is None or len(fill):raise ValueError('Only explicit opaque RGB fills supported')
            ln=sp.find('a:ln',ns)
            if ln is not None and ln.find('a:noFill',ns) is None and ln.get('w')!='0':raise ValueError('Stroked source paths not supported')
            off,ext=xf.find('a:off',ns),xf.find('a:ext',ns)
            x,y,w,h=[float(e.get(k))/render.EMU for e,k in ((off,'x'),(off,'y'),(ext,'cx'),(ext,'cy'))]
            paths=sp.findall('a:custGeom/a:pathLst/a:path',ns)
            if len(paths)!=1:raise ValueError('Single explicit custom path required')
            path=paths[0];pw,ph=float(path.get('w')),float(path.get('h'));commands=[]
            if pw<=0 or ph<=0:raise ValueError('Invalid path units')
            for cmd in path:
                tag=LX.QName(cmd).localname
                if tag not in ('moveTo','lnTo','cubicBezTo','close'):raise ValueError('Unsupported source path command')
                c=[{'moveTo':'M','lnTo':'L','cubicBezTo':'C','close':'Z'}[tag]]
                for pt in cmd:
                    c.extend([float(pt.get('x'))/pw*w,float(pt.get('y'))/ph*h])
                commands.append(c)
            objects.append({'type':'path','x':x,'y':y,'w':w,'h':h,'fill':'#'+fill.get('val'),'stroke':'none','stroke_width':0,'commands':commands})
            bounds.append((x,y,x+w,y+h))
    x0,y0=min(b[0] for b in bounds),min(b[1] for b in bounds)
    canvas=[max(b[2] for b in bounds)-x0,max(b[3] for b in bounds)-y0]
    for o in objects:o['x']-=x0;o['y']-=y0
    a={'schema':SCHEMA,'id':asset_id,'version':1,'name':name,'category':'关系表达','purpose':instruction,
       'tags':[name,'原生曲线'],'sensitivity':sensitivity,'source':{'kind':origin,'description':f'Page {page}; objects: {names}. {instruction}',
       'files':[{'path':rel.as_posix(),'sha256':digest(source.read_bytes())}]},'parameters':{},'presets':{},
       'artwork':{'schema':'presentation-artwork-v2','canvas':canvas,'background':'none','rationale':'明确选择的原生曲线对象，保持几何及实色','objects':objects}}
    validate(a)
    target=safe(output,output=True,private=sensitivity=='private')
    with transaction(target) as out:
        save(out/'candidate.json',a);save(out/'extraction.json',{'source':a['source'],'page':page,'objects':names,'original_box':[x0,y0,*canvas]})
    return {'status':'prepared','candidate':str(target/'candidate.json'),'source':str(source),'objects':len(objects)}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('extract');q.add_argument('--source',required=True);q.add_argument('--page',type=int,required=True);q.add_argument('--object',dest='names',action='append',required=True);q.add_argument('--output',required=True);q.add_argument('--id',dest='asset_id',required=True);q.add_argument('--name',required=True);q.add_argument('--origin',choices=['generated','source-derived'],default='source-derived');q.add_argument('--instruction',required=True);q.add_argument('--sensitivity',choices=['public','local_only','private'],default='local_only')
    q=sub.add_parser('prepare');q.add_argument('--brief',required=True);q.add_argument('--private',action='store_true')
    q=sub.add_parser('check');q.add_argument('--candidate',required=True)
    q=sub.add_parser('collect');q.add_argument('--candidate',required=True);q.add_argument('--instruction',required=True)
    q=sub.add_parser('search');q.add_argument('query',nargs='?',default='');q.add_argument('--private',action='store_true')
    q=sub.add_parser('show');q.add_argument('--asset',required=True);q.add_argument('--private',action='store_true')
    for name in ('use','insert'):
        q=sub.add_parser(name);q.add_argument('--asset',required=True);q.add_argument('--output',required=True);q.add_argument('--private',action='store_true')
        q.add_argument('--preset');q.add_argument('--values');q.add_argument('--width',type=float);q.add_argument('--rotation',type=float,default=0);q.add_argument('--font',default='Arial')
        q.add_argument('--color',action='append',default=[],help='Repeat role=#RRGGBB; overrides preset and values')
        if name=='insert':
            q.add_argument('--source',required=True);q.add_argument('--page',type=int,default=1);q.add_argument('--x',type=float,required=True);q.add_argument('--y',type=float,required=True);q.add_argument('--replace',action='append',default=[])
    args=p.parse_args(argv)
    try:
        if args.command=='extract':result=extract(**{k:v for k,v in vars(args).items() if k!='command'})
        elif args.command=='prepare':result=prepare(args.brief,args.private)
        elif args.command=='check':
            a=read(args.candidate);validate(a);source_check(a,safe(args.candidate));result={'status':'valid','id':a['id'],'sha256':digest(canonical(a))}
        elif args.command=='collect':result=collect(args.candidate,args.instruction)
        elif args.command=='search':
            terms=args.query.lower().split();items=catalog(args.private)
            for row in items:row['score']=sum(t in ' '.join([row['id'],row['name'],row['purpose'],*row['tags']]).lower() for t in terms)
            items=[r for r in items if not terms or r['score']];items.sort(key=lambda r:(r['score'],r['version'],r['origin']!='builtin'),reverse=True)
            result={'status':'ok','assets':items[:30],'total':len(items)}
        elif args.command=='show':
            path,a=locate(args.asset,args.private);result={'status':'ok','asset':a,'path':str(path),'sha256':digest(canonical(a)),
                'color_parameters':color_parameters(a),
                'previews':[str(path.parent/n) for n in ('preview.png','asset.svg') if (path.parent/n).exists()]}
        else:
            options={k:getattr(args,k) for k in ('private','preset','width','rotation','font')}
            if args.values and safe(args.values).relative_to(REPO).parts[0]=='private':
                safe(args.output,output=True,private=True)
            if args.command=='insert':options.update({k:getattr(args,k) for k in ('source','page','x','y','replace')})
            result=use(args.asset,args.output,values=read(args.values) if args.values else None,colors=parse_colors(args.color),**options)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
        print(json.dumps({'status':'error','error':str(exc)},ensure_ascii=False));return 1


if __name__=='__main__':raise SystemExit(main())

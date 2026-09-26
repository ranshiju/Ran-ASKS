#!/usr/bin/env python3
"""Read-only OOXML style/component inventory. No rendering, network or semantic claims."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart'}
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
SCHEMA = 'pptx-structure-v1'

def stable_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def relationships(z, part):
    rp = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    if rp not in z.namelist():
        return {}
    result = {}
    for r in ET.fromstring(z.read(rp)):
        external = r.get('TargetMode') == 'External'
        target = r.get('Target', '')
        if not external:
            target = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
            if target.startswith('../'):
                raise ValueError('Invalid package relationship')
        result[r.get('Id')] = {'part': target, 'kind': r.get('Type', '').rsplit('/', 1)[-1], 'external': external}
    return result

def linked(z, part, kind):
    for r in relationships(z, part).values():
        if r['kind'] == kind and not r['external']:
            return r['part'], ET.fromstring(z.read(r['part']))
    return '', None

def attrs_xml(el):
    if el is None:
        return None
    return {'tag': el.tag.rsplit('}', 1)[-1], 'attributes': dict(el.attrib),
            'children': [attrs_xml(x) for x in el]}

def union_area(rects):
    """Exact union of supplied axis-aligned clipped rectangles (not pixel coverage)."""
    xs = sorted({x for a,b,c,d in rects for x in (a,c) if c > a and d > b})
    total = 0
    for left,right in zip(xs, xs[1:]):
        intervals = sorted((b,d) for a,b,c,d in rects if a < right and c > left and d > b)
        end = -math.inf
        length = 0
        for a,b in intervals:
            length += max(0, b-max(a,end)); end=max(end,b)
        total += (right-left)*length
    return total

def mul(a,b):
    return [[sum(a[i][k]*b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

IDENTITY = [[1,0,0],[0,1,0],[0,0,1]]
def affine(x=0,y=0,sx=1,sy=1):
    return [[sx,0,x],[0,sy,y],[0,0,1]]

def transform(shape, parent, inherited=None):
    t = shape.find('p:spPr/a:xfrm', NS)
    if t is None: t=shape.find('a:xfrm',NS)
    if t is None: t=shape.find('p:xfrm',NS)
    if t is None: t=shape.find('p:grpSpPr/a:xfrm',NS)
    if t is None and inherited is not None:
        return transform(inherited, parent)
    if t is None: return None, parent, None
    off=t.find('a:off',NS); ext=t.find('a:ext',NS)
    if off is None or ext is None: return None,parent,attrs_xml(t)
    x,y,w,h=[float(el.get(key,0)) for el,key in ((off,'x'),(off,'y'),(ext,'cx'),(ext,'cy'))]
    angle=float(t.get('rot',0))/60000*math.pi/180
    co,si=math.cos(angle),math.sin(angle)
    rotate=[[co,-si,0],[si,co,0],[0,0,1]]
    orient=mul(affine(x+w/2,y+h/2),mul(rotate,mul(affine(sx=-1 if t.get('flipH')=='1' else 1,sy=-1 if t.get('flipV')=='1' else 1),affine(-w/2,-h/2))))
    local=mul(parent,orient)
    pts=[(local[0][0]*a+local[0][1]*b+local[0][2],local[1][0]*a+local[1][1]*b+local[1][2]) for a,b in ((0,0),(w,0),(0,h),(w,h))]
    box=[min(p[0] for p in pts),min(p[1] for p in pts),max(p[0] for p in pts),max(p[1] for p in pts)]
    ch=t.find('a:chOff',NS); ce=t.find('a:chExt',NS)
    if ch is not None and ce is not None:
        cw,chh=float(ce.get('cx',0)),float(ce.get('cy',0))
        if not cw or not chh: return box,local,attrs_xml(t)
        local=mul(local,mul(affine(sx=w/cw,sy=h/chh),affine(-float(ch.get('x',0)),-float(ch.get('y',0)))))
    return box,local,attrs_xml(t)

def placeholder(shape):
    return shape.find('.//p:ph',NS)

def matching(root, shape):
    if root is None: return None
    ph=placeholder(shape)
    if ph is None: return None
    candidates=root.findall('.//p:sp',NS)
    for candidate in candidates:
        q=placeholder(candidate)
        if q is not None and q.get('idx','0')==ph.get('idx','0'): return candidate
    for candidate in candidates:
        q=placeholder(candidate)
        if q is not None and q.get('type','body')==ph.get('type','body'): return candidate
    return None

def run_styles(shape, inherited, master, presentation, theme):
    result=[]
    ph=placeholder(shape)
    category='titleStyle' if ph is not None and ph.get('type') in ('title','ctrTitle') else ('bodyStyle' if ph is not None else 'otherStyle')
    for p in shape.findall('.//a:p',NS):
        pp=p.find('a:pPr',NS); level=int(pp.get('lvl',0)) if pp is not None else 0
        sources=[]
        def add(label, el):
            if el is not None: sources.append((label,el))
        add('presentation.defaultTextStyle',presentation.find(f'p:defaultTextStyle/a:lvl{level+1}pPr/a:defRPr',NS))
        if master is not None: add('master.'+category,master.find(f'p:txStyles/p:{category}/a:lvl{level+1}pPr/a:defRPr',NS))
        for label, obj in list(reversed(inherited))+[('shape',shape)]:
            if obj is None: continue
            add(label+'.listStyle',obj.find(f'p:txBody/a:lstStyle/a:lvl{level+1}pPr/a:defRPr',NS))
            if obj is not shape:
                add(label+'.paragraph',obj.find('p:txBody/a:p/a:pPr/a:defRPr',NS))
        add('paragraph',p.find('a:pPr/a:defRPr',NS))
        for run in list(p.findall('a:r',NS))+list(p.findall('a:fld',NS)):
            text=''.join(run.itertext())
            style={}; provenance={}
            for label,properties in sources+[('run',run.find('a:rPr',NS))]:
                if properties is None: continue
                for key,value in properties.attrib.items():
                    style[key]=value; provenance[key]=label
                for tag in ('latin','ea','cs','solidFill','highlight'):
                    el=properties.find('a:'+tag,NS)
                    if el is not None: style[tag]=attrs_xml(el); provenance[tag]=label
            fonts={}
            for script in ('latin','ea','cs'):
                val=style.get(script,{}).get('attributes',{}).get('typeface')
                resolved=val
                if val and val.startswith('+') and theme is not None:
                    family='majorFont' if val.startswith('+mj') else 'minorFont'
                    e=theme.find(f'a:themeElements/a:fontScheme/a:{family}/a:{script}',NS)
                    resolved=e.get('typeface') if e is not None else None
                    if not resolved and script=='ea':
                        e=theme.find(f'a:themeElements/a:fontScheme/a:{family}/a:font[@script="Hans"]',NS)
                        resolved=e.get('typeface') if e is not None else None
                fonts[script]={'declared':val,'theme_resolved':resolved or None,'rendered':None}
            result.append({'text':text,'level':level,'font_size_pt':float(style['sz'])/100 if 'sz' in style else None,
                           'fonts':fonts,'properties':style,'provenance':provenance,'paragraph':attrs_xml(pp)})
    return result

def extract(source):
    source=Path(source)
    with zipfile.ZipFile(source) as z:
        pres=ET.fromstring(z.read('ppt/presentation.xml'))
        size=pres.find('p:sldSz',NS)
        width,height=int(size.get('cx')),int(size.get('cy'))
        rels=relationships(z,'ppt/presentation.xml'); pages=[]
        for n,ref in enumerate(pres.findall('p:sldIdLst/p:sldId',NS),1):
            part=rels[ref.get('{'+NS['r']+'}id')]['part']
            root=ET.fromstring(z.read(part))
            lp,layout=linked(z,part,'slideLayout'); mp,master=linked(z,lp,'slideMaster') if lp else ('',None)
            tp,theme=linked(z,mp,'theme') if mp else ('',None)
            objects=[]; warnings=[]
            def visit(tree,parent=IDENTITY,group=None):
                if tree is None: return
                for shape in tree:
                    tag=shape.tag.rsplit('}',1)[-1]
                    if tag not in ('sp','pic','grpSp','graphicFrame','cxnSp'):
                        if tag not in ('nvGrpSpPr','grpSpPr','extLst'): warnings.append('Unsupported object: '+tag)
                        continue
                    nv=shape.find('.//p:cNvPr',NS)
                    sid=nv.get('id') if nv is not None else None
                    inherited=[('layout',matching(layout,shape)),('master',matching(master,shape))]
                    fallback=next((e for _,e in inherited if e is not None),None)
                    box,childmat,raw=transform(shape,parent,fallback)
                    text='\n'.join(''.join(p.itertext()) for p in shape.findall('.//a:p',NS)) if tag!='grpSp' else ''
                    obj={'id':sid,'name':nv.get('name','') if nv is not None else '', 'kind':tag,'group_id':group,
                         'bbox_emu':box,'bbox_normalized':[box[0]/width,box[1]/height,box[2]/width,box[3]/height] if box else None,
                         'transform':raw,'text':text,'styles':run_styles(shape,inherited,master,pres,theme) if tag!='grpSp' else [],
                         'shape_properties':attrs_xml(shape.find('p:spPr',NS)), 'editable':tag!='pic'}
                    if box is None: warnings.append('Missing resolved geometry: '+str(sid))
                    if tag=='cxnSp':
                        obj['connections']=[{'endpoint':q.tag.rsplit('}',1)[-1],**q.attrib} for q in shape.findall('.//a:stCxn',NS)+shape.findall('.//a:endCxn',NS)]
                    if tag=='pic':
                        blip=shape.find('.//a:blip',NS)
                        rid=blip.get('{'+NS['r']+'}embed') if blip is not None else None
                        relation=relationships(z,part).get(rid)
                        obj['image']={'relationship':relation,'crop':attrs_xml(shape.find('.//a:srcRect',NS))}
                        if relation and not relation['external']: obj['image']['sha256']=hashlib.sha256(z.read(relation['part'])).hexdigest()
                    table=shape.find('.//a:tbl',NS)
                    if table is not None:
                        obj['kind']='table'
                        obj['table']={'columns_emu':[int(c.get('w')) for c in table.findall('a:tblGrid/a:gridCol',NS)],
                          'rows':[{'height_emu':int(r.get('h',0)),'cells':[{'text':'\n'.join(''.join(p.itertext()) for p in c.findall('.//a:p',NS)), 'merge':dict(c.attrib),'properties':attrs_xml(c.find('a:tcPr',NS))} for c in r.findall('a:tc',NS)]} for r in table.findall('a:tr',NS)],'properties':attrs_xml(table.find('a:tblPr',NS))}
                    chart=shape.find('.//c:chart',NS)
                    if chart is not None:
                        cr=relationships(z,part).get(chart.get('{'+NS['r']+'}id'))
                        obj['kind']='chart'; obj['chart']={'relationship':cr,'data_status':'unknown'}
                        if cr and not cr['external']:
                            croot=ET.fromstring(z.read(cr['part']))
                            obj['chart'].update(data_status='cached_not_recalculated', structure=attrs_xml(croot),dependencies=list(relationships(z,cr['part']).values()))
                    if tag=='graphicFrame' and table is None and chart is None:
                        obj['editable']=False; obj['limitations']=['Unsupported embedded/SmartArt object; preserved, no editable reconstruction']
                    objects.append(obj)
                    if tag=='grpSp': visit(shape,childmat,sid)
            visit(root.find('p:cSld/p:spTree',NS))
            # Master/shape backgrounds are not content. Full-canvas objects are only
            # candidates, so expose both estimates instead of silently declaring them decorative.
            rects=[]; exclusions=[]; allrects=[]
            for o in objects:
                b=o['bbox_emu']
                if not b or o['kind']=='grpSp': continue
                clipped=[max(0,b[0]),max(0,b[1]),min(width,b[2]),min(height,b[3])]
                allrects.append(clipped)
                candidate=(not o['text'].strip() and (b[2]-b[0])*(b[3]-b[1])>=.95*width*height and o['kind'] in ('sp','pic'))
                if candidate: exclusions.append({'id':o['id'],'reason':'full_canvas_background_candidate_not_semantically_confirmed'})
                else: rects.append(clipped)
            notespart,notes=linked(z,part,'notesSlide')
            notes_text=[]
            if notes is not None:
                for sh in notes.findall('.//p:sp',NS):
                    ph=placeholder(sh)
                    if ph is None or ph.get('type')=='body': notes_text+=[''.join(p.itertext()) for p in sh.findall('.//a:p',NS)]
            pages.append({'number':n,'part':part,'hidden':root.get('show')=='0','objects':objects,'notes':'\n'.join(notes_text),
                          'dependencies':{'layout':lp,'master':mp,'theme':tp},
                          'coverage':{'method':'clipped_axis_aligned_bbox_union_estimate','ratio':union_area(rects)/(width*height),
                                      'including_background_candidates_ratio':union_area(allrects)/(width*height),'excluded':exclusions,
                                      'limitations':['Images may contain internal whitespace; rotation uses bounding boxes; not a readability score.','Master decorations not counted.']},
                          'limitations':warnings})
    return {'schema':SCHEMA,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'canvas':{'width_emu':width,'height_emu':height},'pages':pages}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source'); parser.add_argument('--output')
    args=parser.parse_args(); result=extract(args.source)
    if args.output:
        out=Path(args.output).resolve(); repo=Path(__file__).resolve().parent.parent
        if not out.is_relative_to(repo/'temp'): parser.error('Output must be under repository temp/')
        out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'output':str(out),'pages':len(result['pages'])}))
    else: print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()

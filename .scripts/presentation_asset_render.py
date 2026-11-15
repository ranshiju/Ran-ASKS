#!/usr/bin/env python3
"""Local rendering of validated vector assets, with native editable PPTX groups."""
from __future__ import annotations

import copy
import math
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_DEFLATED

import presentation_artwork as art
import presentation_runtime as runtime

EMU = 9525
NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}


def arc_arrow(item):
    """Generic annular arc arrow; supports independent arcs and multi-step cycles."""
    fields = {'type','cx','cy','radius','width','start','sweep','gap','head_angle','fill'}
    if set(item) != fields:
        raise ValueError('Invalid arc_arrow fields')
    cx, cy = [art.number(item[k], 1, 4096) for k in ('cx','cy')]
    r = art.number(item['radius'], 4, 2048)
    w = art.number(item['width'], .5, r*.65)
    start = art.number(item['start'], -360, 360)
    sweep = art.number(item['sweep'], 30, 330)
    gap = art.number(item['gap'], 0, 30)
    head = art.number(item['head_angle'], 8, 45)
    if sweep <= 2*gap+head+5:
        raise ValueError('Arc too short for gap and arrowhead')
    art.color(item['fill'])
    outer, inner, shoulder = r+w/2, r-w/2, r+w*1.25
    if min(cx,cy) < shoulder:
        raise ValueError('Arc extends before canvas origin')
    start, end = start+gap, start+sweep-gap
    base = end-head
    def point(rad, deg):
        theta = math.radians(deg)
        return [shoulder+rad*math.cos(theta), shoulder+rad*math.sin(theta)]
    commands = [['M', *point(outer,start)]]
    def arc(rad, a, b):
        count = math.ceil(abs(b-a)/60)
        for i in range(count):
            aa, bb = a+(b-a)*i/count, a+(b-a)*(i+1)/count
            pa, pb = point(rad,aa), point(rad,bb)
            aa,bb=math.radians(aa),math.radians(bb)
            k=4/3*math.tan((bb-aa)/4)
            commands.append(['C',pa[0]-k*rad*math.sin(aa),pa[1]+k*rad*math.cos(aa),
                             pb[0]+k*rad*math.sin(bb),pb[1]-k*rad*math.cos(bb),*pb])
    arc(outer,start,base)
    commands.extend([['L',*point(shoulder,base)],['L',*point(r,end)],
                     ['L',*point(max(.1,r-w*1.25),base)],['L',*point(inner,base)]])
    arc(inner,base,start)
    commands.append(['Z'])
    return {'type':'path','x':cx-shoulder,'y':cy-shoulder,'w':2*shoulder,'h':2*shoulder,
            'fill':item['fill'],'stroke':'none','stroke_width':0,'commands':commands}


def rotated_size(canvas, rotation):
    a=math.radians(rotation)
    w,h=canvas
    return [abs(w*math.cos(a))+abs(h*math.sin(a)),abs(w*math.sin(a))+abs(h*math.cos(a))]


def svg_asset(artwork, rotation=0, width=None, font='Arial'):
    svg,_=art.export_artwork(artwork)
    root=ET.fromstring(svg)
    w,h=artwork['canvas']; nw,nh=rotated_size([w,h],rotation)
    scale=(width or nw)/nw
    root.set('width',str(nw*scale));root.set('height',str(nh*scale))
    root.set('viewBox',f'0 0 {nw} {nh}')
    group=ET.Element('{http://www.w3.org/2000/svg}g')
    group.set('transform',f'translate({nw/2} {nh/2}) rotate({rotation}) translate({-w/2} {-h/2})')
    for child in list(root):root.remove(child);group.append(child)
    root.append(group)
    for node in root.iter():
        if node.tag.endswith('}text'):node.set('font-family',font)
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    return ET.tostring(root,encoding='unicode'),[nw*scale,nh*scale]


def native_deck(artwork, asset_id, rotation=0, width=None, font='Arial'):
    runtime._load_pptx()
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
    from pptx.enum.text import PP_ALIGN
    from pptx.dml.color import RGBColor
    from pptx.util import Pt
    from pptx.oxml.xmlchemy import OxmlElement
    from pptx.oxml.ns import qn
    deck=Presentation()
    w,h=artwork['canvas']; nw,nh=rotated_size([w,h],rotation); scale=(width or nw)/nw
    deck.slide_width=round(max(96,nw*scale)*EMU);deck.slide_height=round(max(96,nh*scale)*EMU)
    slide=deck.slides.add_slide(deck.slide_layouts[6]);slide.background.fill.background()
    group=slide.shapes.add_group_shape();group.name='asset:'+asset_id
    def node(tag, **attrs):
        el=OxmlElement(tag)
        for key,value in attrs.items():el.set(key,str(value))
        return el
    if artwork['background']!='none':
        bg=group.shapes.add_shape(MSO_SHAPE.RECTANGLE,0,0,round(w*EMU),round(h*EMU))
        bg.name='asset-background';bg.fill.solid();bg.fill.fore_color.rgb=RGBColor.from_string(artwork['background'][1:]);bg.line.fill.background()
    for i,a in enumerate(artwork['objects']):
        kind=a['type']
        if kind=='line':
            sh=group.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,*[round(a[k]*EMU) for k in ('x1','y1','x2','y2')])
        else:
            args=[round(a[k]*EMU) for k in ('x','y','w','h')]
            sh=(group.shapes.add_textbox(*args) if kind=='text' else
                group.shapes.add_shape(MSO_SHAPE.OVAL if kind=='ellipse' else MSO_SHAPE.ROUNDED_RECTANGLE if kind=='rect' and a['radius'] else MSO_SHAPE.RECTANGLE,*args))
        sh.name=f'asset:{asset_id}:{i+1}'
        for style in list(sh._element):
            if style.tag==qn('p:style'):sh._element.remove(style)
        if kind=='text':
            tf=sh.text_frame;tf.clear();tf.word_wrap=False
            tf.margin_left=tf.margin_right=tf.margin_top=tf.margin_bottom=0
            for j,line in enumerate(a['text'].split('\n')):
                p=tf.paragraphs[0] if j==0 else tf.add_paragraph();p.text=line
                p.alignment={'left':PP_ALIGN.LEFT,'center':PP_ALIGN.CENTER,'right':PP_ALIGN.RIGHT}[a['align']]
                p.font.name=font;p.font.size=Pt(a['size']*.75);p.font.bold=a['bold'];p.font.color.rgb=RGBColor.from_string(a['color'][1:])
            sh.fill.background();sh.line.fill.background()
            continue
        if kind!='line':
            if a['fill']=='none':sh.fill.background()
            else:sh.fill.solid();sh.fill.fore_color.rgb=RGBColor.from_string(a['fill'][1:])
        if a['stroke']=='none' or a['stroke_width']==0:sh.line.fill.background()
        else:sh.line.color.rgb=RGBColor.from_string(a['stroke'][1:]);sh.line.width=Pt(a['stroke_width']*.75)
        if kind=='rect' and a['radius']:
            sh.adjustments[0]=a['radius']/min(a['w'],a['h'])
        if kind=='path':
            sp=sh._element.spPr
            for geom in list(sp):
                if geom.tag in (qn('a:prstGeom'),qn('a:custGeom')):sp.remove(geom)
            geom=node('a:custGeom')
            for tag in ('a:avLst','a:gdLst','a:ahLst','a:cxnLst'):geom.append(node(tag))
            geom.append(node('a:rect',l='0',t='0',r='r',b='b'))
            paths=node('a:pathLst');path=node('a:path',w=round(a['w']*1000),h=round(a['h']*1000))
            for c in a['commands']:
                cmd=node({'M':'a:moveTo','L':'a:lnTo','C':'a:cubicBezTo','Z':'a:close'}[c[0]])
                for j in range(1,len(c),2):cmd.append(node('a:pt',x=round(c[j]*1000),y=round(c[j+1]*1000)))
                path.append(cmd)
            paths.append(path);geom.append(paths);sp.insert(1,geom)
    # Explicit canvas transform keeps padding, rotations and child coordinates consistent.
    xf=group._element.grpSpPr.get_or_add_xfrm()
    for child in list(xf):xf.remove(child)
    xf.set('rot',str(round(rotation*60000)))
    xf.append(node('a:off',x=round((nw-w)*scale/2*EMU),y=round((nh-h)*scale/2*EMU)))
    xf.append(node('a:ext',cx=round(w*scale*EMU),cy=round(h*scale*EMU)))
    xf.append(node('a:chOff',x=0,y=0));xf.append(node('a:chExt',cx=round(w*EMU),cy=round(h*EMU)))
    return deck


def export(artwork, asset_id, output, *, rotation=0, width=None, font='Arial'):
    svg,size=svg_asset(artwork,rotation,width,font)
    if max(size)>5376 or size[0]*size[1]>16_777_216:
        raise ValueError('Export exceeds Office page or preview pixel limits')
    (output/'asset.svg').write_text(svg,encoding='utf-8')
    native_deck(artwork,asset_id,rotation,width,font).save(output/'asset.pptx')
    import fitz
    doc=fitz.open(stream=svg.encode(),filetype='svg')
    pdf=fitz.open('pdf',doc.convert_to_pdf())
    pix=pdf[0].get_pixmap(matrix=fitz.Matrix(2,2),alpha=True)
    pix.save(output/'preview.png');pdf.close();doc.close()
    import hashlib
    generator={Path(p).name:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (__file__,art.__file__)}
    return {'canvas':size,'generator':generator,'formats':{'asset.pptx':'native-editable-group','asset.svg':'vector','preview.png':'transparent-raster'},'remote_calls':0}


def insert(source, fragment, output, page, x, y, width, replace_names=()):
    """Only replace selected slide XML, preserving all other ZIP parts byte-for-byte."""
    from lxml import etree as LX
    with ZipFile(source) as src,ZipFile(fragment) as frag:
        if len(src.namelist())!=len(set(src.namelist())):raise ValueError('Duplicate package entries')
        pres=LX.fromstring(src.read('ppt/presentation.xml'))
        refs=pres.findall('p:sldIdLst/p:sldId',NS)
        if not 1<=page<=len(refs):raise ValueError('Slide outside deck')
        rels=LX.fromstring(src.read('ppt/_rels/presentation.xml.rels'))
        rid=refs[page-1].get('{'+NS['r']+'}id')
        target=next(r.get('Target') for r in rels if r.get('Id')==rid)
        import posixpath
        name=target.lstrip('/') if target.startswith('/') else posixpath.normpath('ppt/'+target)
        if not name.startswith('ppt/slides/'):raise ValueError('Invalid slide relationship')
        root=LX.fromstring(src.read(name));tree=root.find('p:cSld/p:spTree',NS)
        matches={n:[] for n in replace_names}
        for el in list(tree):
            props=el.find('.//p:cNvPr',NS)
            if props is not None and props.get('name') in matches:matches[props.get('name')].append(el)
        if any(len(v)!=1 for v in matches.values()):raise ValueError('Replacement names must identify unique top-level objects')
        for values in matches.values():tree.remove(values[0])
        group=copy.deepcopy(LX.fromstring(frag.read('ppt/slides/slide1.xml')).find('.//p:grpSp',NS))
        max_id=max([int(e.get('id')) for e in root.findall('.//p:cNvPr',NS)]+[1])
        for i,e in enumerate(group.findall('.//p:cNvPr',NS)):e.set('id',str(max_id+i+1))
        frame=group.find('p:grpSpPr/a:xfrm',NS);ext=frame.find('a:ext',NS)
        fw,fh=rotated_size([int(ext.get('cx'))/EMU,int(ext.get('cy'))/EMU],int(frame.get('rot','0'))/60000)
        scale=width/fw
        decksz=pres.find('p:sldSz',NS);dw,dh=int(decksz.get('cx'))/EMU,int(decksz.get('cy'))/EMU
        if x<0 or y<0 or x+width>dw+.01 or y+fh*scale>dh+.01:raise ValueError('Insertion outside slide')
        # Outer placement group preserves the fragment's rotation and canvas transform.
        wrapper=LX.Element('{'+NS['p']+'}grpSp',nsmap=None)
        nv=LX.SubElement(wrapper,'{'+NS['p']+'}nvGrpSpPr')
        LX.SubElement(nv,'{'+NS['p']+'}cNvPr',id=str(max_id+len(group.findall('.//p:cNvPr',NS))+1),name='asset-instance')
        LX.SubElement(nv,'{'+NS['p']+'}cNvGrpSpPr');LX.SubElement(nv,'{'+NS['p']+'}nvPr')
        pr=LX.SubElement(wrapper,'{'+NS['p']+'}grpSpPr');xf=LX.SubElement(pr,'{'+NS['a']+'}xfrm')
        for tag,attrs in [('off',{'x':round(x*EMU),'y':round(y*EMU)}),('ext',{'cx':round(width*EMU),'cy':round(fh*scale*EMU)}),('chOff',{'x':0,'y':0}),('chExt',{'cx':round(fw*EMU),'cy':round(fh*EMU)})]:
            LX.SubElement(xf,'{'+NS['a']+'}'+tag,**{k:str(v) for k,v in attrs.items()})
        wrapper.append(group);tree.append(wrapper)
        xml=LX.tostring(root,xml_declaration=True,encoding='UTF-8',standalone=True)
        with ZipFile(output,'w',ZIP_DEFLATED) as out:
            for info in src.infolist():out.writestr(info,xml if info.filename==name else src.read(info.filename))
    return {'page':page,'box':[x,y,width,fh*scale],'replaced':list(replace_names)}

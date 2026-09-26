#!/usr/bin/env python3
"""Deterministic, explicitly mapped direct/adaptable slide reuse. No model calls."""
from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.dom import minidom

import pptx_structure as structure
import slide_library as library

PROFILE_SCHEMA = 'slide-reuse-v1'
COMPONENT_SCHEMA = 'slide-library-component-v2'
KINDS = {'direct': '直接使用型', 'adaptable': '套用填充型'}
require = library.require


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def safe_input(path):
    path = Path(path).absolute()
    require(not any(p.is_symlink() for p in [path, *path.parents]), 'Symlink input is not allowed')
    require(path.is_file(), 'Input file missing: ' + str(path))
    return path.resolve()


def text_capacity(slot, value):
    require(isinstance(value, str) and bool(value.strip()), 'Empty text: ' + slot['id'])
    require(not any(ord(c) < 32 and c != '\n' for c in value), 'Unsupported control character')
    lines = value.split('\n')
    require(len(lines) <= slot['max_lines'], 'Too many lines: ' + slot['id'])
    require(len(value) <= slot['max_chars'], 'Text capacity exceeded: ' + slot['id'])
    require(all(len(line) <= slot['max_chars_per_line'] for line in lines),
            'Line capacity exceeded: ' + slot['id'])


def validate_profile(profile, data):
    require(isinstance(profile, dict) and set(profile) == {
        'schema', 'kind', 'use_when', 'constraints', 'slots', 'fixed_objects'}, 'Invalid reuse profile fields')
    require(profile['schema'] == PROFILE_SCHEMA and profile['kind'] in KINDS, 'Invalid reuse kind/schema')
    for field in ('use_when', 'constraints'):
        require(isinstance(profile[field], list) and profile[field]
                and all(isinstance(v, str) and v.strip() for v in profile[field]), 'Missing ' + field)
    require(isinstance(profile['slots'], list) and isinstance(profile['fixed_objects'], list), 'Invalid mappings')
    objects = {(p['number'], str(o['id'])): o for p in data['pages'] for o in p['objects']}
    used, ids = set(), set()
    for slot in profile['slots']:
        common = {'id', 'page', 'object_id', 'type', 'label', 'guidance', 'placeholder'}
        require(isinstance(slot, dict) and slot.get('type') in {'text', 'image'}, 'Unsupported slot type')
        expected = common | ({'max_chars', 'max_lines', 'max_chars_per_line'} if slot['type'] == 'text' else set())
        require(set(slot) == expected, 'Invalid slot fields')
        require(isinstance(slot['id'], str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', slot['id']), 'Invalid slot ID')
        require(slot['id'] not in ids, 'Duplicate slot ID')
        ids.add(slot['id'])
        require(type(slot['page']) is int and isinstance(slot['object_id'], str), 'Invalid slot address')
        key = (slot['page'], slot['object_id'])
        require(key in objects and key not in used, 'Missing/duplicate mapped object')
        used.add(key)
        for field in ('label', 'guidance', 'placeholder'):
            require(isinstance(slot[field], str) and slot[field].strip(), 'Missing slot ' + field)
        obj = objects[key]
        if slot['type'] == 'text':
            require(obj['kind'] == 'sp' and obj['text'].strip(), 'Text slot needs a native text shape')
            for field in ('max_chars', 'max_lines', 'max_chars_per_line'):
                require(type(slot[field]) is int and 1 <= slot[field] <= 10000, 'Invalid capacity')
            text_capacity(slot, slot['placeholder'])
        else:
            require(obj['kind'] == 'pic', 'Image slot needs a picture object')
    for fixed in profile['fixed_objects']:
        require(isinstance(fixed, dict) and set(fixed) == {'page', 'object_id', 'reason'}, 'Invalid fixed object')
        key = (fixed['page'], fixed['object_id'])
        require(key in objects and key not in used and isinstance(fixed['reason'], str)
                and fixed['reason'].strip(), 'Invalid/duplicate fixed object')
        used.add(key)
    if profile['kind'] == 'direct':
        require(not profile['slots'], 'Direct reuse must not replace content')
    else:
        require(profile['slots'], 'Adaptable template requires slots')
        meaningful = {key for key, obj in objects.items()
                      if obj['text'].strip() or obj['kind'] in {'pic', 'graphicFrame'}}
        require(meaningful <= used, 'Declare slots or fixed reasons for all content objects')
    return profile


def elements(node, ns, name):
    return list(node.getElementsByTagNameNS(structure.NS[ns], name))


def replace_text(shape, value):
    """Replace full text, retaining shape geometry and paragraph/run formatting."""
    body = elements(shape, 'p', 'txBody')
    require(len(body) == 1, 'Missing text body')
    body = body[0]
    paragraphs = [x for x in body.childNodes if x.nodeType == x.ELEMENT_NODE and x.localName == 'p']
    require(paragraphs, 'Missing paragraph style')
    for p in paragraphs:
        body.removeChild(p)
    for i, line in enumerate(value.split('\n')):
        p = paragraphs[min(i, len(paragraphs) - 1)].cloneNode(deep=True)
        runs = elements(p, 'a', 'r')
        run = runs[0].cloneNode(deep=True) if runs else shape.ownerDocument.createElementNS(structure.NS['a'], 'a:r')
        for child in list(run.childNodes):
            if child.nodeType != child.ELEMENT_NODE or child.localName != 'rPr':
                run.removeChild(child)
        for child in list(p.childNodes):
            if child.nodeType != child.ELEMENT_NODE or child.localName not in ('pPr', 'endParaRPr'):
                p.removeChild(child)
        t = shape.ownerDocument.createElementNS(structure.NS['a'], 'a:t')
        t.appendChild(shape.ownerDocument.createTextNode(line))
        run.appendChild(t)
        end = next((x for x in p.childNodes if x.nodeType == x.ELEMENT_NODE and x.localName == 'endParaRPr'), None)
        p.insertBefore(run, end)
        body.appendChild(p)


def picture_bytes(value, size):
    """Fit PNG/JPEG into the original frame, without cropping or stretching."""
    from PIL import Image, ImageDraw, ImageOps
    width, height = size
    canvas = Image.new('RGB', size, '#f0f3f7')
    if value is None:
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((2, 2, width - 3, height - 3), outline='#8795a6', width=3)
        draw.line((width // 4, height // 2, width * 3 // 4, height // 2), fill='#8795a6', width=3)
        draw.line((width // 2, height // 4, width // 2, height * 3 // 4), fill='#8795a6', width=3)
    else:
        with Image.open(value) as img:
            require(img.format in {'PNG', 'JPEG'}, 'Image slot supports PNG/JPEG only')
            require(img.width * img.height <= 40_000_000, 'Image too large')
            img = ImageOps.exif_transpose(img).convert('RGBA')
            img.thumbnail(size, Image.Resampling.LANCZOS)
            canvas.paste(img, ((width-img.width)//2, (height-img.height)//2), img)
    out = io.BytesIO()
    canvas.save(out, format='PNG')
    return out.getvalue()


def transform(source, output, profile, values=None):
    """Apply explicit slot addresses; preserve unrelated native objects."""
    data = structure.extract(source)
    validate_profile(profile, data)
    with zipfile.ZipFile(source) as z:
        package = {n: z.read(n) for n in z.namelist()}
        for page in data['pages']:
            doc = minidom.parseString(package[page['part']])
            shapes = {}
            for tag in ('sp', 'pic'):
                for shape in elements(doc, 'p', tag):
                    ids = elements(shape, 'p', 'cNvPr')
                    if ids:
                        shapes[ids[0].getAttribute('id')] = shape
            relpart = str(Path(page['part']).parent / '_rels' / (Path(page['part']).name + '.rels'))
            rels = minidom.parseString(package[relpart]) if relpart in package else None
            for slot in profile['slots']:
                if slot['page'] != page['number']:
                    continue
                shape = shapes[slot['object_id']]
                if slot['type'] == 'text':
                    replace_text(shape, slot['placeholder'] if values is None else values[slot['id']])
                else:
                    require(rels is not None, 'Picture relationship missing')
                    blips = elements(shape, 'a', 'blip')
                    require(len(blips) == 1, 'Unsupported picture')
                    rid = 'reuse_' + slot['id']
                    name = f'reuse-{page["number"]}-{slot["id"]}.png'
                    media = 'ppt/media/' + name
                    obj = next(o for o in page['objects'] if str(o['id']) == slot['object_id'])
                    box = obj['bbox_emu']
                    aspect = (box[2]-box[0]) / max(1, box[3]-box[1])
                    require(0.05 <= aspect <= 20, 'Unsupported picture aspect ratio')
                    size = (1000, max(50, round(1000/aspect))) if aspect >= 1 else (max(50, round(1000*aspect)), 1000)
                    package[media] = picture_bytes(None if values is None else values[slot['id']], size)
                    for rel in list(rels.documentElement.childNodes):
                        if rel.nodeType == rel.ELEMENT_NODE and rel.getAttribute('Id') == rid:
                            rels.documentElement.removeChild(rel)
                    rel = rels.createElementNS(structure.REL, 'Relationship')
                    for key, val in {'Id': rid, 'Type': structure.NS['r'] + '/image', 'Target': '../media/' + name}.items():
                        rel.setAttribute(key, val)
                    rels.documentElement.appendChild(rel)
                    blips[0].setAttributeNS(structure.NS['r'], 'r:embed', rid)
                    blips[0].removeAttributeNS(structure.NS['r'], 'link') if blips[0].hasAttributeNS(structure.NS['r'], 'link') else None
                    for crop in elements(shape, 'a', 'srcRect'):
                        crop.parentNode.removeChild(crop)
            # Derivative notes describe use, never carry stale talk statements.
            for relation in structure.relationships(z, page['part']).values():
                if relation['kind'] == 'notesSlide':
                    note = minidom.parseString(package[relation['part']])
                    for shape in elements(note, 'p', 'sp'):
                        if elements(shape, 'p', 'txBody'):
                            replace_text(shape, '套用模板：请替换全部指定槽位；检查实际显示。')
                    package[relation['part']] = note.toxml(encoding='utf-8')
            package[page['part']] = doc.toxml(encoding='utf-8')
            if rels is not None:
                package[relpart] = rels.toxml(encoding='utf-8')
        ct = minidom.parseString(package['[Content_Types].xml'])
        if not any(x.getAttribute('Extension') == 'png' for x in ct.getElementsByTagNameNS('*', 'Default')):
            item = ct.createElementNS(ct.documentElement.namespaceURI, 'Default')
            item.setAttribute('Extension', 'png')
            item.setAttribute('ContentType', 'image/png')
            ct.documentElement.appendChild(item)
        package['[Content_Types].xml'] = ct.toxml(encoding='utf-8')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in package.items():
            z.writestr(name, content)


def usage_md(name, profile, receipt):
    lines = ['# ' + name, '', '**类型：' + KINDS[profile['kind']] + '**', '',
             f"来源：`{receipt['source']}`；原第 {receipt['pages']} 页。", '', '## 适用场景', '']
    lines += ['- ' + x for x in profile['use_when']]
    lines += ['', '## 使用条件与限制', ''] + ['- ' + x for x in profile['constraints']]
    if profile['kind'] == 'direct':
        lines += ['', '将 component.pptx 整页复制到目标演示文稿，核对语境、引用和时效性。']
    else:
        lines += ['', '## 填充说明', '',
                  '打开 component.pptx 可手动替换；程序填充使用 values.example.json 的全部字段。',
                  '图片字段填写本仓库 PNG/JPEG 的绝对路径，等比居中留边；文本以换行分段，保留原生文本框。',
                  '下表容量是保守的字符/显式行数预算，不是排版通过保证。', '',
                  '| 字段 | 模板页 / 对象 | 用途 | 容量与规则 |', '|---|---|---|---|']
        for s in profile['slots']:
            capacity = (f"总 {s['max_chars']} 字符 / {s['max_lines']} 行 / 每行 {s['max_chars_per_line']} 字符"
                        if s['type'] == 'text' else 'PNG/JPEG；等比居中，不裁切')
            lines.append(f"| {s['id']} | {s['page']} / {s['object_id']} | {s['label']} | {capacity}；{s['guidance']} |")
        lines += ['', '固定对象：'] + [f"- 第 {o['page']} 页对象 {o['object_id']}：{o['reason']}" for o in profile['fixed_objects']]
    lines += ['', '结构参数见 structure.json；用户最终检查、跨平台字体和 PowerPoint 编辑验收独立进行。',
              '未自动执行 PPT形式检查、PPT证据核查或 PPT引用脱敏；模板通用化不等于全面脱敏。', '']
    return '\n'.join(lines)


def new_output(path):
    raw = Path(path).absolute()
    require(not any(p.is_symlink() for p in [raw, *raw.parents]), 'Symlink output is not allowed')
    path = raw.resolve()
    require(path.is_relative_to((library.REPO/'temp').resolve()) and not path.exists(),
            'Output must be a new directory under repository temp')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, Path(tempfile.mkdtemp(prefix='.reuse-', dir=path.parent))


def validate_layout(component, report_path):
    report_path = safe_input(report_path)
    report = read_json(report_path)
    data = structure.extract(Path(component)/'component.pptx')
    require(report.get('schema') == 'pptx-analysis-v1' and report.get('mode') == 'layout'
            and report.get('status') == 'complete' and report.get('source_sha256') == data['source_sha256'],
            'Layout report does not match this derivative')
    require([r.get('page') for r in report.get('pages', [])] == list(range(1, len(data['pages'])+1)),
            'Layout report must cover every template page exactly once')
    manifest = read_json(report_path.parent/'pptx-manifest.json')
    for page, r in zip(data['pages'], report['pages']):
        image = safe_input(report_path.parent/'pptx-renders'/manifest['pages'][page['number']-1]['image'])
        library.vision.validate_receipt(r, image=image, mode='layout', source_sha256=data['source_sha256'], page=page)
        require(r['result']['verdict'] in {'pass', 'warn'} and not any(x['critical'] for x in r['result']['limitations']),
                'Layout review has unresolved blocking issues')
    return report


def render_artifacts(folder):
    kind, rendered, count = library.qa._prepare_source(folder/'component.pptx', folder)
    require(count == len(structure.extract(folder/'component.pptx')['pages']), 'Rendered page count mismatch')
    for i in range(1, count+1):
        library.qa.render_page(kind, rendered, i, folder/f'preview-{i}.png', dpi=90)


def prepare(component, profile, target, name):
    component = Path(component).absolute()
    safe_input(component/'receipt.json')
    require(component.resolve().is_relative_to((library.REPO/'slide-library/templates').resolve()), 'Use a collected component')
    parent = library.verify_bundle(component)
    source = component/'component.pptx'
    data = structure.extract(source)
    validate_profile(profile, data)
    parent_digest = library.vision.digest(component/'receipt.json')
    target, pending = new_output(target)
    try:
        if profile['kind'] == 'direct':
            shutil.copyfile(source, pending/'component.pptx')
        else:
            transform(source, pending/'component.pptx', profile)
        structure_data = structure.extract(pending/'component.pptx')
        structure_data['source'] = 'component.pptx'
        library.vision.write(pending/'structure.json', structure_data)
        library.vision.write(pending/'reuse.json', profile)
        if profile['kind'] == 'adaptable':
            example = {s['id']: s['placeholder'] if s['type'] == 'text' else '/path/to/' + s['id'] + '.png'
                       for s in profile['slots']}
            library.vision.write(pending/'values.example.json', example)
        (pending/'component.md').write_text(usage_md(name, profile, parent), encoding='utf-8')
        render_artifacts(pending)
        receipt = {k: copy.deepcopy(parent[k]) for k in ('source', 'source_sha256', 'pages', 'limitations')}
        receipt.update(schema=COMPONENT_SCHEMA, name=name, reuse_kind=profile['kind'],
                       profile_sha256=structure.stable_hash(profile), parent_component=str(component.relative_to(library.REPO)),
                       parent_receipt_sha256=parent_digest, layout_analysis='not_executed',
                       files={p.name: library.vision.digest(p) for p in pending.iterdir() if p.is_file()})
        library.vision.write(pending/'receipt.json', receipt)
        require(library.vision.digest(component/'receipt.json') == parent_digest, 'Parent component changed')
        library.verify_bundle(component)
        library.verify_bundle(pending)
        os.rename(pending, target)
        return {'status': 'prepared', 'kind': profile['kind'], 'directory': str(target)}
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise


def fill(component, values, target):
    component = Path(component).absolute()
    safe_input(component/'receipt.json')
    receipt = library.verify_bundle(component)
    template_digest = library.vision.digest(component/'receipt.json')
    require(receipt.get('reuse_kind') == 'adaptable', 'Fill requires an adaptable template')
    profile = validate_profile(read_json(component/'reuse.json'), structure.extract(component/'component.pptx'))
    require(isinstance(values, dict) and set(values) == {s['id'] for s in profile['slots']}, 'Provide every slot, no unknown fields')
    checked, inputs = {}, {}
    for slot in profile['slots']:
        value = values[slot['id']]
        if slot['type'] == 'text':
            text_capacity(slot, value)
            require(value != slot['placeholder'], 'Replace placeholder: ' + slot['id'])
            checked[slot['id']] = value
        else:
            require(isinstance(value, str) and value, 'Image path required')
            image = safe_input(value)
            require(image.is_relative_to(library.REPO) and not image.is_relative_to(library.REPO/'private'),
                    'Use a public workspace image; private needs an isolated workflow')
            checked[slot['id']] = str(image)
            inputs[slot['id']] = {'path': str(image.relative_to(library.REPO)), 'sha256': library.vision.digest(image)}
    target, pending = new_output(target)
    try:
        transform(component/'component.pptx', pending/'component.pptx', profile, checked)
        render_artifacts(pending)
        library.verify_bundle(component)
        require(library.vision.digest(component/'receipt.json') == template_digest, 'Template changed during fill')
        for item in inputs.values():
            require(library.vision.digest(library.REPO/item['path']) == item['sha256'], 'Image changed during fill')
        library.vision.write(pending/'instance.json', {
            'schema': 'slide-reuse-instance-v1', 'template': str(component.relative_to(library.REPO)),
            'template_receipt_sha256': template_digest,
            'values': values, 'image_inputs': inputs,
            'files': {p.name: library.vision.digest(p) for p in pending.iterdir() if p.is_file()},
            'visual_review': 'not_executed', 'user_approval': 'not_requested'})
        os.rename(pending, target)
        return {'status': 'filled', 'directory': str(target), 'pptx': str(target/'component.pptx')}
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise

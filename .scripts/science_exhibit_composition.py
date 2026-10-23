#!/usr/bin/env python3
"""Bounded exhibit composition, immutable image assets and single-source text."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import tempfile
from review_contract import MAX_IMAGE_BYTES, image_info, png_info


def import_asset(core, store, path, source_label, usage):
    core.public_label(source_label)
    core.text(usage, 500)
    path = Path(path)
    path = path if path.is_absolute() else core.REPO / path
    relative = path.relative_to(core.REPO)
    core.require(relative.parts and relative.parts[0] in {'projects', 'temp', 'examples'} and
                 not {'private', '.git', '.env'}.intersection(relative.parts), 'Unsupported asset input path')
    path = core.managed_path(path, relative.parts[0])
    core.require(path.suffix.lower() in {'.png', '.jpg', '.jpeg'} and path.is_file(), 'A managed PNG/JPEG asset is required')
    core.require(path.stat().st_size <= MAX_IMAGE_BYTES, 'PNG exceeds its byte budget')
    data = path.read_bytes()
    width, height, kind = image_info(data, {'PNG', 'JPEG'})
    sha256 = hashlib.sha256(data).hexdigest()
    folder = core.managed_path(store / 'assets', 'projects')
    folder.mkdir(exist_ok=True)
    suffix = '.png' if kind == 'PNG' else '.jpg'
    destination = core.managed_path(folder / f'{sha256}{suffix}', 'projects')
    if destination.exists():
        core.require(core.file_hash(destination) == sha256, 'Frozen asset corrupted')
    else:
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix='.asset-')
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            descriptor = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {'id': 'asset-' + sha256[:16], 'path': str(destination.relative_to(core.REPO)),
            'sha256': sha256, 'width': width, 'height': height,
            'source_label': source_label, 'usage': usage}


def text_map(content, brief):
    return {**{item['id']: item['text'] for item in content['texts']},
            'source-label': '研究来源：' + brief['source_label'],
            'draft-label': '待审草稿；科学表达、视觉效果与公开展示授权仍待确认。'}


def resolved_objects(content, brief):
    values = text_map(content, brief)
    objects = []
    for item in content['composition']['objects']:
        obj = {key: deepcopy(value) for key, value in item.items() if key not in {'id', 'claim_ids'}}
        if obj['type'] == 'text':
            obj['text'] = values[obj.pop('text_ref')]
        objects.append(obj)
    return objects


def asset_paths(core, content, store=None):
    result = {}
    total_bytes = 0
    for asset in content['assets']:
        core.fields(asset, ('id', 'path', 'sha256', 'width', 'height', 'source_label', 'usage'))
        core.require(isinstance(asset['id'], str) and core.IDENTIFIER.fullmatch(asset['id']) and asset['id'] not in result,
                     'Unique asset ID required')
        core.require(isinstance(asset['sha256'], str) and re.fullmatch('[a-f0-9]{64}', asset['sha256']), 'Invalid asset hash')
        path = core.managed_path(asset['path'], 'projects')
        relative = path.relative_to(core.REPO / 'projects').parts
        core.require(len(relative) == 5 and relative[1] == 'exhibits' and relative[3] == 'assets' and
                     path.name in {asset['sha256'] + '.png', asset['sha256'] + '.jpg'}, 'Assets must use frozen exhibit storage')
        if store is not None:
            core.require(path.parent == core.store_path(store) / 'assets', 'Asset belongs to another exhibit')
        core.require(path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES, 'Missing or oversized frozen asset')
        total_bytes += path.stat().st_size
        core.require(total_bytes <= 64 * 1024 * 1024, 'Exhibit asset budget exceeded')
        core.require(core.file_hash(path) == asset['sha256'], 'Frozen asset changed')
        width, height, kind = image_info(path.read_bytes(), {'PNG', 'JPEG'})
        core.require(path.suffix == ('.png' if kind == 'PNG' else '.jpg'), 'Asset format differs from its frozen suffix')
        core.require(type(asset['width']) is int and type(asset['height']) is int and
                     (width, height) == (asset['width'], asset['height']), 'Asset dimensions differ')
        core.public_label(asset['source_label'])
        core.text(asset['usage'], 500)
        result[asset['id']] = path
    return result


def validate(core, content, state):
    import presentation_artwork as artwork
    core.fields(content, ('schema', 'claims', 'texts', 'assets', 'composition'))
    core.require(content['schema'] == 'science-exhibit-content-v2', 'Unsupported composition content')
    claims = core.validate_claims(content['claims'], state)
    core.require(isinstance(content['texts'], list) and 3 <= len(content['texts']) <= 120, 'Bounded visible texts required')
    values, roles, used = {}, set(), set()
    for item in content['texts']:
        core.fields(item, ('id', 'text', 'claim_ids', 'roles'))
        identifier = item['id']
        core.require(isinstance(identifier, str) and core.IDENTIFIER.fullmatch(identifier) and
                     identifier not in values and identifier not in {'source-label', 'draft-label'}, 'Unique text ID required')
        core.text(item['text'], 300)
        core.require(isinstance(item['roles'], list) and len(item['roles']) == len(set(item['roles'])) and
                     set(item['roles']) <= set(core.PANELS), 'Unknown semantic role')
        references = item['claim_ids']
        core.require(isinstance(references, list) and len(references) == len(set(references)) and
                     all(isinstance(key, str) and key in claims for key in references), 'Unknown text claim')
        if item['roles']:
            core.require(references, 'Scientific roles require evidence')
        values[identifier] = item['text']
        roles.update(item['roles'])
        used.update(references)
    core.require({'title', 'hook', 'takeaway'} <= set(values), 'Title, hook and takeaway text IDs required')
    core.text(values['title'], 80)
    core.require(roles == set(core.PANELS), 'Question, approach, finding and boundary must be represented')
    core.require(isinstance(content['assets'], list) and len(content['assets']) <= 24, 'Bounded asset list required')
    paths = asset_paths(core, content)
    composition = content['composition']
    core.fields(composition, ('schema', 'canvas', 'background', 'objects'))
    core.require(composition['schema'] == 'science-exhibit-composition-v1', 'Unsupported composition schema')
    brief = {'schema': 'presentation-artwork-brief-v1', 'canvas': composition['canvas'], 'sensitivity': 'local_only',
             'brief': values['title'], 'sources': [state['source']['path']]}
    artwork.validate_brief(brief)
    artwork.color(composition['background'])
    core.require(isinstance(composition['objects'], list) and 1 <= len(composition['objects']) <= 240,
                 'Bounded composition objects required')
    seen, displayed, images = set(), [], set()
    native = []
    for original, obj in zip(composition['objects'], resolved_objects(content, state['brief'])):
        identifier = original['id']
        core.require(isinstance(identifier, str) and core.IDENTIFIER.fullmatch(identifier) and identifier not in seen,
                     'Unique object ID required')
        seen.add(identifier)
        references = original['claim_ids']
        core.require(isinstance(references, list) and len(references) == len(set(references)) and
                     all(isinstance(key, str) and key in claims for key in references), 'Unknown object claim')
        used.update(references)
        if obj['type'] == 'image':
            core.fields(obj, ('type', 'x', 'y', 'w', 'h', 'asset_id') + (('fit',) if 'fit' in obj else ()))
            core.require(obj.get('fit', 'contain') in {'contain', 'cover'}, 'Unsupported image fitting mode')
            core.require(obj['asset_id'] in paths, 'Unknown image asset')
            for coordinate, limit in (('x', composition['canvas'][0]), ('y', composition['canvas'][1]),
                                      ('w', composition['canvas'][0]), ('h', composition['canvas'][1])):
                artwork.number(obj[coordinate], 0 if coordinate in {'x', 'y'} else 1, limit)
            core.require(obj['x'] + obj['w'] <= composition['canvas'][0] and
                         obj['y'] + obj['h'] <= composition['canvas'][1], 'Image outside canvas')
            asset = next(item for item in content['assets'] if item['id'] == obj['asset_id'])
            core.require(obj.get('fit') == 'cover' or abs(obj['w'] / obj['h'] / (asset['width'] / asset['height']) - 1) <= 0.005,
                         'Images must retain their aspect ratio; use explicit cover cropping or prepare a new asset')
            images.add(obj['asset_id'])
        else:
            native.append(obj)
            if obj['type'] == 'text':
                displayed.append(original['text_ref'])
    core.require(set(displayed) == set(text_map(content, state['brief'])) and len(displayed) == len(set(displayed)),
                 'Every visible text including source/draft labels must be rendered exactly once')
    core.require(images == set(paths), 'Every frozen asset must be used')
    core.require(used == set(claims), 'Every claim must be used')
    for start in range(0, len(native), 100):
        artwork.validate_artwork({'schema': 'presentation-artwork-v1', 'canvas': composition['canvas'],
                                  'background': composition['background'], 'rationale': 'Bounded exhibit composition',
                                  'objects': native[start:start + 100]}, brief)
    all_text = '\n'.join(text_map(content, state['brief']).values())
    for claim in content['claims']:
        for qualifier in claim['qualifiers']:
            core.require(qualifier in all_text, 'Declared scientific qualifier must remain visible')
    core.validate_display_acronyms(display_texts(content, state['brief']))
    return content


def display_texts(content, brief):
    values = text_map(content, brief)
    return [(item['text_ref'], values[item['text_ref']])
            for item in sorted(content['composition']['objects'], key=lambda obj: (obj.get('y', 0), obj.get('x', 0)))
            if item['type'] == 'text']


def compile_layout(content, brief, font, canvas, wrap_text):
    from science_exhibit_render import POINTS_PER_MM
    logical = content['composition']['canvas']
    scale = min(actual / nominal for actual, nominal in zip(canvas, logical))
    offset = [(actual - nominal * scale) / 2 for actual, nominal in zip(canvas, logical)]
    objects = []
    for original, obj in zip(content['composition']['objects'], resolved_objects(content, brief)):
        result = {'id': original['id'], 'claim_ids': original['claim_ids'], 'panel_id': None}
        if obj['type'] == 'line':
            result.update(type='line', start_mm=[obj['x1'] * scale + offset[0], obj['y1'] * scale + offset[1]],
                          end_mm=[obj['x2'] * scale + offset[0], obj['y2'] * scale + offset[1]],
                          stroke=obj['stroke'].lstrip('#'), stroke_pt=obj['stroke_width'] * scale * POINTS_PER_MM)
        else:
            box = [obj['x'] * scale + offset[0], obj['y'] * scale + offset[1], obj['w'] * scale, obj['h'] * scale]
            result['bbox_mm'] = box
            if obj['type'] == 'image':
                result.update(type='image', asset_id=obj['asset_id'])
                if obj.get('fit') == 'cover':
                    asset = next(item for item in content['assets'] if item['id'] == obj['asset_id'])
                    ratio = asset['width'] / asset['height']
                    box_ratio = obj['w'] / obj['h']
                    crop = (1 - box_ratio / ratio) / 2 if ratio > box_ratio else (1 - ratio / box_ratio) / 2
                    result['crop'] = [crop, 0, crop, 0] if ratio > box_ratio else [0, crop, 0, crop]
            elif obj['type'] == 'text':
                size = obj['size'] * scale * POINTS_PER_MM
                result.update(type='text', text=wrap_text(obj['text'], box[2], box[3], size, font),
                              font_pt=size, color=obj['color'].lstrip('#'), bold=obj['bold'], align=obj['align'])
            else:
                result.update(type='shape', shape_kind=obj['type'], color=obj['fill'].lstrip('#'),
                              stroke=obj['stroke'].lstrip('#'), stroke_pt=obj['stroke_width'] * scale * POINTS_PER_MM)
                if obj['type'] == 'rect':
                    result['radius_mm'] = obj['radius'] * scale
        objects.append(result)
    return {'schema': 'science-exhibit-layout-v2', 'canvas_mm': list(canvas), 'visual_kind': 'composition',
            'background': content['composition']['background'].lstrip('#'), 'objects': objects}

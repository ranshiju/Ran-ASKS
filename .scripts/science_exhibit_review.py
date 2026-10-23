#!/usr/bin/env python3
"""Host-owned image-review handoffs and deterministic exhibit comparisons."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import shutil
import tempfile
import uuid


def describe(core, store, state):
    preview = state['workflow']['preview']
    decisions = [item for item in state['workflow']['feedback'] if item.get('review_binding')]
    return {'available': preview is not None, 'backend': 'agent', 'optional_advisory': True,
            'method': 'operations/SCIENCE_EXHIBIT.md#全流程接续与独立试读',
            'prepare_action': 'prepare-review' if preview else None,
            'required_parameters': ['host-capabilities', 'sensitivity', 'allow-host-processing'],
            'current_preview_decisions': [item for item in decisions if preview and
                item['review_binding']['manifest_sha256'] == preview['manifest_sha256']],
            'historical_decision_count': len(decisions),
            'notice': 'Review is advisory; no required criticism count, automatic retry or acceptance.'}


def prepare_review(core, store, state, args):
    import review
    import science_exhibit_workflow as work
    core.require(review.REPO.resolve() == core.REPO.resolve(), 'Review and exhibit must use the same repository')
    preview = work.validate_preview(core, store, state, check_runtime=True)
    folder = core.managed_path(store / 'review-briefs', 'projects')
    folder.mkdir(exist_ok=True)
    requirements = state['workflow']['requirements']
    brief = {'audience': requirements['audience']['value'], 'use_context': requirements['setting']['value'],
             'goal': '先用日常语言复述实际展板的研究主线和直觉图景，再指出帮助理解的表达、具体断点与可选建议。',
             'constraints': ['不要求掌握技术细节；不凭专业背景替展板补全；阅读预算是情境而非实际计时。']}
    if args.brief:
        brief = review.read_json(args.brief)
        core.require(brief.get('audience') == requirements['audience']['value'], 'Review audience differs from the exhibit brief')
    path = folder / f'{core.digest(brief)}.json'
    if path.exists():
        core.require(core.read_json(path) == brief, 'Frozen review brief changed')
    else:
        core.atomic_json(path, brief)
    output = core.managed_path(preview['output_dir'], 'projects')
    return review.prepare(output / 'preview.png', path, args.reviewer, args.host_capabilities,
        sensitivity=args.sensitivity, allow_host_processing=args.allow_host_processing,
        accept_unenforced_tools=args.accept_unenforced_tools, require_enforced_tools=args.require_enforced_tools,
        workspace=store.parent.parent, artifact_manifest=output / 'manifest.json',
        image_regions=getattr(args, 'image_regions', None),
        detail_source=output / 'board.pdf' if getattr(args, 'image_regions', None) else None)


def validate_decisions(core, decisions):
    core.require(isinstance(decisions, list) and len(decisions) <= 100, 'Bounded review decisions required')
    seen = set()
    for item in decisions:
        core.fields(item, ('feedback_id', 'action', 'reason'))
        core.require(isinstance(item['feedback_id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['feedback_id']) and
                     item['feedback_id'] not in seen, 'Unique reviewed feedback ID required')
        seen.add(item['feedback_id'])
        core.require(item['action'] in {'adopt', 'partly-adopt', 'reject', 'defer'}, 'Unknown review decision')
        core.text(item['reason'], 1500)


def record_review(core, store, state, args):
    import review
    import science_exhibit_workflow as work
    core.require(review.REPO.resolve() == core.REPO.resolve(), 'Review and exhibit must use the same repository')
    core.text(args.instruction, 2000)
    run, request, packet, extension, binding_hash = review.verified(args.run)
    result = review.read_result(run, packet, extension, binding_hash)
    core.require(result['execution']['kind'] == 'host-agent', 'Offline fixtures cannot be recorded as actual exhibit review')
    preview = work.validate_preview(core, store, state)
    artifact = request.get('artifact_manifest', {})
    core.require(artifact.get('sha256') == preview['manifest_sha256'] and
                 artifact.get('source') == str((core.REPO / preview['output_dir'] / 'manifest.json').relative_to(core.REPO)),
                 'Review belongs to a different preview; do not apply it silently to the current version')
    core.require(review.applicability(request) == 'current', 'Review input or brief changed')
    core.require(packet['material'].get('kind') == 'image' and not result['diagnostics'], 'A completed image review is required')
    decisions = core.read_json(core.managed_path(args.decisions, 'temp'))
    validate_decisions(core, decisions)
    identifiers = {item['id'] for item in result['content']['feedback']}
    core.require(all(item['feedback_id'] in identifiers for item in decisions), 'Decision references unknown feedback')
    binding = {'run': review.relative(run), 'result_sha256': review.digest(review.encode(result)),
               'manifest_sha256': preview['manifest_sha256'], 'content_sha256': preview['content_sha256']}
    previous = [item for item in state['workflow']['feedback'] if item.get('review_binding') == binding]
    if previous:
        if previous[-1]['decisions'] == decisions:
            return {'status': 'review_recorded', 'revision': state['revision'], 'already_recorded': True}
    state = deepcopy(state)
    state['revision'] += 1
    state['last_request'] = None
    state['workflow']['feedback'].append({'id': uuid.uuid4().hex, 'scope': 'visual',
        'text': '独立图片审阅与主 Agent 取舍；采纳是修改意向，不代表已实施、理解改善或作品批准。',
        'instruction': args.instruction, 'revision': state['revision'], 'review_binding': binding, 'decisions': decisions})
    work.validate_state_shape(core, state)
    core.save_revision(store, state)
    return {'status': 'review_recorded', 'revision': state['revision'], 'already_recorded': False,
            'review_binding': binding, 'decisions': decisions, 'preview_acceptance': 'unchanged'}


def historical_artifact(core, store, value):
    import science_exhibit_workflow as work
    directory = core.managed_path(value, 'projects')
    core.require(directory.parent == store / 'outputs' and re.fullmatch(r'r[0-9]{6}-[a-f0-9]{32}', directory.name),
                 'Comparison inputs must be actual outputs of this exhibit')
    manifest_path = core.managed_path(directory / 'manifest.json', 'projects')
    manifest = core.read_json(manifest_path)
    core.require(manifest.get('schema') == 'science-exhibit-delivery-v1' and
                 isinstance(manifest.get('files'), dict) and set(manifest['files']) == set(work.ARTIFACTS), 'Invalid artifact set')
    core.require({path.name for path in directory.iterdir()} == set(work.ARTIFACTS) | {'manifest.json'}, 'Invalid artifact directory')
    for name, sha256 in manifest['files'].items():
        core.require(core.file_hash(core.managed_path(directory / name, 'projects')) == sha256, 'Comparison artifact changed')
    core.require(manifest.get('checks', {}).get('mechanical_status') == 'passed', 'Comparison requires completed mechanical output')
    return directory, manifest, core.file_hash(manifest_path)


def compare(core, store, state, before, after):
    from PIL import Image, ImageDraw, ImageChops
    from review_contract import safe_text
    from science_exhibit_composition import png_info
    previous, previous_manifest, previous_hash = historical_artifact(core, store, before)
    current, current_manifest, current_hash = historical_artifact(core, store, after)
    core.require(previous_manifest['source_sha256'] == current_manifest['source_sha256'], 'Compare versions of the same source paper')
    layouts = [core.read_json(directory / 'layout.json') for directory in (previous, current)]
    values = [{item['id']: item for item in layout['objects']} for layout in layouts]
    changed = sorted(key for key in set(values[0]) | set(values[1]) if values[0].get(key) != values[1].get(key))
    text_changes = [{'id': key, 'before': values[0].get(key, {}).get('text'), 'after': values[1].get(key, {}).get('text')}
                    for key in changed if values[0].get(key, {}).get('type') == 'text' or values[1].get(key, {}).get('type') == 'text']
    receipt = {'schema': 'science-exhibit-comparison-v1', 'before': {'output_dir': str(previous.relative_to(core.REPO)), 'manifest_sha256': previous_hash},
               'after': {'output_dir': str(current.relative_to(core.REPO)), 'manifest_sha256': current_hash},
               'changed_object_ids': changed, 'text_changes': text_changes,
               'assets_before': previous_manifest['checks'].get('image_assets', {}),
               'assets_after': current_manifest['checks'].get('image_assets', {}),
               'decisions': [item for item in state['workflow']['feedback'] if item.get('review_binding', {}).get('manifest_sha256') == previous_hash],
               'notice': 'Mechanical comparison, not proof of improved comprehension. Adoption is not proof of implementation.'}
    identifier = core.digest(receipt)
    root = core.managed_path(store / 'comparisons', 'projects')
    root.mkdir(exist_ok=True)
    destination = core.managed_path(root / identifier, 'projects')
    if not destination.exists():
        temporary = Path(tempfile.mkdtemp(dir=root, prefix='.compare-'))
        try:
            images = []
            for directory in (previous, current):
                path = directory / 'preview.png'
                png_info(path.read_bytes())
                with Image.open(path) as source:
                    image = source.convert('RGB')
                target_width = min(image.width, 1000)
                images.append(image.resize((target_width, round(image.height * target_width / image.width))))
            combined = Image.new('RGB', (sum(image.width for image in images) + 20, max(image.height for image in images) + 30), 'white')
            draw = ImageDraw.Draw(combined)
            left = 0
            for label, image in zip(('Before', 'After'), images):
                draw.text((left + 10, 7), label, fill='black')
                combined.paste(image, (left, 30))
                left += image.width + 20
            combined.save(temporary / 'comparison.png')
            crop = ImageChops.difference(images[0], images[1]).getbbox() if images[0].size == images[1].size else None
            if crop:
                left, top, right, bottom = crop
                crop = (max(0, left - 20), max(0, top - 20), min(images[0].width, right + 20), min(images[0].height, bottom + 20))
                sections = [image.crop(crop) for image in images]
                details = Image.new('RGB', (sections[0].width * 2 + 20, sections[0].height + 30), 'white')
                detail_draw = ImageDraw.Draw(details)
                for index, image in enumerate(sections):
                    origin = index * (image.width + 20)
                    detail_draw.text((origin + 10, 7), ('Before', 'After')[index], fill='black')
                    details.paste(image, (origin, 30))
                details.save(temporary / 'details.png')
            core.atomic_json(temporary / 'comparison.json', receipt)
            lines = ['# 展板版本对比', '', '左旧右新；差异不是理解改善的认证。', '',
                     f'变更对象：{len(changed)}；文字变化：{len(text_changes)}。', '', '## 文字变化', '']
            for item in text_changes:
                lines.extend([f'### {safe_text(item["id"])}', '', f'修改前：{safe_text(item["before"])}', '', f'修改后：{safe_text(item["after"])}', ''])
            (temporary / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
            core.atomic_json(temporary / 'generated.json', {'schema': 'science-exhibit-comparison-files-v1',
                'input_binding_sha256': identifier, 'detail_region_on_resized_previews': crop,
                'files': {path.name: core.file_hash(path) for path in temporary.iterdir() if path.is_file()}})
            temporary.rename(destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    else:
        core.require(core.read_json(destination / 'comparison.json') == receipt, 'Comparison binding changed')
        generated = core.read_json(core.managed_path(destination / 'generated.json', 'projects'))
        core.require(generated.get('input_binding_sha256') == identifier and isinstance(generated.get('files'), dict), 'Comparison files lost their binding')
        core.require(set(generated['files']) in ({'comparison.png', 'comparison.json', 'report.md'},
                     {'comparison.png', 'comparison.json', 'report.md', 'details.png'}), 'Unexpected comparison file set')
        for name, expected in generated['files'].items():
            core.require(core.file_hash(core.managed_path(destination / name, 'projects')) == expected, 'Derived comparison changed; rebuild from the original outputs')
    return {'status': 'compared', 'output_dir': str(destination.relative_to(core.REPO)),
            'comparison': str((destination / 'comparison.png').relative_to(core.REPO)),
            'report': str((destination / 'report.md').relative_to(core.REPO)), 'changed_object_ids': changed,
            'details': str((destination / 'details.png').relative_to(core.REPO)) if (destination / 'details.png').exists() else None,
            'semantic_effect': 'not_certified'}


def add_commands(sub):
    for name in ('prepare-review', 'record-review', 'compare'):
        command = sub.add_parser(name)
        command.add_argument('--store', required=True)
        command.add_argument('--expected-revision', type=int, required=True)
        if name == 'prepare-review':
            command.add_argument('--host-capabilities', required=True)
            command.add_argument('--reviewer', default='reader-comprehension')
            command.add_argument('--brief')
            command.add_argument('--sensitivity', choices=('public', 'internal'), required=True)
            command.add_argument('--allow-host-processing', action='store_true')
            command.add_argument('--accept-unenforced-tools', action='store_true')
            command.add_argument('--require-enforced-tools', action='store_true')
            command.add_argument('--image-regions', help='explicit image-view-spec-v1; render detail regions from the bound PDF locally')
        elif name == 'record-review':
            command.add_argument('--run', required=True)
            command.add_argument('--decisions', required=True)
            command.add_argument('--instruction', required=True)
        else:
            command.add_argument('--before', required=True)
            command.add_argument('--after', required=True)


def act(core, args):
    with core.locked(args.store) as store:
        state = core.load_state(store)
        core.require(state['revision'] == args.expected_revision, 'Stale expected revision')
        if args.action == 'compare':
            return compare(core, store, state, args.before, args.after)
        core.verify_source(state)
        core.require(state['schema'] == 'science-exhibit-v2', 'Collaborative exhibit required')
        if args.action == 'prepare-review':
            return prepare_review(core, store, state, args)
        return record_review(core, store, state, args)

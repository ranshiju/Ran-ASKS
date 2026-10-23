#!/usr/bin/env python3
"""Isolated exhibit contract tests and opt-in actual native rendering."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import science_exhibit as exhibit
import science_exhibit_render as renderer


def test_typographic_units():
    assert renderer.typographic_units(list('甲乙丙丁。')) == ['甲', '乙', '丙', '丁。']
    assert renderer.typographic_units(list('甲（乙）丙')) == ['甲', '（乙）', '丙']
    assert renderer.typographic_units(['AI', '，', '答', '案', '。']) == ['AI，', '答', '案。']
    sample = '原生（可编辑）展板：证据、条件与边界。'
    assert ''.join(renderer.typographic_units(list(sample))) == sample
    measured = SimpleNamespace(getlength=len)
    image_font = SimpleNamespace(truetype=lambda *args, **kwargs: measured)
    width_mm = 4.01 / (renderer.POINTS_PER_MM * 4 * 0.97)
    with patch.dict('sys.modules', {'PIL': SimpleNamespace(ImageFont=image_font)}):
        font = {'path': 'TEST-ONLY', 'index': 0}
        assert renderer.wrap_text('甲乙丙丁。', width_mm, 100, 1, font) == '甲乙丙\n丁。'
        assert renderer.wrap_text('甲乙丙（丁）', width_mm, 100, 1, font) == '甲乙丙\n（丁）'


def test_rendered_lines_match_objects_not_overlapping_empty_boxes():
    import fitz
    factor = renderer.POINTS_PER_MM
    objects = [{'id': 'heading', 'type': 'text', 'bbox_mm': [10, 10, 100, 20], 'text': '主标题'},
               {'id': 'caption', 'type': 'text', 'bbox_mm': [80, 20, 30, 10], 'text': '图注'}]
    lines = [(fitz.Rect(10, 10, 40, 18) * factor, '主标题'),
             (fitz.Rect(80, 20, 100, 28) * factor, '图注')]
    renderer.validate_rendered_lines(objects, lines)
    fails(lambda: renderer.validate_rendered_lines(objects, lines + [lines[0]]))
    fails(lambda: renderer.validate_rendered_lines(objects, [(lines[0][0], '主'), lines[1]]))
    fails(lambda: renderer.validate_rendered_lines(objects, [(fitz.Rect(5, 10, 40, 18) * factor, '主标题'), lines[1]]))


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        source = root / 'academic/raw/references/fixture/paper.md'
        source.parent.mkdir(parents=True)
        source.write_text('测试模型仅用于工程检查，不对应真实科学研究。\n第二行：这是用于验证文本引用的合成材料。\n', encoding='utf-8')
        (root / 'projects/fixture').mkdir(parents=True)
        with patch.object(exhibit, 'REPO', root):
            store = root / 'projects/fixture/exhibits/board'
            exhibit.init_store(store, str(source.relative_to(root)), '仅供工程测试的合成材料，不对应真实论文', legacy=True)
            yield root, source, store


def content(source, root):
    return {
        'schema': 'science-exhibit-content-v1', 'title': '科学展板工程样板',
        'hook': '怎样让科学解释更容易理解？',
        'takeaway': {'text': '本样板仅用于工程检查，不对应真实研究。', 'claim_ids': ['claim-1']},
        'claims': [{
            'id': 'claim-1', 'kind': 'method', 'locator': str(source.relative_to(root)) + '#L1',
            'quote': '测试模型仅用于工程检查，不对应真实科学研究。', 'qualifiers': ['仅用于工程检查'],
        }],
        'panels': [
            {'id': 'question', 'headline': '为什么需要解释？', 'body': '这是合成材料，用于测试原生文字和来源定位。', 'claim_ids': ['claim-1']},
            {'id': 'approach', 'headline': '怎样制作展板？', 'body': '先明确证据，再组织公众故事，最后编译原生对象。', 'claim_ids': ['claim-1']},
            {'id': 'finding', 'headline': '检查什么？', 'body': '检查真实尺寸、中文、可编辑对象和版本一致性。', 'claim_ids': ['claim-1']},
            {'id': 'boundary', 'headline': '不意味着什么？', 'body': '仅用于工程检查。文件生成成功不代表科学结论已获批准。', 'claim_ids': ['claim-1']},
        ],
        'diagram': {'headline': '从证据到解释', 'steps': [
            {'id': 'step-source', 'text': '定位证据', 'claim_ids': ['claim-1']},
            {'id': 'step-story', 'text': '组织故事', 'claim_ids': ['claim-1']},
            {'id': 'step-board', 'text': '制作展板', 'claim_ids': ['claim-1']},
        ]},
    }


def concept_content(source, root):
    value = content(source, root)
    value['diagram'] = {'kind': 'concept', 'headline': '目标与可达范围', 'claim_ids': ['claim-1'],
                        'artwork': {'schema': 'presentation-artwork-v1', 'canvas': [1280, 720],
                                    'background': '#F7FAF8', 'rationale': 'TEST-ONLY 关系图，不对应真实物理空间',
                                    'objects': [
        {'type': 'rect', 'x': 40, 'y': 40, 'w': 1200, 'h': 620, 'fill': '#FFFFFF', 'stroke': '#236F78', 'stroke_width': 3, 'radius': 24},
        {'type': 'ellipse', 'x': 100, 'y': 210, 'w': 600, 'h': 360, 'fill': '#E4F0EE', 'stroke': '#236F78', 'stroke_width': 3},
        {'type': 'ellipse', 'x': 910, 'y': 350, 'w': 32, 'h': 32, 'fill': '#B75136', 'stroke': 'none', 'stroke_width': 0},
        {'type': 'line', 'x1': 925, 'y1': 365, 'x2': 690, 'y2': 390, 'stroke': '#B75136', 'stroke_width': 4},
        {'type': 'text', 'x': 150, 'y': 90, 'w': 950, 'h': 70, 'text': '仅供工程测试的图形主视觉检查', 'size': 40, 'color': '#18323B', 'align': 'center', 'bold': True},
        {'type': 'text', 'x': 170, 'y': 335, 'w': 450, 'h': 90, 'text': '可达范围\n仅用于工程检查', 'size': 32, 'color': '#18323B', 'align': 'center', 'bold': False},
        {'type': 'text', 'x': 830, 'y': 250, 'w': 350, 'h': 60, 'text': '想达到的目标', 'size': 32, 'color': '#B75136', 'align': 'right', 'bold': True},
    ]}}
    return value


def fails(operation):
    try:
        operation()
    except (ValueError, OSError, TypeError):
        return
    raise AssertionError('Invalid operation was accepted')


def test_visible_acronym_definitions():
    for values in (
        ['矩阵乘积纠缠（MPE）', '使用 MPE 比较'],
        ['MPE (matrix product entanglement)', 'MPE 刻画'],
        ['矩阵乘积态(MPS)与矩阵乘积纠缠（MPE）', 'MPS 与 MPE'],
        ['人工智能（ＡＩ）', 'AI 应用'],
        ['人工智能（A.I.）', 'AI 应用'],
        ['二氧化碳（CO2）', 'CO2'],
        ['A0 画布和 χ=2，作者 Shuo Qi'],
    ):
        exhibit.validate_display_acronyms([(str(index), value) for index, value in enumerate(values)])
    for values in (
        ['MPE 尚未定义'], ['MPE', '矩阵乘积纠缠（MPE）'], ['MPE（MPS）'],
        ['MPE（2026）'], ['（MPE）'], ['未定义的 A.I.'],
    ):
        fails(lambda: exhibit.validate_display_acronyms([(str(index), value) for index, value in enumerate(values)]))
    with fixture() as (root, source, store):
        state = exhibit.load_state(store)
        valid = content(source, root)
        for field in ('title', 'hook'):
            invalid = deepcopy(valid)
            invalid[field] = 'MPE 尚未定义'
            fails(lambda: exhibit.validate_content(invalid, state))
        invalid = deepcopy(valid)
        invalid['panels'][0]['headline'] = 'MPS 是什么？'
        fails(lambda: exhibit.validate_content(invalid, state))
        invalid = deepcopy(valid)
        invalid['diagram']['steps'][0]['text'] = 'MPE'
        fails(lambda: exhibit.validate_content(invalid, state))
        state['brief']['source_label'] = 'PRL · DOI: 10.0000/test'
        try:
            exhibit.validate_content(valid, state)
        except ValueError as error:
            assert 'brief.source_label: PRL' in str(error) and 'brief.source_label: DOI' in str(error)
        else:
            raise AssertionError('Undefined source abbreviations accepted')
        state['brief']['source_label'] = 'Physical Review Letters · 论文标识：10.0000/test'
        exhibit.validate_content(valid, state)
        concept = concept_content(source, root)
        concept['diagram']['artwork']['rationale'] = 'UNDEFINED 仅为不展示的创作说明'
        exhibit.validate_content(concept, state)
        labels = concept['diagram']['artwork']['objects']
        labels[4]['text'] = 'MPE 尚未定义'
        fails(lambda: exhibit.validate_content(concept, state))
        fails(lambda: renderer.compile_layout(concept, state['brief'], {}))
        labels[4]['text'] = '矩阵乘积纠缠（MPE）'
        labels[5]['text'] = 'MPE 仅用于工程检查'
        labels[4], labels[5] = labels[5], labels[4]
        exhibit.validate_content(concept, state)
        task = exhibit.prepare(store, 0)
        invalid = deepcopy(valid)
        invalid['hook'] = 'MPE 尚未定义'
        request = candidate(task, invalid, root)
        fails(lambda: exhibit.check_or_commit(request, commit=True))
        assert exhibit.load_state(store)['revision'] == 0
        committed(root, source, store)
        state = exhibit.load_state(store)
        state['content']['hook'] = 'MPE 尚未定义'
        exhibit.save_revision(store, state)
        fails(lambda: exhibit.build(store, 1))
        assert not (store / 'outputs').exists()
    original_hash = renderer.file_hash
    baseline = renderer.compiler_hash()
    with patch.object(renderer, 'file_hash', side_effect=lambda path: 'changed' if Path(path) == Path(exhibit.__file__) else original_hash(path)):
        assert renderer.compiler_hash() != baseline


def candidate(task, value, root):
    exhibit.atomic_json(root / task['agent_task']['outputs'][0]['path'], value)
    return root / task['request_path']


def committed(root, source, store):
    task = exhibit.prepare(store, 0)
    request = candidate(task, content(source, root), root)
    assert exhibit.check_or_commit(request)['status'] == 'ready_to_commit'
    assert exhibit.load_state(store)['revision'] == 0
    assert exhibit.check_or_commit(request, commit=True)['revision'] == 1
    return task, request


def test_creation_validation_and_idempotency():
    with fixture() as (root, source, store):
        task, request = committed(root, source, store)
        assert task['agent_task']['schema'] == 'agent-task-v1'
        assert task['agent_task']['context']['source_data_trust'] == 'untrusted'
        assert task['agent_task']['context']['authoring_method'] == 'operations/SCIENCE_EXHIBIT.md#公众叙事制作方法'
        assert exhibit.check_or_commit(request, commit=True)['already_committed'] is True
        assert exhibit.load_state(store)['revision'] == 1
        changed = content(source, root)
        changed['title'] = '修改已提交候选'
        candidate(task, changed, root)
        fails(lambda: exhibit.check_or_commit(request, commit=True))
        assert exhibit.status(store)['semantic_review'] == 'pending'
        assert exhibit.status(store)['public_release'] == 'not_approved'


def test_content_rejections_and_source_drift():
    with fixture() as (root, source, store):
        state = exhibit.load_state(store)
        valid = content(source, root)
        for update in (
            lambda item: item['claims'][0].update(quote='不存在的引文'),
            lambda item: item['claims'][0].update(locator=str(source.relative_to(root)) + '#L99'),
            lambda item: item['claims'][0].update(locator='private/raw/secret.md#L1'),
            lambda item: item['claims'][0].update(qualifiers=['未显示的限定语']),
            lambda item: item['takeaway'].update(claim_ids=['unknown']),
            lambda item: item['panels'].reverse(),
            lambda item: item['diagram']['steps'][1].update(id='step-source'),
            lambda item: item.update(extra='untrusted instruction'),
        ):
            invalid = deepcopy(valid)
            update(invalid)
            fails(lambda: exhibit.validate_content(invalid, state))
        source.write_text(source.read_text(encoding='utf-8') + '来源变更。\n', encoding='utf-8')
        fails(lambda: exhibit.status(store))
        fails(lambda: exhibit.prepare(store, 0))
        fails(lambda: exhibit.validate_content(valid, state))


def test_panel_edits_stale_requests_and_audience_change():
    with fixture() as (root, source, store):
        committed(root, source, store)
        panel_task = exhibit.prepare(store, 1, panel='finding')
        competing = exhibit.prepare(store, 1)
        previous = exhibit.load_state(store)['content']
        changed = deepcopy(previous)
        changed['panels'][2]['body'] = '本地机械检查不能代替普通读者理解测试。'
        request = candidate(panel_task, changed, root)
        assert exhibit.check_or_commit(request)['status'] == 'ready_to_commit'
        unrelated = deepcopy(changed)
        unrelated['title'] = '无关标题变化'
        candidate(panel_task, unrelated, root)
        fails(lambda: exhibit.check_or_commit(request, commit=True))
        candidate(panel_task, changed, root)
        exhibit.check_or_commit(request, commit=True)
        assert exhibit.load_state(store)['content']['diagram'] == previous['diagram']
        stale = candidate(competing, previous, root)
        fails(lambda: exhibit.check_or_commit(stale, commit=True))
        audience_task = exhibit.prepare(store, 2, audience='高中生')
        request = candidate(audience_task, changed, root)
        exhibit.check_or_commit(request, commit=True)
        assert exhibit.status(store)['brief']['audience'] == '高中生'
        assert exhibit.load_state(store)['source']['sha256'] == exhibit.file_hash(source)


def test_path_safety_bad_json_and_snapshot_corruption():
    with fixture() as (root, source, store):
        fails(lambda: exhibit.store_path('../outside'))
        fails(lambda: exhibit.store_path(root / 'projects/fixture/outputs/not-a-store'))
        alias = source.parent / 'alias.md'
        alias.symlink_to(source)
        fails(lambda: exhibit.source_path(str(alias.relative_to(root))))
        outside = root / 'projects/fixture/exhibits/linked'
        outside.symlink_to(store, target_is_directory=True)
        fails(lambda: exhibit.status(outside))
        task = exhibit.prepare(store, 0)
        request = candidate(task, content(source, root), root)
        output = root / task['agent_task']['outputs'][0]['path']
        output.write_text('{"schema":"one", "schema":"two"}', encoding='utf-8')
        fails(lambda: exhibit.check_or_commit(request))
        output.unlink()
        output.symlink_to(source)
        fails(lambda: exhibit.check_or_commit(request))
        pointer = exhibit.read_json(store / 'current.json')
        (store / 'revisions' / pointer['snapshot']).write_text('{}', encoding='utf-8')
        fails(lambda: exhibit.load_state(store))


def test_failed_pointer_publish_and_failed_render():
    with fixture() as (root, source, store):
        task = exhibit.prepare(store, 0)
        request = candidate(task, content(source, root), root)
        original = exhibit.atomic_json

        def fail_pointer(path, value):
            if Path(path) == store / 'current.json':
                raise OSError('simulated publication failure')
            original(path, value)

        with patch.object(exhibit, 'atomic_json', side_effect=fail_pointer):
            fails(lambda: exhibit.check_or_commit(request, commit=True))
        assert exhibit.load_state(store)['revision'] == 0
        exhibit.check_or_commit(request, commit=True)
        state_hash = exhibit.file_hash(store / 'current.json')

        def fail_render(content_value, brief, directory, *, canvas_mm=None):
            (directory / 'partial.pptx').write_bytes(b'not an artifact')
            raise ValueError('simulated renderer failure')

        with patch.object(renderer, 'render', side_effect=fail_render):
            fails(lambda: exhibit.build(store, 1))
        assert list((store / 'outputs').iterdir()) == []
        assert exhibit.file_hash(store / 'current.json') == state_hash
        with patch.object(renderer, 'render', return_value={'mechanical_status': 'passed', 'remote_calls': 0}):
            fails(lambda: exhibit.build(store, 1))
        assert list((store / 'outputs').iterdir()) == []


def test_actual_render(font, output=None):
    with fixture() as (root, source, store):
        committed(root, source, store)
        state = exhibit.load_state(store)
        state['brief']['font'] = font
        state['content']['takeaway']['text'] = 'ExampleModel 先找相关文段，再依据文段组织带引用的回答，并通过反馈继续修订；本样板仅用于工程检查，不对应真实研究。'
        state['content']['panels'][1]['body'] = 'ExampleModel 面向科学解释任务，使用约 4500 个合成测试文段检查中英文混排。系统保留原生文字，并在导出后逐行回读换行结果。这仅用于工程检查，不代表模型表现或真实科学结论。'
        exhibit.save_revision(store, state)
        result = exhibit.build(store, 1)
        assert result['checks']['mechanical_status'] == 'passed'
        assert result['checks']['canvas_mm'] == [594, 841]
        assert result['checks']['remote_calls'] == 0
        assert result['checks']['renderer_sha256'] == renderer.compiler_hash()
        assert result['public_release'] == 'not_approved'
        directory = root / result['output_dir']
        manifest = exhibit.read_json(directory / 'manifest.json')
        assert set(manifest['files']) == {'board.pptx', 'board.pdf', 'preview.png', 'layout.json'}
        assert str(root) not in (directory / 'manifest.json').read_text(encoding='utf-8')
        for name, expected_hash in manifest['files'].items():
            assert exhibit.file_hash(directory / name) == expected_hash
        layout = exhibit.read_json(directory / 'layout.json')
        body_objects = [obj for obj in layout['objects'] if obj.get('panel_id') and obj['id'].endswith('-body')]
        assert len(body_objects) == 4 and all(obj['claim_ids'] for obj in body_objects)
        overflow = deepcopy(state['content'])
        overflow['panels'][0]['body'] = '不可静默缩小字体。' * 150
        fails(lambda: renderer.compile_layout(overflow, state['brief'], renderer.font_info(font)))
        if output:
            import shutil
            target = Path(output)
            assert not target.exists(), 'Never overwrite an existing smoke output'
            shutil.copytree(directory, target)
            print(json.dumps({'test_only': True, 'preview': str((target / 'preview.png').resolve())}, ensure_ascii=False))


def test_source_drift_and_fsync_failure_during_build():
    for scenario in ('source_drift', 'directory_fsync'):
        with fixture() as (root, source, store):
            committed(root, source, store)
            state_hash = exhibit.file_hash(store / 'current.json')
            original_fsync = exhibit.os.fsync

            def fake_render(content_value, brief, directory, *, canvas_mm=None):
                for name in ('board.pptx', 'board.pdf', 'preview.png', 'layout.json'):
                    (directory / name).write_bytes(b'TEST-ONLY artifact placeholder')
                if scenario == 'source_drift':
                    source.write_text('source changed during build', encoding='utf-8')
                return {'mechanical_status': 'passed', 'remote_calls': 0}

            def fail_final_fsync(descriptor):
                outputs = store / 'outputs'
                if scenario == 'directory_fsync' and exhibit.os.fstat(descriptor).st_ino == outputs.stat().st_ino:
                    raise OSError('simulated final directory fsync failure')
                original_fsync(descriptor)

            with patch.object(renderer, 'render', side_effect=fake_render), patch.object(exhibit.os, 'fsync', side_effect=fail_final_fsync):
                fails(lambda: exhibit.build(store, 1))
            assert list((store / 'outputs').iterdir()) == []
            assert exhibit.file_hash(store / 'current.json') == state_hash


def composition_content(root, source, store):
    from PIL import Image
    import science_exhibit_composition as composition
    image = root / 'projects/fixture/synthetic.png'
    Image.new('RGB', (160, 80), '#267F86').save(image)
    asset = composition.import_asset(exhibit, store, image, '合成插图，仅用于工程检查', 'TEST-ONLY，不是实验图或科学结论')
    texts = [
        {'id': 'title', 'text': '合成科学展板', 'roles': [], 'claim_ids': []},
        {'id': 'hook', 'text': '怎样保留理解主线？', 'roles': ['question'], 'claim_ids': ['claim-1']},
        {'id': 'takeaway', 'text': '仅用于工程检查，不对应真实科学研究。',
         'roles': ['approach', 'finding', 'boundary'], 'claim_ids': ['claim-1']},
    ]
    objects = []
    for identifier, top, size, height in [('title', 40, 40, 80), ('hook', 160, 28, 70),
                                           ('takeaway', 300, 24, 100), ('source-label', 1180, 16, 100),
                                           ('draft-label', 1320, 12, 45)]:
        objects.append({'id': 'text-' + identifier, 'type': 'text', 'x': 40, 'y': top,
                        'w': 920, 'h': height, 'size': size, 'color': '#18323B', 'bold': False,
                        'align': 'left', 'text_ref': identifier, 'claim_ids': []})
    objects.insert(3, {'id': 'main-image', 'type': 'image', 'x': 40, 'y': 600, 'w': 600, 'h': 300,
                       'asset_id': asset['id'], 'claim_ids': ['claim-1']})
    return {'schema': 'science-exhibit-content-v2', 'claims': content(source, root)['claims'],
            'texts': texts, 'assets': [asset], 'composition': {'schema': 'science-exhibit-composition-v1',
                'canvas': [1000, 1400], 'background': '#F7FAF8', 'objects': objects}}


def test_composition_content_assets_and_legacy_compatibility():
    import science_exhibit_composition as composition
    with fixture() as (root, source, store):
        candidate = composition_content(root, source, store)
        state = exhibit.load_state(store)
        exhibit.validate_content(candidate, state)
        request = exhibit.prepare(store, 0)
        exhibit.atomic_json(root / request['agent_task']['outputs'][0]['path'], candidate)
        exhibit.check_or_commit(root / request['request_path'], commit=True)
        assert exhibit.check_or_commit(root / request['request_path'], commit=True)['already_committed']
        assert exhibit.load_state(store)['content'] == candidate
        for change in ('missing_text', 'missing_role', 'stretch', 'lost_qualifier', 'unknown_claim', 'hidden_asset', 'extra_field'):
            bad = deepcopy(candidate)
            if change == 'missing_text':
                bad['composition']['objects'].pop()
            elif change == 'missing_role':
                bad['texts'][-1]['roles'].remove('boundary')
            elif change == 'stretch':
                bad['composition']['objects'][3]['w'] = 500
            elif change == 'lost_qualifier':
                bad['texts'][-1]['text'] = '未保留科学限定'
            elif change == 'unknown_claim':
                bad['texts'][-1]['claim_ids'] = ['unknown']
            elif change == 'hidden_asset':
                bad['composition']['objects'].pop(3)
            else:
                bad['composition']['objects'][0]['script'] = 'untrusted'
            fails(lambda: exhibit.validate_content(bad, state))
        asset = root / candidate['assets'][0]['path']
        asset.write_bytes(b'changed')
        fails(lambda: exhibit.validate_content(candidate, state))
        symlink = root / 'projects/fixture/link.png'
        symlink.symlink_to(root / 'projects/fixture/synthetic.png')
        fails(lambda: composition.import_asset(exhibit, store, symlink, 'TEST-ONLY', '合成测试'))


def test_actual_composition_render(font):
    import science_exhibit_composition as composition
    from PIL import Image
    with fixture() as (root, source, store):
        state = exhibit.load_state(store)
        candidate = composition_content(root, source, store)
        jpeg = root / 'projects/fixture/synthetic.jpg'
        Image.new('RGB', (160, 80), '#7967A2').save(jpeg)
        asset = composition.import_asset(exhibit, store, jpeg, '合成JPEG，仅用于工程测试', '显式居中裁切测试，非科学证据')
        candidate['assets'].append(asset)
        candidate['composition']['objects'].insert(4, {'id': 'cropped-jpeg', 'type': 'image', 'x': 650, 'y': 600,
            'w': 300, 'h': 300, 'fit': 'cover', 'asset_id': asset['id'], 'claim_ids': []})
        state['brief']['font'] = font
        exhibit.validate_content(candidate, state)
        directory = store / 'actual-mixed-render'
        directory.mkdir()
        result = renderer.render(candidate, state['brief'], directory, canvas_mm=[594, 841])
        assert result['mechanical_status'] == 'passed' and result['image_object_count'] == 2
        assert len(result['image_assets']) == 2 and result['image_effective_dpi']
        assert str(root) not in (directory / 'layout.json').read_text()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--render', action='store_true')
    parser.add_argument('--font', default='Heiti SC')
    parser.add_argument('--output')
    args = parser.parse_args()
    test_typographic_units()
    test_rendered_lines_match_objects_not_overlapping_empty_boxes()
    test_visible_acronym_definitions()
    test_creation_validation_and_idempotency()
    test_content_rejections_and_source_drift()
    test_panel_edits_stale_requests_and_audience_change()
    test_path_safety_bad_json_and_snapshot_corruption()
    test_failed_pointer_publish_and_failed_render()
    test_source_drift_and_fsync_failure_during_build()
    test_composition_content_assets_and_legacy_compatibility()
    if args.render:
        test_actual_render(args.font, args.output)
        test_actual_composition_render(args.font)
    print('science exhibit regression: PASS' + (' (actual render)' if args.render else ''))


if __name__ == '__main__':
    main()

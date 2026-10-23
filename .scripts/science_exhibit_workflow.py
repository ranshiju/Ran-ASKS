#!/usr/bin/env python3
"""Shared exhibit collaboration records; the host owns all semantic decisions."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
import shlex
import uuid

REQUIREMENTS = ('topic', 'audience', 'setting', 'goal', 'delivery', 'constraints')
REQUIRED = REQUIREMENTS[:-1]
WORK_FIELDS = ('requirements', 'plan', 'confirmation', 'authorization', 'content_binding',
               'preview', 'acceptance', 'delivery', 'feedback', 'last_candidate_sha256')
ARTIFACTS = ('board.pptx', 'board.pdf', 'preview.png', 'layout.json')


def initial_state(topic='', audience=''):
    requirements = {key: {'value': '', 'status': 'unknown', 'basis': ''} for key in REQUIREMENTS}
    for key, value in (('topic', topic), ('audience', audience), ('delivery', '中文 A1 竖版，可编辑 PPTX、PDF 和预览')):
        if value:
            requirements[key] = {'value': value, 'status': 'proposed', 'basis': '初始化候选或 Agent 建议，尚未获用户确认'}
    return {
        'requirements': requirements, 'plan': None, 'confirmation': None, 'authorization': None,
        'content_binding': None, 'preview': None, 'acceptance': None, 'delivery': None,
        'feedback': [], 'last_candidate_sha256': None,
    }


def optional_text(core, value, maximum):
    core.require(isinstance(value, str), 'Text field required')
    if value:
        core.text(value, maximum)


def validate_requirements(core, requirements):
    core.fields(requirements, REQUIREMENTS)
    for record in requirements.values():
        core.fields(record, ('value', 'status', 'basis'))
        core.require(record['status'] in {'confirmed', 'proposed', 'assumption', 'unknown'}, 'Invalid requirement status')
        optional_text(core, record['value'], 800)
        optional_text(core, record['basis'], 1200)
        if record['status'] == 'unknown':
            core.require(record['value'] == '', 'Unknown requirements cannot carry a settled value')
        else:
            core.text(record['value'], 800)
            core.text(record['basis'], 1200)


def validate_state_shape(core, state):
    work = state['workflow']
    core.fields(work, WORK_FIELDS)
    validate_requirements(core, work['requirements'])
    core.require(work['plan'] is None or isinstance(work['plan'], dict), 'Invalid plan record')
    if work['plan'] is not None:
        validate_plan(core, work['plan'], state)
    for key in ('content_binding', 'last_candidate_sha256'):
        core.require(work[key] is None or isinstance(work[key], str) and re.fullmatch(r'[a-f0-9]{64}', work[key]), 'Invalid binding hash')
    for key in ('confirmation', 'authorization'):
        if work[key] is not None:
            core.fields(work[key], ('binding_sha256', 'instruction'))
            core.text(work[key]['instruction'], 2000)
            core.require(isinstance(work[key]['binding_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', work[key]['binding_sha256']), 'Invalid confirmation binding')
    if work['preview'] is not None:
        core.fields(work['preview'], ('output_dir', 'manifest_sha256', 'content_sha256', 'binding_sha256'))
        core.text(work['preview']['output_dir'], 500)
        for key in ('manifest_sha256', 'content_sha256', 'binding_sha256'):
            core.require(isinstance(work['preview'][key], str) and re.fullmatch(r'[a-f0-9]{64}', work['preview'][key]), 'Invalid preview binding')
    if work['acceptance'] is not None:
        core.fields(work['acceptance'], ('manifest_sha256', 'instruction'))
        core.text(work['acceptance']['instruction'], 2000)
        core.require(work['preview'] is not None and work['acceptance']['manifest_sha256'] == work['preview']['manifest_sha256'], 'Acceptance must bind the selected preview')
    if work['delivery'] is not None:
        core.fields(work['delivery'], ('manifest_sha256', 'output_dir'))
        core.require(work['acceptance'] is not None and work['delivery']['manifest_sha256'] == work['acceptance']['manifest_sha256'] and
                     work['delivery']['output_dir'] == work['preview']['output_dir'], 'Delivery must bind an accepted preview')
    core.require(isinstance(work['feedback'], list) and len(work['feedback']) <= 500, 'Invalid feedback history')
    for item in work['feedback']:
        core.fields(item, ('id', 'scope', 'text', 'instruction', 'revision') +
                    (('review_binding', 'decisions') if 'review_binding' in item else ()))
        core.require(item['scope'] in {'brief', 'plan', 'content', 'visual'}, 'Invalid feedback scope')
        core.require(isinstance(item['id'], str) and re.fullmatch(r'[a-f0-9]{32}', item['id']), 'Invalid feedback identity')
        core.require(type(item['revision']) is int and 0 <= item['revision'] <= state['revision'], 'Invalid feedback revision')
        core.text(item['text'], 2000)
        core.text(item['instruction'], 2000)
        if 'review_binding' in item:
            from science_exhibit_review import validate_decisions
            core.fields(item['review_binding'], ('run', 'result_sha256', 'manifest_sha256', 'content_sha256'))
            core.managed_path(item['review_binding']['run'], 'projects')
            for key in ('result_sha256', 'manifest_sha256', 'content_sha256'):
                core.require(isinstance(item['review_binding'][key], str) and re.fullmatch('[a-f0-9]{64}', item['review_binding'][key]),
                             'Invalid review binding')
            validate_decisions(core, item['decisions'])


def binding(core, state):
    return core.digest({'requirements': state['workflow']['requirements'], 'plan': state['workflow']['plan'],
                        'source': state['source'], 'brief': state['brief']})


def missing_requirements(state):
    return [key for key in REQUIRED if state['workflow']['requirements'][key]['status'] != 'confirmed']


def confirmed(core, state, key):
    record = state['workflow'][key]
    return record is not None and record['binding_sha256'] == binding(core, state)


def production_blockers(state):
    plan = state['workflow']['plan']
    if plan is None:
        return ['尚未提交创作方案']
    blockers = []
    if plan['visual']['kind'] not in {'flow', 'concept', 'composition'}:
        blockers.append('当前 renderer 支持 flow、原生图元 concept 或受控混排 composition')
    import science_exhibit_render
    if tuple(plan['canvas_mm']) not in science_exhibit_render.SUPPORTED_CANVASES:
        blockers.append('当前 renderer 支持 A1/A0 竖版：594 × 841 或 841 × 1189 mm')
    return blockers


def phase(core, state):
    if state['schema'] == 'science-exhibit-v1':
        return 'legacy_draft'
    work = state['workflow']
    if state['source'] is None or missing_requirements(state):
        return 'briefing'
    if work['plan'] is None or not confirmed(core, state, 'confirmation') or not confirmed(core, state, 'authorization'):
        return 'planning'
    if state['content'] is None or work['content_binding'] != binding(core, state) or work['preview'] is None:
        return 'making'
    return 'delivered' if work['delivery'] is not None else 'reviewing'


def require_authorization(core, state):
    core.require(state['schema'] == 'science-exhibit-v2', 'Adopt the legacy draft before collaborative actions')
    core.require(not missing_requirements(state), 'Confirm topic, audience, setting, goal and delivery through the brief first')
    core.require(confirmed(core, state, 'confirmation') and confirmed(core, state, 'authorization'), 'Confirmed plan and explicit production authorization required')
    core.require(not production_blockers(state), '; '.join(production_blockers(state)))
    core.verify_source(state)


def invalidate(state, *, planning=False):
    work = state['workflow']
    if planning:
        work['confirmation'] = None
        work['authorization'] = None
        work['content_binding'] = None
    work['preview'] = None
    work['acceptance'] = None
    work['delivery'] = None


def record_content(core, state):
    invalidate(state)
    state['workflow']['content_binding'] = binding(core, state)


def validate_plan(core, plan, state):
    core.fields(plan, ('schema', 'title', 'goal', 'viewer_question', 'story', 'visual', 'layout',
                       'canvas_mm', 'claims', 'boundaries', 'open_questions'))
    core.require(plan['schema'] == 'science-exhibit-plan-v1', 'Unsupported plan schema')
    for key, maximum in (('title', 80), ('goal', 500), ('viewer_question', 300), ('story', 1600), ('layout', 1000)):
        core.text(plan[key], maximum)
    core.fields(plan['visual'], ('kind', 'description'))
    core.require(plan['visual']['kind'] in {'flow', 'concept', 'composition'}, 'Unsupported visual kind')
    core.text(plan['visual']['description'], 1200)
    core.require(isinstance(plan['canvas_mm'], list) and len(plan['canvas_mm']) == 2 and
                 all(type(value) is int and 100 <= value <= 2000 for value in plan['canvas_mm']), 'Valid physical canvas required')
    core.validate_claims(plan['claims'], state)
    for key in ('boundaries', 'open_questions'):
        core.require(isinstance(plan[key], list) and len(plan[key]) <= 30, 'Bounded plan notes required')
        for value in plan[key]:
            core.text(value, 500)
    core.require(plan['boundaries'], 'Scientific boundaries must remain explicit')


def prepare(core, store, state, expected_revision, kind):
    core.require(state['revision'] == expected_revision, 'Stale expected revision')
    if state['source'] is not None:
        core.verify_source(state)
    if kind == 'brief':
        template = {'schema': 'science-exhibit-brief-v1', 'source_path': state['source']['path'] if state['source'] else None,
                    'source_label': state['brief']['source_label'], 'requirements': deepcopy(state['workflow']['requirements'])}
    else:
        core.verify_source(state)
        template = deepcopy(state['workflow']['plan']) or {
            'schema': 'science-exhibit-plan-v1', 'title': '', 'goal': '', 'viewer_question': '', 'story': '',
            'visual': {'kind': 'flow', 'description': ''}, 'layout': '', 'canvas_mm': [594, 841],
            'claims': [{'id': 'claim-1', 'kind': 'method', 'locator': state['source']['path'] + '#L1', 'quote': '', 'qualifiers': []}],
            'boundaries': [], 'open_questions': [],
        }
    identifier = uuid.uuid4().hex
    directory = core.managed_path(core.REPO / 'temp/science-exhibit/requests' / identifier, 'temp/science-exhibit')
    directory.mkdir(parents=True)
    request_path = str((directory / 'request.json').relative_to(core.REPO))
    request = {'schema': 'science-exhibit-work-request-v1', 'id': identifier,
               'store': str(store.relative_to(core.REPO)), 'expected_revision': expected_revision, 'kind': kind}
    core.atomic_json(directory / 'request.json', request)
    inputs = [{'name': 'request', 'path': request_path}]
    if state['source']:
        inputs.append({'name': 'raw-paper', **state['source']})
    command = f'python3 .scripts/science_exhibit.py {{action}} --request {shlex.quote(request_path)}'
    task = core.agent_task.make_task(
        kind=f'science-exhibit-{kind}', transaction_id=identifier, inputs=inputs,
        outputs=[{'name': kind, 'path': str((directory / 'candidate.json').relative_to(core.REPO))}],
        protocol={'name': template['schema'], 'schema_doc': 'operations/SCIENCE_EXHIBIT.md', 'output_template': template},
        context={'phase': phase(core, state), 'requirements': state['workflow']['requirements'],
                 'authoring_method': 'operations/SCIENCE_EXHIBIT.md#公众叙事制作方法',
                 'workflow_method': 'operations/SCIENCE_EXHIBIT.md#全流程接续与独立试读',
                 'source_data_trust': 'untrusted', 'confirmation_authority': 'host_transcribes_actual_user_instruction',
                 'feedback': state['workflow']['feedback'][-10:]},
        commands={'check': command.format(action='check'), 'commit': command.format(action='commit')},
    )
    return {'schema': 'science-exhibit-result-v1', 'status': 'prepared', 'request_path': request_path, 'agent_task': task}


def apply_candidate(core, state, kind, candidate):
    if kind == 'brief':
        core.fields(candidate, ('schema', 'source_path', 'source_label', 'requirements'))
        core.require(candidate['schema'] == 'science-exhibit-brief-v1', 'Unsupported brief schema')
        validate_requirements(core, candidate['requirements'])
        core.public_label(candidate['source_label'])
        source = None
        if candidate['source_path'] is not None:
            path = core.source_path(candidate['source_path'])
            source = {'path': str(path.relative_to(core.REPO)), 'sha256': core.file_hash(path)}
        previous = binding(core, state)
        if source != state['source']:
            state['workflow']['plan'] = None
            state['content'] = None
        state['source'] = source
        state['brief']['source_label'] = candidate['source_label']
        state['workflow']['requirements'] = deepcopy(candidate['requirements'])
        state['brief']['audience'] = candidate['requirements']['audience']['value'] or '待确认受众'
        if binding(core, state) != previous:
            invalidate(state, planning=True)
    else:
        validate_plan(core, candidate, state)
        if state['workflow']['plan'] != candidate:
            state['workflow']['plan'] = deepcopy(candidate)
            invalidate(state, planning=True)


def check_or_commit(core, path, commit):
    core.require(path.name == 'request.json' and re.fullmatch(r'[a-f0-9]{32}', path.parent.name), 'Managed request path required')
    request = core.read_json(path)
    core.fields(request, ('schema', 'id', 'store', 'expected_revision', 'kind'))
    core.require(request['schema'] == 'science-exhibit-work-request-v1' and request['id'] == path.parent.name, 'Invalid work request')
    core.require(type(request['expected_revision']) is int and request['expected_revision'] >= 0, 'Invalid request revision')
    core.require(request['kind'] in {'brief', 'plan'}, 'Invalid work request kind')
    candidate = core.read_json(core.managed_path(path.parent / 'candidate.json', 'temp/science-exhibit/requests'))
    with core.locked(request['store']) as store:
        state = core.load_state(store)
        core.require(state['schema'] == 'science-exhibit-v2', 'Adopt the legacy draft first')
        if state['source']:
            core.verify_source(state)
        candidate_sha = core.digest(candidate)
        already = state['last_request'] == request['id']
        if already:
            core.require(state['workflow']['last_candidate_sha256'] == candidate_sha, 'Committed candidate changed')
        else:
            core.require(state['revision'] == request['expected_revision'], 'Stale expected revision')
            state = deepcopy(state)
            apply_candidate(core, state, request['kind'], candidate)
            if commit:
                state['revision'] += 1
                state['last_request'] = request['id']
                state['workflow']['last_candidate_sha256'] = candidate_sha
                core.save_revision(store, state)
        return {'schema': 'science-exhibit-result-v1', 'status': 'committed' if commit or already else 'ready_to_commit',
                'revision': state['revision'], 'phase': phase(core, state), 'already_committed': already,
                'semantic_review': 'pending', 'public_release': 'not_approved'}


def artifact(core, store, state, output_dir, *, check_runtime=False):
    directory = core.managed_path(output_dir, 'projects')
    core.require(directory.parent == store / 'outputs' and re.fullmatch(r'r[0-9]{6}-[a-f0-9]{32}', directory.name), 'Rendered output must belong to this exhibit')
    manifest_path = core.managed_path(directory / 'manifest.json', 'projects')
    manifest = core.read_json(manifest_path)
    core.require(isinstance(manifest, dict) and manifest.get('schema') == 'science-exhibit-delivery-v1', 'Invalid delivery manifest')
    core.require(manifest.get('workflow_binding_sha256') == binding(core, state) and
                 manifest.get('source_sha256') == state['source']['sha256'] and
                 manifest.get('content_sha256') == core.digest(state['content']), 'Export belongs to an earlier source, brief, plan or content')
    core.require(isinstance(manifest.get('files'), dict) and set(manifest['files']) == set(ARTIFACTS), 'Invalid artifact manifest')
    core.require({path.name for path in directory.iterdir()} == set(ARTIFACTS) | {'manifest.json'}, 'Invalid artifact set')
    for name, expected_hash in manifest['files'].items():
        path = core.managed_path(directory / name, 'projects')
        core.require(path.is_file() and core.file_hash(path) == expected_hash, 'Rendered artifact changed or missing')
    checks = manifest.get('checks')
    core.require(isinstance(checks, dict) and checks.get('mechanical_status') == 'passed' and checks.get('remote_calls') == 0, 'Local mechanical checks required')
    core.require(checks.get('canvas_mm') == state['workflow']['plan']['canvas_mm'], 'Rendered canvas differs from the confirmed plan')
    if check_runtime:
        import science_exhibit_render as renderer
        core.validate_content(state['content'], state)
        core.require(checks.get('renderer_sha256') == renderer.compiler_hash(), 'Renderer or artwork validator changed since preview')
        core.require(checks.get('font_sha256') == renderer.font_info(state['brief']['font'])['sha256'], 'Font changed since preview')
    return {'output_dir': str(directory.relative_to(core.REPO)), 'manifest_sha256': core.file_hash(manifest_path),
            'content_sha256': core.digest(state['content']), 'binding_sha256': binding(core, state)}


def validate_preview(core, store, state, *, check_runtime=False):
    preview = state['workflow']['preview']
    core.require(preview is not None, 'Record an actual rendered preview first')
    actual = artifact(core, store, state, preview['output_dir'], check_runtime=check_runtime)
    core.require(actual == preview, 'Recorded preview or manifest changed')
    return preview


def describe(core, store, state):
    import science_exhibit_review
    stage = phase(core, state)
    prefix = f'python3 .scripts/wg.py science-exhibit {{action}} --store {shlex.quote(str(core.store_path(store).relative_to(core.REPO)))} --expected-revision {state["revision"]}'
    actions = []

    def action(name, suffix='', requires_user_instruction=False):
        actions.append({'action': name, 'command': prefix.format(action=name) + suffix,
                        'requires_user_instruction': requires_user_instruction})

    if stage == 'legacy_draft':
        action('adopt')
        return {'phase': stage, 'next_actions': actions, 'user_preview_acceptance': 'not_recorded',
                'warning': '历史原型仅为草稿；显式 adopt 不推定任何需求、方案、制作或预览批准'}
    work = state['workflow']
    if work['preview'] is not None:
        validate_preview(core, core.store_path(store), state)
    if stage == 'briefing':
        action('prepare', ' --kind brief')
    elif stage == 'planning':
        action('prepare', ' --kind plan')
        if work['plan'] is not None and not work['plan']['open_questions']:
            if not confirmed(core, state, 'confirmation'):
                action('confirm-plan', f' --binding-sha256 {binding(core, state)}', True)
            elif not production_blockers(state):
                action('start-making', f' --binding-sha256 {binding(core, state)}', True)
    else:
        action('prepare', ' --kind content')
        if work['content_binding'] == binding(core, state):
            action('build')
        if work['preview'] is not None:
            if work['acceptance'] is None:
                action('accept-preview', f' --preview-sha256 {work["preview"]["manifest_sha256"]}', True)
            elif work['delivery'] is None:
                action('deliver', f' --preview-sha256 {work["preview"]["manifest_sha256"]}')
    return {
        'phase': stage, 'requirements': work['requirements'], 'missing_requirements': missing_requirements(state),
        'source_selected': state['source'] is not None, 'plan': work['plan'], 'binding_sha256': binding(core, state),
        'plan_confirmed': confirmed(core, state, 'confirmation'), 'production_authorized': confirmed(core, state, 'authorization'),
        'production_blockers': production_blockers(state), 'preview': work['preview'],
        'user_preview_acceptance': 'accepted' if work['acceptance'] is not None else 'not_recorded',
        'confirmation': work['confirmation'], 'authorization': work['authorization'], 'acceptance': work['acceptance'],
        'delivery': work['delivery'], 'feedback': work['feedback'][-10:], 'next_actions': actions,
        'confirmation_authority': 'host_transcription_not_machine_verified_human_identity',
        'workflow_method': 'operations/SCIENCE_EXHIBIT.md#全流程接续与独立试读',
        'input_action': None if state['source'] else 'Use the existing managed academic ingest for a new paper; bind its Raw locator through the brief.',
        'review_actions': science_exhibit_review.describe(core, store, state),
    }


def add_commands(sub):
    for name in ('adopt', 'confirm-plan', 'start-making', 'record-preview', 'accept-preview', 'deliver', 'feedback'):
        parser = sub.add_parser(name)
        parser.add_argument('--store', required=True)
        parser.add_argument('--expected-revision', required=True, type=int)
        if name in {'confirm-plan', 'start-making'}:
            parser.add_argument('--binding-sha256', required=True)
        if name in {'confirm-plan', 'start-making', 'accept-preview', 'feedback'}:
            parser.add_argument('--instruction', required=True)
        if name == 'record-preview':
            parser.add_argument('--output-dir', required=True)
        if name in {'accept-preview', 'deliver'}:
            parser.add_argument('--preview-sha256', required=True)
        if name == 'feedback':
            parser.add_argument('--scope', choices=('brief', 'plan', 'content', 'visual'), required=True)
            parser.add_argument('--text', required=True)


def act(core, args):
    with core.locked(args.store) as store:
        state = core.load_state(store)
        core.require(state['revision'] == args.expected_revision, 'Stale expected revision')
        if state['source'] is not None:
            core.verify_source(state)
        state = deepcopy(state)
        if args.action == 'adopt':
            core.require(state['schema'] == 'science-exhibit-v1', 'Only a legacy draft needs adoption')
            state['schema'] = 'science-exhibit-v2'
            state['workflow'] = initial_state(state['brief']['source_label'], state['brief']['audience'])
        else:
            core.require(state['schema'] == 'science-exhibit-v2', 'Adopt the legacy draft first')
            work = state['workflow']
            if hasattr(args, 'instruction'):
                core.text(args.instruction, 2000)
            if args.action in {'confirm-plan', 'start-making'}:
                core.require(not missing_requirements(state), 'Requirements are still unconfirmed')
                core.require(args.binding_sha256 == binding(core, state), 'Stale brief/plan binding')
                validate_plan(core, work['plan'], state)
                core.require(not work['plan']['open_questions'], 'Resolve plan questions before confirming or making')
                if args.action == 'start-making':
                    core.require(confirmed(core, state, 'confirmation'), 'Confirm the specific plan first')
                    core.require(not production_blockers(state), '; '.join(production_blockers(state)))
                key = 'confirmation' if args.action == 'confirm-plan' else 'authorization'
                work[key] = {'binding_sha256': binding(core, state), 'instruction': args.instruction}
            elif args.action == 'feedback':
                core.text(args.text, 2000)
                core.require(len(work['feedback']) < 500, 'Feedback history limit reached; start a new exhibit')
                work['feedback'].append({'id': uuid.uuid4().hex, 'scope': args.scope, 'text': args.text,
                                         'instruction': args.instruction, 'revision': state['revision']})
                if args.scope in {'brief', 'plan'}:
                    invalidate(state, planning=True)
                else:
                    work['acceptance'] = None
                    work['delivery'] = None
            else:
                require_authorization(core, state)
                core.require(work['content_binding'] == binding(core, state), 'Content needs recommitting against the current plan')
                if args.action == 'record-preview':
                    preview = artifact(core, store, state, args.output_dir)
                    core.require(work['preview'] is not None and preview == work['preview'], 'Preview must have been registered by the managed build; handwritten receipts are not accepted')
                    return core.status(store)
                else:
                    preview = validate_preview(core, store, state, check_runtime=True)
                    core.require(args.preview_sha256 == preview['manifest_sha256'], 'Stale preview acceptance')
                    if args.action == 'accept-preview':
                        work['acceptance'] = {'manifest_sha256': preview['manifest_sha256'], 'instruction': args.instruction}
                    elif args.action == 'deliver':
                        core.require(work['acceptance'] is not None and work['acceptance']['manifest_sha256'] == preview['manifest_sha256'], 'Actual preview acceptance required before delivery')
                        work['delivery'] = {'manifest_sha256': preview['manifest_sha256'], 'output_dir': preview['output_dir']}
                    else:
                        raise ValueError('Unknown collaboration action')
        state['revision'] += 1
        state['last_request'] = None
        validate_state_shape(core, state)
        core.save_revision(store, state)
    return core.status(store)

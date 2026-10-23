#!/usr/bin/env python3
"""Evidence-bound host-Agent authoring and local draft exhibit delivery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid

import agent_task
import source_locator
import science_exhibit_workflow as workflow

REPO = Path(__file__).resolve().parent.parent
PANELS = ('question', 'approach', 'finding', 'boundary')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, expected):
    require(isinstance(value, dict) and set(value) == set(expected), f'Expected fields: {sorted(expected)}')


def text(value, maximum):
    require(isinstance(value, str) and value.strip() and len(value) <= maximum, f'Nonempty text of at most {maximum} characters required')
    require(not any(ord(character) < 32 and character not in '\n\t' for character in value), 'Control characters are not allowed')
    return value


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    require(Path(path).stat().st_size <= 2_000_000, 'JSON exceeds the bounded input size')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'Duplicate JSON field: {key}')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f'Non-finite JSON number: {value}')

    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique, parse_constant=reject_constant)


def managed_path(value, prefix):
    path = Path(value)
    if not path.is_absolute():
        path = REPO / path
    relative = path.relative_to(REPO)
    require('..' not in relative.parts, 'Path traversal rejected')
    path.relative_to(REPO / prefix)
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'Symbolic links are not allowed')
        if parent == REPO:
            break
    path.resolve().relative_to((REPO / prefix).resolve())
    return path


def store_path(value):
    path = managed_path(value, 'projects')
    parts = path.relative_to(REPO / 'projects').parts
    require(len(parts) == 3 and parts[1] == 'exhibits' and IDENTIFIER.fullmatch(parts[2]), 'Store must be projects/<workspace>/exhibits/<id>')
    require((REPO / 'projects' / parts[0]).is_dir(), 'Create or select the project workspace first')
    return path


def source_path(value):
    path = managed_path(value, 'academic/raw')
    require(path.is_file() and path.suffix in {'.md', '.txt'}, 'First version requires an ingested academic Raw text file')
    return path


def atomic_json(path, value):
    path = Path(path)
    require(not path.is_symlink(), 'Symbolic link output rejected')
    previous = path.read_bytes() if path.exists() else None
    replaced = False
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        replaced = True
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        if replaced:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                restore_descriptor, restore_path = tempfile.mkstemp(dir=path.parent, prefix='.restore-')
                try:
                    with os.fdopen(restore_descriptor, 'wb') as stream:
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(restore_path, path)
                finally:
                    Path(restore_path).unlink(missing_ok=True)
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def locked(store):
    store = store_path(store)
    require(store.is_dir(), 'Exhibit store does not exist')
    descriptor = os.open(store / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield store
    finally:
        os.close(descriptor)


def load_state(store):
    store = store_path(store)
    pointer = read_json(managed_path(store / 'current.json', 'projects'))
    fields(pointer, ('snapshot', 'sha256'))
    require(isinstance(pointer['snapshot'], str) and re.fullmatch(r'r[0-9]{6}-[a-f0-9]{32}\.json', pointer['snapshot']), 'Invalid snapshot locator')
    snapshot = managed_path(store / 'revisions' / pointer['snapshot'], 'projects')
    require(file_hash(snapshot) == pointer['sha256'], 'Revision snapshot corrupted')
    state = read_json(snapshot)
    expected = ('schema', 'revision', 'source', 'brief', 'content', 'last_request')
    fields(state, expected + (('workflow',) if state.get('schema') == 'science-exhibit-v2' else ()))
    require(state['schema'] in {'science-exhibit-v1', 'science-exhibit-v2'} and type(state['revision']) is int and state['revision'] >= 0, 'Invalid exhibit state')
    if state['source'] is not None:
        fields(state['source'], ('path', 'sha256'))
    else:
        require(state['schema'] == 'science-exhibit-v2', 'Legacy exhibits require a source')
    fields(state['brief'], ('audience', 'source_label', 'font'))
    for key in state['brief']:
        text(state['brief'][key], 400)
    if state['schema'] == 'science-exhibit-v2':
        workflow.validate_state_shape(sys.modules[__name__], state)
    return state


def verify_source(state):
    require(state['source'] is not None, 'Select an ingested paper before planning or making content')
    path = source_path(state['source']['path'])
    require(file_hash(path) == state['source']['sha256'], 'Source changed; start a new source-bound exhibit instead of silently adopting it')
    return path.read_text(encoding='utf-8')


def save_revision(store, state):
    filename = f'r{state["revision"]:06d}-{uuid.uuid4().hex}.json'
    revisions = managed_path(store / 'revisions', 'projects')
    revisions.mkdir(exist_ok=True)
    snapshot = revisions / filename
    atomic_json(snapshot, state)
    atomic_json(store / 'current.json', {'snapshot': filename, 'sha256': file_hash(snapshot)})


def public_label(source_label):
    text(source_label, 240)
    require(not re.search(r'file://|synology://|/(?:Users|home|private|tmp)/|[A-Za-z]:[\\/]', source_label), 'Public source label must not contain local paths')
    return source_label


def init_store(store, source=None, source_label='待选论文', audience='非专业成年公众', font='Heiti SC', *, legacy=False):
    store = store_path(store)
    path = source_path(source) if source else None
    require(not legacy or path is not None, 'Legacy exhibits require a source')
    public_label(source_label)
    text(audience, 120)
    text(font, 120)
    require(not store.exists(), 'Exhibit already exists; use prepare to revise it')
    store.parent.mkdir(exist_ok=True)
    store.mkdir()
    with locked(store):
        state = {
            'schema': 'science-exhibit-v1' if legacy else 'science-exhibit-v2', 'revision': 0,
            'source': {'path': str(path.relative_to(REPO)), 'sha256': file_hash(path)} if path else None,
            'brief': {'audience': audience, 'source_label': source_label, 'font': font},
            'content': None, 'last_request': None,
        }
        if not legacy:
            state['workflow'] = workflow.initial_state(source_label if path else '', audience)
        save_revision(store, state)
    return status(store)


def normalized(value):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip()


def validate_claims(claims, state):
    source_lines = verify_source(state).splitlines()
    require(isinstance(claims, list) and 1 <= len(claims) <= 16, 'Expected 1..16 source-bound claims')
    by_id = {}
    for claim in claims:
        fields(claim, ('id', 'kind', 'locator', 'quote', 'qualifiers'))
        require(isinstance(claim['id'], str) and IDENTIFIER.fullmatch(claim['id']) and claim['id'] not in by_id, 'Unique claim ID required')
        require(claim['kind'] in {'background', 'observation', 'theory', 'simulation', 'method', 'limitation'}, 'Unsupported claim kind')
        path_part, anchor = source_locator.split_locator(text(claim['locator'], 500))
        require(path_part == state['source']['path'], 'Claim must cite the bound Raw text')
        line_range = source_locator.line_range(anchor)
        require(line_range is not None and line_range[1] <= len(source_lines) and line_range[1] - line_range[0] <= 50, 'Valid bounded Raw line range required')
        quote = text(claim['quote'], 3000)
        evidence = '\n'.join(source_lines[line_range[0] - 1:line_range[1]])
        require(normalized(quote) in normalized(evidence), 'Quote not found at the declared source locator')
        qualifiers = claim['qualifiers']
        require(isinstance(qualifiers, list) and len(qualifiers) <= 5, 'Qualifier list required')
        for qualifier in qualifiers:
            text(qualifier, 120)
        by_id[claim['id']] = claim
    return by_id


def display_texts(content, brief):
    if content.get('schema') == 'science-exhibit-content-v2':
        import science_exhibit_composition
        return science_exhibit_composition.display_texts(content, brief)
    items = [('title', content['title']), ('hook', content['hook']), ('takeaway.text', content['takeaway']['text'])]
    diagram = content['diagram']
    items.append(('diagram.headline', diagram['headline']))
    if diagram.get('kind') == 'concept':
        labels = [(index, item) for index, item in enumerate(diagram['artwork']['objects']) if item['type'] == 'text']
        for index, item in sorted(labels, key=lambda pair: (pair[1]['y'], pair[1]['x'], pair[0])):
            items.append((f'diagram.artwork.objects[{index}].text', item['text']))
    else:
        items.extend((f'diagram.steps[{index}].text', step['text']) for index, step in enumerate(diagram['steps']))
    for panel in content['panels']:
        items.extend((f'panels.{panel["id"]}.{key}', panel[key]) for key in ('headline', 'body'))
    items.append(('brief.source_label', brief['source_label']))
    return items


def validate_display_acronyms(items):
    pattern = re.compile(r'(?<![A-Za-z0-9])(?:[A-Z]\.){2,}|(?<![A-Za-z0-9])[A-Z][A-Z0-9]+(?![A-Za-z0-9])')

    def expansion(value):
        return (not pattern.search(value) and
                (len(re.findall(r'[\u3400-\u9fff]', value)) >= 2 or
                 len(re.findall(r'\b[A-Za-z]*[a-z][A-Za-z]+\b', value)) >= 2))

    defined = set()
    issues = []
    for location, value in items:
        value = unicodedata.normalize('NFKC', value)
        for match in pattern.finditer(value):
            abbreviation = match.group().replace('.', '')
            if sum(character.isupper() for character in abbreviation) < 2:
                continue
            before, after = value[:match.start()], value[match.end():]
            preceding = re.search(r'([^。！？!?;；:\n()]{2,100})\(\s*$', before)
            following = re.match(r'\s*\(([^()\n]{2,100})\)', after)
            introduced = (preceding and re.match(r'\s*\)', after) and expansion(preceding.group(1))) or (following and expansion(following.group(1)))
            if introduced:
                defined.add(abbreviation)
            elif abbreviation not in defined:
                issues.append(f'{location}: {match.group()}')
    require(not issues, 'Undefined English abbreviation(s); define at first visible use, e.g. 中文全称（ABC）: ' + '; '.join(issues))


def validate_content(content, state):
    require(isinstance(content, dict), 'Content object required')
    if content.get('schema') == 'science-exhibit-content-v2':
        import science_exhibit_composition
        return science_exhibit_composition.validate(sys.modules[__name__], content, state)
    fields(content, ('schema', 'title', 'hook', 'takeaway', 'claims', 'panels', 'diagram'))
    require(content['schema'] == 'science-exhibit-content-v1', 'Unsupported content schema')
    text(content['title'], 40)
    text(content['hook'], 100)
    claims = content['claims']
    by_id = validate_claims(claims, state)

    def references(value):
        require(isinstance(value, list) and value and len(value) == len(set(value)), 'Nonempty unique claim references required')
        require(all(isinstance(identifier, str) and identifier in by_id for identifier in value), 'Unknown claim reference')

    fields(content['takeaway'], ('text', 'claim_ids'))
    text(content['takeaway']['text'], 160)
    references(content['takeaway']['claim_ids'])
    require(isinstance(content['panels'], list) and len(content['panels']) == 4, 'Four question/approach/finding/boundary panels required')
    for index, panel in enumerate(content['panels']):
        fields(panel, ('id', 'headline', 'body', 'claim_ids'))
        require(panel['id'] == PANELS[index], 'Stable ordered panel IDs required')
        text(panel['headline'], 32)
        text(panel['body'], 320)
        references(panel['claim_ids'])
    diagram = content['diagram']
    require(isinstance(diagram, dict), 'Diagram object required')
    if diagram.get('kind') == 'concept':
        import presentation_artwork
        fields(diagram, ('kind', 'headline', 'claim_ids', 'artwork'))
        references(diagram['claim_ids'])
        artwork = diagram['artwork']
        require(isinstance(artwork, dict), 'Embedded artwork object required')
        artwork_brief = {'schema': 'presentation-artwork-brief-v1', 'canvas': artwork.get('canvas'),
                         'sensitivity': 'local_only', 'brief': diagram['headline'],
                         'sources': [state['source']['path']]}
        presentation_artwork.validate_brief(artwork_brief)
        presentation_artwork.validate_artwork(artwork, artwork_brief)
        diagram_items = [diagram]
        diagram_text = [item['text'] for item in artwork['objects'] if item['type'] == 'text']
        require(diagram_text, 'Concept visual requires readable labels')
    else:
        fields(diagram, ('headline', 'steps'))
        steps = diagram['steps']
        require(isinstance(steps, list) and 2 <= len(steps) <= 4, 'Diagram requires 2..4 original explanatory steps')
        step_ids = set()
        for step in steps:
            fields(step, ('id', 'text', 'claim_ids'))
            require(isinstance(step['id'], str) and re.fullmatch(r'step-[a-z0-9]{1,16}', step['id']) and step['id'] not in step_ids, 'Unique diagram step ID required')
            step_ids.add(step['id'])
            text(step['text'], 60)
            references(step['claim_ids'])
        diagram_items = steps
        diagram_text = [step['text'] for step in steps]
    text(diagram['headline'], 50)
    displayed = '\n'.join([content['takeaway']['text'], *(panel['body'] for panel in content['panels']), *diagram_text])
    used = set(content['takeaway']['claim_ids'])
    for item in [*content['panels'], *diagram_items]:
        used.update(item['claim_ids'])
    require(used == set(by_id), 'Every claim must be used; remove unneeded evidence')
    for claim in claims:
        for qualifier in claim['qualifiers']:
            require(qualifier in displayed, 'Declared scientific qualifier must remain visible')
    validate_display_acronyms(display_texts(content, state['brief']))
    return content


def status(store):
    state = load_state(store)
    if state['source'] is not None:
        verify_source(state)
    result = {
        'schema': 'science-exhibit-result-v1', 'status': 'draft',
        'store': str(store_path(store).relative_to(REPO)), 'revision': state['revision'],
        'content_present': state['content'] is not None, 'brief': state['brief'],
        'source': state['source'], 'semantic_review': 'pending',
        'visual_review': 'not_checked', 'public_release': 'not_approved',
    }
    result.update(workflow.describe(sys.modules[__name__], store, state))
    return result


def prepare(store, expected_revision, panel=None, audience=None, kind=None):
    with locked(store) as store:
        state = load_state(store)
        if state['schema'] == 'science-exhibit-v2':
            selected = kind or ('brief' if workflow.phase(sys.modules[__name__], state) == 'briefing' else 'plan' if workflow.phase(sys.modules[__name__], state) == 'planning' else 'content')
            if selected in {'brief', 'plan'}:
                require(panel is None and audience is None, 'Brief/plan requests cannot use content-edit arguments')
                return workflow.prepare(sys.modules[__name__], store, state, expected_revision, selected)
            require(selected == 'content', 'Unsupported work kind')
            workflow.require_authorization(sys.modules[__name__], state)
            require(audience is None or audience == state['brief']['audience'], 'Change audience through the brief, then reconfirm the plan')
            if panel:
                require(state['workflow']['content_binding'] == workflow.binding(sys.modules[__name__], state), 'Changed brief/plan requires a whole-story revision before panel edits')
        else:
            require(kind is None or kind == 'content', 'Adopt the legacy exhibit before preparing a brief or plan')
        verify_source(state)
        require(state['revision'] == expected_revision, 'Stale expected revision')
        require(panel is None or state['content'] is not None and panel in PANELS, 'Panel-scoped edits require existing content')
        require(not (panel and audience), 'Audience changes require a whole-story request')
        identifier = uuid.uuid4().hex
        root = managed_path(REPO / 'temp/science-exhibit/requests', 'temp/science-exhibit')
        root.mkdir(parents=True, exist_ok=True)
        directory = root / identifier
        directory.mkdir()
        request = {
            'schema': 'science-exhibit-request-v1', 'id': identifier,
            'store': str(store.relative_to(REPO)), 'expected_revision': expected_revision,
            'panel': panel, 'audience': audience or state['brief']['audience'],
        }
        text(request['audience'], 120)
        atomic_json(directory / 'request.json', request)
        template = deepcopy(state['content']) or {
            'schema': 'science-exhibit-content-v1', 'title': '', 'hook': '',
            'takeaway': {'text': '', 'claim_ids': ['claim-1']},
            'claims': [{'id': 'claim-1', 'kind': 'method', 'locator': state['source']['path'] + '#L1', 'quote': '', 'qualifiers': []}],
            'panels': [{'id': panel_id, 'headline': '', 'body': '', 'claim_ids': ['claim-1']} for panel_id in PANELS],
            'diagram': {'headline': '', 'steps': [{'id': f'step-{index}', 'text': '', 'claim_ids': ['claim-1']} for index in range(1, 4)]},
        }
        if state['content'] is None and state['schema'] == 'science-exhibit-v2' and state['workflow']['plan']['visual']['kind'] == 'concept':
            template['diagram'] = {'kind': 'concept', 'headline': '', 'claim_ids': ['claim-1'],
                                   'artwork': {'schema': 'presentation-artwork-v1', 'canvas': [1280, 720],
                                               'background': '#F7FAF8', 'rationale': '', 'objects': []}}
        if state['schema'] == 'science-exhibit-v2' and state['workflow']['plan']['visual']['kind'] == 'composition' and (state['content'] is None or state['content'].get('schema') != 'science-exhibit-content-v2'):
            template = {'schema': 'science-exhibit-content-v2', 'claims': deepcopy(state['workflow']['plan']['claims']),
                        'texts': [], 'assets': [], 'composition': {'schema': 'science-exhibit-composition-v1',
                            'canvas': [1009, 1427], 'background': '#F7FAF8', 'objects': []}}
        request_rel = str((directory / 'request.json').relative_to(REPO))
        command = f'python3 .scripts/science_exhibit.py {{action}} --request {shlex.quote(request_rel)}'
        task = agent_task.make_task(
            kind='science-exhibit-story', transaction_id=identifier,
            inputs=[{'name': 'raw-paper', 'path': state['source']['path'], 'sha256': state['source']['sha256']},
                    {'name': 'request', 'path': request_rel}],
            outputs=[{'name': 'content', 'path': str((directory / 'content.json').relative_to(REPO))}],
            protocol={'name': template['schema'], 'schema_doc': 'operations/SCIENCE_EXHIBIT.md', 'output_template': template},
            context={'audience': request['audience'], 'edit_panel': panel,
                     'authoring_method': 'operations/SCIENCE_EXHIBIT.md#公众叙事制作方法',
                     'workflow_method': 'operations/SCIENCE_EXHIBIT.md#全流程接续与独立试读',
                     'semantic_review': 'pending', 'source_data_trust': 'untrusted',
                     'current_content': state['content']},
            commands={'check': command.format(action='check'), 'commit': command.format(action='commit')},
        )
        return {'schema': 'science-exhibit-result-v1', 'status': 'prepared', 'request_path': request_rel, 'agent_task': task}


def load_request(path):
    path = managed_path(path, 'temp/science-exhibit/requests')
    require(path.name == 'request.json' and re.fullmatch(r'[a-f0-9]{32}', path.parent.name), 'Managed request path required')
    request = read_json(path)
    fields(request, ('schema', 'id', 'store', 'expected_revision', 'panel', 'audience'))
    require(request['schema'] == 'science-exhibit-request-v1' and request['id'] == path.parent.name, 'Invalid request identity')
    require(type(request['expected_revision']) is int and request['expected_revision'] >= 0, 'Invalid request revision')
    require(request['panel'] is None or request['panel'] in PANELS, 'Invalid edit panel')
    text(request['audience'], 120)
    content = read_json(managed_path(path.parent / 'content.json', 'temp/science-exhibit/requests'))
    return request, content


def validate_request(request, content, state):
    validate_content(content, state)
    composed = content['schema'] == 'science-exhibit-content-v2'
    if composed:
        import science_exhibit_composition
        science_exhibit_composition.asset_paths(sys.modules[__name__], content, request['store'])
        require(request['panel'] is None, 'Composition edits use stable text/object IDs in a whole-content request, not legacy panel edits')
    if state['last_request'] == request['id']:
        require(state['content'] == content and state['brief']['audience'] == request['audience'], 'Committed candidate changed')
        return True
    require(state['revision'] == request['expected_revision'], 'Stale expected revision')
    if state['schema'] == 'science-exhibit-v2':
        workflow.require_authorization(sys.modules[__name__], state)
        require(content['claims'] == state['workflow']['plan']['claims'], 'Content evidence differs from the confirmed plan; revise the plan first')
        visual_kind = 'composition' if composed else content['diagram'].get('kind', 'flow')
        require(visual_kind == state['workflow']['plan']['visual']['kind'], 'Content visual differs from the confirmed plan; do not silently downgrade or replace it')
        if request['panel']:
            require(state['workflow']['content_binding'] == workflow.binding(sys.modules[__name__], state), 'Changed brief/plan requires a whole-story revision')
    if request['panel']:
        old, candidate = deepcopy(state['content']), deepcopy(content)
        require(old is not None, 'Panel edit requires previous content')
        index = PANELS.index(request['panel'])
        candidate['panels'][index] = old['panels'][index]
        require(candidate == old and request['audience'] == state['brief']['audience'], 'Panel edit changed unrelated content or evidence')
    return False


def check_or_commit(path, commit=False):
    request_file = managed_path(path, 'temp/science-exhibit/requests')
    request_value = read_json(request_file)
    require(isinstance(request_value, dict), 'Request object required')
    if request_value.get('schema') == 'science-exhibit-work-request-v1':
        return workflow.check_or_commit(sys.modules[__name__], request_file, commit)
    request, content = load_request(path)
    with locked(request['store']) as store:
        state = load_state(store)
        already_committed = validate_request(request, content, state)
        if commit and not already_committed:
            state = deepcopy(state)
            state['revision'] += 1
            state['content'] = content
            state['brief']['audience'] = request['audience']
            state['last_request'] = request['id']
            if state['schema'] == 'science-exhibit-v2':
                workflow.record_content(sys.modules[__name__], state)
            save_revision(store, state)
        return {
            'schema': 'science-exhibit-result-v1',
            'status': 'committed' if commit or already_committed else 'ready_to_commit',
            'revision': state['revision'], 'source_check': 'located',
            'semantic_review': 'pending', 'public_release': 'not_approved',
            'already_committed': already_committed,
        }


def build(store, expected_revision):
    import science_exhibit_render
    with locked(store) as store:
        state = load_state(store)
        require(state['revision'] == expected_revision, 'Stale expected revision')
        if state['schema'] == 'science-exhibit-v2':
            workflow.require_authorization(sys.modules[__name__], state)
            require(state['workflow']['content_binding'] == workflow.binding(sys.modules[__name__], state), 'Content belongs to an earlier brief/plan; prepare and commit it again')
        validate_content(state['content'], state)
        outputs = managed_path(store / 'outputs', 'projects')
        outputs.mkdir(exist_ok=True)
        candidate = Path(tempfile.mkdtemp(dir=outputs, prefix='.candidate-'))
        destination = outputs / f'r{state["revision"]:06d}-{uuid.uuid4().hex}'
        published = False
        try:
            canvas = state['workflow']['plan']['canvas_mm'] if state['schema'] == 'science-exhibit-v2' else None
            result = science_exhibit_render.render(state['content'], state['brief'], candidate, canvas_mm=canvas)
            expected = {'board.pptx', 'board.pdf', 'preview.png', 'layout.json'}
            require({path.name for path in candidate.iterdir()} == expected, 'Incomplete rendered artifact set')
            require(all(path.is_file() and not path.is_symlink() and path.stat().st_size for path in candidate.iterdir()), 'Invalid rendered artifact')
            require(result.get('mechanical_status') == 'passed' and result.get('remote_calls') == 0, 'Local mechanical render check failed')
            verify_source(state)
            manifest = {
                'schema': 'science-exhibit-delivery-v1', 'status': 'draft',
                'revision': state['revision'], 'snapshot_sha256': digest(state),
                'source_sha256': state['source']['sha256'], 'source_label': state['brief']['source_label'],
                'checks': result,
                'files': {path.name: file_hash(path) for path in sorted(candidate.iterdir()) if path.is_file()},
            }
            if state['schema'] == 'science-exhibit-v2':
                manifest['workflow_binding_sha256'] = workflow.binding(sys.modules[__name__], state)
                manifest['content_sha256'] = digest(state['content'])
            atomic_json(candidate / 'manifest.json', manifest)
            for path in candidate.iterdir():
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            descriptor = os.open(candidate, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(candidate, destination)
            published = True
            descriptor = os.open(outputs, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if state['schema'] == 'science-exhibit-v2':
                state = deepcopy(state)
                state['workflow']['preview'] = {
                    'output_dir': str(destination.relative_to(REPO)),
                    'manifest_sha256': file_hash(destination / 'manifest.json'),
                    'content_sha256': digest(state['content']),
                    'binding_sha256': workflow.binding(sys.modules[__name__], state),
                }
                state['workflow']['acceptance'] = None
                state['workflow']['delivery'] = None
                state['revision'] += 1
                state['last_request'] = None
                workflow.validate_state_shape(sys.modules[__name__], state)
                save_revision(store, state)
        except BaseException:
            shutil.rmtree(candidate, ignore_errors=True)
            if published:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        delivery = {
            'schema': 'science-exhibit-result-v1', 'status': 'draft_exported',
            'revision': state['revision'], 'output_dir': str(destination.relative_to(REPO)),
            'artifacts': {name: str((destination / name).relative_to(REPO)) for name in (*manifest['files'], 'manifest.json')},
            'checks': result, 'public_release': 'not_approved',
        }
        if state['schema'] == 'science-exhibit-v2':
            delivery['output_revision'] = manifest['revision']
            delivery['commands'] = {'record_preview': f'python3 .scripts/science_exhibit.py record-preview --store {shlex.quote(str(store.relative_to(REPO)))} --expected-revision {state["revision"]} --output-dir {shlex.quote(str(destination.relative_to(REPO)))}'}
        return delivery


def parser():
    root = argparse.ArgumentParser(description='Evidence-bound public science exhibit drafts; no implicit remote calls')
    sub = root.add_subparsers(dest='action', required=True)
    init = sub.add_parser('init')
    init.add_argument('--store', required=True)
    init.add_argument('--source')
    init.add_argument('--source-label', default='待选论文')
    init.add_argument('--audience', default='非专业成年公众')
    init.add_argument('--font', default='Heiti SC')
    for action in ('status', 'resume', 'prepare', 'build'):
        command = sub.add_parser(action)
        command.add_argument('--store', required=True)
        if action not in {'status', 'resume'}:
            command.add_argument('--expected-revision', type=int, required=True)
        if action == 'prepare':
            command.add_argument('--panel', choices=PANELS)
            command.add_argument('--audience')
            command.add_argument('--kind', choices=('brief', 'plan', 'content'))
    for action in ('check', 'commit'):
        sub.add_parser(action).add_argument('--request', required=True)
    asset = sub.add_parser('add-asset')
    asset.add_argument('--store', required=True)
    asset.add_argument('--expected-revision', type=int, required=True)
    asset.add_argument('--path', required=True)
    asset.add_argument('--source-label', required=True)
    asset.add_argument('--usage', required=True)
    import science_exhibit_review
    science_exhibit_review.add_commands(sub)
    workflow.add_commands(sub)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'init':
            result = init_store(args.store, args.source, args.source_label, args.audience, args.font)
        elif args.action in {'status', 'resume'}:
            result = status(args.store)
        elif args.action == 'prepare':
            result = prepare(args.store, args.expected_revision, args.panel, args.audience, args.kind)
        elif args.action == 'build':
            result = build(args.store, args.expected_revision)
        elif args.action in {'check', 'commit'}:
            result = check_or_commit(args.request, commit=args.action == 'commit')
        elif args.action == 'add-asset':
            import science_exhibit_composition
            with locked(args.store) as store:
                state = load_state(store)
                require(state['revision'] == args.expected_revision, 'Stale expected revision')
                workflow.require_authorization(sys.modules[__name__], state)
                result = {'status': 'asset_frozen', 'asset': science_exhibit_composition.import_asset(
                    sys.modules[__name__], store, args.path, args.source_label, args.usage)}
        elif args.action in {'prepare-review', 'record-review', 'compare'}:
            import science_exhibit_review
            result = science_exhibit_review.act(sys.modules[__name__], args)
        else:
            result = workflow.act(sys.modules[__name__], args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(json.dumps({'schema': 'science-exhibit-result-v1', 'status': 'error', 'error': str(error), 'public_release': 'not_approved'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

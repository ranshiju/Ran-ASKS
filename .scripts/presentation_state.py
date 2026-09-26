#!/usr/bin/env python3
"""Shared presentation state, atomic revisions and local single-slide authoring CLI."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
import uuid

from presentation_render import digest, file_hash

REPO = Path(__file__).resolve().parent.parent
SCHEMA = 'presentation-v1'
ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
HASH = re.compile(r'^[a-f0-9]{64}$')
MAX_FILE = 50 * 1024 * 1024
ARTIFACT_SUFFIX = {'pptx': '.pptx', 'pdf': '.pdf', 'preview': '.png'}


class StateError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise StateError(message)


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names.split()),
            f'Expected exact fields: {names}')


def text(value, maximum=2000):
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
            'Expected bounded nonempty text')


def identifier(value):
    require(isinstance(value, str) and ID.fullmatch(value), 'Invalid stable ID')
    return value


def sha(value):
    require(isinstance(value, str) and HASH.fullmatch(value), 'Invalid SHA-256')
    return value


def number(value, low, high):
    require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high,
            'Number outside supported range')


def load_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= MAX_FILE, 'Missing/oversize JSON input')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(StateError(f'Invalid JSON {x}')))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def safe_path(root, relative, *, exists=False):
    require(isinstance(relative, str) and relative, 'Relative path required')
    rel = PurePosixPath(relative)
    require(not rel.is_absolute() and '..' not in rel.parts and '\\' not in relative,
            'Path must remain inside its owner')
    path = root.joinpath(*rel.parts)
    require(path.resolve() == path, 'Symlink paths are not allowed')
    if exists:
        require(path.is_file(), f'Missing file: {relative}')
    return path


def source_path(repo, source):
    path = safe_path(repo, source['path'], exists=True)
    parts = PurePosixPath(source['path']).parts
    allowed = ((len(parts) >= 3 and parts[0] in
                {'academic', 'admin', 'teaching', 'business', 'cross-domain', 'private'}
                and parts[1] == 'raw') or (len(parts) >= 3 and parts[0] == 'projects'))
    require(allowed and not any(p.startswith('.') for p in parts)
            and 'presentations' not in parts, 'Source must be an explicit Raw/project material, not secrets or output state')
    require(path.suffix.lower() in {'.pdf', '.md', '.txt', '.csv', '.png', '.jpg', '.jpeg', '.webp'},
            'Unsupported source type')
    require(path.stat().st_size <= MAX_FILE, 'Source exceeds 50 MiB')
    if 'private' in parts:
        require(source['sensitivity'] == 'private', 'Private source cannot be downgraded')
    return path


def validate_plan(plan):
    fields(plan, 'schema purpose audience language duration_minutes key_message theme slides')
    require(plan['schema'] == SCHEMA, 'Unsupported plan schema')
    for key in ('purpose', 'audience', 'language', 'key_message'):
        text(plan[key])
    number(plan['duration_minutes'], .1, 600)
    theme = plan['theme']
    fields(theme, 'id version font background foreground accent')
    for key in ('id', 'version', 'font'):
        text(theme[key], 100)
    for key in ('background', 'foreground', 'accent'):
        require(isinstance(theme[key], str) and re.fullmatch('[A-Fa-f0-9]{6}', theme[key]), 'Invalid RGB color')
    require(isinstance(plan['slides'], dict) and 1 <= len(plan['slides']) <= 40, 'Expected 1–40 slides')
    for sid, entry in plan['slides'].items():
        identifier(sid)
        fields(entry, 'task time_budget_seconds')
        text(entry['task'])
        number(entry['time_budget_seconds'], 1, 36000)


def validate_content(content, sid):
    fields(content, 'schema slide_id title body notes sources')
    require(content['schema'] == SCHEMA and content['slide_id'] == sid, 'Content schema/slide mismatch')
    text(content['title'], 160)
    require(isinstance(content['body'], list) and 1 <= len(content['body']) <= 6, 'Expected 1–6 body entries')
    for item in content['body']:
        text(item, 700)
    require(isinstance(content['notes'], str) and len(content['notes']) <= 10000, 'Invalid speaker notes')
    require(isinstance(content['sources'], list) and len(content['sources']) <= 20, 'Invalid sources')
    ids = set()
    for source in content['sources']:
        fields(source, 'id path sha256 locator evidence_status sensitivity')
        identifier(source['id'])
        require(source['id'] not in ids, 'Duplicate source ID')
        ids.add(source['id'])
        text(source['path'])
        sha(source['sha256'])
        text(source['locator'])
        require(source['evidence_status'] in {'user_provided', 'unresolved'},
                'This version does not mechanically certify raw_verified claims')
        require(source['sensitivity'] in {'public', 'private'}, 'Invalid source sensitivity')


def validate_design(design, sid, content):
    fields(design, 'schema slide_id layout_id layout_version image_source')
    require(design['schema'] == SCHEMA and design['slide_id'] == sid, 'Design schema/slide mismatch')
    require(design['layout_version'] == '1' and design['layout_id'] in {'title', 'image_text', 'flow'},
            'Unsupported layout/version')
    if design['layout_id'] == 'image_text':
        matches = [s for s in content['sources'] if s['id'] == design['image_source']]
        require(len(matches) == 1 and Path(matches[0]['path']).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'},
                'Image layout needs a declared raster source')
    else:
        require(design['image_source'] is None, 'Unexpected image source')
    if design['layout_id'] == 'flow':
        require(2 <= len(content['body']) <= 4, 'Flow requires 2–4 body entries')


def snapshot(state, sid, kind):
    slide = state['slides'][sid]
    content = slide.get('content')
    if kind == 'content':
        return digest({'content': content, 'task': state['plan']['slides'][sid],
                       'brief': {k: v for k, v in state['plan'].items() if k not in ('theme', 'slides')}})
    design = {'content': snapshot(state, sid, 'content'), 'design': slide.get('design'),
              'theme': state['plan']['theme']}
    if kind == 'design':
        return digest(design)
    require(kind in ('visual', 'preview') and 'build' in slide and 'ir' in slide, 'Actual build required')
    return digest({'design': digest(design), 'ir': slide['ir'], 'build': slide['build']})


def stage(state, sid):
    records = state['session']['approval_records'][sid]
    slide = state['slides'][sid]
    if 'content' not in records:
        return 'content_review'
    if 'design' not in records:
        return 'design_review'
    if 'build' not in slide:
        return 'build_ready'
    return 'locked' if 'preview' in records else 'preview_review'


def validate_state(state):
    fields(state, 'session plan slides')
    validate_plan(state['plan'])
    session = state['session']
    fields(session, 'schema presentation_id revision_id parent_revision approval_mode slide_order source_policy approval_records')
    require(session['schema'] == SCHEMA and session['approval_mode'] == 'strict'
            and session['source_policy'] == 'local_only', 'Unsupported session contract')
    identifier(session['presentation_id'])
    identifier(session['revision_id'])
    if session['parent_revision'] is not None:
        identifier(session['parent_revision'])
    order = session['slide_order']
    require(isinstance(order, list) and len(order) == len(set(order))
            and set(order) == set(state['slides']) == set(state['plan']['slides'])
            == set(session['approval_records']), 'Duplicate/mismatched slide IDs')
    for sid, slide in state['slides'].items():
        require(isinstance(slide, dict) and not set(slide) - {'content', 'design', 'ir', 'build'}, 'Unexpected slide fields')
        if 'content' in slide:
            validate_content(slide['content'], sid)
        if 'design' in slide:
            require('content' in slide, 'Design requires content')
            validate_design(slide['design'], sid, slide['content'])
        records = session['approval_records'][sid]
        require(isinstance(records, dict) and not set(records) - {'content', 'design', 'visual', 'preview'}, 'Invalid approval kind')
        for kind, event in records.items():
            fields(event, 'kind actor confirmation_source expected_revision approved_at snapshot_sha256 accepted_warnings'
                   + (' verdict findings' if kind == 'visual' else '')
                   + (' api_receipt' if kind == 'visual' and 'api_receipt' in event else ''))
            if kind == 'visual':
                require(event['verdict'] in {'passed', 'failed'} and isinstance(event['findings'], list)
                        and all(isinstance(x, str) and x.strip() for x in event['findings']), 'Invalid visual review result')
                require(event['verdict'] != 'failed' or event['findings'], 'Failed visual review needs findings')
            require(event['kind'] == kind, 'Approval kind mismatch')
            for key in ('actor', 'confirmation_source', 'approved_at'):
                text(event[key])
            identifier(event['expected_revision'])
            require(event['snapshot_sha256'] == snapshot(state, sid, kind), 'Approval snapshot mismatch')
            require(isinstance(event['accepted_warnings'], list), 'Invalid warning acknowledgement')
        if 'content' in records:
            require('content' in slide, 'Content approval without content')
        if 'design' in records:
            require('content' in records and 'design' in slide, 'Design approval out of order')
        if 'build' in slide or 'ir' in slide:
            require('design' in records and {'build', 'ir'} <= set(slide), 'Build without approved design/IR')
            from presentation_render import validate_ir
            validate_ir(slide['ir'])
            require(slide['ir']['slide_id'] == sid, 'IR slide identity mismatch')
            build = slide['build']
            fields(build, 'schema renderer native_objects artifacts structural_check text_roundtrip requested_font observed_pdf_fonts warnings visual_review user_approval remote_calls')
            require(build['schema'] == 'presentation-build-v1' and build['structural_check'] == 'passed'
                    and build['text_roundtrip'] == 'passed' and build['remote_calls'] == 0
                    and build['visual_review'] == 'not_checked' and build['user_approval'] == 'not_requested',
                    'Build is not a local mechanical success')
            fields(build['artifacts'], 'pptx pdf preview')
            for artifact in build['artifacts'].values():
                fields(artifact, 'sha256')
                sha(artifact['sha256'])
        if 'visual' in records:
            require('build' in slide, 'Visual review requires actual preview')
        if 'preview' in records:
            require('visual' in records and records['visual']['verdict'] == 'passed', 'User preview approval requires passed visual review')
            require(records['preview']['accepted_warnings'] == slide['build']['warnings'], 'Warnings not acknowledged')


def _checkpoint(name):
    """No-op fault-injection seam; tests replace it, never controlled by environment."""


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _checkpoint('file:' + path.name)


class Store:
    def __init__(self, path, repo=REPO):
        self.repo = Path(repo).resolve()
        raw = Path(path)
        self.root = raw if raw.is_absolute() else self.repo / raw
        require(self.root.resolve() == self.root, 'Store path/parents must not be symlinks')
        try:
            parts = self.root.relative_to(self.repo).parts
        except ValueError as exc:
            raise StateError('Store must be in this repository') from exc
        require(len(parts) == 4 and parts[0] == 'projects' and parts[2] == 'presentations',
                'Store must be projects/<workspace>/presentations/<id>')
        identifier(parts[3])
        require((self.repo / 'projects' / parts[1]).is_dir(), 'Owning project must already exist')

    @contextmanager
    def locked(self):
        require(self.root.resolve() == self.root, 'Store path changed to a symlink')
        self.root.mkdir(parents=True, exist_ok=True)
        path = safe_path(self.root, '.writer.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _blob(self, data, suffix=""):
        require(len(data) <= MAX_FILE, 'Snapshot exceeds 50 MiB')
        import hashlib
        key = hashlib.sha256(data).hexdigest()
        path = safe_path(self.root, f'assets/{key}{suffix}')
        if path.exists():
            require(file_hash(path) == key, 'Immutable asset was externally modified')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            pending = safe_path(self.root, f'assets/.pending-{uuid.uuid4().hex}')
            _write(pending, data)
            os.replace(pending, path)
            _sync_dir(path.parent)
        return key

    def source_issues(self, state):
        issues = []
        for sid, slide in state['slides'].items():
            for source in slide.get('content', {}).get('sources', []):
                try:
                    path = source_path(self.repo, source)
                    require(file_hash(path) == source['sha256'], 'Source version changed')
                except (ValueError, OSError) as exc:
                    issues.append({'slide_id': sid, 'source_id': source['id'], 'error': str(exc)})
        return issues

    def _assets(self, state, *, capture=False):
        for slide in state['slides'].values():
            for source in slide.get('content', {}).get('sources', []):
                key = source['sha256']
                if capture:
                    data = source_path(self.repo, source).read_bytes()
                    import hashlib
                    require(hashlib.sha256(data).hexdigest() == key, 'Source changed before snapshot')
                    self._blob(data)
                path = safe_path(self.root, f'assets/{key}', exists=True)
                require(file_hash(path) == key, 'Source snapshot externally modified')
            for name, artifact in slide.get('build', {}).get('artifacts', {}).items():
                key = sha(artifact['sha256'])
                path = safe_path(self.root, f'assets/{key}{ARTIFACT_SUFFIX[name]}', exists=True)
                require(file_hash(path) == key, 'Build/preview snapshot externally modified; treat external edits as new input')

    def read(self):
        pointer = load_json(safe_path(self.root, 'current.json', exists=True))
        fields(pointer, 'schema revision_id receipt_sha256')
        require(pointer['schema'] == SCHEMA, 'Unsupported pointer schema')
        revision = identifier(pointer['revision_id'])
        folder = safe_path(self.root, f'revisions/{revision}')
        receipt_path = safe_path(folder, 'receipt.json', exists=True)
        require(file_hash(receipt_path) == sha(pointer['receipt_sha256']), 'Receipt integrity failure')
        receipt = load_json(receipt_path)
        fields(receipt, 'schema revision_id parent_revision action files')
        require(receipt['schema'] == SCHEMA and receipt['revision_id'] == revision, 'Receipt revision mismatch')
        require(isinstance(receipt['files'], dict), 'Invalid revision manifest')
        actual_files = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
        require(actual_files == set(receipt['files']) | {'receipt.json'}, 'Revision file set changed')
        for name, key in receipt['files'].items():
            require(file_hash(safe_path(folder, name, exists=True)) == sha(key), f'Revision integrity failure: {name}')
        session = load_json(folder / 'session.json')
        require(session['revision_id'] == revision and session['parent_revision'] == receipt['parent_revision']
                and session['presentation_id'] == self.root.name, 'Session identity mismatch')
        slides = {}
        for sid in session['slide_order']:
            identifier(sid)
            slides[sid] = {kind: load_json(folder / f'slides/{sid}/{kind}.json')
                           for kind in ('content', 'design', 'ir', 'build')
                           if f'slides/{sid}/{kind}.json' in receipt['files']}
        state = {'session': session, 'plan': load_json(folder / 'deck_plan.json'), 'slides': slides}
        validate_state(state)
        expected_files = {'session.json', 'deck_plan.json'} | {
            f'slides/{sid}/{kind}.json' for sid, slide in slides.items() for kind in slide}
        require(expected_files == set(receipt['files']), 'Unexpected state files')
        self._assets(state)
        return state

    def _save(self, state, action):
        validate_state(state)
        self._assets(state)
        revision = state['session']['revision_id']
        revisions = safe_path(self.root, 'revisions')
        revisions.mkdir(exist_ok=True)
        pending = safe_path(self.root, f'revisions/.pending-{revision}')
        pending.mkdir()
        files = {'session.json': state['session'], 'deck_plan.json': state['plan']}
        for sid, slide in state['slides'].items():
            for kind, value in slide.items():
                files[f'slides/{sid}/{kind}.json'] = value
        hashes = {}
        for name, value in files.items():
            path = pending / name
            _write(path, json_bytes(value))
            hashes[name] = file_hash(path)
        receipt = {'schema': SCHEMA, 'revision_id': revision,
                   'parent_revision': state['session']['parent_revision'], 'action': action, 'files': hashes}
        _write(pending / 'receipt.json', json_bytes(receipt))
        for directory in sorted((p for p in pending.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            _sync_dir(directory)
        _sync_dir(pending)
        _checkpoint('before_revision_rename')
        target = safe_path(self.root, f'revisions/{revision}')
        os.rename(pending, target)
        _sync_dir(revisions)
        _checkpoint('after_revision_rename')
        pointer = {'schema': SCHEMA, 'revision_id': revision, 'receipt_sha256': file_hash(target / 'receipt.json')}
        temporary = safe_path(self.root, f'.current-{uuid.uuid4().hex}.json')
        _write(temporary, json_bytes(pointer))
        _checkpoint('before_pointer_replace')
        os.replace(temporary, safe_path(self.root, 'current.json'))
        _sync_dir(self.root)
        _checkpoint('after_pointer_replace')
        result = self.summary(state, check_dependencies=False)
        result['status'] = 'committed'
        return result

    def initialize(self, plan):
        validate_plan(plan)
        with self.locked():
            require(not (self.root / 'current.json').exists(), 'Presentation already initialized')
            state = {'plan': deepcopy(plan), 'slides': {sid: {} for sid in plan['slides']},
                     'session': {'schema': SCHEMA, 'presentation_id': self.root.name,
                        'revision_id': uuid.uuid4().hex, 'parent_revision': None,
                        'approval_mode': 'strict', 'slide_order': list(plan['slides']),
                        'source_policy': 'local_only', 'approval_records': {sid: {} for sid in plan['slides']}}}
            return self._save(state, {'op': 'init'})

    def summary(self, state=None, *, check_dependencies=True):
        state = self.read() if state is None else state
        issues = self.source_issues(state)
        for sid, records in state['session']['approval_records'].items():
            if records.get('visual', {}).get('verdict') == 'failed':
                issues.append({'slide_id': sid, 'error': 'Visual review failed; fix and review again',
                               'findings': records['visual']['findings']})
        if check_dependencies and any('build' in slide for slide in state['slides'].values()):
            try:
                from presentation_render import fingerprint
                current = fingerprint()
                issues += [{'slide_id': sid, 'error': 'Renderer/font dependencies changed; unlock and rebuild'}
                           for sid, slide in state['slides'].items()
                           if 'build' in slide and slide['build']['renderer'] != current]
            except Exception as exc:
                issues.append({'error': f'Render dependency verification failed: {exc}'})
        pages = {}
        for sid, slide in state['slides'].items():
            hashes = {kind: snapshot(state, sid, kind) for kind in ('content', 'design')
                      if kind in slide}
            if 'build' in slide:
                hashes['preview'] = snapshot(state, sid, 'preview')
            pages[sid] = {'stage': stage(state, sid), 'snapshots': hashes}
            if 'build' in slide:
                pages[sid]['preview_path'] = str(self.root / 'assets' / (slide['build']['artifacts']['preview']['sha256'] + '.png'))
                pages[sid]['single_slide_pptx'] = str(self.root / 'assets' / (slide['build']['artifacts']['pptx']['sha256'] + '.pptx'))
                pages[sid]['warnings'] = slide['build']['warnings']
                review = state['session']['approval_records'][sid].get('visual')
                pages[sid]['visual_review'] = ({'verdict': review['verdict'], 'findings': review['findings']}
                                              if review else {'verdict': 'not_checked', 'findings': []})
        # Recovery is pointer-based, never newest-by-mtime. Report unreachable candidates.
        reachable, rev = set(), state['session']['revision_id']
        while rev and rev not in reachable:
            reachable.add(rev)
            receipt = load_json(safe_path(self.root, f'revisions/{identifier(rev)}/receipt.json', exists=True))
            rev = receipt['parent_revision']
        orphans = sorted(p.name for p in (self.root / 'revisions').iterdir() if p.name not in reachable)
        return {'schema': SCHEMA, 'status': 'blocked' if issues else 'ready',
                'store': str(self.root), 'revision_id': state['session']['revision_id'],
                'slide_order': state['session']['slide_order'], 'slides': pages,
                'issues': issues, 'uncommitted_revisions': orphans,
                'source_policy': 'local_only', 'dependency_check': 'performed' if check_dependencies else 'not_performed',
                'final_assembly_available': True}

    def apply(self, request, *, dry_run=False):
        fields(request, 'schema expected_revision op slide_id payload')
        require(request['schema'] == SCHEMA, 'Unsupported request schema')
        identifier(request['expected_revision'])
        with self.locked():
            state = self.read()
            require(request['expected_revision'] == state['session']['revision_id'], 'Revision conflict; reload before retrying')
            before = deepcopy(state)
            sid, op, payload = request['slide_id'], request['op'], request['payload']
            require(op in {'set-content', 'set-design', 'set-plan', 'reorder', 'approve-content', 'approve-design',
                           'build', 'review', 'lock', 'unlock'}, 'Unsupported operation')
            if op in ('set-plan', 'reorder'):
                require(sid is None, 'Global operation must not name a slide')
            else:
                require(sid in state['slides'], 'Unknown slide ID')
            locked_ids = [s for s in state['slides'] if stage(state, s) == 'locked']
            affected = list(state['slides']) if sid is None else [sid]
            if op != 'unlock':
                conflicts = sorted(set(locked_ids) & set(affected))
                require(not conflicts, f'Explicit user unlock required for affected locked slides: {conflicts}')
            records = state['session']['approval_records'].get(sid)
            slide = state['slides'].get(sid)

            def invalidate(which, level):
                rec = state['session']['approval_records'][which]
                for kind in ({'content', 'design', 'visual', 'preview'} if level == 'content' else {'design', 'visual', 'preview'}):
                    rec.pop(kind, None)
                for key in ('ir', 'build'):
                    state['slides'][which].pop(key, None)

            if op == 'set-content':
                validate_content(payload, sid)
                slide['content'] = deepcopy(payload)
                slide.pop('design', None)
                invalidate(sid, 'content')
            elif op == 'set-design':
                require('content' in records, 'Confirm content before proposing design')
                validate_design(payload, sid, slide['content'])
                slide['design'] = deepcopy(payload)
                invalidate(sid, 'design')
            elif op == 'set-plan':
                validate_plan(payload)
                require(set(payload['slides']) == set(state['slides']), 'Changing slide membership is not supported yet')
                state['plan'] = deepcopy(payload)
                for page in state['slides']:
                    level = 'content' if snapshot(before, page, 'content') != snapshot(state, page, 'content') else 'design'
                    invalidate(page, level)
            elif op == 'reorder':
                fields(payload, 'slide_order')
                order = payload['slide_order']
                require(isinstance(order, list) and len(order) == len(set(order)) and set(order) == set(state['slides']), 'Invalid/duplicate page order')
                state['session']['slide_order'] = order
                for page in state['slides']:
                    state['session']['approval_records'][page].pop('visual', None)
                    state['session']['approval_records'][page].pop('preview', None)
                    state['slides'][page].pop('ir', None)
                    state['slides'][page].pop('build', None)
            elif op in ('approve-content', 'approve-design', 'review', 'lock', 'unlock'):
                fields(payload, 'actor confirmation_source snapshot_sha256 accepted_warnings'
                       + (' verdict findings' if op == 'review' else '')
                       + (' api_receipt' if op == 'review' and 'api_receipt' in payload else ''))
                text(payload['actor'])
                text(payload['confirmation_source'])
                require(isinstance(payload['accepted_warnings'], list), 'Expected accepted warnings list')
                kind = {'approve-content': 'content', 'approve-design': 'design', 'review': 'visual',
                        'lock': 'preview', 'unlock': 'preview'}[op]
                require(payload['snapshot_sha256'] == snapshot(state, sid, kind), 'Approval targets a different snapshot')
                if op == 'approve-content':
                    require('content' in slide, 'No proposed content')
                elif op == 'approve-design':
                    require('content' in records and 'design' in slide, 'Content approval/design proposal required')
                elif op == 'review':
                    require('build' in slide, 'Real rendered preview required')
                    if 'api_receipt' in payload:
                        import pptx_visual
                        pptx_visual.validate_state_review(self, sid, payload)
                elif op == 'lock':
                    require('visual' in records and records['visual']['verdict'] == 'passed',
                            'Actual preview must pass visual inspection first; failed review blocks locking')
                    require(payload['accepted_warnings'] == slide['build']['warnings'], 'Acknowledge the exact build warnings')
                elif op == 'unlock':
                    require(stage(state, sid) == 'locked', 'Slide is not locked')
                if op == 'unlock':
                    records.pop('preview')
                    records.pop('visual', None)
                else:
                    records[kind] = {'kind': kind, **deepcopy(payload),
                        'expected_revision': request['expected_revision'],
                        'approved_at': datetime.now(timezone.utc).isoformat()}
            elif op == 'build':
                fields(payload, '')
                require(stage(state, sid) in {'build_ready', 'preview_review'}, 'Content and design approvals required before build')
                require(not self.source_issues(state), 'Source drift blocks build')
                from presentation_render import compile_slide, render
                compile_slide(slide['content'], slide['design'])
                if not dry_run:
                    self._assets(state)
                    cache = safe_path(self.repo, 'temp/presentation')
                    cache.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix='render-', dir=cache) as tmp:
                        ir, build, files = render(slide['content'], slide['design'], state['plan']['theme'],
                                                  self.root / 'assets', Path(tmp) / 'page')
                        for name, path in files.items():
                            require(self._blob(path.read_bytes(), ARTIFACT_SUFFIX[name]) == build['artifacts'][name]['sha256'], 'Render artifact changed')
                        slide['ir'], slide['build'] = ir, build
                    records.pop('visual', None)
                    records.pop('preview', None)
            if op != 'unlock':
                require(not self.source_issues(state), 'Source drift: refresh content hashes explicitly or unlock affected pages')
            if op in ('review', 'lock'):
                from presentation_render import fingerprint
                require(slide['build']['renderer'] == fingerprint(), 'Stale renderer/font dependencies; rebuild first')
            validate_state(state)
            if dry_run:
                return {'status': 'valid', 'expected_revision': request['expected_revision'],
                        'op': op, 'render_executed': False, 'writes': False}
            # Source snapshots are taken only after the candidate passes the same validator.
            self._assets(state, capture=(op != 'unlock'))
            state['session']['parent_revision'] = request['expected_revision']
            state['session']['revision_id'] = uuid.uuid4().hex
            # Receipts preserve explicit unlock authority as well as ordinary approvals.
            action = {'op': op, 'slide_id': sid}
            if op in ('approve-content', 'approve-design', 'review', 'lock', 'unlock'):
                action['declaration'] = deepcopy(payload)
            return self._save(state, action)

    def prepare(self, sid, kind):
        require(kind in ('content', 'design'), 'Prepare supports content/design proposals only')
        state = self.read()
        require(sid in state['slides'], 'Unknown slide ID')
        require(stage(state, sid) != 'locked', 'Unlock before preparing changes')
        if kind == 'design':
            require('content' in state['session']['approval_records'][sid], 'Content approval required')
        folder = safe_path(self.repo, 'temp/presentation')
        folder.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='task-', dir=folder))
        value = state['slides'][sid].get(kind)
        if value is None:
            value = ({'schema': SCHEMA, 'slide_id': sid, 'title': '', 'body': [''], 'notes': '', 'sources': []}
                     if kind == 'content' else {'schema': SCHEMA, 'slide_id': sid, 'layout_id': 'title',
                                               'layout_version': '1', 'image_source': None})
        request = {'schema': SCHEMA, 'expected_revision': state['session']['revision_id'],
                   'op': 'set-' + kind, 'slide_id': sid, 'payload': value}
        _write(directory / 'request.json', json_bytes(request))
        rel = lambda p: p.relative_to(self.repo).as_posix()
        command = f'python3 .scripts/presentation_state.py {{}} --store {shlex.quote(rel(self.root))} --request {shlex.quote(rel(directory / "request.json"))}'
        from agent_task import make_task
        inputs = [{'name': 'committed_plan', 'path': rel(self.root / 'revisions' / state['session']['revision_id'] / 'deck_plan.json')}]
        if kind == 'design':
            inputs.append({'name': 'approved_content', 'path': rel(self.root / 'revisions' / state['session']['revision_id'] / f'slides/{sid}/content.json')})
        task = make_task(kind='presentation_' + kind, transaction_id=directory.name, inputs=inputs,
                         outputs=[{'name': 'candidate_request', 'path': rel(directory / 'request.json')}],
                         protocol={'name': SCHEMA, 'document': 'operations/PRESENTATION.md',
                                   'section': '阶段1A 命令与数据协议', 'operation': 'set-' + kind},
                         issues=[{'message': 'Host Agent proposes content/design; do not fabricate user confirmation'}],
                         commands={'check': command.format('check'), 'commit': command.format('commit'),
                                   'resume': f'python3 .scripts/presentation_state.py status --store {shlex.quote(rel(self.root))}'})
        _write(directory / 'task.json', json_bytes(task))
        return task


def example_plan():
    return {'schema': SCHEMA, 'purpose': '三页技术验收（非事实材料）', 'audience': '项目维护者',
            'language': 'zh-CN', 'duration_minutes': 3, 'key_message': '验证逐页制作，不代替用户确认',
            'theme': {'id': 'plain', 'version': '1', 'font': 'Arial', 'background': 'F5F7FA',
                      'foreground': '16253D', 'accent': 'E0EBFA'},
            'slides': {sid: {'task': task, 'time_budget_seconds': 60} for sid, task in
                       [('s1', '标题页'), ('s2', '文字配图页'), ('s3', '流程图页')]}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('example-plan')
    init = sub.add_parser('init')
    init.add_argument('--project', required=True, help='Existing projects/<workspace>')
    init.add_argument('--plan', required=True)
    for name in ('status', 'check', 'commit', 'prepare', 'review-api'):
        item = sub.add_parser(name)
        item.add_argument('--store', required=True)
        if name in ('check', 'commit'):
            item.add_argument('--request', required=True)
        if name == 'review-api':
            item.add_argument('--slide', required=True)
            item.add_argument('--expected-revision', required=True)
            item.add_argument('--allow-remote', action='store_true')
        if name == 'prepare':
            item.add_argument('--slide', required=True)
            item.add_argument('--kind', choices=['content', 'design'], required=True)
    args = parser.parse_args(argv)
    store = None
    try:
        if args.command == 'example-plan':
            result = example_plan()
        elif args.command == 'init':
            project = Path(args.project)
            if project.is_absolute():
                project = project.relative_to(REPO)
            require(len(project.parts) == 2 and project.parts[0] == 'projects', 'Expected projects/<workspace>')
            store = Store(REPO / project / 'presentations' / uuid.uuid4().hex)
            result = store.initialize(load_json(args.plan))
        else:
            store = Store(args.store)
            if args.command == 'status':
                result = store.summary()
            elif args.command == 'prepare':
                result = store.prepare(args.slide, args.kind)
            elif args.command == 'review-api':
                import pptx_visual
                result = pptx_visual.review_state(store,args.slide,args.expected_revision,args.allow_remote)
            else:
                result = store.apply(load_json(args.request), dry_run=args.command == 'check')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('status') == 'blocked' else 0
    except Exception as exc:
        error = {'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc)}
        if store is not None:
            error['store'] = str(store.root)
            error['recovery'] = 'Inspect status/current.json; never infer rollback or approval from an interrupted command'
        print(json.dumps(error, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

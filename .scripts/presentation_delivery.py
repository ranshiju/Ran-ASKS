#!/usr/bin/env python3
"""Local native deck export and three explicit, independently scoped Agent actions."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid

import presentation_render as render
import presentation_state as ps
from agent_task import make_task

SCHEMA = 'presentation-delivery-v1'
RESULT_SCHEMA = 'presentation-assistance-v1'
KINDS = ('form-check', 'evidence-check', 'citation-redact')
LABELS = dict(zip(KINDS, ('PPT形式检查', 'PPT证据核查', 'PPT引用脱敏')))


def _checkpoint(name):
    """Local test seam; never configured through input or environment."""


def _publish(root, key, record, files):
    """Seal a complete immutable directory, then atomically expose its stable name."""
    ps.identifier(key)
    target = ps.safe_path(root, key)
    ps.require(not target.exists(), 'Output/request already committed; never overwrite')
    root.mkdir(parents=True, exist_ok=True)
    pending = ps.safe_path(root, '.pending-' + uuid.uuid4().hex)
    pending.mkdir()
    hashes = {}
    for name, value in files.items():
        path = ps.safe_path(pending, name)
        ps._write(path, value if isinstance(value, bytes) else Path(value).read_bytes())
        hashes[name] = render.file_hash(path)
        _checkpoint('file:' + name)
    receipt = dict(record, schema=SCHEMA, id=key, files=hashes, source_policy='local_only')
    ps._write(pending / 'receipt.json', ps.json_bytes(receipt))
    ps._write(pending / 'seal.json', ps.json_bytes({'schema': SCHEMA,
             'receipt_sha256': render.file_hash(pending / 'receipt.json')}))
    for path in sorted((p for p in pending.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        ps._sync_dir(path)
    ps._sync_dir(pending)
    _checkpoint('before_publish')
    os.rename(pending, target)
    ps._sync_dir(root)
    _checkpoint('after_publish')
    return receipt


def _read(root, key):
    folder = ps.safe_path(root, ps.identifier(key))
    ps.require(folder.is_dir(), 'Missing bundle directory')
    seal = ps.load_json(ps.safe_path(folder, 'seal.json', exists=True))
    ps.fields(seal, 'schema receipt_sha256')
    ps.require(seal['schema'] == SCHEMA, 'Unsupported seal')
    receipt_path = ps.safe_path(folder, 'receipt.json', exists=True)
    ps.require(render.file_hash(receipt_path) == ps.sha(seal['receipt_sha256']), 'Receipt integrity failure')
    receipt = ps.load_json(receipt_path)
    ps.require(receipt['schema'] == SCHEMA and receipt['id'] == key
               and receipt['source_policy'] == 'local_only', 'Bundle identity/policy mismatch')
    ps.require(isinstance(receipt['files'], dict), 'Invalid bundle manifest')
    entries = list(folder.rglob('*'))
    ps.require(not any(p.is_symlink() for p in entries), 'Bundle contains symlink')
    actual = {p.relative_to(folder).as_posix() for p in entries if p.is_file()}
    ps.require(actual == set(receipt['files']) | {'receipt.json', 'seal.json'}, 'Bundle file set changed')
    for name, key_hash in receipt['files'].items():
        ps.require(render.file_hash(ps.safe_path(folder, name, exists=True)) == ps.sha(key_hash),
                   'Bundle artifact integrity failure: ' + name)
    return receipt, folder


def _unchecked():
    return {kind: 'not_executed' for kind in KINDS}


class Delivery:
    def __init__(self, store):
        self.store = store

    def _root(self, name):
        return ps.safe_path(self.store.root, name)

    def _current(self, expected, *, locked=False):
        state = self.store.read()
        ps.require(state['session']['revision_id'] == ps.identifier(expected), 'Revision conflict; reload before retrying')
        ps.require(not self.store.source_issues(state), 'Source version changed or unavailable; refresh explicitly')
        if locked:
            missing = [sid for sid in state['slides'] if ps.stage(state, sid) != 'locked']
            ps.require(not missing, f'User-approved locked previews required: {missing}')
            current = render.fingerprint()
            for sid, slide in state['slides'].items():
                ps.require(slide['build']['renderer'] == current, 'Renderer/font dependencies changed; unlock and rebuild')
                ps.require(slide['ir'] == render.compile_slide(slide['content'], slide['design']),
                           'Approved IR does not match content/design')
        return state

    def _output(self, key):
        receipt, folder = _read(self._root('outputs'), key)
        ps.require(receipt['kind'] in ('export', 'citation-redact'), 'This action requires a native deck output')
        return receipt, folder

    def _render_output(self, state, items, key, record, extras=None):
        cache = ps.safe_path(self.store.repo, 'temp/presentation')
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='delivery-', dir=cache) as temporary:
            build, paths = render.assemble(items, state['plan']['theme'], self.store.root / 'assets',
                                           Path(temporary) / 'render')
            ps.require(build['renderer'] == render.fingerprint(), 'Dependencies changed before publication')
            ps.require(all(slide['build']['renderer'] == build['renderer'] for slide in state['slides'].values()),
                       'Output renderer no longer matches approved previews; unlock and rebuild explicitly')
            # External edits/source drift must not be hidden by a successful renderer.
            after = self._current(state['session']['revision_id'])
            ps.require(render.digest(after) == render.digest(state), 'State changed during export')
            files = {'snapshot.json': ps.json_bytes(state), 'deck-content.json': ps.json_bytes(items)}
            artifact_names = {}
            for name, path in paths.items():
                filename = (f'previews/{ps.identifier(name.split(":", 1)[1])}.png'
                            if name.startswith('preview:') else 'deck.' + name)
                files[filename] = path
                artifact_names[name] = filename
            files.update(extras or {})
            record = dict(record, revision_id=state['session']['revision_id'],
                          origin_state_sha256=render.digest(state), build=build, artifacts=artifact_names,
                          user_approval='not_requested', remote_calls=0)
            receipt = _publish(self._root('outputs'), key, record, files)
        return self._result(receipt)

    def _result(self, receipt):
        folder = self._root('outputs') / receipt['id']
        return {'status': 'committed', 'output_id': receipt['id'], 'kind': receipt['kind'],
                'source_policy': 'local_only', 'receipt': str(folder / 'receipt.json'),
                'artifacts': {k: str(folder / v) for k, v in receipt.get('artifacts', {}).items()},
                'assistance': receipt['assistance'], 'user_approval': 'not_requested'}

    def export(self, expected_revision):
        with self.store.locked():
            state = self._current(expected_revision, locked=True)
            items = [deepcopy(state['slides'][sid]) for sid in state['session']['slide_order']]
            # Never read single-slide PPTX binaries as assembly input.
            items = [{k: x[k] for k in ('content', 'design', 'ir')} for x in items]
            return self._render_output(state, items, uuid.uuid4().hex,
                                       {'kind': 'export', 'assistance': _unchecked()})

    def status(self):
        with self.store.locked():
            state = self.store.read()
            result = {'status': 'ready', 'revision_id': state['session']['revision_id'],
                      'outputs': [], 'pending_directories': [], 'issues': self.store.source_issues(state),
                      'source_policy': 'local_only', 'dependency_check': 'not_performed'}
            for name in ('outputs', 'requests'):
                root = self._root(name)
                if not root.exists():
                    continue
                for path in sorted(root.iterdir()):
                    if path.name.startswith('.pending-'):
                        result['pending_directories'].append(str(path))
                        continue
                    try:
                        receipt, _ = _read(root, path.name)
                        if name == 'outputs':
                            result['outputs'].append(dict(self._result(receipt),
                                current_revision=receipt['revision_id'] == state['session']['revision_id']))
                    except (ValueError, OSError, KeyError) as exc:
                        result['issues'].append({'path': str(path), 'error': str(exc)})
            if result['issues']:
                result['status'] = 'blocked'
            return result

    def prepare(self, kind, expected_revision, slide_ids, instruction, *, output_id=None, question=None):
        ps.require(kind in KINDS, 'Unknown assistance kind')
        ps.text(instruction, 4000)
        if kind == 'evidence-check':
            ps.text(question, 4000)
        else:
            ps.require(question is None, 'Question only belongs to evidence-check')
            ps.require(output_id is not None, 'Form/redaction actions require an explicit output ID')
        with self.store.locked():
            state = self._current(expected_revision)
            ps.require(isinstance(slide_ids, list) and slide_ids and len(slide_ids) == len(set(slide_ids))
                       and set(slide_ids) <= set(state['slides']), 'Explicit unique slide scope required')
            for sid in slide_ids:
                ps.require('content' in state['slides'][sid], 'Requested slide has no content')
            base_sha = None
            selected_content = {sid: state['slides'][sid]['content'] for sid in slide_ids}
            inputs = []
            rel = lambda p: p.relative_to(self.store.repo).as_posix()
            if output_id:
                receipt, folder = self._output(output_id)
                ps.require(receipt['origin_state_sha256'] == render.digest(state), 'Output belongs to a different state snapshot')
                base_sha = render.file_hash(folder / 'receipt.json')
                inputs.append({'name': 'deck_content', 'path': rel(folder / 'deck-content.json')})
                inputs.append({'name': 'output_receipt', 'path': rel(folder / 'receipt.json')})
                exported_content = {x['content']['slide_id']: x['content'] for x in ps.load_json(folder / 'deck-content.json')}
                selected_content = {sid: exported_content[sid] for sid in slide_ids}
                if kind == 'form-check':
                    for sid in slide_ids:
                        inputs.append({'name': 'final_' + sid, 'path': rel(folder / receipt['artifacts']['preview:' + sid]), 'read': 'reference'})
                        approved = state['slides'][sid]['build']['artifacts']['preview']['sha256']
                        inputs.append({'name': 'approved_' + sid, 'path': rel(self.store.root / f'assets/{approved}.png'), 'read': 'reference'})
            request_id = uuid.uuid4().hex
            intent = {'kind': kind, 'expected_revision': expected_revision, 'slide_ids': slide_ids,
                      'instruction': instruction, 'question': question, 'output_id': output_id,
                      'output_receipt_sha256': base_sha, 'state_sha256': render.digest(state)}
            context = {'slides': selected_content,
                       'question': question, 'source_policy': 'local_only'}
            if kind == 'evidence-check':
                for sid in slide_ids:
                    for source in state['slides'][sid]['content']['sources']:
                        inputs.append({'name': sid + '_' + source['id'],
                                       'path': rel(self.store.root / 'assets' / source['sha256'])})
            files = {'intent.json': ps.json_bytes(intent), 'context.json': ps.json_bytes(context)}
            _publish(self._root('requests'), request_id, {'kind': 'request', 'revision_id': expected_revision}, files)
            directory = ps.safe_path(self.store.repo, 'temp/presentation')
            directory.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix='action-', dir=directory))
            result_path = directory / 'result.json'
            ps._write(result_path, ps.json_bytes({'schema': RESULT_SCHEMA, 'request_id': request_id,
                'intent_sha256': render.digest(intent), 'actor': '', 'summary': '', 'items': []}))
            inputs += [{'name': name, 'path': rel(self._root('requests') / request_id / (name + '.json'))}
                       for name in ('intent', 'context')]
            command = ('python3 .scripts/presentation_delivery.py {} --store ' + shlex.quote(rel(self.store.root))
                       + ' --result ' + shlex.quote(rel(result_path)))
            task = make_task(kind='presentation_' + kind, transaction_id=request_id, inputs=inputs,
                outputs=[{'name': 'result', 'path': rel(result_path)}],
                protocol={'name': RESULT_SCHEMA, 'document': 'operations/PRESENTATION.md',
                          'section': '阶段1B 导出与按需操作协议', 'operation': kind},
                issues=[{'message': 'Only the requested scope; no fabricated user consent or semantic certification'}],
                commands={'check': command.format('check'), 'commit': command.format('commit'),
                          **({'refresh': command.format('form-api') + ' --allow-remote'} if kind == 'form-check' else {}),
                          'resume': 'python3 .scripts/presentation_delivery.py status --store ' + shlex.quote(rel(self.store.root))},
                context={'label': LABELS[kind], 'question': question, 'instruction': instruction,
                         'source_policy': 'local_only', 'semantic_executor': 'pptx_visual_api' if kind == 'form-check' else 'current_host_agent'})
            ps._write(directory / 'task.json', ps.json_bytes(task))
            return task

    def _form_inputs(self, state, base, folder, sid):
        import pptx_structure
        ps.require(not any(s.get('sensitivity')=='private' or 'private' in Path(s['path']).parts for s in state['slides'][sid]['content']['sources']), 'Private source cannot be uploaded')
        pptx = folder / base['artifacts']['pptx']
        data = pptx_structure.extract(pptx)
        order = [x['content']['slide_id'] for x in ps.load_json(folder / 'deck-content.json')]
        page = data['pages'][order.index(sid)]
        image = folder / base['artifacts']['preview:' + sid]
        approved = self.store.root / 'assets' / (state['slides'][sid]['build']['artifacts']['preview']['sha256'] + '.png')
        return pptx, image, approved, page

    def _validate_api_form(self, item, state, base, folder):
        import pptx_visual
        pptx, image, approved, page = self._form_inputs(state, base, folder, item['slide_id'])
        r = pptx_visual.validate_receipt(item['api_receipt'], image=image, reference=approved,
                mode='form-check', source_sha256=render.file_hash(pptx), page=page)
        verdict = {'pass': 'passed', 'warn': 'issues_found', 'fail': 'issues_found', 'not_checked': 'unable_to_check'}[r['result']['verdict']]
        ps.require(item['verdict'] == verdict, 'API form verdict changed')

    def form_api(self, result, *, allow_remote=False):
        import pptx_visual
        _, request = _read(self._root('requests'), result['request_id'])
        intent = ps.load_json(request / 'intent.json')
        ps.require(intent['kind'] == 'form-check' and result['intent_sha256'] == render.digest(intent), 'Explicit form request required')
        state = self._current(intent['expected_revision'])
        ps.require(render.digest(state) == intent['state_sha256'], 'Requested state changed')
        base, folder = self._output(intent['output_id'])
        ps.require(render.file_hash(folder / 'receipt.json') == intent['output_receipt_sha256'], 'Requested output changed')
        items = []
        for sid in intent['slide_ids']:
            pptx, image, approved, page = self._form_inputs(state, base, folder, sid)
            r = pptx_visual.analyze_image(image, mode='form-check', source_sha256=render.file_hash(pptx), page=page,
                                          reference=approved, allow_remote=allow_remote)
            if r['status'] != 'complete': return {'status':'partial','page':sid,'reason':r.get('reason')}
            v = r['result']; findings = [x['description'] for x in v['issues']] + [x['detail'] for x in v['limitations']]
            verdict = {'pass':'passed','warn':'issues_found','fail':'issues_found','not_checked':'unable_to_check'}[v['verdict']]
            if verdict != 'passed' and not findings: findings = [v['summary'] or 'API could not complete inspection']
            items.append({'slide_id':sid, 'verdict':verdict, 'findings':findings,
                         'inspected_preview_sha256':render.file_hash(image), 'approved_preview_sha256':render.file_hash(approved), 'api_receipt':r})
        candidate = dict(result, actor='pptx_visual API', summary='API form inspection; no user approval', items=items)
        self.apply(candidate, dry_run=True)
        return candidate

    def _quote(self, source, locator, quote):
        """Check exact source location/quote only, never whether it supports a claim."""
        ps.text(quote, 6000)
        path = self.store.root / 'assets' / source['sha256']
        suffix = Path(source['path']).suffix.lower()
        if suffix == '.pdf':
            match = re.fullmatch(r'page:([1-9][0-9]*)', locator)
            ps.require(match is not None, 'PDF evidence locator must be page:N')
            import fitz
            with fitz.open(path) as doc:
                page = int(match[1])
                ps.require(page <= len(doc), 'Evidence page outside source')
                excerpt = doc[page - 1].get_text()
        else:
            ps.require(suffix in {'.txt', '.md', '.csv'}, 'Raster evidence cannot be text-verified; use insufficient with explanation')
            match = re.fullmatch(r'lines:([1-9][0-9]*)-([1-9][0-9]*)', locator)
            ps.require(match is not None, 'Text evidence locator must be lines:N-M')
            lines = path.read_text(encoding='utf-8').splitlines()
            first, last = map(int, match.groups())
            ps.require(first <= last <= len(lines), 'Evidence lines outside source')
            excerpt = '\n'.join(lines[first-1:last])
        ps.require(quote in excerpt, 'Quote not found at the specified source location')

    def _validate(self, result):
        ps.fields(result, 'schema request_id intent_sha256 actor summary items')
        ps.require(result['schema'] == RESULT_SCHEMA, 'Unsupported assistance schema')
        ps.text(result['actor']); ps.text(result['summary'], 10000)
        _, directory = _read(self._root('requests'), result['request_id'])
        intent = ps.load_json(directory / 'intent.json')
        ps.require(result['intent_sha256'] == render.digest(intent), 'Request scope hash mismatch')
        state = self._current(intent['expected_revision'])
        ps.require(render.digest(state) == intent['state_sha256'], 'Requested state changed')
        base, folder = None, None
        if intent['output_id']:
            base, folder = self._output(intent['output_id'])
            ps.require(render.file_hash(folder / 'receipt.json') == intent['output_receipt_sha256'], 'Output version changed')
        items = result['items']; kind = intent['kind']; scope = intent['slide_ids']
        ps.require(isinstance(items, list) and len(items) <= 240, 'Invalid action items')
        if kind in ('form-check', 'evidence-check'):
            ps.require(items and all(isinstance(x, dict) and 'slide_id' in x for x in items)
                       and {x['slide_id'] for x in items} == set(scope), 'Result must cover exactly the requested slide scope')
        edited = ps.load_json(folder / 'deck-content.json') if kind == 'citation-redact' else None
        used_fields = set()
        for item in items:
            if kind == 'form-check':
                ps.fields(item, 'slide_id verdict findings inspected_preview_sha256 approved_preview_sha256'
                          + (' api_receipt' if 'api_receipt' in item else ''))
                sid = item['slide_id']
                ps.require(item['verdict'] in {'passed', 'issues_found', 'unable_to_check'}, 'Invalid form verdict')
                ps.require(isinstance(item['findings'], list) and all(isinstance(x, str) and x.strip() for x in item['findings']), 'Invalid findings')
                ps.require(item['verdict'] == 'passed' or item['findings'], 'Non-pass review must explain findings')
                ps.require(item['inspected_preview_sha256'] == base['files'][base['artifacts']['preview:' + sid]], 'Wrong final preview')
                ps.require(item['approved_preview_sha256'] == state['slides'][sid]['build']['artifacts']['preview']['sha256'], 'Wrong approved preview')
                if 'api_receipt' in item:
                    self._validate_api_form(item,state,base,folder)
            elif kind == 'evidence-check':
                ps.fields(item, 'slide_id claim verdict reason evidence')
                sid = item['slide_id']; ps.text(item['claim']); ps.text(item['reason'], 6000)
                ps.require(item['verdict'] in {'supports', 'contradicts', 'insufficient'}, 'Invalid evidence verdict')
                ps.require(isinstance(item['evidence'], list) and len(item['evidence']) <= 20, 'Invalid evidence list')
                ps.require(item['verdict'] == 'insufficient' or item['evidence'], 'A support/contradiction finding needs source evidence')
                sources = {s['id']: s for s in state['slides'][sid]['content']['sources']}
                for ref in item['evidence']:
                    ps.fields(ref, 'source_id locator quote')
                    ps.require(ref['source_id'] in sources, 'Evidence must reference a declared slide source')
                    self._quote(sources[ref['source_id']], ref['locator'], ref['quote'])
            else:
                ps.fields(item, 'slide_id field before after')
                sid = item['slide_id']; field = item['field']
                ps.require(sid in scope, 'Redaction outside requested scope')
                ps.require((sid, field) not in used_fields, 'Use one complete replacement per text field')
                used_fields.add((sid, field))
                ps.text(item['before'], 10000)
                ps.require(isinstance(item['after'], str) and len(item['after']) <= 10000, 'Invalid replacement')
                c = next(x['content'] for x in edited if x['content']['slide_id'] == sid)
                if field in ('title', 'notes'):
                    parent, slot = c, field
                else:
                    match = re.fullmatch(r'body:([0-5])', field)
                    ps.require(match is not None and int(match[1]) < len(c['body']), 'Unsupported citation text slot')
                    parent, slot = c['body'], int(match[1])
                ps.require(parent[slot] == item['before'], 'Replacement must match the complete original field')
                parent[slot] = item['after']
                ps.validate_content(c, sid)
        if kind == 'form-check':
            ps.require(len(items) == len(scope), 'Duplicate form results')
        if edited is not None:
            ps.require(render.fingerprint() == base['build']['renderer'],
                       'Redaction renderer changed; rebuild explicitly before requesting a derivative')
            changed_slides = {sid for sid, _ in used_fields}
            for item in edited:
                if item['content']['slide_id'] in changed_slides:
                    item['ir'] = render.compile_slide(item['content'], item['design'])
        return intent, state, edited

    def apply(self, result, *, dry_run=False):
        with self.store.locked():
            intent, state, edited = self._validate(result)
            key = 'action-' + result['request_id']
            ps.require(not (self._root('outputs') / key).exists(), 'Request already committed')
            if dry_run:
                return {'status': 'validated', 'kind': intent['kind'], 'semantic_verification': 'not_performed_by_validator',
                        'rendered': False, 'committed': False}
            kind = intent['kind']
            statuses = _unchecked(); statuses[kind] = 'executed'
            record = {'kind': kind, 'revision_id': state['session']['revision_id'], 'assistance': statuses,
                      'origin_state_sha256': render.digest(state), 'request_id': result['request_id'],
                      'semantic_executor': 'pptx_visual_api' if kind == 'form-check' and all('api_receipt' in i for i in result['items']) else 'host_agent',
                      'user_approval': 'not_requested', 'remote_calls': sum(len(i.get('api_receipt',{}).get('attempts',[])) for i in result['items'])}
            files = {'result.json': ps.json_bytes(result), 'intent.json': ps.json_bytes(intent),
                     'context.json': (self._root('requests') / result['request_id'] / 'context.json')}
            if kind == 'citation-redact':
                record['limitations'] = ['Text fields only; raster pixels are not redacted',
                    'Internal receipts/snapshots retain private source context; do not publish this directory',
                    'Redaction is not publication authorization or a full privacy audit']
                return self._render_output(state, edited, key, record, files)
            record['artifacts'] = {'report': 'result.json', 'request': 'intent.json'}
            if kind == 'evidence-check':
                record['artifacts']['source_context'] = 'context.json'
            receipt = _publish(self._root('outputs'), key, record, files)
            return self._result(receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('export', 'status', 'prepare', 'check', 'commit', 'form-api'):
        command = sub.add_parser(name)
        command.add_argument('--store', required=True)
        if name in ('export', 'prepare'):
            command.add_argument('--expected-revision', required=True)
        if name == 'prepare':
            command.add_argument('--kind', choices=KINDS, required=True)
            command.add_argument('--slides', nargs='+', required=True, help='Stable slide IDs, not guessed page numbers')
            command.add_argument('--instruction', required=True, help='Actual user request, not inferred authorization')
            command.add_argument('--question')
            command.add_argument('--output-id')
        if name == 'form-api':
            command.add_argument('--allow-remote', action='store_true')
        if name in ('check', 'commit', 'form-api'):
            command.add_argument('--result', required=True)
    args = parser.parse_args(argv)
    try:
        delivery = Delivery(ps.Store(args.store))
        if args.command == 'export':
            result = delivery.export(args.expected_revision)
        elif args.command == 'status':
            result = delivery.status()
        elif args.command == 'prepare':
            result = delivery.prepare(args.kind, args.expected_revision, args.slides, args.instruction,
                                      output_id=args.output_id, question=args.question)
        elif args.command == 'form-api':
            result = delivery.form_api(ps.load_json(args.result), allow_remote=args.allow_remote)
            if result.get('schema') == RESULT_SCHEMA:
                path = Path(args.result).resolve()
                ps.require(path.is_relative_to(delivery.store.repo / 'temp'), 'Candidate must stay in temp')
                ps._write(path, ps.json_bytes(result))
        else:
            result = delivery.apply(ps.load_json(args.result), dry_run=args.command == 'check')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('status') == 'blocked' else 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__, 'error': str(exc),
            'recovery': 'Inspect delivery status; pending directories are never promoted by mtime'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""State/transaction tests use isolated fixtures. --render additionally requires real slides."""
from __future__ import annotations
import argparse
from copy import deepcopy
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import presentation_state as ps
import presentation_render as render

REPO = Path(__file__).resolve().parent.parent
DECLARATION = 'TEST FIXTURE ONLY: simulated explicit confirmation, not a real user approval'


def content(sid='s1', title='Synthetic title'):
    return {'schema': ps.SCHEMA, 'slide_id': sid, 'title': title,
            'body': ['Knowledge', 'Evidence'], 'notes': 'Synthetic fixture only', 'sources': []}


def design(sid='s1', layout='title', image=None):
    return {'schema': ps.SCHEMA, 'slide_id': sid, 'layout_id': layout,
            'layout_version': '1', 'image_source': image}


def request(store, op, sid, payload):
    return {'schema': ps.SCHEMA, 'expected_revision': store.read()['session']['revision_id'],
            'op': op, 'slide_id': sid, 'payload': payload}


def approval(store, sid, kind, warnings=None):
    result = {'actor': 'test-fixture', 'confirmation_source': DECLARATION,
              'snapshot_sha256': ps.snapshot(store.read(), sid, kind), 'accepted_warnings': warnings or []}
    if kind == 'visual':
        result.update(verdict='passed', findings=[])
    return result


def fake_render(c, d, theme, assets, output):
    """Mechanical adapter stub only. RealRenderTests never uses this stub."""
    output.mkdir()
    paths = {}
    for name, suffix in ps.ARTIFACT_SUFFIX.items():
        paths[name] = output / ('fake' + suffix)
        paths[name].write_bytes(('TEST-ONLY-' + name + ps.digest(c)).encode())
    ir = render.compile_slide(c, d)
    return ir, {'schema': 'presentation-build-v1', 'renderer': {'test_only': True},
                'native_objects': {}, 'artifacts': {n: {'sha256': ps.file_hash(p)} for n, p in paths.items()},
                'structural_check': 'passed', 'text_roundtrip': 'passed',
                'requested_font': theme['font'], 'observed_pdf_fonts': ['TEST-ONLY'],
                'warnings': ['TEST-ONLY visual review required'], 'visual_review': 'not_checked',
                'user_approval': 'not_requested', 'remote_calls': 0}, paths


def process_commit(repo, root, req, queue=None, gate=None, crash=None):
    # Fault injection replaces a pure no-op seam in this child only.
    if crash:
        def checkpoint(name):
            if name == crash:
                os._exit(73)
        ps._checkpoint = checkpoint
    if gate:
        gate.wait()
    try:
        result = ps.Store(Path(root), Path(repo)).apply(req)
        if queue:
            queue.put(('ok', result['revision_id']))
    except Exception as exc:
        if queue:
            queue.put(('error', str(exc)))
        else:
            raise


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name).resolve()
        (self.repo / 'projects/demo').mkdir(parents=True)
        self.store = ps.Store(self.repo / 'projects/demo/presentations/test-deck', self.repo)
        self.store.initialize(ps.example_plan())
        self.patches = [patch.object(render, 'render', side_effect=fake_render),
                        patch.object(render, 'fingerprint', return_value={'test_only': True})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def apply(self, op, sid='s1', payload=None):
        return self.store.apply(request(self.store, op, sid, {} if payload is None else payload))

    def ready(self, sid='s1', layout='title', image=None, value=None):
        self.apply('set-content', sid, value or content(sid))
        self.apply('approve-content', sid, approval(self.store, sid, 'content'))
        self.apply('set-design', sid, design(sid, layout, image))
        self.apply('approve-design', sid, approval(self.store, sid, 'design'))

    def locked(self, sid='s1'):
        self.ready(sid)
        self.apply('build', sid)
        self.apply('review', sid, approval(self.store, sid, 'visual'))
        warnings = self.store.read()['slides'][sid]['build']['warnings']
        self.apply('lock', sid, approval(self.store, sid, 'preview', warnings))

    def source(self, sid='s1', *, private=False):
        path = self.repo / ('private/raw/fixture.txt' if private else 'projects/demo/material.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('source fixture')
        result = content(sid)
        result['sources'] = [{'id': 'src', 'path': path.relative_to(self.repo).as_posix(),
                              'sha256': ps.file_hash(path), 'locator': 'user supplied fixture, line 1',
                              'evidence_status': 'user_provided', 'sensitivity': 'private' if private else 'public'}]
        return path, result

    def test_init_schema_unknown_fields_and_duplicate_ids(self):
        state = self.store.read()
        self.assertEqual(state['session']['slide_order'], ['s1', 's2', 's3'])
        baseline = (self.store.root / 'current.json').read_bytes()
        for value in ({**content(), 'schema': 'future'}, {**content(), 'approved': True},
                      {**content(), 'body': []}, {**content(), 'title': ''}):
            with self.assertRaises(ps.StateError):
                self.apply('set-content', payload=value)
        with self.assertRaises(ps.StateError):
            self.apply('reorder', None, {'slide_order': ['s1', 's1', 's3']})
        file = self.repo / 'duplicate.json'
        file.write_text('{"slides":{"s1":{},"s1":{}}}')
        with self.assertRaises(ps.StateError):
            ps.load_json(file)
        self.assertEqual((self.store.root / 'current.json').read_bytes(), baseline)

    def test_ir_duplicate_bounds_and_dangling_connector(self):
        valid = render.compile_slide(content(), design(layout='flow'))
        render.validate_ir(valid)
        for mutate in ('duplicate', 'bounds', 'dangling', 'nan', 'code'):
            ir = deepcopy(valid)
            if mutate == 'duplicate':
                ir['objects'].append(deepcopy(ir['objects'][0]))
            elif mutate == 'bounds':
                ir['objects'][0]['bbox'][0] = 99
            elif mutate == 'dangling':
                ir['objects'][-1]['end'] = 'missing'
            elif mutate == 'nan':
                ir['objects'][0]['bbox'][0] = float('nan')
            else:
                ir['objects'][0]['code'] = 'run command'
            with self.assertRaises(ValueError):
                render.validate_ir(ir)

    def test_approval_order_declaration_and_snapshot(self):
        for op in ('build', 'approve-design', 'review', 'lock'):
            with self.assertRaises(ps.StateError):
                self.apply(op, payload={} if op == 'build' else {'actor': '', 'confirmation_source': '',
                    'snapshot_sha256': '0' * 64, 'accepted_warnings': []})
        self.apply('set-content', payload=content())
        for key in ('actor', 'confirmation_source', 'snapshot_sha256'):
            event = approval(self.store, 's1', 'content')
            event[key] = ''
            with self.assertRaises(ps.StateError):
                self.apply('approve-content', payload=event)
        self.apply('approve-content', payload=approval(self.store, 's1', 'content'))
        self.assertEqual(ps.stage(self.store.read(), 's1'), 'design_review')

    def test_content_and_source_change_invalidate_downstream(self):
        path, value = self.source()
        self.ready(value=value)
        self.apply('build')
        old = self.store.read()
        old_hash = old['slides']['s1']['content']['sources'][0]['sha256']
        path.write_text('changed external source')
        self.assertEqual(self.store.summary()['status'], 'blocked')
        with self.assertRaises(ps.StateError):
            self.apply('build')
        value['sources'][0]['sha256'] = ps.file_hash(path)
        self.apply('set-content', payload=value)
        current = self.store.read()
        self.assertEqual(ps.stage(current, 's1'), 'content_review')
        self.assertEqual(current['session']['approval_records']['s1'], {})
        self.assertNotIn('build', current['slides']['s1'])
        self.assertEqual((self.store.root / 'assets' / old_hash).read_text(), 'source fixture')

    def test_design_change_retains_only_content_approval(self):
        self.ready()
        self.apply('build')
        approved = deepcopy(self.store.read()['session']['approval_records']['s1']['content'])
        self.apply('set-design', payload=design(layout='flow'))
        state = self.store.read()
        self.assertEqual(state['session']['approval_records']['s1'], {'content': approved})
        self.assertNotIn('build', state['slides']['s1'])

    def test_third_slide_does_not_change_first_locked_snapshot(self):
        self.locked('s1')
        before = self.store.read()
        self.ready('s3', 'flow')
        self.apply('build', 's3')
        after = self.store.read()
        self.assertEqual(before['slides']['s1'], after['slides']['s1'])
        self.assertEqual(before['session']['approval_records']['s1'], after['session']['approval_records']['s1'])
        self.assertEqual(ps.stage(after, 's1'), 'locked')

    def test_locked_mutation_theme_reorder_rejected_and_explicit_unlock(self):
        self.locked()
        changed = deepcopy(self.store.read()['plan'])
        changed['theme']['font'] = 'Different font'
        for op, sid, value in [('set-content', 's1', content()), ('build', 's1', {}),
                               ('set-plan', None, changed), ('reorder', None, {'slide_order': ['s3','s2','s1']})]:
            with self.assertRaisesRegex(ps.StateError, 'locked slides'):
                self.apply(op, sid, value)
        self.apply('unlock', payload=approval(self.store, 's1', 'preview'))
        self.apply('set-plan', None, changed)
        state = self.store.read()
        self.assertEqual(ps.stage(state, 's1'), 'design_review')
        self.assertIn('content', state['session']['approval_records']['s1'])

    def test_stale_revision_and_two_process_concurrency(self):
        req = request(self.store, 'set-content', 's1', content())
        ctx = mp.get_context('spawn')
        gate, queue = ctx.Event(), ctx.Queue()
        workers = [ctx.Process(target=process_commit, args=(str(self.repo), str(self.store.root), req, queue, gate)) for _ in range(2)]
        for worker in workers:
            worker.start()
        gate.set()
        results = [queue.get(timeout=30) for _ in workers]
        for worker in workers:
            worker.join(30)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(r[0] for r in results), ['error', 'ok'])
        self.assertIn('Revision conflict', next(r[1] for r in results if r[0] == 'error'))
        with self.assertRaisesRegex(ps.StateError, 'Revision conflict'):
            self.store.apply(req)

    def test_process_death_before_and_after_atomic_pointer(self):
        ctx = mp.get_context('spawn')
        for checkpoint in ('file:session.json', 'file:deck_plan.json', 'file:content.json', 'file:design.json', 'file:ir.json', 'file:build.json', 'file:receipt.json',
                           'before_revision_rename', 'after_revision_rename', 'before_pointer_replace', 'after_pointer_replace'):
            with self.subTest(checkpoint=checkpoint):
                root = self.repo / 'projects/demo/presentations' / ('crash-' + checkpoint.replace(':', '-').replace('.', '-'))
                store = ps.Store(root, self.repo)
                store.initialize(ps.example_plan())
                store.apply(request(store, 'set-content', 's1', content()))
                store.apply(request(store, 'approve-content', 's1', approval(store, 's1', 'content')))
                store.apply(request(store, 'set-design', 's1', design()))
                store.apply(request(store, 'approve-design', 's1', approval(store, 's1', 'design')))
                store.apply(request(store, 'build', 's1', {}))
                before = store.read()['session']['revision_id']
                req = request(store, 'set-content', 's2', content('s2'))
                child = ctx.Process(target=process_commit, args=(str(self.repo), str(root), req, None, None, checkpoint))
                child.start()
                child.join(30)
                self.assertEqual(child.exitcode, 73)
                state = ps.Store(root, self.repo).read()
                if checkpoint == 'after_pointer_replace':
                    self.assertNotEqual(state['session']['revision_id'], before)
                    self.assertEqual(state['slides']['s2']['content'], content('s2'))
                else:
                    self.assertEqual(state['session']['revision_id'], before)
                    self.assertNotIn('content', state['slides']['s2'])
                    self.assertTrue(store.summary()['uncommitted_revisions'])
                # New safe commit after process death; flock is automatically released.
                store.apply(request(store, 'set-content', 's2', content('s2')))

    def test_cache_deletion_preserves_approval_preview_and_restart(self):
        self.locked()
        before = self.store.read()
        preview = Path(self.store.summary()['slides']['s1']['preview_path'])
        image_hash = ps.file_hash(preview)
        shutil.rmtree(self.repo / 'temp')
        restarted = ps.Store(self.store.root, self.repo)
        self.assertEqual(restarted.read(), before)
        self.assertEqual(ps.file_hash(preview), image_hash)
        self.assertEqual(restarted.summary()['slides']['s1']['stage'], 'locked')

    def test_external_artifact_and_receipt_corruption_is_blocking(self):
        self.locked()
        state = self.store.read()
        pptx = Path(self.store.summary()['slides']['s1']['single_slide_pptx'])
        old = pptx.read_bytes()
        pptx.write_bytes(b'external PowerPoint edit')
        with self.assertRaisesRegex(ps.StateError, 'externally modified'):
            self.store.read()
        pptx.write_bytes(old)
        receipt = self.store.root / 'revisions' / state['session']['revision_id'] / 'receipt.json'
        receipt.write_text('{}')
        with self.assertRaisesRegex(ps.StateError, 'Receipt integrity'):
            self.store.read()

    def test_private_sources_stay_local_and_no_false_fact_verification(self):
        path, value = self.source(private=True)
        self.apply('set-content', payload=value)
        self.assertEqual(self.store.summary()['source_policy'], 'local_only')
        bad = deepcopy(value)
        bad['sources'][0]['sensitivity'] = 'public'
        with self.assertRaises(ps.StateError):
            self.apply('set-content', payload=bad)
        bad = deepcopy(value)
        bad['sources'][0]['evidence_status'] = 'raw_verified'
        with self.assertRaises(ps.StateError):
            self.apply('set-content', payload=bad)
        self.assertEqual(path.read_text(), 'source fixture')

    def test_no_visual_or_user_approval_from_mechanical_pass(self):
        self.ready()
        self.apply('build')
        state = self.store.read()
        self.assertEqual(ps.stage(state, 's1'), 'preview_review')
        self.assertNotIn('visual', state['session']['approval_records']['s1'])
        with self.assertRaisesRegex(ps.StateError, 'inspect'):
            self.apply('lock', payload=approval(self.store, 's1', 'preview'))
        self.apply('review', payload=approval(self.store, 's1', 'visual'))
        with self.assertRaisesRegex(ps.StateError, 'warnings'):
            self.apply('lock', payload=approval(self.store, 's1', 'preview'))
        # No operation exists to accept a hand-written build/QA receipt.
        with self.assertRaisesRegex(ps.StateError, 'Unsupported operation'):
            self.apply('import-build', payload={'status': 'passed'})

    def test_failed_visual_review_blocks_user_lock_even_with_warning_acceptance(self):
        self.ready()
        self.apply('build')
        event = approval(self.store, 's1', 'visual')
        event.update(verdict='failed', findings=['TEST ONLY: title is clipped'])
        self.apply('review', payload=event)
        warnings = self.store.read()['slides']['s1']['build']['warnings']
        with self.assertRaisesRegex(ps.StateError, 'failed review blocks'):
            self.apply('lock', payload=approval(self.store, 's1', 'preview', warnings))
        self.assertEqual(self.store.summary()['slides']['s1']['visual_review']['verdict'], 'failed')

    def test_renderer_or_font_change_blocks_review_and_lock(self):
        self.ready()
        self.apply('build')
        with patch.object(render, 'fingerprint', return_value={'new_font': True}):
            self.assertEqual(self.store.summary()['status'], 'blocked')
            with self.assertRaisesRegex(ps.StateError, 'Stale renderer'):
                self.apply('review', payload=approval(self.store, 's1', 'visual'))

    def test_prepare_check_commit_same_validator_no_model(self):
        task = self.store.prepare('s1', 'content')
        self.assertEqual(task['schema'], 'agent-task-v1')
        self.assertEqual(set(task['commands']), {'check', 'commit', 'resume'})
        candidate = self.repo / task['outputs'][0]['path']
        req = ps.load_json(candidate)
        req['payload'] = content()
        baseline = (self.store.root / 'current.json').read_bytes()
        self.assertEqual(self.store.apply(req, dry_run=True)['status'], 'valid')
        self.assertEqual((self.store.root / 'current.json').read_bytes(), baseline)
        self.store.apply(req)
        self.assertEqual(self.store.read()['slides']['s1']['content'], content())
        for source in (Path(ps.__file__).read_text(), Path(render.__file__).read_text()):
            self.assertNotIn('import dsh', source)
            self.assertNotIn('llm_structured', source)
            self.assertNotIn('load_visual_env(', source)

    def test_symlink_external_store_sources_assets_and_cache_are_rejected(self):
        outside = self.repo / 'outside'
        outside.mkdir()
        link = self.repo / 'projects/demo/presentations/link'
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ps.StateError):
            ps.Store(link, self.repo)
        with self.assertRaises(ps.StateError):
            ps.Store(self.repo / 'academic/raw/presentations/deck', self.repo)
        material = self.repo / 'projects/demo/link.txt'
        material.symlink_to(outside / 'source.txt')
        (outside / 'source.txt').write_text('not allowed')
        value = content()
        value['sources'] = [{'id':'source', 'path':'projects/demo/link.txt', 'sha256':ps.file_hash(material),
            'locator':'fixture', 'sensitivity':'public', 'evidence_status':'user_provided'}]
        with self.assertRaises(ps.StateError):
            self.apply('set-content', payload=value)
        (self.repo / 'temp').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ps.StateError):
            self.store.prepare('s1', 'content')
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ['source.txt'])

    def test_locked_source_drift_requires_explicit_unlock_then_reapproval(self):
        path, value = self.source()
        self.ready(value=value)
        self.apply('build')
        self.apply('review', payload=approval(self.store, 's1', 'visual'))
        warnings = self.store.read()['slides']['s1']['build']['warnings']
        self.apply('lock', payload=approval(self.store, 's1', 'preview', warnings))
        path.write_text('external update')
        value['sources'][0]['sha256'] = ps.file_hash(path)
        with self.assertRaisesRegex(ps.StateError, 'locked slides'):
            self.apply('set-content', payload=value)
        self.apply('unlock', payload=approval(self.store, 's1', 'preview'))
        self.apply('set-content', payload=value)
        self.assertEqual(ps.stage(self.store.read(), 's1'), 'content_review')

    def test_real_cli_init_prepare_check_commit_status_and_invalid_json(self):
        scripts = self.repo / '.scripts'
        scripts.mkdir()
        for name in ('presentation_state.py', 'presentation_render.py', 'agent_task.py'):
            shutil.copyfile(REPO / '.scripts' / name, scripts / name)
        def cli(*args, expected=0):
            proc = subprocess.run([sys.executable, '-B', str(scripts / 'presentation_state.py'), *args],
                                  cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(proc.returncode, expected, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)
        plan = self.repo / 'plan.json'
        plan.write_bytes(ps.json_bytes(cli('example-plan')))
        created = cli('init', '--project', 'projects/demo', '--plan', str(plan))
        root = created['store']
        task = cli('prepare', '--store', root, '--slide', 's1', '--kind', 'content')
        file = self.repo / task['outputs'][0]['path']
        candidate = ps.load_json(file)
        candidate['payload'] = content()
        file.write_bytes(ps.json_bytes(candidate))
        cli('check', '--store', root, '--request', str(file))
        cli('commit', '--store', root, '--request', str(file))
        state = cli('status', '--store', root)
        self.assertEqual(state['slides']['s1']['stage'], 'content_review')
        cli('commit', '--store', root, '--request', str(file), expected=1)
        file.write_text('{"schema":"presentation-v1","schema":"duplicate"}')
        invalid = cli('check', '--store', root, '--request', str(file), expected=1)
        self.assertIn('Duplicate JSON key', invalid['error'])

    def test_build_failure_preserves_current_and_no_approval(self):
        self.ready()
        pointer = (self.store.root / 'current.json').read_bytes()
        with patch.object(render, 'render', side_effect=RuntimeError('render failed')):
            with self.assertRaisesRegex(RuntimeError, 'render failed'):
                self.apply('build')
        self.assertEqual((self.store.root / 'current.json').read_bytes(), pointer)
        self.assertEqual(ps.stage(self.store.read(), 's1'), 'build_ready')

    def test_asset_directory_symlink_cannot_redirect_snapshot_writes(self):
        outside = self.repo / 'outside-assets'
        outside.mkdir()
        (self.store.root / 'assets').symlink_to(outside, target_is_directory=True)
        _, value = self.source()
        with self.assertRaisesRegex(ps.StateError, 'Symlink'):
            self.apply('set-content', payload=value)
        self.assertEqual(list(outside.iterdir()), [])

    def test_bad_source_hash_does_not_write_snapshot(self):
        path, value = self.source()
        value['sources'][0]['sha256'] = '0' * 64
        with self.assertRaises(ps.StateError):
            self.apply('set-content', payload=value)
        self.assertFalse((self.store.root / 'assets').exists())


class RealRenderTests(unittest.TestCase):
    def test_three_layouts_real_render_without_fabricated_user_acceptance(self):
        from PIL import Image, ImageDraw
        # Test-only project lives in an isolated repository root, not active user projects.
        with tempfile.TemporaryDirectory(prefix='presentation-real-') as temp:
            repo = Path(temp).resolve()
            project = repo / 'projects/fixture'
            project.mkdir(parents=True)
            image = Image.new('RGB', (640, 420), '#E0EBFA')
            draw = ImageDraw.Draw(image)
            draw.rectangle((70, 80, 250, 300), fill='#3265A8')
            draw.ellipse((350, 110, 550, 310), fill='#748CA8')
            material = project / 'synthetic.png'
            image.save(material)
            store = ps.Store(project / 'presentations/real-fixture', repo)
            store.initialize(ps.example_plan())
            for sid, layout in [('s1', 'title'), ('s2', 'image_text'), ('s3', 'flow')]:
                c = content(sid, '合成测试 · ' + layout)
                c['body'] = ['Knowledge 知识', 'Evidence 证据']
                if layout == 'image_text':
                    c['sources'] = [{'id': 'image', 'path': material.relative_to(repo).as_posix(),
                        'sha256': ps.file_hash(material), 'locator': 'synthetic fixture, no scientific evidence',
                        'evidence_status': 'user_provided', 'sensitivity': 'public'}]
                for op, payload in [('set-content', c)]:
                    store.apply(request(store, op, sid, payload))
                store.apply(request(store, 'approve-content', sid, approval(store, sid, 'content')))
                store.apply(request(store, 'set-design', sid, design(sid, layout, 'image' if layout == 'image_text' else None)))
                store.apply(request(store, 'approve-design', sid, approval(store, sid, 'design')))
                store.apply(request(store, 'build', sid, {}))
            state = store.read()
            status = store.summary()
            self.assertEqual(status['status'], 'ready')
            self.assertTrue(all(p['stage'] == 'preview_review' for p in status['slides'].values()))
            self.assertEqual(state['slides']['s2']['build']['native_objects']['image'], 1)
            self.assertEqual(state['slides']['s3']['build']['native_objects']['connector'], 1)
            for slide in state['slides'].values():
                self.assertEqual(slide['build']['remote_calls'], 0)
                self.assertEqual(slide['build']['user_approval'], 'not_requested')
            # Keep only labeled non-factual previews/receipts for the host's actual visual review.
            output = REPO / 'temp/presentation-runtime'
            output.mkdir(parents=True, exist_ok=True)
            saved = Path(tempfile.mkdtemp(prefix='three-layouts-', dir=output))
            for sid, data in status['slides'].items():
                shutil.copyfile(data['preview_path'], saved / f'{sid}.png')
                shutil.copyfile(data['single_slide_pptx'], saved / f'{sid}.pptx')
                (saved / f'{sid}-build.json').write_bytes(ps.json_bytes(state['slides'][sid]['build']))
            (saved / 'README.txt').write_text(
                'SYNTHETIC TEST ONLY. Content/design confirmations are test fixtures. '
                'No real user preview acceptance or final assembly.\n',
                encoding='utf-8',
            )
            print(f'Actual three-layout previews: {saved}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--render', action='store_true')
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(StateTests)
    if args.render:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RealRenderTests))
    else:
        print('Real rendering NOT executed; pass --render to require the local dependency chain.')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

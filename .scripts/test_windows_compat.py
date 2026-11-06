#!/usr/bin/env python3
"""Real-process portability regressions; all writable fixtures are temporary."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import platform_compat as pc

SCRIPTS = Path(__file__).resolve().parent
RENDER = '--render' in sys.argv
if RENDER:
    sys.argv.remove('--render')

WORKER = '''
import os, sys
sys.path.insert(0, sys.argv[1])
from platform_compat import fcntl, safe_open
fd = safe_open(sys.argv[2], os.O_CREAT | os.O_RDWR)
kind, wait = sys.argv[3:5]
mode = fcntl.LOCK_SH if kind == 'shared' else fcntl.LOCK_EX
if wait == 'try': mode |= fcntl.LOCK_NB
print('ready', flush=True)
try:
    fcntl.flock(fd, mode)
except BlockingIOError:
    print('busy', flush=True)
else:
    print('acquired', flush=True)
    if wait == 'hold': sys.stdin.read(1)
    fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
'''


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / '中文 空间'
        self.root.mkdir()

    def worker(self, kind='exclusive', wait='hold'):
        proc = subprocess.Popen([sys.executable, '-c', WORKER, str(SCRIPTS),
                                 str(self.root / '互斥.lock'), kind, wait],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', env=pc.python_env())
        def cleanup():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
        self.addCleanup(cleanup)
        lines = queue.Queue()
        def reader():
            for line in proc.stdout:
                lines.put(line.strip())
            lines.put('<EOF>')
        threading.Thread(target=reader, daemon=True).start()
        self.assertEqual(lines.get(timeout=10), 'ready')
        return proc, lines

    def test_exclusive_contention_and_blocking_resume(self):
        holder, lines = self.worker()
        self.assertEqual(lines.get(timeout=10), 'acquired')
        busy, lines = self.worker(wait='try')
        self.assertEqual(lines.get(timeout=10), 'busy')
        self.assertEqual(busy.wait(timeout=10), 0)
        waiter, waiting = self.worker(wait='wait')
        with self.assertRaises(queue.Empty):
            waiting.get(timeout=.25)
        holder.stdin.write('x'); holder.stdin.flush()
        self.assertEqual(holder.wait(timeout=10), 0)
        self.assertEqual(waiting.get(timeout=10), 'acquired')
        self.assertEqual(waiter.wait(timeout=10), 0)

    def test_shared_locks_and_crash_release(self):
        first, lines = self.worker(kind='shared')
        self.assertEqual(lines.get(timeout=10), 'acquired')
        second, lines = self.worker(kind='shared')
        self.assertEqual(lines.get(timeout=10), 'acquired')
        busy, lines = self.worker(wait='try')
        self.assertEqual(lines.get(timeout=10), 'busy')
        self.assertEqual(busy.wait(timeout=10), 0)
        first.kill(); first.wait(timeout=10)
        second.kill(); second.wait(timeout=10)
        fresh, lines = self.worker(wait='try')
        self.assertEqual(lines.get(timeout=10), 'acquired')
        self.assertEqual(fresh.wait(timeout=10), 0)

    def test_win32_errors_are_not_retried_or_hidden(self):
        api = pc._WindowsAPI.__new__(pc._WindowsAPI)
        api.Overlapped = lambda: object()
        api.kernel = SimpleNamespace(LockFileEx=lambda *a: False, UnlockFileEx=lambda *a: False)
        with patch.dict(sys.modules, {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: fd)}):
            for error in (5, 6, 33):
                api.ctypes = SimpleNamespace(byref=lambda x: x, get_last_error=lambda: error,
                    WinError=lambda code: OSError(code, 'native error'))
                with self.assertRaises(BlockingIOError if error == 33 else OSError) as raised:
                    api.lock(1, exclusive=True, nonblocking=True)
                if error != 33:
                    self.assertNotIsInstance(raised.exception, BlockingIOError)
                with self.assertRaises(OSError):
                    api.lock(1, exclusive=True, nonblocking=False, unlock=True)

    def test_safe_file_open_and_file_sync(self):
        path = self.root / '文件.txt'
        fd = pc.safe_open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, '中文'.encode()); os.close(fd)
        with self.assertRaises(OSError):
            pc.safe_open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        fd = pc.safe_open(path)
        self.assertEqual(os.read(fd, 100), '中文'.encode()); os.close(fd)
        with self.assertRaises((OSError, ValueError)):
            pc.safe_open(self.root)
        self.assertTrue(pc.fsync_path(path))
        self.assertEqual(pc.fsync_path(self.root), not pc.IS_WINDOWS)
        with patch.object(pc.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                pc.fsync_path(path)

    def test_final_symlink_is_rejected(self):
        target = self.root / 'target'; target.write_bytes(b'original')
        link = self.root / 'link'
        try:
            link.symlink_to(target)
        except OSError as exc:
            if pc.IS_WINDOWS and getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Windows host lacks symlink privilege; junction safety is independently required')
            raise
        with self.assertRaises((ValueError, OSError)):
            pc.safe_open(link, os.O_RDWR)
        self.assertEqual(target.read_bytes(), b'original')

    @unittest.skipUnless(pc.IS_WINDOWS, 'Windows native reparse-point test')
    def test_windows_junction_and_device_paths_are_rejected(self):
        target = self.root / 'target'; target.mkdir()
        (target / 'file').write_bytes(b'original')
        junction = self.root / 'junction'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.addCleanup(lambda: junction.rmdir() if junction.exists() else None)
        with self.assertRaises(ValueError):
            pc.safe_open(junction / 'file', os.O_RDWR)
        for path in (r'\\.\NUL', r'\\?\C:\Windows\file', str(target / 'file') + ':stream', self.root / 'NUL'):
            with self.assertRaises((OSError, ValueError)):
                pc.safe_open(path)

    def test_protocol_paths_normalize_without_accepting_escape(self):
        self.assertEqual(pc.relative_protocol_path(r'temp\任务\result.json'), 'temp/任务/result.json')
        for value in ('', '.', '/', '../bad', r'a\..\b', 'C:foo', r'C:\foo',
                      r'\\host\share\x', r'\\?\C:\x', r'\rooted', 'x:stream'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pc.relative_protocol_path(value)
        import agent_task
        folder = self.root / 'temp' / '任务'; folder.mkdir(parents=True)
        expected = folder / '结果.json'; expected.write_bytes(b'{}')
        self.assertEqual(agent_task.resolve_temp_artifact(self.root, r'temp\任务\结果.json', '任务'), expected)
        self.assertEqual(agent_task._relative_path(r'temp\任务\结果.json'), 'temp/任务/结果.json')

    def test_utf8_overrides_legacy_child_encoding_and_preserves_bytes(self):
        env = dict(os.environ, PYTHONIOENCODING='cp936', PYTHONUTF8='0')
        payload = '中文与合法替换字符\ufffd'
        code = 'import sys; print(sys.stdin.read(),end=""); print("诊断",file=sys.stderr)'
        result = pc.run_python([sys.executable, '-c', code], input=payload, capture_output=True, env=env)
        self.assertEqual(result.stdout, payload); self.assertEqual(result.stderr.strip(), '诊断')
        code = 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'
        result = pc.run_python([sys.executable, '-c', code], input=payload.encode(), capture_output=True, env=env)
        self.assertEqual(result.stdout, payload)
        with self.assertRaises(UnicodeDecodeError):
            pc.run_python([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(bytes([255]))'], capture_output=True)
        self.assertEqual(env['PYTHONIOENCODING'], 'cp936')

    def test_wg_and_agent_boundaries_override_cp936(self):
        env = dict(os.environ, PYTHONIOENCODING='cp936', PYTHONUTF8='0')
        # The outer WG process starts under CP936; its machine output still is UTF-8.
        result = subprocess.run([sys.executable, str(SCRIPTS / 'wg.py'), 'functions', 'show',
                                 '不存在的功能'], capture_output=True, env=env, timeout=30)
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertIn('不存在的功能', json.dumps(payload, ensure_ascii=False))
        import agent_task
        scripts = self.root / '.scripts'; scripts.mkdir()
        (scripts / 'echo.py').write_text('print("中文与\\ufffd")', encoding='utf-8')
        task = agent_task.make_task(kind='test', transaction_id='test',
            inputs=[{'name':'in', 'path':'in.md'}], outputs=[{'name':'out', 'path':'temp/out.json'}],
            protocol={'name':'test'}, commands={'check':'python3 .scripts/echo.py'})
        with patch.dict(os.environ, env, clear=True):
            result = agent_task.run_action({'agent_task': task}, 'check', self.root)
        self.assertEqual(result['returncode'], 0, result['stderr'])
        self.assertEqual(result['stdout'].strip(), '中文与\ufffd')

    def test_module_imports_without_platform_import_failures(self):
        names = ['oral_exam', 'presentation_state', 'review', 'science_exhibit', 'slide_library',
                 'relativize_internal_symlinks', 'paper_impact', 'citing_papers', 'graph_lib',
                 'source_ingest', 'knowledge_store']
        result = pc.run_python([sys.executable, '-c',
            'import sys, importlib; sys.path.insert(0,sys.argv[1]); [importlib.import_module(n) for n in sys.argv[2:]]',
            str(SCRIPTS), *names], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_ingest_preserves_structured_diagnostic(self):
        import ingest_inbox
        payload = {'status': 'failed', 'errors': ['视觉服务超时', '第二原因'], 'retryable': True,
                   'transaction_id': 'test-only'}
        result = ingest_inbox._result_entry('test.pptx', 'document', payload, json.dumps(payload, indent=2))
        self.assertEqual(result['reason'], '视觉服务超时')
        self.assertEqual(result['errors'], payload['errors']); self.assertTrue(result['retryable'])
        result = ingest_inbox._result_entry('x', 'document', {'status': 'failed', 'error': '原因'}, '{')
        self.assertEqual(result['reason'], '原因')

    def test_custom_visual_endpoint_and_windows_soffice(self):
        import visual_qa as qa
        for base, path, expected in [
            ('https://example.invalid', '', 'https://example.invalid/v1/chat/completions'),
            ('https://example.invalid/api/v4/', '', 'https://example.invalid/api/v4/chat/completions'),
            ('https://example.invalid/v1/chat/completions', '', 'https://example.invalid/v1/chat/completions'),
            ('https://example.invalid/proxy', '/custom/chat', 'https://example.invalid/proxy/custom/chat'),
        ]:
            self.assertEqual(qa._chat_completions_url(base, path), expected)
        with self.assertRaises(ValueError):
            qa._chat_completions_url('https://example.invalid', '//other.invalid/chat')
        office = self.root / 'Program Files' / 'LibreOffice' / 'program' / 'soffice.exe'
        office.parent.mkdir(parents=True); office.write_bytes(b'test only'); office.chmod(0o700)
        with patch.dict(os.environ, {'ProgramFiles': str(self.root / 'Program Files')}, clear=True), \
                patch.object(qa.shutil, 'which', return_value=None):
            self.assertEqual(qa._find_soffice(), office)

    def test_content_signals_are_advisory_and_do_not_claim_omissions(self):
        import pptx_visual
        page = {'objects': [{'kind': 'pic', 'text': '', 'bbox_normalized': [0, 0, 1, 1]}]}
        self.assertEqual(pptx_visual.content_review_signals(page, '')[0]['issue'], 'pptx_sparse_content')
        self.assertEqual(pptx_visual.content_review_signals(page, '充分识读补充' * 20), [])
        self.assertEqual(pptx_visual.content_review_signals({'objects': []}, ''), [])
        page['objects'][0]['bbox_normalized'] = [0, 0, .1, .1]
        self.assertEqual(pptx_visual.content_review_signals(page, ''), [])
        page['objects'][0]['styles'] = [{'fonts': {'latin': {'declared': 'Symbol', 'rendered': None}}}]
        self.assertEqual(pptx_visual.content_review_signals(page, '')[0]['issue'], 'pptx_symbol_font')

    @unittest.skipUnless(RENDER, 'pass --render to require installed LibreOffice and actual rendering')
    def test_actual_render_with_image_replacement_and_chinese_paths(self):
        from PIL import Image
        from pptx import Presentation
        from pptx.util import Inches
        import fitz
        import slide_reuse
        import visual_qa
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        old = self.root / '原图.png'; new = self.root / '替换.png'
        Image.new('RGB', (300, 200), 'red').save(old)
        Image.new('RGB', (1500, 1000), 'blue').save(new)
        pic = slide.shapes.add_picture(str(old), Inches(1), Inches(1), Inches(6), Inches(4))
        source = self.root / '中文 原稿.pptx'; output = self.root / '中文 替换.pptx'
        deck.save(source)
        profile = {'schema': slide_reuse.PROFILE_SCHEMA, 'kind': 'adaptable',
                   'use_when': ['TEST-ONLY'], 'constraints': ['TEST-ONLY'], 'fixed_objects': [],
                   'slots': [{'id': 'figure', 'page': 1, 'object_id': str(pic.shape_id), 'type': 'image',
                              'label': '合成图', 'guidance': 'TEST-ONLY', 'placeholder': '图'}]}
        slide_reuse.transform(source, output, profile, {'figure': str(new)})
        with zipfile.ZipFile(output) as package:
            self.assertTrue(all('\\' not in name for name in package.namelist()))
            image = Image.open(io.BytesIO(package.read('ppt/media/reuse-1-figure.png'))).convert('RGB')
            self.assertEqual(image.getpixel((image.width // 2, image.height // 2)), (0, 0, 255))
        pdf = self.root / '中文 预览.pdf'
        visual_qa.convert_slides_to_pdf(output, pdf)
        with fitz.open(pdf) as document:
            self.assertEqual(len(document), 1)
            pixels = document[0].get_pixmap()
            # The substantial blue image must survive the real Office rendering pipeline.
            rendered = Image.frombytes('RGB', [pixels.width, pixels.height], pixels.samples)
            blue = sum(b > 200 and r < 40 and g < 40 for r, g, b in (rendered.get_flattened_data() if hasattr(rendered, 'get_flattened_data') else rendered.getdata()))
            self.assertGreater(blue / (rendered.width * rendered.height), .1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

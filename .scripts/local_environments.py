#!/usr/bin/env python3
"""Keep Python runtime files local while preserving repository entry points."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

NAMES = {'.venv', '.venv-docling'}
MANIFEST = Path('.project/runtime-environments.json')
SCHEMA = 'wikigraph-local-environments-v1'
ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+')


def descriptors(root):
    """Read only the small in-workspace manifest, never a linked runtime."""
    path = Path(root) / ("project-config/runtime-environments.json" if (Path(root) / "system-tools").is_dir() else MANIFEST)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('Runtime manifest must not be a symlink')
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 256_000:
        raise ValueError('Invalid runtime manifest file')
    doc = json.loads(path.read_text())
    entries = doc.get('environments')
    if doc.get('schema') != SCHEMA or not isinstance(entries, dict) or not set(entries) <= NAMES:
        raise ValueError('Invalid runtime manifest schema')
    for name, entry in entries.items():
        if (not isinstance(entry, dict)
                or not isinstance(entry.get('python'), str)
                or not re.fullmatch(r'3\.\d+\.\d+', entry['python'])
                or not isinstance(entry.get('packages'), list)
                or not all(isinstance(p, str) and PIN.fullmatch(p) for p in entry['packages'])):
            raise ValueError('Invalid runtime dependency specification: ' + name)
    return entries


def registered(root, name):
    return descriptors(root).get(name) if name in NAMES else None


def swap(left, right):
    """Exchange the directory and its replacement link in one macOS syscall."""
    if sys.platform != 'darwin':
        raise RuntimeError('Atomic environment relocation currently supports macOS only')
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renamex_np
    fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(os.fsencode(left), os.fsencode(right), 2) != 0:  # RENAME_SWAP
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def locations(root, name, store):
    import workspace_checkpoint as cp
    root = cp.reject_link_components(Path(root).expanduser().absolute())
    if name not in NAMES or not (root / '.scripts').is_dir():
        raise ValueError('Choose a named runtime in a WikiGraph workspace')
    store = cp.outside(Path(store).expanduser(), root)
    key = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return root, root / name, store / ('wikigraph-' + key) / name


def smoke(python):
    return json.loads(subprocess.check_output([
        str(python), '-c',
        'import json,sys; print(json.dumps({"prefix":sys.prefix,"version":list(sys.version_info[:3])}))'
    ], text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}))


def externalize(root, name, store):
    import workspace_checkpoint as cp
    root, source, target = locations(root, name, store)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('Source must be an in-tree runtime directory')
    cfg = source / 'pyvenv.cfg'
    if cfg.is_symlink() or not cfg.is_file():
        raise ValueError('Source is not a verified Python virtual environment')
    if os.path.lexists(target):
        raise FileExistsError('External target already exists')
    if (root / '.git').exists() and subprocess.check_output([
            'git', '-C', str(root), 'ls-files', '-z', '--', name]):
        raise ValueError('Tracked runtime files require a separate migration plan')
    before_files, _ = cp.inventory(source, filter_runtime=False)
    before = smoke(source / 'bin/python')
    entry = cp.environment_inventory(root, [name])[name]
    entries = descriptors(root)
    entries[name] = entry
    # Publish the rebuild information first; a failed swap leaves the directory intact.
    manifest = root / ("project-config/runtime-environments.json" if (root / "system-tools").is_dir() else MANIFEST)
    cp.reject_link_components(manifest.parent)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    cp.write_json(manifest, {'schema': SCHEMA, 'environments': entries})
    if before_files != cp.inventory(source, filter_runtime=False)[0]:
        raise ValueError('Runtime changed while recording dependencies; original retained')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if source.stat().st_dev != target.parent.stat().st_dev:
        raise OSError(errno.EXDEV, 'Atomic relocation requires the same filesystem')
    os.symlink(str(target), target)  # Reserved self-link becomes the source entry after swap.
    try:
        swap(source, target)
    except Exception:
        if target.is_symlink() and os.readlink(target) == str(target):
            target.unlink()
        raise
    try:
        after = smoke(source / 'bin/python')
        if before['version'] != after['version'] or Path(after['prefix']).resolve() != target:
            raise ValueError('Interpreter identity changed after relocation')
        if before_files != cp.inventory(target, filter_runtime=False)[0]:
            raise ValueError('Runtime files changed during relocation')
        cp.write_json(target.with_name(name + '.placement.json'), {
            'source': str(source), 'external': str(target), 'kind': 'externalized'})
    except Exception:
        swap(source, target)
        target.unlink()  # Our reserved self-link; runtime contents are back at source.
        raise
    return {'name': name, 'source': str(source), 'external': str(target),
            'python': entry['python'], 'packages': len(entry['packages']),
            'verified': True, 'rollback': 'restore-in-tree ' + name}


def restore_in_tree(root, name):
    import workspace_checkpoint as cp
    root = cp.reject_link_components(Path(root).expanduser().absolute())
    if name not in NAMES or not registered(root, name):
        raise ValueError('Runtime is not registered')
    source = root / name
    if not source.is_symlink():
        raise ValueError('Runtime entry must be a link')
    link = Path(os.readlink(source))
    target = cp.outside(link if link.is_absolute() else source.parent / link, root)
    if not target.is_dir() or not (target / 'pyvenv.cfg').is_file():
        raise ValueError('External runtime is missing')
    receipt = cp.reject_link_components(target.with_name(name + '.placement.json'))
    if json.loads(receipt.read_text()) != {
            'source': str(source), 'external': str(target), 'kind': 'externalized'}:
        raise ValueError('Only an original atomically externalized runtime can use reverse swap')
    before = smoke(source / 'bin/python')
    swap(source, target)
    try:
        after = smoke(source / 'bin/python')
        if after['version'] != before['version'] or Path(after['prefix']).resolve() != source:
            raise ValueError('Interpreter changed during rollback')
    except Exception:
        swap(source, target)
        raise
    target.unlink()  # Exchanged link, never the environment directory.
    return {'name': name, 'restored': str(source), 'verified': True}


def prepare(root, name, store, python=None):
    """Build a new local runtime from portable pins; never replace an existing entry."""
    import workspace_checkpoint as cp
    root, source, target = locations(root, name, store)
    entry = registered(root, name)
    if not entry:
        raise ValueError('No saved dependency manifest; externalize/export the source environment first')
    if os.path.lexists(source) or os.path.lexists(target):
        raise FileExistsError('Runtime entry and external target must both be absent')
    uv = shutil.which('uv')
    if not uv:
        raise RuntimeError('Install uv before rebuilding a runtime')
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reserve the target against concurrent preparations; uv explicitly accepts our empty directory.
    target.mkdir(mode=0o700)
    subprocess.run([uv, 'venv', '--allow-existing', '--python', python or entry['python'], str(target)], check=True)
    interpreter = target / 'bin/python'
    pins = [p for p in entry['packages'] if p.split('==')[0].lower().replace('_', '-') != 'ran-asks']
    if pins:
        with tempfile.TemporaryDirectory(prefix='wg-environment-pins-') as tmp:
            requirements = Path(tmp) / 'requirements.txt'
            requirements.write_text('\n'.join(pins) + '\n')
            subprocess.run([uv, 'pip', 'install', '--python', str(interpreter), '-r', str(requirements)], check=True)
    if len(pins) != len(entry['packages']):
        subprocess.run([uv, 'pip', 'install', '--python', str(interpreter), '--no-deps', '-e', str(root)], check=True)
    result = smoke(interpreter)
    if result['version'][:2] != [int(x) for x in entry['python'].split('.')[:2]]:
        raise ValueError('Rebuilt interpreter has a different Python major/minor version')
    cp.write_json(target.with_name(name + '.placement.json'), {
        'source': str(source), 'external': str(target), 'kind': 'prepared'})
    os.symlink(str(target), source)  # Exclusive publish only after a working interpreter/install.
    return {'name': name, 'external': str(target), 'verified': True, 'python': result['version']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default=str(ROOT))
    p.add_argument('--store', default=str(Path.home() / 'WikiGraphData/environments'))
    sub = p.add_subparsers(dest='action', required=True)
    for action in ['externalize', 'restore-in-tree', 'prepare']:
        command = sub.add_parser(action)
        command.add_argument('name', choices=sorted(NAMES))
        if action == 'prepare':
            command.add_argument('--python', help='Explicit available interpreter (same major/minor required)')
    args = p.parse_args()
    if args.action == 'externalize':
        result = externalize(args.root, args.name, args.store)
    elif args.action == 'prepare':
        result = prepare(args.root, args.name, args.store, args.python)
    else:
        result = restore_in_tree(args.root, args.name)
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

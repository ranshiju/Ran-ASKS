#!/usr/bin/env python3
"""Discover trusted local packages as data; execute only an explicit selection."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
ID = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')


def confined(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('package paths must be nonempty relative paths')
    path = root / relative
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError('package symlinks are not supported')
        cursor = cursor.parent
    path.resolve().relative_to(root.resolve())
    if not path.is_file():
        raise ValueError(f'package file missing: {relative}')
    return path


def package(identifier: str, repo: Path = REPO) -> dict:
    if not ID.fullmatch(identifier):
        raise ValueError('invalid extension id')
    private = repo / 'private'
    base = private / 'extensions'
    root = base / identifier
    if any(p.is_symlink() for p in (private, base, root)):
        raise ValueError('extension roots must not be symlinks')
    manifest = confined(root, 'extension.json')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('schema') != 'local-extension-v1' or data.get('id') != identifier:
        raise ValueError('invalid extension manifest schema/id')
    for field in ('title', 'summary', 'version'):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f'missing extension {field}')
    entry = confined(root, data.get('entry'))
    guide = confined(root, data.get('guide'))
    if entry.suffix != '.py':
        raise ValueError('only explicitly installed Python entries are supported')
    if data.get('backend') != 'agent' or data.get('visibility') != 'private':
        raise ValueError('local extensions require private visibility and agent backend')
    return {**data, 'entry_path': str(entry), 'guide_path': str(guide)}


def discover(repo: Path = REPO) -> list[dict]:
    base = repo / 'private' / 'extensions'
    if not base.exists():
        return []
    if base.is_symlink() or base.parent.is_symlink():
        raise ValueError('extension roots must not be symlinks')
    return [package(p.name, repo) for p in sorted(base.iterdir()) if (p / 'extension.json').exists()]


def dispatch(identifier: str, arguments: list[str], repo: Path = REPO) -> int:
    selected = package(identifier, repo)
    args = arguments[1:] if arguments[:1] == ['--'] else arguments
    # The installed program is trusted code, not a sandbox. Never use shell=True.
    return subprocess.run([sys.executable, selected['entry_path'], *args], cwd=repo).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Read installed manifests without importing code')
    show = sub.add_parser('show', help='Read one installed manifest and guide location')
    show.add_argument('id')
    run = sub.add_parser('run', help='Explicitly run installed trusted code with host permissions')
    run.add_argument('id')
    run.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return dispatch(args.id, args.arguments)
        result = discover() if args.command == 'list' else package(args.id)
        print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

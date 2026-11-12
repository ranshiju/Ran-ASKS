"""Resolve explicitly recorded project moves without rewriting frozen artifacts.

The optional projects/.path-migrations.json is private workspace configuration.
Absent maps preserve the original layout. All returned paths remain under the
same projects root; callers still enforce their own schemas and artifact hashes.
"""
from pathlib import Path
import json


def _parts(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Invalid project relocation path')
    p = Path(value)
    if p.is_absolute() or '..' in p.parts or not p.parts or any(x.startswith('.') for x in p.parts):
        raise ValueError('Project relocation must use visible relative project paths')
    return p


def _no_symlinks(base, relative):
    cursor = base
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError('Project relocation cannot traverse symlinks')


def project_path(projects, relative):
    """Return an absolute project path, honoring an explicit missing-root move."""
    base = Path(projects).absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in str(relative):
        raise ValueError('Project path must remain relative to projects')
    config = base / '.path-migrations.json'
    if not config.exists() and not config.is_symlink():
        return base / rel
    if config.is_symlink() or config.stat().st_size > 65536:
        raise ValueError('Invalid project relocation configuration')
    data = json.loads(config.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'schema', 'moves'} or data['schema'] != 'project-path-migrations-v1':
        raise ValueError('Invalid project relocation schema')
    moves = data['moves']
    if not isinstance(moves, dict) or len(moves) > 256:
        raise ValueError('Invalid project relocation map')
    pairs = [(_parts(old), _parts(new)) for old, new in moves.items()]
    for old, new in pairs:
        if old == new or new.is_relative_to(old):
            raise ValueError('Project relocation cannot point into its old root')
        for other, _ in pairs:
            if other != old and (old.is_relative_to(other) or other.is_relative_to(old)):
                raise ValueError('Overlapping project relocation rules')
            if new.is_relative_to(other):
                raise ValueError('Chained project relocation rules')
    for old, new in pairs:
        if not rel.is_relative_to(old):
            continue
        _no_symlinks(base, rel)
        if (base / old).exists():
            return base / rel
        target = new / rel.relative_to(old)
        _no_symlinks(base, target)
        if not (base / new).is_dir():
            raise ValueError('Project relocation target is missing')
        return base / target
    return base / rel


def repo_path(repo, relative):
    """Return an absolute repository path; relocate only explicit projects paths."""
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or '\\' in str(relative):
        raise ValueError('Repository-relative path required')
    if rel.parts and rel.parts[0] == 'projects':
        return project_path(Path(repo) / 'projects', Path(*rel.parts[1:]))
    return Path(repo) / rel

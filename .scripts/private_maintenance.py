#!/usr/bin/env python3
"""Hash-gated private legacy Hub retirement and mixed-cache quarantine; no Raw changes."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import uuid
from pathlib import Path

import graph_lib as gl


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_receipt(path, receipt):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _target(hub):
    path = Path(hub)
    if path.is_absolute():
        path = path.relative_to(gl.REPO)
    if not str(path).endswith('.md'):
        path = Path(str(path) + '.md')
    target = gl.REPO / path
    root = gl.REPO / 'private'
    if (path.parts[:3] != ('private', 'wiki', 'hubs') or '..' in path.parts
            or target.is_symlink() or not target.resolve().is_relative_to(root.resolve())):
        raise ValueError('target must be a physical private/wiki/hubs Markdown file')
    gl.validate_graph_target(path.as_posix(), root / 'graph.db')
    return target, path.as_posix().removesuffix('.md'), root


def plan_retirement(hub):
    target, node, root = _target(hub)
    text = target.read_text()
    blockers = []
    if ('arXiv 物理分类研究方向' not in text or 'hub_subtype: research-direction' not in text
            or re.search(r'^## Scope\s*$', text, re.M)):
        blockers.append('not an unconverted legacy arXiv-template Hub')
    fm = gl.read_frontmatter(target)
    if gl.parse_list_field(fm, 'sources'):
        blockers.append('Hub has Raw sources')
    conn = gl.connect(root / 'graph.db', read_only=True)
    try:
        row = conn.execute('SELECT * FROM nodes WHERE path=?', (node,)).fetchone()
        if not row or row['type'] != 'hub':
            blockers.append('missing Hub node or wrong node type')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checks = {
            'edges': ['subject', 'object'], 'temporal_facts': ['subject', 'object'],
            'edge_origins': ['origin_page'], 'edge_evidence': ['source'],
            'node_origins': ['node_path', 'origin_page'],
            'node_glosses': ['node_path', 'origin_page'],
            'node_description_reviews': ['node_path'], 'hub_scope_history': ['hub_path'],
        }
        for table, columns in checks.items():
            if table not in tables:
                continue
            present = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            columns = [col for col in columns if col in present]
            if not columns:
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE ' +
                                 ' OR '.join(f'{col}=?' for col in columns),
                                 [node] * len(columns)).fetchone()[0]
            if count:
                blockers.append(f'{table}: {count} references')
        aliases = [list(r) for r in conn.execute(
            'SELECT alias,node_path FROM aliases WHERE node_path=? ORDER BY alias', (node,))]
    finally:
        conn.close()
    names = {node, node + '.md', Path(node).name, str(fm.get('title', '')),
             'hubs/' + Path(node).name, 'wiki/hubs/' + Path(node).name}
    names.update(a[0] for a in aliases)
    references = []
    for folder in (root / 'wiki', root / 'topics'):
        for page in folder.rglob('*.md'):
            if page == target or page == root / 'wiki/log.md':
                continue  # audit history must not block retirement
            content = page.read_text()
            links = re.findall(r'\[\[([^\]|#]+)', content)
            links += re.findall(r'\]\(([^)#]+)', content)
            if any(link in names or (page.parent / link).resolve() == target.resolve()
                   or (page.parent / (link + '.md')).resolve() == target.resolve() for link in links):
                references.append(page.relative_to(gl.REPO).as_posix())
    if references:
        blockers.append('Wiki references: ' + ', '.join(references))
    payload = {'hub': node, 'hub_sha256': _sha(target.read_bytes()),
               'node': dict(row) if row else None, 'aliases': aliases, 'blockers': blockers}
    plan_hash = _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())
    return {'operation': 'retire-private-legacy-hub-v1', 'eligible': not blockers,
            'plan_hash': plan_hash, **payload}


def _append_log(log, node):
    previous = log.read_text() if log.exists() else '# private operation log\n'
    log.write_text(previous + f'\n## [{datetime.date.today()}] maintenance | retire legacy Hub\n'
                   f'- Retired `{node}`: orphan arXiv-template Hub; Raw unchanged.\n')


def retire(hub, *, apply=False, expected_hash=''):
    target, node, root = _target(hub)
    db = root / 'graph.db'
    if not apply:
        return plan_retirement(hub)
    with gl.graph_writer_lock(db):
        plan = plan_retirement(hub)
        if not plan['eligible'] or not expected_hash or plan['plan_hash'] != expected_hash:
            raise ValueError('retirement blocked or plan changed; review a fresh dry-run')
        backup = root / 'outputs/maintenance' / (datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
        if not gl.private_graph_path(backup):
            raise ValueError('backup path escapes private storage')
        log = root / 'wiki/log.md'
        if log.is_symlink() or not gl.private_graph_path(log):
            raise ValueError('log path escapes private storage')
        backup.mkdir(parents=True)
        old_hub = target.read_bytes()
        old_log = log.read_bytes() if log.exists() else None
        (backup / 'hub.md').write_bytes(old_hub)
        if old_log is not None:
            (backup / 'log.md').write_bytes(old_log)
        source = gl.connect(db, read_only=True)
        try:
            gl.backup_graph(source, backup / 'graph.db')
        finally:
            source.close()
        receipt = {**plan, 'status': 'prepared', 'backup': str(backup.relative_to(gl.REPO)),
                   'log_existed': old_log is not None}
        receipt_path = backup / 'receipt.json'
        _write_receipt(receipt_path, receipt)
        conn = None
        try:
            conn = gl.connect(db)  # managed schema upgrade, covered by snapshot rollback
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM aliases WHERE node_path=?', (node,))
            conn.execute('DELETE FROM nodes WHERE path=?', (node,))
            if conn.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('foreign-key validation failed')
            target.unlink()
            _append_log(log, node)
            conn.commit()
            conn.close()
            conn = None
            receipt['status'] = 'completed'
            _write_receipt(receipt_path, receipt)
        except Exception:
            if conn is not None:
                conn.rollback()
                conn.close()
            gl.restore_graph(backup / 'graph.db', db)
            target.write_bytes(old_hub)
            if old_log is None:
                log.unlink(missing_ok=True)
            else:
                log.write_bytes(old_log)
            receipt['status'] = 'rolled_back'
            _write_receipt(receipt_path, receipt)
            raise
        return {'status': 'completed', 'hub': node,
                'receipt': str(receipt_path.relative_to(gl.REPO)), 'raw_changed': False}


def _cache_target():
    target = gl.REPO / 'cross-domain/embeddings.db'
    if target.is_symlink() or target.resolve().parent != gl.REPO.resolve() / 'cross-domain':
        raise ValueError('public cache must be a physical cross-domain/embeddings.db')
    if not target.is_file():
        raise FileNotFoundError(target)
    return target


def _cache_plan(conn):
    tables = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    if 'embeddings' not in tables or set(tables) - {'embeddings', 'node_texts'}:
        raise ValueError('unexpected cache schema; review before quarantine')
    digest = hashlib.sha256()
    counts = {}
    for table in tables:
        schema = conn.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
        digest.update(json.dumps([table, schema]).encode())
        counts[table] = 0
        for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1'):
            # Preserve exact BLOB identity without writing any text into a public report.
            values = [{'blob_sha256': _sha(v)} if isinstance(v, bytes) else v for v in row]
            digest.update(json.dumps(values, ensure_ascii=False).encode() + b'\n')
            counts[table] += 1
    return {'operation': 'quarantine-public-embedding-cache-v1',
            'plan_hash': digest.hexdigest(), 'counts': counts,
            'reason': 'legacy mixed cache has no reliable per-text domain provenance; rebuild both caches independently'}


def _clear_cache_tables(conn, tables):
    for table in tables:
        conn.execute(f'DELETE FROM {table}')


def quarantine_public_cache(*, apply=False, expected_hash=''):
    """Snapshot the entire legacy mixed cache inside private, then securely empty it.

    A SQLite immediate transaction serializes even cache writers that do not take
    the public graph lock. Never restore sensitive data to public after commit.
    """
    import sqlite3
    target = _cache_target()
    if not apply:
        conn = gl.connect(target, read_only=True)
        try:
            return _cache_plan(conn)
        finally:
            conn.close()
    with gl.graph_writer_lock(gl.GRAPH_DB):
        conn = sqlite3.connect(target.resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
        receipt = receipt_path = None
        committed = False
        try:
            conn.execute('PRAGMA secure_delete=ON')
            conn.execute('BEGIN IMMEDIATE')
            plan = _cache_plan(conn)
            if not expected_hash or plan['plan_hash'] != expected_hash:
                raise ValueError('cache plan changed; review a fresh dry-run')
            backup = gl.REPO / 'private/outputs/maintenance' / (
                'cache-' + datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
            if not gl.private_graph_path(backup):
                raise ValueError('cache backup escapes private storage')
            backup.mkdir(parents=True)
            source = gl.connect(target, read_only=True)
            try:
                gl.backup_graph(source, backup / 'embeddings.db')
            finally:
                source.close()
            receipt = {**plan, 'status': 'prepared', 'backup': str(backup.relative_to(gl.REPO))}
            receipt_path = backup / 'receipt.json'
            _write_receipt(receipt_path, receipt)
            _clear_cache_tables(conn, plan['counts'])
            conn.commit()
            committed = True
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
                checkpoint = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    raise RuntimeError('cache emptied but WAL still busy; stop readers and finish sanitation')
            conn.execute('VACUUM')
            if conn.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal':
                if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise RuntimeError('cache compaction WAL busy; sanitation incomplete')
            receipt['status'] = 'completed'
            receipt['remaining'] = _cache_plan(conn)['counts']
            _write_receipt(receipt_path, receipt)
            return {'status': 'completed', 'receipt': str(receipt_path.relative_to(gl.REPO)),
                    'cleared': plan['counts'], 'remaining': receipt['remaining'], 'raw_changed': False}
        except Exception:
            conn.rollback()
            if receipt is not None:
                receipt['status'] = 'sanitation_pending' if committed else 'rolled_back'
                _write_receipt(receipt_path, receipt)
            raise
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('hub', nargs='?')
    parser.add_argument('--quarantine-public-cache', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expect-hash', default='')
    args = parser.parse_args()
    if bool(args.hub) == args.quarantine_public_cache:
        parser.error('choose exactly one Hub target or --quarantine-public-cache')
    result = (quarantine_public_cache(apply=args.apply, expected_hash=args.expect_hash)
              if args.quarantine_public_cache else
              retire(args.hub, apply=args.apply, expected_hash=args.expect_hash))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

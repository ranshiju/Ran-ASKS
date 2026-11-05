#!/usr/bin/env python3
"""Managed, local-only bibliography supplements for an already archived paper."""
from __future__ import annotations

import argparse
import errno
import json
import re
import shlex
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml
import bibliography_sidecar as bs
import external_bibliography as eb
import graph_lib as gl
import graph_ingest as gi
import inbox_finalize as finalizer

REPO = Path(__file__).resolve().parent.parent
PROTOCOL = 'paper-bibliography-update-v1'
UPDATE_ERRORS = (ValueError, OSError, yaml.YAMLError, sqlite3.Error, KeyError, TypeError)


def digest(path):
    return finalizer.sha256_file(path)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.partial')
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError('checkpoint paths must not be symlinks')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def bounded_path(repo, value, prefix):
    path = Path(value)
    path = path if path.is_absolute() else repo / path
    relative = path.relative_to(repo)
    if relative.parts[:len(prefix)] != tuple(prefix) or '..' in relative.parts:
        raise ValueError('path is outside the managed update scope')
    if any((repo / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise ValueError('bibliography update paths must not contain symlinks')
    return path


def frontmatter(text):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', text, re.S)
    if not match:
        raise ValueError('Wiki has no frontmatter')
    fm = yaml.safe_load(match[1])
    if not isinstance(fm, dict):
        raise ValueError('Wiki frontmatter is not a mapping')
    return fm, match


def update_wiki(text, bibliography, today):
    fm, match = frontmatter(text)
    if any('\n' in str(value) or '\r' in str(value) for value in
           [bibliography['title'], bibliography['venue'], *bibliography['authors']]):
        raise ValueError('multiline bibliography display fields are unsupported')
    fields = {'title': bibliography['title'], 'authors': bibliography['authors'],
              'date': bibliography['year'], 'venue': bibliography['venue'],
              'doi': bibliography['doi'], 'updated': today}
    metadata = match[1] + '\n'
    starts = list(re.finditer(r'(?m)^([A-Za-z_][\w-]*):', metadata))
    names = [item[1] for item in starts]
    if len(set(names)) != len(names) or set(names) != set(fm):
        raise ValueError('unsupported or duplicate Wiki metadata keys')
    edits = []
    for i, item in enumerate(starts):
        if item[1] in fields:
            end = starts[i+1].start() if i+1 < len(starts) else len(metadata)
            edits.append((item.start(), end, yaml.safe_dump({item[1]: fields[item[1]]},
                         allow_unicode=True, sort_keys=False)))
    for start, end, value in reversed(edits):
        metadata = metadata[:start] + value + metadata[end:]
    for key, value in fields.items():
        if key not in names:
            metadata += yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False)
    body = text[match.end():]
    prefix, boundary, rest = body.partition('\n## ')
    prefix = re.sub(r'(?m)^# [^\n]*$', lambda _: '# ' + bibliography['title'], prefix, count=1)
    heading = r'(?m)^> \*\*作者\*\*：[^\n]*\| \*\*发表\*\*：[^\n]*$'
    if '**作者**' in prefix and not re.search(heading, prefix):
        raise ValueError('nonstandard bibliography display needs a targeted update')
    prefix = re.sub(heading, lambda _: '> **作者**：' + '、'.join(bibliography['authors']) +
                  ' | **发表**：' + bibliography['venue'] + ' (' + bibliography['year'] + ')', prefix, count=1)
    body = prefix + boundary + rest
    return '---\n' + metadata + '---\n' + body


def associated_wiki(repo, raw_md):
    relative = raw_md.relative_to(repo).as_posix()
    found = []
    for page in (repo / 'academic/wiki/papers').glob('*.md'):
        if page.is_symlink():
            continue
        try:
            bounded_path(repo, page, ('academic','wiki','papers'))
            fm, _ = frontmatter(page.read_text(encoding='utf-8'))
        except (ValueError, yaml.YAMLError):
            continue
        sources = fm.get('sources') or []
        if not isinstance(sources, list):
            sources = [sources]
        paths = {str(source).split('#', 1)[0] for source in sources}
        if fm.get('type') == 'paper-summary' and (relative in paths or relative.removeprefix('academic/') in paths):
            found.append(page)
    if len(found) != 1:
        raise ValueError('bibliography update requires exactly one associated paper Wiki')
    return found[0]


def check_identity_conflicts(old, new, fm):
    """A source-matched new record replaces old descriptive fields, even if locked."""
    previous = old.get('bibliographic') or {}
    if not isinstance(previous, dict):
        raise ValueError('existing bibliography is not a mapping')
    new_doi = eb.doi(new.get('doi') or '')
    if not new_doi or new_doi.startswith('10.48550/arxiv.'):
        return
    for value in (previous.get('doi'), fm.get('doi')):
        doi = eb.doi(value or '')
        if doi and not doi.startswith('10.48550/arxiv.') and doi != new_doi:
            raise ValueError('existing formal DOI conflicts with new bibliography')


def audit_entry(wiki, txn, evidence, today):
    datetime.strptime(today, '%Y-%m-%d')
    return (f'\n## [{today}] update | {wiki.stem} (书目增补)\n\n'
            f'- 事务 `{txn}`；原 PDF/Markdown 保留，旧书目归档；新书目来源 `{evidence}`。\n')


def source_record(old, bibliography, txn, source, md_hash, pdf_hash, old_hash):
    """Compile the same accepted record for preparation and independent validation."""
    return {**old, 'bibliographic': bibliography, 'bibliography_update': {
        'protocol_version': PROTOCOL, 'transaction_id': txn, 'source': source,
        'source_sha256': md_hash, 'pdf_sha256': pdf_hash, 'previous_source_sha256': old_hash}}


def text_snapshot(path):
    data = path.read_bytes()
    # Match Path.read_text's newline handling while retaining the byte-level guard.
    return data.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n'), bs.sha(data)


def selected_inputs(repo, pdf, local, pdf_hash):
    if not local.get('bibliographic'):
        raise ValueError('local bibliography remains ' + local['status'])
    receipt = local['receipt']
    scope = bs.discovery_scope(pdf, receipt)
    candidates = bs.discover(pdf, same_name_only=scope == 'same-name')
    expected = {item['original_name']: item['sha256'] for item in receipt['files']}
    if {p.name for p in candidates} != set(expected):
        raise ValueError('selected bibliography file set changed')
    inputs = {pdf.relative_to(repo).as_posix(): pdf_hash}
    for path in candidates:
        bounded_path(repo, path, ('inbox',))
        if not path.is_file() or path.stat().st_size > bs.MAX_FILE_BYTES or digest(path) != expected[path.name]:
            raise ValueError('selected bibliography input changed')
        inputs[path.relative_to(repo).as_posix()] = expected[path.name]
    return inputs


def prepare(repo, pdf, raw_pdf, *, _resolved=None):
    repo = Path(repo).resolve()
    pdf = bounded_path(repo, pdf, ('inbox',))
    raw_pdf = bounded_path(repo, raw_pdf, ('academic', 'raw'))
    pdf_hash, raw_pdf_hash = digest(pdf), digest(raw_pdf)
    if pdf.suffix.lower() != '.pdf' or raw_pdf.name != 'paper.pdf' or pdf_hash != raw_pdf_hash:
        raise ValueError('bibliography supplement requires the identical archived paper.pdf')
    raw_dir = raw_pdf.parent
    raw_md = bounded_path(repo, raw_dir / 'paper.md', ('academic', 'raw'))
    wiki = associated_wiki(repo, raw_md)
    old_text = wiki.read_text(encoding='utf-8')
    fm, _ = frontmatter(old_text)
    md, md_hash = text_snapshot(raw_md)
    provenance = raw_dir / 'source.yaml'
    if provenance.is_symlink():
        raise ValueError('source.yaml must not be a symlink')
    old_bytes = provenance.read_bytes() if provenance.is_file() else None
    old = yaml.safe_load(old_bytes) if old_bytes else {}
    if not isinstance(old, dict):
        raise ValueError('existing source.yaml is not a mapping')
    local = _resolved if _resolved is not None else bs.resolve_local(pdf, md)
    if local['receipt']['source_sha256'] != eb.digest(md):
        raise ValueError('Raw text changed after local resolution')
    inputs = selected_inputs(repo, pdf, local, pdf_hash)
    seed = {'inputs': inputs, 'wiki': digest(wiki), 'raw_md': md_hash,
            'provenance': bs.sha(old_bytes) if old_bytes is not None else None}
    txn = 'bib-update-' + eb.digest(json.dumps(seed, sort_keys=True))[:24]
    extract = repo / 'temp/inbox-extract' / txn
    bounded_path(repo, extract, ('temp','inbox-extract'))
    plan_path = bounded_path(repo, extract / 'update-plan.json', ('temp','inbox-extract'))
    if plan_path.exists():
        return {'status': 'prepared', 'transaction_id': txn, 'plan': str(plan_path.relative_to(repo))}
    extract.mkdir(parents=True, exist_ok=True)
    bs.stage_resolved(local, extract, md)
    b = local.get('bibliographic')
    if not b:
        raise ValueError('local bibliography remains ' + local['status'])
    check_identity_conflicts(old, b, fm)
    for path, expected in inputs.items():
        if digest(repo / path) != expected:
            raise ValueError('input changed during bibliography preparation')
    history = 'bibliography-history/' + txn
    for name in ('source.yaml', 'wiki.md', 'original-wiki.md', 'manifest.json', history+'/source.yaml', history+'/accepted.yaml'):
        bounded_path(repo, extract/name, tuple(extract.relative_to(repo).parts))
    (extract / history).mkdir(parents=True, exist_ok=True)
    replacement = None
    if old_bytes is not None:
        old_path = history + '/source.yaml'
        (extract / old_path).write_bytes(old_bytes)
        replacement = {'sha256': bs.sha(old_bytes), 'archive_path': old_path}
    accepted = source_record(old, b, txn, raw_md.relative_to(repo).as_posix(),
                             md_hash, raw_pdf_hash, seed['provenance'])
    serialized = yaml.safe_dump(accepted, allow_unicode=True, sort_keys=False)
    (extract / 'source.yaml').write_text(serialized, encoding='utf-8')
    accepted_path = history + '/accepted.yaml'
    (extract / accepted_path).write_text(serialized, encoding='utf-8')
    today = datetime.now().astimezone().date().isoformat()
    new_wiki = update_wiki(old_text, b, today)
    (extract / 'wiki.md').write_text(new_wiki, encoding='utf-8')
    (extract / 'original-wiki.md').write_text(old_text, encoding='utf-8')
    guards = {raw_pdf.relative_to(repo).as_posix(): raw_pdf_hash, raw_md.relative_to(repo).as_posix(): md_hash}
    artifacts = []
    for relative in ['source.yaml', accepted_path] + ([replacement['archive_path']] if replacement else []) + [f['path'] for f in local['receipt']['files']]:
        source = extract / relative
        target = raw_dir / relative
        bounded_path(repo, target, ('academic', 'raw'))
        if target.exists() and relative != 'source.yaml':
            if digest(target) != digest(source):
                raise ValueError('immutable bibliography artifact collision')
            guards[target.relative_to(repo).as_posix()] = digest(target)
            continue
        artifacts.append({'path': relative, 'role': 'source-provenance' if relative.endswith('.yaml') else 'bibliographic-source',
                          'sha256': digest(source), 'bytes': source.stat().st_size})
    manifest = {'schema': finalizer.MANIFEST_V2, 'raw_artifacts': artifacts, 'wiki_file': 'wiki.md'}
    write_json(extract / 'manifest.json', manifest)
    graph = repo / 'cross-domain/graph.db'
    if not graph.is_file():
        raise ValueError('existing public graph is required')
    page = wiki.relative_to(repo).as_posix().removesuffix('.md')
    evidence = (raw_dir / accepted_path).relative_to(repo).as_posix()
    new_fm, _ = frontmatter(new_wiki)
    with gl.graph_writer_lock(graph):
        conn = gl.connect(graph)
        try:
            before = gi.bibliography_graph_state(conn, page)
            conn.execute('BEGIN')
            after = gi.refresh_paper_bibliography(conn, page, new_fm, evidence)
        finally:
            conn.rollback(); conn.close()
    plan = {'protocol_version': PROTOCOL, 'transaction_id': txn, 'status': 'prepared',
            'updated_date': today, 'inputs': inputs, 'raw_pdf': raw_pdf.relative_to(repo).as_posix(),
            'raw_dir': raw_dir.relative_to(repo).as_posix(), 'wiki': wiki.relative_to(repo).as_posix(),
            'wiki_sha256': seed['wiki'], 'draft_sha256': digest(extract/'wiki.md'),
            'manifest_sha256': digest(extract/'manifest.json'), 'raw_guards': guards,
            'provenance_update': replacement, 'source_yaml_sha256': digest(extract/'source.yaml'),
            'graph_before': before, 'graph_after': after, 'graph_evidence': evidence,
            'log_entry': audit_entry(wiki, txn, evidence, today)}
    write_json(plan_path, plan)
    return {'status': 'prepared', 'transaction_id': txn, 'plan': plan_path.relative_to(repo).as_posix()}


def validate_prepared(repo, extract, plan, manifest, raw_dir, raw_hashes):
    """Recompute accepted metadata and the limited edit; plans are not authority."""
    txn = plan['transaction_id']
    if not re.fullmatch(r'bib-update-[0-9a-f]{24}', txn):
        raise ValueError('invalid update transaction id')
    history = 'bibliography-history/' + txn
    accepted_path = history + '/accepted.yaml'
    for name in ('source.yaml', 'wiki.md', 'original-wiki.md', accepted_path):
        bounded_path(repo, extract/name, tuple(extract.relative_to(repo).parts))
    raw_md, raw_pdf = raw_dir/'paper.md', raw_dir/'paper.pdf'
    if plan['raw_pdf'] != raw_pdf.relative_to(repo).as_posix():
        raise ValueError('inconsistent archived PDF path')
    source = yaml.safe_load((extract/'source.yaml').read_text())
    b = source['bibliographic']
    if b.get('external_receipt', {}).get('protocol_version') != bs.PROTOCOL:
        raise ValueError('only local bibliography receipts may update an existing paper')
    md, md_hash = text_snapshot(raw_md)
    if md_hash != raw_hashes.get(raw_md.relative_to(repo).as_posix()):
        raise ValueError('Raw text changed during validation')
    raw_pdf_hash = raw_hashes.get(raw_pdf.relative_to(repo).as_posix())
    verified = bs.replay(b['external_receipt'], md, extract)
    if not verified or verified != b:
        raise ValueError('local bibliography update receipt failed replay')
    pdf_inputs = [path for path in plan['inputs'] if Path(path).suffix.lower() == '.pdf']
    if len(pdf_inputs) != 1 or plan['inputs'][pdf_inputs[0]] != raw_pdf_hash:
        raise ValueError('plan input PDF does not match archived PDF')
    input_pdf = bounded_path(repo, pdf_inputs[0], ('inbox',))
    expected_inputs = {pdf_inputs[0]: raw_pdf_hash}
    for item in b['external_receipt']['files']:
        name = item.get('original_name', '')
        if not name or Path(name).name != name or Path(name).suffix.lower() not in bs.SUFFIXES:
            raise ValueError('invalid selected bibliography source name')
        expected_inputs[(input_pdf.parent/name).relative_to(repo).as_posix()] = item['sha256']
    if plan['inputs'] != expected_inputs:
        raise ValueError('plan inputs do not match the accepted bibliography originals')

    replacement = plan['provenance_update']
    old, old_hash = {}, None
    if replacement:
        old_path = history + '/source.yaml'
        bounded_path(repo, extract/old_path, tuple(extract.relative_to(repo).parts))
        old_hash = digest(extract/old_path)
        if replacement != {'archive_path': old_path, 'sha256': old_hash}:
            raise ValueError('old provenance snapshot changed')
        old = yaml.safe_load((extract/old_path).read_text())
    original = (extract/'original-wiki.md').read_text()
    if digest(extract/'original-wiki.md') != plan['wiki_sha256']:
        raise ValueError('original Wiki snapshot changed')
    fm, _ = frontmatter(original)
    check_identity_conflicts(old, b, fm)
    paths = fm.get('sources') or []
    if not isinstance(paths, list): paths = [paths]
    raw_relative = raw_md.relative_to(repo).as_posix()
    if not {raw_relative, raw_relative.removeprefix('academic/')} & {str(p).split('#',1)[0] for p in paths}:
        raise ValueError('Wiki does not reference the updated Raw')
    seed = {'inputs': plan['inputs'], 'wiki': plan['wiki_sha256'],
            'raw_md': md_hash, 'provenance': old_hash}
    if txn != 'bib-update-' + eb.digest(json.dumps(seed, sort_keys=True))[:24]:
        raise ValueError('update identity changed')
    expected = source_record(old, verified, txn, raw_relative, md_hash, raw_pdf_hash, old_hash)
    if source != expected or (extract/'source.yaml').read_bytes() != (extract/accepted_path).read_bytes():
        raise ValueError('accepted provenance changed outside bibliography fields')
    if digest(extract/'source.yaml') != plan['source_yaml_sha256']:
        raise ValueError('accepted source snapshot changed')
    if update_wiki(original, verified, plan['updated_date']) != (extract/'wiki.md').read_text():
        raise ValueError('draft changed outside deterministic bibliography edit')
    if plan['graph_evidence'] != (raw_dir/accepted_path).relative_to(repo).as_posix():
        raise ValueError('graph evidence must reference immutable accepted snapshot')
    if plan['log_entry'] != audit_entry(Path(plan['wiki']), txn, plan['graph_evidence'], plan['updated_date']):
        raise ValueError('update audit entry changed')
    required = {'source.yaml', accepted_path}
    if replacement: required.add(replacement['archive_path'])
    allowed = required | {item['path'] for item in b['external_receipt']['files']}
    entries = [item['path'] for item in manifest['raw_artifacts']]
    if (len(entries) != len(set(entries)) or not required <= set(entries) <= allowed
            or manifest != {'schema': finalizer.MANIFEST_V2, 'raw_artifacts': manifest['raw_artifacts'], 'wiki_file':'wiki.md'}):
        raise ValueError('manifest exceeds bibliography update scope')
    guards = {raw_pdf.relative_to(repo).as_posix(): raw_pdf_hash, raw_relative: md_hash}
    for item in b['external_receipt']['files']:
        if item['path'] not in entries:
            guards[(raw_dir/item['path']).relative_to(repo).as_posix()] = item['sha256']
    if plan['raw_guards'] != guards:
        raise ValueError('original Raw guards changed')
    return input_pdf, b['external_receipt']


def apply(repo, plan_value):
    repo = Path(repo).resolve()
    plan_path = bounded_path(repo, plan_value, ('temp', 'inbox-extract'))
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    if plan.get('protocol_version') != PROTOCOL or plan_path.parent.name != plan.get('transaction_id'):
        raise ValueError('invalid bibliography update plan')
    extract = plan_path.parent
    raw_dir = bounded_path(repo, plan['raw_dir'], ('academic','raw'))
    wiki = bounded_path(repo, plan['wiki'], ('academic','wiki','papers'))
    page = plan['wiki'].removesuffix('.md')
    for name in ('manifest.json', 'wiki.md', 'source.yaml', 'original-wiki.md'):
        bounded_path(repo, extract/name, tuple(extract.relative_to(repo).parts))
    if digest(extract/'manifest.json') != plan['manifest_sha256'] or digest(extract/'wiki.md') != plan['draft_sha256']:
        raise ValueError('prepared update artifacts changed')
    manifest = json.loads((extract/'manifest.json').read_text())
    finalizer.load_manifest(extract/'manifest.json')
    for entry in manifest['raw_artifacts']:
        staged = bounded_path(repo, extract/entry['path'], tuple(extract.relative_to(repo).parts))
        bounded_path(repo, raw_dir/entry['path'], tuple(raw_dir.relative_to(repo).parts))
        finalizer.verify_manifest_artifact(staged, entry)
    raw_hashes = {}
    for path, expected in plan['raw_guards'].items():
        target = bounded_path(repo, path, ('academic','raw'))
        raw_hashes[path] = digest(target)
        if raw_hashes[path] != expected:
            raise ValueError('original Raw changed since bibliography preparation')
    input_pdf, bibliography_receipt = validate_prepared(repo, extract, plan, manifest, raw_dir, raw_hashes)
    # File receipts are durable even if the process stopped immediately before graph commit.
    receipts = []
    for path in (repo/'temp/inbox-receipts').glob(plan['transaction_id']+'-*.json'):
        bounded_path(repo, path, ('temp','inbox-receipts'))
        value = json.loads(path.read_text())
        if (value.get('status') == 'committed' and value.get('paper_id') == plan['transaction_id']
                and value.get('raw_dir') == plan['raw_dir']
                and value.get('manifest_sha256') == plan['manifest_sha256']
                and value.get('wiki_path') == plan['wiki']
                and value.get('replaced_wiki_sha256') == plan['wiki_sha256']
                and value.get('provenance_update') == plan['provenance_update']):
            receipts.append(path)
    if len(receipts) > 1:
        raise ValueError('ambiguous bibliography update receipts')
    landed = bool(receipts)
    if landed:
        log = bounded_path(repo, 'academic/wiki/log.md', ('academic','wiki'))
        if log.read_text().count(plan['log_entry']) != 1:
            raise ValueError('committed bibliography log entry missing or duplicated')
        if digest(wiki) != plan['draft_sha256']:
            raise ValueError('committed Wiki changed; refusing resume')
        for entry in manifest['raw_artifacts']:
            finalizer.verify_manifest_artifact(raw_dir/entry['path'], entry)
    else:
        scope = bs.discovery_scope(input_pdf, bibliography_receipt)
        selected = bs.discover(input_pdf, same_name_only=scope == 'same-name')
        if {p.name for p in selected} != {f['original_name'] for f in bibliography_receipt['files']}:
            raise ValueError('selected bibliography file set changed since preparation')
        if digest(wiki) != plan['wiki_sha256']:
            raise ValueError('original Wiki changed since preparation')
        for path, expected in plan['inputs'].items():
            if digest(bounded_path(repo,path,('inbox',))) != expected:
                raise ValueError('intake source changed since preparation')
    graph = repo/'cross-domain/graph.db'
    if not graph.is_file():
        raise ValueError('existing graph is missing')
    fm, _ = frontmatter((extract/'wiki.md').read_text())
    with gl.graph_writer_lock(graph):
        conn = gl.connect(graph)
        try:
            current = gi.bibliography_graph_state(conn,page)
            if current not in (plan['graph_before'],plan['graph_after']):
                raise ValueError('paper bibliography graph changed since preparation')
            conn.execute('BEGIN')
            unchanged = current == plan['graph_after']
            if unchanged:
                conn.execute('SAVEPOINT bibliography_recheck')
            actual = gi.refresh_paper_bibliography(conn,page,fm,plan['graph_evidence'])
            if actual != plan['graph_after']:
                raise ValueError('graph attachment changed since preflight')
            if unchanged:
                conn.execute('ROLLBACK TO SAVEPOINT bibliography_recheck')
                conn.execute('RELEASE SAVEPOINT bibliography_recheck')
            if landed:
                finalizer.run_ingest_check(repo,wiki)
                conn.commit()
                receipt = receipts[0]
            else:
                receipt = finalizer.finalize(repo, plan['transaction_id'],raw_dir,wiki,extract,
                    extract/'manifest.json',False,True,expected_wiki_sha256=plan['wiki_sha256'],
                    raw_guards=tuple((repo/path,value) for path,value in plan['raw_guards'].items()),
                    update_log=(repo/'academic/wiki/log.md',plan['log_entry']),
                    provenance_update=plan['provenance_update'], commit_hook=conn.commit)
            if gi.bibliography_graph_state(conn,page) != plan['graph_after']:
                raise ValueError('graph verification after commit failed')
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    result={'status':'completed','transaction_id':plan['transaction_id'], 'receipt':receipt.relative_to(repo).as_posix(),
            'plan':plan_path.relative_to(repo).as_posix(),'wiki_path':plan['wiki'], 'raw_dir':plan['raw_dir'],
            'http_requests':0,'llm_calls':0,'ocr_calls':0,'resumed_file_commit':landed}
    write_json(extract/'update-result.json',result)
    return result


def transient_failure(exc):
    if isinstance(exc, OSError):
        return exc.errno in {errno.EAGAIN, errno.EBUSY, errno.EINTR, errno.ETIMEDOUT}
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, 'sqlite_errorcode', None)
        return isinstance(code, int) and (code & 0xff) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return False


def failure_result(exc, prepared=None):
    infrastructure = isinstance(exc, (OSError, sqlite3.Error)) and not isinstance(exc, FileNotFoundError)
    retryable = transient_failure(exc)
    result = {'status': 'failed' if infrastructure else 'bibliographic_review_required',
              'reason': str(exc), 'retryable': retryable,
              'failure_domain': ('graph' if isinstance(exc, sqlite3.Error) else 'storage') if infrastructure else 'validation',
              'next_action': ('retry_bibliography_update' if retryable else 'repair_storage_then_retry')
                             if infrastructure else 'resolve_bibliography_update_diagnostics'}
    if infrastructure:
        result['failure_disposition'] = {
            'category': 'local_transient' if retryable else 'local_environment_failure',
            'domain': result['failure_domain'],
            'disposition': 'bounded_client_retry' if retryable else 'configuration_fix',
            'owner': 'program' if retryable else 'configuration', 'retryable': retryable,
            'next_action': result['next_action'], 'fingerprints': []}
    if prepared:
        result.update({key: prepared[key] for key in ('transaction_id', 'plan')})
        result['apply_command'] = 'python3 .scripts/bibliography_update.py --apply ' + shlex.quote(prepared['plan'])
    return result


def execute_plan(repo, prepared):
    """At most one deterministic retry; apply guards each attempt independently."""
    reasons = []
    for attempt in (1, 2):
        try:
            result = apply(repo, prepared['plan'])
        except UPDATE_ERRORS as exc:
            if attempt == 1 and transient_failure(exc):
                reasons.append(str(exc)[:512])
                continue
            result = failure_result(exc, prepared)
        result.update(apply_attempts=attempt, automatic_retry_count=attempt-1)
        if reasons:
            result['retry_reasons'] = reasons
        return result


def scan_warning(result, warnings):
    if warnings:
        result['plan_scan_warning_count'] = len(warnings)
        result['plan_scan_warnings'] = warnings[:20]
    return result


def recover_pending(repo, raw_pdf):
    """Do not let duplicate cleanup hide a landed file phase with uncommitted graph."""
    repo = Path(repo).resolve()
    prepared = None
    try:
        candidate = Path(raw_pdf)
        relative = candidate.relative_to(repo) if candidate.is_absolute() else candidate
        if relative.parts[:2] != ('academic','raw') or relative.name != 'paper.pdf':
            return None
        raw_pdf = bounded_path(repo, raw_pdf, ('academic','raw'))
        source = bounded_path(repo, raw_pdf.parent/'source.yaml', ('academic','raw'))
        if not source.is_file(): return None
        metadata = yaml.safe_load(source.read_text()) or {}
        if not isinstance(metadata, dict):
            raise ValueError('archived provenance is not a mapping')
        update = metadata.get('bibliography_update') or {}
        if not isinstance(update, dict):
            raise ValueError('archived bibliography update is not a mapping')
        if update.get('protocol_version') != PROTOCOL: return None
        txn = update.get('transaction_id', '')
        if not re.fullmatch(r'bib-update-[0-9a-f]{24}', txn):
            raise ValueError('invalid archived bibliography transaction')
        extract = bounded_path(repo, repo/'temp/inbox-extract'/txn, ('temp','inbox-extract'))
        prepared = {'transaction_id': txn, 'plan': (extract/'update-plan.json').relative_to(repo).as_posix()}
        checkpoint = bounded_path(repo, extract/'update-result.json', ('temp','inbox-extract'))
        if checkpoint.is_file():
            result = json.loads(checkpoint.read_text())
            if result.get('status') == 'completed' and result.get('transaction_id') == txn:
                return None
            raise ValueError('invalid bibliography completion checkpoint')
        return execute_plan(repo, prepared)
    except UPDATE_ERRORS as exc:
        return failure_result(exc, prepared)


def run(repo, pdf, raw_pdf):
    """Shared inbox controller: resume a bound plan or prepare/apply locally."""
    repo = Path(repo).resolve()
    prepared = None
    warnings = []
    try:
        pdf = bounded_path(repo, pdf, ('inbox',))
        raw_pdf = bounded_path(repo, raw_pdf, ('academic','raw'))
        raw_md = bounded_path(repo, raw_pdf.parent/'paper.md', ('academic','raw'))
        local = bs.resolve_local(pdf, raw_md.read_text(encoding='utf-8'))
        inputs = selected_inputs(repo, pdf, local, digest(pdf))
        matches = []
        for path in (repo/'temp/inbox-extract').glob('bib-update-*/update-plan.json'):
            try:
                bounded_path(repo, path, ('temp','inbox-extract'))
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    raise ValueError('bibliography plan is not a mapping')
            except (ValueError, OSError) as exc:
                warnings.append({'plan': path.relative_to(repo).as_posix(), 'reason': str(exc)[:512]})
                continue
            if value.get('inputs') == inputs and value.get('raw_pdf') == raw_pdf.relative_to(repo).as_posix():
                checkpoint = bounded_path(repo, path.with_name('update-result.json'), ('temp','inbox-extract'))
                if checkpoint.is_file():
                    result = json.loads(checkpoint.read_text())
                    provenance = bounded_path(repo, raw_pdf.parent/'source.yaml', ('academic','raw'))
                    current = yaml.safe_load(provenance.read_text()) if provenance.is_file() else {}
                    if not isinstance(current, dict) or not isinstance(current.get('bibliography_update') or {}, dict):
                        raise ValueError('archived provenance update is not a mapping')
                    active = (current.get('bibliography_update') or {}).get('transaction_id')
                    if (result.get('status') == 'completed' and result.get('transaction_id') == value.get('transaction_id')
                            and active and active != value.get('transaction_id')):
                        continue  # Superseded completed work is history, not a resume target.
                matches.append(path)
        if len(matches) > 1:
            raise ValueError('multiple matching bibliography plans; use explicit --apply')
        prepared = ({'plan': matches[0].relative_to(repo).as_posix(), 'transaction_id': matches[0].parent.name}
                    if matches else prepare(repo, pdf, raw_pdf, _resolved=local))
        return scan_warning(execute_plan(repo, prepared), warnings)
    except UPDATE_ERRORS as exc:
        return scan_warning(failure_result(exc, prepared), warnings)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf');parser.add_argument('--raw-pdf');parser.add_argument('--apply')
    args=parser.parse_args()
    try:
        if args.apply:
            path=bounded_path(REPO,args.apply,('temp','inbox-extract'))
            result=execute_plan(REPO,{'transaction_id':path.parent.name,'plan':path.relative_to(REPO).as_posix()})
        elif args.pdf and args.raw_pdf:
            result=prepare(REPO,args.pdf,args.raw_pdf)
        else:
            parser.error('use --pdf + --raw-pdf to prepare, or --apply <plan>')
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['status'] in {'failed','bibliographic_review_required'}:
            raise SystemExit(1)
    except UPDATE_ERRORS as exc:
        print(json.dumps(failure_result(exc),ensure_ascii=False))
        raise SystemExit(1)


if __name__=='__main__':
    main()

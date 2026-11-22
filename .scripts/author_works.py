#!/usr/bin/env python3
"""Offline author inventory. Counts describe discovered records, not verified publications."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata

import yaml
import graph_lib as gl
import source_locator as sl
import source_ingest as si

REPO = Path(__file__).resolve().parent.parent
PROTOCOL = 'author-works-v1'
MAX_PAGES = 20000
MAX_HEADER_BYTES = 65536


def key(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value)).casefold() if c.isalnum())


def safe_path(repo, value, area):
    path = repo / str(value).split('#', 1)[0]
    resolved = path.resolve()
    if not resolved.is_relative_to((repo / area).resolve()):
        raise ValueError('path outside ' + area)
    return path


def frontmatter(path):
    with path.open(encoding='utf-8') as handle:
        text = handle.read(MAX_HEADER_BYTES + 1)
    if not text.startswith('---\n'):
        return {}
    end = re.search(r'^---\s*$', text[4:], re.M)
    if not end:
        raise ValueError('missing or oversized frontmatter')
    value = yaml.safe_load(text[4:4 + end.start()])
    if not isinstance(value, dict):
        raise ValueError('frontmatter must be mapping')
    return value


def author_names(value):
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    return [v if isinstance(v, str) else str(v.get('name', '')) for v in value if isinstance(v, (str, dict))]


def resolve_author(conn, author):
    rows = conn.execute("SELECT path,title,status FROM nodes WHERE type='people' OR path LIKE 'academic/wiki/authors/%'").fetchall()
    aliases = defaultdict(list)
    for row in conn.execute('SELECT node_path,alias FROM aliases'):
        aliases[row['node_path']].append(row['alias'])
    matches = []
    for row in rows:
        path = row['path']
        if not path.startswith('academic/') or row['status'] == 'deprecated':
            continue
        names = [row['title'], *aliases[path]]
        if author.removesuffix('.md') == path or any(key(author) == key(n) for n in names):
            matches.append({'path': path, 'title': row['title'], 'names': sorted(set(
                n for n in names if n and '/' not in n and '[' not in n))})
    if len(matches) != 1:
        raise ValueError('author unresolved or ambiguous: ' + json.dumps(matches, ensure_ascii=False))
    return matches[0]


def header(path):
    """Bounded identity region. Never search body/reference lists for author identity."""
    with path.open(encoding='utf-8') as handle:
        text = handle.read(MAX_HEADER_BYTES + 1)
    lines = text.splitlines()[:160]
    stop = len(lines)
    for i, line in enumerate(lines):
        if re.match(r'^\s*(?:#{1,6}\s*)?(?:(?:\d+(?:\.\d+)*|[IVX]+)[.\s]+)?(?:abstract|introduction|references|bibliography|摘要|参考文献)\b', line, re.I):
            stop = i
            break
        if re.match(r'^\s*\[1\]\s', line):
            stop = i
            break
    # Header is a discovery hint. When no explicit section boundary exists,
    # restrict name search to 40 lines rather than arbitrary manuscript prose.
    return lines[:min(stop, 40)], len(text) > MAX_HEADER_BYTES


def name_hits(lines, names):
    hits = []
    for i, line in enumerate(lines, 1):
        plain = re.sub(r'<(?:sup|sub)\b[^>]*>.*?</(?:sup|sub)>', ' ', line, flags=re.I)
        plain = re.sub(r'<[^>]+>', ' ', plain)
        for name in names:
            chars = key(name)
            if not chars:
                continue
            pattern = r'(?<!\w)' + r'[\W_]*'.join(map(re.escape, chars)) + r'(?!\w)'
            if re.search(pattern, unicodedata.normalize('NFKC', plain).casefold()):
                hits.append({'line': i, 'name': name, 'text': line})
                break
    return hits


def own_doi(lines):
    matches = []
    for line in lines:
        # Only an explicit DOI header, not arbitrary DOI mentions/citations.
        m = re.match(r'^\s*(?:doi\s*[:：]\s*|https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/\S+)\s*$', line, re.I)
        if m:
            matches.append(m[1].rstrip('.,;)').lower())
    return matches[0] if len(set(matches)) == 1 else ''


def category_hint(title, venues, source):
    text = ' '.join([title, *venues, Path(source).name]).casefold()
    if re.search(r'supplement(?:al|ary)?|补充材料', text):
        return 'supplement'
    if re.search(r'patent|专利|软件著作权|软件著作|software', text) or '/patents/' in source or '/software/' in source:
        return 'intellectual_property'
    if re.search(r'isbn|出版社|书封面|版权页', text) or '/books/' in source:
        return 'book'
    if re.search(r'cccf|中国计算机学会通讯', text):
        return 'magazine_article'
    if re.search(r'proceedings|论文集', text):
        return 'proceedings_volume'
    if re.search(r'workshop|子会议|conference', text):
        return 'conference_item'
    if re.search(r'preprint|arxiv|预印本|submitted', text):
        return 'preprint'
    if re.search(r'editorial', text):
        return 'editorial'
    if re.search(r'news\s*&\s*views|viewpoint|观点', text):
        return 'commentary'
    return 'paper_candidate'


def grep_pages(repo, names):
    """Use rg as the cheap prefilter; parse authors only in the returned files."""
    root = repo / 'academic/wiki'
    if not root.is_dir():
        return [], 'empty_scope'
    patterns = [r'(?<!\w)' + r'[\W_]*'.join(map(re.escape, key(n))) + r'(?!\w)' for n in names if key(n)]
    if shutil.which('rg'):
        args = ['rg', '--files-with-matches', '--null', '--ignore-case', '--pcre2', '--glob', '*.md', '--glob', '!outputs/**']
        for pattern in patterns: args.extend(['-e', pattern])
        run = subprocess.run([*args, str(root)], capture_output=True)
        if run.returncode not in {0, 1}:
            raise ValueError('rg failed: ' + run.stderr.decode('utf-8', errors='replace')[:250])
        return sorted(Path(x.decode('utf-8')) for x in run.stdout.split(b'\0') if x), 'rg'
    combined = re.compile('|'.join(patterns), re.I)
    return sorted(p for p in root.rglob('*.md') if 'outputs' not in p.parts and combined.search(p.read_text(encoding='utf-8'))), 'python_fallback'


def inventory(author, *, repo=REPO, max_pages=MAX_PAGES, extra_sources=()):
    repo = Path(repo).resolve()
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError('max_pages must be between 1 and ' + str(MAX_PAGES))
    db = repo / 'cross-domain/graph.db'
    if not db.is_file():
        raise ValueError('graph database missing; will not create it')
    conn = gl.connect(str(db), read_only=True)
    try:
        person = resolve_author(conn, author)
        linked = set()
        for e in conn.execute("SELECT subject,object FROM edges WHERE (subject=? OR object=?) AND predicate IN ('作者','通讯作者','第一作者')", (person['path'], person['path'])):
            linked.add(e['object'] if e['subject'] == person['path'] else e['subject'])
        graph_status = {r['path']: r['status'] for r in conn.execute("SELECT path,status FROM nodes WHERE path LIKE 'academic/wiki/%'")}
    finally:
        conn.close()
    paths, backend = grep_pages(repo, person['names'])
    grep_count = len(paths)
    paths = sorted(set(paths) | {repo / (p + '.md') for p in linked if p.startswith('academic/wiki/')})
    truncated = len(paths) > max_pages
    issues, sources, metadata_matches = [], {}, set()
    keys = {key(n) for n in person['names']}
    read_pages = 0
    for page in paths[:max_pages]:
        try:
            page = safe_path(repo, page.relative_to(repo).as_posix(), 'academic/wiki')
            page_id = page.relative_to(repo).as_posix().removesuffix('.md')
            if graph_status.get(page_id) == 'deprecated': continue
            fm = frontmatter(page); read_pages += 1
            if fm.get('status') == 'deprecated': continue
            matched = any(key(a) in keys for a in author_names(fm.get('authors')))
            if not matched and page_id not in linked: continue
            if matched: metadata_matches.add(page_id)
            page_sources = fm.get('sources', [])
            if not isinstance(page_sources, list) or any(not isinstance(s, str) for s in page_sources):
                raise ValueError('sources must be a list of paths')
            for source in page_sources:
                if not isinstance(source, str): continue
                source = source.split('#', 1)[0]
                if not source.startswith('academic/raw/'): continue
                entry = sources.setdefault(source, {'pages': [], 'hints': []})
                entry['pages'].append(page_id)
                entry['hints'].append({'page': page_id, 'title': str(fm.get('title', '')),
                    'venue': str(fm.get('venue', '')), 'authors': author_names(fm.get('authors')),
                    'author_match': matched, 'graph_author_link': page_id in linked})
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            issues.append({'path': str(page), 'error': str(exc)[:250]})
    for source in extra_sources:
        if not str(source).startswith('academic/raw/'):
            raise ValueError('extra source must be an explicitly located academic/raw path')
        sources.setdefault(str(source).split('#', 1)[0], {'pages': [], 'hints': []})
    records = []
    for source, meta in sorted(sources.items()):
        try:
            original = safe_path(repo, source, 'academic/raw')
            target = original
            if target.suffix.lower() == '.pdf':
                options = [target.with_suffix('.md'), target.with_suffix('') / 'paper.md']
                target = next((p for p in options if p.is_file()), target)
            if target.suffix.lower() not in {'.md', '.txt'} or not target.is_file():
                issues.append({'path': source, 'error': 'paper text unavailable; managed parse required'})
                lines, hits, read_path = [], [], ''
            else:
                safe_path(repo, target.relative_to(repo).as_posix(), 'academic/raw')
                binding = si.binding_for_target(repo, target)
                if binding and binding['status'] != 'valid': raise ValueError('invalid Source binding')
                companion = sl.companion_binding_for_target(target)
                if companion and companion['status'] == 'invalid': raise ValueError('invalid companion binding')
                lines, _ = header(target)
                hits = name_hits(lines, person['names'])
                read_path = target.relative_to(repo).as_posix()
            title = next((h['title'] for h in meta['hints'] if h['title']), '')
            raw_title = next((re.sub(r'^#+\s*', '', line).strip() for line in lines if re.match(r'^#+\s+', line)), '')
            if not title:
                title = raw_title
            venues = sorted({h['venue'] for h in meta['hints'] if h['venue']})
            evidence = [{'locator': read_path + '#L' + str(h['line']), 'text': h['text']} for h in hits]
            records.append({'id': hashlib.sha256(source.encode()).hexdigest()[:16], 'source': source,
                'read_path': read_path, 'pages': sorted(set(meta['pages'])), 'title': title, 'raw_title': raw_title,
                'metadata_hints': meta['hints'], 'raw_name_matches': evidence,
                'header_locator': read_path + '#L1-L' + str(len(lines)) if lines else '',
                'header': '\n'.join(lines), 'doi': own_doi(lines),
                'category_hint': category_hint(title + ' ' + raw_title, venues, source),
                'author_status': 'raw_header_match_requires_identity_review' if hits else 'metadata_only',
                'evidence_sha256': hashlib.sha256('\n'.join(lines).encode()).hexdigest() if lines else ''})
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            issues.append({'path': source, 'error': str(exc)[:250]})
    groups = group_records(records)
    result = {'ok': True, 'protocol': PROTOCOL, 'author': person,
        'coverage': {'scope': 'public academic Wiki author fields plus registered authorship edges',
            'prefilter': backend, 'grep_matched_files': grep_count, 'pages_read': read_pages,
            'sources_considered': len(sources), 'truncated': truncated, 'issues': issues,
            'enumeration_complete': not truncated and not issues,
            'author_graph_neighbors': len(linked), 'metadata_matched_pages': len(metadata_matches),
            'source_only_records': 'not_searched; expand only for a concrete evidence gap',
            'worldwide_publication_completeness': 'not_established'},
        'counts': {'source_records': len(records), 'identity_groups': len(groups),
            'raw_header_matched_records': sum(bool(r['raw_name_matches']) for r in records),
            'category_hints_by_source': dict(sorted(Counter(r['category_hint'] for r in records).items()))},
        'publication_count': None,
        'count_boundary': 'Identity groups and category hints are discovery statistics. Review Raw authors, publication status, work type and version relations before reporting publications.',
        'groups': groups, 'records': records}
    result['inventory_digest'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def group_records(records):
    """Same read target or explicit own DOI only; title equality remains a hint."""
    groups = []
    for record in records:
        matches = [g for g in groups if (record['read_path'] and record['read_path'] in g['read_paths'])
                   or (record['doi'] and record['doi'] in g['dois']
                       and (record['category_hint'] == 'supplement') == ('supplement' in g['category_hints']))]
        if matches:
            group = matches[0]
            for other in matches[1:]:
                for field in group: group[field].extend(other[field])
                groups.remove(other)
        else:
            group = {'ids': [], 'read_paths': [], 'dois': [], 'category_hints': []}
            groups.append(group)
        group['ids'].append(record['id'])
        if record['read_path']: group['read_paths'].append(record['read_path'])
        if record['doi']: group['dois'].append(record['doi'])
        group['category_hints'].append(record['category_hint'])
        for field in group: group[field] = sorted(set(group[field]))
    titles = defaultdict(list)
    for record in records:
        title = record.get('raw_title') or record['title']
        if key(title): titles[key(title)].append(record['id'])
    for group in groups:
        group['same_title_review_candidates'] = sorted({rid for ids in titles.values()
            if set(ids) & set(group['ids']) for rid in ids if rid not in group['ids']})
    return groups


REVIEW_CATEGORIES = {'journal_research', 'journal_review', 'journal_commentary', 'journal_editorial',
    'magazine_article', 'conference_item', 'preprint', 'supplement', 'book', 'intellectual_property',
    'proceedings_volume', 'non_work', 'unknown', 'duplicate'}


def reviewed_counts(result, review, *, repo=REPO):
    """Count host-reviewed classifications only when current inventory/evidence agree.

    Quote checks validate provenance, not the semantics of the reviewer's conclusion.
    """
    if not isinstance(review, dict) or review.get('protocol') != 'author-works-review-v1' or review.get('inventory_digest') != result['inventory_digest']:
        raise ValueError('review protocol or inventory digest mismatch; recheck current sources')
    group_for = {rid: i for i, group in enumerate(result['groups']) for rid in group['ids']}
    decisions = review.get('decisions')
    if not isinstance(decisions, list):
        raise ValueError('review decisions must be a list')
    found, duplicate_targets = {}, {}
    for item in decisions:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not isinstance(item.get('category'), str):
            raise ValueError('each review decision needs a string id and category')
        group = group_for.get(item.get('id'))
        category = item.get('category')
        if group is None or group in found or category not in REVIEW_CATEGORIES:
            raise ValueError('unknown/duplicate review group or invalid category')
        evidence = item.get('evidence', [])
        if not isinstance(evidence, list):
            raise ValueError('review evidence must be a list')
        if category not in {'unknown', 'non_work'} and not evidence:
            raise ValueError('reviewed work classifications need Raw evidence')
        for fact in evidence:
            if not isinstance(fact, dict) or not isinstance(fact.get('locator'), str) or not isinstance(fact.get('quote'), str):
                raise ValueError('review evidence needs string locator and quote')
            locator, quote = fact.get('locator', ''), fact.get('quote', '')
            path, anchor = sl.split_locator(locator)
            if not anchor or not quote or not any(path.startswith(d + '/raw/') for d in ('academic','admin','teaching','cross-domain')):
                raise ValueError('review evidence needs a public Raw locator and quote')
            target = safe_path(Path(repo).resolve(), path, path.split('/')[0] + '/raw')
            if target.suffix not in {'.md', '.txt'}:
                raise ValueError('use managed text evidence, not binary extraction')
            binding = si.binding_for_target(Path(repo).resolve(), target)
            companion = sl.companion_binding_for_target(target)
            if (binding and binding['status'] != 'valid') or (companion and companion['status'] == 'invalid'):
                raise ValueError('invalid review evidence binding')
            text = sl.read_locator_text(target, anchor)
            if text is None or len(text) > 12000 or quote not in text:
                raise ValueError('review quote absent or evidence range oversized')
        found[group] = category
        if category == 'duplicate':
            if not isinstance(item.get('duplicate_of'), str):
                raise ValueError('duplicate_of must be a record id')
            target_group = group_for.get(item.get('duplicate_of'))
            if target_group is None or target_group == group:
                raise ValueError('invalid duplicate target')
            duplicate_targets[group] = target_group
    for group, target in duplicate_targets.items():
        if target not in found or found[target] in {'duplicate','unknown','non_work'}:
            raise ValueError('duplicate target must be a directly classified work')
    counts = dict(sorted(Counter(found.values()).items()))
    return {'categories': counts, 'reviewed_groups': len(found),
        'unreviewed_groups': len(result['groups']) - len(found),
        'journal_articles': sum(counts.get(c, 0) for c in REVIEW_CATEGORIES if c.startswith('journal_')),
        'journal_and_magazine_articles': sum(counts.get(c, 0) for c in REVIEW_CATEGORIES if c.startswith('journal_')) + counts.get('magazine_article', 0),
        'semantic_owner': 'caller_review', 'evidence_check': 'exact_quote_and_current_inventory',
        'boundary': 'Counts cover reviewed in-library records; quote validation is not semantic certification or worldwide completeness.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('author', help='canonical person path, exact name or registered alias')
    parser.add_argument('--repo', type=Path, default=REPO)
    parser.add_argument('--max-pages', type=int, default=MAX_PAGES)
    parser.add_argument('--source', action='append', default=[], help='explicitly located academic Raw source to check for a known coverage gap')
    parser.add_argument('--review', type=Path, help='optional source-bound caller classifications to aggregate')
    parser.add_argument('--compact', action='store_true', help='omit header text and per-page metadata; retain locators')
    args = parser.parse_args(argv)
    try:
        result = inventory(args.author, repo=args.repo, max_pages=args.max_pages, extra_sources=args.source)
        if args.review:
            result['reviewed_counts'] = reviewed_counts(result, json.loads(args.review.read_text(encoding='utf-8')), repo=args.repo)
        if args.compact:
            for record in result['records']:
                record.pop('header', None); record.pop('metadata_hints', None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({'ok': False, 'protocol': PROTOCOL, 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())

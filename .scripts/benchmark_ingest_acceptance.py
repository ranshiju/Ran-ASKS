#!/usr/bin/env python3
"""Offline ingestion acceptance. Reads evidence; never runs ingestion or judges science."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

SCHEMA = 'ingest-acceptance-manifest-v1'
TIMING = 'ingest-qualified-elapsed-v1'
BOUNDARY = 'accepted-input-through-runtime-completion'
CHECKS = ('bibliography', 'source_support', 'conditions', 'uncertainty', 'relations', 'downstream_answers')
ARTIFACTS = ('source', 'wiki', 'semantics', 'checklist')
MATCH = ('input_sha256', 'initial_graph_sha256', 'initial_cache_sha256', 'model_config_sha256',
         'parser_config_sha256', 'checklist_sha256', 'measurement_boundary')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read_evidence(root: Path, refs: dict):
    """Only manifest paths select files. Audit/source text never selects more files."""
    data, hashes, errors = {}, {}, []
    for role, ref in refs.items():
        try:
            relative = Path(ref['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('unsafe_path')
            path = root / relative
            if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
                raise ValueError('symlink')
            path.resolve().relative_to(root.resolve())
            raw = path.read_bytes()
            hashes[role] = digest(raw)
            if hashes[role] != ref['sha256']:
                errors.append('hash_mismatch:' + role)
            data[role] = json.loads(raw) if path.suffix == '.json' else {'binary_sha256': hashes[role]} if role == 'input' else raw.decode('utf-8')
        except (OSError, ValueError, KeyError, TypeError):
            errors.append('invalid_evidence:' + role)
    return data, hashes, errors


def request_ledger(events):
    """One row per provider attempt; retain failed attempts and unknown usage."""
    rows, seen, errors = [], {}, []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            errors.append('invalid_event'); continue
        if event.get('event_kind') != 'llm_api_call':
            continue
        identity = event.get('event_id')
        has_identity = isinstance(identity, str) and bool(identity)
        has_call = isinstance(event.get('call_id'), str) and bool(event.get('call_id')) and type(event.get('attempt')) is int
        key = identity if has_identity else (str(event.get('transaction_id')), event.get('call_id'),
                  str(event.get('profile')), event.get('attempt')) if has_call else ('unidentified', index)
        if key in seen:
            if seen[key] != event:
                errors.append('conflicting_duplicate_event')
            continue
        seen[key] = event
        usage = event.get('usage')
        valid_usage = isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                         for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
        if valid_usage and usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
            valid_usage = False
        if not has_identity and not has_call:
            errors.append('missing_attempt_identity')
        rows.append({k: event.get(k) for k in ('event_id', 'call_id', 'transaction_id', 'operation', 'model',
                                             'profile', 'attempt', 'status', 'latency_sec')})
        rows[-1]['event_id'] = identity if has_identity else None
        rows[-1]['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} if valid_usage else None
    return {'attempts': rows, 'attempt_count': len(rows),
            'failed_attempts': sum(r['status'] != 'ok' for r in rows),
            'retry_attempts': sum(type(r['attempt']) is int and r['attempt'] > 1 for r in rows),
            'known_tokens': {k: sum((r['usage'] or {}).get(k, 0) for r in rows)
                             for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
            'missing_usage_attempts': sum(r['usage'] is None for r in rows),
            'currency_cost': None, 'unmetered_services': ['pdf_extraction', 'embedding', 'host_review'],
            'errors': errors}


def assess(run: dict, data: dict, hashes: dict, integrity_errors=()):
    reasons = list(integrity_errors)
    def require(condition, reason):
        if not condition: reasons.append(reason)
    def obj(role):
        v = data.get(role)
        return v if isinstance(v, dict) else {}
    require(run.get('scope') == 'full_ingestion', 'not_full_ingestion')
    pipeline, timing, audit = obj('pipeline'), obj('timing'), obj('audit')
    maintenance = obj('maintenance')
    if 'maintenance' in maintenance: maintenance = maintenance['maintenance']
    if not isinstance(maintenance, dict): maintenance = {}
    txn = pipeline.get('transaction_id')
    require(pipeline.get('status') == 'completed', 'pipeline_not_completed')
    require(pipeline.get('quality_status') == 'complete', 'pipeline_quality_not_complete')
    require(not pipeline.get('agent_required') and not pipeline.get('errors') and
            not pipeline.get('quality_warnings'), 'pipeline_unresolved_issues')
    receipt = pipeline.get('verification_receipt') or {}
    require(isinstance(receipt, dict) and receipt.get('status') == 'PASS' and receipt.get('committed') is True
            and receipt.get('transaction_id') == txn, 'commit_receipt_not_passed')
    receipt_wiki = ((receipt.get('artifacts') or {}).get('wiki_path') or {}) if isinstance(receipt, dict) else {}
    require(receipt_wiki.get('sha256') == hashes.get('wiki') and 'wiki' in hashes, 'commit_wiki_unbound')
    require(pipeline.get('cleanup_pending') is False, 'cleanup_incomplete')
    frontier = pipeline.get('frontier_capture') or {}
    answers = frontier.get('answer_attempts') if isinstance(frontier, dict) else None
    require(isinstance(frontier, dict) and frontier.get('backend') == 'api' and frontier.get('answer_policy') == 'api'
            and not frontier.get('error') and not frontier.get('errors') and isinstance(answers, list)
            and type(frontier.get('count')) is int and len(answers) >= frontier['count']
            and all(isinstance(a, dict) and a.get('status') == 'completed' for a in answers), 'frontier_not_completed')
    require(maintenance.get('status') == 'completed', 'maintenance_not_completed')
    require(not maintenance.get('actions') and not maintenance.get('errors'), 'maintenance_unresolved_actions')
    require((maintenance.get('publication') or {}).get('status') == 'completed', 'publication_not_completed')
    components = maintenance.get('components') or {}
    for name in ('abbreviations', 'people', 'hubs'):
        item = components.get(name, {})
        require(isinstance(item, dict) and item.get('status') in ('completed', 'no_action')
                and not item.get('errors') and not item.get('error'), 'maintenance_component:' + name)
    triggers = maintenance.get('trigger_files') or []
    require(bool(txn) and any(isinstance(t, dict) and t.get('transaction_id') == txn for t in triggers),
            'maintenance_transaction_unbound')
    require(audit.get('scientific_pass') is True and not audit.get('issues'), 'scientific_quality_not_passed')
    checks = audit.get('checks') or {}
    for name in CHECKS:
        require(isinstance(checks, dict) and checks.get(name) == 'pass', 'quality_check_unpassed:' + name)
    bindings = dict(audit.get('artifact_sha256') or {})
    if 'source' not in bindings and audit.get('source_sha256'):
        bindings['source'] = audit['source_sha256']
    for name in ARTIFACTS:
        require(name in hashes and isinstance(bindings, dict) and bindings.get(name) == hashes[name],
                'audit_unbound:' + name)
    require(audit.get('transaction_id') == txn and bool(txn), 'audit_transaction_unbound')
    require(timing.get('schema') == TIMING and timing.get('boundary') == BOUNDARY,
            'measurement_boundary_incomplete')
    require(timing.get('transaction_id') == txn and bool(txn), 'timing_transaction_unbound')
    start, end, elapsed = timing.get('wall_start', obj('start').get('wall_start')), timing.get('wall_end'), timing.get('elapsed_sec')
    clocks_ok = all(number(v) for v in (start, end, elapsed)) and end >= start and abs(end - start - elapsed) <= 1
    require(clocks_ok and elapsed > 0, 'continuous_clock_unverified')
    review_end = audit.get('review_end_epoch')
    mode = audit.get('review_mode')
    if mode == 'external_read_only':
        require(clocks_ok and number(review_end) and review_end >= start and audit.get('artifacts_modified') is False,
                'external_review_not_read_only')
    else:
        require(mode == 'runtime' and clocks_ok and number(review_end) and start <= review_end <= end,
                'runtime_quality_review_outside_timer')
    require(timing.get('complete') is True, 'timer_not_closed_at_qualified_completion')
    require(timing.get('deducted_sec') == 0, 'time_deductions_not_allowed')
    require(timing.get('includes') == ['entry', 'extraction', 'generation', 'repairs', 'graph', 'frontier',
                                     'maintenance', 'publication', 'runtime_quality_checks', 'waiting'], 'timed_scope_incomplete')
    observed = elapsed if number(elapsed) else None
    events = data.get('events')
    if not isinstance(events, list): events = []
    ledger = request_ledger(events)
    reasons.extend(ledger['errors'])
    require(run.get('event_coverage') == 'complete', 'request_coverage_unverified')
    require(bool(events) and ledger['attempt_count'] > 0, 'request_log_missing')
    require(all(r['transaction_id'] == txn for r in ledger['attempts']) and bool(txn), 'request_transaction_unbound')
    context = run.get('context') or {}
    require(all(isinstance(context.get(k), str) and re.fullmatch(r'[0-9a-f]{64}', context[k])
                for k in MATCH if k != 'measurement_boundary'), 'comparison_context_missing')
    require(context.get('input_sha256') == hashes.get('input') and 'input' in hashes, 'input_context_mismatch')
    require(context.get('checklist_sha256') == hashes.get('checklist') and 'checklist' in hashes,
            'checklist_context_mismatch')
    require(context.get('measurement_boundary') == BOUNDARY, 'comparison_boundary_mismatch')
    return {'id': run['id'], 'case': run['case'], 'variant': run['variant'], 'scope': run.get('scope'),
            'qualified': not reasons, 'reasons': list(dict.fromkeys(reasons)),
            'evidence_integrity_verified': not integrity_errors,
            'observed_elapsed_sec': observed, 'qualified_elapsed_sec': observed if not reasons else None,
            'pipeline_status': pipeline.get('status'), 'maintenance_status': maintenance.get('status'),
            'scientific_pass': audit.get('scientific_pass'),
            'maintenance_components': {n: v.get('status') for n, v in components.items() if isinstance(v, dict)},
            'ledger': ledger, 'context': context}


def compare(results, expected_cases, threshold=0.70):
    pairs, reasons = [], []
    for case in expected_cases:
        group = [r for r in results if r['case'] == case]
        baseline = [r for r in group if r['variant'] == 'baseline']
        candidate = [r for r in group if r['variant'] == 'candidate']
        if len(baseline) != 1 or len(candidate) != 1:
            reasons.append(case + ':missing_or_multiple_pair'); continue
        b, c = baseline[0], candidate[0]
        if not b['qualified'] or not c['qualified']:
            reasons.append(case + ':unqualified_pair'); continue
        if any(b['context'].get(k) != c['context'].get(k) for k in MATCH):
            reasons.append(case + ':context_mismatch'); continue
        if not number(b.get('qualified_elapsed_sec')) or b['qualified_elapsed_sec'] <= 0 or not number(c.get('qualified_elapsed_sec')):
            reasons.append(case + ':invalid_duration'); continue
        ratio = c['qualified_elapsed_sec'] / b['qualified_elapsed_sec']
        pairs.append({'case': case, 'baseline': b['id'], 'candidate': c['id'], 'ratio': ratio,
                      'meets_threshold': ratio <= threshold})
    if any(r['case'] not in expected_cases for r in results): reasons.append('undeclared_case')
    return {'threshold': threshold, 'pairs': pairs, 'reasons': reasons,
            'target_accepted_for_declared_cases': bool(expected_cases) and not reasons and len(pairs) == len(expected_cases)
                and all(p['meets_threshold'] for p in pairs),
            'population_or_p95_claim_supported': False}


def evaluate(manifest: dict, root: Path):
    if manifest.get('schema') != SCHEMA: raise ValueError('Unknown acceptance manifest')
    expected = manifest.get('expected_cases')
    if not isinstance(expected, list) or not expected or any(not isinstance(c, str) or not c for c in expected) or len(set(expected)) != len(expected):
        raise ValueError('Declare unique, nonempty expected_cases')
    results, ids = [], set()
    for run in manifest['runs']:
        if run['id'] in ids: raise ValueError('Duplicate run id')
        ids.add(run['id'])
        data, hashes, errors = read_evidence(root, run['files'])
        results.append(assess(run, data, hashes, errors))
    # Reused log files must not turn one execution into two independent runs.
    owners = {}
    for result in results:
        for attempt in result['ledger']['attempts']:
            identity = attempt.get('event_id') or (attempt.get('call_id'), attempt.get('attempt'), attempt.get('profile'))
            if identity in owners and owners[identity] is not result:
                for affected in (owners[identity], result):
                    affected['qualified'] = False
                    affected['qualified_elapsed_sec'] = None
                    if 'attempt_reused_across_runs' not in affected['reasons']:
                        affected['reasons'].append('attempt_reused_across_runs')
            else:
                owners[identity] = result
    duplicate_attempts = any('attempt_reused_across_runs' in r['reasons'] for r in results)
    return {'schema': 'ingest-acceptance-report-v1', 'runs': results,
            'aggregate_cost_verified': not duplicate_attempts and all(not r['ledger']['errors'] and r['evidence_integrity_verified'] for r in results),
            'run_count': len(results), 'qualified_count': sum(r['qualified'] for r in results),
            'known_observed_work_sec': sum(r['observed_elapsed_sec'] or 0 for r in results),
            'unqualified_observed_work_sec': sum(r['observed_elapsed_sec'] or 0 for r in results if not r['qualified']),
            'duration_sum_is_not_single_document_latency': True,
            'request_attempts': sum(r['ledger']['attempt_count'] for r in results),
            'failed_requests': sum(r['ledger']['failed_attempts'] for r in results),
            'known_tokens': {k: sum(r['ledger']['known_tokens'][k] for r in results)
                             for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
            'missing_usage_attempts': sum(r['ledger']['missing_usage_attempts'] for r in results),
            'currency_cost': None, 'comparison': compare(results, expected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = evaluate(json.loads(args.manifest.read_text()), args.root.resolve())
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.resolve().relative_to((args.root.resolve() / 'temp').resolve())
        # Explicitly new report only; cannot overwrite the evidence being evaluated.
        with args.output.open('x', encoding='utf-8') as handle: handle.write(text)
    else: print(text, end='')


if __name__ == '__main__': main()

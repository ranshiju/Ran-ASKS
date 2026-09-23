#!/usr/bin/env python3
"""工程元图查询：capability、impact、验证映射和漂移检查。"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path
import yaml
REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / 'operations/engineering/graph.yaml'
IMPACT_SCHEMA = 'engineering-impact-v1'
MAX_IMPACT_RESULTS = 100
MAX_LIST_RESULTS = 100
GIT_DISCOVERY_TIMEOUT = 5
def load():
    data = yaml.safe_load(MANIFEST.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not all(k in data and data[k] for k in ('nodes', 'edges', 'capabilities')):
        raise ValueError(
            f'{MANIFEST} 结构不完整或为空（got {type(data).__name__}）。'
            '应为含 nodes/edges/capabilities 非空段的字典；节点是命名字典而非 "- id:" 列表，'
            '用 grep "  - id:" 等列表式 pattern 会误判为空。'
        )
    return (data['nodes'], data['edges'], data['capabilities'], data.get('contracts', []),
            data.get('verification', {}), data.get('script_contracts', {}), data.get('untracked', []))
def node_ref(nodes, node_id):
    node = nodes[node_id]
    return f"{node_id}: {node['path']} ({node['role']})"


class TargetResolutionError(ValueError):
    pass


class ChangeDiscoveryError(RuntimeError):
    pass


def _normalize_target_path(value):
    raw = str(value).strip().replace('\\', '/')
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = REPO / candidate
    try:
        return candidate.resolve().relative_to(REPO.resolve()).as_posix()
    except ValueError:
        return raw


def _unique_target(nodes, target, matches, match_kind):
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ', '.join(f"{node_id} ({nodes[node_id]['path']})" for node_id in matches)
        raise TargetResolutionError(
            f"目标 {target!r} 的{match_kind}不唯一；请改用 canonical node ID: {candidates}"
        )
    return None


def resolve_target(nodes, capabilities, target, *, allow_capability=False):
    """Resolve a CLI target to a registered canonical node or capability ID."""
    requested = str(target or '').strip()
    if allow_capability and requested in capabilities:
        return 'capability', requested
    if requested in nodes:
        return 'node', requested

    normalized = _normalize_target_path(requested)
    path_matches = [
        node_id for node_id, node in nodes.items()
        if _normalize_target_path(node.get('path', '')) == normalized
    ]
    resolved = _unique_target(nodes, requested, path_matches, '注册路径')
    if resolved:
        return 'node', resolved

    requested_path = requested.replace('\\', '/')
    if '/' not in requested_path:
        filename_matches = [
            node_id for node_id, node in nodes.items()
            if Path(str(node.get('path', ''))).name == requested_path
        ]
        resolved = _unique_target(nodes, requested, filename_matches, '文件名')
        if resolved:
            return 'node', resolved

    if '/' not in requested_path and not Path(requested_path).suffix:
        stem_matches = [
            node_id for node_id, node in nodes.items()
            if Path(str(node.get('path', ''))).stem == requested_path
        ]
        resolved = _unique_target(nodes, requested, stem_matches, 'stem')
        if resolved:
            return 'node', resolved

    accepted = 'canonical node ID、capability 或唯一已注册 path/文件名/stem' \
        if allow_capability else 'canonical node ID 或唯一已注册 path/文件名/stem'
    raise TargetResolutionError(f"未知工程目标 {requested!r}；可接受 {accepted}")


_GUIDANCE_CACHE = None
_GUIDANCE_LOCATOR_CACHE = None

def guidance_anchors(nodes):
    """解析 code-guidance.md，构建 {node_id: "§sec_num title"} 映射。

    按节点 path 字段匹配 section 标题中的脚本路径，运行时解析保证与文档同步。
    """
    global _GUIDANCE_CACHE
    if _GUIDANCE_CACHE is not None:
        return _GUIDANCE_CACHE
    import re
    cg_path = REPO / 'operations/engineering/code-guidance.md'
    anchors = {}
    if cg_path.exists():
        for line in cg_path.read_text(encoding='utf-8').splitlines():
            m = re.match(r'^#{2,4}\s+(\d+(?:\.\d+[a-z]?)?)\s+(.*)', line)
            if not m:
                continue
            sec_num, title = m.group(1), m.group(2)
            pm = re.search(r'`([^`]+\.(?:py|sh))`', title)
            if pm:
                script_path = pm.group(1)
                anchors.setdefault(script_path, f"§{sec_num}")
    result = {}
    for nid, node in nodes.items():
        p = node.get('path', '')
        if p in anchors:
            result[nid] = anchors[p]
    _GUIDANCE_CACHE = result
    return result


def guidance_locators(nodes):
    """Map graph nodes to exact code-guidance Markdown locators."""
    global _GUIDANCE_LOCATOR_CACHE
    if _GUIDANCE_LOCATOR_CACHE is not None:
        return _GUIDANCE_LOCATOR_CACHE
    from engineering_locator import markdown_blocks
    path = REPO / 'operations/engineering/code-guidance.md'
    result = {}
    if path.is_file():
        blocks = markdown_blocks(path)
        for node_id, node in nodes.items():
            source_path = str(node.get('path') or '')
            if not source_path:
                continue
            match = next((block for block in blocks if source_path in block.title), None)
            if match:
                result[node_id] = match.locator
    _GUIDANCE_LOCATOR_CACHE = result
    return result
def capability(nodes, capabilities, name, compact=False):
    if name not in capabilities: raise KeyError(name)
    cap=capabilities[name]
    keys=cap['required'] if compact else cap['required']+cap.get('optional', [])
    lines=[f"[工程上下文包] capability={name}", "读取顺序:"]
    lines += [f"- {node_ref(nodes,k)}" for k in keys]
    if cap.get('forbidden'): lines.append('禁止作为当前主入口: '+', '.join(node_ref(nodes,k) for k in cap['forbidden']))
    if cap.get('guardrails'):
        lines.append('任务卡（不可跳过）:')
        lines += [f"- {item}" for item in cap['guardrails']]
    return '\n'.join(lines)
def impact_distances(nodes, edges, seeds, max_depth=2):
    seed_set = set(seeds)
    unknown = sorted(seed_set - set(nodes))
    if unknown:
        raise KeyError(unknown[0])
    distances = {node_id: 0 for node_id in seed_set}
    frontier = set(seed_set)
    # 工程依赖不是业务因果链：规则改动可沿“实现/校验/文档”双向传播。
    # 两跳保留最小充分影响面，避免整张工程图被同一基础设施节点拉入。
    for depth in range(1, max_depth + 1):
        nxt = set()
        for source, _rel, dest in edges:
            if source in frontier and dest not in distances:
                nxt.add(dest)
            if dest in frontier and source not in distances:
                nxt.add(source)
        for node_id in nxt:
            distances[node_id] = depth
        frontier = nxt
        if not frontier:
            break
    return distances


def impacted_nodes(nodes, edges, target):
    return set(impact_distances(nodes, edges, [target]))


def _family_path_matches(pattern, path):
    pattern_parts = Path(pattern).parts
    path_parts = Path(path).parts
    if len(pattern_parts) != len(path_parts):
        return False
    return all(
        (part.startswith('<') and part.endswith('>')) or part == actual
        for part, actual in zip(pattern_parts, path_parts)
    )


def resolve_changed_path(nodes, path):
    """Resolve a repository-relative changed path without basename guessing."""
    normalized = _normalize_target_path(path)
    if Path(normalized).is_absolute() or normalized == '..' or normalized.startswith('../'):
        return None, normalized, 'outside_repository'

    exact = [
        node_id for node_id, node in nodes.items()
        if '<' not in str(node.get('path', ''))
        and _normalize_target_path(node.get('path', '')) == normalized
    ]
    if len(exact) == 1:
        return exact[0], normalized, None
    if len(exact) > 1:
        return None, normalized, 'ambiguous_registered_path'

    family = [
        node_id for node_id, node in nodes.items()
        if '<' in str(node.get('path', ''))
        and _family_path_matches(str(node.get('path')), normalized)
    ]
    if len(family) == 1:
        return family[0], normalized, None
    if len(family) > 1:
        return None, normalized, 'ambiguous_path_family'

    directory = [
        (len(str(node.get('path'))), node_id)
        for node_id, node in nodes.items()
        if str(node.get('path', '')).endswith('/')
        and normalized.startswith(str(node.get('path')))
    ]
    if directory:
        longest = max(length for length, _node_id in directory)
        matches = sorted(node_id for length, node_id in directory if length == longest)
        if len(matches) == 1:
            return matches[0], normalized, None
        return None, normalized, 'ambiguous_registered_directory'
    return None, normalized, 'unregistered_path'


def resolve_changed_files(nodes, paths):
    resolved = []
    unresolved = []
    for raw_path in sorted(set(str(path) for path in paths if str(path).strip())):
        node_id, normalized, reason = resolve_changed_path(nodes, raw_path)
        if node_id:
            resolved.append({'path': normalized, 'node_id': node_id})
        else:
            unresolved.append({'path': normalized, 'reason': reason})
    return resolved, unresolved


def _out_of_scope_reason(path):
    parts = Path(path).parts
    if not parts:
        return None
    if Path(path).suffix.lower() in {
            '.db', '.sqlite', '.doc', '.docx', '.pdf', '.png', '.jpg', '.jpeg',
            '.gif', '.ppt', '.pptx', '.xls', '.xlsx'}:
        return 'binary_or_state_artifact'
    if any(part in {'raw', 'wiki', 'temp', 'outputs'} for part in parts):
        return 'knowledge_or_temporary_content'
    if parts[0] in {
            'academic', 'admin', 'teaching', 'business', 'cross-domain',
            'inbox', 'memory', 'private', 'slide-library'}:
        return 'knowledge_or_user_state'
    if parts[0] == 'projects':
        return 'project_workspace_content'
    return None


def classify_changed_files(nodes, paths):
    """Separate engineering mappings from user/state paths and true graph gaps."""
    resolved = []
    unresolved = []
    out_of_scope = []
    for raw_path in sorted(set(str(path) for path in paths if str(path).strip())):
        node_id, normalized, reason = resolve_changed_path(nodes, raw_path)
        if node_id and nodes[node_id].get('kind') != 'data':
            resolved.append({'path': normalized, 'node_id': node_id})
            continue
        scope_reason = 'registered_state_node' if node_id else _out_of_scope_reason(normalized)
        if scope_reason:
            out_of_scope.append({'path': normalized, 'reason': scope_reason})
        else:
            unresolved.append({'path': normalized, 'reason': reason})
    return resolved, unresolved, out_of_scope


def _run_git(args, *, repo=REPO, timeout=GIT_DISCOVERY_TIMEOUT):
    try:
        proc = subprocess.run(
            ['git', *args], cwd=repo, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChangeDiscoveryError(
            f"git {' '.join(args)} 超过 {timeout}s；未把超时解释为无变化"
        ) from exc
    except OSError as exc:
        raise ChangeDiscoveryError(f'无法执行 git: {exc}') from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode('utf-8', errors='replace').strip()
        raise ChangeDiscoveryError(
            f"git {' '.join(args)} 失败（rc={proc.returncode}）: {detail or '无错误文本'}"
        )
    return proc.stdout


def _decode_git_paths(payload):
    try:
        return [part.decode('utf-8') for part in payload.split(b'\0') if part]
    except UnicodeDecodeError as exc:
        raise ChangeDiscoveryError('Git 变更路径不是 UTF-8，无法可靠映射工程节点') from exc


def discover_git_changes(kind, value=None, *, repo=REPO, timeout=GIT_DISCOVERY_TIMEOUT):
    diff_args = ['diff', '--name-only', '-z', '--diff-filter=ACDMRTUXB']
    if kind == 'staged':
        paths = _decode_git_paths(_run_git(
            [*diff_args[:1], '--cached', *diff_args[1:]], repo=repo, timeout=timeout,
        ))
        return sorted(set(paths)), {'kind': 'staged'}
    if kind == 'working-tree':
        unstaged = _decode_git_paths(_run_git(diff_args, repo=repo, timeout=timeout))
        staged = _decode_git_paths(_run_git(
            [*diff_args[:1], '--cached', *diff_args[1:]], repo=repo, timeout=timeout,
        ))
        untracked = _decode_git_paths(_run_git(
            ['ls-files', '--others', '--exclude-standard', '-z'], repo=repo, timeout=timeout,
        ))
        return sorted(set(unstaged + staged + untracked)), {'kind': 'working-tree'}
    if kind == 'base':
        base = str(value or '').strip()
        if not base:
            raise ChangeDiscoveryError('分支基线不能为空')
        merge_base = _run_git(
            ['merge-base', 'HEAD', base], repo=repo, timeout=timeout,
        ).decode('ascii', errors='strict').strip()
        if not merge_base:
            raise ChangeDiscoveryError(f'HEAD 与 {base!r} 没有可用 merge-base')
        paths = _decode_git_paths(_run_git(
            [*diff_args, f'{merge_base}...HEAD'], repo=repo, timeout=timeout,
        ))
        return sorted(set(paths)), {
            'kind': 'base', 'base': base, 'merge_base': merge_base,
        }
    raise ChangeDiscoveryError(f'未知 Git 变更发现模式: {kind}')


def engineering_locator_entries(nodes, node_ids):
    """Emit filtered discovery only for files lacking a known exact locator."""
    entries = []
    for node_id in sorted(node_ids):
        raw_path = str(nodes.get(node_id, {}).get('path') or '')
        if not raw_path or '<' in raw_path or '>' in raw_path:
            continue
        relative = Path(raw_path)
        if any(part in {'raw', 'wiki'} for part in relative.parts):
            continue
        target = REPO / relative
        if not target.is_file() or target.suffix.lower() in {'.db', '.pdf', '.docx', '.png'}:
            continue
        if target.suffix.lower() == '.py':
            prefix = 'py:'
        elif target.suffix.lower() in {'.md', '.markdown'}:
            prefix = 'md:'
        elif target.suffix.lower() in {'.yaml', '.yml'}:
            prefix = 'yaml:/'
        else:
            continue
        entries.append(
            f"- {node_id}: python3 .scripts/engineering_locator.py list "
            f"{shlex.quote(raw_path)} --prefix {shlex.quote(prefix)}"
        )
    return entries


def recommended_locator_entries(nodes, node_ids, target, capabilities, script_contracts):
    """Emit exact locators that follow mechanically from engineering metadata."""
    entries = []
    graph_path = 'operations/engineering/graph.yaml'
    if target in nodes:
        entries.append(("工程节点", graph_path, f"yaml:/nodes/{target}"))
    if target in script_contracts:
        entries.append(("脚本契约", graph_path, f"yaml:/script_contracts/{target}"))
    if target in capabilities:
        entries.append(("能力包", graph_path, f"yaml:/capabilities/{target}"))
    for node_id, locator in sorted(guidance_locators(nodes).items()):
        if node_id in node_ids:
            entries.append((f"{node_id} 指南", 'operations/engineering/code-guidance.md', locator))
    rendered = []
    seen = set()
    for label, path, locator in entries:
        value = f"{path}#{locator}"
        if value in seen:
            continue
        seen.add(value)
        rendered.append(
            f"- {label}: python3 .scripts/engineering_locator.py read {shlex.quote(value)}"
        )
    return rendered


def _bounded(values, limit=MAX_LIST_RESULTS):
    values = list(values)
    return values[:limit], max(0, len(values) - limit)


def _optional_git_text(args):
    try:
        return _run_git(args).decode('utf-8').strip()
    except (ChangeDiscoveryError, UnicodeDecodeError):
        return None


def analysis_basis():
    manifest_hash = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    head = _optional_git_text(['rev-parse', 'HEAD'])
    dirty = _optional_git_text([
        'status', '--porcelain=v1', '--untracked-files=no', '--',
        MANIFEST.relative_to(REPO).as_posix(),
    ])
    return {
        'manifest': MANIFEST.relative_to(REPO).as_posix(),
        'manifest_sha256': manifest_hash,
        'manifest_dirty': None if dirty is None else bool(dirty),
        'git_head': head,
    }


def verification_entries(node_ids, verification):
    by_command = {}
    for node_id in sorted(node_ids):
        for command in verification.get(node_id, []):
            by_command.setdefault(command, []).append(node_id)
    return [
        {'command': command, 'origin_nodes': origins}
        for command, origins in by_command.items()
    ]


def verification_commands(node_ids, verification):
    return [entry['command'] for entry in verification_entries(node_ids, verification)]


def _locator_commands(nodes, node_ids, targets, capabilities, script_contracts):
    commands = []
    seen = set()
    for target in targets:
        for command in recommended_locator_entries(
                nodes, node_ids, target, capabilities, script_contracts):
            if command not in seen:
                seen.add(command)
                commands.append(command)
    return commands


def _structured_command_entries(entries):
    result = []
    for entry in entries:
        value = entry[2:] if entry.startswith('- ') else entry
        label, separator, command = value.partition(': ')
        result.append({
            'label': label if separator else '',
            'command': command if separator else value,
        })
    return result


def build_impact_result(
        nodes, edges, capabilities, seed_nodes, *, source, target=None,
        changed_files=(), resolved_files=(), unresolved_files=(), out_of_scope_files=(),
        verification=None, script_contracts=None, max_results=MAX_IMPACT_RESULTS):
    if not 1 <= max_results <= MAX_IMPACT_RESULTS:
        raise ValueError(
            f'max_results 必须在 1..{MAX_IMPACT_RESULTS}，不能解除工程上下文硬上限'
        )
    seed_nodes = sorted(set(seed_nodes))
    distances = impact_distances(nodes, edges, seed_nodes) if seed_nodes else {}
    ordered_ids = sorted(distances, key=lambda node_id: (distances[node_id], node_id))
    visible_ids, nodes_omitted = _bounded(ordered_ids, max_results)

    changed_files = sorted(set(changed_files))
    resolved_files = list(resolved_files)
    unresolved_files = list(unresolved_files)
    out_of_scope_files = list(out_of_scope_files)
    visible_changed, changed_omitted = _bounded(changed_files)
    visible_resolved, resolved_omitted = _bounded(resolved_files)
    visible_unresolved, unresolved_omitted = _bounded(unresolved_files)
    visible_out_of_scope, out_of_scope_omitted = _bounded(out_of_scope_files)
    visible_seeds, seeds_omitted = _bounded(seed_nodes)

    direct = []
    related = []
    direct_omitted = related_omitted = 0
    if verification is not None:
        direct_all = verification_entries(seed_nodes, verification)
        direct_commands = {entry['command'] for entry in direct_all}
        related_all = [
            entry for entry in verification_entries(
                [node_id for node_id in distances if node_id not in seed_nodes],
                verification,
            )
            if entry['command'] not in direct_commands
        ]
        direct, direct_omitted = _bounded(direct_all)
        related, related_omitted = _bounded(related_all)

    status = 'partial' if unresolved_files else 'ok'
    if not changed_files and source.get('kind') != 'target':
        reason = 'no_changed_files'
    elif changed_files and not seed_nodes and not unresolved_files:
        reason = 'no_engineering_changes'
    elif changed_files and not seed_nodes:
        reason = 'no_registered_changed_files'
    elif seed_nodes and len(distances) == len(seed_nodes):
        reason = 'registered_targets_have_no_graph_neighbors'
    elif unresolved_files:
        reason = 'changed_files_only_partially_registered'
    else:
        reason = 'registered_graph_traversal_completed'

    script_contracts = script_contracts or {}
    locator_targets = [target] if target else seed_nodes
    recommended = _locator_commands(
        nodes, visible_ids, locator_targets, capabilities, script_contracts,
    )
    exact_paths = {
        line.split(' read ', 1)[1].split('#', 1)[0].strip("'")
        for line in recommended if ' read ' in line
    }
    discovery = [
        line for line in engineering_locator_entries(nodes, visible_ids)
        if not any(path in line for path in exact_paths)
    ]
    visible_recommended, recommended_omitted = _bounded(recommended)
    visible_discovery, discovery_omitted = _bounded(discovery)
    list_truncated = any((
        changed_omitted, resolved_omitted, unresolved_omitted,
        out_of_scope_omitted, seeds_omitted, nodes_omitted,
        direct_omitted, related_omitted,
        recommended_omitted, discovery_omitted,
    ))
    result = {
        'schema': IMPACT_SCHEMA,
        'status': status,
        'reason': reason,
        'source': source,
        'analysis_basis': analysis_basis(),
        'completeness': {
            'changed_files_total': len(changed_files),
            'changed_files_returned': len(visible_changed),
            'changed_files_omitted': changed_omitted,
            'resolved_files_total': len(resolved_files),
            'resolved_files_returned': len(visible_resolved),
            'resolved_files_omitted': resolved_omitted,
            'unresolved_files_total': len(unresolved_files),
            'unresolved_files_returned': len(visible_unresolved),
            'unresolved_files_omitted': unresolved_omitted,
            'out_of_scope_files_total': len(out_of_scope_files),
            'out_of_scope_files_returned': len(visible_out_of_scope),
            'out_of_scope_files_omitted': out_of_scope_omitted,
            'seed_nodes_total': len(seed_nodes),
            'seed_nodes_returned': len(visible_seeds),
            'seed_nodes_omitted': seeds_omitted,
            'affected_nodes_total': len(ordered_ids),
            'affected_nodes_returned': len(visible_ids),
            'affected_nodes_omitted': nodes_omitted,
            'direct_verifications_omitted': direct_omitted,
            'related_verifications_omitted': related_omitted,
            'recommended_locators_omitted': recommended_omitted,
            'discovery_entries_omitted': discovery_omitted,
            'truncated': list_truncated,
        },
        'limits': {
            'affected_nodes': max_results,
            'hard_max_affected_nodes': MAX_IMPACT_RESULTS,
            'other_lists': MAX_LIST_RESULTS,
        },
        'changed_files': visible_changed,
        'resolved_files': visible_resolved,
        'unresolved_files': visible_unresolved,
        'out_of_scope_files': visible_out_of_scope,
        'seed_nodes': visible_seeds,
        'affected_nodes': [
            {
                'id': node_id,
                'path': nodes[node_id]['path'],
                'role': nodes[node_id]['role'],
                'distance': distances[node_id],
                'seed': node_id in seed_nodes,
            }
            for node_id in visible_ids
        ],
        'verification': {
            'direct': direct,
            'related': related,
            'note': 'related 来自影响节点，只表示回归相关性，不证明 seed 节点被直接覆盖。',
        } if verification is not None else None,
        'recommended_locators': _structured_command_entries(visible_recommended),
        'discovery': _structured_command_entries(visible_discovery),
        'limitations': [
            '工程元图是人工治理的责任图，不是完整调用图。',
            '影响面固定为双向两跳；未注册变更必须人工归类或补图。',
        ],
    }
    return result


def render_impact(result, nodes):
    source = result['source']
    if source['kind'] == 'target':
        target = source['target']
        if source['target_kind'] == 'capability':
            lines = [f"[建设影响面] capability={target}（required 节点）"]
        else:
            lines = [f"[建设影响面] {node_ref(nodes, target)}"]
    else:
        lines = [f"[建设变更影响面] source={source['kind']} status={result['status']}"]

    completeness = result['completeness']
    lines.append(
        '完整性: '
        f"changed={completeness['changed_files_total']} "
        f"resolved={completeness['resolved_files_total']} "
        f"unresolved={completeness['unresolved_files_total']} "
        f"out_of_scope={completeness['out_of_scope_files_total']} "
        f"seeds={completeness['seed_nodes_total']} "
        f"affected={completeness['affected_nodes_total']} "
        f"returned={completeness['affected_nodes_returned']} "
        f"omitted={completeness['affected_nodes_omitted']} "
        f"truncated={str(completeness['truncated']).lower()}"
    )
    basis = result['analysis_basis']
    lines.append(
        f"分析基线: manifest_sha256={basis['manifest_sha256']} "
        f"manifest_dirty={basis['manifest_dirty']} git_head={basis['git_head'] or 'unavailable'}"
    )
    lines.append(f"结果原因: {result['reason']}")

    if source['kind'] != 'target':
        if result['changed_files']:
            lines.append('变更文件:')
            lines += [f"- {path}" for path in result['changed_files']]
        if result['resolved_files']:
            lines.append('已映射工程节点:')
            lines += [f"- {item['path']} -> {item['node_id']}" for item in result['resolved_files']]
        if result['unresolved_files']:
            lines.append('未注册变更（影响分析不完整）:')
            lines += [
                f"- {item['path']} ({item['reason']})"
                for item in result['unresolved_files']
            ]
        if result['out_of_scope_files']:
            lines.append('工程审查范围外（仅记录路径，不读取内容）:')
            lines += [
                f"- {item['path']} ({item['reason']})"
                for item in result['out_of_scope_files']
            ]

    anchors = guidance_anchors(nodes)
    target_node = source.get('target') if source.get('target_kind') == 'node' else None
    for item in result['affected_nodes']:
        node_id = item['id']
        if node_id == target_node:
            continue
        ref = f"- {node_ref(nodes, node_id)}"
        if node_id in anchors:
            ref += f"  → code-guidance {anchors[node_id]}"
        if source['kind'] != 'target' and item['seed']:
            ref += '  [seed]'
        lines.append(ref)

    if result['recommended_locators']:
        lines.append('推荐精确 locator（先直接 read）:')
        lines += [
            f"- {entry['label']}: {entry['command']}"
            for entry in result['recommended_locators']
        ]
    if result['discovery']:
        lines += [
            '过滤发现入口（推荐 locator 不足时）:',
            '- Python symbol 不由 impact 猜测；用 rg 确定符号关键词后进一步收紧 `--prefix py:<symbol>`。',
        ]
        lines += [
            f"- {entry['label']}: {entry['command']}"
            for entry in result['discovery']
        ]
    verification = result.get('verification')
    if verification is not None:
        lines.append('直接验证（seed 节点声明）:')
        lines += [f"- {entry['command']}" for entry in verification['direct']] or ['- 无']
        lines.append('相关验证（影响节点带入，不代表直接覆盖）:')
        lines += [f"- {entry['command']}" for entry in verification['related']] or ['- 无']
    lines.append('交付：检查上述规范/实现/测试；工程文档若受影响由 agent 自主判断并同步更新。')
    return '\n'.join(lines)


def impact(nodes, edges, capabilities, target, verification=None, script_contracts=None,
           max_results=MAX_IMPACT_RESULTS):
    if target in capabilities:
        target_kind = 'capability'
        seed_nodes = capabilities[target]['required']
    else:
        target_kind = 'node'
        seed_nodes = [target]
    result = build_impact_result(
        nodes, edges, capabilities, seed_nodes,
        source={'kind': 'target', 'target': target, 'target_kind': target_kind},
        target=target, verification=verification, script_contracts=script_contracts,
        max_results=max_results,
    )
    return render_impact(result, nodes)


def script_contract(nodes, contracts, node_id):
    if node_id not in nodes:
        raise KeyError(node_id)
    if node_id not in contracts:
        return f"[脚本契约] {node_ref(nodes, node_id)}\n- 尚无专用契约：按 capability、命中规范和实际实现补查。"
    contract = contracts[node_id]
    lines = [f"[脚本契约] {node_ref(nodes, node_id)}"]
    labels = (("前置", "preconditions"), ("可写", "writes"), ("禁止", "forbidden"), ("成功后验证", "verify"))
    for label, key in labels:
        values = contract.get(key, [])
        lines.append(f"- {label}: {'；'.join(values) if values else '无'}")
    return '\n'.join(lines)


def contract_failures(nodes, capabilities, contracts):
    failures = []
    for contract in contracts:
        for node_id in contract.get('nodes', []):
            if node_id not in nodes:
                failures.append(f"unknown contract node: {contract.get('id')}/{node_id}")
    checks = {check for contract in contracts for check in contract.get('checks', [])}
    if 'graph_entrypoints' in checks:
        source = (REPO / '.scripts/graph_ingest.py').read_text(encoding='utf-8')
        for marker in ('def cmd_ingest(', 'def cmd_init(', 'graph.db 主数据化'):
            if marker not in source:
                failures.append(f"graph ingest entrypoint drift: missing '{marker}'")
    if 'ingest_capability_contract' in checks:
        required = {'agents', 'ingest', 'schemas', 'graph_ingest', 'ingest_check'}
        if not required.issubset(set(capabilities.get('ingest', {}).get('required', []))):
            failures.append('ingest capability missing required closure nodes')
    if 'raw_redline_text' in checks:
        agents = (REPO / 'AGENTS.md').read_text(encoding='utf-8')
        markers = ('Agent 不直接修改 `raw/`', 'Raw 写入只经受管摄入事务')
        if not all(marker in agents for marker in markers):
            failures.append('raw redline drift: AGENTS.md no longer declares managed Raw writes')
    if 'function_registry_coverage' in checks:
        import function_registry
        registry_errors = function_registry.validate_runtime()
        failures.extend(f"function registry: {error}" for error in registry_errors)
        if registry_errors:
            return failures
        try:
            tasks, on_demand = function_registry.registered_route_surface()
        except function_registry.RegistryError as exc:
            failures.append(f"function registry: {exc}")
            return failures
        registered = tasks | on_demand
        missing_tasks = sorted(tasks - set(capabilities))
        missing_on_demand = sorted(on_demand - set(capabilities))
        extra = sorted(set(capabilities) - registered)
        bad_entries = sorted(
            name for name, cap in capabilities.items() if cap.get('entry') != 'route'
        )
        if missing_tasks:
            failures.append(f"registered Route task missing capability: {missing_tasks}")
        if missing_on_demand:
            failures.append(
                f"registered on-demand route missing capability: {missing_on_demand}"
            )
        if extra:
            failures.append(f"capability missing registered Route binding: {extra}")
        if bad_entries:
            failures.append(f"capability entry is not route: {bad_entries}")
    return failures


def orphan_scripts(nodes, untracked):
    """孤儿脚本检测：.scripts 下未登记脚本(warning)与已登记但磁盘消失脚本(error)。"""
    on_disk = {p.stem for p in (REPO / '.scripts').glob('*.py')}
    registered = {Path(n['path']).stem for n in nodes.values()
                  if n['path'].startswith('.scripts/') and n['path'].endswith('.py')}
    whitelist = set(untracked or [])
    missing = sorted(registered - on_disk)
    orphan = sorted(on_disk - registered - whitelist)
    errors = [f'registered script missing from disk: .scripts/{m}.py' for m in missing]
    warnings = [f'unregistered script: .scripts/{o}.py（未在 graph.yaml 登记；'
                f'叶子工具加入 untracked 白名单，否则登记为 implementation 节点）' for o in orphan]
    return errors, warnings


def public_assets_drift():
    """公开副本漂移检测：operations/engineering/ 下的 public_assets 镜像须与正本同步。
    根目录副本（如 .gitignore）可能有意分叉，不纳入。"""
    manifest_path = REPO / 'operations/engineering/open-source-manifest.yaml'
    if not manifest_path.exists():
        return []
    manifest = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
    warnings = []
    for dest, src in manifest.get('public_assets', {}).items():
        if not dest.startswith('operations/engineering/') or dest == src:
            continue
        orig, copy = REPO / dest, REPO / src
        if not orig.exists() or not copy.exists():
            continue
        if orig.read_text(encoding='utf-8') != copy.read_text(encoding='utf-8'):
            warnings.append(f'public_assets 副本与正本不同步：{src} 应与 {dest} 一致（用正本覆盖副本）')
    return warnings


def apply_forget(target, dry_run=False):
    data = yaml.safe_load(MANIFEST.read_text(encoding='utf-8'))
    nodes = data['nodes']
    if target not in nodes:
        raise KeyError(target)
    edges = data['edges']
    removed_edges = [[s, r, d] for s, r, d in edges if s == target or d == target]
    removed_caps = [f'{n}.{k}' for n, c in data.get('capabilities', {}).items()
                    for k in ('required', 'optional', 'forbidden') if target in c.get(k, [])]
    removed_verif = target in data.get('verification', {})
    removed_sc = target in data.get('script_contracts', {})
    removed_contracts = [c.get('id') for c in data.get('contracts', []) if target in c.get('nodes', [])]
    print(f'[forget] 节点 {target} ({nodes[target]["path"]})')
    print(f'  删除边 {len(removed_edges)}: ' + (', '.join(f'{s}--{r}-->{d}' for s, r, d in removed_edges) or '无'))
    if removed_caps:
        print(f'  从 capability 列表移除: {", ".join(removed_caps)}')
    if removed_verif:
        print(f'  从 verification 移除: {target}')
    if removed_sc:
        print(f'  从 script_contracts 移除: {target}')
    if removed_contracts:
        print(f'  从 contracts.nodes 移除: {", ".join(removed_contracts)}')
    if dry_run:
        print('  (--dry-run，未写回)')
        return
    del nodes[target]
    data['edges'] = [[s, r, d] for s, r, d in edges if s != target and d != target]
    for cap in data.get('capabilities', {}).values():
        for k in ('required', 'optional', 'forbidden'):
            if k in cap:
                cap[k] = [x for x in cap[k] if x != target]
    data.get('verification', {}).pop(target, None)
    data.get('script_contracts', {}).pop(target, None)
    for c in data.get('contracts', []):
        if 'nodes' in c:
            c['nodes'] = [x for x in c['nodes'] if x != target]
    MANIFEST.write_text(
        yaml.dump(data, allow_unicode=True, width=100000, sort_keys=False,
                  default_flow_style=False, indent=2), encoding='utf-8')
    print(f'  已写回 {MANIFEST}；请运行 python3 .scripts/engineering_graph.py validate 复核')


def validate(nodes, edges, capabilities, contracts=(), verification=None, script_contracts=None, untracked=None):
    failures=[]
    warnings=[]
    for key,node in nodes.items():
        path=node['path']
        if '<' not in path and not node.get('optional', False) and not (REPO/path).exists(): failures.append(f'missing path: {key} -> {path}')
    for source,_,dest in edges:
        if source not in nodes or dest not in nodes: failures.append(f'unknown edge endpoint: {source}->{dest}')
    for name, cap in capabilities.items():
        for key in cap['required']+cap.get('optional',[])+cap.get('forbidden',[]):
            if key not in nodes: failures.append(f'unknown capability node: {name}/{key}')
    if verification is not None:
        for node_id in verification:
            if node_id not in nodes:
                failures.append(f'unknown verification node: {node_id}')
    if script_contracts is not None:
        for node_id, contract in script_contracts.items():
            if node_id not in nodes:
                failures.append(f'unknown script contract node: {node_id}')
            for field in ('preconditions', 'writes', 'forbidden', 'verify'):
                if field not in contract or not isinstance(contract[field], list):
                    failures.append(f'invalid script contract: {node_id}/{field}')
    failures.extend(contract_failures(nodes, capabilities, contracts))
    errs, warns = orphan_scripts(nodes, untracked)
    failures.extend(errs)
    warnings.extend(warns)
    warnings.extend(public_assets_drift())
    return failures, warnings
def main():
    parser=argparse.ArgumentParser(description='WikiRan 工程元图查询')
    sub=parser.add_subparsers(dest='command', required=True)
    pc=sub.add_parser('capability'); pc.add_argument('node'); pc.add_argument('--compact', action='store_true')
    pi=sub.add_parser('impact')
    pi.add_argument('node', nargs='?')
    pi.add_argument('--verify', action='store_true')
    pi.add_argument('--format', choices=('text', 'json'), default='text', dest='output_format')
    pi.add_argument('--max-results', type=int, default=MAX_IMPACT_RESULTS)
    source_group = pi.add_mutually_exclusive_group()
    source_group.add_argument('--files', nargs='+', metavar='PATH')
    source_group.add_argument('--working-tree', action='store_true')
    source_group.add_argument('--staged', action='store_true')
    source_group.add_argument('--base', metavar='REF')
    for name in ('status', 'contract'):
        p=sub.add_parser(name); p.add_argument('node')
    sub.add_parser('validate')
    pf=sub.add_parser('forget'); pf.add_argument('node'); pf.add_argument('--dry-run', action='store_true')
    args=parser.parse_args()
    try:
        nodes,edges,capabilities,contracts,verification,script_contracts,untracked=load()
    except ValueError as exc:
        print(f'ERROR: {exc}',file=sys.stderr); sys.exit(1)
    failures,warnings=validate(nodes,edges,capabilities,contracts,verification,script_contracts,untracked)
    if warnings:
        print('\n'.join('WARN: '+w for w in warnings),file=sys.stderr)
    if failures:
        print('\n'.join('ERROR: '+f for f in failures),file=sys.stderr); sys.exit(1)
    try:
        if args.command=='capability': print(capability(nodes,capabilities,args.node,args.compact))
        elif args.command=='impact':
            source_selected = bool(args.files or args.working_tree or args.staged or args.base)
            if bool(args.node) == source_selected:
                pi.error('必须且只能提供一个单目标，或 --files/--working-tree/--staged/--base 之一')
            if not 1 <= args.max_results <= MAX_IMPACT_RESULTS:
                pi.error(f'--max-results 必须在 1..{MAX_IMPACT_RESULTS}')

            if args.node:
                target_kind, target = resolve_target(
                    nodes, capabilities, args.node, allow_capability=True)
                seed_nodes = capabilities[target]['required'] if target_kind == 'capability' else [target]
                result = build_impact_result(
                    nodes, edges, capabilities, seed_nodes,
                    source={'kind': 'target', 'target': target, 'target_kind': target_kind},
                    target=target,
                    verification=verification if args.verify else None,
                    script_contracts=script_contracts,
                    max_results=args.max_results,
                )
            else:
                if args.files:
                    changed_files = sorted(set(args.files))
                    source = {'kind': 'files'}
                elif args.working_tree:
                    changed_files, source = discover_git_changes('working-tree')
                elif args.staged:
                    changed_files, source = discover_git_changes('staged')
                else:
                    changed_files, source = discover_git_changes('base', args.base)
                resolved, unresolved, out_of_scope = classify_changed_files(
                    nodes, changed_files,
                )
                normalized_changed_files = [
                    item['path'] for item in [*resolved, *unresolved, *out_of_scope]
                ]
                result = build_impact_result(
                    nodes, edges, capabilities,
                    [item['node_id'] for item in resolved],
                    source=source,
                    changed_files=normalized_changed_files,
                    resolved_files=resolved,
                    unresolved_files=unresolved,
                    out_of_scope_files=out_of_scope,
                    verification=verification if args.verify else None,
                    script_contracts=script_contracts,
                    max_results=args.max_results,
                )
            if args.output_format == 'json':
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(render_impact(result, nodes))
        elif args.command=='contract':
            _kind, target = resolve_target(nodes, capabilities, args.node)
            print(script_contract(nodes,script_contracts,target))
        elif args.command=='status':
            _kind, target = resolve_target(nodes, capabilities, args.node)
            print(node_ref(nodes,target))
        elif args.command=='forget':
            _kind, target = resolve_target(nodes, capabilities, args.node)
            apply_forget(target, args.dry_run)
        else: print(f'工程元图有效: {len(nodes)} 节点, {len(edges)} 边, {len(capabilities)} 能力包, {len(contracts)} 契约')
    except TargetResolutionError as exc:
        print(f'ERROR: {exc}',file=sys.stderr); sys.exit(2)
    except ChangeDiscoveryError as exc:
        if getattr(args, 'output_format', 'text') == 'json':
            print(json.dumps({
                'schema': IMPACT_SCHEMA,
                'status': 'error',
                'reason': 'change_discovery_failed',
                'error': str(exc),
            }, ensure_ascii=False, indent=2))
        else:
            print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyError as exc:
        print(f'ERROR: 未知节点/能力 {exc.args[0]}',file=sys.stderr); sys.exit(2)
if __name__=='__main__': main()

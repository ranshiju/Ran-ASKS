#!/usr/bin/env python3
"""工程元图查询：capability、impact、验证映射和漂移检查。"""
import argparse
import ast
import shlex
import sys
from pathlib import Path
import yaml
REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / 'operations/engineering/graph.yaml'
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
def impacted_nodes(nodes, edges, target):
    if target not in nodes: raise KeyError(target)
    seen={target}; frontier={target}
    # 工程依赖不是业务因果链：规则改动可沿“实现/校验/文档”双向传播。
    # 两跳保留最小充分影响面，避免整张工程图被同一基础设施节点拉入。
    for _depth in range(2):
        nxt=set()
        for source, rel, dest in edges:
            if source in frontier and dest not in seen:
                nxt.add(dest)
            if dest in frontier and source not in seen:
                nxt.add(source)
        seen |= nxt; frontier=nxt
        if not frontier:
            break
    return seen


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


def impact(nodes, edges, capabilities, target, verification=None, script_contracts=None):
    anchors = guidance_anchors(nodes)
    if target in capabilities:
        capability_nodes = capabilities[target]['required']
        seen = set()
        for node_id in capability_nodes:
            seen |= impacted_nodes(nodes, edges, node_id)
        lines = [f"[建设影响面] capability={target}（required 节点）"]
    else:
        seen = impacted_nodes(nodes, edges, target)
        lines = [f"[建设影响面] {node_ref(nodes,target)}"]
    for k in sorted(seen):
        if k == target:
            continue
        ref = f"- {node_ref(nodes,k)}"
        if k in anchors:
            ref += f"  → code-guidance {anchors[k]}"
        lines.append(ref)
    script_contracts = script_contracts or {}
    recommended = recommended_locator_entries(
        nodes, seen, target, capabilities, script_contracts,
    )
    if recommended:
        lines += [
            '推荐精确 locator（先直接 read）:',
            *recommended,
        ]
    exact_paths = {
        line.split(' read ', 1)[1].split('#', 1)[0].strip("'")
        for line in recommended if ' read ' in line
    }
    discovery = [line for line in engineering_locator_entries(nodes, seen)
                 if not any(path in line for path in exact_paths)]
    if discovery:
        lines += [
            '过滤发现入口（推荐 locator 不足时）:',
            '- Python symbol 不由 impact 猜测；用 rg 确定符号关键词后进一步收紧 `--prefix py:<symbol>`。',
            *discovery,
        ]
    if verification is not None:
        commands = verification_commands(seen, verification)
        lines.append('最小验证:')
        lines += [f"- {command}" for command in commands]
    lines.append('交付：检查上述规范/实现/测试；工程文档若受影响由 agent 自主判断并同步更新。')
    return '\n'.join(lines)


def verification_commands(node_ids, verification):
    commands = []
    for node_id in sorted(node_ids):
        for command in verification.get(node_id, []):
            if command not in commands:
                commands.append(command)
    return commands


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


def route_tasks():
    route_path = REPO / '.scripts/route.py'
    tree = ast.parse(route_path.read_text(encoding='utf-8'))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'ROUTES' for target in statement.targets
        ):
            routes = ast.literal_eval(statement.value)
            return set(routes)
    raise ValueError('route.py 中未找到字面量 ROUTES')


def contract_failures(nodes, capabilities, contracts):
    failures = []
    for contract in contracts:
        for node_id in contract.get('nodes', []):
            if node_id not in nodes:
                failures.append(f"unknown contract node: {contract.get('id')}/{node_id}")
    checks = {check for contract in contracts for check in contract.get('checks', [])}
    if 'route_task_coverage' in checks:
        tasks = route_tasks()
        missing = sorted(tasks - set(capabilities))
        extra = sorted(set(capabilities) - tasks)
        bad_entries = sorted(name for name, cap in capabilities.items() if cap.get('entry') != 'route')
        if missing:
            failures.append(f"route task missing capability: {missing}")
        if extra:
            failures.append(f"capability missing route task: {extra}")
        if bad_entries:
            failures.append(f"capability entry is not route: {bad_entries}")
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
        if '绝不修改 raw/' not in agents:
            failures.append('raw redline drift: AGENTS.md no longer declares raw immutability')
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
    for name in ('capability','impact','status', 'contract'):
        p=sub.add_parser(name); p.add_argument('node')
        if name=='capability': p.add_argument('--compact', action='store_true')
        if name=='impact': p.add_argument('--verify', action='store_true')
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
        elif args.command=='impact': print(impact(
            nodes, edges, capabilities, args.node,
            verification if args.verify else None, script_contracts,
        ))
        elif args.command=='contract': print(script_contract(nodes,script_contracts,args.node))
        elif args.command=='status': print(node_ref(nodes,args.node))
        elif args.command=='forget': apply_forget(args.node, args.dry_run)
        else: print(f'工程元图有效: {len(nodes)} 节点, {len(edges)} 边, {len(capabilities)} 能力包, {len(contracts)} 契约')
    except KeyError as exc:
        print(f'ERROR: 未知节点/能力 {exc.args[0]}',file=sys.stderr); sys.exit(2)
if __name__=='__main__': main()

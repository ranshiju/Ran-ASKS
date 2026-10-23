"""Read-only discovery of scientific code capabilities and pinned evidence."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path

import yaml
import source_locator


REPO = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO / "computing/code-library/graph.yaml"
MAX_CATALOG_BYTES = 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_EXCERPT_CHARS = 8000
MAX_CONTEXT_RELATIONS = 64
NODE_KINDS = {"physical_system", "problem", "model", "method", "algorithm",
              "implementation", "experiment", "validation"}
RELATIONS = {
    "describes": ({"model"}, {"physical_system"}),
    "formulates": ({"model"}, {"problem"}),
    "addresses": ({"method", "algorithm"}, {"problem"}),
    "uses": ({"algorithm", "implementation"}, {"method", "algorithm", "model"}),
    "implements": ({"implementation"}, {"method", "algorithm", "model"}),
    "approximates": ({"algorithm"}, {"model"}),
    "depends_on": ({"implementation"}, {"implementation"}),
    "evaluates": ({"experiment"}, {"implementation"}),
    "checks": ({"validation"}, {"implementation", "algorithm", "model"}),
}
ID_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CatalogError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise CatalogError("目录键必须是唯一字符串")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def text_field(value, label, maximum=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CatalogError(f"{label} 必须是非空、有限长度字符串")
    return value


def id_field(value, label):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise CatalogError(f"{label} 不是有效稳定 ID")
    return value


def string_list(value, label, maximum=64):
    if not isinstance(value, list) or len(value) > maximum:
        raise CatalogError(f"{label} 必须是有限列表")
    for item in value:
        text_field(item, label, 200)
    if len(set(value)) != len(value):
        raise CatalogError(f"{label} 不允许重复")
    return value


def public_path(value, kind):
    text_field(value, "evidence.path", 500)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\\" in value:
        raise CatalogError("证据路径必须是仓库相对路径，不允许穿越")
    parts = relative.parts
    shared = parts[:2] == ("computing", "code-library")
    project = len(parts) >= 4 and parts[0] == "projects" and parts[2] == "codes"
    if kind == "raw":
        allowed = parts[:2] == ("academic", "raw") and relative.suffix == ".md"
    elif kind in {"code", "test"}:
        allowed = (shared or project) and relative.suffix == ".py"
    elif kind == "run":
        allowed = (len(parts) == 6 and parts[0] == "projects" and parts[2:4] == ("outputs", "code-runs")
                   and parts[-1] == "metadata.json")
    else:
        allowed = (shared or project) and relative.suffix == ".md"
    if not allowed:
        raise CatalogError("证据不在公共科研代码或 academic Raw 允许范围")
    current = REPO
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise CatalogError("证据路径不允许软链接")
    try:
        current.resolve().relative_to(REPO.resolve())
    except ValueError:
        raise CatalogError("证据路径越出仓库") from None
    return current


def validate_structure(data):
    if not isinstance(data, dict) or set(data) != {"schema", "scope", "nodes", "relations", "evidence"}:
        raise CatalogError("目录顶层必须包含 schema/scope/nodes/relations/evidence")
    if data["schema"] != "scientific-code-library-v1" or data["scope"] != "public_working_catalog":
        raise CatalogError("只支持 scientific-code-library-v1 公共工作目录")
    nodes, relations, evidence = data["nodes"], data["relations"], data["evidence"]
    if not isinstance(nodes, dict) or not nodes or len(nodes) > 2000:
        raise CatalogError("nodes 必须是 1–2000 个命名对象")
    if not isinstance(evidence, dict) or len(evidence) > 4000:
        raise CatalogError("evidence 必须是有限命名字典")
    if not isinstance(relations, list) or len(relations) > 8000:
        raise CatalogError("relations 必须是有限列表")
    for evidence_id, record in evidence.items():
        id_field(evidence_id, "evidence ID")
        if not isinstance(record, dict) or set(record) != {"kind", "title", "path", "locator", "sha256"}:
            raise CatalogError(f"{evidence_id}: 无效证据字段")
        if not isinstance(record["kind"], str) or record["kind"] not in {"raw", "code", "doc", "test", "run"}:
            raise CatalogError(f"{evidence_id}: 无效证据类型")
        text_field(record["title"], "evidence.title", 200)
        locator = text_field(record["locator"], "evidence.locator", 200)
        if locator == "全篇" or (record["kind"] in {"code", "test"} and not locator.startswith("py:")):
            raise CatalogError(f"{evidence_id}: 必须使用精确 locator")
        if record["kind"] == "run" and locator not in {"json:/validation", "json:/parameters", "json:/source_bindings"}:
            raise CatalogError(f"{evidence_id}: 运行回执只接受固定 JSON pointer")
        if not isinstance(record["sha256"], str) or not HASH_PATTERN.fullmatch(record["sha256"]):
            raise CatalogError(f"{evidence_id}: 必须绑定 SHA-256")
        public_path(record["path"], record["kind"])
    for node_id, node in nodes.items():
        id_field(node_id, "node ID")
        if not isinstance(node, dict) or set(node) != {"kind", "title", "summary", "aliases", "status", "evidence"}:
            raise CatalogError(f"{node_id}: 无效节点字段")
        if (not isinstance(node["kind"], str) or node["kind"] not in NODE_KINDS
                or not isinstance(node["status"], str) or node["status"] not in {"available", "planned"}):
            raise CatalogError(f"{node_id}: 无效类型或可用性状态")
        text_field(node["title"], "node.title", 200)
        text_field(node["summary"], "node.summary")
        string_list(node["aliases"], "node.aliases")
        references = string_list(node["evidence"], "node.evidence")
        if any(reference not in evidence for reference in references):
            raise CatalogError(f"{node_id}: 未知证据引用")
        if node["kind"] == "implementation" and node["status"] == "available":
            if not any(evidence[reference]["kind"] == "code" for reference in references):
                raise CatalogError(f"{node_id}: 可用实现必须有代码定位")
    seen = set()
    for relation in relations:
        if not isinstance(relation, dict) or set(relation) != {"source", "predicate", "target", "status", "scope", "evidence"}:
            raise CatalogError("关系字段无效")
        source, target, predicate = relation["source"], relation["target"], relation["predicate"]
        if not isinstance(source, str) or not isinstance(target, str) or source not in nodes or target not in nodes:
            raise CatalogError("关系端点不存在")
        if not isinstance(predicate, str) or predicate not in RELATIONS:
            raise CatalogError("未知关系类型")
        source_kinds, target_kinds = RELATIONS[predicate]
        if nodes[source]["kind"] not in source_kinds or nodes[target]["kind"] not in target_kinds:
            raise CatalogError("关系端点类型不匹配")
        if not isinstance(relation["status"], str) or relation["status"] not in {"proposed", "documented"}:
            raise CatalogError("关系仅支持 proposed/documented，不接受科学正确性标签")
        text_field(relation["scope"], "relation.scope")
        references = string_list(relation["evidence"], "relation.evidence")
        if any(reference not in evidence for reference in references):
            raise CatalogError("关系引用未知证据")
        if relation["status"] == "documented" and not references:
            raise CatalogError("documented 关系必须有定位依据")
        identity = (source, predicate, target, relation["scope"])
        if identity in seen:
            raise CatalogError("重复关系")
        seen.add(identity)


def load_catalog():
    expected = REPO / "computing/code-library/graph.yaml"
    if CATALOG_PATH != expected:
        raise CatalogError("不接受任意目录路径")
    for candidate in (expected, expected.parent, expected.parent.parent):
        if candidate.is_symlink():
            raise CatalogError("目录不允许软链接")
    if not expected.is_file() or expected.stat().st_size > MAX_CATALOG_BYTES:
        raise CatalogError("目录缺失或超过大小预算")
    text = expected.read_text(encoding="utf-8")
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(text)):
        raise CatalogError("目录不允许 YAML anchor/alias")
    data = yaml.load(text, Loader=UniqueLoader)
    validate_structure(data)
    return data


def python_excerpt(text, locator):
    symbol = locator.removeprefix("py:")
    matches = []

    def visit(body, prefix=""):
        for definition in body:
            if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f"{prefix}.{definition.name}" if prefix else definition.name
                if symbol == qualified or ("." not in symbol and definition.name == symbol):
                    matches.append(definition)
                visit(definition.body, qualified)

    visit(ast.parse(text).body)
    if len(matches) != 1:
        raise CatalogError("Python 符号不存在或存在歧义")
    definition = matches[0]
    start = min([definition.lineno] + [decorator.lineno for decorator in definition.decorator_list])
    end = definition.end_lineno
    return "\n".join(text.splitlines()[start - 1:end]), start, end


def inspect_evidence(evidence_id, record, include_content=False):
    result = {"evidence_id": evidence_id, **record, "citation_eligible": False,
              "semantic_verification": "not_performed"}
    path = public_path(record["path"], record["kind"])
    if not path.is_file():
        return {**result, "status": "missing"}
    if path.stat().st_size > MAX_SOURCE_BYTES:
        return {**result, "status": "over_budget"}
    payload = path.read_bytes()
    actual_hash = hashlib.sha256(payload).hexdigest()
    result["actual_sha256"] = actual_hash
    if actual_hash != record["sha256"]:
        return {**result, "status": "stale"}
    try:
        text = payload.decode("utf-8")
        if record["kind"] == "run":
            import code_run
            inspection = code_run.inspect_record(path)
            run = inspection["record"]
            result.update(run_id=run["run_id"], recorded_validation_status=run["status"],
                          scientific_status=run["scientific_status"], dependencies=run["dependencies"],
                          parameters=run["parameters"], snapshot_integrity=inspection["snapshot_integrity"])
            if inspection["status"] != "current":
                return {**result, "status": "stale", "source_changes": inspection["source_changes"],
                        "dependency_changes": inspection["dependency_changes"]}
            excerpt = json.dumps(run[record["locator"].removeprefix("json:/")], ensure_ascii=False, allow_nan=False)
        elif record["kind"] in {"code", "test"}:
            excerpt, start, end = python_excerpt(text, record["locator"])
            result.update(start_line=start, end_line=end)
        else:
            if source_locator.locator_status(record["locator"], path) != "present":
                raise CatalogError("文档定位失效")
            excerpt = source_locator.read_locator_text(path, record["locator"])
            if not excerpt:
                raise CatalogError("文档定位无内容")
        if hashlib.sha256(path.read_bytes()).hexdigest() != actual_hash:
            return {**result, "status": "stale"}
    except (ValueError, SyntaxError, UnicodeError, OSError) as exc:
        return {**result, "status": "invalid_run_record" if record["kind"] == "run" else "invalid_locator", "error": str(exc)[:200]}
    result.update(status="current", citation_eligible=record["kind"] == "raw")
    if include_content:
        if len(excerpt) > MAX_EXCERPT_CHARS:
            return {**result, "status": "excerpt_over_budget", "citation_eligible": False}
        result["content"] = excerpt
        result["content_sha256"] = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
        result["evidence_role"] = {"raw": "source_statement", "run": "local_run_record"}.get(record["kind"], "implementation_context")
    return result


def response(action, **values):
    return {"schema": "scientific-code-context-v1", "action": action,
            "catalog_status": "working_catalog_not_published", "semantic_verification": "not_performed",
            "execution_performed": False, "sources": [], **values}


def node_context(data, node_id):
    node = data["nodes"][node_id]
    evidence = [inspect_evidence(reference, data["evidence"][reference]) for reference in node["evidence"]]
    unhealthy = any(item["status"] != "current" for item in evidence)
    return {"node_id": node_id, **node, "evidence": evidence,
            "availability": "needs_review" if unhealthy else node["status"]}


def validate_catalog(data):
    records = [inspect_evidence(reference, record) for reference, record in data["evidence"].items()]
    failures = [record for record in records if record["status"] != "current"]
    return response("validate", ok=not failures, node_count=len(data["nodes"]),
                    relation_count=len(data["relations"]), evidence_count=len(records), failures=failures,
                    notice="机械校验不证明科学解释正确、测试已运行或论文已复现")


def search(data, query, topk=8):
    text_field(query, "query", 500)
    if not isinstance(topk, int) or not 1 <= topk <= 20:
        raise CatalogError("topk 必须在 1–20 之间")
    terms = query.casefold().split()
    candidates = []
    for node_id, node in data["nodes"].items():
        names = [node_id, node["title"], *node["aliases"]]
        origin_titles = [data["evidence"][reference]["title"] for reference in node["evidence"]]
        corpus = " ".join([*names, node["summary"], *origin_titles]).casefold()
        score = sum(term in corpus for term in terms) / len(terms)
        if query.casefold() in [name.casefold() for name in names]:
            score += 2
        if score:
            candidates.append((score, node_id))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    hits = [{**node_context(data, node_id), "score": score} for score, node_id in candidates[:topk]]
    return response("search", ok=True, query=query, candidates=hits,
                    count=len(hits), truncated=len(candidates) > topk, identity_claim=False,
                    next="context <node_id>；再按 evidence_id 精确 read；工作目录不是事实源")


def context(data, node_id, depth=1, limit=20):
    if node_id not in data["nodes"]:
        raise CatalogError("未知节点")
    if depth not in {0, 1, 2} or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise CatalogError("depth 必须为 0–2，limit 必须为 1–50")
    selected, frontier, truncated = {node_id}, {node_id}, False
    for _ in range(depth):
        neighbors = set()
        for relation in data["relations"]:
            if relation["source"] in frontier:
                neighbors.add(relation["target"])
            if relation["target"] in frontier:
                neighbors.add(relation["source"])
        candidates = sorted(neighbors - selected)
        remaining = limit - len(selected)
        truncated |= len(candidates) > remaining
        frontier = set(candidates[:remaining])
        selected.update(frontier)
    relations = []
    for relation in data["relations"]:
        if relation["source"] in selected and relation["target"] in selected:
            if len(relations) >= MAX_CONTEXT_RELATIONS:
                truncated = True
                break
            records = [inspect_evidence(reference, data["evidence"][reference]) for reference in relation["evidence"]]
            relations.append({**relation, "evidence": records,
                              "evidence_status": "current" if all(record["status"] == "current" for record in records) else "needs_review"})
    return response("context", ok=True, root=node_id, nodes=[node_context(data, key) for key in sorted(selected)],
                    relations=relations, truncated=truncated,
                    notice="documented 表示有定位依据，不表示科学结论已验证；验证状态不跨关系传播")


def read_evidence(data, evidence_id):
    if evidence_id not in data["evidence"]:
        raise CatalogError("未知证据 ID；不能直接提供任意文件路径")
    record = inspect_evidence(evidence_id, data["evidence"][evidence_id], include_content=True)
    current = record["status"] == "current"
    sources = [f"{record['path']}#{record['locator']}"] if current and record["citation_eligible"] else []
    return response("read", ok=current, evidence=record, sources=sources,
                    error="" if current else f"证据不可读取: {record['status']}")


def parser():
    argument_parser = argparse.ArgumentParser(description="公共科研代码能力元图，只读且不执行代码")
    commands = argument_parser.add_subparsers(dest="action", required=True)
    commands.add_parser("validate")
    search_parser = commands.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--topk", type=int, default=8)
    context_parser = commands.add_parser("context")
    context_parser.add_argument("node_id")
    context_parser.add_argument("--depth", type=int, default=1)
    context_parser.add_argument("--limit", type=int, default=20)
    read_parser = commands.add_parser("read")
    read_parser.add_argument("evidence_id")
    return argument_parser


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        data = load_catalog()
        if args.action == "validate":
            result = validate_catalog(data)
        elif args.action == "search":
            result = search(data, args.query, args.topk)
        elif args.action == "context":
            result = context(data, args.node_id, args.depth, args.limit)
        else:
            result = read_evidence(data, args.evidence_id)
    except (CatalogError, OSError, UnicodeError, yaml.YAMLError) as exc:
        result = response(args.action, ok=False, error=str(exc)[:500])
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

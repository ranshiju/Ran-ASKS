#!/usr/bin/env python3
"""内存文档子图、主图对齐计划、query 探针与 SAVEPOINT 融合。

GraphDelta 是一次摄入的逻辑事务对象，不是新的事实源或永久数据库。它只用代码：
先描述本篇文档子图，再对主图做确定性名称解析和小型查询探针，最后由调用方提供
实际 writer，在 SQLite SAVEPOINT 中原子融合。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections import deque
import hashlib
import json
import re
from typing import Callable, Iterable

import graph_lib as gl
import node_semantics as ns


GRAPH_DELTA_CONTRACT_VERSION = "graph-delta-v1"
GRAPH_DELTA_VALIDATOR_VERSION = "graph-delta-validator-v1"


@dataclass
class GraphDelta:
    page: str
    title: str
    raw_packages: list[str]
    edges: list[dict]
    boundary_mentions: list[str]
    deterministic_edges: int
    semantic_edges: int
    concept_glosses: list[dict] = field(default_factory=list)
    canonical_endpoints: list[str] = field(default_factory=list)
    deterministic_metadata_endpoints: dict[str, str] = field(default_factory=dict)
    hard_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class DeltaContractError(ValueError):
    """GraphDelta 的最小硬结构不满足，禁止进入融合。"""


def _content_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clean_edge(edge: dict, page: str) -> tuple[dict | None, str | None]:
    subject = str(edge.get("subject") or "").strip()
    predicate = str(edge.get("predicate") or "").strip()
    obj = str(edge.get("object") or "").strip()
    if subject == "本论文":
        subject = page
    if obj == "本论文":
        obj = page
    if not subject or not predicate or not obj:
        return None, f"空三元组字段: {subject or '-'} | {predicate or '-'} | {obj or '-'}"
    if subject == obj:
        return None, f"自环三元组: {subject} | {predicate} | {obj}"
    cleaned = dict(edge)
    cleaned.update({"subject": subject, "predicate": predicate, "object": obj})
    cleaned["subject_is_canonical"] = bool(cleaned.get("subject_is_canonical"))
    cleaned["object_is_canonical"] = bool(cleaned.get("object_is_canonical"))
    cleaned["subject_metadata_kind"] = str(cleaned.get("subject_metadata_kind") or "").strip()
    cleaned["object_metadata_kind"] = str(cleaned.get("object_metadata_kind") or "").strip()
    cleaned["source"] = str(cleaned.get("source") or cleaned.get("locator") or "").strip()
    cleaned.pop("locator", None)
    return cleaned, None


def build_document_delta(
    page: str,
    frontmatter: dict,
    triples: Iterable[dict],
    deterministic_triple_count: int = 0,
    concept_glosses: Iterable[dict] | None = None,
) -> GraphDelta:
    """构造纯内存文档子图；只检查端点、来源骨架和完全重复边。"""
    page = str(page or "").removesuffix(".md").strip()
    title = str((frontmatter or {}).get("title") or page.rsplit("/", 1)[-1]).strip()
    hard_errors = []
    if not page:
        hard_errors.append("缺 Wiki page anchor")

    raw_packages = []
    local_sources = 0
    unmapped_local_sources = []
    for source in gl.parse_list_field(frontmatter or {}, "sources"):
        source_text = str(source or "").split("#", 1)[0].strip()
        if not source_text or source_text.startswith(("http://", "https://", "synology://")):
            continue
        local_sources += 1
        raw_path = gl.raw_node_path(source, page)
        if raw_path:
            if raw_path not in raw_packages:
                raw_packages.append(raw_path)
        else:
            unmapped_local_sources.append(source_text)
    if local_sources and not raw_packages:
        hard_errors.append("本地 sources 无法形成 Raw 文档包")
    elif unmapped_local_sources:
        hard_errors.append("部分本地 sources 无法形成 Raw 文档包: " + ", ".join(unmapped_local_sources))

    source_edges = [
        {
            "subject": page,
            "predicate": "来源",
            "object": raw_path,
            "source": "",
            "origin": "raw_source_skeleton",
            "subject_is_canonical": True,
            "object_is_canonical": True,
        }
        for raw_path in raw_packages
    ]

    cleaned_triples = []
    seen = set()
    for edge in triples or []:
        cleaned, error = _clean_edge(edge, page)
        if error:
            hard_errors.append(error)
            continue
        key = (cleaned["subject"], cleaned["predicate"], cleaned["object"])
        if key in seen:
            continue
        seen.add(key)
        cleaned_triples.append(cleaned)

    all_edges = source_edges + cleaned_triples
    raw_set = set(raw_packages)
    mentions = []
    endpoint_modes: dict[str, list[bool]] = {}
    metadata_modes: dict[str, set[str]] = {}
    for edge in cleaned_triples:
        for role in ("subject", "object"):
            endpoint = edge[role]
            metadata_kind = str(edge.get(f"{role}_metadata_kind") or "").strip()
            if metadata_kind:
                metadata_modes.setdefault(endpoint, set()).add(metadata_kind)
            if endpoint == page or endpoint in raw_set or endpoint in mentions:
                if endpoint != page and endpoint not in raw_set:
                    endpoint_modes.setdefault(endpoint, []).append(
                        bool(edge.get(f"{role}_is_canonical"))
                    )
                continue
            mentions.append(endpoint)
            endpoint_modes.setdefault(endpoint, []).append(
                bool(edge.get(f"{role}_is_canonical"))
            )

    # 同一字符串只要有一次来自 surface mention，就不能被整体提升为 canonical ID。
    canonical_endpoints = [
        endpoint for endpoint in mentions
        if endpoint_modes.get(endpoint) and all(endpoint_modes[endpoint])
    ]
    deterministic_metadata_endpoints = {
        endpoint: next(iter(kinds))
        for endpoint, kinds in metadata_modes.items() if len(kinds) == 1
    }

    cleaned_glosses = []
    seen_glosses = set()
    for gloss in concept_glosses or []:
        mention = str(gloss.get("mention") or gloss.get("name") or "").strip()
        description = str(gloss.get("description") or "").strip()
        source = str(gloss.get("source") or "").strip()
        if not mention or not description or not source:
            continue
        key = (mention, source)
        if key in seen_glosses:
            continue
        seen_glosses.add(key)
        cleaned_glosses.append({
            "mention": mention,
            "description": description,
            "source": source,
        })

    deterministic_triple_count = max(0, min(int(deterministic_triple_count), len(cleaned_triples)))
    return GraphDelta(
        page=page,
        title=title,
        raw_packages=raw_packages,
        edges=all_edges,
        boundary_mentions=mentions,
        deterministic_edges=len(source_edges) + deterministic_triple_count,
        semantic_edges=len(cleaned_triples) - deterministic_triple_count,
        concept_glosses=cleaned_glosses,
        canonical_endpoints=canonical_endpoints,
        deterministic_metadata_endpoints=deterministic_metadata_endpoints,
        hard_errors=hard_errors,
    )


def knowledge_edges(delta: GraphDelta) -> list[dict]:
    """返回交给知识边 writer 的规范化三元组，不重复写 Raw 来源骨架。"""
    return [
        dict(edge)
        for edge in delta.edges
        if edge.get("origin") != "raw_source_skeleton"
    ]


def _exact_candidates(name: str, title_idx: dict, alias_idx: dict, suffix_idx: dict) -> list[str]:
    candidates = []
    for index in (title_idx, alias_idx, suffix_idx):
        for path in index.get(name, []):
            if path not in candidates:
                candidates.append(path)
    return candidates


def _mention_context(delta: GraphDelta, mention: str) -> str:
    lines = [
        gloss["description"] for gloss in delta.concept_glosses
        if gloss.get("mention") == mention and gloss.get("description")
    ]
    for edge in knowledge_edges(delta):
        if mention not in (edge["subject"], edge["object"]):
            continue
        lines.append(f"{edge['subject']} {edge['predicate']} {edge['object']}")
    return "；".join(lines[:4])


def _mention_node_types(delta: GraphDelta, mention: str) -> list[str] | None:
    """普通知识端点默认是 entity；纯引用标题保留 page/entity 双类型解析。"""
    related = [
        edge for edge in knowledge_edges(delta)
        if mention in (edge.get("subject"), edge.get("object"))
    ]
    if related and all(edge.get("predicate") == "引用" for edge in related):
        return None
    return ["entity"]


PROPOSITION_PREDICATES = {"核心创新点", "局限性", "未来展望"}


def _mention_is_proposition(delta: GraphDelta, mention: str) -> bool:
    """Return whether the edge contract assigns this mention proposition identity."""
    for edge in knowledge_edges(delta):
        predicate = edge.get("predicate")
        if edge.get("object") == mention and predicate in PROPOSITION_PREDICATES:
            return True
        if edge.get("subject") == mention and predicate == "包含":
            return True
    return False


def _proposition_candidates(conn, candidates: list[str]) -> list[str]:
    """Keep exact candidates that are already proposition nodes."""
    return [
        candidate for candidate in candidates
        if (row := conn.execute(
            "SELECT entity_subtype FROM nodes WHERE path=?", (candidate,)
        ).fetchone()) is not None and row["entity_subtype"] == "proposition"
    ]


def _venue_identity_key(value: str) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"^(?:proceedings|findings)\s+of\s+", "", text)
    text = re.sub(r"^the\s+", "", text)
    text = re.sub(r"\s*,?\s*(?:pages?|pp\.)\s+\S+.*$", "", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _deterministic_metadata_candidates(conn, mention: str, kind: str) -> list[str]:
    if kind != "venue":
        return []
    key = _venue_identity_key(mention)
    if not key:
        return []
    rows = conn.execute(
        "SELECT path,title FROM nodes WHERE type='entity' AND COALESCE(entity_subtype,'') IN ('','venue')"
    ).fetchall()
    return sorted({
        row["path"] for row in rows
        if _venue_identity_key(row["title"] or row["path"]) == key
    })


def plan_attachment(conn, delta: GraphDelta) -> dict:
    """形成只读 attach plan；显式 ID 与 surface mention 使用不同解析入口。"""
    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    decisions = []
    merge_map = {}
    ambiguous = []
    new_nodes = []
    for mention in delta.boundary_mentions:
        metadata_kind = delta.deterministic_metadata_endpoints.get(mention)
        if metadata_kind:
            candidates = _exact_candidates(mention, title_idx, alias_idx, suffix_idx)
            if not candidates:
                candidates = _deterministic_metadata_candidates(conn, mention, metadata_kind)
            if len(candidates) == 1:
                target = candidates[0]
                decisions.append({
                    "mention": mention,
                    "action": "reuse_deterministic_metadata",
                    "target": target,
                    "metadata_kind": metadata_kind,
                    "candidate_count": 1,
                })
                merge_map[mention] = target
            elif len(candidates) > 1:
                item = {
                    "mention": mention,
                    "action": "abstain_ambiguous_metadata",
                    "metadata_kind": metadata_kind,
                    "candidates": candidates,
                    "candidate_count": len(candidates),
                }
                decisions.append(item)
                ambiguous.append(item)
            else:
                decisions.append({
                    "mention": mention,
                    "action": "create_deterministic_metadata",
                    "target": mention,
                    "metadata_kind": metadata_kind,
                    "reason": "locked_frontmatter_metadata",
                })
                new_nodes.append(mention)
            continue
        if mention in delta.canonical_endpoints:
            resolution = ns.resolve_node_id(conn, mention)
            if resolution.get("decision") == "resolved":
                decisions.append({
                    "mention": mention,
                    "action": "reuse_canonical_id",
                    "target": mention,
                    "candidate_count": 1,
                })
                merge_map[mention] = mention
            else:
                decisions.append({
                    "mention": mention,
                    "action": "abstain_missing_canonical",
                    "candidates": [],
                    "candidate_count": 0,
                    "reason": resolution.get("reason", "canonical_id_not_found"),
                })
            continue
        if _mention_is_proposition(delta, mention):
            candidates = _proposition_candidates(
                conn, _exact_candidates(mention, title_idx, alias_idx, suffix_idx))
            if len(candidates) == 1:
                target = candidates[0]
                decisions.append({
                    "mention": mention,
                    "action": "reuse_unique_proposition",
                    "target": target,
                    "candidate_count": 1,
                })
                merge_map[mention] = target
            elif len(candidates) > 1:
                item = {
                    "mention": mention,
                    "action": "abstain_ambiguous_proposition",
                    "candidates": candidates,
                    "candidate_count": len(candidates),
                }
                decisions.append(item)
                ambiguous.append(item)
            else:
                decisions.append({
                    "mention": mention,
                    "action": "create_local_proposition",
                    "target": mention,
                    "reason": "proposition_identity_preserved",
                })
                new_nodes.append(mention)
            continue
        candidates = _exact_candidates(mention, title_idx, alias_idx, suffix_idx)
        if len(candidates) == 1:
            resolved = candidates[0]
            decisions.append({
                "mention": mention,
                "action": "reuse_unique",
                "target": resolved,
                "candidate_count": 1,
            })
            merge_map[mention] = resolved
        elif len(candidates) > 1:
            resolution = ns.resolve_node(
                conn, mention, _mention_context(delta, mention),
                node_types=_mention_node_types(delta, mention), top_k=5
            )
            if resolution.get("decision") == "resolved":
                target = resolution["node_id"]
                decisions.append({
                    "mention": mention,
                    "action": "reuse_identity",
                    "target": target,
                    "match_mode": resolution.get("match_mode", "context_disambiguated"),
                    "reason": resolution.get("reason", ""),
                    "candidate_count": len(candidates),
                })
                merge_map[mention] = target
                continue
            item = {
                "mention": mention,
                "action": "abstain_ambiguous",
                "candidates": candidates,
                "candidate_count": len(candidates),
            }
            decisions.append(item)
            ambiguous.append(item)
        else:
            resolution = ns.resolve_node(
                conn, mention, _mention_context(delta, mention),
                node_types=_mention_node_types(delta, mention), top_k=5
            )
            if resolution.get("decision") == "resolved":
                target = resolution["node_id"]
                semantic_candidates = resolution.get("candidates", [])
                decisions.append({
                    "mention": mention,
                    "action": "reuse_identity",
                    "target": target,
                    "match_mode": resolution.get("match_mode", "dual_view_identity"),
                    "reason": resolution.get("reason", ""),
                    "candidate_count": max(1, len(semantic_candidates)),
                })
                merge_map[mention] = target
            elif resolution.get("decision") == "ambiguous":
                semantic_candidates = resolution.get("candidates", [])
                item = {
                    "mention": mention,
                    "action": "keep_local_ambiguous",
                    "target": mention,
                    "candidates": [candidate.get("node_id", "") for candidate in semantic_candidates],
                    "candidate_count": len(semantic_candidates),
                    "reason": resolution.get("reason", "semantic_identity_ambiguous"),
                }
                decisions.append(item)
                ambiguous.append(item)
                new_nodes.append(mention)
            else:
                decisions.append({
                    "mention": mention,
                    "action": "create_local",
                    "target": mention,
                    "reason": resolution.get("reason", "no_identity_candidate"),
                })
                new_nodes.append(mention)
    return {
        "decisions": decisions,
        "merge_map": merge_map,
        "new_nodes": new_nodes,
        "ambiguous": ambiguous,
        "abstained": [
            item["mention"] for item in decisions
            if item["action"].startswith("abstain")
        ],
        "counts": {
            "reuse": len(merge_map),
            "new": len(new_nodes),
            "ambiguous": len(ambiguous),
            "abstained": sum(item["action"].startswith("abstain") for item in decisions),
        },
    }


def _mapped_endpoint(endpoint: str, delta: GraphDelta, attach_plan: dict) -> str:
    if endpoint == delta.page or endpoint in delta.raw_packages:
        return endpoint
    if endpoint in attach_plan["merge_map"]:
        return attach_plan["merge_map"][endpoint]
    if endpoint in attach_plan["abstained"]:
        return f"local:{endpoint}"
    return endpoint


def _overlay_adjacency(delta: GraphDelta, attach_plan: dict) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in delta.edges:
        subject = _mapped_endpoint(edge["subject"], delta, attach_plan)
        obj = _mapped_endpoint(edge["object"], delta, attach_plan)
        adjacency.setdefault(subject, set()).add(obj)
        adjacency.setdefault(obj, set()).add(subject)
    return adjacency


def _reachable_within(conn, start: str, target: str, overlay: dict[str, set[str]], depth: int) -> int | None:
    if start == target:
        return 0
    queue = deque([(start, 0)])
    visited = {start}
    while queue:
        node, hops = queue.popleft()
        if hops >= depth:
            continue
        neighbors = set(overlay.get(node, set()))
        if gl.node_exists(conn, node):
            for row in conn.execute(
                "SELECT subject,object FROM edges WHERE subject=? OR object=?", (node, node)
            ):
                neighbors.add(row["object"] if row["subject"] == node else row["subject"])
        for neighbor in neighbors:
            if neighbor == target:
                return hops + 1
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, hops + 1))
    return None


def run_query_probes(conn, delta: GraphDelta, attach_plan: dict) -> dict:
    """以 query 可用性检查子图 overlay；不计算密度、度数或全连通分数。"""
    overlay = _overlay_adjacency(delta, attach_plan)
    boundary_results = []
    for mention in delta.boundary_mentions:
        start = _mapped_endpoint(mention, delta, attach_plan)
        hops = _reachable_within(conn, start, delta.page, overlay, depth=2)
        boundary_results.append({"mention": mention, "hops_to_wiki": hops})

    title_idx, alias_idx, suffix_idx = gl.build_name_index(conn)
    anchor_candidates = _exact_candidates(delta.title, title_idx, alias_idx, suffix_idx)
    if delta.page not in anchor_candidates:
        anchor_candidates.append(delta.page)
    reachable = sum(item["hops_to_wiki"] is not None for item in boundary_results)
    total = len(boundary_results)
    candidate_counts = [
        item.get("candidate_count", 1 if item["action"].startswith("reuse") else 0)
        for item in attach_plan["decisions"]
    ]
    return {
        "usable": not delta.hard_errors,
        "anchor_hit": bool(delta.page),
        "anchor_discriminable": len(set(anchor_candidates)) == 1,
        "anchor_candidate_count": len(set(anchor_candidates)),
        "raw_probe_applicable": bool(delta.raw_packages),
        "raw_reachable_one_hop": all(
            raw in overlay.get(delta.page, set()) for raw in delta.raw_packages
        ) if delta.raw_packages else None,
        "boundary_total": total,
        "boundary_reachable_within_2": reachable,
        "boundary_path_success": round(reachable / total, 3) if total else 1.0,
        "ambiguous_mentions": len(attach_plan["ambiguous"]),
        "max_candidate_burden": max(candidate_counts, default=0),
        "boundary_results": boundary_results,
    }


def inspect_delta(conn, delta: GraphDelta, attach_plan: dict | None = None) -> dict:
    attach_plan = attach_plan or plan_attachment(conn, delta)
    probes = run_query_probes(conn, delta, attach_plan)
    receipt = {
        "contract_version": GRAPH_DELTA_CONTRACT_VERSION,
        "validator_version": GRAPH_DELTA_VALIDATOR_VERSION,
        "delta_sha256": _content_hash(delta.to_dict()),
        "attach_plan_sha256": _content_hash(attach_plan),
        "status": "prevalidated" if not delta.hard_errors else "rejected",
        "checks": {
            "hard_structure": not delta.hard_errors,
            "query_overlay_usable": bool(probes.get("usable")),
            "wiki_anchor_discriminable": bool(probes.get("anchor_discriminable")),
            "raw_reachable_when_applicable": (
                probes.get("raw_reachable_one_hop")
                if probes.get("raw_probe_applicable") else None
            ),
        },
        "outer_commit_required": True,
    }
    return {
        "subgraph": {
            "page": delta.page,
            "raw_packages": delta.raw_packages,
            "edge_count": len(delta.edges),
            "boundary_count": len(delta.boundary_mentions),
            "deterministic_edges": delta.deterministic_edges,
            "semantic_edges": delta.semantic_edges,
            "hard_errors": delta.hard_errors,
        },
        "attach_plan": attach_plan,
        "query_probes": probes,
        "validation_receipt": receipt,
    }


def _post_fusion_hard_checks(conn, delta: GraphDelta) -> list[str]:
    errors = []
    if not gl.node_exists(conn, delta.page):
        errors.append(f"融合后缺 Wiki anchor: {delta.page}")
    for raw_path in delta.raw_packages:
        if not gl.node_exists(conn, raw_path):
            errors.append(f"融合后缺 Raw 文档包: {raw_path}")
            continue
        if not conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate='来源' AND object=?",
            (delta.page, raw_path),
        ).fetchone():
            errors.append(f"融合后缺 Wiki→来源→Raw: {delta.page} → {raw_path}")
    return errors


def fuse_with_savepoint(
    conn,
    delta: GraphDelta,
    writer: Callable[[], object],
    inspection: dict | None = None,
) -> tuple[object, dict]:
    """运行 attach plan/probes，并在 SAVEPOINT 中执行调用方 writer。"""
    inspection = inspection or inspect_delta(conn, delta)
    if delta.hard_errors:
        raise DeltaContractError("; ".join(delta.hard_errors))
    conn.execute("SAVEPOINT document_graph_delta")
    before_changes = conn.total_changes
    try:
        result = writer()
        post_errors = _post_fusion_hard_checks(conn, delta)
        if post_errors:
            raise DeltaContractError("; ".join(post_errors))
        conn.execute("RELEASE SAVEPOINT document_graph_delta")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT document_graph_delta")
        conn.execute("RELEASE SAVEPOINT document_graph_delta")
        raise
    inspection["fusion"] = {
        "committed": True,
        "transaction_state": "savepoint_released",
        "outer_commit_required": True,
        "sqlite_changes": conn.total_changes - before_changes,
        "hard_errors": [],
        "soft_probe_blocking": False,
    }
    receipt = inspection.setdefault("validation_receipt", {})
    receipt.update({
        "contract_version": GRAPH_DELTA_CONTRACT_VERSION,
        "validator_version": GRAPH_DELTA_VALIDATOR_VERSION,
        "delta_sha256": receipt.get("delta_sha256") or _content_hash(delta.to_dict()),
        "status": "validated",
        "savepoint": "released",
        "sqlite_changes": conn.total_changes - before_changes,
        "postconditions": {
            "wiki_anchor_and_raw_lineage": True,
        },
        "outer_commit_required": True,
    })
    return result, inspection

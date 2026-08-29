#!/usr/bin/env python3
"""节点身份解析与语义召回。

对 LLM/Agent 暴露的是 ``resolve_node`` 和 ``semantic_search``，而不是裸向量。
图只读；唯一可能的写入是 ``embed_cached_batch`` 对 embeddings.db 的文本缓存。
"""
from __future__ import annotations

import json
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import graph_lib as gl


DEFAULT_IDENTITY = {
    "label": 0.92,
    "semantic": 0.90,
    "combined": 0.91,
    "margin": 0.04,
    "candidate_floor": 0.72,
}
DEFAULT_SEARCH_FLOOR = 0.45
MAX_TOP_K = 20
DESCRIPTION_WARM_LIMIT = 32


def _configured_identity() -> dict:
    try:
        config = gl.load_embed_config()
        return {**DEFAULT_IDENTITY, **(config.get("identity_gate") or {})}
    except Exception:
        return dict(DEFAULT_IDENTITY)


def _configured_search_floor() -> float:
    try:
        config = gl.load_embed_config()
        return float((config.get("semantic_search") or {}).get("floor", DEFAULT_SEARCH_FLOOR))
    except Exception:
        return DEFAULT_SEARCH_FLOOR


def label_text(title: str) -> str:
    return str(title or "").strip()


def semantic_text(title: str, description: str = "") -> str:
    title = label_text(title)
    description = str(description or "").strip()
    return f"名称: {title}\n含义: {description}" if description else title


def query_semantic_text(name: str, context: str = "") -> str:
    name = str(name or "").strip()
    context = str(context or "").strip()
    return f"名称: {name}\n上下文: {context}" if context else name


def _normalize(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").casefold())


def _meaningful_components(value: str) -> set[str]:
    parts = {value, *gl.decompose_name_to_aliases(value)}
    return {
        normalized
        for part in parts
        if len(normalized := _normalize(part)) >= 3
    }


def lexical_identity_signal(left: str, right: str) -> bool:
    """代码化身份信号：等价组成部分重合或一个规范全名包含另一个。"""
    left_n, right_n = _normalize(left), _normalize(right)
    if not left_n or not right_n:
        return False
    if left_n == right_n:
        return True
    if min(len(left_n), len(right_n)) >= 4 and (left_n in right_n or right_n in left_n):
        return True
    return bool(_meaningful_components(left) & _meaningful_components(right))


def _embed_queries(texts: list[str]):
    """只为本次少量 query 文本生成向量；失败返回 None，调用方机械降级。"""
    try:
        from embed_helper import embed_cached_batch
        return embed_cached_batch(texts, cache_type="node-query")
    except Exception:
        return None


def _cached_vectors(texts: list[str]) -> dict[str, object]:
    """仅读取既有候选向量，避免一次 Agent 调用批量生成全图缓存。"""
    if not texts:
        return {}
    db_path = gl.REPO / "cross-domain" / "embeddings.db"
    if not db_path.exists():
        return {}
    try:
        import numpy as np
        db = sqlite3.connect(db_path)
        unique = list(dict.fromkeys(texts))
        found = {}
        for start in range(0, len(unique), 400):
            batch = unique[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            for text, blob in db.execute(
                f"SELECT text,vector FROM embeddings WHERE text IN ({placeholders})", batch
            ):
                vector = np.frombuffer(blob, dtype=np.float32)
                if vector.size and np.isfinite(vector).all():
                    found[text] = vector
        db.close()
        return found
    except Exception:
        return {}


def _cosine(query_vector, candidate_vector) -> float:
    try:
        import numpy as np
        q = np.asarray(query_vector, dtype=np.float32)
        c = np.asarray(candidate_vector, dtype=np.float32)
        denom = float(np.linalg.norm(q) * np.linalg.norm(c))
        return float(np.dot(q, c) / denom) if denom else 0.0
    except Exception:
        return 0.0


def _row_dict(row) -> dict:
    return {
        "node_id": row["path"],
        "title": row["title"] or row["path"],
        "description": row["description"] or "",
        "type": row["type"] or "",
        "entity_subtype": row["entity_subtype"] or "",
    }


def _node_by_path(conn, path: str):
    return conn.execute(
        "SELECT path,title,description,type,entity_subtype FROM nodes WHERE path=?", (path,)
    ).fetchone()


def resolve_node_id(conn, node_id: str, node_types: list[str] | None = None) -> dict:
    """解析调用方已经持有的 canonical ID；不把 surface mention 当作 ID。

    这个入口只做 path 存在性与可选类型检查，不读取 title/alias，也不调用
    embedding。Raw/LLM 抽取文本必须走 ``resolve_node``。
    """
    node_id = str(node_id or "").strip()
    if not node_id:
        return {"decision": "invalid", "reason": "empty_node_id"}
    row = _node_by_path(conn, node_id)
    if row is None:
        return {
            "decision": "unmatched",
            "node_id": node_id,
            "reason": "canonical_id_not_found",
        }
    item = _row_dict(row)
    if node_types and item["type"] not in node_types:
        return {
            "decision": "type_mismatch",
            "node_id": node_id,
            "reason": "canonical_id_type_mismatch",
            "actual_type": item["type"],
        }
    return {
        "decision": "resolved",
        **item,
        "match_mode": "canonical_id",
        "reason": "explicit_canonical_id",
        "candidates": [],
    }


def _exact_candidates(conn, name: str, node_types: list[str] | None = None) -> list[dict]:
    params: list[object] = [name, name, name]
    type_clause = ""
    if node_types:
        type_clause = f" AND n.type IN ({','.join('?' for _ in node_types)})"
        params.extend(node_types)
    rows = conn.execute(
        "SELECT DISTINCT n.path,n.title,n.description,n.type,n.entity_subtype "
        "FROM nodes n LEFT JOIN aliases a ON a.node_path=n.path "
        "WHERE (n.path=? OR n.title=? OR a.alias=?)" + type_clause,
        params,
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _decomposed_exact_candidates(
    conn, name: str, node_types: list[str] | None = None
) -> list[dict]:
    """用双语名分解结果精确查 title/alias；完整名称优先于缩写。"""
    components = gl.decompose_name_to_aliases(name)
    if not components:
        return []
    long_forms = [item for item in components if not re.fullmatch(r"[A-Z]{2,}[A-Za-z0-9\-]*", item)]
    abbreviations = [item for item in components if item not in long_forms]

    def collect(selected):
        by_path = {}
        for component in selected:
            params: list[object] = [component, component]
            type_clause = ""
            if node_types:
                type_clause = f" AND n.type IN ({','.join('?' for _ in node_types)})"
                params.extend(node_types)
            rows = conn.execute(
                "SELECT DISTINCT n.path,n.title,n.description,n.type,n.entity_subtype "
                "FROM nodes n LEFT JOIN aliases a ON a.node_path=n.path "
                "WHERE (n.title=? OR a.alias=?)" + type_clause,
                params,
            ).fetchall()
            for row in rows:
                item = _row_dict(row)
                by_path[item["node_id"]] = item
        return list(by_path.values())

    # 中文/英文完整名称是更强的身份信号；缩写只在完整名称均未命中时兜底，
    # 避免历史同缩写节点推翻唯一完整名称。
    long_matches = collect(long_forms)
    if long_matches:
        return long_matches
    return collect(abbreviations)


def _identity_rows(conn, node_types: list[str] | None = None) -> list[dict]:
    if node_types:
        placeholders = ",".join("?" for _ in node_types)
        rows = conn.execute(
            "SELECT path,title,description,type,entity_subtype FROM nodes "
            f"WHERE type IN ({placeholders})", node_types,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path,title,description,type,entity_subtype FROM nodes "
            "WHERE type='entity' AND COALESCE(entity_subtype,'') NOT IN ('person','proposition')"
        ).fetchall()
    return [_row_dict(row) for row in rows]


def _rank_with_embeddings(name: str, context: str, rows: list[dict]) -> tuple[list[dict], str]:
    if not rows:
        return [], "empty_index"
    query_vectors = _embed_queries([label_text(name), query_semantic_text(name, context)])
    if query_vectors is None or len(query_vectors) < 2:
        return [], "embedding_unavailable"
    labels = [label_text(row["title"]) for row in rows]
    semantics = [semantic_text(row["title"], row["description"]) for row in rows]
    cached = _cached_vectors(labels + semantics)
    # description 是稀疏可选字段；只为少量尚未缓存的描述视图懒生成，绝不在
    # 一次 Agent 调用中重算全图 title 向量。
    missing_descriptions = list(dict.fromkeys(
        semantic for row, semantic in zip(rows, semantics)
        if row["description"] and semantic not in cached
    ))[:DESCRIPTION_WARM_LIMIT]
    if missing_descriptions:
        generated = _embed_queries(missing_descriptions)
        if generated is not None:
            cached.update(dict(zip(missing_descriptions, generated)))
    ranked = []
    for row, label, semantic in zip(rows, labels, semantics):
        label_vector = cached.get(label)
        semantic_vector = cached.get(semantic)
        if label_vector is None and semantic_vector is None:
            continue
        label_score = _cosine(query_vectors[0], label_vector) if label_vector is not None else 0.0
        semantic_score = _cosine(query_vectors[1], semantic_vector) if semantic_vector is not None else label_score
        item = dict(row)
        item.update({
            "label_score": round(label_score, 4),
            "semantic_score": round(semantic_score, 4),
            "combined_score": round(0.45 * label_score + 0.55 * semantic_score, 4),
        })
        ranked.append(item)
    ranked.sort(key=lambda item: item["combined_score"], reverse=True)
    return ranked, "dual_embedding" if ranked else "embedding_cache_miss"


def _lexical_rank(query: str, rows: list[dict], top_k: int) -> list[dict]:
    query_n = _normalize(query)
    terms = set(re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9-]{2,}", query.casefold()))
    ranked = []
    for row in rows:
        haystack = f"{row['title']} {row['description']}"
        haystack_n = _normalize(haystack)
        overlap = sum(term in haystack.casefold() for term in terms)
        ratio = SequenceMatcher(None, query_n, haystack_n).ratio() if query_n and haystack_n else 0.0
        score = overlap + ratio
        if score <= 0:
            continue
        item = dict(row)
        item.update({"lexical_score": round(score, 4), "match_role": "lexical_candidate"})
        ranked.append(item)
    ranked.sort(key=lambda item: item["lexical_score"], reverse=True)
    return ranked[:top_k]


def resolve_node(
    conn,
    name: str,
    context: str = "",
    node_types: list[str] | None = None,
    top_k: int = 5,
    thresholds: dict | None = None,
) -> dict:
    """解析表述到 canonical node_id；只返回计划，不修改图。"""
    name = str(name or "").strip()
    if not name:
        return {"decision": "invalid", "reason": "empty_name", "candidates": []}
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    exact = _exact_candidates(conn, name, node_types)
    if len(exact) == 1:
        candidate = exact[0]
        match_mode = "exact_path" if candidate["node_id"] == name else "unique_title_or_alias"
        return {
            "decision": "resolved",
            "node_id": candidate["node_id"],
            "title": candidate["title"],
            "description": candidate["description"],
            "match_mode": match_mode,
            "reason": "deterministic_unique_match",
            "candidates": [],
        }
    if len(exact) > 1:
        ranked, mode = ([], "deterministic")
        if context and any(candidate.get("description") for candidate in exact):
            ranked, mode = _rank_with_embeddings(name, context, exact)
        if context and len(ranked) >= 1:
            first = ranked[0]
            second_score = ranked[1]["semantic_score"] if len(ranked) > 1 else 0.0
            if first["semantic_score"] >= 0.90 and first["semantic_score"] - second_score >= 0.04:
                return {
                    "decision": "resolved",
                    "node_id": first["node_id"],
                    "title": first["title"],
                    "description": first["description"],
                    "match_mode": "context_disambiguated_alias",
                    "reason": "ambiguous_name_resolved_by_description_context",
                    "candidates": ranked[:top_k],
                }
        return {
            "decision": "ambiguous",
            "reason": "multiple_exact_name_targets",
            "match_mode": mode,
            "candidates": (ranked or exact)[:top_k],
            "allowed_actions": ["use_context", "keep_multiple", "ask_user"],
        }

    decomposed = _decomposed_exact_candidates(conn, name, node_types)
    if len(decomposed) == 1:
        candidate = decomposed[0]
        return {
            "decision": "resolved",
            "node_id": candidate["node_id"],
            "title": candidate["title"],
            "description": candidate["description"],
            "match_mode": "unique_decomposed_title_or_alias",
            "reason": "deterministic_unique_decomposed_name_match",
            "candidates": [],
        }
    if len(decomposed) > 1:
        return {
            "decision": "ambiguous",
            "reason": "decomposed_name_components_conflict",
            "match_mode": "deterministic",
            "candidates": decomposed[:top_k],
            "allowed_actions": ["keep_local", "use_context", "review_later"],
        }

    all_rows = _identity_rows(conn, node_types)
    # 自动身份复用必须先有代码化名称信号；没有名称证据时不调用 embedding，
    # semantic relatedness 留给 semantic_search，不冒充 identity。
    rows = [row for row in all_rows if lexical_identity_signal(name, row["title"])]
    if not rows:
        return {
            "decision": "unmatched",
            "reason": "no_lexical_identity_candidate",
            "match_mode": "deterministic",
            "candidates": _lexical_rank(name, all_rows, top_k),
            "allowed_actions": ["create_local", "review_later"],
        }
    ranked, mode = _rank_with_embeddings(name, context, rows)
    cfg = {**_configured_identity(), **(thresholds or {})}
    candidates = [item for item in ranked if item["combined_score"] >= cfg["candidate_floor"]]
    if candidates:
        first = candidates[0]
        second_combined = candidates[1]["combined_score"] if len(candidates) > 1 else 0.0
        passes_gate = (
            lexical_identity_signal(name, first["title"])
            and first["label_score"] >= cfg["label"]
            and first["semantic_score"] >= cfg["semantic"]
            and first["combined_score"] >= cfg["combined"]
            and first["combined_score"] - second_combined >= cfg["margin"]
        )
        if passes_gate:
            return {
                "decision": "resolved",
                "node_id": first["node_id"],
                "title": first["title"],
                "description": first["description"],
                "match_mode": "dual_view_identity",
                "reason": "lexical_signal_and_label_semantic_gate",
                "candidates": candidates[:top_k],
            }
        return {
            "decision": "ambiguous",
            "reason": "semantic_candidates_not_identity_equivalent",
            "match_mode": mode,
            "candidates": candidates[:top_k],
            "allowed_actions": ["keep_local", "use_context", "review_later"],
        }
    return {
        "decision": "unmatched",
        "reason": mode,
        "match_mode": "lexical_degraded" if mode != "dual_embedding" else mode,
        "candidates": _lexical_rank(name, all_rows, top_k),
        "allowed_actions": ["create_local", "review_later"],
    }


def semantic_search(
    conn,
    query: str,
    scope: str = "node",
    top_k: int = 8,
    semantic_floor: float | None = None,
) -> dict:
    """按含义召回节点或 Hub；结果只表示相关性，不能用于身份合并。"""
    query = str(query or "").strip()
    if not query:
        return {"decision": "invalid", "reason": "empty_query", "candidates": []}
    if scope not in {"node", "hub"}:
        return {"decision": "invalid", "reason": f"unsupported_scope:{scope}", "candidates": []}
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    if scope == "hub":
        rows = [_row_dict(row) for row in conn.execute(
            "SELECT path,title,description,type,entity_subtype FROM nodes WHERE type='hub'"
        )]
    else:
        rows = [_row_dict(row) for row in conn.execute(
            "SELECT path,title,description,type,entity_subtype FROM nodes WHERE type!='raw'"
        )]
    # 概念性 query 本身就是 semantic text，不重复包装为“名称+上下文”。
    ranked, mode = _rank_with_embeddings(query, "", rows)
    semantic_floor = _configured_search_floor() if semantic_floor is None else float(semantic_floor)
    candidates = []
    for item in ranked:
        if item["semantic_score"] < float(semantic_floor):
            continue
        item = dict(item)
        item["match_role"] = "semantic_candidate"
        candidates.append(item)
        if len(candidates) >= top_k:
            break
    if not candidates:
        candidates = _lexical_rank(query, rows, top_k)
        mode = "lexical_degraded"
    return {
        "decision": "candidates" if candidates else "no_candidate",
        "query": query,
        "scope": scope,
        "mode": mode,
        "count": len(candidates),
        "candidates": candidates,
        "identity_claim": False,
        "next": "resolve_node before treating a candidate as the same entity",
    }


def _json_print(value: dict):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("name")
    resolve.add_argument("--context", default="")
    resolve.add_argument("--top-k", type=int, default=5)
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--scope", choices=["node", "hub"], default="node")
    search.add_argument("--top-k", type=int, default=8)
    args = parser.parse_args()
    conn = gl.connect()
    try:
        if args.command == "resolve":
            _json_print(resolve_node(conn, args.name, args.context, top_k=args.top_k))
        else:
            _json_print(semantic_search(conn, args.query, args.scope, args.top_k))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

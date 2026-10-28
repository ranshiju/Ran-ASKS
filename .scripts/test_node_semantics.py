#!/usr/bin/env python3
"""双视图节点解析、语义召回和 alias 多映射回归。"""
import sqlite3
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import node_semantics as ns


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    return conn


def add_node(conn, path, title=None, description="", node_type="entity", subtype="keyword"):
    gl.ensure_node(
        conn, path, title or path, node_type,
        entity_subtype=subtype if node_type == "entity" else None,
        description=description,
    )


def test_lexical_top_k_matches_exhaustive_ranking_and_stable_ties():
    import copy
    import random
    import re
    from difflib import SequenceMatcher

    # Exhaustive reference intentionally has no pruning or incremental top-k.
    def exhaustive(query, rows, k):
        query_n = ns._normalize(query)
        terms = set(re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9-]{2,}", query.casefold()))
        ranked = []
        for row in rows:
            text = f"{row['title']} {row['description']}"
            text_n = ns._normalize(text)
            overlap = sum(term in text.casefold() for term in terms)
            ratio = SequenceMatcher(None, query_n, text_n).ratio() if query_n and text_n else 0.0
            score = overlap + ratio
            if score > 0:
                ranked.append({**row, "lexical_score": round(score, 4), "match_role": "lexical_candidate"})
        return sorted(ranked, key=lambda row: row["lexical_score"], reverse=True)[:k]

    rng = random.Random(20261027)
    alphabet = "aaabccxyz 量子模型条件猜想MPS+-_()éß0123"
    rows = [{"node_id": str(i),
             "title": "".join(rng.choices(alphabet, k=rng.randint(0, 24))),
             "description": "".join(rng.choices(alphabet, k=rng.randint(0, 280)))}
            for i in range(180)]
    rows += [{"node_id": f"tie-{i}", "title": "Same (MPS)", "description": "相同"} for i in range(25)]
    queries = ["", "量子模型", "abc", "Same (MPS)", "a" * 240, "éß", "未知假设条件", "---"]
    for order in (rows, list(reversed(rows))):
        before = copy.deepcopy(order)
        for query in queries:
            for k in (-2, 0, 1, 5, 20, 999):
                assert ns._lexical_rank(query, order, k) == exhaustive(query, order, k)
        assert order == before


def test_lexical_top_k_skips_impossible_scores_without_changing_tie_order():
    from difflib import SequenceMatcher

    rows = [{"node_id": str(i), "title": "MPS", "description": ""} for i in range(3)]
    rows += [{"node_id": "long", "title": "unrelated", "description": "x" * 1000}] * 100
    with patch.object(ns, "SequenceMatcher", wraps=SequenceMatcher) as matcher:
        ranked = ns._lexical_rank("MPS", rows, 1)
    assert ranked == [{**rows[0], "lexical_score": 2.0, "match_role": "lexical_candidate"}]
    assert matcher.call_count == 1


def test_alias_schema_migrates_to_many_to_many():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE nodes(path TEXT PRIMARY KEY,title TEXT,type TEXT,entity_subtype TEXT,
            source_type TEXT,date TEXT,status TEXT,has_raw_source INTEGER,ingest_version INTEGER DEFAULT 0);
        CREATE TABLE aliases(alias TEXT PRIMARY KEY,node_path TEXT);
        INSERT INTO nodes(path,title,type) VALUES('a','A','entity'),('b','B','entity');
        INSERT INTO aliases(alias,node_path) VALUES('Shared','a');
    """)
    gl.init_schema(conn)
    conn.execute("INSERT INTO aliases(alias,node_path) VALUES('Shared','b')")
    paths = [row[0] for row in conn.execute(
        "SELECT node_path FROM aliases WHERE alias='Shared' ORDER BY node_path"
    )]
    assert paths == ["a", "b"]
    pk = {row[1]: row[5] for row in conn.execute("PRAGMA table_info(aliases)")}
    assert pk == {"alias": 1, "node_path": 2}


def test_description_is_optional_and_preserved():
    conn = make_db()
    add_node(conn, "mps", "矩阵乘积态(MPS)", "一维量子多体态的张量网络表示")
    gl.ensure_node(conn, "mps", "矩阵乘积态(MPS)", "entity", entity_subtype="keyword")
    row = conn.execute("SELECT description FROM nodes WHERE path='mps'").fetchone()
    assert row[0] == "一维量子多体态的张量网络表示"


def test_unique_alias_resolves_without_embedding():
    conn = make_db()
    add_node(conn, "mps", "矩阵乘积态(MPS)")
    gl.insert_aliases(conn, "mps", ["MPS", "matrix product state"])
    result = ns.resolve_node(conn, "MPS")
    assert result["decision"] == "resolved"
    assert result["node_id"] == "mps"
    assert result["match_mode"] == "unique_title_or_alias"


def test_bilingual_name_reuses_unique_decomposed_title():
    conn = make_db()
    add_node(conn, "rag", "检索增强生成")
    add_node(conn, "rag-hub", "检索增强生成", node_type="hub", subtype="")
    add_node(conn, "rag-other", "研究核心定位Wiki与检索增强生成(RAG)")
    gl.insert_aliases(conn, "rag-other", ["RAG"])
    result = ns.resolve_node(
        conn, "检索增强生成retrieval-augmented generation(RAG)", node_types=["entity"]
    )
    assert result["decision"] == "resolved"
    assert result["node_id"] == "rag"
    assert result["match_mode"] == "unique_decomposed_title_or_alias"


def test_generic_decomposed_alias_does_not_reuse_unrelated_concept():
    conn = make_db()
    add_node(conn, "backflow", "backflow变换backflow transformation")
    gl.insert_aliases(conn, "backflow", ["变换", "backflow transformation"])
    result = ns.resolve_node(
        conn, "Jordan-Wigner变换Jordan-Wigner transformation", node_types=["entity"]
    )
    assert result["decision"] == "unmatched"


def test_decomposed_method_alias_does_not_reuse_longer_compound_concept():
    conn = make_db()
    add_node(
        conn,
        "symmetric-peps",
        "含SU(2)对称性的simple update投影纠缠对态",
    )
    gl.insert_aliases(conn, "symmetric-peps", ["simple update", "SU"])
    result = ns.resolve_node(
        conn, "简单更新simple update(SU)", node_types=["entity"]
    )
    assert result["decision"] != "resolved", result


def test_named_framework_does_not_reuse_generic_component():
    for mention in ("OSPREY框架", "SPARK框架", "框架framework(OSPREY)"):
        conn = make_db()
        add_node(conn, "generic", "框架")
        gl.insert_aliases(conn, "generic", ["framework"])
        result = ns.resolve_node(conn, mention, node_types=["entity"])
        assert result["decision"] == "unmatched", (mention, result)


def test_named_framework_reuses_specific_abbreviation_after_generic_filter():
    conn = make_db()
    add_node(conn, "generic", "框架")
    add_node(conn, "osprey", "OSPREY")
    add_node(conn, "spark", "SPARK")
    for mention, expected in (("OSPREY框架", "osprey"), ("SPARK框架", "spark")):
        result = ns.resolve_node(conn, mention, node_types=["entity"])
        assert result["decision"] == "resolved", result
        assert result["node_id"] == expected, result
    assert ns.resolve_node(conn, "框架")["node_id"] == "generic"


def test_full_method_name_reuses_variant_with_optional_trailing_acronym():
    conn = make_db()
    add_node(conn, "simple-update", "简单更新simple update(SU)")
    result = ns.resolve_node(
        conn, "简单更新simple update", node_types=["entity"]
    )
    assert result["decision"] == "resolved", result
    assert result["node_id"] == "simple-update"
    assert result["match_mode"] == "optional_trailing_acronym"


def test_complexity_qualifier_does_not_become_bilingual_identity():
    conn = make_db()
    add_node(conn, "hard", "困难")
    add_node(conn, "complete", "完全")
    add_node(conn, "hard-problem", "困难问题")
    add_node(conn, "complete-problem", "完全问题")
    for mention in ("QMA困难QMA-hard", "BQP完全BQP-complete"):
        result = ns.resolve_node(conn, mention, node_types=["entity"])
        assert result["decision"] == "unmatched", (mention, result)


def test_anchored_complexity_problem_reuses_full_parenthetical_identity():
    conn = make_db()
    add_node(conn, "qma-problem", "QMA难问题")
    add_node(conn, "bqp-problem", "BQP完全问题")
    add_node(conn, "hard-problem", "困难问题")
    add_node(conn, "complete-problem", "完全问题")
    cases = {
        "QMA困难问题(QMA-hard)": "qma-problem",
        "BQP完全问题(BQP-complete)": "bqp-problem",
    }
    for mention, expected in cases.items():
        result = ns.resolve_node(conn, mention, node_types=["entity"])
        assert result["decision"] == "resolved", (mention, result)
        assert result["node_id"] == expected, (mention, result)
        assert result["match_mode"] == "unique_decomposed_title_or_alias"


def test_bilingual_hybrid_prefix_resolves_without_embedding():
    conn = make_db()
    add_node(conn, "jw", "Jordan-Wigner变换")
    result = ns.resolve_node(
        conn, "Jordan-Wigner变换Jordan-Wigner transformation", node_types=["entity"]
    )
    assert result["decision"] == "resolved"
    assert result["node_id"] == "jw"
    assert result["match_mode"] == "unique_decomposed_title_or_alias"


def test_embedded_english_mention_does_not_reuse_whole_concept():
    conn = make_db()
    add_node(conn, "majorana", "Majorana费米子Majorana fermion")
    gl.insert_aliases(conn, "majorana", ["Majorana"])
    add_node(conn, "material", "扭曲Kitaev链材料CoNb2O6")
    gl.insert_aliases(conn, "material", ["Kitaev"])
    for mention in (
        "以边缘单方向磁化探测Majorana边缘零模",
        "Kitaev磁体候选材料的实验探测",
    ):
        result = ns.resolve_node(conn, mention, node_types=["entity"])
        assert result["decision"] == "unmatched", (mention, result)


def test_leading_multiword_eponym_does_not_reuse_proposition_identity():
    conn = make_db()
    target = "中间测量仅限计算基下完全测量"
    add_node(conn, target, target, subtype="proposition")
    gl.insert_aliases(conn, target, ["von Neumann"])
    mention = "von Neumann熵量化协作协助下可获得的最大额外量子相干性"
    result = ns.resolve_node(conn, mention, node_types=["entity"])
    assert result["decision"] == "unmatched", result


def test_eponym_prefixed_specific_concept_does_not_reuse_generic_remainder():
    conn = make_db()
    add_node(conn, "entropy", "熵")
    result = ns.resolve_node(conn, "von Neumann 熵", node_types=["entity"])
    assert result["decision"] == "unmatched", result


def test_pure_english_venue_does_not_decompose_to_stopword():
    old_venue = "the 64th Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)"
    new_venue = "the 2024 Conference on Empirical Methods in Natural Language Processing"
    assert gl.decompose_name_to_aliases(old_venue) == []
    assert gl.extract_keyword_id(old_venue) == old_venue
    assert gl.decompose_name_to_aliases(new_venue) == []

    conn = make_db()
    add_node(conn, "the", old_venue)
    conn.execute("INSERT INTO aliases(alias,node_path) VALUES (?,?)", ("the", "the"))
    result = ns.resolve_node(conn, new_venue, node_types=["entity"])
    assert result["decision"] == "unmatched"


def test_bilingual_name_component_conflict_is_ambiguous():
    conn = make_db()
    add_node(conn, "rag-cn", "检索增强生成")
    add_node(conn, "rag-other", "另一种生成方法")
    gl.insert_aliases(conn, "rag-other", ["retrieval-augmented generation"])
    result = ns.resolve_node(
        conn, "检索增强生成retrieval-augmented generation(RAG)", node_types=["entity"]
    )
    assert result["decision"] == "ambiguous"
    assert result["reason"] == "decomposed_name_components_conflict"
    assert {item["node_id"] for item in result["candidates"]} == {"rag-cn", "rag-other"}


def test_explicit_node_id_uses_path_only():
    conn = make_db()
    add_node(conn, "Shared", "第一义项", "第一个含义")
    add_node(conn, "second", "第二义项", "第二个含义")
    gl.insert_aliases(conn, "second", ["Shared"])
    mention = ns.resolve_node(conn, "Shared")
    canonical = ns.resolve_node_id(conn, "Shared")
    assert mention["decision"] == "ambiguous"
    assert canonical["decision"] == "resolved"
    assert canonical["node_id"] == "Shared"
    assert canonical["match_mode"] == "canonical_id"


def test_ambiguous_alias_abstains_without_context():
    conn = make_db()
    for path, title in (("a", "概念A"), ("b", "概念B")):
        add_node(conn, path, title)
        gl.insert_aliases(conn, path, ["Shared"])
    result = ns.resolve_node(conn, "Shared")
    assert result["decision"] == "ambiguous"
    assert {item["node_id"] for item in result["candidates"]} == {"a", "b"}


def test_dual_view_identity_gate_reuses_light_variant():
    conn = make_db()
    add_node(
        conn, "mps", "matrix product state",
        "one-dimensional quantum many-body state representation",
    )
    add_node(
        conn, "mps-operator", "matrix product state operator",
        "operator representation on a chain",
    )
    name = "matrix-product-state"
    context = "one-dimensional quantum many-body state representation"
    vectors = {
        name: np.array([1.0, 0.0]),
        ns.query_semantic_text(name, context): np.array([1.0, 0.0]),
        "matrix product state": np.array([0.99, 0.01]),
        ns.semantic_text("matrix product state", "one-dimensional quantum many-body state representation"): np.array([1.0, 0.0]),
        "matrix product state operator": np.array([0.72, 0.28]),
        ns.semantic_text("matrix product state operator", "operator representation on a chain"): np.array([0.70, 0.30]),
    }
    old_queries, old_cached = ns._embed_queries, ns._cached_vectors
    ns._embed_queries = lambda texts: np.array([vectors[text] for text in texts])
    ns._cached_vectors = lambda texts: {text: vectors[text] for text in texts if text in vectors}
    try:
        result = ns.resolve_node(conn, name, context)
    finally:
        ns._embed_queries, ns._cached_vectors = old_queries, old_cached
    assert result["decision"] == "resolved"
    assert result["node_id"] == "mps"
    assert result["match_mode"] == "dual_view_identity"


def test_semantic_search_returns_related_not_identity():
    conn = make_db()
    add_node(conn, "tensor-hub", "张量网络", "张量表示与算法", "hub", "")
    query = "张量表示方法"
    query_semantic = ns.query_semantic_text(query, query)
    hub_semantic = ns.semantic_text("张量网络", "张量表示与算法")
    vectors = {
        query: np.array([1.0, 0.0]),
        query_semantic: np.array([1.0, 0.0]),
        "张量网络": np.array([0.9, 0.1]),
        hub_semantic: np.array([0.98, 0.02]),
    }
    old_queries, old_cached = ns._embed_queries, ns._cached_vectors
    ns._embed_queries = lambda texts: np.array([vectors[text] for text in texts])
    ns._cached_vectors = lambda texts: {text: vectors[text] for text in texts if text in vectors}
    try:
        result = ns.semantic_search(conn, query, scope="hub")
    finally:
        ns._embed_queries, ns._cached_vectors = old_queries, old_cached
    assert result["decision"] == "candidates"
    assert result["identity_claim"] is False
    assert result["candidates"][0]["node_id"] == "tensor-hub"
    assert result["candidates"][0]["match_role"] == "semantic_candidate"


def test_embedding_failure_degrades_to_lexical_search():
    conn = make_db()
    add_node(conn, "mps", "矩阵乘积态", "一维量子态表示")
    old_queries = ns._embed_queries
    ns._embed_queries = lambda texts: None
    try:
        result = ns.semantic_search(conn, "矩阵乘积态")
    finally:
        ns._embed_queries = old_queries
    assert result["decision"] == "candidates"
    assert result["mode"] == "lexical_degraded"
    assert result["candidates"][0]["node_id"] == "mps"


def test_persisted_distinction_blocks_alias_and_embedding_identity_reuse():
    conn = make_db()
    add_node(conn, "tensor-network", "张量网络", "张量表示方法")
    gl.insert_aliases(conn, "tensor-network", ["谱张量网络"])
    assert ns.record_identity_distinction(
        conn,
        "谱张量网络",
        "张量网络",
        source="user:2026-10-06",
        reason="用户明确要求保持不同概念",
    )
    assert ns.record_identity_distinction(
        conn,
        "张量网络",
        "谱张量网络",
        source="user:2026-10-06",
        reason="幂等更新",
    )
    assert conn.execute("SELECT COUNT(*) FROM identity_distinctions").fetchone()[0] == 1
    old_queries = ns._embed_queries
    ns._embed_queries = lambda _texts: (_ for _ in ()).throw(
        AssertionError("persisted distinction must run before embeddings")
    )
    try:
        result = ns.resolve_node(conn, "谱张量网络")
    finally:
        ns._embed_queries = old_queries
    assert result["decision"] == "unmatched"
    assert result["reason"] == "persisted_distinct"
    assert result["allowed_actions"] == ["create_local"]


def test_dsh_registers_semantic_capabilities_not_raw_embedding_tools():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dsh.tools import build_tools
    tools = {tool.name: tool for tool in build_tools()}
    assert "node_resolve" in tools
    assert "semantic_search" in tools
    assert "hub_route" in tools
    assert "hub_inspect" in tools
    assert "read_engineering" not in tools
    assert "write_engineering" not in tools
    assert "embed_label" not in tools
    assert "embed_semantic" not in tools
    assert "不能据此认定同一节点" in tools["semantic_search"].description


def test_dsh_prompt_explains_semantic_tool_arguments():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from dsh.agent_loop import AgentLoop
    prompt = AgentLoop()._build_prompt("查找张量网络", 1, [])
    assert "node_resolve(name*, context, node_types, topk)" in prompt
    assert "semantic_search(query*, scope, topk, include_deprecated)" in prompt
    assert "hub_route(page*, topk)" in prompt
    assert "hub_inspect(hub*)" in prompt
    assert "read_engineering" not in prompt
    assert "不能据此认定同一节点" in prompt


def test_query_actions_expose_structured_semantic_tools():
    import json
    import query_actions as qa
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = gl.connect(db_path)
        gl.init_schema(conn)
        add_node(conn, "mps", "矩阵乘积态(MPS)")
        gl.insert_aliases(conn, "mps", ["MPS"])
        conn.commit()
        conn.close()
        old_connect = qa.gl.connect
        def connect_fixture(path=None, *, read_only=False):
            assert read_only, "query actions must use a read-only graph connection"
            return old_connect(db_path, read_only=True)
        qa.gl.connect = connect_fixture
        try:
            resolved = qa.execute("node_resolve", {"name": "MPS"})
        finally:
            qa.gl.connect = old_connect
    assert resolved["ok"]
    payload = json.loads(resolved["text"])
    assert payload["decision"] == "resolved"
    assert payload["node_id"] == "mps"
    assert "node_resolve" in qa.DISPATCH
    assert "semantic_search" in qa.DISPATCH
    assert "read_engineering" not in qa.DISPATCH


def test_ensure_node_preserves_omitted_status_and_allows_explicit_transition():
    conn = make_db()
    add_node(conn, "page", "Page", node_type="page")
    assert conn.execute("SELECT status FROM nodes WHERE path='page'").fetchone()["status"] == "current"
    gl.ensure_node(conn, "page", "Page", "page", status="deprecated")
    gl.insert_aliases(conn, "page", ["Old label"])
    gl.ensure_node(conn, "page", "Page", "page")
    assert conn.execute("SELECT status FROM nodes WHERE path='page'").fetchone()["status"] == "deprecated"
    assert conn.execute("SELECT alias FROM aliases WHERE node_path='page'").fetchone()["alias"] == "Old label"
    gl.ensure_node(conn, "page", "Page", "page", status="active")
    assert conn.execute("SELECT status FROM nodes WHERE path='page'").fetchone()["status"] == "active"
    conn.close()


def test_semantic_page_filter_precedes_embedding_and_topk():
    conn = make_db()
    gl.ensure_node(conn, "academic/wiki/papers/a-old", "Shared", "page", status="deprecated")
    gl.ensure_node(conn, "academic/wiki/papers/z-current", "Shared overview", "page")
    original_rank = ns._rank_with_embeddings
    observed = []

    def rank(query, context, rows):
        observed.append([item["node_id"] for item in rows])
        return [{**item, "semantic_score": 1.0} for item in rows], "synthetic"

    ns._rank_with_embeddings = rank
    try:
        result = ns.semantic_search(conn, "Shared", top_k=1)
        assert observed[-1] == ["academic/wiki/papers/z-current"]
        assert result["candidates"][0]["node_id"] == "academic/wiki/papers/z-current"
        history = ns.semantic_search(conn, "Shared", top_k=1, include_deprecated=True)
        assert "academic/wiki/papers/a-old" in observed[-1]
        assert history["candidates"][0]["status"] == "deprecated"
        try:
            ns.semantic_search(conn, "Shared", include_deprecated="false")
        except ValueError:
            pass
        else:
            raise AssertionError("semantic core accepted non-boolean history flag")
    finally:
        ns._rank_with_embeddings = original_rank
        conn.close()


def test_semantic_lexical_degradation_keeps_version_filter():
    conn = make_db()
    gl.ensure_node(conn, "academic/wiki/papers/old", "UniqueOldLabel", "page", status="deprecated")
    original_queries = ns._embed_queries
    ns._embed_queries = lambda texts: None
    try:
        default = ns.semantic_search(conn, "UniqueOldLabel")
        assert default["candidates"] == [] and default["include_deprecated"] is False
        history = ns.semantic_search(conn, "UniqueOldLabel", include_deprecated=True)
        assert history["mode"] == "lexical_degraded"
        assert history["candidates"][0]["node_id"] == "academic/wiki/papers/old"
    finally:
        ns._embed_queries = original_queries
        conn.close()


def test_batch_identity_preserves_decisions_and_reduces_embedding_roundtrips():
    conn = make_db()
    add_node(conn, "mps", "matrix product state", "state representation")
    add_node(conn, "mpo", "matrix product state operator", "operator representation")
    requests = [{"name": "matrix-product-state", "context": context}
                for context in ("one-dimensional state", "chain state")]
    calls = []

    def vectors(texts):
        calls.append(list(texts))
        return np.array([[0.6, 0.8] if "operator" in text else [1.0, 0.0] for text in texts])

    def cached(texts):
        return {text: np.array([0.6, 0.8] if "operator" in text else [1.0, 0.0])
                for text in texts if text in {"matrix product state", "matrix product state operator"}}

    try:
        with patch.object(ns, "_embed_queries", side_effect=vectors), \
                patch.object(ns, "_cached_vectors", side_effect=cached):
            expected = [ns.resolve_node(conn, **request) for request in requests]
            original_calls = len(calls)
            calls.clear()
            actual = ns.resolve_nodes(conn, requests)
            assert actual == expected
            assert all(item["decision"] == "resolved" for item in actual)
            assert original_calls == 4 and len(calls) == 1
            assert len(calls[0]) == len(set(calls[0]))
    finally:
        conn.close()


def test_batch_identity_keeps_fast_paths_and_falls_back_on_incomplete_vectors():
    conn = make_db()
    add_node(conn, "mps", "matrix product state", "state representation")
    add_node(conn, "tensor", "张量网络", "张量表示")
    ns.record_identity_distinction(conn, "谱张量网络", "张量网络", source="test", reason="distinct")
    try:
        with patch.object(ns, "_embed_queries", side_effect=AssertionError("unexpected embedding")):
            results = ns.resolve_nodes(conn, [
                {"name": "matrix product state"}, {"name": "谱张量网络"}, {"name": "unknown concept"},
            ])
            assert results[0]["decision"] == "resolved"
            assert results[1]["reason"] == "persisted_distinct"
            assert results[2]["reason"] == "no_lexical_identity_candidate"
        requests = [{"name": "matrix-product-state", "context": context}
                    for context in ("state", "chain")]
        with patch.object(ns, "_embed_queries", return_value=None), \
                patch.object(ns, "_cached_vectors", return_value={}):
            expected = [ns.resolve_node(conn, **request) for request in requests]
            assert ns.resolve_nodes(conn, requests) == expected
        with patch.object(ns, "_embed_queries", side_effect=[np.array([[1.0, 0.0]]), None, None]) as embed, \
                patch.object(ns, "_cached_vectors", return_value={}):
            assert ns.resolve_nodes(conn, requests) == expected
            assert embed.call_count == 3
    finally:
        conn.close()


def test_batch_identity_preserves_sequential_cache_visibility_and_description_limit():
    conn = make_db()
    count = ns.DESCRIPTION_WARM_LIMIT + 8
    for index in range(count):
        add_node(conn, f"node-{index:02}", f"spectral method {index:02}", f"description {index:02}")
    requests = [{"name": "spectral-method", "context": context} for context in ("one", "two")]

    def run(batch):
        cache = {f"spectral method {index:02}": np.array([1.0, 0.0]) for index in range(count)}
        calls = []

        def embed(texts):
            calls.append(list(texts))
            for text in texts:
                cache[text] = np.array([0.6, 0.8] if "\n含义:" in text else [1.0, 0.0])
            return np.array([cache[text] for text in texts])

        with patch.object(ns, "_embed_queries", side_effect=embed), \
                patch.object(ns, "_cached_vectors", side_effect=lambda texts: {
                    text: cache[text] for text in texts if text in cache}):
            result = ns.resolve_nodes(conn, requests) if batch else [
                ns.resolve_node(conn, **request) for request in requests]
        return result, calls

    try:
        expected, sequential_calls = run(False)
        actual, batch_calls = run(True)
        assert actual == expected
        assert expected[0]["candidates"][0]["node_id"] == f"node-{ns.DESCRIPTION_WARM_LIMIT:02}"
        assert expected[1]["candidates"][0]["node_id"] == "node-00"
        assert list(map(len, sequential_calls)) == [2, ns.DESCRIPTION_WARM_LIMIT, 2, 8]
        assert len(batch_calls) == 1
        assert len(batch_calls[0]) == count + 3
    finally:
        conn.close()


def test_identity_query_warms_its_label_at_original_sequential_position():
    conn = make_db()
    add_node(conn, "first", "spectral method 00", "first description")
    add_node(conn, "second", "spectral method 00", "second description")
    requests = [{"name": name, "context": "context", "thresholds": {"candidate_floor": 0.1}}
                for name in ("spectral-method", "spectral method 00")]

    def run(batch):
        cache = {}

        def embed(texts):
            cache.update({text: np.array([1.0, 0.0]) for text in texts})
            return np.array([cache[text] for text in texts])

        with patch.object(ns, "_embed_queries", side_effect=embed), \
                patch.object(ns, "_cached_vectors", side_effect=lambda texts: {
                    text: cache[text] for text in texts if text in cache}):
            return ns.resolve_nodes(conn, requests) if batch else [
                ns.resolve_node(conn, **request) for request in requests]

    try:
        expected = run(False)
        assert run(True) == expected
        assert all(candidate["label_score"] == 0.0 for candidate in expected[0]["candidates"])
        assert expected[1]["decision"] == "ambiguous"
        assert all(candidate["label_score"] == 1.0 for candidate in expected[1]["candidates"])
    finally:
        conn.close()


def test_batch_reuses_name_processing_and_keeps_graph_decisions_fresh():
    from collections import Counter

    conn = make_db()
    titles = [f"unrelated topic {index:02}" for index in range(12)]
    for index, title in enumerate(titles):
        add_node(conn, f"candidate-{index}", title)
    requests = [{"name": f"missing query {index:02}"} for index in range(3)]
    original = gl.decompose_name_to_aliases
    counts = Counter()

    def decompose(name):
        counts[name] += 1
        return original(name)

    try:
        with patch.object(gl, "decompose_name_to_aliases", side_effect=decompose), \
                patch.object(ns, "_embed_queries", side_effect=AssertionError("unexpected embedding")):
            expected = [ns.resolve_node(conn, **request) for request in requests]
            assert all(counts[title] == len(requests) for title in titles)
            counts.clear()
            assert ns.resolve_nodes(conn, requests) == expected
            assert all(counts[title] == 1 for title in titles)
            counts.clear()
            assert ns.resolve_nodes(conn, requests) == expected
            assert all(counts[title] == 1 for title in titles)
            gl.insert_aliases(conn, "candidate-0", [requests[0]["name"]])
            current = ns.resolve_nodes(conn, requests)
            assert current[0]["decision"] == "resolved"
            assert current[0]["node_id"] == "candidate-0"
            ns.record_identity_distinction(
                conn, requests[0]["name"], titles[0], source="test", reason="separate identities",
            )
            current = ns.resolve_nodes(conn, requests)
            assert current[0]["reason"] == "persisted_distinct"
    finally:
        conn.close()


def test_batch_name_processing_scope_restores_after_nested_failure():
    scopes = []

    def resolve(conn, requests):
        scope = ns._NAME_VIEW_MEMO.get()
        assert isinstance(scope, dict)
        scopes.append(scope)
        if requests[0]["name"] == "outer":
            scope["outer"] = True
            assert ns.resolve_nodes(conn, [{"name": "inner"}]) == []
            assert ns._NAME_VIEW_MEMO.get() is scope
            raise RuntimeError("resolution failed")
        assert "outer" not in scope
        return []

    with patch.object(ns, "_resolve_nodes", side_effect=resolve):
        try:
            ns.resolve_nodes(None, [{"name": "outer"}])
        except RuntimeError as error:
            assert str(error) == "resolution failed"
        else:
            raise AssertionError("Resolution failure must propagate")
    assert scopes[0] is not scopes[1]
    assert ns._NAME_VIEW_MEMO.get() is None


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS: {len(tests)} node semantics tests")


if __name__ == "__main__":
    main()

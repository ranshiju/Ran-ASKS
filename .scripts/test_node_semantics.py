#!/usr/bin/env python3
"""双视图节点解析、语义召回和 alias 多映射回归。"""
import sqlite3
import sys
import tempfile
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
    assert "semantic_search(query*, scope, topk)" in prompt
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
        qa.gl.connect = lambda: old_connect(db_path)
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


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS: {len(tests)} node semantics tests")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""GraphDelta 两阶段建图的最小回归测试；只使用内存数据库。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_delta as gd
import graph_ingest as gi
import graph_lib as gl


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    return conn


def add_node(conn, path, title=None, node_type="entity"):
    gl.ensure_node(conn, path, title or path.rsplit("/", 1)[-1], node_type)


def prepare_source_skeleton(conn, delta):
    add_node(conn, delta.page, delta.title, "page")
    for raw_path in delta.raw_packages:
        add_node(conn, raw_path, gl.raw_node_title(raw_path), "raw")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '来源', ?, '机械', '', 0)",
            (delta.page, raw_path),
        )


def test_same_stem_raw_package_and_source_skeleton():
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {
            "title": "Example",
            "sources": [
                "academic/raw/works/example/paper.pdf",
                "academic/raw/works/example/paper.md#L10-L20",
            ],
        },
        [],
    )
    assert delta.raw_packages == ["academic/raw/works/example/paper"]
    source_edges = [edge for edge in delta.edges if edge["predicate"] == "来源"]
    assert len(source_edges) == 1
    assert source_edges[0]["subject"] == delta.page
    assert source_edges[0]["object"] == delta.raw_packages[0]


def test_exact_and_unique_alias_are_reused():
    conn = make_db()
    add_node(conn, "academic/wiki/topics/exact", "Exact")
    add_node(conn, "academic/wiki/topics/alias-target", "Alias Target")
    conn.execute(
        "INSERT INTO aliases(alias,node_path) VALUES(?,?)",
        ("Stable Alias", "academic/wiki/topics/alias-target"),
    )
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [
            {"subject": "本论文", "predicate": "涉及", "object": "academic/wiki/topics/exact"},
            {"subject": "本论文", "predicate": "涉及", "object": "Stable Alias"},
        ],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["merge_map"]["academic/wiki/topics/exact"] == "academic/wiki/topics/exact"
    assert plan["merge_map"]["Stable Alias"] == "academic/wiki/topics/alias-target"


def test_bilingual_name_reuses_unique_decomposed_title():
    conn = make_db()
    add_node(conn, "rag", "检索增强生成")
    add_node(conn, "rag-hub", "检索增强生成", "hub")
    add_node(conn, "rag-other", "研究核心定位Wiki与检索增强生成(RAG)")
    gl.insert_aliases(conn, "rag-other", ["RAG"])
    mention = "检索增强生成retrieval-augmented generation(RAG)"
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "核心方法", "object": mention}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["merge_map"][mention] == "rag"
    decision = next(item for item in plan["decisions"] if item["mention"] == mention)
    assert decision["action"] == "reuse_identity"
    assert decision["match_mode"] == "unique_decomposed_title_or_alias"


def test_citation_title_keeps_page_entity_type_ambiguity():
    conn = make_db()
    add_node(conn, "paper-page", "Shared Paper", "page")
    add_node(conn, "paper-entity", "Shared Paper", "entity")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "引用", "object": "Shared Paper"}],
    )
    plan = gd.plan_attachment(conn, delta)
    decision = next(item for item in plan["decisions"] if item["mention"] == "Shared Paper")
    assert decision["action"] == "abstain_ambiguous"


def test_dual_view_identity_resolution_is_applied_to_attach_plan():
    conn = make_db()
    add_node(conn, "mps", "matrix product state")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "涉及", "object": "matrix-product-state"}],
    )
    old_resolver = gd.ns.resolve_node
    gd.ns.resolve_node = lambda *_args, **_kwargs: {
        "decision": "resolved",
        "node_id": "mps",
        "match_mode": "dual_view_identity",
        "reason": "lexical_signal_and_label_semantic_gate",
        "candidates": [{"node_id": "mps"}],
    }
    try:
        plan = gd.plan_attachment(conn, delta)
    finally:
        gd.ns.resolve_node = old_resolver
    assert plan["merge_map"]["matrix-product-state"] == "mps"
    assert plan["decisions"][0]["action"] == "reuse_identity"


def test_ambiguous_name_abstains():
    conn = make_db()
    for path in ("academic/wiki/topics/a", "academic/wiki/topics/b"):
        add_node(conn, path, "Shared")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "涉及", "object": "Shared"}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["counts"]["ambiguous"] == 1
    assert "Shared" not in plan["merge_map"]
    assert plan["decisions"][0]["action"] == "abstain_ambiguous"


def test_surface_mention_equal_to_path_still_checks_shared_name_targets():
    conn = make_db()
    add_node(conn, "Shared", "第一义项")
    add_node(conn, "other", "第二义项")
    conn.execute("INSERT INTO aliases(alias,node_path) VALUES('Shared','other')")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "涉及", "object": "Shared"}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["decisions"][0]["action"] == "abstain_ambiguous"
    assert "Shared" not in plan["merge_map"]


def test_explicit_canonical_endpoint_reuses_path_without_name_disambiguation():
    conn = make_db()
    add_node(conn, "Shared", "第一义项")
    add_node(conn, "other", "第二义项")
    conn.execute("INSERT INTO aliases(alias,node_path) VALUES('Shared','other')")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{
            "subject": "本论文", "predicate": "涉及", "object": "Shared",
            "object_is_canonical": True,
        }],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["decisions"][0]["action"] == "reuse_canonical_id"
    assert plan["merge_map"]["Shared"] == "Shared"


def test_context_can_disambiguate_same_name_surface_mention():
    conn = make_db()
    add_node(conn, "state", "张量网络")
    add_node(conn, "algorithm", "张量网络")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "用于表示量子态", "object": "张量网络"}],
    )
    old_resolver = gd.ns.resolve_node
    gd.ns.resolve_node = lambda *_args, **_kwargs: {
        "decision": "resolved",
        "node_id": "state",
        "match_mode": "context_disambiguated_alias",
        "reason": "ambiguous_name_resolved_by_description_context",
    }
    try:
        plan = gd.plan_attachment(conn, delta)
    finally:
        gd.ns.resolve_node = old_resolver
    assert plan["merge_map"]["张量网络"] == "state"


def test_writer_obeys_ambiguous_abstention():
    conn = make_db()
    page = "academic/wiki/papers/example"
    add_node(conn, page, "Example", "page")
    for path in ("academic/wiki/topics/a", "academic/wiki/topics/b"):
        add_node(conn, path, "Shared")
    triples = [{"subject": "本论文", "predicate": "涉及", "object": "Shared"}]
    delta = gd.build_document_delta(page, {"title": "Example"}, triples)
    plan = gd.plan_attachment(conn, delta)
    result, _report = gd.fuse_with_savepoint(
        conn,
        delta,
        lambda: gi.add_knowledge_edges(
            conn, page, gd.knowledge_edges(delta), attach_plan=plan
        ),
    )
    assert result.resolve_ambig == 1
    edge = conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='涉及'", (page,)
    ).fetchone()
    assert edge is None
    assert not gl.node_exists(conn, "Shared")


def test_query_probes_cover_anchor_raw_and_two_hop_boundary():
    conn = make_db()
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {
            "title": "Example",
            "sources": ["academic/raw/works/example/paper.md"],
        },
        [
            {"subject": "本论文", "predicate": "提出", "object": "Claim"},
            {"subject": "Claim", "predicate": "涉及", "object": "Concept"},
        ],
    )
    plan = gd.plan_attachment(conn, delta)
    probes = gd.run_query_probes(conn, delta, plan)
    assert probes["anchor_hit"]
    assert probes["raw_reachable_one_hop"]
    assert probes["boundary_reachable_within_2"] == 2
    assert probes["boundary_path_success"] == 1.0


def test_empty_endpoint_and_self_loop_are_hard_errors():
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [
            {"subject": "", "predicate": "涉及", "object": "Concept"},
            {"subject": "Same", "predicate": "相关", "object": "Same"},
        ],
    )
    conn = make_db()
    add_node(conn, delta.page, delta.title, "page")
    try:
        gd.fuse_with_savepoint(conn, delta, lambda: None)
    except gd.DeltaContractError:
        pass
    else:
        raise AssertionError("hard errors must reject fusion")


def test_writer_failure_rolls_back_savepoint():
    conn = make_db()
    delta = gd.build_document_delta(
        "academic/wiki/papers/example", {"title": "Example"}, []
    )
    add_node(conn, delta.page, delta.title, "page")

    def failing_writer():
        add_node(conn, "temporary-node")
        raise RuntimeError("writer failed")

    try:
        gd.fuse_with_savepoint(conn, delta, failing_writer)
    except RuntimeError:
        pass
    else:
        raise AssertionError("writer exception must escape")
    assert not gl.node_exists(conn, "temporary-node")


def test_writer_success_releases_savepoint():
    conn = make_db()
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example", "sources": ["academic/raw/example/document.md"]},
        [],
    )
    prepare_source_skeleton(conn, delta)

    def writer():
        add_node(conn, "committed-node")
        return "ok"

    result, report = gd.fuse_with_savepoint(conn, delta, writer)
    assert result == "ok"
    assert report["fusion"]["committed"]
    assert gl.node_exists(conn, "committed-node")


def test_soft_probe_failure_does_not_block_commit():
    conn = make_db()
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "Detached A", "predicate": "相关", "object": "Detached B"}],
    )
    add_node(conn, delta.page, delta.title, "page")
    inspection = gd.inspect_delta(conn, delta)
    assert inspection["query_probes"]["boundary_path_success"] == 0.0
    _, report = gd.fuse_with_savepoint(
        conn, delta, lambda: add_node(conn, "still-committed"), inspection=inspection
    )
    assert report["fusion"]["soft_probe_blocking"] is False
    assert gl.node_exists(conn, "still-committed")


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS: {len(tests)} GraphDelta tests")


if __name__ == "__main__":
    main()

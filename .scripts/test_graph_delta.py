#!/usr/bin/env python3
"""GraphDelta 两阶段建图的最小回归测试；只使用内存数据库。"""
import sqlite3
import sys
import tempfile
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


def test_locked_venue_uses_predicate_specific_identity_key():
    conn = make_db()
    existing = "the 2024 Conference on Empirical Methods in Natural Language Processing"
    mention = "Proceedings of the 2024 Conference on Empirical Methods in Natural Language Processing"
    add_node(conn, existing, existing)
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{
            "subject": "本论文", "predicate": "发表于", "object": mention,
            "object_metadata_kind": "venue",
        }],
        deterministic_triple_count=1,
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["merge_map"][mention] == existing
    assert plan["abstained"] == []
    assert plan["decisions"][0]["action"] == "reuse_deterministic_metadata"


def test_locked_venue_without_match_creates_local_metadata_node():
    conn = make_db()
    mention = "Proceedings of the Example Conference 2030"
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{
            "subject": "本论文", "predicate": "发表于", "object": mention,
            "object_metadata_kind": "venue",
        }],
        deterministic_triple_count=1,
    )
    plan = gd.plan_attachment(conn, delta)
    assert mention in plan["new_nodes"]
    assert plan["decisions"][0]["action"] == "create_deterministic_metadata"


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


def test_leading_multiword_eponym_creates_local_proposition():
    conn = make_db()
    target = "中间测量仅限计算基下完全测量"
    gl.ensure_node(
        conn, target, target, "entity", entity_subtype="proposition",
    )
    gl.insert_aliases(conn, target, ["von Neumann"])
    mention = "von Neumann熵量化协作协助下可获得的最大额外量子相干性"
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "提出", "object": mention}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert mention not in plan["merge_map"]
    assert mention in plan["new_nodes"]
    decision = next(item for item in plan["decisions"] if item["mention"] == mention)
    assert decision["action"] == "create_local"


def test_proposition_slot_does_not_collapse_to_embedded_keyword_alias():
    conn = make_db()
    gl.ensure_node(conn, "DMRG", "DMRG", "entity", entity_subtype="keyword")
    gl.insert_aliases(conn, "DMRG", ["DMRG"])
    mention = "通过逐步匹配约化密度矩阵的确定性DMRG式训练算法给出泛化误差的解析预测"
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "核心创新点", "object": mention}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert mention not in plan["merge_map"]
    assert mention in plan["new_nodes"]
    decision = next(item for item in plan["decisions"] if item["mention"] == mention)
    assert decision["action"] == "create_local_proposition"
    assert decision["target"] == mention


def test_proposition_slot_reuses_only_exact_existing_proposition():
    conn = make_db()
    mention = "理论预测依赖训练集平均而实验产生不同权重"
    gl.ensure_node(conn, mention, mention, "entity", entity_subtype="proposition")
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "局限性", "object": mention}],
    )
    plan = gd.plan_attachment(conn, delta)
    assert plan["merge_map"][mention] == mention
    decision = next(item for item in plan["decisions"] if item["mention"] == mention)
    assert decision["action"] == "reuse_unique_proposition"


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


def test_semantic_ambiguity_without_exact_collision_keeps_local_edge_and_gloss():
    conn = make_db()
    page = "academic/wiki/papers/example"
    mention = "Feynman-Vernon影响泛函"
    add_node(conn, page, "Example", "page")
    delta = gd.build_document_delta(
        page,
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "研究基础", "object": mention}],
        concept_glosses=[{
            "mention": mention,
            "description": "线性耦合到谐振子浴时可解析求得的影响泛函。",
            "source": "academic/raw/references/example/paper.md#L55",
        }],
    )
    old_resolver = gd.ns.resolve_node
    gd.ns.resolve_node = lambda *_args, **_kwargs: {
        "decision": "ambiguous",
        "reason": "semantic_identity_ambiguous",
        "candidates": [{"node_id": "影响泛函influence functional"}],
    }
    try:
        plan = gd.plan_attachment(conn, delta)
    finally:
        gd.ns.resolve_node = old_resolver
    assert plan["decisions"][0]["action"] == "keep_local_ambiguous"
    assert mention in plan["new_nodes"]
    assert mention not in plan["abstained"]

    result, _report = gd.fuse_with_savepoint(
        conn,
        delta,
        lambda: gi.add_knowledge_edges(
            conn, page, gd.knowledge_edges(delta), attach_plan=plan,
            concept_glosses=delta.concept_glosses,
        ),
    )
    assert result.edges_added == 1
    edge = conn.execute(
        "SELECT object FROM edges WHERE subject=? AND predicate='研究基础'", (page,)
    ).fetchone()
    assert edge and edge[0] == mention
    node = conn.execute(
        "SELECT description FROM nodes WHERE path=?", (mention,)
    ).fetchone()
    assert node and "谐振子浴" in node[0]
    gloss = conn.execute(
        "SELECT source,description,is_primary FROM node_glosses WHERE node_path=?",
        (mention,),
    ).fetchone()
    assert gloss[0].endswith("paper.md#L55")
    assert gloss[2] == 1


def test_later_gloss_does_not_overwrite_nonempty_canonical_description():
    conn = make_db()
    add_node(conn, "concept", "Concept")
    conn.execute("UPDATE nodes SET entity_subtype='keyword',description='已有主描述' WHERE path='concept'")
    assert gl.add_node_gloss(
        conn, "concept", "academic/wiki/papers/later",
        "academic/raw/references/later/paper.md#L10", "后续论文局部说明",
    )
    node = conn.execute("SELECT description FROM nodes WHERE path='concept'").fetchone()
    gloss = conn.execute(
        "SELECT description,is_primary FROM node_glosses WHERE node_path='concept'"
    ).fetchone()
    assert node[0] == "已有主描述"
    assert tuple(gloss) == ("后续论文局部说明", 0)


def test_reused_keyword_receives_source_local_gloss():
    conn = make_db()
    page = "academic/wiki/papers/example"
    mention = "费曼-弗农影响泛函Feynman-Vernon influence functional(IF)"
    add_node(conn, page, "Example", "page")
    gl.ensure_node(conn, mention, mention, "entity", entity_subtype="keyword")
    delta = gd.build_document_delta(
        page,
        {"title": "Example"},
        [{"subject": "本论文", "predicate": "研究基础", "object": mention}],
        concept_glosses=[{
            "mention": mention,
            "description": "线性耦合到谐振子浴时可解析求得的影响泛函。",
            "source": "academic/raw/references/example/paper.md#L55",
        }],
    )
    plan = gd.plan_attachment(conn, delta)
    gi.add_knowledge_edges(
        conn, page, gd.knowledge_edges(delta), attach_plan=plan,
        concept_glosses=delta.concept_glosses,
    )
    row = conn.execute(
        "SELECT source,is_primary FROM node_glosses WHERE node_path=?", (mention,)
    ).fetchone()
    assert tuple(row) == ("academic/raw/references/example/paper.md#L55", 1)
    description = conn.execute(
        "SELECT description FROM nodes WHERE path=?", (mention,)
    ).fetchone()[0]
    assert "谐振子浴" in description


def test_clean_page_promotes_remaining_gloss_without_overwriting_manual_description():
    conn = make_db()
    gl.ensure_node(conn, "concept", "Concept", "entity", entity_subtype="keyword")
    gl.add_node_gloss(
        conn, "concept", "academic/wiki/papers/first",
        "academic/raw/references/first/paper.md#L1", "第一来源说明",
    )
    gl.add_node_gloss(
        conn, "concept", "academic/wiki/papers/second",
        "academic/raw/references/second/paper.md#L2", "第二来源说明",
    )
    report = gi.clean_page_edges(conn, "academic/wiki/papers/first")
    assert report["node_glosses_removed"] == 1
    node = conn.execute("SELECT description FROM nodes WHERE path='concept'").fetchone()
    remaining = conn.execute(
        "SELECT description,is_primary FROM node_glosses WHERE node_path='concept'"
    ).fetchone()
    assert node[0] == "第二来源说明"
    assert tuple(remaining) == ("第二来源说明", 1)

    conn.execute("UPDATE nodes SET description='人工主描述' WHERE path='concept'")
    gi.clean_page_edges(conn, "academic/wiki/papers/second")
    node = conn.execute("SELECT description FROM nodes WHERE path='concept'").fetchone()
    assert node[0] == "人工主描述"


def test_merge_nodes_preserves_source_gloss_and_target_description_priority():
    conn = make_db()
    gl.ensure_node(
        conn, "source", "Source", "entity", entity_subtype="keyword",
        description="来源主描述",
    )
    gl.ensure_node(
        conn, "target", "Target", "entity", entity_subtype="keyword",
        description="目标主描述",
    )
    gl.add_node_gloss(
        conn, "source", "academic/wiki/papers/source",
        "academic/raw/references/source/paper.md#L3", "来源局部说明",
    )
    gi.merge_nodes(conn, "source", "target")
    node = conn.execute("SELECT description FROM nodes WHERE path='target'").fetchone()
    gloss = conn.execute(
        "SELECT node_path,description,is_primary FROM node_glosses WHERE node_path='target'"
    ).fetchone()
    assert node[0] == "目标主描述"
    assert tuple(gloss) == ("target", "来源局部说明", 0)


def test_concept_gloss_parser_and_raw_locator_choose_specific_citation():
    semantic = """三元组:
本论文 | 研究基础 | Feynman-Vernon影响泛函
概念说明:
Feynman-Vernon影响泛函 | Feynman 与 Vernon 在线性耦合谐振子浴下推导的解析影响泛函。
"""
    glosses = gi.parse_concept_glosses(semantic)
    assert len(glosses) == 1
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = root / "academic/raw/references/example/paper.md"
        wiki = root / "academic/wiki/papers/example.md"
        raw.parent.mkdir(parents=True)
        wiki.parent.mkdir(parents=True)
        raw.write_text(
            "一般量子动力学背景。\n"
            "Feynman and Vernon derived an analytical influence functional "
            "for linear coupling to a harmonic bath.\n",
            encoding="utf-8",
        )
        wiki.write_text(
            "## Content\n\n"
            "一般背景。[^r1]\n"
            "Feynman-Vernon 影响泛函用于线性耦合谐振子浴。[^r2]\n\n"
            "## Sources\n\n"
            "[^r1]: academic/raw/references/example/paper.md#L1\n"
            "[^r2]: academic/raw/references/example/paper.md#L2\n",
            encoding="utf-8",
        )
        original_gl_repo = gl.REPO
        original_wl_repo = gi.wl.REPO
        original_locator_repo = gi.wl.raw_locator.REPO
        try:
            gl.REPO = root
            gi.wl.REPO = root
            gi.wl.raw_locator.REPO = root
            located, report = gi.attach_concept_gloss_sources(
                glosses, "academic/wiki/papers/example"
            )
        finally:
            gl.REPO = original_gl_repo
            gi.wl.REPO = original_wl_repo
            gi.wl.raw_locator.REPO = original_locator_repo
    assert report == {"located_glosses": 1, "unlocated_glosses": 0}
    assert located[0]["source"] == "academic/raw/references/example/paper.md#L2"


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
    assert report["fusion"]["transaction_state"] == "savepoint_released"
    assert report["fusion"]["outer_commit_required"] is True
    receipt = report["validation_receipt"]
    assert receipt["contract_version"] == "graph-delta-v1"
    assert receipt["status"] == "validated"
    assert receipt["savepoint"] == "released"
    assert len(receipt["delta_sha256"]) == 64
    assert len(receipt["attach_plan_sha256"]) == 64
    assert gl.node_exists(conn, "committed-node")


def test_validation_receipt_is_content_addressed_and_stable():
    conn = make_db()
    delta = gd.build_document_delta(
        "academic/wiki/papers/example",
        {"title": "Example"},
        [{"subject": "Example", "predicate": "涉及", "object": "Concept"}],
    )
    add_node(conn, delta.page, delta.title, "page")
    first = gd.inspect_delta(conn, delta)["validation_receipt"]
    second = gd.inspect_delta(conn, delta)["validation_receipt"]
    assert first["delta_sha256"] == second["delta_sha256"]
    assert first["attach_plan_sha256"] == second["attach_plan_sha256"]
    changed = gd.build_document_delta(
        delta.page, {"title": "Example"},
        [{"subject": "Example", "predicate": "涉及", "object": "Other"}],
    )
    third = gd.inspect_delta(conn, changed)["validation_receipt"]
    assert third["delta_sha256"] != first["delta_sha256"]


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

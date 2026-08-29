#!/usr/bin/env python3
"""Hub Scope canonical 定义、路由与 Agent 生命周期回归。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import hub_semantics as hs


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    return conn


class TempRepo:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_hs, self.old_gl = hs.REPO, gl.REPO
        hs.REPO = self.root
        gl.REPO = self.root
        return self.root

    def __exit__(self, *_args):
        hs.REPO, gl.REPO = self.old_hs, self.old_gl
        self.tmp.cleanup()


def test_configured_page_root_isolates_hub_lifecycle_files():
    conn = make_db()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        hs.configure_page_root(root)
        try:
            report = hs.create_hub(
                conn,
                path="academic/wiki/hubs/e1-isolated-test",
                title="隔离实验方向",
                scope="研究隔离实验中的知识结构、导航行为与持续编译动力学问题。",
                agent_confirmed=True,
            )
            assert (root / f"{report['created']}.md").is_file()
            assert hs.read_hub_scope(report["created"])
        finally:
            hs.configure_page_root(None)


def test_empty_hub_index_feeds_unassigned_nodes_to_new_hub_discovery():
    conn = make_db()
    for index in range(hs.NEW_HUB_MIN_MEMBERS):
        node = f"concept-{index}"
        gl.ensure_node(
            conn, node, node, "entity",
            entity_subtype="keyword", description="同一主题的量子结构",
        )
    old_embed = hs._embed
    hs._embed = lambda texts: np.array([[1.0, 0.0] for _ in texts], dtype=float)
    try:
        plan = hs.dynamics_plan(conn)
    finally:
        hs._embed = old_embed
    assert all(item["decision"] == "unassigned" for item in plan["membership"]["nodes"])
    assert plan["new_hubs"]["decision"] == "agent_definition_required"
    assert plan["new_hubs"]["candidates"]


def write_page(root: Path, rel: str, text: str):
    path = root / f"{rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def hub_text(title: str, scope: str = "", status: str = "active") -> str:
    section = f"\n## Scope\n\n{scope}\n" if scope else ""
    return (
        f"---\ntitle: {title}\ntype: topic-hub\nhub_subtype: research-direction\n"
        f"status: {status}\n---\n\n# {title}\n{section}"
    )


def paper_text(profile: str) -> str:
    return (
        "---\ntitle: Paper\ntype: paper-summary\n---\n\n# Paper\n\n"
        f"## 研究方向定位\n\n{profile}[^r1]\n\n"
        "## Sources\n\n[^r1]: academic/raw/references/paper/paper.md#L10-L12\n"
    )


def people_text(name: str, portrait: str) -> str:
    return (
        f"---\ntitle: {name}\ntype: people\n---\n\n# {name}\n\n"
        f"## 人物画像\n\n{portrait}\n\n## Navigation\n\n- 导航入口\n"
    )


def add_hub(conn, root: Path, path: str, title: str, scope: str):
    write_page(root, path, hub_text(title, scope))
    gl.ensure_node(conn, path, title, "hub", description=scope)


def test_paper_profile_is_precisely_locatable():
    with TempRepo() as root:
        write_page(root, "academic/wiki/papers/paper", paper_text("研究开放量子系统中的纠缠动力学。"))
        profile = hs.read_paper_profile("academic/wiki/papers/paper")
        assert profile is not None
        assert profile.locator.endswith("#研究方向定位")
        assert profile.text == "研究开放量子系统中的纠缠动力学。"
        assert profile.raw_citations == ("academic/raw/references/paper/paper.md#L10-L12",)


def test_people_portraits_are_locatable_and_role_neutral():
    conn = make_db()
    portraits = {
        "researcher": "研究开放量子系统中的纠缠动力学，主要使用张量网络方法。",
        "administrator": "负责研究生教务协调、培养流程与学生服务。",
        "student": "博士研究生阶段，关注量子信息与张量网络数值方法。",
    }
    with TempRepo() as root:
        for role, portrait in portraits.items():
            path = f"academic/wiki/authors/{role}"
            write_page(root, path, people_text(role, portrait))
            gl.ensure_node(conn, path, role, "people")
            profile = hs.node_profile(conn, path)
            assert profile is not None
            assert profile.kind == "people"
            assert profile.text == portrait
            assert profile.locator.endswith("#人物画像")


def test_only_people_page_with_portrait_participates():
    conn = make_db()
    with TempRepo() as root:
        page = "academic/wiki/authors/admin"
        write_page(root, page, people_text("Admin", "负责科研项目的行政协调与公共服务。"))
        gl.ensure_node(conn, page, "Admin", "people")
        gl.ensure_node(conn, "Light Person", "Light Person", "entity", entity_subtype="person")
        gl.ensure_node(conn, "academic/wiki/authors/no-portrait", "No Portrait", "people")
        write_page(root, "academic/wiki/authors/no-portrait", "---\ntitle: No Portrait\ntype: people\n---\n")
        ids = {item.node_id for item in hs.ordinary_profiles(conn)}
        assert page in ids
        assert "Light Person" not in ids
        assert "academic/wiki/authors/no-portrait" not in ids


def test_keyword_and_proposition_profiles_use_description():
    conn = make_db()
    gl.ensure_node(conn, "K", "量子路由", "entity", entity_subtype="keyword",
                   description="在量子网络中选择传输路径。")
    gl.ensure_node(conn, "P", "纠缠增长受限", "entity", entity_subtype="proposition",
                   description="有限维缠络限制可表示的纠缠。")
    assert hs.node_profile(conn, "K").text == "量子路由。在量子网络中选择传输路径。"
    assert hs.node_profile(conn, "P").kind == "proposition"


def test_overlapping_membership_and_targeted_apply():
    conn = make_db()
    with TempRepo() as root:
        add_hub(conn, root, "academic/wiki/hubs/q", "量子系统",
                "研究量子系统的状态、演化及其信息处理问题。")
        add_hub(conn, root, "academic/wiki/hubs/m", "机器学习",
                "研究机器学习模型的训练、推理及其应用问题。")
        gl.ensure_node(conn, "qm", "量子机器学习", "entity", entity_subtype="keyword")
        gl.ensure_node(conn, "untouched", "不受影响节点", "entity", entity_subtype="keyword")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence) VALUES(?,?,?,?)",
            ("untouched", hs.MEMBERSHIP_PREDICATE, "academic/wiki/hubs/q", "推断"),
        )
        old_embed = hs._embed
        def fake_embed(texts):
            vectors = []
            for text in texts:
                if text == "量子机器学习":
                    vectors.append([1.0, 1.0])
                elif "机器学习模型" in text:
                    vectors.append([0.0, 1.0])
                else:
                    vectors.append([1.0, 0.0])
            return np.array(vectors, dtype=float)
        hs._embed = fake_embed
        try:
            plan = hs.plan_memberships(conn, ["qm"])
            report = hs.apply_membership_plan(conn, plan)
        finally:
            hs._embed = old_embed
        assert report == {"applied": True, "nodes": 1, "edges": 2}
        memberships = {row[0] for row in conn.execute(
            "SELECT object FROM edges WHERE subject='qm' AND predicate=?",
            (hs.MEMBERSHIP_PREDICATE,),
        )}
        assert memberships == {"academic/wiki/hubs/q", "academic/wiki/hubs/m"}
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject='untouched' AND predicate=?",
            (hs.MEMBERSHIP_PREDICATE,),
        ).fetchone()


def test_membership_hysteresis_and_embedding_failure_preserve_edges():
    conn = make_db()
    with TempRepo() as root:
        hub = "academic/wiki/hubs/h"
        add_hub(conn, root, hub, "动力学", "研究复杂系统的演化、稳定性与非平衡动力学问题。")
        gl.ensure_node(conn, "old", "旧边界主题", "entity", entity_subtype="keyword")
        gl.ensure_node(conn, "new", "新边界主题", "entity", entity_subtype="keyword")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,score) VALUES(?,?,?,?,?)",
            ("old", hs.MEMBERSHIP_PREDICATE, hub, "推断", 0.7),
        )
        old_embed = hs._embed
        hs._embed = lambda texts: np.array(
            [[1.0, 0.0] if text == "新边界主题" else [0.6, 0.8] for text in texts], dtype=float
        )
        try:
            plan = hs.plan_memberships(conn, ["old", "new"])
        finally:
            hs._embed = old_embed
        by_id = {item["node_id"]: item for item in plan["nodes"]}
        assert by_id["old"]["memberships"] == [hub]
        assert by_id["new"]["memberships"] == []
        hs._embed = lambda _texts: None
        try:
            degraded = hs.plan_memberships(conn, ["old"])
            report = hs.apply_membership_plan(conn, degraded)
        finally:
            hs._embed = old_embed
        assert not report["applied"]
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject='old' AND predicate=? AND object=?",
            (hs.MEMBERSHIP_PREDICATE, hub),
        ).fetchone()


def test_new_hub_analysis_only_returns_candidate():
    conn = make_db()
    for index in range(4):
        gl.ensure_node(conn, f"n{index}", f"紧密主题{index}", "entity", entity_subtype="keyword")
    old_embed = hs._embed
    hs._embed = lambda texts: np.array([[1.0, 0.0] for _ in texts], dtype=float)
    try:
        report = hs.analyze_new_hubs(conn)
    finally:
        hs._embed = old_embed
    assert report["decision"] == "agent_definition_required"
    assert len(report["candidates"][0]["members"]) == 4
    assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='hub'").fetchone()[0] == 0


def test_auto_create_check_finds_eligible_and_apply_creates_hubs():
    conn = make_db()
    with TempRepo() as root:
        parent_hub = "academic/wiki/hubs/parent"
        add_hub(conn, root, parent_hub, "父方向",
                "研究父方向的基础理论、核心方法与关键科学问题。")
        for index in range(5):
            gl.ensure_node(conn, f"k{index}", f"紧密主题{index}", "entity",
                           entity_subtype="keyword")
        old_embed = hs._embed
        def mock_embed(texts):
            rows = []
            for t in texts:
                if "父方向" in t or "基础理论" in t:
                    rows.append([0.0, 1.0])
                else:
                    rows.append([1.0, 0.0])
            return np.array(rows, dtype=float)
        hs._embed = mock_embed
        try:
            check = hs.auto_create_check(conn)
        finally:
            hs._embed = old_embed
        assert check["status"] == "agent_required"
        assert len(check["eligible"]) == 1
        candidate = check["eligible"][0]
        assert candidate["cohesion"] >= hs.AUTO_CREATE_COHESION
        assert len(candidate["members"]) >= hs.AUTO_CREATE_MIN_MEMBERS
        assert "suggested_parent" in candidate
        definitions = [{
            "title": "紧密主题方向",
            "scope": "研究紧密主题方向的核心科学问题、方法论与实验验证框架。",
            "parent": parent_hub,
            "members": candidate["members"],
        }]
        result = hs.create_hubs_from_definitions(conn, definitions)
        assert len(result["created"]) == 1
        assert not result["errors"]
        hub_path = "academic/wiki/hubs/紧密主题方向"
        assert gl.node_exists(conn, hub_path)
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate=?",
            (hub_path, "子方向"),
        ).fetchone() or conn.execute(
            "SELECT 1 FROM edges WHERE object=? AND predicate=?",
            (hub_path, "子方向"),
        ).fetchone()


def test_refresh_after_ingest_is_local_and_membership_only():
    conn = make_db()
    with TempRepo() as root:
        hub = "academic/wiki/hubs/q"
        add_hub(conn, root, hub, "量子系统",
                "研究量子系统的状态、演化及其信息处理问题。")
        page = "academic/wiki/papers/p"
        concept = "量子态"
        gl.ensure_node(conn, page, "Paper", "page")
        gl.ensure_node(conn, concept, concept, "entity", entity_subtype="keyword")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence) VALUES(?,?,?,?)",
            (page, "核心方法", concept, "可追溯"),
        )
        old_embed = hs._embed
        hs._embed = lambda texts: np.array([[1.0, 0.0] for _ in texts], dtype=float)
        try:
            report = hs.refresh_after_ingest(conn, page)
        finally:
            hs._embed = old_embed
        assert report["membership_apply"]["applied"]
        assert report["lifecycle_auto_applied"] is False
        assert conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate=? AND object=?",
            (concept, hs.MEMBERSHIP_PREDICATE, hub),
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM edges WHERE subject=? AND predicate=?", (page, hs.MEMBERSHIP_PREDICATE)
        ).fetchone()


def test_scope_route_requires_canonical_scope_and_margin():
    definitions = [
        hs.HubDefinition("open", "开放系统", "研究开放量子系统的动力学与耗散问题。", "", "active", True, "scope"),
        hs.HubDefinition("legacy", "量子系统", "量子系统", "", "active", False, "legacy_title"),
    ]
    old_embed = hs._embed
    hs._embed = lambda _texts: np.array([[1.0, 0.0], [0.99, 0.01], [0.5, 0.5]])
    try:
        result = hs.route_profile("开放量子系统纠缠动力学", definitions)
    finally:
        hs._embed = old_embed
    assert result["decision"] == "resolved"
    assert result["node_id"] == "open"


def test_legacy_title_is_candidate_not_canonical_identity():
    definitions = [
        hs.HubDefinition("legacy", "张量网络", "张量网络", "", "active", False, "legacy_title"),
    ]
    old_embed = hs._embed
    hs._embed = lambda _texts: np.array([[1.0, 0.0], [1.0, 0.0]])
    try:
        result = hs.route_profile("张量网络", definitions)
    finally:
        hs._embed = old_embed
    assert result["decision"] == "candidates"
    assert result["candidates"][0]["scope_mode"] == "legacy_title"


def test_agent_confirmed_create_writes_scope_without_seeds_or_keywords():
    conn = make_db()
    with TempRepo() as root:
        try:
            hs.create_hub(
                conn, path="academic/wiki/hubs/new", title="新方向",
                scope="研究开放量子系统中的非平衡动力学、耗散与纠缠演化问题。",
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("missing Agent confirmation must reject")
        report = hs.create_hub(
            conn, path="academic/wiki/hubs/new", title="新方向",
            scope="研究开放量子系统中的非平衡动力学、耗散与纠缠演化问题。",
            agent_confirmed=True,
        )
        text = (root / "academic/wiki/hubs/new.md").read_text(encoding="utf-8")
        row = conn.execute("SELECT description FROM nodes WHERE path=?", (report["created"],)).fetchone()
        assert "## Scope" in text
        assert "seeds:" not in text and "## 关键词" not in text
        assert row[0] == "研究开放量子系统中的非平衡动力学、耗散与纠缠演化问题。"


def test_split_requires_agent_scopes_and_passes_code_route_probe():
    conn = make_db()
    with TempRepo() as root:
        parent = "academic/wiki/hubs/parent"
        write_page(root, parent, hub_text("父方向", "研究两类不同的量子动力学与表示问题。"))
        gl.ensure_node(conn, parent, "父方向", "hub", description="研究两类不同的量子动力学与表示问题。")
        members = []
        for prefix, profile in (("a", "研究开放系统的耗散动力学。"), ("b", "研究张量网络态的数值表示。")):
            for index in range(2):
                page = f"academic/wiki/papers/{prefix}{index}"
                write_page(root, page, paper_text(profile))
                members.append((prefix, page))
        children = [
            {"path": "academic/wiki/hubs/a", "title": "耗散动力学",
             "scope": "研究开放量子系统中的耗散、退相干和非平衡动力学问题。",
             "members": [page for prefix, page in members if prefix == "a"]},
            {"path": "academic/wiki/hubs/b", "title": "张量网络表示",
             "scope": "研究利用张量网络表示并数值计算量子多体态及其演化的问题。",
             "members": [page for prefix, page in members if prefix == "b"]},
        ]
        old_embed = hs._embed
        def fake_embed(texts):
            return np.array([
                [1.0, 0.0] if ("耗散" in text or "开放系统" in text) else [0.0, 1.0]
                for text in texts
            ], dtype=float)
        hs._embed = fake_embed
        try:
            report = hs.apply_split(conn, parent, children, agent_confirmed=True)
        finally:
            hs._embed = old_embed
        assert len(report["created"]) == 2
        assert report["route_success"] == 1.0
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE predicate='子方向'").fetchone()[0] == 2


def test_merge_is_non_destructive_and_updates_survivor_scope():
    conn = make_db()
    with TempRepo() as root:
        for path, title in (("academic/wiki/hubs/a", "方向A"), ("academic/wiki/hubs/b", "方向B")):
            write_page(root, path, hub_text(title, "研究量子系统中的动力学、表示与计算问题。"))
            gl.ensure_node(conn, path, title, "hub", description="研究量子系统中的动力学、表示与计算问题。")
        report = hs.merge_hubs(
            conn, survivor="academic/wiki/hubs/a", retired="academic/wiki/hubs/b",
            scope="研究量子系统中的动力学、状态表示及其数值计算问题。",
            agent_confirmed=True,
        )
        assert report["redirect"]
        assert (root / "academic/wiki/hubs/b.md").is_file()
        assert conn.execute("SELECT status FROM nodes WHERE path='academic/wiki/hubs/b'").fetchone()[0] == "retired"
        assert conn.execute("SELECT 1 FROM edges WHERE subject=? AND predicate='合并至' AND object=?",
                            ("academic/wiki/hubs/b", "academic/wiki/hubs/a")).fetchone()


def test_blood_relation_traces_direction_edges():
    conn = make_db()
    with TempRepo() as root:
        add_hub(conn, root, "academic/wiki/hubs/root", "根方向",
                "研究量子多体物理中的基本理论、纠缠动力学与数值计算方法。")
        add_hub(conn, root, "academic/wiki/hubs/child", "子方向",
                "研究量子多体系统中的纠缠拓扑性质及其数值模拟方法。")
        add_hub(conn, root, "academic/wiki/hubs/grandchild", "孙方向",
                "研究拓扑序的矩阵乘积态表示及其纠缠谱计算。")
        add_hub(conn, root, "academic/wiki/hubs/unrelated", "无关方向",
                "研究经典统计力学中的相变理论与临界现象的标度分析。")
        conn.executemany(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '子方向', ?, '推断', '', 0)",
            [("academic/wiki/hubs/root", "academic/wiki/hubs/child"),
             ("academic/wiki/hubs/child", "academic/wiki/hubs/grandchild")],
        )
        conn.commit()
        ancestors = hs.get_ancestors(conn, "academic/wiki/hubs/grandchild")
        assert "academic/wiki/hubs/root" in ancestors
        assert "academic/wiki/hubs/child" in ancestors
        assert hs.has_blood_relation(
            conn, "academic/wiki/hubs/root", "academic/wiki/hubs/grandchild")
        assert hs.has_blood_relation(
            conn, "academic/wiki/hubs/root", "academic/wiki/hubs/child")
        assert not hs.has_blood_relation(
            conn, "academic/wiki/hubs/root", "academic/wiki/hubs/unrelated")
        children = hs.get_child_hubs(conn, "academic/wiki/hubs/root")
        assert "academic/wiki/hubs/child" in children


def test_hub_overload_detects_split_and_redistribute():
    conn = make_db()
    with TempRepo() as root:
        # Hub with 21 members, no children -> split_candidate
        add_hub(conn, root, "academic/wiki/hubs/big", "大Hub",
                "研究量子信息科学中的纠缠、退相干与量子纠错问题。")
        for i in range(21):
            nid = f"kw{i}"
            gl.ensure_node(conn, nid, f"关键词{i}", "entity", entity_subtype="keyword",
                           description=f"第{i}个测试关键词的语义描述。")
            conn.execute(
                "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr,score) "
                "VALUES(?, '聚类于', ?, '推断', '', 0, NULL)",
                (nid, "academic/wiki/hubs/big"))
        conn.commit()
        result = hs._check_hub_overload(conn)
        assert len(result) == 1
        assert result[0]["hub"] == "academic/wiki/hubs/big"
        assert result[0]["member_count"] == 21
        assert result[0]["action"] == "split_candidate"

        # Add child -> action becomes redistribute
        add_hub(conn, root, "academic/wiki/hubs/bigchild", "子Hub",
                "研究量子纠错码的拓扑性质与容错量子计算方法。")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '子方向', ?, '推断', '', 0)",
            ("academic/wiki/hubs/big", "academic/wiki/hubs/bigchild"))
        conn.commit()
        result = hs._check_hub_overload(conn)
        big = [r for r in result if r["hub"] == "academic/wiki/hubs/big"][0]
        assert big["action"] == "redistribute"


def test_split_rejects_indistinct_scopes():
    conn = make_db()
    with TempRepo() as root:
        parent = "academic/wiki/hubs/parent"
        write_page(root, parent, hub_text("父方向", "研究两类不同的量子动力学与表示问题。"))
        gl.ensure_node(conn, parent, "父方向", "hub",
                       description="研究两类不同的量子动力学与表示问题。")
        members = []
        for prefix, profile in (("a", "研究开放系统的耗散动力学。"),
                                ("b", "研究张量网络态的数值表示。")):
            for index in range(2):
                page = f"academic/wiki/papers/{prefix}{index}"
                write_page(root, page, paper_text(profile))
                members.append((prefix, page))
        children = [
            {"path": "academic/wiki/hubs/a", "title": "耗散动力学",
             "scope": "研究开放量子系统中的耗散、退相干和非平衡动力学问题。",
             "members": [page for prefix, page in members if prefix == "a"]},
            {"path": "academic/wiki/hubs/b", "title": "张量网络表示",
             "scope": "研究利用张量网络表示并数值计算量子多体态及其演化的问题。",
             "members": [page for prefix, page in members if prefix == "b"]},
        ]
        old_embed = hs._embed
        # All texts map to same vector -> Scope cosine = 1.0 >= 0.85
        hs._embed = lambda _texts: np.array([[1.0, 0.0] for _ in _texts], dtype=float)
        try:
            try:
                hs.apply_split(conn, parent, children, agent_confirmed=True)
            except ValueError as exc:
                assert "区分度不足" in str(exc) or "Scope" in str(exc)
            else:
                raise AssertionError("indistinct scopes must be rejected")
        finally:
            hs._embed = old_embed


def test_merge_excludes_blood_related_hubs():
    conn = make_db()
    with TempRepo() as root:
        add_hub(conn, root, "academic/wiki/hubs/parent", "父Hub",
                "研究量子多体物理中的基本理论、纠缠动力学与数值计算方法。")
        add_hub(conn, root, "academic/wiki/hubs/child", "子Hub",
                "研究量子多体系统中的纠缠拓扑性质及其数值模拟方法。")
        add_hub(conn, root, "academic/wiki/hubs/other", "无关Hub",
                "研究量子多体物理中的基本理论、纠缠动力学与数值计算方法。")
        conn.execute(
            "INSERT INTO edges(subject,predicate,object,confidence,source,is_sr) "
            "VALUES(?, '子方向', ?, '推断', '', 0)",
            ("academic/wiki/hubs/parent", "academic/wiki/hubs/child"))
        conn.commit()
        old_embed = hs._embed
        # All hubs identical embedding -> all pairs >= 0.88 merge threshold
        hs._embed = lambda _texts: np.array([[1.0, 0.0] for _ in _texts], dtype=float)
        try:
            result = hs.analyze_merge_candidates(conn)
        finally:
            hs._embed = old_embed
        candidates = result.get("candidates", [])
        pairs = {(c["left"], c["right"]) for c in candidates}
        # parent-child pair must be excluded (blood relation)
        assert ("academic/wiki/hubs/parent", "academic/wiki/hubs/child") not in pairs
        assert ("academic/wiki/hubs/child", "academic/wiki/hubs/parent") not in pairs
        # parent-other pair should be a candidate (no blood relation)
        assert any(
            ("academic/wiki/hubs/parent", "academic/wiki/hubs/other") == (c["left"], c["right"])
            or ("academic/wiki/hubs/other", "academic/wiki/hubs/parent") == (c["left"], c["right"])
            for c in candidates
        )



def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS: {len(tests)} Hub semantics tests")


if __name__ == "__main__":
    main()

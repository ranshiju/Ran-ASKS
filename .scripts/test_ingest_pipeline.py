#!/usr/bin/env python3
"""摄入关键链路回归：作者抽取、方向配置、语义槽解析和图校验。"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import hashlib
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import graph_ingest
import ingest_check
import ingest_pipeline
import wiki_skeleton
from wiki_skeleton import extract_authors_from_text


def test_nature_author_block():
    raw = """# Quantum machine learning

Jacob B iamonte<sup>1,2</sup>, Peter Wittek<sup>3</sup>, Nicola P ancotti<sup>4</sup>, Patrick R ebentrost<sup>5</sup>, Nathan Wiebe<sup>6</sup> & Seth Lloyd

Abstract
"""
    assert extract_authors_from_text(raw) == [
        "Jacob Biamonte", "Peter Wittek", "Nicola Pancotti", "Patrick Rebentrost",
        "Nathan Wiebe", "Seth Lloyd",
    ]


def test_blank_after_author_block_stops_abstract_words():
    raw = """# Deep-neural-network solution

Jan Hermann, Zeno Schätzle and Frank Noé

The electronic Schrödinger equation is solved with Quantum Monte Carlo.
"""
    assert extract_authors_from_text(raw) == ["Jan Hermann", "Zeno Schätzle", "Frank Noé"]


def test_affiliation_does_not_become_author():
    raw = """# Test paper

David Pfau,<sup>*</sup> James S. Spencer,<sup>*</sup> and Alexander G. D. G. Matthews DeepMind, 6 Pancras Square, London

W. M. C. Foulkes

Department of Physics, Imperial College London
"""
    assert extract_authors_from_text(raw) == [
        "David Pfau", "James S. Spencer", "Alexander G. D. G. Matthews", "W. M. C. Foulkes",
    ]


def test_internal_capital_surname_is_not_truncated():
    raw = """# Efficient perturbation theory

Emanuele Tirrito,<sup>1</sup> Ian P. McCulloch,<sup>2</sup> and Maciej Lewenstein<sup>1</sup>

Department of Physics
"""
    assert extract_authors_from_text(raw) == [
        "Emanuele Tirrito", "Ian P. McCulloch", "Maciej Lewenstein",
    ]


def test_multiword_affiliation_does_not_leave_prefix_as_author():
    raw = """# Benchmarking a Tunable Quantum Neural Network

Djamil Lakhdar-Hamina, Xingxin Liu, Richard Barney, Sarah H. Miller, Alaina M. Green, Norbert M. Linke and Victor Galitski

Joint Quantum Institute and Department of Physics, University of Maryland
"""
    assert extract_authors_from_text(raw) == [
        "Djamil Lakhdar-Hamina", "Xingxin Liu", "Richard Barney", "Sarah H. Miller",
        "Alaina M. Green", "Norbert M. Linke", "Victor Galitski",
    ]


def test_numbered_affiliation_does_not_become_author():
    raw = """# Not All Contexts Are Equal

Ruotong Pan<sup>1,2</sup>, Boxi Cao<sup>1,2</sup>, Hongyu Lin<sup>1</sup>

<sup>1</sup>Chinese Information Processing Laboratory, Institute of Software, Chinese Academy of Sciences

## Abstract
"""
    assert extract_authors_from_text(raw) == ["Ruotong Pan", "Boxi Cao", "Hongyu Lin"]


def test_email_author_rows_scan_full_block_without_affiliation_fragments():
    raw = """# SUFFICIENT CONTEXT: A NEW LENS

Hailey Joren<sup>∗</sup> UC San Diego hjoren@ucsd.edu

Jianyi Zhang<sup>†</sup> Duke University jianyi.zhang@duke.edu

Da-Cheng Juan Google dacheng@google.com

Chun-Sung Ferng Google csferng@google.com

Ankur Taly Google ataly@google.com

Cyrus Rashtchian Google cyroid@google.com

## ABSTRACT
"""
    assert extract_authors_from_text(raw) == [
        "Hailey Joren", "Jianyi Zhang", "Da-Cheng Juan", "Chun-Sung Ferng",
        "Ankur Taly", "Cyrus Rashtchian",
    ]


def test_conference_skeleton_raw_lookup():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw = repo / "academic/raw/conferences/2026/0728-1-demo/0728-1-demo.txt"
        corrected = raw.parent / "corrected.md"
        raw.parent.mkdir(parents=True)
        raw.write_text("会议原文\n", encoding="utf-8")
        corrected.write_text("历史修正版\n", encoding="utf-8")
        old_repo = wiki_skeleton.REPO
        wiki_skeleton.REPO = repo
        try:
            assert wiki_skeleton.find_conference_raw("academic", "0728-1-demo") == (
                "academic/raw/conferences/2026/0728-1-demo/0728-1-demo.txt"
            )
        finally:
            wiki_skeleton.REPO = old_repo


def test_conference_skeleton_contract():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw = repo / "academic/raw/conferences/2026/0728-1-demo/0728-1-demo.txt"
        raw.parent.mkdir(parents=True)
        raw.write_text("会议原文\n", encoding="utf-8")
        old_repo = wiki_skeleton.REPO
        wiki_skeleton.REPO = repo
        try:
            captured = StringIO()
            with redirect_stdout(captured):
                wiki_skeleton.gen_skeleton("academic/wiki/conferences/0728-1-demo", Namespace())
            output = captured.getvalue()
        finally:
            wiki_skeleton.REPO = old_repo
        assert "source_type: speech-recognition" in output
        assert "status: current" in output
        assert "## Navigation" in output and "## Content" in output
        assert "academic/raw/conferences/2026/0728-1-demo/0728-1-demo.txt" in output


def test_semantic_ingest_contract():
    semantic = """三元组:
本论文 | 主要研究 | 量子信息
本论文 | 涉及 | 机器学习
本论文 | 研究关键词 | 量子机器学习
本论文 | 核心方法 | 量子线性代数
"""
    triples, keywords, main, _, _, predicates = graph_ingest.parse_semantic_text(
        semantic, "academic/wiki/papers/test"
    )
    assert main == "量子信息"
    assert predicates == [("量子信息", "主要研究"), ("机器学习", "涉及")]
    assert keywords == ["量子机器学习", "量子线性代数"]
    assert {tuple(item.values()) for item in triples} >= {
        ("academic/wiki/papers/test", "主要研究", "量子信息"),
        ("academic/wiki/papers/test", "涉及", "机器学习"),
    }


def test_paper_metadata_edges_come_from_frontmatter_not_weak_llm_slots():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        page_rel = "academic/wiki/papers/test"
        page = root / f"{page_rel}.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: Test\ntype: paper-summary\n"
            "authors: [Alice Smith, Bob Li]\n"
            "venue: 'Phys. Rev. A 91, 032306 (2015)'\n---\n",
            encoding="utf-8",
        )
        old_repo = graph_ingest.gl.REPO
        graph_ingest.gl.REPO = root
        try:
            semantic = (
                "期刊:\n（未在 wiki 中明确给出期刊缩写）\n"
                "第一作者:\n错误作者\n通讯作者:\n（wiki 未提供）\n"
                "三元组:\n本论文 | 核心方法 | hybrid算法\n"
            )
            triples, *_ = graph_ingest.parse_semantic_text(semantic, page_rel)
        finally:
            graph_ingest.gl.REPO = old_repo
        metadata = {(t["subject"], t["predicate"], t["object"]) for t in triples
                    if t["predicate"] in {"第一作者", "作者", "通讯作者", "发表于"}}
        assert ("Alice Smith", "第一作者", page_rel) in metadata
        assert ("Bob Li", "作者", page_rel) in metadata
        assert (page_rel, "发表于", "Phys. Rev. A") in metadata
        assert not any("未提供" in str(item) or "错误作者" in str(item) for item in metadata)


def test_semantic_direction_normalization_matches_or_downgrades_to_keyword():
    page = "academic/wiki/papers/test"
    triples = [
        {"subject": page, "predicate": "主要研究", "object": "量子信息处理"},
        {"subject": page, "predicate": "涉及", "object": "未知研究方向"},
    ]
    keywords = ["已有关键词"]
    original_resolve = graph_ingest.resolve_semantic_direction_hub
    graph_ingest.resolve_semantic_direction_hub = lambda _conn, direction: (
        "academic/wiki/hubs/量子信息" if direction == "量子信息处理" else None
    )
    try:
        normalized, directions = graph_ingest.normalize_semantic_directions(
            None, triples, keywords, page, document_text="量子信息"
        )
    finally:
        graph_ingest.resolve_semantic_direction_hub = original_resolve
    assert normalized == [{
        "subject": page, "predicate": "主要研究", "object": "academic/wiki/hubs/量子信息",
    }]
    assert directions == [("academic/wiki/hubs/量子信息", "主要研究")]
    assert keywords == ["已有关键词", "未知研究方向"]


def test_conference_semantic_contract():
    semantic = """参会者:
cnu-wu-xi
cnu-ran-shiju
三元组:
本会议 | 讨论 | 模型压缩
本会议 | 汇报 | 知识蒸馏
本会议 | 规划 | CoT评测
cnu-ran-shiju | 指导 | cnu-wu-xi
"""
    triples, keywords, main, _, _, _ = graph_ingest.parse_semantic_text(
        semantic, "academic/wiki/conferences/test"
    )
    assert main is None
    assert keywords == ["模型压缩", "知识蒸馏", "CoT评测"]
    assert {item["predicate"] for item in triples} == {"讨论", "汇报", "规划", "参会", "指导"}


def test_duplicate_edge_keeps_one_optional_locator_and_page_origins():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        graph_ingest.gl.ensure_node(conn, "keyword", "Keyword", "entity")
        triples = [
            {"subject": "page", "predicate": "讨论", "object": "keyword", "source": "academic/raw/a.txt#topic"},
            {"subject": "page", "predicate": "讨论", "object": "keyword", "source": "academic/raw/b.txt#topic"},
        ]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        assert result[0] == 1
        assert result[2] == 1
        assert conn.execute("SELECT COUNT(*) FROM edge_evidence").fetchone()[0] == 0
        assert conn.execute("SELECT source FROM edges").fetchone()[0] == "academic/raw/a.txt#topic"
        assert conn.execute("SELECT COUNT(*) FROM edge_origins").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 1
        conn.close()


def test_page_sources_do_not_become_semantic_edge_evidence():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        page.write_text("---\ntitle: Test\ntype: paper-summary\nsources: [academic/raw/a.md, academic/raw/b.md]\nsource_type: official-doc\n---\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            triples = graph_ingest.fill_defaults([{"subject": "page", "predicate": "讨论", "object": "keyword"}], {"sources": ["academic/raw/a.md", "academic/raw/b.md"]})
            graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
            graph_ingest.gl.ensure_node(conn, "keyword", "Keyword", "entity")
            graph_ingest.add_knowledge_edges(conn, "page", triples)
            assert conn.execute("SELECT COUNT(*) FROM edge_evidence").fetchone()[0] == 0
            assert conn.execute("SELECT source FROM edges").fetchone()[0] == ""
            conn.close()
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_batch_page_sources_leave_edge_locator_optional():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\ntitle: Test\ntype: paper-summary\nsources: [academic/raw/a.md, academic/raw/b.md]\nsource_type: official-doc\n---\n", encoding="utf-8")
        old_repo = graph_ingest.gl.REPO
        graph_ingest.gl.REPO = repo
        try:
            triples = graph_ingest.fill_defaults([{"page": "academic/wiki/papers/test", "subject": "page", "predicate": "讨论", "object": "keyword"}], {})
            assert triples[0]["source"] == ""
            assert "evidence_sources" not in triples[0]
        finally:
            graph_ingest.gl.REPO = old_repo


def test_conference_prefill_uses_meeting_keyword_slot(capsys):
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/conferences/0728-demo.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\ntitle: 会议金样\ntype: conference-summary\n---\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO = repo
        graph_ingest.gl.GRAPH_DB = repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            graph_ingest.cmd_prefill(Namespace(page="academic/wiki/conferences/0728-demo"))
            output = capsys.readouterr().out
            assert "会议关键词" in output
            assert "核心方法:" not in output
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_graph_check_end_to_end_fixture():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/test.md"
        page.parent.mkdir(parents=True)
        page.write_text("---\ntitle: Test\n---\n", encoding="utf-8")
        db = repo / "cross-domain/graph.db"
        db.parent.mkdir()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE nodes (path TEXT PRIMARY KEY);
            CREATE TABLE edges (subject TEXT, predicate TEXT, object TEXT, source TEXT);
        """)
        conn.execute("INSERT INTO nodes VALUES (?)", ("academic/wiki/papers/test",))
        conn.execute("INSERT INTO edges VALUES (?,?,?,?)", ("academic/wiki/papers/test", "主要研究", "academic/wiki/hubs/量子信息", "academic/raw/test.md"))
        conn.commit()
        conn.close()
        old_repo = ingest_check.REPO
        ingest_check.REPO = repo
        try:
            assert ingest_check.graph_checks(page) == ([], [])
        finally:
            ingest_check.REPO = old_repo


def test_replayable_conference_ingest():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        raw = repo / "academic/raw/conferences/2026/0728-demo/0728-demo.txt"
        page = repo / "academic/wiki/conferences/0728-demo.md"
        semantic = repo / "conference-semantic.txt"
        catch_all = repo / "academic/wiki/hubs/未归类关键词.md"
        raw.parent.mkdir(parents=True)
        page.parent.mkdir(parents=True)
        catch_all.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        raw.write_text("# 0728 demo\n", encoding="utf-8")
        page.write_text("""---
title: 会议金样
type: conference-summary
sources:
  - academic/raw/conferences/2026/0728-demo/0728-demo.txt
source_type: speech-recognition
date: 2026-07-28
confidence: medium
created: 2026-07-28
updated: 2026-07-28
status: current
---
## Navigation
会议金样。
## Content
### 一、讨论
模型压缩。
""", encoding="utf-8")
        catch_all.write_text("# 未归类关键词\n\n## 关键词\n", encoding="utf-8")
        legacy_catch_all = catch_all.read_text(encoding="utf-8")
        semantic.write_text("参会者:\ncnu-wu-xi\n三元组:\n本会议 | 讨论 | 模型压缩\n本会议 | 规划 | CoT评测\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        old_check_repo = ingest_check.REPO
        graph_ingest.gl.REPO = repo
        graph_ingest.gl.GRAPH_DB = repo / "cross-domain/graph.db"
        ingest_check.REPO = repo
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            original_assign = graph_ingest.assign_keyword_hubs_meeting_admin
            graph_ingest.assign_keyword_hubs_meeting_admin = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("新摄入不应调用旧 Hub 关键词归属"))
            try:
                graph_ingest.cmd_ingest(Namespace(
                    page="academic/wiki/conferences/0728-demo", semantic=str(semantic),
                    triples=None, triples_json=None, citations=None,
                ))
            finally:
                graph_ingest.assign_keyword_hubs_meeting_admin = original_assign
            errors, warnings = ingest_check.graph_checks(page)
            assert not errors
            assert not warnings
            conn = sqlite3.connect(graph_ingest.gl.GRAPH_DB)
            edges = set(conn.execute("SELECT subject, predicate, object FROM edges"))
            conn.close()
            assert ("academic/wiki/conferences/0728-demo", "讨论", "模型压缩") in edges
            assert not any(predicate == "主要研究" for _, predicate, _ in edges)
            assert not any(object_.startswith("academic/wiki/hubs/") for _, _, object_ in edges)
            # 旧 Hub 关键词/catch-all 只读兼容；概念只保留语义边。
            assert catch_all.read_text(encoding="utf-8") == legacy_catch_all
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db
            ingest_check.REPO = old_check_repo


def test_paper_ingest_filters_unsupported_derived_direction():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/demo.md"
        semantic = repo / "semantic.txt"
        page.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        page.write_text(
            "---\ntitle: Demo tensor network paper\ntype: paper-summary\nsources: []\n"
            "source_type: official-doc\ndate: 2026\nconfidence: high\ncreated: 2026-07-31\n"
            "updated: 2026-07-31\nstatus: current\n---\n## Navigation\n\n"
            "This page does not name the derived direction or its seed.\n\n## Content\n",
            encoding="utf-8",
        )
        semantic.write_text("研究关键词:\nkeyword-one\nkeyword-two\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            import direction_matcher
            original_classify = direction_matcher.classify_keywords
            original_support = direction_matcher.direction_has_document_support
            direction_matcher.classify_keywords = lambda *_args, **_kwargs: ({}, {"测试方向": ["keyword-one", "keyword-two"]}, [])
            direction_matcher.direction_has_document_support = lambda *_args: False
            try:
                graph_ingest.cmd_ingest(Namespace(
                    page="academic/wiki/papers/demo", semantic=str(semantic),
                    triples=None, triples_json=None, citations=None,
                ))
            finally:
                direction_matcher.classify_keywords = original_classify
                direction_matcher.direction_has_document_support = original_support
            conn = sqlite3.connect(graph_ingest.gl.GRAPH_DB)
            edges = set(conn.execute("SELECT predicate, object FROM edges"))
            conn.close()
            assert ("主要研究", "测试方向") not in edges
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_descriptive_phrase_catches_sentence_patterns():
    """is_descriptive_phrase 双门槛(2026-08-03):<=15放过,16-20仅标点,>20+触发词才判。"""
    from graph_ingest import is_descriptive_phrase
    # 真描述性短语(长>20且含触发词):应判 True
    assert is_descriptive_phrase("首次将二维张量网络投影纠缠对态(PEPS)系统应用于图像分类")
    assert is_descriptive_phrase("证明准纠缠熵可用于精确确定Kosterlitz-Thouless转变温度")
    assert is_descriptive_phrase("提出处理任意紧致半单李代数非阿贝尔对称性的通用框架")
    assert is_descriptive_phrase("纯态与混合态结果整合至同一图表，剔除纯态聚焦混态分析")
    # 16-20字含触发词但非描述性短语:仅标点判定,触发词不判(降误伤)
    assert not is_descriptive_phrase("利用自动微分构建物理方程残差")  # 14字<=15直接放过
    assert not is_descriptive_phrase("将MPS推广到连续极限定义CMPS")  # 18字,触发词但<20不判
    # <=15字合法概念名:直接放过
    assert not is_descriptive_phrase("投影纠缠对态projected entangled pair state(PEPS)")
    assert not is_descriptive_phrase("密度矩阵重整化群density matrix renormalization group(DMRG)")
    assert not is_descriptive_phrase("矩阵乘积态matrix product state(MPS)")
    assert not is_descriptive_phrase("量子计算quantum computing")
    assert not is_descriptive_phrase("纠缠熵")
    # 新门槛:10-15字概念名含触发词不再误伤
    assert not is_descriptive_phrase("探索更深网络与高阶微分算子")  # 13字,含探索但不判
    assert not is_descriptive_phrase("扩展至二维张量网络")  # 9字<=15直接过
    assert not is_descriptive_phrase("应用于混合量子电路")  # 9字<=15直接过
def test_is_citation_fragment_detection():
    """is_citation_fragment 检测 MinerU 误建的引文残片实体名。"""
    from graph_ingest import is_citation_fragment
    assert is_citation_fragment("2019;1:538–550.-2019")
    assert is_citation_fragment("1992;69(19):2863–2866.-1992")
    assert not is_citation_fragment("矩阵乘积态matrix product state(MPS)")
    assert not is_citation_fragment("投影纠缠对态(PEPS)")
    assert not is_citation_fragment("PEPS")


def test_add_knowledge_edges_skips_citation_fragment():
    """引文残片三元组不建节点不连边，记 citation_fragment warning。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [
            {"subject": "page", "predicate": "引用", "object": "2019;1:538–550.-2019"},
            {"subject": "page", "predicate": "引用", "object": "正常概念"},
        ]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        added, _dedup, _dup, _rh, _ra, _rm, dwarns = result
        assert added == 1
        assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='entity'").fetchone()[0] == 1
        cites = [w for w in dwarns if w.get("issue") == "citation_fragment"]
        assert len(cites) == 1
        conn.close()


def test_is_fragment_token_detection():
    """is_fragment_token 检测 LLM/OCR 拆出的裸小写 ASCII 残片（如 Rényi→nyi）。"""
    from graph_ingest import is_fragment_token
    assert is_fragment_token("nyi")
    assert is_fragment_token("ose")
    assert not is_fragment_token("anyon")     # len 5, 合法术语
    assert not is_fragment_token("MPS")       # 大写缩写
    assert not is_fragment_token("量子纯度")   # 含中文
    assert not is_fragment_token("Rényi")     # 含大写+非ASCII
    assert not is_fragment_token("t_q")       # 含下划线
    assert not is_fragment_token("")          # 空


def test_add_knowledge_edges_skips_fragment_token():
    """碎片 token 三元组不建节点不连边，记 fragment_token warning。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [
            {"subject": "page", "predicate": "研究基础", "object": "nyi"},
            {"subject": "page", "predicate": "研究基础", "object": "量子纯度"},
        ]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        added, _dedup, _dup, _rh, _ra, _rm, dwarns = result
        assert added == 1
        frags = [w for w in dwarns if w.get("issue") == "fragment_token"]
        assert len(frags) == 1
        conn.close()


def test_add_knowledge_edges_warns_free_edge_bare_abbreviation():
    """非 keyword 谓词的 subject/object 裸缩写应记 bare_abbreviation warning。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [{"subject": "iPEPS", "predicate": "基于", "object": "PEPS"}]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        *_, dwarns = result
        bare = [w for w in dwarns if w.get("issue") == "bare_abbreviation"]
        assert len(bare) == 2
        conn.close()


def test_metadata_venue_abbreviation_does_not_warn():
    """确定性 venue 简称是书目元数据，不进入自由语义边裸缩写审计。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [{"subject": "page", "predicate": "发表于", "object": "ICLR 2025"}]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        *_, dwarns = result
        assert not [w for w in dwarns if w.get("issue") == "bare_abbreviation"]
        conn.close()


def test_deterministic_venue_metadata_sets_subtype_on_create_and_reuse():
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        page = "academic/wiki/papers/page"
        graph_ingest.gl.ensure_node(conn, page, "Page", "page")
        graph_ingest.gl.ensure_node(conn, "Phys. Rev. B", "Phys. Rev. B", "entity")
        triples = [
            {"subject": page, "predicate": "发表于", "object": "Phys. Rev. B"},
            {"subject": page, "predicate": "发表于", "object": "ICLR"},
        ]
        attach_plan = {"decisions": [
            {"mention": "Phys. Rev. B", "action": "reuse_deterministic_metadata",
             "target": "Phys. Rev. B", "metadata_kind": "venue"},
            {"mention": "ICLR", "action": "create_deterministic_metadata",
             "target": "ICLR", "metadata_kind": "venue"},
        ]}

        graph_ingest.add_knowledge_edges(conn, page, triples, attach_plan=attach_plan)

        rows = conn.execute(
            "SELECT path,entity_subtype FROM nodes WHERE path IN ('Phys. Rev. B','ICLR')"
        ).fetchall()
        assert {row["path"]: row["entity_subtype"] for row in rows} == {
            "Phys. Rev. B": "venue", "ICLR": "venue",
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM node_origins WHERE origin_page=?", (page,)
        ).fetchone()[0] == 2
        conn.close()


def test_first_author_reuse_upgrades_person_subtype():
    """第一作者复用旧裸 entity 时仍须补 person subtype，避免进入 Hub membership。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        page = "academic/wiki/papers/page"
        graph_ingest.gl.ensure_node(conn, page, "Page", "page")
        graph_ingest.gl.ensure_node(
            conn, "hailey-joren", "Hailey Joren", "entity", entity_subtype="keyword"
        )
        triples = [{"subject": "Hailey Joren", "predicate": "第一作者", "object": page}]
        attach_plan = {"decisions": [{
            "mention": "Hailey Joren", "action": "reuse_unique", "target": "hailey-joren",
        }]}
        graph_ingest.add_knowledge_edges(conn, page, triples, attach_plan=attach_plan)
        row = conn.execute(
            "SELECT entity_subtype FROM nodes WHERE path='hailey-joren'"
        ).fetchone()
        assert row["entity_subtype"] == "person"
        conn.close()


def test_fallback_single_hit_direction_not_promoted():
    """Fix: 方向仅单点 keyword 命中(< _MIN_KW_FOR_EDGE)时,兜底不得提升为「主要研究」。

    旧实现 `... or _dir_kw` 把单点噪声也兜底提升,写出错误方向边(误导导航)。
    """
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/demo.md"
        page.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        page.write_text(
            "---\ntitle: Demo paper\ntype: paper-summary\nsources: []\nsource_type: official-doc\n"
            "date: 2026\nconfidence: high\ncreated: 2026-08-23\nupdated: 2026-08-23\nstatus: current\n---\n"
            "## Navigation\n\nNo direction named here.\n\n## Content\n", encoding="utf-8")
        semantic = repo / "semantic.txt"
        # 三元组段 + 研究关键词谓词 → 填充 keywords(触发方向派生 fallback)
        semantic.write_text(
            "三元组:\n本论文 | 研究关键词 | 基于模式schema-based方法\n"
            "本论文 | 研究关键词 | 知识抽取\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            import direction_matcher
            orig_cls = direction_matcher.classify_keywords
            orig_sup = direction_matcher.direction_has_document_support
            # 单点命中: 测试方向只匹配 1 个 keyword(< _MIN_KW_FOR_EDGE=2)
            direction_matcher.classify_keywords = lambda *a, **k: (
                {}, {"测试方向": ["基于模式schema-based方法"]}, ["知识抽取"])
            direction_matcher.direction_has_document_support = lambda *a, **k: False
            try:
                graph_ingest.cmd_ingest(Namespace(
                    page="academic/wiki/papers/demo", semantic=str(semantic),
                    triples=None, triples_json=None, citations=None))
            finally:
                direction_matcher.classify_keywords = orig_cls
                direction_matcher.direction_has_document_support = orig_sup
            conn = sqlite3.connect(graph_ingest.gl.GRAPH_DB)
            edges = set(conn.execute("SELECT predicate, object FROM edges"))
            conn.close()
            assert not any(p == "主要研究" for p, _ in edges), "单点命中方向不得提升为主要研究"
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_assign_keyword_hubs_resolves_existing_hub_by_title():
    """embedding 返回语义名时，须按 title 命中既有子方向而非重建根 hub。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        (repo / "academic/wiki/hubs").mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        main_path = "academic/wiki/hubs/测试主方向"
        sub_path = "academic/wiki/hubs/子方向-a"
        (repo / f"{main_path}.md").write_text(
            "---\ntitle: \"测试主方向\"\ntype: topic-hub\n---\n\n## 关键词\n",
            encoding="utf-8")
        (repo / f"{sub_path}.md").write_text(
            "---\ntitle: \"测试子方向\"\ntype: topic-hub\n---\n\n## 关键词\n",
            encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            graph_ingest.gl.ensure_node(conn, main_path, "测试主方向", "hub")
            graph_ingest.gl.ensure_node(conn, sub_path, "测试子方向", "hub")
            graph_ingest.gl.add_child_edge(conn, main_path, sub_path)
            conn.commit()
            import direction_matcher
            original_classify = direction_matcher.classify_keywords
            direction_matcher.classify_keywords = lambda *_args, **_kwargs: (
                {"新关键词": [("测试子方向", 0.9)]}, {}, [])
            try:
                result = graph_ingest.assign_keyword_hubs(
                    conn, ["新关键词"], [(main_path, "主要研究")], "academic/wiki/papers/demo")
            finally:
                direction_matcher.classify_keywords = original_classify
            assert result.synced == 1
            assert "- 新关键词" in (repo / f"{sub_path}.md").read_text(encoding="utf-8")
            assert not (repo / "academic/wiki/hubs/测试子方向.md").exists()
            title_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE type='hub' AND title='测试子方向'"
            ).fetchone()[0]
            assert title_count == 1
            conn.close()
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db


def test_free_edge_bare_abbreviation_report_accurate():
    """Fix: 自由边 bare_abbreviation 报告须准确——object 存真实客体(非 subj_raw,旧 bug 致伪自环),
    value 存被标记文本;结构性谓词(包含/相似)跳过,避免同一 proposition 多条包含边重复告警。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [
            {"subject": "iPEPS", "predicate": "基于", "object": "PEPS"},
            {"subject": "MPS变体", "predicate": "包含", "object": "MPS"},
        ]
        result = graph_ingest.add_knowledge_edges(conn, "page", triples)
        *_, dwarns = result
        bare = [w for w in dwarns if w.get("issue") == "bare_abbreviation"]
        # 基于(非结构性): subject iPEPS + object PEPS 共 2 条; 包含(结构性): 跳过 0 条
        assert len(bare) == 2, "基于边应有 2 条 bare 告警,包含边应跳过"
        subj_warns = [w for w in bare if w.get("field") == "subject"]
        assert subj_warns, "应有 field=subject 告警"
        sw = subj_warns[0]
        assert sw["object"] == "PEPS", "object 须存真实客体,不得是 subj_raw(旧 bug 致伪自环)"
        assert sw["value"] == "iPEPS", "value 存被标记文本"
        assert not any(w.get("predicate") == "包含" for w in bare), "结构性谓词(包含)应跳过裸缩写校验"
        conn.close()


def test_add_knowledge_edges_creates_proposition_and_inclusion_edges():
    """proposition 改革：命题谓词 object 建 proposition 节点；包含边机械派生且跳审计。

    - 命题全名 → proposition 节点（path=extract_descriptive_id 替换内嵌概念，subtype='proposition'）
    - 概念全名 → keyword 节点（path=extract_keyword_id 短形式，subtype='keyword'）
    - 包含边（predicate='包含'）：proposition → 主宾概念，不触发 descriptive_phrase warning
    - 语义边（LLM 抽取的原子三元组）：主宾概念间，谓词未登记仍建边
    - 概念不误 resolve 到 proposition（abbr 相同的归一化误匹配被 discard）
    """
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [
            {"subject": "page", "predicate": "核心创新点",
             "object": "证明ANTN超越矩阵乘积态matrix product state(MPS)"},
            {"subject": "ANTN", "predicate": "超越",
             "object": "矩阵乘积态matrix product state(MPS)"},
            {"subject": "证明ANTN超越矩阵乘积态matrix product state(MPS)", "predicate": "包含",
             "object": "ANTN"},
            {"subject": "证明ANTN超越矩阵乘积态matrix product state(MPS)", "predicate": "包含",
             "object": "矩阵乘积态matrix product state(MPS)"},
        ]
        added, _sd, _dup, _rh, _ra, _rm, dwarns = graph_ingest.add_knowledge_edges(conn, "page", triples)
        # proposition 节点
        prop = conn.execute("SELECT path, title FROM nodes WHERE entity_subtype='proposition'").fetchall()
        assert len(prop) == 1
        assert prop[0]["path"] == "证明ANTN超越MPS"
        assert prop[0]["title"] == "证明ANTN超越矩阵乘积态matrix product state(MPS)"
        # keyword 节点（MPS 短形式，未误 resolve 到 proposition）
        kw_paths = {r["path"] for r in conn.execute("SELECT path FROM nodes WHERE entity_subtype='keyword'")}
        assert "ANTN" in kw_paths and "MPS" in kw_paths, f"keyword paths={kw_paths}"
        # 包含边
        incl = conn.execute("SELECT object FROM edges WHERE predicate='包含' AND subject='证明ANTN超越MPS'").fetchall()
        assert {r["object"] for r in incl} == {"ANTN", "MPS"}, f"包含边 objects={[r['object'] for r in incl]}"
        # 语义边
        assert conn.execute("SELECT COUNT(*) FROM edges WHERE predicate='超越'").fetchone()[0] == 1
        # 包含边不触发 descriptive_phrase warning
        # 包含边（predicate=包含）的 object 不触发 descriptive_phrase；命题全名作为核心创新点 object 的 warning 仍产生（审计信息，非阻断）
        incl_desc = [w for w in dwarns if w.get("issue") == "descriptive_phrase" and w.get("predicate") == "包含"]
        assert incl_desc == [], f"包含边不应触发 descriptive_phrase: {incl_desc}"
        conn.close()


def test_sparse_proposition_links_exact_concepts_without_atom_nodes():
    """完整命题仅链接确认/唯一精确概念；不从句内碎片建节点，重入幂等。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        graph_ingest.gl.ensure_node(
            conn, "MPS", "矩阵乘积态matrix product state(MPS)", "entity",
            entity_subtype="keyword",
        )
        graph_ingest.gl.insert_aliases(conn, "MPS", ["MPS", "矩阵乘积态"])
        # 同一 alias 指向两个既有概念时必须静默 abstain。
        graph_ingest.gl.ensure_node(conn, "SC-1", "充分上下文一", "entity", entity_subtype="keyword")
        graph_ingest.gl.ensure_node(conn, "SC-2", "充分上下文二", "entity", entity_subtype="keyword")
        graph_ingest.gl.insert_aliases(conn, "SC-1", ["SC"])
        graph_ingest.gl.insert_aliases(conn, "SC-2", ["SC"])
        triples = [
            {"subject": "page", "predicate": "核心方法",
             "object": "注意力神经张量网络attention neural tensor network(ANTN)"},
            {"subject": "page", "predicate": "核心创新点",
             "object": "ANTN超越MPS并输出分数，进一步研究SC"},
        ]
        graph_ingest.add_knowledge_edges(conn, "page", triples)
        prop = conn.execute(
            "SELECT path FROM nodes WHERE entity_subtype='proposition'"
        ).fetchone()["path"]
        included = {
            row["object"] for row in conn.execute(
                "SELECT object FROM edges WHERE subject=? AND predicate='包含'", (prop,)
            )
        }
        assert "ANTN" in included and "MPS" in included, included
        assert "SC-1" not in included and "SC-2" not in included, "歧义 alias 不应链接"
        for fragment in ("输出分数", "进一步研究", "研究", "SC"):
            assert conn.execute("SELECT 1 FROM nodes WHERE path=? OR title=?", (fragment, fragment)).fetchone() is None
        before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        graph_ingest.add_knowledge_edges(conn, "page", triples)
        after = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        assert after == before, f"re-ingest 节点数应幂等: {before} -> {after}"
        conn.close()


def test_cleanup_ghost_hubs_removes_orphan():
    """.md 不存在的 type='hub' 节点应被清理（节点 + 关联边）。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
            graph_ingest.gl.ensure_node(conn, "academic/wiki/hubs/real", "Real", "hub")
            (repo / "academic/wiki/hubs").mkdir(parents=True)
            (repo / "academic/wiki/hubs/real.md").write_text("hub_subtype: research-direction", encoding="utf-8")
            graph_ingest.gl.ensure_node(conn, "academic/wiki/hubs/ghost", "Ghost", "hub")
            conn.execute("INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                         "VALUES (?,?,?,?,?,?)", ("academic/wiki/hubs/ghost", "子方向", "page", "[可追溯]", "", 0))
            conn.commit()
            cleaned = graph_ingest.cleanup_ghost_hubs(conn)
            assert "academic/wiki/hubs/ghost" in cleaned
            assert "academic/wiki/hubs/real" not in cleaned
            assert conn.execute("SELECT COUNT(*) FROM nodes WHERE type='hub'").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM edges WHERE subject='academic/wiki/hubs/ghost'").fetchone()[0] == 0
            conn.close()
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db



def test_inbox_state_records_telemetry_events():
    """P0：inbox_state.save 记录 source_hash 与状态/重试时间线。"""
    import inbox_state
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        source = repo / "source.txt"
        source.write_text("hello", encoding="utf-8")
        original_repo = inbox_state.REPO
        inbox_state.REPO = repo
        try:
            state = {"source": "source.txt", "status": "init",
                     "retry_count": 0, "errors": []}
            inbox_state.save("txn", state)
            state["status"] = "write_wiki"
            state["errors"] = ["e"]
            inbox_state.save("txn", state)
            state = inbox_state.load("txn")
        finally:
            inbox_state.REPO = original_repo
    assert state["telemetry"]["source_hash"] == hashlib.sha256(b"hello").hexdigest()
    events = state["telemetry"]["events"]
    assert len(events) == 2
    assert events[0]["to"] == "init" and events[1]["to"] == "write_wiki"
    assert events[1]["errors_count"] == 1
    assert events[1]["recovery_attempts"] == 0
    assert state["telemetry"]["execution_events"] == {
        "event_version": "execution-event-v1",
        "directory": "temp/llm-events",
        "transaction_id": "txn",
        "canonical_for_api_calls": True,
    }
    assert events[1]["failure"]["category"] == "unknown_failure"
    assert events[1]["failure"]["retryable"] is False
    assert len(events[1]["failure"]["fingerprints"]) == 1
    assert inbox_state.classify_failure({
        "status": "failed", "errors": ["HTTP Error 429: Too Many Requests"],
    })["category"] == "api_rate_limit"
    assert inbox_state.classify_failure({
        "status": "agent_required", "errors": ["谓词格式不合法"],
    })["owner"] == "specialist_agent"


def test_inbox_state_atomic_save_and_guarded_resume():
    import inbox_state
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        original_repo = inbox_state.REPO
        original_replace = inbox_state.os.replace
        inbox_state.REPO = repo
        try:
            state = {"status": "failed", "errors": ["graph_ingest failed"]}
            path = inbox_state.save("txn-atomic", state)
            persisted = path.read_text(encoding="utf-8")
            state["errors"] = ["new failure"]
            inbox_state.os.replace = lambda *_args: (_ for _ in ()).throw(OSError("interrupt"))
            try:
                inbox_state.save("txn-atomic", state)
                raise AssertionError("atomic replace failure must propagate")
            except OSError:
                pass
            assert path.read_text(encoding="utf-8") == persisted
            inbox_state.os.replace = original_replace

            try:
                inbox_state.transition(state, "completed", reason="unsafe skip")
                raise AssertionError("failed transaction must not skip to completed")
            except ValueError:
                pass
            inbox_state.transition(state, "graph_ready", reason="graph repaired")
            inbox_state.save("txn-atomic", state)
            loaded = inbox_state.load("txn-atomic")
            event = loaded["telemetry"]["events"][-1]
            assert event["transition"]["from"] == "failed"
            assert event["transition"]["to"] == "graph_ready"
            assert event["transition"]["reason"] == "graph repaired"

            try:
                inbox_state.save("txn-invalid", {"status": "invented"})
                raise AssertionError("unknown status must fail")
            except ValueError:
                pass
        finally:
            inbox_state.os.replace = original_replace
            inbox_state.REPO = original_repo


def test_inbox_state_runtime_summary_uses_canonical_events():
    import inbox_state
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        states = root / "states"
        events = root / "events"
        states.mkdir()
        events.mkdir()
        (states / "txn.json").write_text(json.dumps({
            "transaction_id": "txn", "status": "agent_required",
            "errors": ["semantic review required"],
            "quality_warnings": [{"issue": "demo"}],
            "recovery": {"attempts": {"semantic_revision": 1}},
        }), encoding="utf-8")
        api_event = {
            "event_version": "execution-event-v1", "event_kind": "llm_api_call",
            "transaction_id": "txn", "operation": "ingest_wiki_write", "status": "ok",
            "latency_sec": 1.25, "usage": {"total_tokens": 120},
        }
        (events / "events.jsonl").write_text(
            json.dumps(api_event) + "\nnot-json\n", encoding="utf-8")
        report = inbox_state.summarize_runtime(states, events)
    assert report["transactions"] == 1
    assert report["by_status"] == {"agent_required": 1}
    assert report["degraded"] == 1
    assert report["recovery_attempts"] == {"semantic_revision": 1}
    assert report["api"]["calls"] == 1
    assert report["api"]["total_tokens"] == 120
    assert report["invalid_event_lines"] == 1

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        empty_states = root / "states"
        unrelated_events = root / "events"
        empty_states.mkdir()
        unrelated_events.mkdir()
        (unrelated_events / "events.jsonl").write_text(
            json.dumps(api_event) + "\n", encoding="utf-8")
        empty_report = inbox_state.summarize_runtime(empty_states, unrelated_events)
    assert empty_report["transactions"] == 0
    assert empty_report["api"]["calls"] == 0


def test_step_update_graph_fails_on_non_json_output():
    """P0：graph_ingest 输出非 JSON 报告时不得静默成功。"""
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        original_run = ic.run_tracked
        try:
            ic.run_tracked = lambda cmd, repo_path, state=None, label=None: "not json"
            state = {"wiki_path": "academic/wiki/papers/test",
                    "semantic_path": "temp/test.sem"}
            ok, msg = ic.step_update_graph(state, repo, clean=False)
        finally:
            ic.run_tracked = original_run
    assert not ok and "JSON" in msg
    assert state["graph_report"] is None
    assert state["telemetry"]["graph_report_parse"]["status"] == "failed"


def test_validate_completion_blocks_stale_errors_and_empty_graph():
    """P0：历史错误未清空、语义槽缺失、graph_report 空跑均阻断 completed。"""
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        sem = repo / "temp/test.sem"
        sem.parent.mkdir(parents=True)
        sem.write_text("三元组:\n本论文 | 研究关键词 | 测试\n", encoding="utf-8")
        stale = {"errors": ["历史失败"], "semantic_path": "temp/test.sem",
                 "graph_report": {"edges_added": 1, "dup_skipped": 0, "nodes_created": 1}}
        assert ic.validate_completion(stale, repo)
        missing_sem = {"errors": [], "semantic_path": None,
                       "graph_report": {"edges_added": 1, "dup_skipped": 0, "nodes_created": 1}}
        assert ic.validate_completion(missing_sem, repo)
        empty_graph = {"errors": [], "semantic_path": "temp/test.sem",
                        "graph_report": {"edges_added": 0, "dup_skipped": 0, "nodes_created": 0}}
        assert ic.validate_completion(empty_graph, repo)
        clean = {"errors": [], "semantic_path": "temp/test.sem",
                 "graph_report": {"edges_added": 1, "dup_skipped": 0, "nodes_created": 1}}
        assert ic.validate_completion(clean, repo) == []


def test_resolve_bare_name_normalized_match():
    """归一化匹配:同一概念不同写法应解析到同一节点（解决碎片化）。"""
    canonical = "矩阵乘积态matrix product state(MPS)"
    title_idx = {canonical: [canonical]}
    alias_idx = {"MPS": [canonical]}
    suffix_idx = {canonical: [canonical], "正定MPS": ["正定MPS"]}
    # 裸缩写精确匹配
    r, a = graph_ingest.gl.resolve_bare_name("MPS", title_idx, alias_idx, suffix_idx)
    assert r == canonical and not a
    # 中英括号变体归一化匹配（全角括号）
    r, a = graph_ingest.gl.resolve_bare_name("矩阵乘积态（MPS）", title_idx, alias_idx, suffix_idx)
    assert r == canonical and not a
    # 派生概念不误匹配
    r, a = graph_ingest.gl.resolve_bare_name("正定MPS", title_idx, alias_idx, suffix_idx)
    assert r == "正定MPS" and not a
    # 不存在的名仍返回 None
    r, a = graph_ingest.gl.resolve_bare_name("完全不存在的概念xyz", title_idx, alias_idx, suffix_idx)
    assert r is None and not a


def test_ensure_keyword_connectivity():
    """命题内部概念只报告导航候选，不提升为论文级研究关键词。"""
    page = "academic/wiki/papers/test"
    triples = [
        {"subject": page, "predicate": "核心方法", "object": "MPS参数化"},
        {"subject": "MPS参数化", "predicate": "基于", "object": "矩阵乘积态matrix product state(MPS)"},
        {"subject": "矩阵乘积态matrix product state(MPS)", "predicate": "参数化", "object": "监督学习模型权重"},
        {"subject": "张三", "predicate": "作者", "object": page},
    ]
    original = [dict(item) for item in triples]
    candidates = graph_ingest.navigation_connectivity_candidates(triples, page, set())
    added = graph_ingest.ensure_keyword_connectivity(triples, page, set())
    assert added == 0
    assert triples == original
    assert candidates == ["矩阵乘积态matrix product state(MPS)", "监督学习模型权重"]


def test_locator_aware_page_adds_optional_wiki_section_locator_only():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = root / "academic/raw/references/demo/paper.md"
        page = root / "academic/wiki/papers/demo.md"
        raw.parent.mkdir(parents=True)
        page.parent.mkdir(parents=True)
        raw.write_text("# Demo\n\nThe hybrid algorithm reduces error.\n", encoding="utf-8")
        page.write_text(
            "# Demo\n\n## Method\n\nThe hybrid algorithm reduces error.[^r1]\n\n"
            "## Sources\n\n[^r1]: academic/raw/references/demo/paper.md#L3\n",
            encoding="utf-8",
        )
        old_repos = (graph_ingest.gl.REPO, graph_ingest.wl.REPO,
                     graph_ingest.wl.raw_locator.REPO)
        graph_ingest.gl.REPO = root
        graph_ingest.wl.REPO = root
        graph_ingest.wl.raw_locator.REPO = root
        try:
            triples = [{
                "subject": "academic/wiki/papers/demo",
                "predicate": "核心方法",
                "object": "hybrid algorithm",
                "source": "academic/raw/references/demo/paper.md#L3",
            }]
            report = graph_ingest.attach_wiki_section_sources(
                triples, "academic/wiki/papers/demo")
        finally:
            (graph_ingest.gl.REPO, graph_ingest.wl.REPO,
             graph_ingest.wl.raw_locator.REPO) = old_repos
        assert report["located_edges"] == 1
        assert triples[0]["source"] == "academic/wiki/papers/demo#method"
        assert "evidence_sources" not in triples[0]


def test_raw_original_and_locator_companion_share_node_and_wiki_source_edge():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        raw_dir = root / "academic/raw/references/demo"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.pdf").write_bytes(b"pdf")
        (raw_dir / "paper.md").write_text("# Demo\n", encoding="utf-8")
        page = root / "academic/wiki/papers/demo.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: Demo\ntype: paper-summary\n"
            "sources: [academic/raw/references/demo/paper.md]\n---\n",
            encoding="utf-8",
        )
        old_repo = graph_ingest.gl.REPO
        graph_ingest.gl.REPO = root
        try:
            conn = graph_ingest.gl.connect(root / "graph.db")
            graph_ingest.gl.init_schema(conn)
            graph_ingest.gl.ensure_node(conn, "academic/wiki/papers/demo", "Demo", "page")
            nodes = graph_ingest.ensure_raw_support_edge(conn, "academic/wiki/papers/demo")
            edge = conn.execute(
                "SELECT subject,predicate,object,source FROM edges"
            ).fetchone()
            aliases = {row[0] for row in conn.execute(
                "SELECT alias FROM aliases WHERE node_path=?", (nodes[0],))}
            conn.close()
        finally:
            graph_ingest.gl.REPO = old_repo
        assert nodes == ["academic/raw/references/demo/paper"]
        assert tuple(edge[:3]) == (
            "academic/wiki/papers/demo", "来源", "academic/raw/references/demo/paper")
        assert edge["source"] == ""
        assert aliases == {
            "academic/raw/references/demo/paper.md",
            "academic/raw/references/demo/paper.pdf",
        }


def test_agent_slots_resume_does_not_rewrite_wiki():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_to = root / "temp" / "agent-slots.txt"
        write_to.parent.mkdir(parents=True)
        write_to.write_text("三元组:\n本文件 | 涉及 | 测试主题\n", encoding="utf-8")
        state = {
            "transaction_id": "agent-slots-resume",
            "status": "agent_required",
            "pre_handoff_status": "write_slots",
            "_awaiting_agent_slots": True,
            "agent_required": True,
            "agent_write_to": str(write_to.relative_to(root)),
            "wiki_content": "existing wiki",
            "errors": [],
        }
        calls = []

        def write_wiki(_state):
            calls.append("write_wiki")
            return False, "must not run"

        def write_slots(current):
            calls.append("write_slots")
            current.pop("_awaiting_agent_slots", None)
            current["slots_content"] = write_to.read_text(encoding="utf-8")
            return True, ""

        steps = {
            "dedup_check": lambda _state: (False, ""),
            "preprocess": lambda _state: (True, ""),
            "write_wiki": write_wiki,
            "validate_wiki": lambda _state: [],
            "write_slots": write_slots,
            "validate_semantics": lambda _state: ([], []),
            "repair_slots": lambda _state, _warnings: (True, ""),
            "finalize": lambda _state: (True, ""),
            "update_graph": lambda current: (current.setdefault("graph_report", {"edges_added": 1}) is not None, ""),
            "validate_graph": lambda _state: [],
            "finalize_tail": lambda _state: (True, ""),
        }
        spec = {"script_name": "test_driver.py", "steps": steps, "normalize_slots": lambda text: text,
                "completion_label_key": None, "max_retries": 3}
        original_repo = ingest_pipeline.REPO
        original_save = ingest_pipeline._save
        original_fill = ingest_pipeline.ic.step_fill_semantics
        original_completion = ingest_pipeline.ic.validate_completion
        try:
            ingest_pipeline.REPO = root
            ingest_pipeline._save = lambda _state: None

            def fill_semantics(current, repo, normalize):
                current["semantic_path"] = "temp/final-semantic.txt"
                return True, ""

            ingest_pipeline.ic.step_fill_semantics = fill_semantics
            ingest_pipeline.ic.validate_completion = lambda _state, _repo: []
            result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
        finally:
            ingest_pipeline.REPO = original_repo
            ingest_pipeline._save = original_save
            ingest_pipeline.ic.step_fill_semantics = original_fill
            ingest_pipeline.ic.validate_completion = original_completion
        assert result["status"] == "completed"
        assert calls == ["write_slots"]


def test_update_graph_failure_rolls_back_and_exposes_resume_point():
    state = {
        "transaction_id": "update-graph-failure",
        "status": "update_graph",
        "errors": [],
    }
    rollback_calls = []
    steps = {
        "validate_semantics": lambda _state: ([], []),
        "update_graph": lambda _state: (False, "graph write failed"),
    }
    spec = {
        "script_name": "test_driver.py",
        "steps": steps,
        "rollback_fn": lambda current: rollback_calls.append(current["transaction_id"]) or
        ["wiki", "raw companion", "graph snapshot"],
    }
    original_save = ingest_pipeline._save
    original_validate = ingest_pipeline.ic.validate_before_commit
    try:
        ingest_pipeline._save = lambda _state: None
        ingest_pipeline.ic.validate_before_commit = lambda *_args, **_kwargs: []
        result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        ingest_pipeline._save = original_save
        ingest_pipeline.ic.validate_before_commit = original_validate
    assert rollback_calls == ["update-graph-failure"]
    assert result["status"] == "failed"
    assert result["resume_from"] == "finalize"
    assert "raw companion" in result["errors"][1]


def test_graph_validation_failure_exposes_clean_graph_retry_point():
    state = {
        "transaction_id": "graph-validation-failure",
        "status": "validate_graph",
        "errors": [],
    }
    spec = {
        "script_name": "test_driver.py",
        "steps": {
            "validate_semantics": lambda _state: ([], []),
            "validate_graph": lambda _state: ["venue mismatch"],
        },
        "retry_graph_with_clean": True,
    }
    original_save = ingest_pipeline._save
    try:
        ingest_pipeline._save = lambda _state: None
        result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        ingest_pipeline._save = original_save
    assert result["status"] == "failed"
    assert result["resume_from"] == "graph_ready"
    assert result["reingest"] is True


def test_wiki_validation_retry_budget_hands_off_without_third_full_rewrite():
    state = {
        "transaction_id": "wiki-retry-budget",
        "status": "write_wiki",
        "extract_dir": "temp/inbox-extract/wiki-retry-budget",
        "errors": [],
    }
    calls = []

    def write_wiki(current):
        calls.append("write")
        current["wiki_content"] = "invalid wiki"
        return True, ""

    spec = {
        "script_name": "test_driver.py",
        "steps": {
            "write_wiki": write_wiki,
            "validate_wiki": lambda _state: ["缺少 ## Content 段"],
        },
        "max_retries": 3,
        "max_wiki_validation_retries": 1,
    }
    original_save = ingest_pipeline._save
    try:
        ingest_pipeline._save = lambda _state: None
        result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        ingest_pipeline._save = original_save
    assert calls == ["write", "write"], "初次 + 1 次定向重写后必须停止"
    assert result["status"] == "agent_required"
    assert result["recovery"]["attempts"]["wiki_revision"] == 1
    assert result["write_to"] if "write_to" in result else result["agent_write_to"].endswith("wiki.md")
    assert "缺少 ## Content" in result["agent_prompt"]


def test_semantic_hard_error_gets_one_bounded_rewrite_then_handoff():
    state = {
        "transaction_id": "semantic-hard-retry-budget",
        "status": "write_slots",
        "extract_dir": "temp/inbox-extract/semantic-hard-retry-budget",
        "wiki_content": "valid wiki",
        "errors": [],
    }
    calls = []

    def write_slots(current):
        calls.append(list(current.get("slots_errors", [])))
        current["slots_content"] = "invalid slots"
        return True, ""

    spec = {
        "script_name": "test_driver.py",
        "steps": {
            "write_slots": write_slots,
            "validate_semantics": lambda _state: (["三元组格式错误"], []),
        },
        "normalize_slots": lambda text: text,
        "max_retries": 3,
        "max_semantic_hard_retries": 1,
    }
    original_save = ingest_pipeline._save
    original_fill = ingest_pipeline.ic.step_fill_semantics
    try:
        ingest_pipeline._save = lambda _state: None
        ingest_pipeline.ic.step_fill_semantics = lambda *_args, **_kwargs: (True, "")
        result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        ingest_pipeline._save = original_save
        ingest_pipeline.ic.step_fill_semantics = original_fill

    assert len(calls) == 2, "初次生成后只允许一次定向重写"
    assert calls[1] == ["三元组格式错误"]
    assert result["semantic_hard_retry"] == 1
    assert result["recovery"]["attempts"]["semantic_revision"] == 1
    assert result["status"] == "agent_required"


def test_llm_transport_failure_is_not_retried_by_pipeline():
    state = {
        "transaction_id": "transport-owned-by-client",
        "status": "write_wiki",
        "errors": [],
    }
    calls = []
    spec = {
        "script_name": "test_driver.py",
        "steps": {
            "write_wiki": lambda _state: calls.append("write") or
            (False, "LLM 调用失败: HTTP 429 exhausted"),
        },
        "recovery_limits": {
            "infrastructure": 1,
            "output_transport": 1,
            "wiki_revision": 1,
            "semantic_revision": 1,
            "deterministic_repair": 1,
            "subagent": 1,
        },
    }
    original_save = ingest_pipeline._save
    try:
        ingest_pipeline._save = lambda _state: None
        result = ingest_pipeline.run_pipeline(state, spec, lambda *args, **kwargs: None)
    finally:
        ingest_pipeline._save = original_save
    assert calls == ["write"]
    assert result["status"] == "failed"
    assert result["recovery"]["attempts"] == {}
    assert "llm_calls" not in result.get("telemetry", {})


def test_resume_post_maintenance_uses_unified_inbox_tail():
    import sys
    from types import ModuleType
    import ingest_common as ic

    calls = []
    fake = ModuleType("ingest_inbox")
    fake.run_post_ingest_maintenance = lambda results, session_id: calls.append(
        (results, session_id)
    ) or {"status": "agent_required", "receipt_path": "temp/receipt.json"}
    fake.compact_maintenance = lambda envelope: {
        "status": envelope["status"], "receipt_path": envelope["receipt_path"]
    }
    previous = sys.modules.get("ingest_inbox")
    sys.modules["ingest_inbox"] = fake
    try:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            inbox = repo / "inbox"
            inbox.mkdir()
            pending = inbox / "next-paper.pdf"
            pending.write_bytes(b"pending")
            state = {
                "status": "completed", "transaction_id": "txn-1",
                "source_filename": "paper.pdf", "repo": str(repo),
            }
            deferred = ic.run_resume_post_maintenance(state)
            assert deferred["status"] == "deferred"
            assert deferred["reason"] == "pending_inbox_files"
            assert deferred["pending_count"] == 1
            assert calls == []

            pending.unlink()
            maintenance = ic.run_resume_post_maintenance(state)
            assert maintenance == {
                "status": "agent_required", "receipt_path": "temp/receipt.json"
            }
            assert calls == [([{"file": "paper.pdf", "ok": True, "status": "completed"}],
                              "resume-txn-1")]
            assert ic.run_resume_post_maintenance({"status": "failed"}) is None
            fake.run_post_ingest_maintenance = lambda *_args: (_ for _ in ()).throw(
                RuntimeError("maintenance unavailable")
            )
            failed_maintenance = ic.run_resume_post_maintenance(state)
            assert failed_maintenance["status"] == "error"
            assert "maintenance unavailable" in failed_maintenance["errors"][0]
    finally:
        if previous is None:
            sys.modules.pop("ingest_inbox", None)
        else:
            sys.modules["ingest_inbox"] = previous


def main():
    test_nature_author_block()
    test_blank_after_author_block_stops_abstract_words()
    test_affiliation_does_not_become_author()
    test_multiword_affiliation_does_not_leave_prefix_as_author()
    test_numbered_affiliation_does_not_become_author()
    test_email_author_rows_scan_full_block_without_affiliation_fragments()
    test_conference_skeleton_raw_lookup()
    test_conference_skeleton_contract()
    test_semantic_ingest_contract()
    test_paper_metadata_edges_come_from_frontmatter_not_weak_llm_slots()
    test_semantic_direction_normalization_matches_or_downgrades_to_keyword()
    test_conference_semantic_contract()
    test_descriptive_phrase_catches_sentence_patterns()
    test_duplicate_edge_keeps_one_optional_locator_and_page_origins()
    test_page_sources_do_not_become_semantic_edge_evidence()
    test_batch_page_sources_leave_edge_locator_optional()
    test_graph_check_end_to_end_fixture()
    test_replayable_conference_ingest()
    test_paper_ingest_filters_unsupported_derived_direction()
    test_paper_prefill_does_not_raise_name_error()
    test_is_citation_fragment_detection()
    test_add_knowledge_edges_skips_citation_fragment()
    test_add_knowledge_edges_warns_free_edge_bare_abbreviation()
    test_metadata_venue_abbreviation_does_not_warn()
    test_deterministic_venue_metadata_sets_subtype_on_create_and_reuse()
    test_first_author_reuse_upgrades_person_subtype()
    test_fallback_single_hit_direction_not_promoted()
    test_free_edge_bare_abbreviation_report_accurate()
    test_cleanup_ghost_hubs_removes_orphan()
    test_inbox_state_records_telemetry_events()
    test_inbox_state_atomic_save_and_guarded_resume()
    test_inbox_state_runtime_summary_uses_canonical_events()
    test_step_update_graph_fails_on_non_json_output()
    test_validate_completion_blocks_stale_errors_and_empty_graph()
    test_resolve_bare_name_normalized_match()
    test_ensure_keyword_connectivity()
    test_locator_aware_page_adds_optional_wiki_section_locator_only()
    test_raw_original_and_locator_companion_share_node_and_wiki_source_edge()
    test_agent_slots_resume_does_not_rewrite_wiki()
    test_bare_abbreviation_resolved_to_keyword_no_warning()
    test_bare_abbreviation_resolve_miss_keeps_warning()
    test_resolve_abbreviations_list_reports_unresolved_warnings()
    test_cleanup_orphans_removes_dangling_aliases_and_edges()
    test_sync_keyword_appends_to_hub_dedup()
    test_resolve_abbreviation_todo_finds_full_name_node()
    test_update_graph_failure_rolls_back_and_exposes_resume_point()
    test_graph_validation_failure_exposes_clean_graph_retry_point()
    test_wiki_validation_retry_budget_hands_off_without_third_full_rewrite()
    test_semantic_hard_error_gets_one_bounded_rewrite_then_handoff()
    test_resume_post_maintenance_uses_unified_inbox_tail()
    print("ingest pipeline regression: PASS")


def test_cleanup_orphans_removes_dangling_aliases_and_edges():
    """孤儿 alias（指向已删节点）+ 孤儿边（引用已删节点）应被 cleanup_orphan_references 清理。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        # 先建两个节点，建边，再删一个节点制造孤儿
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        graph_ingest.gl.ensure_node(conn, "academic/raw/temp", "Temp", "raw")
        conn.execute("INSERT INTO aliases (alias, node_path) VALUES (?, ?)",
                     ("临时别名", "academic/raw/temp"))
        conn.execute("INSERT INTO aliases (alias, node_path) VALUES (?, ?)",
                     ("the", "page"))
        conn.execute("INSERT INTO edges (subject, predicate, object, confidence, source, is_sr) "
                     "VALUES (?,?,?,?,?,?)",
                     ("page", "引用", "academic/raw/temp", "[可追溯]", "", 0))
        conn.commit()
        # 删节点制造孤儿 alias + 孤儿边
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM nodes WHERE path='academic/raw/temp'")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        cleaned = graph_ingest.cleanup_orphan_references(conn)
        assert cleaned["invalid_aliases"] == 1
        assert cleaned["orphan_aliases"] == 1
        assert conn.execute("SELECT COUNT(*) FROM aliases WHERE alias='the'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM aliases WHERE alias='临时别名'").fetchone()[0] == 0
        conn.close()


def test_sync_keyword_appends_to_hub_dedup():
    """sync_hub_keywords_to_hub 追加 keyword 到 ## 关键词 段，幂等去重。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        old_repo = graph_ingest.gl.REPO
        graph_ingest.gl.REPO = repo
        try:
            hub_dir = repo / "academic" / "wiki" / "hubs"
            hub_dir.mkdir(parents=True)
            hub_file = hub_dir / "测试方向.md"
            hub_file.write_text(
                "---\ntitle: 测试方向\ntype: topic-hub\n---\n\n# 测试方向\n\n## 关键词\n\n- 已有词\n",
                encoding="utf-8"
            )
            hub_path = "academic/wiki/hubs/测试方向"
            # 追加新词
            added = graph_ingest.sync_hub_keywords_to_hub(hub_path, ["新关键词"])
            assert added == 1
            text = hub_file.read_text(encoding="utf-8")
            assert "- 新关键词" in text
            assert "- 已有词" in text
            # 幂等：重复追加不增加
            added2 = graph_ingest.sync_hub_keywords_to_hub(hub_path, ["新关键词"])
            assert added2 == 0
        finally:
            graph_ingest.gl.REPO = old_repo


def test_resolve_abbreviation_todo_finds_full_name_node():
    """abbreviation-todo 中的裸缩写，若图里已有含该缩写括号释义的全称节点，则补 alias 并消解。"""
    import sync_keyword_aliases
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        # 写 abbreviation-todo.jsonl
        todo_dir = repo / "cross-domain"
        todo_dir.mkdir()
        todo_path = todo_dir / "abbreviation-todo.jsonl"
        todo_path.write_text(
            json.dumps({"object": "TransE", "predicate": "对比方法", "doc_id": "test"}) + "\n" +
            json.dumps({"object": "DistMult", "predicate": "对比方法", "doc_id": "test"}) + "\n",
            encoding="utf-8"
        )
        # 临时 graph.db
        conn = graph_ingest.gl.connect(repo / "graph.db")
        graph_ingest.gl.init_schema(conn)
        # 建全称节点（括号含 TransE）
        graph_ingest.gl.ensure_node(conn, "翻译模型translating embeddings(TransE)",
                                    "翻译模型translating embeddings(TransE)", "entity", entity_subtype="keyword")
        conn.commit()
        resolved, remaining = sync_keyword_aliases.resolve_abbreviation_todo(conn, repo)
        assert resolved == 1, f"expected 1 resolved, got {resolved}"
        assert remaining == 1, f"expected 1 remaining, got {remaining}"
        # alias 已写入
        alias = conn.execute("SELECT node_path FROM aliases WHERE alias='TransE'").fetchone()
        assert alias and "translating embeddings" in alias["node_path"]
        # todo 文件只含 DistMult
        remaining_objs = [json.loads(l)["object"] for l in todo_path.read_text(encoding="utf-8").strip().split("\n") if l.strip()]
        assert "TransE" not in remaining_objs
        assert "DistMult" in remaining_objs
        conn.close()


def test_paper_prefill_does_not_raise_name_error():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory).resolve()
        page = repo / "academic/wiki/papers/demo.md"
        page.parent.mkdir(parents=True)
        (repo / "cross-domain").mkdir()
        page.write_text("---\ntitle: Demo\ntype: paper-summary\nvenue: Test venue\n---\n\n## Navigation\n\nDemo navigation.\n\n## Content\n\n### 三、主要贡献\n\n- Demo contribution\n", encoding="utf-8")
        old_repo, old_db = graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB
        graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = repo, repo / "cross-domain/graph.db"
        try:
            conn = graph_ingest.gl.connect()
            graph_ingest.gl.init_schema(conn)
            conn.commit()
            conn.close()
            captured = StringIO()
            with redirect_stdout(captured):
                graph_ingest.cmd_prefill(Namespace(page="academic/wiki/papers/demo"))
            assert "研究关键词" in captured.getvalue()
        finally:
            graph_ingest.gl.REPO, graph_ingest.gl.GRAPH_DB = old_repo, old_db



def test_bare_abbreviation_resolved_to_keyword_no_warning():
    """能 resolve 到已有 keyword 的裸缩写 → 融合期复查移除 warning。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        # 预建 keyword 节点 + alias（MPS）
        graph_ingest.gl.ensure_node(conn, "矩阵乘积态matrix product state(MPS)",
                                    "矩阵乘积态matrix product state(MPS)", "entity",
                                    entity_subtype="keyword")
        graph_ingest.gl.insert_aliases(conn, "矩阵乘积态matrix product state(MPS)", ["MPS"])
        conn.commit()
        # 命题 object 含裸缩写 MPS（预建 keyword + alias 后可 resolve）
        triples = [{"subject": "page", "predicate": "局限性", "object": "实验局限于MPS架构"}]
        *_, dwarns = graph_ingest.add_knowledge_edges(conn, "page", triples)
        bare = [w for w in dwarns if w.get("issue") == "bare_abbreviation"]
        assert bare == [], f"resolve 命中的裸缩写不应 warning: {bare}"
        conn.close()


def test_bare_abbreviation_resolve_miss_keeps_warning():
    """无法 resolve 的裸缩写 → 保留 warning（交后置 agent 补全）。"""
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        graph_ingest.gl.ensure_node(conn, "page", "Page", "page")
        triples = [{"subject": "GPT", "predicate": "基于", "object": "BERT"}]
        *_, dwarns = graph_ingest.add_knowledge_edges(conn, "page", triples)
        bare = [w for w in dwarns if w.get("issue") == "bare_abbreviation"]
        assert len(bare) == 2, f"resolve miss 的裸缩写应保留 warning: {bare}"
        conn.close()


def test_resolve_abbreviations_list_reports_unresolved_warnings():
    """--list 知识库内双层校验：能 resolve 到 keyword 的不报，resolve miss 报 warning。"""
    spec = importlib.util.spec_from_file_location(
        "resolve_abbreviations", SCRIPTS / "resolve_abbreviations.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "graph.db"
        conn = graph_ingest.gl.connect(db_path)
        graph_ingest.gl.init_schema(conn)
        # 预建 keyword 节点 + alias（MPS），作为图层 resolve 命中
        graph_ingest.gl.ensure_node(conn, "矩阵乘积态matrix product state(MPS)",
                                    "矩阵乘积态matrix product state(MPS)", "entity",
                                    entity_subtype="keyword")
        graph_ingest.gl.insert_aliases(conn, "矩阵乘积态matrix product state(MPS)", ["MPS"])
        # 命题1：含可 resolve 的 MPS → 不应报
        graph_ingest.gl.ensure_node(conn, "实验局限于MPS架构", "实验局限于MPS架构",
                                    "entity", entity_subtype="proposition")
        # 命题2：含无法 resolve 的 GPT → 应报 warning
        graph_ingest.gl.ensure_node(conn, "实验局限于GPT架构", "实验局限于GPT架构",
                                    "entity", entity_subtype="proposition")
        conn.commit()
        from io import StringIO
        from contextlib import redirect_stdout
        buf = StringIO()
        with redirect_stdout(buf):
            mod.list_pending(conn)
        out = buf.getvalue()
        assert "MPS" not in out, "resolve 命中的缩写不应报 warning"
        assert "GPT" in out and "warning" in out, "resolve miss 的缩写应报 warning"
        conn.close()


if __name__ == "__main__":
    main()

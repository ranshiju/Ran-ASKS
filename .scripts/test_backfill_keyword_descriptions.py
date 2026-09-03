#!/usr/bin/env python3
"""Regression tests for source-bounded legacy keyword description backfill."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill_keyword_descriptions as bk
import graph_lib as gl
import source_locator as sl
import wiki_locator as wl


def make_fixture():
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    raw_rel = "academic/raw/references/demo/paper.md"
    raw = root / raw_rel
    raw.parent.mkdir(parents=True)
    raw.write_text(
        "矩阵乘积态用于以一维张量链压缩表示量子多体波函数及其纠缠结构。\n",
        encoding="utf-8",
    )
    page = "academic/wiki/papers/demo"
    wiki = root / f"{page}.md"
    wiki.parent.mkdir(parents=True)
    wiki.write_text(
        "---\ntitle: Demo\ntype: paper-summary\nsources:\n"
        f"  - {raw_rel}\n---\n\n# Demo\n\n## 方法\n\n"
        "本文使用矩阵乘积态表示量子多体波函数。[^r1]\n\n"
        f"## Sources\n\n[^r1]: {raw_rel}#L1\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(root / "graph.db")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    gl.ensure_node(
        conn,
        "矩阵乘积态matrix product state(MPS)",
        "矩阵乘积态matrix product state(MPS)",
        "entity",
        entity_subtype="keyword",
    )
    gl.add_node_origin(conn, "矩阵乘积态matrix product state(MPS)", page)
    conn.commit()
    originals = (gl.REPO, wl.REPO, sl.REPO)
    gl.REPO = wl.REPO = sl.REPO = root
    return temporary, conn, originals


def restore_fixture(temporary, conn, originals):
    conn.close()
    gl.REPO, wl.REPO, sl.REPO = originals
    temporary.cleanup()


def test_collect_candidates_requires_precise_raw_evidence():
    temporary, conn, originals = make_fixture()
    try:
        plan = bk.collect_candidates(conn)
        assert plan["candidate_count"] == 1
        candidate = plan["candidates"][0]
        assert candidate["raw_source"].endswith("paper.md#L1")
        assert "量子多体波函数" in candidate["evidence_quote"]
    finally:
        restore_fixture(temporary, conn, originals)


def test_prior_node_review_blocks_alternate_source_by_default():
    temporary, conn, originals = make_fixture()
    try:
        gl.add_node_description_review(
            conn,
            "矩阵乘积态matrix product state(MPS)",
            "academic/wiki/papers/other",
            "academic/raw/references/other/paper.md#L1",
            "reviewer_rejected",
            "alternate evidence rejected",
        )
        conn.commit()
        assert bk.collect_candidates(conn)["candidate_count"] == 0
        assert bk.collect_candidates(conn, retry_rejected=True)["candidate_count"] == 1
    finally:
        restore_fixture(temporary, conn, originals)


def test_apply_promotes_only_valid_source_bound_description():
    temporary, conn, originals = make_fixture()
    original_call = bk.call_json
    try:
        candidate = bk.collect_candidates(conn)["candidates"]
        def fake_call(*_args, **kwargs):
            if kwargs["operation"] == "ingest_api_keywords":
                return {
                    "ok": True,
                    "parsed": {
                        "descriptions": [{
                            "id": "K1",
                            "description": "矩阵乘积态以一维张量链压缩表示量子多体波函数及纠缠结构。",
                            "evidence_id": "E1",
                        }],
                        "uncertain": [],
                    },
                }
            return {"ok": True, "parsed": {"approved": ["K1"], "rejected": []}}
        bk.call_json = fake_call
        report = bk.apply_batches(conn, candidate, batch_size=1)
        assert report["accepted_count"] == 1
        assert report["api_calls"] == 2
        row = conn.execute(
            "SELECT description FROM nodes WHERE path=?",
            ("矩阵乘积态matrix product state(MPS)",),
        ).fetchone()
        assert "一维张量链" in row["description"]
        gloss = conn.execute(
            "SELECT source,is_primary FROM node_glosses WHERE node_path=?",
            ("矩阵乘积态matrix product state(MPS)",),
        ).fetchone()
        assert gloss["source"].endswith("paper.md#L1")
        assert gloss["is_primary"] == 1
    finally:
        bk.call_json = original_call
        restore_fixture(temporary, conn, originals)


def test_response_schema_rejects_worker_selected_locator():
    schema = bk.response_schema(["K1"])
    assert schema({
        "descriptions": [{"id": "K1", "description": "一条有效的文档局部概念说明。", "evidence_id": "E1"}],
        "uncertain": [],
    })
    assert not schema({
        "descriptions": [{
            "id": "K1",
            "description": "一条有效的文档局部概念说明。",
            "evidence_id": "E1",
            "source": "invented#L1",
        }],
        "uncertain": [],
    })
    assert not schema({
        "descriptions": [],
        "uncertain": [],
    })
    assert not schema({
        "descriptions": [{"id": "K1", "description": "一条有效的文档局部概念说明。", "evidence_id": "E1"}],
        "uncertain": ["K1"],
    })


def test_review_schema_is_exhaustive_and_reviewer_can_block_write():
    schema = bk.review_response_schema(["K1", "K2"])
    assert schema({
        "approved": ["K1"],
        "rejected": [{"id": "K2", "reason": "术语误译"}],
    })
    assert not schema({"approved": ["K1"], "rejected": []})
    assert not schema({
        "approved": ["K1"],
        "rejected": [{"id": "K1", "reason": "重复决定"}],
    })

    temporary, conn, originals = make_fixture()
    original_call = bk.call_json
    try:
        candidate = bk.collect_candidates(conn)["candidates"]
        def fake_call(*_args, **kwargs):
            if kwargs["operation"] == "ingest_api_keywords":
                return {
                    "ok": True,
                    "parsed": {
                        "descriptions": [{
                            "id": "K1",
                            "description": "矩阵乘积态是与证据不一致的错误概念说明。",
                            "evidence_id": "E1",
                        }],
                        "uncertain": [],
                    },
                }
            return {
                "ok": True,
                "parsed": {"approved": [], "rejected": [{"id": "K1", "reason": "证据不支持"}]},
            }
        bk.call_json = fake_call
        report = bk.apply_batches(conn, candidate, batch_size=1)
        assert report["accepted_count"] == 0
        assert report["rejected_count"] == 1
        assert report["rejected"][0]["reason"].startswith("reviewer_rejected:")
        assert not conn.execute(
            "SELECT 1 FROM node_glosses WHERE node_path=?",
            ("矩阵乘积态matrix product state(MPS)",),
        ).fetchone()
        review = conn.execute(
            "SELECT status,reason FROM node_description_reviews WHERE node_path=?",
            ("矩阵乘积态matrix product state(MPS)",),
        ).fetchone()
        assert review["status"] == "reviewer_rejected"
        assert bk.collect_candidates(conn)["candidate_count"] == 0
        assert bk.collect_candidates(conn, retry_rejected=True)["candidate_count"] == 1
    finally:
        bk.call_json = original_call
        restore_fixture(temporary, conn, originals)


def test_api_failures_are_persisted_and_not_reselected():
    for failing_operation, expected_status in (
        ("ingest_api_keywords", "generator_failed"),
        ("ingest_api_keyword_review", "review_failed"),
    ):
        temporary, conn, originals = make_fixture()
        original_call = bk.call_json
        try:
            candidate = bk.collect_candidates(conn)["candidates"]

            def fake_call(*_args, **kwargs):
                if kwargs["operation"] == failing_operation:
                    return {"ok": False, "error": "schema 校验失败"}
                return {
                    "ok": True,
                    "parsed": {
                        "descriptions": [{
                            "id": "K1",
                            "description": "矩阵乘积态以一维张量链压缩表示量子多体波函数及纠缠结构。",
                            "evidence_id": "E1",
                        }],
                        "uncertain": [],
                    },
                }

            bk.call_json = fake_call
            report = bk.apply_batches(conn, candidate, batch_size=1)
            assert report["accepted_count"] == 0
            assert report["rejected_count"] == 1
            review = conn.execute(
                "SELECT status,reason FROM node_description_reviews WHERE node_path=?",
                ("矩阵乘积态matrix product state(MPS)",),
            ).fetchone()
            assert review["status"] == expected_status
            assert review["reason"] == "schema 校验失败"
            assert bk.collect_candidates(conn)["candidate_count"] == 0
            assert bk.collect_candidates(conn, retry_rejected=True)["candidate_count"] == 1
        finally:
            bk.call_json = original_call
            restore_fixture(temporary, conn, originals)


def test_validation_preserves_source_language():
    english = "A spin-up-up-down state is selected and stabilized by quantum fluctuations."
    assert bk.validate_description(
        "1/3 magnetization plateau",
        "该磁化平台由上上下自旋态构成并受到量子涨落稳定。",
        english,
    ) == "source_language_mismatch"
    assert not bk.validate_description(
        "1/3 magnetization plateau",
        "The plateau has a spin-up-up-down state selected and stabilized by quantum fluctuations.",
        english,
    )
    assert "输出语言: English" in bk.build_prompt([{
        "title": "1/3 magnetization plateau",
        "evidence_quote": english,
    }])


def main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"keyword description backfill regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

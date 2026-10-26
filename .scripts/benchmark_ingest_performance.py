#!/usr/bin/env python3
"""Isolated latency replay with equal outputs; never a live end-to-end speed claim."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

import frontier
import graph_lib as gl
import node_semantics as ns


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def identity_trial(batch, delay_sec, count):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gl.init_schema(conn)
    for index in range(count):
        gl.ensure_node(conn, f"node-{index}", f"spectral method {index}", "entity")
    requests = [{"name": f"spectral-method-{index}", "context": f"evidence for method {index}"}
                for index in range(count)]
    calls = []

    def embed(texts):
        calls.append(len(texts))
        time.sleep(delay_sec)
        return np.array([[1.0, 0.0] for text in texts])

    try:
        with patch.object(ns, "_embed_queries", side_effect=embed), \
                patch.object(ns, "_cached_vectors", side_effect=lambda texts: {
                    text: np.array([1.0, 0.0]) for text in texts}):
            started = time.perf_counter()
            result = ns.resolve_nodes(conn, requests) if batch else [
                ns.resolve_node(conn, **request) for request in requests]
            elapsed = time.perf_counter() - started
        return {"seconds": elapsed, "requests": len(calls), "output_sha256": digest(result)}
    finally:
        conn.close()


def answer_trial(parallel, delay_sec):
    packet = {
        "question": "Which bound is supported?", "coverage": "fixed replay evidence",
        "candidates": [{"path": "academic/wiki/papers/replay", "title": "Replay"}],
        "raw_evidence": [{"locator": "academic/raw/references/replay/paper.md#L1",
                          "excerpt": "An upper bound is available; tightness is unknown."}],
        "anchors": {"raw": ["academic/raw/references/replay/paper.md#L1"],
                    "wiki": [], "graph": []}, "duplicate_candidates": [],
    }
    answer = {
        "kb_state": "partial", "answer": "An upper bound is supported; tightness is unknown.",
        "supported_claims": [{"claim": "An upper bound is available.",
                              "evidence": ["academic/raw/references/replay/paper.md#L1"]}],
        "derived_claims": [], "residual_gaps": ["tightness"], "coverage_note": "fixed replay evidence",
    }

    def generate(*args, **kwargs):
        time.sleep(delay_sec)
        return {"ok": True, "parsed": answer}

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        record_ids = []
        for index in range(3):
            record = frontier.new_question(f"Bound question {index}?", "user_proposed", packet)
            record["id"] = f"Q-replay-{index}"
            frontier.write_record(root, record)
            record_ids.append(record["id"])
        with patch.object(frontier.agent_task, "query_backend", return_value="api"), \
                patch.object(frontier, "build_kb_packet", side_effect=lambda *args, **kwargs: dict(packet)), \
                patch.object(frontier, "run_answer", side_effect=generate):
            started = time.perf_counter()
            result = frontier.capture_answer_attempts(root, record_ids, 6, "replay") if parallel else [
                frontier.answer_question(root, record_id, 6, transaction_id="replay")
                for record_id in record_ids]
            elapsed = time.perf_counter() - started
        if not all(item.get("status") == "completed" for item in result):
            raise AssertionError(f"Replay did not complete all original validators: {result}")
        saved = [frontier.find_record(root, record_id)[0] for record_id in record_ids]
        outputs = [{"fingerprint": record["answer_fingerprint"], "state": record["kb_state"],
                    "revisions": len(record["answer_history"]),
                    "evidence_scope": record["answer_history"][-1]["evidence_scope"]} for record in saved]
        return {"seconds": elapsed, "output_sha256": digest(outputs)}


def compare(trial, repeats):
    before = []
    after = []
    details = []
    for repeat in range(repeats):
        previous, current = (trial(False), trial(True)) if repeat % 2 == 0 else tuple(reversed(
            (trial(True), trial(False))))
        if previous["output_sha256"] != current["output_sha256"]:
            raise AssertionError("Optimization changed identity decisions or evidence-bound answers")
        before.append(previous["seconds"])
        after.append(current["seconds"])
        details.append({"before": previous, "after": current})
    baseline = statistics.median(before)
    optimized = statistics.median(after)
    return {"before_median_sec": round(baseline, 6), "after_median_sec": round(optimized, 6),
            "reduction_percent": round((1 - optimized / baseline) * 100, 2) if baseline else None,
            "outputs_identical": True, "trials": details}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-sec", type=float, default=0.05)
    parser.add_argument("--requests", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if not 0 <= args.delay_sec <= 10 or not 2 <= args.requests <= 64 or not 1 <= args.repeats <= 20:
        parser.error("delay-sec must be 0..10, requests 2..64, repeats 1..20")
    report = {
        "schema": "ingest-performance-replay-v1",
        "scope": "isolated fixed-latency replay; NOT live-paper end-to-end performance or scientific quality evaluation",
        "injected_request_latency_sec": args.delay_sec, "repeats": args.repeats,
        "identity": compare(lambda batch: identity_trial(batch, args.delay_sec, args.requests), args.repeats),
        "frontier": compare(lambda parallel: answer_trial(parallel, args.delay_sec), args.repeats),
        "live_graph_modified": False, "external_calls": 0,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        path = Path(args.output).resolve()
        allowed = (Path(__file__).resolve().parent.parent / "temp").resolve()
        if not path.is_relative_to(allowed):
            parser.error("output must be inside the repository temp directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()

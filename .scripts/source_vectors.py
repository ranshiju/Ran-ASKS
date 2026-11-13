#!/usr/bin/env python3
"""Explicit public Source vector projections; typed navigation, never facts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import source_ingest as si

POLICY = "source-segments-32l-6000c_utf8-2400-v1"
from vector_windows import MAX_BYTES, windows


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()



def _connection(repo, *, write=False):
    path = si.managed(repo, "academic/sources/vectors.sqlite")
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.executescript("""
          CREATE TABLE IF NOT EXISTS manifests (
            namespace TEXT, source_id TEXT, parse_id TEXT, text_sha256 TEXT,
            policy TEXT, dimension INTEGER, count INTEGER, built_at TEXT,
            PRIMARY KEY(namespace,source_id));
          CREATE TABLE IF NOT EXISTS vectors (
            namespace TEXT, source_id TEXT, locator TEXT, start INTEGER, end INTEGER,
            input_sha256 TEXT, vector BLOB, vector_sha256 TEXT,
            PRIMARY KEY(namespace,source_id,locator,start));
        """)
    else:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _snapshot(repo, source_id):
    record = si.load_receipt(repo, source_id)
    parsed = si.selected_parse(record)
    if record.get("source_status") != "source_ready" or not parsed:
        raise ValueError("Source is not ready")
    si.validate_binding(repo, record, parsed)
    body = si.managed(repo, parsed["text_path"]).read_text(encoding="utf-8")
    pieces, skipped = si.segments(body)
    parents, children = {}, []
    for first, last, text in pieces:
        locator = f"{parsed['text_path']}#L{first}-L{last}"
        parents[locator] = {"source_id": source_id, "locator": locator, "body": text,
                            "parse_id": parsed["parse_id"], "text_sha256": parsed["text_sha256"],
                            "title": record["title"]}
        for start, end, value in windows(text):
            children.append((locator, start, end, _hash(value), value))
    return record, parsed, parents, children, skipped


def _public_scope():
    # Direct CLI/build callers must honor the same private boundary as query_actions.
    import graph_lib as gl
    if gl.private_graph_path(gl.scoped_graph_path()):
        raise ValueError("Source vectors support public academic Sources only")


def build(source_ids, *, repo=si.REPO, events=None):
    """Build only explicitly selected Sources, atomically per source; no Raw writes."""
    _public_scope()
    import embed_helper as eh
    np = eh.require_numpy()
    ledger = events if events is not None else []
    namespace = eh._cache_namespace()
    summaries = []
    for sid in dict.fromkeys(source_ids):
        record, parsed, parents, children, skipped = _snapshot(repo, sid)
        if not children:
            raise ValueError("Source has no vectorizable indexed text")
        vectors = eh.embed_cached_batch([item[4] for item in children],
            request_options={"batch_size": 16, "timeout": 45, "retries": 0}, events=ledger)
        if vectors.ndim != 2 or len(vectors) != len(children) or not np.isfinite(vectors).all() or (np.linalg.norm(vectors, axis=1) == 0).any():
            raise ValueError("invalid cached embedding vectors")
        # Provider waiting must not permit publication against a changed selected parse.
        current = si.load_receipt(repo, sid)
        if current.get("source_status") != "source_ready" or si.selected_parse(current) != parsed:
            raise ValueError("selected Source parse changed during vector build")
        si.validate_binding(repo, current, parsed)
        rows = []
        for child, vector in zip(children, vectors):
            blob = np.asarray(vector, dtype="<f4").tobytes()
            rows.append((namespace, sid, *child[:4], blob, _hash(blob)))
        db = _connection(repo, write=True)
        try:
            with db:
                db.execute("DELETE FROM vectors WHERE namespace=? AND source_id=?", (namespace, sid))
                db.executemany("INSERT INTO vectors VALUES (?,?,?,?,?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO manifests VALUES (?,?,?,?,?,?,?,?)",
                    (namespace, sid, parsed["parse_id"], parsed["text_sha256"], POLICY,
                     vectors.shape[1], len(children), si.now()))
        finally:
            db.close()
        summaries.append({"source_id": sid, "parse_id": parsed["parse_id"], "parents": len(parents),
                          "vectors": len(children), "dimension": vectors.shape[1], "skipped_lines": skipped})
    return {"schema": "source-vectors-build-v1", "namespace": namespace, "policy": POLICY,
            "sources": summaries, "events": ledger}


def _load(db, repo, sid, namespace):
    import embed_helper as eh
    np = eh.require_numpy()
    manifest = db.execute("SELECT * FROM manifests WHERE namespace=? AND source_id=?", (namespace, sid)).fetchone()
    if not manifest:
        return None
    record, parsed, parents, children, skipped = _snapshot(repo, sid)
    if (manifest["parse_id"], manifest["text_sha256"], manifest["policy"]) != (parsed["parse_id"], parsed["text_sha256"], POLICY):
        raise ValueError("stale Source vector projection; explicit rebuild required")
    rows = db.execute("SELECT * FROM vectors WHERE namespace=? AND source_id=? ORDER BY locator,start", (namespace, sid)).fetchall()
    expected = {(p, a, b, h) for p, a, b, h, _ in children}
    actual = {(r["locator"], r["start"], r["end"], r["input_sha256"]) for r in rows}
    if len(rows) != manifest["count"] or len(rows) != len(children) or expected != actual:
        raise ValueError("Source vector coverage/binding mismatch; rebuild required")
    vectors = []
    for row in rows:
        if _hash(row["vector"]) != row["vector_sha256"]:
            raise ValueError("Source vector hash mismatch")
        v = np.frombuffer(row["vector"], dtype="<f4")
        if len(v) != manifest["dimension"] or not np.isfinite(v).all() or np.linalg.norm(v) == 0:
            raise ValueError("invalid stored Source vector")
        vectors.append(v)
    return parents, rows, np.asarray(vectors), skipped


def search(query, *, repo=si.REPO, topk=5, domain="academic", source_id=""):
    _public_scope()
    # Includes pre-LIMIT scope validation and never silently widens a requested source.
    result = si.search(query, repo=repo, topk=topk, domain=domain, source_id=source_id)
    result["mode"] = "source_hybrid"
    events = []
    status = {"status": "unavailable", "covered_sources": 0, "total_sources": 0,
              "missing_sources": [], "invalid_sources": [], "events": events}
    result["vector_channel"] = status
    if not query.strip():
        status["status"] = "empty_query"
        return result
    if len(query.encode("utf-8")) > MAX_BYTES:
        status.update(status="query_over_limit", max_query_utf8_bytes=MAX_BYTES)
        return result
    db = _connection(repo)
    if db is None:
        status["status"] = "index_missing"
        return result
    import embed_helper as eh
    np = eh.require_numpy()
    namespace = eh._cache_namespace()
    loaded = []
    try:
        ids = [source_id] if source_id else sorted(p.stem for p in si.managed(repo, "academic/sources").glob("S-*.json"))
        status["total_sources"] = len(ids)
        for sid in ids:
            try:
                item = _load(db, repo, sid, namespace)
                if item is None:
                    status["missing_sources"].append(sid)
                else:
                    loaded.append(item)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status["invalid_sources"].append({"source_id": sid, "error": str(exc)})
    finally:
        db.close()
    status["covered_sources"] = len(loaded)
    status["coverage_basis"] = "existing_lexical_segments; oversized_lines_remain_unindexed"
    if not loaded:
        status["status"] = "no_current_vectors"
        return result
    started = time.perf_counter()
    try:
        q = eh.embed_cached_batch([query], request_options={"batch_size": 1, "timeout": 30, "retries": 0}, events=events)[0]
        if not np.isfinite(q).all() or np.linalg.norm(q) == 0:
            raise ValueError("invalid query vector")
        best = {}
        for parents, rows, vectors, skipped in loaded:
            if vectors.shape[1] != len(q):
                raise ValueError("query/source vector dimension mismatch")
            similarities = vectors @ q / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(q))
            for row, score in zip(rows, similarities):
                key = row["locator"]
                if key not in best or score > best[key][0]:
                    best[key] = (float(score), parents[key], {"start": row["start"], "end": row["end"]})
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        status.update(status="query_failed", error_type=type(exc).__name__, http_status=getattr(exc, "status_code", None))
        return result
    finally:
        status["query_seconds"] = round(time.perf_counter() - started, 6)
    limit = max(1, min(int(topk), 20))
    ranked = sorted(best.values(), key=lambda item: (-item[0], item[1]["locator"]))
    # Global source discovery cannot be monopolized by a long source's many windows.
    per_source, bounded = {}, []
    for score, parent, span in ranked:
        sid = parent["source_id"]
        if not source_id and per_source.get(sid, 0) >= 2:
            continue
        per_source[sid] = per_source.get(sid, 0) + 1
        bounded.append((score, parent, span))
        if len(bounded) >= min(100, limit * 5):
            break
    vector_candidates = si.source_candidates([p for _, p, _ in bounded], repo=repo, query=query, issues=result["issues"])
    metadata = {p["locator"]: (score, span) for score, p, span in bounded}
    combined = {}
    for channel, lane in (("lexical", result["candidates"]), ("vector", vector_candidates)):
        for rank, candidate in enumerate(lane, 1):
            key = candidate["locator"]
            item = combined.setdefault(key, {**candidate, "retrieval_ranks": {}, "fusion_score": 0.0})
            item["retrieval_ranks"][channel] = rank
            item["fusion_score"] += 1 / (60 + rank)
            if channel == "vector":
                score, span = metadata[key]
                item["vector_match"] = {"cosine": score, "body_span": span, "meaning": "navigation_only"}
    ordered = sorted(combined.values(), key=lambda item: (-item["fusion_score"], item["locator"]))
    candidates, counts = [], {}
    for item in ordered:
        sid = item["source_id"]
        if not source_id and counts.get(sid, 0) >= 2:
            continue
        counts[sid] = counts.get(sid, 0) + 1
        candidates.append(item)
        if len(candidates) >= limit:
            break
    result["candidates"] = candidates
    result["retrieval_capabilities"].append("source_vector")
    result["passage_preference"] = "rrf_lexical_vector"
    result["source_candidate_cap"] = None if source_id else 2
    status["status"] = "partial" if status["missing_sources"] or status["invalid_sources"] else "ready"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build",))
    parser.add_argument("source_ids", nargs="+")
    args = parser.parse_args()
    events = []
    try:
        result = build(args.source_ids, events=events)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "status_code", None), "events": events}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

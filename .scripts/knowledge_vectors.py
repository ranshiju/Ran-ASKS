#!/usr/bin/env python3
"""Explicit current Statement vector projections; navigation, never certification."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time

import knowledge_store as ks
import source_ingest as si
from vector_windows import MAX_BYTES, windows

POLICY = "ku-title-statement-unresolved_utf8-2400-v1"


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _public_scope():
    import graph_lib as gl
    if gl.private_graph_path(gl.scoped_graph_path()):
        raise ValueError("KU vectors support public academic Statements only")


def _connection(repo, *, write=False):
    path = si.managed(repo, "academic/knowledge/vectors.sqlite")
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.executescript("""
          CREATE TABLE IF NOT EXISTS manifests (
            namespace TEXT, unit_id TEXT, revision INTEGER, snapshot_sha256 TEXT,
            policy TEXT, dimension INTEGER, count INTEGER, built_at TEXT,
            PRIMARY KEY(namespace,unit_id));
          CREATE TABLE IF NOT EXISTS vectors (
            namespace TEXT, unit_id TEXT, start INTEGER, end INTEGER,
            input_sha256 TEXT, vector BLOB, vector_sha256 TEXT,
            PRIMARY KEY(namespace,unit_id,start));
        """)
    else:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _snapshot(repo, uid):
    # Statement identity/path guard runs before reading anything else.
    ks.unit_path(repo, uid)
    record = ks.read(uid, repo=repo)
    body = record["title"] + "\n\n" + record["statement"]
    if record.get("unresolved"):
        body += "\n\nUnresolved:\n" + "\n".join(record["unresolved"])
    children = [(a, b, _hash(text), text) for a, b, text in windows(body)]
    if not children:
        raise ValueError("KU has no vectorizable text")
    return record, children


def build(unit_ids, *, repo=si.REPO, events=None):
    """Explicit selection only; small successful batches cached before proceeding."""
    _public_scope()
    import embed_helper as eh
    np = eh.require_numpy()
    ledger = events if events is not None else []
    namespace = eh._cache_namespace()
    snapshots, pending = [], []
    for uid in dict.fromkeys(unit_ids):
        with ks.lock(repo, uid):
            record, children = _snapshot(repo, uid)
        snapshots.append((record, children))
        pending.extend(item[3] for item in children)
    # The helper caches only once its call completes, so call it once per small batch.
    batches = []
    for start in range(0, len(pending), 16):
        texts = pending[start:start+16]
        values = eh.embed_cached_batch(texts, request_options={"batch_size": 16, "timeout": 45, "retries": 0}, events=ledger)
        if (values.ndim != 2 or len(values) != len(texts) or not values.shape[1]
                or not np.isfinite(values).all() or (np.linalg.norm(values, axis=1) == 0).any()):
            raise ValueError("invalid cached KU vectors")
        batches.extend(values)
    if batches and len({len(v) for v in batches}) != 1:
        raise ValueError("inconsistent KU vector dimensions")
    offset, summaries = 0, []
    for record, children in snapshots:
        uid = record["id"]
        values = batches[offset:offset+len(children)]
        offset += len(children)
        with ks.lock(repo, uid):
            current = ks.read(uid, repo=repo)
            if (current["revision"], current["snapshot_sha256"]) != (record["revision"], record["snapshot_sha256"]):
                raise ValueError("KU changed during vector build; explicit rebuild required")
            rows = []
            for child, vector in zip(children, values):
                blob = np.asarray(vector, dtype="<f4").tobytes()
                rows.append((namespace, uid, *child[:3], blob, _hash(blob)))
            db = _connection(repo, write=True)
            try:
                with db:
                    db.execute("DELETE FROM vectors WHERE namespace=? AND unit_id=?", (namespace, uid))
                    db.executemany("INSERT INTO vectors VALUES (?,?,?,?,?,?,?)", rows)
                    db.execute("INSERT OR REPLACE INTO manifests VALUES (?,?,?,?,?,?,?,?)",
                        (namespace, uid, record["revision"], record["snapshot_sha256"], POLICY,
                         len(values[0]), len(children), si.now()))
            finally:
                db.close()
        summaries.append({"id": uid, "revision": record["revision"], "vectors": len(children), "dimension": len(values[0])})
    return {"schema": "knowledge-vectors-build-v1", "namespace": namespace, "policy": POLICY,
            "units": summaries, "events": ledger}


def _load(db, repo, uid, namespace):
    import embed_helper as eh
    np = eh.require_numpy()
    manifest = db.execute("SELECT * FROM manifests WHERE namespace=? AND unit_id=?", (namespace, uid)).fetchone()
    if manifest is None:
        return None
    record, children = _snapshot(repo, uid)
    if (manifest["revision"], manifest["snapshot_sha256"], manifest["policy"]) != (record["revision"], record["snapshot_sha256"], POLICY):
        raise ValueError("stale KU vector projection; explicit rebuild required")
    rows = db.execute("SELECT * FROM vectors WHERE namespace=? AND unit_id=? ORDER BY start", (namespace, uid)).fetchall()
    expected = {(a, b, h) for a, b, h, _ in children}
    actual = {(r["start"], r["end"], r["input_sha256"]) for r in rows}
    if len(rows) != manifest["count"] or len(rows) != len(children) or expected != actual:
        raise ValueError("KU vector coverage/binding mismatch; rebuild required")
    vectors = []
    for row in rows:
        if _hash(row["vector"]) != row["vector_sha256"]:
            raise ValueError("KU vector hash mismatch")
        v = np.frombuffer(row["vector"], dtype="<f4")
        if len(v) != manifest["dimension"] or not np.isfinite(v).all() or np.linalg.norm(v) == 0:
            raise ValueError("invalid stored KU vector")
        vectors.append(v)
    return record, np.asarray(vectors)


def search(query, *, repo=si.REPO, topk=5, kind="all"):
    _public_scope()
    result = ks.search(query, repo=repo, topk=topk, kind=kind)
    limit = max(1, min(int(topk), 20))
    events = []
    status = {"status": "unavailable", "indexed_kind": "statement", "question_channel": "lexical",
              "covered_units": 0, "total_units": 0, "missing_units": [], "invalid_units": [], "events": events}
    result["requested_retrieval"] = "hybrid"
    result["vector_channel"] = status
    if kind == "question" or not query.strip():
        status["status"] = "not_applicable" if kind == "question" else "empty_query"
        return result
    if len(query.encode("utf-8")) > MAX_BYTES:
        status.update(status="query_over_limit", max_query_utf8_bytes=MAX_BYTES)
        return result
    import embed_helper as eh
    loaded, db = [], None
    try:
        db = _connection(repo)
        if db is None:
            status["status"] = "index_missing"
            return result
        np = eh.require_numpy()
        namespace = eh._cache_namespace()
        ids = sorted(p.stem for p in si.managed(repo, "academic/knowledge/units").glob("KU-*.md"))
        status["total_units"] = len(ids)
        for uid in ids:
            try:
                item = _load(db, repo, uid, namespace)
                if item is None:
                    status["missing_units"].append(uid)
                else:
                    loaded.append(item)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                status["invalid_units"].append({"id": uid, "error": str(exc)})
    except (sqlite3.Error, ImportError, RuntimeError) as exc:
        status.update(status="index_unavailable", error_type=type(exc).__name__)
        return result
    finally:
        if db is not None:
            db.close()
    status["covered_units"] = len(loaded)
    if not loaded:
        status["status"] = "no_current_vectors"
        return result
    started = time.perf_counter()
    try:
        q = eh.embed_cached_batch([query], request_options={"batch_size": 1, "timeout": 30, "retries": 0}, events=events)[0]
        if not np.isfinite(q).all() or np.linalg.norm(q) == 0:
            raise ValueError("invalid query vector")
        ranked = []
        for record, vectors in loaded:
            if vectors.shape[1] != len(q):
                raise ValueError("query/KU vector dimension mismatch")
            # One vote per KU; long entries cannot win by having more windows.
            score = float(np.max(vectors @ q / (np.linalg.norm(vectors, axis=1) * np.linalg.norm(q))))
            # Provider waiting may span a revision change. Never merge its stale metadata.
            if si.digest(ks.unit_path(repo, record["id"])) != record["snapshot_sha256"]:
                raise ValueError("KU changed during vector query")
            ranked.append((score, record))
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        status.update(status="query_failed", error_type=type(exc).__name__, http_status=getattr(exc, "status_code", None))
        return result
    finally:
        status["query_seconds"] = round(time.perf_counter() - started, 6)
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    semantic = [{**ks.candidate(record, query), "vector_match": {"cosine": score, "meaning": "navigation_only"}}
                for score, record in ranked[:limit]]
    combined = {}
    for channel, lane in (("lexical", result["candidates"]), ("vector", semantic)):
        for rank, candidate in enumerate(lane, 1):
            item = combined.setdefault(candidate["id"], {**candidate, "retrieval_ranks": {}, "fusion_score": 0.0})
            item["retrieval_ranks"][channel] = rank
            item["fusion_score"] += 1 / (60 + rank)
            if channel == "vector":
                item["vector_match"] = candidate["vector_match"]
    result["candidates"] = sorted(combined.values(), key=lambda r: (-r["fusion_score"], r["id"]))[:limit]
    result["capability"] = "hybrid"
    status["status"] = "partial" if status["missing_units"] or status["invalid_units"] else "ready"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build",))
    parser.add_argument("unit_ids", nargs="+")
    args = parser.parse_args()
    events = []
    try:
        result = build(args.unit_ids, events=events)
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__,
                          "http_status": getattr(exc, "status_code", None), "events": events}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

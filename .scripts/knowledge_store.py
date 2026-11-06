#!/usr/bin/env python3
"""Shared, academic-only Markdown KU store; indexes are disposable projections."""
from __future__ import annotations

from platform_compat import fcntl

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

import source_ingest as si
import source_locator as sl
import lexical_relevance as lr

REPO = si.REPO
KU_ID = re.compile(r"KU-[0-9a-f]{24}\Z")
QUESTION_ID = re.compile(r"Q-[A-Za-z0-9-]+\Z")
RELATIONS = {"supports", "limits", "contradicts", "related"}
STATEMENT_ROLES = {"concept_definition", "method_model", "scientific_finding", "unspecified"}
SOURCE_QUESTION_STATES = {"addressed", "left_open", "unspecified"}


def normalize_content_role(kind, role=None):
    default = "research_question" if kind == "question" else "unspecified"
    if role is None or role == "" or (kind == "question" and role == "unspecified"):
        role = default
    allowed = {"research_question"} if kind == "question" else STATEMENT_ROLES
    if not isinstance(role, str) or role not in allowed:
        raise ValueError("content_role is incompatible with KU kind")
    return role


@contextmanager
def lock(repo, key):
    path = si.managed(repo, "temp/knowledge-locks/" + si.fingerprint(key) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def unit_path(repo, unit_id, revision=None):
    if not KU_ID.fullmatch(unit_id):
        raise ValueError("invalid Statement ID")
    if revision is not None:
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        return si.managed(repo, f"academic/knowledge/history/{unit_id}/{revision}.md")
    return si.managed(repo, f"academic/knowledge/units/{unit_id}.md")


def render(record):
    meta = {k: v for k, v in record.items() if k != "statement"}
    # JSON is also YAML; a one-line header avoids delimiter ambiguity in user text.
    return "---\n" + json.dumps(meta, ensure_ascii=False) + "\n---\n\n" + record["statement"].strip() + "\n"


def read(unit_id, *, repo=REPO, revision=None):
    from frontier import parse_record, current_answer_snapshot
    if QUESTION_ID.fullmatch(unit_id):
        if revision is not None:
            raise ValueError("native Question uses Frontier answer_history, not synthetic KU revisions")
        path = si.managed(repo, f"academic/frontier/questions/{unit_id}.md")
        record, body = parse_record(path)
        if record.get("id") != unit_id or record.get("kind") != "question":
            raise ValueError("Question identity mismatch")
        return {**record, "statement": record.get("question", body), "revision": None,
                "content_role": "research_question", "source_question_status": record.get("source_question_status", "unspecified"),
                "snapshot_sha256": si.digest(path), "path": str(path.relative_to(repo)),
                "evidence": record.get("ku_evidence", []), "unresolved": record.get("residual_gaps", []),
                "current_answer": current_answer_snapshot(record), "native_frontier": True}
    path = unit_path(repo, unit_id)
    record, body = parse_record(path)
    if revision is not None and record.get("revision") != revision:
        path = unit_path(repo, unit_id, revision)
        record, body = parse_record(path)
    if record.get("id") != unit_id or record.get("schema") != "knowledge-unit-v1":
        raise ValueError("KU identity/schema mismatch")
    if revision is not None and record.get("revision") != revision:
        raise ValueError("KU revision mismatch")
    return {**record, "statement": body.strip(), "snapshot_sha256": si.digest(path),
            "content_role": record.get("content_role", "unspecified"),
            "path": str(path.relative_to(repo))}


def evidence_ref(repo, source_id, parse_id, start, end):
    record = si.load_receipt(repo, source_id)
    parsed = next((p for p in record["parses"] if p["parse_id"] == parse_id), None)
    if not parsed:
        raise ValueError("unknown source parse")
    si.validate_binding(repo, record, parsed)
    lines = si.managed(repo, parsed["text_path"]).read_text(encoding="utf-8").splitlines()
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise ValueError("invalid evidence span")
    text = "\n".join(lines[start-1:end])
    if not text.strip() or len(text) > 8000:
        raise ValueError("evidence span empty or too large; select a precise readable span")
    return {"source_id": source_id, "parse_id": parse_id, "source_path": record["original"],
            "source_sha256": record["content_sha256"], "text_sha256": parsed["text_sha256"],
            "locator": f"{parsed['text_path']}#L{start}-L{end}",
            "span_sha256": hashlib.sha256(text.encode()).hexdigest()}, text


def resolve_evidence(repo, ref):
    path, locator = sl.split_locator(ref["locator"])
    span = sl.line_range(locator)
    if not span:
        raise ValueError("KU evidence requires an exact line span")
    fresh, text = evidence_ref(repo, ref["source_id"], ref["parse_id"], *span)
    if any(fresh[k] != ref.get(k) for k in fresh):
        raise ValueError("evidence binding changed")
    return text


def evidence_status(record, *, repo=REPO):
    issues = []
    for ref in record.get("evidence", []):
        try:
            resolve_evidence(repo, ref)
            current = si.load_receipt(repo, ref["source_id"])
            if current.get("parse_id") != ref["parse_id"]:
                issues.append({"locator": ref["locator"], "status": "historical_parse", "readable": True})
        except (OSError, ValueError, KeyError) as exc:
            issues.append({"locator": ref.get("locator"), "status": "unavailable", "error": str(exc)})
    return issues


def index_connection(repo, write=False):
    path = si.managed(repo, "academic/knowledge/index.sqlite")
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path if write else path.as_uri()+"?mode=ro", uri=not write, timeout=30)
    conn.row_factory = sqlite3.Row
    if write:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS units USING fts5(id UNINDEXED, sha256 UNINDEXED, terms)")
    return conn


def index_unit(repo, unit_id):
    record = read(unit_id, repo=repo)
    conn = index_connection(repo, write=True)
    try:
        with conn:
            conn.execute("DELETE FROM units WHERE id=?", (unit_id,))
            conn.execute("INSERT INTO units VALUES(?,?,?)", (unit_id, record["snapshot_sha256"],
                         " ".join(si.terms(record["title"]+"\n"+record["statement"]))))
    finally:
        conn.close()


def rebuild_index(*, repo=REPO):
    folder = si.managed(repo, "academic/knowledge/units")
    paths = sorted(folder.glob("KU-*.md")) if folder.exists() else []
    conn = index_connection(repo, write=True)
    try:
        with conn:
            conn.execute("DELETE FROM units")
            for path in paths:
                record = read(path.stem, repo=repo)
                conn.execute("INSERT INTO units VALUES(?,?,?)", (record["id"], record["snapshot_sha256"],
                             " ".join(si.terms(record["title"]+"\n"+record["statement"]))))
    finally:
        conn.close()
    return {"indexed": len(paths)}


def _evidence_navigation(record):
    """Expose stored reading addresses without fetching or certifying Raw."""
    refs = record.get("evidence") or []
    preview = [{key: ref.get(key) for key in ("locator", "relation", "assessed_revision")}
               for ref in refs[:3] if isinstance(ref, dict) and ref.get("locator")]
    return {"evidence_preview": preview, "evidence_count": len(refs),
            "evidence_truncated": len(preview) < len(refs)}


def search(query, *, repo=REPO, topk=5, kind="all"):
    if kind not in {"all", "statement", "question"}:
        raise ValueError("unsupported KU kind")
    limit = max(1, min(int(topk), 20))
    tokens = si.terms(query)[:48]
    records, issues = [], []
    if kind != "question" and tokens:
        conn = index_connection(repo)
        if conn:
            try:
                expression = " OR ".join('"'+t+'"' for t in tokens)
                for row in conn.execute("SELECT id,sha256 FROM units WHERE units MATCH ? ORDER BY bm25(units) LIMIT ?", (expression, limit*3)):
                    try:
                        record = read(row["id"], repo=repo)
                        if record["snapshot_sha256"] != row["sha256"]:
                            raise ValueError("stale KU index; rebuild required")
                        records.append(record)
                    except (OSError, ValueError, KeyError) as exc:
                        issues.append(str(exc))
            finally:
                conn.close()
        else:
            issues.append("Statement index absent; source/Question recall remains separate; rebuild-index restores existing Statements")
    # Read native Question Markdown; do not rebuild/mutate Frontier merely to query it.
    if kind != "statement" and tokens:
        root = si.managed(repo, "academic/frontier/questions")
        for path in sorted(root.glob("Q-*.md")) if root.exists() else []:
            record = read(path.stem, repo=repo)
            if record.get("status") == "rejected":
                continue
            if set(tokens) & set(si.terms(record["title"]+" "+record["statement"])):
                records.append(record)
    for record in records:
        record["lexical_match"] = lr.match(query, record["title"], record["statement"])
    records.sort(key=lambda record: (*lr.rank_key(record["lexical_match"]), record["id"]))
    return {"query": query, "domain": "academic", "capability": "lexical", "issues": issues,
            "candidates": [{k: r.get(k) for k in ("id", "kind", "title", "revision", "path", "status", "kb_state", "scientific_state", "claim_mode", "origin_kind", "unresolved", "content_role", "source_question_status")} |
                           {**lr.excerpt(query, r["statement"]), **_evidence_navigation(r), "lexical_match": r["lexical_match"],
                            "next": "knowledge_read"} for r in records[:limit]]}


def _unchanged_content(old, proposed):
    fields = ("title", "statement", "origin_kind", "claim_mode", "content_role", "unresolved")
    if any(old.get(key) != proposed[key] for key in fields):
        return False
    if any(ref.get("assessed_revision") != old["revision"] for ref in old["evidence"]):
        return False

    def evidence_set(refs):
        # Order and execution stamps are not new evidence; all binding/extra fields are.
        return {si.fingerprint({**{k: v for k, v in ref.items()
                                 if k not in {"assessor", "assessed_revision"}},
                                "relation": ref.get("relation", "related")}) for ref in refs}

    return evidence_set(old["evidence"]) == evidence_set(proposed["evidence"])


def commit(*, repo=REPO, operation_id, title, statement, evidence, unresolved=None,
           unit_id=None, expected_revision=None, expected_sha256=None, origin_kind="source_statement",
           claim_mode="author_claim", reason="", assessor="agent", operation_sha256="", content_role=None,
           skip_unchanged=False):
    if not title.strip() or not statement.strip() or not reason.strip():
        raise ValueError("KU title, complete statement and change reason are required")
    if len(statement) > 16000 or len(title) > 300:
        raise ValueError("KU too large; preserve the bounded complete claim")
    if origin_kind not in {"source_statement", "synthesis", "user_proposed"}:
        raise ValueError("invalid origin_kind")
    if claim_mode not in {"author_claim", "conjecture", "interpretation", "unknown"}:
        raise ValueError("invalid claim_mode")
    uid = unit_id or "KU-" + si.fingerprint(operation_id)[:24]
    payload = [title, statement, evidence, unresolved, origin_kind, claim_mode, reason]
    if content_role is not None:
        payload.append({"content_role": content_role})
    operation_sha256 = operation_sha256 or si.fingerprint(payload)
    path = unit_path(repo, uid)
    with lock(repo, uid):
        old = read(uid, repo=repo) if path.exists() else None
        if old and operation_id in old.get("operations", []):
            if old.get("operation_digests", {}).get(operation_id) != operation_sha256:
                raise ValueError("operation ID reused with different content")
            return publication_result(repo, uid, reused=True)
        if unit_id and (old is None or old["revision"] != expected_revision or old["snapshot_sha256"] != expected_sha256):
            raise ValueError("KU revision conflict; prepare current context before revising")
        if old and not unit_id:
            raise ValueError("KU identity collision")
        role = normalize_content_role("statement", content_role if content_role is not None else (old or {}).get("content_role"))
        revision = old["revision"]+1 if old else 1
        normalized = []
        for ref in evidence:
            resolve_evidence(repo, ref)
            if ref.get("relation", "related") not in RELATIONS:
                raise ValueError("invalid evidence relation")
            normalized.append({**ref, "assessed_revision": revision, "assessor": assessor})
        if not normalized:
            raise ValueError("source-derived KU needs at least one packet evidence reference")
        record = {"schema": "knowledge-unit-v1", "id": uid, "kind": "statement", "title": title.strip(),
                  "statement": statement.strip(), "revision": revision, "status": "draft",
                  "origin_kind": origin_kind, "claim_mode": claim_mode, "content_role": role, "evidence": normalized,
                  "unresolved": list(unresolved or []), "change_reason": reason,
                  "created_at": old["created_at"] if old else si.now(), "updated_at": si.now(),
                  "operations": (old.get("operations", []) if old else [])+[operation_id],
                  "operation_digests": {**(old.get("operation_digests", {}) if old else {}), operation_id: operation_sha256}}
        if skip_unchanged and old and _unchanged_content(old, record):
            # No primary commit to recover. The caller saves the existing job receipt;
            # an interrupted no-write attempt must revalidate this snapshot on retry.
            return {**publication_result(repo, uid, reused=True), "unchanged": True}
        if old:
            history = unit_path(repo, uid, old["revision"])
            previous = path.read_text(encoding="utf-8")
            if history.exists() and history.read_text(encoding="utf-8") != previous:
                raise ValueError("history conflict; refusing to overwrite an old revision")
            if not history.exists():
                atomic_text(history, previous)
        atomic_text(path, render(record))
        return publication_result(repo, uid, reused=False)


def publication_result(repo, uid, reused):
    result = {"id": uid, "revision": read(uid, repo=repo)["revision"], "reused": reused, "status": "saved"}
    try:
        index_unit(repo, uid)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result.update(status="index_pending", error=str(exc))
    return result


def capture_question(*, repo=REPO, operation_id, question, evidence, title="", unresolved=None,
                     claim_mode="unknown", operation_sha256="", source_question_status="unspecified"):
    from frontier import new_question, validate_record, render_body
    if not isinstance(source_question_status, str) or source_question_status not in SOURCE_QUESTION_STATES:
        raise ValueError("invalid source_question_status; it is a source-local stance, not scientific resolution")
    uid = "Q-" + si.fingerprint(operation_id)[:24]
    path = si.managed(repo, f"academic/frontier/questions/{uid}.md")
    with lock(repo, uid):
        if path.exists():
            existing = read(uid, repo=repo)
            if existing.get("ku_operation") != operation_id:
                raise ValueError("Question ID collision")
            if existing.get("ku_operation_sha256") != operation_sha256:
                raise ValueError("Question operation ID reused with different content")
            return {"id": uid, "revision": None, "reused": True, "status": "saved"}
        for ref in evidence:
            resolve_evidence(repo, ref)
        if not evidence:
            raise ValueError("source question requires evidence")
        record = new_question(question, "paper_explicit", {}, source_locator=evidence[0]["locator"])
        record.update(id=uid, ku_operation=operation_id, ku_operation_sha256=operation_sha256, ku_evidence=evidence)
        record.update(title=title or record["title"], residual_gaps=list(unresolved or []), claim_mode=claim_mode)
        record.update(content_role="research_question", source_question_status=source_question_status)
        record["anchors"]["raw"] = [ref["locator"] for ref in evidence]
        validate_record(record)
        atomic_text(path, "---\n"+json.dumps(record, ensure_ascii=False)+"\n---\n\n"+render_body(record))
        return {"id": uid, "revision": None, "reused": False, "status": "saved"}

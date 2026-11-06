#!/usr/bin/env python3
"""Reusable citing-paper discovery and dated collections; metadata is not Raw evidence."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import argparse
from platform_compat import fcntl
import sys
import uuid

import source_ingest as si
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

from external_bibliography import doi, normalized

MAX_BYTES = 8_000_000
MAX_RECORDS = 20_000
MAX_JSON = 80_000_000
REPO = Path(__file__).resolve().parent.parent
SCHEMA = "citing-papers-collection-v1"
IMPORT_SCHEMA = "citing-papers-import-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def normalized_target(target):
    require(isinstance(target, dict), "target must be an object")
    identifier = doi(target.get("doi", ""))
    require(not target.get("doi") or identifier, "invalid target DOI")
    title = text(target.get("title", ""))
    require(identifier or title, "a valid DOI or exact title is required")
    return {**target, "doi": identifier, "title": title}


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def text(value, limit=4000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid or oversized text field")
    return value.strip()


def normalize_record(record):
    if not isinstance(record, dict):
        raise ValueError("citation record must be an object")
    title = text(record.get("title", ""))
    if not title:
        raise ValueError("citation title required")
    identifier = doi(record.get("doi", ""))
    if record.get("doi") and not identifier:
        raise ValueError("invalid DOI")
    provider_id = text(record.get("provider_id", ""), 300)
    if not identifier and not provider_id:
        raise ValueError("DOI or stable provider_id required; titles are not deduplication keys")
    authors = record.get("authors", [])
    if not isinstance(authors, list) or len(authors) > 10000:
        raise ValueError("invalid authors")
    people = []
    for author in authors:
        if isinstance(author, str):
            author = {"name": author}
        people.append({"name": text(author.get("name", ""), 300),
                       "id": text(author.get("id", ""), 300),
                       "orcid": text(author.get("orcid", ""), 300),
                       "position": text(author.get("position", ""), 40)})
    journal = record.get("journal", {})
    if isinstance(journal, str):
        journal = {"name": journal}
    issns = journal.get("issns", [])
    if not isinstance(issns, list) or not all(isinstance(x, str) and re.fullmatch(r"\d{4}-\d{3}[\dXx]", x) for x in issns):
        raise ValueError("invalid ISSNs")
    return {"doi": identifier, "provider_id": provider_id, "title": title,
            "authors": people, "journal": {"name": text(journal.get("name", ""), 400), "issns": issns},
            "published": text(str(record.get("published") or ""), 40),
            "type": text(record.get("type", "unknown"), 100),
            "url": text(record.get("url", ""), 2000),
            "citation_context": text(record.get("citation_context", ""), 16000)}


def from_openalex(work):
    source = (work.get("primary_location") or {}).get("source") or {}
    people = []
    for item in work.get("authorships", []):
        author = item.get("author") or {}
        people.append({"name": author.get("display_name") or "", "id": author.get("id") or "",
                       "orcid": author.get("orcid") or "", "position": item.get("author_position") or ""})
    return normalize_record({"doi": work.get("doi") or "", "provider_id": work.get("id") or "",
        "title": work.get("display_name") or work.get("title") or "", "authors": people,
        "journal": {"name": source.get("display_name") or "", "issns": source.get("issn") or []},
        "published": work.get("publication_date") or "", "type": work.get("type") or "unknown",
        "url": (work.get("primary_location") or {}).get("landing_page_url") or ""})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("citation metadata redirect refused")


def fetch(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.openalex.org" or
            not (parsed.path == "/works" or parsed.path.startswith("/works/")) or parsed.fragment):
        raise ValueError("unapproved citation metadata endpoint")
    # Only process environment; credentials never enter stored request URLs or diagnostics.
    key = os.environ.get("OPENALEX_API_KEY", "")
    requested = url + ("&" if parsed.query else "?") + urllib.parse.urlencode({"api_key": key}) if key else url
    request = urllib.request.Request(requested, headers={"User-Agent": "WikiGraph-citing-papers/1.0", "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            data = response.read(MAX_BYTES + 1)
    except Exception as exc:
        # HTTPError contains the request URL (possibly a secret). Do not serialize it.
        raise ValueError(f"OpenAlex request failed ({type(exc).__name__}, status={getattr(exc, 'code', 'unknown')}); check access/quota or import a citation list") from None
    if len(data) > MAX_BYTES:
        raise ValueError("citation response exceeds byte limit")
    return json.loads(data)


def discover(target, *, max_pages=5, fetcher=None):
    target = normalized_target(target)
    if not 1 <= max_pages <= 100:
        raise ValueError("max-pages must be 1..100")
    fetcher = fetcher or fetch
    batch = {"provider": "openalex", "started_at": now(), "target": dict(target),
             "records": [], "responses": [], "status": "failed", "complete": False,
             "scope": "OpenAlex indexed citing works; not all scholarly citations", "errors": []}

    def get(url):
        body = fetcher(url)
        if not isinstance(body, dict):
            raise ValueError("provider response must be an object")
        size = len(json.dumps(body, ensure_ascii=False).encode())
        if size > MAX_BYTES or sum(x["bytes"] for x in batch["responses"]) + size > 20_000_000:
            raise ValueError("citation batch response limit reached; partial coverage retained")
        batch["responses"].append({"url": url, "retrieved_at": now(), "sha256": fingerprint(body), "body": body, "bytes": size})
        return body

    try:
        if target.get("doi"):
            url = "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + target["doi"], safe="")
            work = get(url)
            if doi(work.get("doi", "")) != target["doi"]:
                raise ValueError("provider target DOI mismatch")
        else:
            body = get("https://api.openalex.org/works?" + urllib.parse.urlencode({"search": target["title"], "per-page": 50}))
            matches = [w for w in body.get("results", []) if normalized(w.get("display_name", "")) == normalized(target["title"])]
            if len(matches) != 1:
                batch["target_candidates"] = [from_openalex(w) for w in matches]
                raise ValueError("title identity ambiguous or unavailable; supply an exact DOI")
            work = matches[0]
        batch["resolved_target"] = from_openalex(work)
        work_id = str(work.get("id", "")).rsplit("/", 1)[-1]
        if not re.fullmatch(r"W\d+", work_id):
            raise ValueError("invalid OpenAlex target work ID")
        cursor, seen = "*", set()
        for _ in range(max_pages):
            if cursor in seen:
                raise ValueError("provider repeated pagination cursor")
            seen.add(cursor)
            body = get("https://api.openalex.org/works?" + urllib.parse.urlencode({
                "filter": "cites:" + work_id, "per-page": 200, "cursor": cursor}))
            if not isinstance(body.get("results"), list) or not isinstance(body.get("meta"), dict):
                raise ValueError("malformed citation page")
            if len(body["results"]) > 200 or len(batch["records"]) + len(body["results"]) > MAX_RECORDS:
                raise ValueError("citation record limit reached")
            for item in body["results"]:
                if work_id not in [str(x).rsplit("/", 1)[-1] for x in item.get("referenced_works", [])]:
                    raise ValueError("provider result does not reference target work")
                batch["records"].append(from_openalex(item))
            batch["provider_count"] = body["meta"].get("count")
            cursor = body["meta"].get("next_cursor")
            batch["next_cursor"] = cursor
            if not cursor:
                batch["complete"] = True
                break
            if not body["results"]:
                raise ValueError("empty page with nonterminal cursor")
        batch["status"] = "complete" if batch["complete"] else "partial"
    except (ValueError, KeyError, TypeError, OSError) as exc:
        batch["errors"].append(str(exc)[:500])
        batch["status"] = "partial" if batch["records"] else "failed"
    batch["finished_at"] = now()
    return batch


def imported(payload, target):
    target = normalized_target(target)
    if not isinstance(payload, dict) or payload.get("schema") not in {IMPORT_SCHEMA, "paper-impact-import-v1"}:
        raise ValueError("expected citing-papers-import-v1 (legacy paper-impact-import-v1 also accepted)")
    declared = payload.get("target", {})
    if target.get("doi"):
        if doi(declared.get("doi", "")) != target["doi"]:
            raise ValueError("import targets another DOI")
    elif normalized(declared.get("title", "")) != normalized(target["title"]):
        raise ValueError("import targets another title")
    for key in ("provider", "retrieved_at", "scope"):
        if not text(payload.get(key, "")):
            raise ValueError(f"import missing {key}")
    stamp = datetime.fromisoformat(payload["retrieved_at"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
        raise ValueError("retrieved_at requires timezone and cannot be in the future")
    if type(payload.get("complete")) is not bool:
        raise ValueError("import requires explicit completeness")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("invalid/oversized citation list")
    status = payload.get("retrieval_status", "complete" if payload["complete"] else "partial")
    require(status in {"complete", "partial", "failed"}, "invalid retrieval_status")
    require(payload["complete"] == (status == "complete"), "completeness contradicts retrieval_status")
    require(status != "failed" or not records, "failed import cannot claim observed records")
    provenance = payload.get("provenance", [])
    require(isinstance(provenance, list) and all(isinstance(x, dict) for x in provenance), "invalid provenance")
    observations = payload.get("observations", [])
    require(isinstance(observations, list), "invalid observations")
    normalized_records = [normalize_record(x) for x in records]
    known_aliases = {}
    for record in normalized_records:
        identity = "doi:" + record["doi"] if record["doi"] else record["provider_id"]
        for alias in aliases(record):
            known_aliases.setdefault(alias, set()).add(identity)
    for observation in observations:
        require(isinstance(observation, dict) and isinstance(observation.get("aliases"), list)
                and all(isinstance(x, str) for x in observation["aliases"]), "invalid observation aliases")
        matches = set().union(*(known_aliases.get(key, set()) for key in observation["aliases"]))
        require(len(matches) == 1, "observation must identify one exported paper")
        for field in ("first_seen_at", "last_seen_at"):
            at = datetime.fromisoformat(observation[field].replace("Z", "+00:00"))
            require(at.tzinfo is not None and at <= datetime.now(timezone.utc), "invalid observation time")
    return {"provider": payload["provider"], "target": declared,
            "started_at": stamp.astimezone(timezone.utc).isoformat(), "finished_at": now(),
            "scope": payload["scope"], "complete": payload["complete"],
            "status": status, "errors": ["Imported collection reports failed retrieval"] if status == "failed" else [],
            "records": normalized_records, "provenance": deepcopy(provenance),
            "origin_observations": deepcopy(observations),
            "responses": [{"sha256": fingerprint(payload), "body": payload,
                           "retrieved_at": payload["retrieved_at"], "url": "offline-import"}]}


def aliases(record):
    return {x for x in ("doi:" + record["doi"] if record.get("doi") else "", record.get("provider_id", "")) if x}


def merge_batch(state, batch):
    """Merge one validated discovery batch in memory; caller owns atomic persistence."""
    batch = deepcopy(batch)
    batch["id"] = "B-" + uuid.uuid4().hex[:16]
    batch["saved_at"] = now()
    new_ids, observed = [], []
    observed_times = {}
    for observation in batch.get("origin_observations", []):
        stamp = datetime.fromisoformat(observation["last_seen_at"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
        for alias in observation["aliases"]:
            observed_times[alias] = max(observed_times.get(alias, ""), stamp)
    alias_index = {}
    for cid, candidate in state["candidates"].items():
        for alias in candidate["aliases"]:
            require(alias not in alias_index or alias_index[alias] == cid, "stored citation identities conflict")
            alias_index[alias] = cid
    for record in batch["records"]:
        keys = aliases(record)
        record_seen = max((observed_times[k] for k in keys if k in observed_times), default=batch["started_at"])
        matches = {alias_index[key] for key in keys if key in alias_index}
        require(len(matches) <= 1, "cross-provider identity conflict; resolve import before saving")
        if matches:
            candidate = state["candidates"][next(iter(matches))]
            old_doi = candidate["record"].get("doi")
            require(not old_doi or not record["doi"] or old_doi == record["doi"], "same provider ID has conflicting DOIs")
            candidate["aliases"] = sorted(set(candidate["aliases"]) | keys)
            # An incomplete later provider record cannot erase a known DOI.
            record = deepcopy(record)
            record["doi"] = record["doi"] or old_doi
            if record_seen >= candidate["last_seen_at"]:
                candidate["record"] = record
            elif not old_doi and record["doi"]:
                candidate["record"]["doi"] = record["doi"]
            candidate["last_seen_at"] = max(candidate["last_seen_at"], record_seen)
        else:
            key = "doi:" + record["doi"] if record["doi"] else record["provider_id"]
            cid = "C-" + fingerprint(key)[:16]
            candidate = {"id": cid, "record": deepcopy(record), "aliases": sorted(keys),
                         "first_seen_at": batch["saved_at"], "last_seen_at": record_seen, "batch_ids": []}
            state["candidates"][cid] = candidate
            new_ids.append(cid)
        for alias in candidate["aliases"]:
            alias_index[alias] = candidate["id"]
        if batch["id"] not in candidate["batch_ids"]:
            candidate["batch_ids"].append(batch["id"])
        observed.append(candidate["id"])
    batch["candidate_ids"] = sorted(set(observed))
    batch["new_candidate_ids"] = new_ids
    state["batches"].append(batch)
    if batch.get("resolved_target"):
        state["target"] = batch["resolved_target"]
    return {"ok": True, "batch_id": batch["id"], "status": batch["status"], "complete": batch["complete"],
            "observed": len(set(observed)), "new": len(new_ids), "new_candidate_ids": new_ids,
            "errors": batch["errors"], "next_cursor": batch.get("next_cursor"), "scope": batch["scope"]}



def collection_path(repo, collection, suffix=""):
    require(isinstance(collection, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", collection),
            "invalid citation collection ID")
    return si.managed(repo, f"projects/citing-papers/{collection}" + ("/" + suffix if suffix else ""))


def read_json(path):
    require(path.is_file() and path.stat().st_size <= MAX_JSON, "missing/oversized citation JSON")
    return json.loads(path.read_text(encoding="utf-8"))


def load(repo, collection):
    state = read_json(collection_path(repo, collection, "state.json"))
    require(isinstance(state, dict) and state.get("schema") == SCHEMA and state.get("id") == collection,
            "citation collection identity mismatch")
    return state


@contextmanager
def collection_lock(repo, collection, *, create=False):
    root = collection_path(repo, collection)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    else:
        require(root.is_dir(), "citation collection does not exist; init it first")
    with collection_path(repo, collection, ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("citation collection is busy; retry the same command") from None
        try:
            yield root
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def save(repo, state):
    state["revision"] += 1
    state["updated_at"] = now()
    require(len(json.dumps(state, ensure_ascii=False, indent=2).encode()) + 1 <= MAX_JSON,
            "citation collection exceeds 80 MB; prior state retained, start a separate collection")
    si.atomic_json(collection_path(repo, state["id"], "state.json"), state)


def initialize(repo, collection, target):
    target = normalized_target(target)
    with collection_lock(repo, collection, create=True):
        require(not collection_path(repo, collection, "state.json").exists(), "citation collection already exists")
        state = {"schema": SCHEMA, "id": collection, "revision": 0, "created_at": now(),
                 "target": target, "batches": [], "candidates": {}}
        save(repo, state)
    return {"ok": True, "collection": collection, "target": target,
            "path": str(collection_path(repo, collection)), "next": "fetch or import"}


def collect(repo, collection, *, max_pages=5, fetcher=None):
    """Fetch and atomically persist one batch, including errors; no models or paper-impact needed."""
    with collection_lock(repo, collection):
        state = load(repo, collection)
        result = merge_batch(state, discover(state["target"], max_pages=max_pages, fetcher=fetcher))
        save(repo, state)
        result.update(collection=collection, revision=state["revision"], saved=True)
        if result["status"] == "failed":
            result.update(ok=False, error="; ".join(result["errors"]) or "citation retrieval failed")
        return result


def import_collection(repo, collection, payload):
    """Import an explicitly sourced list offline; an upstream export remains archived in the batch."""
    with collection_lock(repo, collection):
        state = load(repo, collection)
        result = merge_batch(state, imported(payload, state["target"]))
        save(repo, state)
        if result["status"] == "failed":
            result.update(ok=False, error="; ".join(result["errors"]))
        return {**result, "collection": collection, "revision": state["revision"], "saved": True}


def batch_summary(batch):
    keys = ("id", "provider", "started_at", "finished_at", "saved_at", "scope", "status", "complete",
            "errors", "provider_count", "new_candidate_ids", "candidate_ids")
    return {**{k: batch[k] for k in keys if k in batch}, "snapshot_sha256": fingerprint(batch),
            "response_sha256": [r["sha256"] for r in batch.get("responses", [])]}


def list_records(state, *, limit=50, offset=0, batch_id=None, new_only=False):
    require(type(limit) is int and 1 <= limit <= 1000 and type(offset) is int and offset >= 0,
            "limit must be 1..1000 and offset nonnegative")
    selected = None
    if batch_id or new_only:
        require(state["batches"], "no citation batch available")
        batch = next((b for b in state["batches"] if b["id"] == batch_id), None) if batch_id else state["batches"][-1]
        require(batch is not None, "unknown citation batch")
        selected = set(batch["new_candidate_ids"] if new_only else batch["candidate_ids"])
    records = [c for cid, c in state["candidates"].items() if selected is None or cid in selected]
    records.sort(key=lambda c: (c["first_seen_at"], c["id"]))
    total = len(records)
    return {"ok": True, "collection": state["id"], "target": state["target"], "total": total,
            "offset": offset, "truncated": offset + limit < total, "records": records[offset:offset + limit],
            "scope": "historical observed union; missing records do not prove citation removal"}


def export_payload(state):
    """Portable historical union with dates and upstream provenance, accepted by any consumer."""
    require(state["batches"], "nothing to export: fetch or import a citation list first")
    provenance, seen = [], set()
    for batch in state["batches"]:
        for record in [batch_summary(batch), *batch.get("provenance", [])]:
            key = fingerprint(record)
            if key not in seen:
                provenance.append(deepcopy(record))
                seen.add(key)
    candidates = sorted(state["candidates"].values(), key=lambda c: c["id"])
    require(len(candidates) <= MAX_RECORDS, "export exceeds 20000 records; use bounded collections")
    # Per-record timestamps stay distinct: exporting an older row does not reobserve it today.
    observations = [{"candidate_id": c["id"], "aliases": c["aliases"],
                     "first_seen_at": c["first_seen_at"], "last_seen_at": c["last_seen_at"],
                     "batch_ids": c["batch_ids"]} for c in candidates]
    for batch in state["batches"]:
        observations.extend(deepcopy(batch.get("origin_observations", [])))
    observations = list({fingerprint(x): x for x in observations}.values())
    succeeded = bool(candidates) or any(b["status"] != "failed" for b in state["batches"])
    return {"schema": IMPORT_SCHEMA, "target": state["target"], "provider": "citing-papers-collection",
            "retrieved_at": max(b["started_at"] for b in state["batches"]),
            "scope": "Historical union of recorded citation batches; provider/date coverage is in provenance; not a current exhaustive census",
            "complete": False, "retrieval_status": "partial" if succeeded else "failed",
            "source_collection": {"id": state["id"], "revision": state["revision"], "sha256": fingerprint(state)},
            "records": [c["record"] for c in candidates], "observations": observations, "provenance": provenance}


def export_collection(repo, collection):
    with collection_lock(repo, collection):
        state = load(repo, collection)
        payload = export_payload(state)
        path = collection_path(repo, collection, f"exports/citations-r{state['revision']}.json")
        require(len(json.dumps(payload, ensure_ascii=False, indent=2).encode()) + 1 <= MAX_JSON,
                "export exceeds byte limit")
        # Deterministic for this revision; repeated export is safe and does not trigger discovery.
        si.atomic_json(path, payload)
        return {"ok": True, "collection": collection, "path": str(path), "schema": IMPORT_SCHEMA,
                "records": len(payload["records"]), "sha256": fingerprint(payload),
                "retrieval_status": payload["retrieval_status"], "scope": payload["scope"]}


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="create an independent collection for one paper")
    p.add_argument("collection"); p.add_argument("--doi", default=""); p.add_argument("--title", default="")
    for name in ("fetch", "import", "list", "status", "export"):
        p = sub.add_parser(name); p.add_argument("collection")
        if name == "fetch": p.add_argument("--max-pages", type=int, default=5)
        if name == "import": p.add_argument("--file", required=True)
        if name == "list":
            p.add_argument("--limit", type=int, default=50); p.add_argument("--offset", type=int, default=0)
            p.add_argument("--batch"); p.add_argument("--new-only", action="store_true")
    return ap


def execute(args, repo=REPO):
    repo = Path(repo).resolve()
    if args.command == "init":
        return initialize(repo, args.collection, {"doi": args.doi, "title": args.title})
    if args.command == "fetch": return collect(repo, args.collection, max_pages=args.max_pages)
    if args.command == "import":
        path = Path(args.file).expanduser()
        path = (path if path.is_absolute() else repo / path).resolve()
        require(not path.is_relative_to((repo / "private").resolve()), "private inputs cannot enter public citation collections")
        return import_collection(repo, args.collection, read_json(path))
    if args.command == "export": return export_collection(repo, args.collection)
    state = load(repo, args.collection)
    if args.command == "list":
        return list_records(state, limit=args.limit, offset=args.offset, batch_id=args.batch, new_only=args.new_only)
    return {"ok": True, "collection": args.collection, "target": state["target"], "revision": state["revision"],
            "candidates": len(state["candidates"]), "batches": [batch_summary(b) for b in state["batches"]],
            "latest_status": state["batches"][-1]["status"] if state["batches"] else "not_queried"}


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(json.dumps({"ok": False, "status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())

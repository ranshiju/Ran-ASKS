#!/usr/bin/env python3
"""Shared Source-first transactions and rebuildable lexical source index.

No semantic model, Wiki, Graph, or global maintenance is required. Receipts are
the recovery authority; SQLite contains disposable search projections only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "source-receipt-v1"
SOURCE_ID = re.compile(r"S-[0-9a-f]{24}\Z")
SUPPORTED = {".pdf", ".md", ".txt"}
MAX_SEGMENT_CHARS = 6000
INDEX_VERSION = "source-fts-v1"


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def managed(repo, relative):
    repo = Path(repo).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError("managed path must be repository-relative")
    cursor = repo
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("managed path cannot traverse symlinks")
    if not cursor.resolve().is_relative_to(repo):
        raise ValueError("managed path escapes repository")
    return cursor


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with tmp.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def copy_original(source, target, expected):
    """Immutable file publication; an interrupted copy never becomes an original."""
    if target.exists():
        if not target.is_file() or digest(target) != expected:
            raise ValueError("original destination conflicts with received bytes")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + target.name + "." + uuid.uuid4().hex)
    try:
        shutil.copyfile(source, tmp)
        if digest(tmp) != expected or digest(source) != expected:
            raise ValueError("source changed while receiving")
        with tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def source_lock(repo, source_id):
    if not SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid source ID")
    path = managed(repo, f"temp/source-ingest/{source_id}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def receipt_path(repo, source_id):
    if not SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid source ID")
    return managed(repo, f"academic/sources/{source_id}.json")


def load_receipt(repo, source_id):
    record = json.loads(receipt_path(repo, source_id).read_text())
    if record.get("schema") != SCHEMA or record.get("source_id") != source_id:
        raise ValueError("source receipt identity mismatch")
    prefix = f"academic/raw/sources/{source_id}/"
    if not str(record.get("original", "")).startswith(prefix):
        raise ValueError("source receipt original escapes source bundle")
    return record


def write_receipt(repo, record):
    record["updated_at"] = now()
    atomic_json(receipt_path(repo, record["source_id"]), record)


def selected_parse(record):
    return next((p for p in record.get("parses", []) if p["parse_id"] == record.get("parse_id")), None)


def validate_binding(repo, record, parsed=None):
    original = managed(repo, record["original"])
    if not original.is_file() or digest(original) != record["content_sha256"]:
        raise ValueError("received original hash mismatch or missing")
    if parsed:
        prefix = f"academic/raw/sources/{record['source_id']}/"
        if not parsed["text_path"].startswith(prefix):
            raise ValueError("parse carrier escapes source bundle")
        text_path = managed(repo, parsed["text_path"])
        if not text_path.is_file() or digest(text_path) != parsed["text_sha256"]:
            raise ValueError("parse carrier hash mismatch or missing")


def binding_for_target(repo, target):
    """Guard Source-first carriers even when reached directly through read_raw."""
    try:
        candidate = Path(target)
        if not candidate.is_absolute():
            candidate = Path(repo).resolve() / candidate
        rel = candidate.relative_to(Path(repo).resolve())
    except ValueError:
        return None
    if rel.parts[:3] != ("academic", "raw", "sources"):
        return None
    try:
        managed(repo, rel)
        if len(rel.parts) < 5:
            raise ValueError("incomplete managed source path")
        record = load_receipt(repo, rel.parts[3])
        name = rel.as_posix()
        parsed = next((p for p in record.get("parses", []) if p["text_path"] == name), None)
        if parsed is None and name != record["original"]:
            raise ValueError("unregistered source carrier")
        validate_binding(repo, record, parsed)
        return {"status": "valid", "reason": "source_receipt_hash_bound",
                "source_id": record["source_id"], "source_path": record["original"],
                "parse_id": parsed["parse_id"] if parsed else None}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"status": "invalid", "reason": str(exc)}


def terms(text):
    """Unicode words plus CJK bigrams; literal FTS input, no query operators."""
    words = re.findall(r"[a-zA-Z0-9_]{2,}", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run[i:i+2] for i in range(max(1, len(run)-1)))
    return list(dict.fromkeys(words))


def segments(text):
    lines = text.splitlines()
    out, skipped = [], []
    start, current, size = 1, [], 0
    def emit():
        if current and terms("\n".join(current)):
            out.append((start, start + len(current)-1, "\n".join(current)))
    for n, line in enumerate(lines, 1):
        if len(line) > MAX_SEGMENT_CHARS:
            emit()
            current, size, start = [], 0, n+1
            skipped.append(n)
            continue
        if current and (size + len(line)+1 > MAX_SEGMENT_CHARS or len(current) >= 32):
            emit()
            current, size, start = [], 0, n
        current.append(line)
        size += len(line)+1
    emit()
    return out, skipped


def index_path(repo):
    return managed(repo, "academic/sources/index.sqlite")


def connect_index(repo, *, write=False):
    path = index_path(repo)
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript("""
          CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY, parse_id TEXT NOT NULL, text_sha256 TEXT NOT NULL,
            title TEXT NOT NULL, indexed_at TEXT NOT NULL);
          CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            source_id UNINDEXED, locator UNINDEXED, body UNINDEXED, terms,
            tokenize='unicode61');
        """)
    else:
        if not path.is_file():
            return None
        conn = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def publish_index(repo, record):
    # A failed rebuild must not leave a successful readiness receipt.
    record["source_status"] = "received"
    write_receipt(repo, record)
    parsed = selected_parse(record)
    if not parsed:
        raise ValueError("no parse carrier to index")
    validate_binding(repo, record, parsed)
    text = managed(repo, parsed["text_path"]).read_text(encoding="utf-8")
    pieces, skipped = segments(text)
    if not pieces:
        raise ValueError("no searchable text segments; original remains received")
    conn = connect_index(repo, write=True)
    try:
        with conn:
            conn.execute("DELETE FROM chunks WHERE source_id=?", (record["source_id"],))
            for start, end, body in pieces:
                locator = f"{parsed['text_path']}#L{start}-L{end}"
                conn.execute("INSERT INTO chunks VALUES (?,?,?,?)", (record["source_id"], locator, body,
                    " ".join(terms(record["title"] + "\n" + body))))
            conn.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)", (
                record["source_id"], parsed["parse_id"], parsed["text_sha256"], record["title"], now()))
        if conn.execute("SELECT COUNT(*) FROM chunks WHERE source_id=?", (record["source_id"],)).fetchone()[0] != len(pieces):
            raise ValueError("index readback failed")
        probe = '"' + terms(pieces[0][2])[0] + '"'
        if not conn.execute("SELECT 1 FROM chunks WHERE source_id=? AND chunks MATCH ? LIMIT 1",
                            (record["source_id"], probe)).fetchone():
            raise ValueError("fulltext index query readback failed")
    finally:
        conn.close()
    record["source_status"] = "source_ready"
    record["index"] = {"version": INDEX_VERSION, "parse_id": parsed["parse_id"],
                       "segments": len(pieces), "skipped_long_lines": skipped, "indexed_at": now()}
    job_id = fingerprint([record["source_id"], parsed["parse_id"], "knowledge_delta_v1"])
    jobs = record.setdefault("pending_jobs", [])
    if not any(job["id"] == job_id for job in jobs):
        status = "not_required" if record.get("knowledge_policy") == "source_only" else "pending"
        jobs.append({"id": job_id, "parse_id": parsed["parse_id"],
                     "kind": "knowledge_delta", "status": status, "created_at": now()})
        record["knowledge_status"] = status
        record.pop("knowledge_run", None)
    record.pop("error", None)
    write_receipt(repo, record)


def reference_section_lines(lines):
    """Locate explicit Markdown bibliography sections, not incidental mentions."""
    marked, active_level, fence = set(), None, None
    for n, line in enumerate(lines, 1):
        delimiter = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if delimiter:
            if fence is None:
                fence = delimiter[1]
            elif delimiter[1][0] == fence[0] and len(delimiter[1]) >= len(fence) and not delimiter[2].strip():
                fence = None
            heading = None
        else:
            heading = None if fence else re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$", line)
        if heading:
            level, title = len(heading[1]), heading[2].strip("*_ :：").casefold()
            if active_level is not None and level <= active_level:
                active_level = None
            title = re.sub(r"^(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)]?[ \t]+", "", title)
            if title in {"references", "bibliography", "参考文献", "参考资料"}:
                active_level = level
        if active_level is not None:
            marked.add(n)
    return marked


def search(query, *, repo=REPO, topk=5, domain="academic"):
    import lexical_relevance as lr
    if domain not in ("academic", "public", ""):
        raise ValueError("Source-first search currently supports academic public sources only")
    limit = max(1, min(int(topk), 20))
    query_terms = terms(query)
    result = {"query": query, "domain": "academic", "mode": "source_fulltext",
              "candidates": [], "issues": [], "query_terms_truncated": len(query_terms)>32,
              "index_available": managed(repo, "academic/sources/index.sqlite").is_file(),
              "retrieval_capabilities": ["lexical", "cjk_bigrams"], "next": "read_raw"}
    if not query_terms:
        return result
    conn = connect_index(repo)
    if conn is None:
        return result
    result["index_available"] = True
    try:
        expression = " OR ".join('"'+t.replace('"','""')+'"' for t in query_terms[:32])
        rows = conn.execute("""SELECT c.source_id,c.locator,c.body,s.parse_id,s.text_sha256,s.title,
                   bm25(chunks) AS rank FROM chunks c JOIN sources s ON s.source_id=c.source_id
                   WHERE chunks MATCH ? ORDER BY rank,c.source_id,c.locator LIMIT ?""",
                   (expression, limit*5)).fetchall()
        checked, carriers, reference_lines, labels = {}, {}, {}, {}
        for row in rows:
            sid = row["source_id"]
            if sid not in checked:
                try:
                    r = load_receipt(repo, sid)
                    p = selected_parse(r)
                    if r["source_status"] != "source_ready" or not p or p["parse_id"] != row["parse_id"] or p["text_sha256"] != row["text_sha256"]:
                        raise ValueError("stale/unpublished source index; resume source to recover")
                    validate_binding(repo, r, p)
                    checked[sid] = r
                    carriers[sid] = managed(repo, p["text_path"]).read_text(encoding="utf-8").splitlines()
                    reference_lines[sid] = reference_section_lines(carriers[sid])
                    labels[sid] = {"title": row["title"], "title_basis": r.get("title_basis", "unknown")}
                    opening = next((line.strip() for line in carriers[sid] if line.strip()), "")
                    heading = re.fullmatch(r"#\s+(.+?)(?:\s+#+)?", opening)
                    if r.get("title_basis") == "filename_unverified" and heading:
                        labels[sid] = {"title": heading[1][:200],
                                       "title_basis": "opening_heading_unverified",
                                       "filename_title": row["title"]}
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    checked[sid] = None
                    result["issues"].append({"source_id": sid, "error": str(exc)})
            record = checked[sid]
            if not record:
                continue
            parsed = selected_parse(record)
            match = re.fullmatch(re.escape(parsed["text_path"])+r"#L(\d+)-L(\d+)", row["locator"])
            if not match:
                result["issues"].append({"source_id": sid, "error": "index locator mismatch; resume to rebuild"})
                continue
            start, end = map(int, match.groups())
            if (not 1 <= start <= end <= len(carriers[sid]) or
                    row["body"] != "\n".join(carriers[sid][start-1:end])):
                result["issues"].append({"source_id": sid, "error": "index excerpt mismatch; resume to rebuild"})
                continue
            result["candidates"].append({"kind": "source", "source_id": sid, **labels[sid],
                "source_path": record["original"], "parse_id": row["parse_id"], "locator": row["locator"],
                "section_role": "references" if all(n in reference_lines[sid] or not carriers[sid][n-1].strip()
                                                     for n in range(start, end+1)) else "unspecified",
                **lr.excerpt(query, row["body"]),
                "lexical_match": lr.match(query, row["title"], row["body"]),
                "limitations": public_receipt(record)["limitations"],
                "knowledge_status": record.get("knowledge_status", "pending")})
        result["candidates"].sort(key=lambda item: lr.rank_key(item["lexical_match"]))
        bibliography_query = re.search(r"\b(?:references?|bibliograph\w*|citations?|cited|cites?|doi)\b|参考文献|引用文献|参考资料", query, re.I)
        result["passage_preference"] = "literal" if bibliography_query else "non_reference_within_source"
        if not bibliography_query:
            # Keep each source's positions in the bounded result pool, but choose
            # its non-bibliography matches first. Reference-only hits remain usable.
            groups = {}
            for item in result["candidates"]:
                groups.setdefault((item["source_id"], item["parse_id"]), []).append(item)
            ordered = {key: iter(sorted(items, key=lambda item: item["section_role"] == "references"))
                       for key, items in groups.items()}
            result["candidates"] = [next(ordered[(item["source_id"], item["parse_id"])])
                                    for item in result["candidates"]]
        result["candidates"] = result["candidates"][:limit]
    finally:
        conn.close()
    return result


def bundle_files(folder, source_hash):
    """Reuse a bound PDF/text bundle; never follow arbitrary parser file paths."""
    from inbox_finalize import manifest_file
    pdf = manifest_file(folder, "paper.pdf", "PDF")
    md = manifest_file(folder, "paper.md", "Markdown")
    if digest(pdf) != source_hash:
        raise ValueError("parse bundle belongs to a different original")
    files, missing = {"paper.md": md}, []
    meta = folder / "parse_meta.yaml"
    if meta.exists():
        files["parse_meta.yaml"] = manifest_file(folder, "parse_meta.yaml", "parser metadata")
    for ref in re.findall(r"!\[[^\]]*\]\(<?([^\s)>]+)", md.read_text(encoding="utf-8")):
        if "://" in ref or ref.startswith("data:"):
            continue  # No remote fetch. The source Markdown remains unchanged.
        from urllib.parse import unquote
        ref = unquote(ref)
        if not ref.startswith("images/") or ".." in Path(ref).parts or "\\" in ref:
            raise ValueError("local parser image must be in images/")
        candidate = managed(folder, ref)
        if not candidate.exists():
            missing.append(ref)
            continue
        files[ref] = manifest_file(folder, ref, "image", nested=True)
    return files, sorted(set(missing))


def parse_source(repo, record, *, reuse_parse=None, allow_paddleocr=False):
    original = managed(repo, record["original"])
    validate_binding(repo, record)
    if original.suffix != ".pdf":
        text = original.read_text(encoding="utf-8")
        if "\x00" in text:
            raise ValueError("text source contains NUL bytes")
        return {"parse_id": "P-text-v1", "text_path": record["original"],
                "text_sha256": record["content_sha256"], "parser": "identity-utf8-v1",
                "limitations": ["原始文字可定位；未核验科学内容或外链附件。"], "artifacts": []}
    if reuse_parse:
        supplied = Path(reuse_parse).expanduser().absolute()
        if any(p.is_symlink() for p in (supplied, *supplied.parents)):
            raise ValueError("parse bundle cannot traverse symlinks")
        folder = supplied.resolve()
        if folder.is_relative_to((Path(repo)/"private").resolve()):
            raise ValueError("private parse bundle cannot be imported into public sources")
        files, missing = bundle_files(folder, record["content_sha256"])
        spec = {"mode": "bound-bundle-reuse", "artifacts": {name:digest(p) for name,p in files.items()},
                "missing_assets": missing}
    else:
        import extractor
        config = extractor.load_config().get("extraction", {})
        # Persist a fingerprint, never provider keys or arbitrary configuration values.
        spec = {"mode": "managed-mineru", "config_sha256": fingerprint(config),
                "adapter_sha256": {name: digest(Path(extractor.__file__).with_name(name))
                                   for name in ("extractor.py", "mineru_api.py", "paddleocr_api.py")},
                "protocol": "source-parse-v1", "allow_paddleocr": bool(allow_paddleocr)}
    parse_id = "P-" + fingerprint([record["content_sha256"], spec])[:24]
    previous = next((p for p in record.get("parses", []) if p["parse_id"]==parse_id), None)
    if previous:
        validate_binding(repo, record, previous)
        return previous
    if not reuse_parse:
        folder = managed(repo, f"temp/source-ingest/{record['source_id']}/{parse_id}/parse")
        folder.mkdir(parents=True, exist_ok=True)
        copy_original(original, folder/"paper.pdf", record["content_sha256"])
        marker = folder/"parse-success.json"
        if not marker.is_file():
            ok = extractor.extract_paper("parse", engine="mineru", papers_dir=folder.parent)
            if not ok and allow_paddleocr and (config.get("paddleocr") or {}).get("enabled"):
                ok = extractor.extract_paper("parse", engine="paddleocr", papers_dir=folder.parent, allow_paddleocr=True)
            if not ok:
                raise ValueError("managed PDF parsing failed; original retained, resume is available")
            files, missing = bundle_files(folder, record["content_sha256"])
            atomic_json(marker, {name:digest(p) for name,p in files.items()})
        files, missing = bundle_files(folder, record["content_sha256"])
        if {name:digest(p) for name,p in files.items()} != json.loads(marker.read_text()):
            raise ValueError("staged parse changed; retain checkpoint for diagnosis")
    rel = f"academic/raw/sources/{record['source_id']}/parses/{parse_id}"
    destination = managed(repo, rel)
    hashes = {name:digest(p) for name,p in files.items()}
    if destination.exists():
        for name,h in hashes.items():
            if digest(managed(repo, rel+"/"+name)) != h:
                raise ValueError("immutable parse destination conflicts with parser output")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name("."+parse_id+"-"+uuid.uuid4().hex)
        staging.mkdir()
        try:
            for name,p in files.items():
                out = staging/name
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, out)
                if digest(out) != hashes[name]:
                    raise ValueError("parse changed while publishing")
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    limitations = ["全文文字层已建立定位；页覆盖、阅读顺序、公式与图表未作全面语义核验。"]
    if missing:
        limitations.append(f"解析包缺少 {len(missing)} 个引用图片；正文可检索，图像须回原件核对，不视为完整图表解析。")
    return {"parse_id": parse_id, "text_path": rel+"/paper.md", "text_sha256": hashes["paper.md"],
            "parser": spec, "limitations": limitations, "missing_assets": missing,
            "created_at": now(),
            "artifacts": [{"path":rel+"/"+name,"sha256":h} for name,h in hashes.items()]}


def finish(repo, record, *, reuse_parse=None, allow_paddleocr=False):
    phase = "parse"
    try:
        parsed = parse_source(repo, record, reuse_parse=reuse_parse, allow_paddleocr=allow_paddleocr)
        if not any(p["parse_id"]==parsed["parse_id"] for p in record["parses"]):
            record["parses"].append(parsed)
        record["parse_id"] = parsed["parse_id"]
        record["source_status"] = "received"
        write_receipt(repo, record)  # Recovery can index without calling the parser again.
        phase = "index"
        publish_index(repo, record)
    except Exception as exc:
        record["error"] = {"phase": phase, "message": f"{type(exc).__name__}: {exc}"[:700]}
        write_receipt(repo, record)
    return record


def parse_request(record, reuse_parse, refresh_parse):
    """Persist the selected parser route, including across a failed bundle reuse."""
    if reuse_parse:
        record["parse_request"] = {"mode": "bound-bundle-reuse", "bundle": str(Path(reuse_parse).expanduser().absolute())}
    elif refresh_parse or "parse_request" not in record:
        record["parse_request"] = {"mode": "managed-parser"}
    return record["parse_request"].get("bundle")


def configure_knowledge(record, required=None):
    """Apply a trusted caller's intent; source text never chooses this policy."""
    if required is None:
        return
    if type(required) is not bool:
        raise ValueError("knowledge_required must be a boolean or omitted")
    record["knowledge_policy"] = "optional" if required else "source_only"
    status = "pending" if required else "not_required"
    for job in record.get("pending_jobs", []):
        if job.get("status") in {"pending", "not_required"} and not job.get("last_run"):
            job["status"] = status
    # Preserve all actual executions and their outcome, including unfinished runs.
    if not record.get("knowledge_run") and record.get("knowledge_status") in {"pending", "not_required"}:
        record["knowledge_status"] = status


def ingest(source, *, repo=REPO, reuse_parse=None, allow_paddleocr=False, refresh_parse=False, knowledge_required=None):
    started = time.perf_counter()
    repo = Path(repo).resolve()
    supplied = Path(source).expanduser()
    if any(p.is_symlink() for p in (supplied.absolute(), *supplied.absolute().parents)):
        raise ValueError("source must be a real file, not a symlink")
    source = supplied.resolve(strict=True)
    if source.is_relative_to((repo/"private").resolve()):
        raise ValueError("private source cannot be imported into academic")
    if not source.is_file() or source.suffix.lower() not in SUPPORTED or source.stat().st_size == 0:
        raise ValueError("Source-first accepts nonempty PDF, Markdown or UTF-8 text")
    h = digest(source)
    source_id = "S-" + h[:24]
    with source_lock(repo, source_id):
        path = receipt_path(repo, source_id)
        if path.exists():
            record = load_receipt(repo, source_id)
            if record["content_sha256"] != h:
                raise ValueError("source ID hash collision")
        else:
            record = {"schema": SCHEMA, "source_id": source_id, "source_version": 1,
                "identity_scope": "immutable_byte_edition", "content_sha256": h,
                "original": f"academic/raw/sources/{source_id}/original{source.suffix.lower()}",
                "title": source.stem, "title_basis": "filename_unverified", "created_at": now(),
                "source_status": "receiving", "knowledge_status": "pending", "parses": [], "origins": []}
        configure_knowledge(record, knowledge_required)
        record["origins"].append({"path": str(source), "received_at": now(),
                                  "knowledge_policy": record.get("knowledge_policy", "optional")})
        requested_bundle = parse_request(record, reuse_parse, refresh_parse)
        write_receipt(repo, record)
        copy_original(source, managed(repo, record["original"]), h)
        if record["source_status"] == "receiving":
            record["source_status"] = "received"
        write_receipt(repo, record)
        # A completed bound parse is reusable across transactions without contacting providers.
        existing = selected_parse(record)
        if existing and not reuse_parse and not refresh_parse:
            try:
                publish_index(repo, record)
            except Exception as exc:
                record["error"] = {"phase":"index", "message":str(exc)[:700]}
                write_receipt(repo, record)
        else:
            record = finish(repo, record, reuse_parse=requested_bundle, allow_paddleocr=allow_paddleocr)
        result = public_receipt(record)
        result["elapsed_sec"] = time.perf_counter()-started
        return result


def resume(source_id, *, repo=REPO, reuse_parse=None, allow_paddleocr=False, refresh_parse=False, knowledge_required=None):
    with source_lock(repo, source_id):
        record = load_receipt(repo, source_id)
        configure_knowledge(record, knowledge_required)
        requested_bundle = parse_request(record, reuse_parse, refresh_parse)
        write_receipt(repo, record)
        if record["source_status"] == "receiving" and not managed(repo, record["original"]).is_file():
            raise ValueError("original reception was interrupted; re-ingest the original file to recover this source")
        validate_binding(repo, record)
        if selected_parse(record) and not reuse_parse and not refresh_parse:
            try:
                publish_index(repo, record)
            except Exception as exc:
                record["error"] = {"phase":"index", "message":str(exc)[:700]}
                write_receipt(repo, record)
        else:
            record = finish(repo, record, reuse_parse=requested_bundle, allow_paddleocr=allow_paddleocr)
        return public_receipt(record)


def public_receipt(record):
    p = selected_parse(record)
    limitations = list(p.get("limitations", [])) if p else ["原件尚无可检索正文；接收状态以 source_status 为准。"]
    skipped = (record.get("index") or {}).get("skipped_long_lines", [])
    if skipped:
        limitations.append(f"索引未覆盖 {len(skipped)} 个超长行；行号见 index.skipped_long_lines，原件保留。")
    return {"profile": "source-first", "schema": SCHEMA, "source_id": record["source_id"],
        "status": record["source_status"], "source_status": record["source_status"],
        "source_path": record["original"], "parse_path": p["text_path"] if p else None,
        "parse_id": p["parse_id"] if p else None, "knowledge_status": record.get("knowledge_status", "pending"),
        "knowledge_run": record.get("knowledge_run"),
        "knowledge_policy": record.get("knowledge_policy", "optional"),
        "limitations": limitations,
        "index": record.get("index"), "error": record.get("error"), "legacy_completed": False,
        "semantic_llm_calls": 0, "origin_count": len(record.get("origins", []))}


def inspect_source(source_id, *, repo=REPO):
    """Read-only live readiness; a saved receipt does not hide missing projections."""
    record = load_receipt(repo, source_id)
    result = public_receipt(record)
    if result["status"] == "source_ready":
        try:
            parsed = selected_parse(record)
            validate_binding(repo, record, parsed)
            conn = connect_index(repo)
            if conn is None:
                raise ValueError("index missing; resume source to rebuild")
            try:
                indexed = conn.execute("SELECT parse_id,text_sha256 FROM sources WHERE source_id=?", (source_id,)).fetchone()
                count = conn.execute("SELECT COUNT(*) FROM chunks WHERE source_id=?", (source_id,)).fetchone()[0]
                if not parsed or not indexed or tuple(indexed) != (parsed["parse_id"], parsed["text_sha256"]) or not count:
                    raise ValueError("index stale/incomplete; resume source to rebuild")
            finally:
                conn.close()
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            result.update(recorded_status="source_ready", status="unavailable", source_status="unavailable",
                          error={"phase": "inspect", "message": str(exc)})
    return result


def ingest_many(files, **kwargs):
    """One source failing never prevents the next independent source from running."""
    results = []
    for name in files:
        try:
            results.append(ingest(name, **kwargs))
        except Exception as exc:
            results.append({"status": "failed", "source": str(name), "error": str(exc)[:700]})
    return {"profile": "source-first", "status": "source_ready" if results and all(
        r["status"] == "source_ready" and not r.get("error") for r in results) else "partial", "files": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("files", nargs="+")
    p.add_argument("--knowledge", choices=("skip", "none"), default="skip")
    p.add_argument("--reuse-parse", type=Path)
    p.add_argument("--allow-paddleocr", action="store_true")
    p.add_argument("--refresh-parse", action="store_true")
    p = sub.add_parser("resume")
    p.add_argument("source_id")
    p.add_argument("--knowledge", choices=("skip", "none"), default="skip")
    p.add_argument("--reuse-parse", type=Path)
    p.add_argument("--allow-paddleocr", action="store_true")
    p.add_argument("--refresh-parse", action="store_true")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--topk", type=int, default=5)
    p = sub.add_parser("show")
    p.add_argument("source_id")
    args = parser.parse_args(argv)
    if args.command == "ingest":
        if args.reuse_parse and len(args.files)!=1:
            parser.error("--reuse-parse requires one source")
        output = ingest_many(args.files, reuse_parse=args.reuse_parse, allow_paddleocr=args.allow_paddleocr,
                             refresh_parse=args.refresh_parse, knowledge_required=False if args.knowledge == "none" else None)
    else:
        try:
            if args.command == "resume":
                output = resume(args.source_id, reuse_parse=args.reuse_parse,
                                allow_paddleocr=args.allow_paddleocr, refresh_parse=args.refresh_parse,
                                knowledge_required=False if args.knowledge == "none" else None)
            elif args.command == "search":
                output = search(args.query, topk=args.topk)
            else:
                output = inspect_source(args.source_id)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            output = {"status": "failed", "error": str(exc)[:700]}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 1 if output.get("status") in {"partial", "failed", "received"} or output.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())

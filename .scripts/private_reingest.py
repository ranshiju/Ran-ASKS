#!/usr/bin/env python3
"""Private-only, hash-bound re-ingest pilot transaction.

The host Agent writes candidate Wiki and semantic-slot files in a private workspace.
This program owns validation, snapshots, graph update, rollback and idempotence checks.
Raw is immutable; public storage is never written.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import shutil
import io
from contextlib import redirect_stdout
import sys
import traceback
import uuid
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_lib as gl
import graph_ingest as gi
import ingest_check as ic
import wiki_locator as wl

SCHEMA = "private-reingest-v1"
TASK_SCHEMA = "agent-task-v1"
ALLOWED_TYPES = {
    "health": {"health-record", "health-knowledge"},
    "metaphysics": {"metaphysics-profile", "metaphysics-knowledge"},
}
FORBIDDEN_TEMPLATE_MARKERS = (
    "arXiv 物理分类研究方向",
    "hub_subtype: research-direction",
    "type: research-direction",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    return _sha(path.read_bytes())


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _safe_name(value: str) -> str:
    result = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-")
    if not result or result in {".", ".."}:
        raise ValueError(f"unsafe name: {value!r}")
    return result


def _private_root() -> Path:
    return gl.REPO.resolve() / "private"


def _batch_root() -> Path:
    root = _private_root() / "outputs" / "reingest"
    if not gl.private_graph_path(root):
        raise ValueError("reingest workspace escapes private storage")
    return root


def _page_rel(page: str) -> str:
    value = str(page or "").removesuffix(".md")
    path = Path(value)
    if (len(path.parts) < 4 or path.parts[0] != "private" or path.parts[1] != "wiki"
            or path.parts[2] not in ALLOWED_TYPES or ".." in path.parts):
        raise ValueError("page must be private/wiki/{health,metaphysics}/<page>.md")
    return value


def _page_file(page: str) -> Path:
    target = gl.REPO / (_page_rel(page) + ".md")
    if target.is_symlink() or not gl.private_graph_path(target) or not target.is_file():
        raise ValueError("page must be an existing physical private Wiki file")
    return target


def _validate_page(page: str) -> tuple[Path, dict, list[str]]:
    target = _page_file(page)
    fm = gl.read_frontmatter(target)
    domain = target.relative_to(gl.REPO).parts[2]
    errors = []
    if fm.get("type") not in ALLOWED_TYPES[domain]:
        errors.append(f"page type {fm.get('type')!r} is not allowed in {domain}")
    sources = gl.parse_list_field(fm, "sources")
    if not sources:
        errors.append("page has no Raw sources")
    normalized = []
    for source in sources:
        raw_path = str(source).split("#", 1)[0]
        raw_file = gl.REPO / raw_path
        if (not raw_file.is_file() or raw_file.is_symlink()
                or not gl.private_graph_path(raw_file)):
            errors.append(f"invalid private Raw source: {source}")
        else:
            normalized.append(raw_path)
    if errors:
        raise ValueError("; ".join(errors))
    return target, fm, normalized


def _workspace(batch: str, page: str) -> Path:
    root = _batch_root() / _safe_name(batch) / _safe_name(Path(_page_rel(page)).parts[2] + "-" + Path(_page_rel(page)).stem)
    if not gl.private_graph_path(root):
        raise ValueError("workspace escapes private storage")
    return root


def _find_transaction(transaction_id: str) -> tuple[Path, dict]:
    for state_path in sorted(_batch_root().glob("*/*/state.json")):
        if not state_path.is_file() or state_path.is_symlink():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if state.get("transaction_id") == transaction_id:
            if not gl.private_graph_path(state_path):
                raise ValueError("transaction state escapes private storage")
            return state_path.parent, state
    raise FileNotFoundError(f"transaction not found: {transaction_id}")


def _public_baseline() -> dict:
    repo = gl.REPO
    values = {}
    for relative in ("cross-domain/graph.db", "cross-domain/embeddings.db",
                     "cross-domain/abbreviation-todo.jsonl"):
        path = repo / relative
        if path.is_file() and not path.is_symlink():
            values[relative] = _file_sha(path)
    return values


def _source_hashes(sources: list[str]) -> dict:
    result = {}
    for source in sources:
        result[source] = _file_sha(gl.REPO / source)
    return result


def prepare(page: str, batch: str) -> dict:
    target, fm, sources = _validate_page(page)
    workspace = _workspace(batch, page)
    if (workspace / "state.json").exists():
        raise FileExistsError(f"transaction already exists: {workspace}")
    page_rel = target.relative_to(gl.REPO).as_posix().removesuffix(".md")
    transaction_id = datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    candidate = workspace / "candidate.md"
    semantic = workspace / "semantic.txt"
    state = {
        "schema": SCHEMA,
        "status": "prepared",
        "transaction_id": transaction_id,
        "batch": _safe_name(batch),
        "page": page_rel,
        "created_at": _now(),
        "original": {
            "wiki_sha256": _file_sha(target),
            "source_hashes": _source_hashes(sources),
            "sources": sources,
            "type": fm.get("type"),
            "source_type": fm.get("source_type"),
            "date": fm.get("date"),
            "status": fm.get("status"),
            "created": fm.get("created"),
        },
        "public_baseline": _public_baseline(),
        "candidate_sha256": None,
        "semantic_sha256": None,
        "validation": {},
        "backup": None,
        "receipt": None,
    }
    task = {
        "schema": TASK_SCHEMA,
        "status": "prepared",
        "kind": "private-reingest",
        "transaction_id": transaction_id,
        "inputs": [
            {"name": f"raw_source_{index}", "path": source, "required": True}
            for index, source in enumerate(sources, 1)
        ] + [
            {"name": "current_wiki", "path": target.relative_to(gl.REPO).as_posix(), "required": True},
            {"name": "private_schema", "path": "private/SCHEMA.md", "required": True},
        ],
        "outputs": [
            {"name": "candidate_wiki", "path": candidate.relative_to(gl.REPO).as_posix(), "required": True},
            {"name": "semantic_slots", "path": semantic.relative_to(gl.REPO).as_posix(), "required": True},
        ],
        "protocol": {
            "name": "private-reingest-agent-v1",
            "candidate_wiki": {
                "frontmatter": "preserve type, sources, source_type, date, status and created; update only title/confidence/updated when evidence supports it",
                "sections": ["Navigation", "Content"],
                "evidence": "retain private Raw footnotes with line locators; do not invent facts or merge unrelated records",
                "forbidden": list(FORBIDDEN_TEMPLATE_MARKERS),
            },
            "semantic_slots": {
                "format": "Chinese section headers; 三元组 lines use 主体|谓词|客体",
                "subject": "use 本文件 for page-subject triples",
                "predicates": sorted(gi.PRIVATE_NAV_PREDICATES),
                "forbidden": ["arXiv direction hubs", "research-direction", "catch-all research keywords"],
            },
        },
        "issues": [],
        "commands": {
            "check": f"python3 .scripts/private_reingest.py validate --txn {transaction_id}",
            "commit": f"python3 .scripts/private_reingest.py commit --txn {transaction_id}",
        },
        "context": {
            "page": page_rel,
            "batch": _safe_name(batch),
            "privacy": "all outputs must remain in this private workspace",
        },
    }
    workspace.mkdir(parents=True)
    _write_json(workspace / "state.json", state)
    _write_json(workspace / "agent-task.json", task)
    return {"status": "prepared", "transaction_id": transaction_id,
            "workspace": workspace.relative_to(gl.REPO).as_posix(),
            "agent_task": task}


def _validate_outputs(workspace: Path, state: dict) -> tuple[dict, list[str], list[str]]:
    candidate = workspace / "candidate.md"
    semantic = workspace / "semantic.txt"
    errors: list[str] = []
    warnings: list[str] = []
    if not candidate.is_file() or candidate.is_symlink() or not gl.private_graph_path(candidate):
        raise ValueError("candidate Wiki missing or outside private workspace")
    if not semantic.is_file() or semantic.is_symlink() or not gl.private_graph_path(semantic):
        raise ValueError("semantic slots missing or outside private workspace")
    text = candidate.read_text(encoding="utf-8")
    semantic_text = semantic.read_text(encoding="utf-8")
    if not text.strip() or not semantic_text.strip():
        raise ValueError("candidate Wiki and semantic slots must be non-empty")
    for marker in FORBIDDEN_TEMPLATE_MARKERS:
        if marker in text or marker in semantic_text:
            errors.append(f"forbidden arXiv template marker: {marker}")
    fm = gl.read_frontmatter(candidate)
    original = state["original"]
    for field in ("type", "source_type", "date", "status", "created"):
        if str(fm.get(field, "")) != str(original.get(field, "")):
            errors.append(f"frontmatter field {field} changed from {original.get(field)!r} to {fm.get(field)!r}")
    sources = gl.parse_list_field(fm, "sources")
    if sources != original["sources"]:
        errors.append("sources changed; reingest must preserve the exact Raw source set")
    domain = Path(state["page"]).parts[2]
    if fm.get("type") not in ALLOWED_TYPES[domain]:
        errors.append(f"candidate type {fm.get('type')!r} is invalid for {domain}")
    for field in ("title", "type", "sources", "source_type", "date", "status"):
        if fm.get(field) in (None, ""):
            errors.append(f"required frontmatter field missing: {field}")
    if not re.search(r"^## Navigation\s*$", text, re.M):
        errors.append("candidate missing ## Navigation")
    if not re.search(r"^## Content\s*$", text, re.M):
        errors.append("candidate missing ## Content")
    footnotes = re.findall(r"^\[\^([^\]]+)\]:\s*(\S+)", text, re.M)
    if not footnotes:
        errors.append("candidate has no Raw locator footnotes")
    for _name, locator in footnotes:
        raw_path = locator.split("#", 1)[0]
        if raw_path not in original["sources"]:
            errors.append(f"footnote source is outside transaction sources: {raw_path}")
        if ("#L" not in locator
                and not (Path(raw_path).suffix.lower() == ".pdf" and "#page-" in locator)):
            warnings.append(f"footnote lacks line locator: {locator}")
    rel_paths, basenames = ic.build_wiki_index()
    check_errors, check_warnings = ic.check_file(candidate, rel_paths, basenames)
    errors.extend(check_errors)
    warnings.extend(check_warnings)
    try:
        locator_errors = wl.validate_wiki_page(candidate)
        errors.extend(f"wiki-locator: {item}" for item in locator_errors)
    except Exception as exc:
        errors.append(f"wiki-locator validation failed: {exc}")
    try:
        triples, _keywords, _main, _corresponding, _cross, _directions = gi.parse_semantic_text(
            semantic_text, state["page"], fm
        )
    except Exception as exc:
        raise ValueError(f"semantic slot parsing failed: {exc}") from exc
    if not triples:
        errors.append("semantic slots contain no valid private triples")
    allowed = set(gi.PRIVATE_NAV_PREDICATES)
    for triple in triples:
        if triple.get("predicate") not in allowed:
            errors.append(f"invalid private predicate: {triple.get('predicate')}")
        for endpoint in (triple.get("subject"), triple.get("object")):
            if isinstance(endpoint, str) and re.match(r"^(academic|admin|teaching|business|cross-domain)/", endpoint):
                errors.append(f"semantic endpoint crosses public domain: {endpoint}")
    result = {
        "candidate_sha256": _file_sha(candidate),
        "semantic_sha256": _file_sha(semantic),
        "triple_count": len(triples),
        "error_count": len(errors),
        "warning_count": len(warnings),
    }
    return result, errors, warnings


def validate(transaction_id: str) -> dict:
    workspace, state = _find_transaction(transaction_id)
    if state.get("status") == "completed":
        raise ValueError("completed transaction cannot be revalidated; create a new transaction")
    result, errors, warnings = _validate_outputs(workspace, state)
    state["validation"] = result | {"errors": errors, "warnings": warnings}
    if errors:
        state["status"] = "prepared"
    else:
        state["status"] = "ready_to_commit"
        state["candidate_sha256"] = result["candidate_sha256"]
        state["semantic_sha256"] = result["semantic_sha256"]
    _write_json(workspace / "state.json", state)
    task_path = workspace / "agent-task.json"
    if task_path.is_file():
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task["issues"] = errors
        task["status"] = "prepared" if errors else "consumed"
        _write_json(task_path, task)
    if errors:
        raise ValueError("validation failed: " + "; ".join(errors))
    return {"status": "ready_to_commit", "transaction_id": transaction_id,
            "validation": {k: v for k, v in state["validation"].items() if k not in ("errors", "warnings")}}

def _append_log(log: Path, page: str, transaction_id: str) -> None:
    previous = log.read_text(encoding="utf-8") if log.exists() else "# private operation log\n"
    entry = (f"\n## [{datetime.date.today()}] update | {Path(page).stem} "
             f"(private re-ingest {transaction_id})\n"
             "- Rebuilt Wiki and private graph contributions from the bound Raw source; Raw unchanged.\n")
    log.write_text(previous + entry, encoding="utf-8")


def _graph_signature(conn, page: str) -> list:
    values = []
    for row in conn.execute(
        "SELECT subject,predicate,object,confidence,source,is_sr FROM edges "
        "WHERE subject=? OR object=? ORDER BY subject,predicate,object,confidence,source,is_sr",
        (page, page),
    ):
        values.append(tuple(row))
    for table, query in (
        ("node_origins", "SELECT node_path,origin_page,source FROM node_origins WHERE node_path=? OR origin_page=?"),
        ("edge_origins", "SELECT e.subject,e.predicate,e.object,eo.origin_page,eo.source "
                           "FROM edge_origins eo JOIN edges e ON e.id=eo.edge_id "
                           "WHERE eo.origin_page=?"),
    ):
        try:
            rows = conn.execute(query, (page, page)).fetchall()
        except Exception:
            continue
        values.extend((table, *tuple(row)) for row in rows)
    return values


def _graph_validation(target: Path) -> tuple[list[str], list[str]]:
    rel_paths, basenames = ic.build_wiki_index()
    errors, warnings = ic.check_file(target, rel_paths, basenames)
    conn = gl.connect(gl.REPO / "private" / "graph.db", read_only=True)
    try:
        graph_errors, graph_warnings = ic.graph_checks(target, conn)
        errors.extend(graph_errors)
        warnings.extend(graph_warnings)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("private graph foreign-key check failed")
        rel = target.resolve().relative_to(gl.REPO).with_suffix("").as_posix()
        if conn.execute("SELECT 1 FROM edges WHERE subject=? AND object=?", (rel, rel)).fetchone():
            errors.append("private page has a self-edge")
    finally:
        conn.close()
    return errors, warnings


def commit(transaction_id: str) -> dict:
    workspace, state = _find_transaction(transaction_id)
    if state.get("status") != "ready_to_commit":
        raise ValueError("transaction is not ready_to_commit; run validate first")
    result, errors, _warnings = _validate_outputs(workspace, state)
    if errors:
        state["status"] = "prepared"
        state["validation"] = result | {"errors": errors}
        _write_json(workspace / "state.json", state)
        raise ValueError("candidate changed and failed revalidation: " + "; ".join(errors))
    if (result["candidate_sha256"] != state.get("candidate_sha256")
            or result["semantic_sha256"] != state.get("semantic_sha256")):
        raise ValueError("candidate changed after validation; run validate again")
    target = _page_file(state["page"])
    current_wiki_hash = _file_sha(target)
    if current_wiki_hash != state["original"]["wiki_sha256"]:
        raise ValueError("live Wiki changed after prepare; create a new transaction")
    for source, expected in state["original"]["source_hashes"].items():
        if _file_sha(gl.REPO / source) != expected:
            raise ValueError(f"Raw source changed after prepare: {source}")
    if _public_baseline() != state.get("public_baseline"):
        raise ValueError("public storage changed after prepare; stop and review concurrency")
    private_db = gl.REPO / "private" / "graph.db"
    log = gl.REPO / "private" / "wiki" / "log.md"
    backup = workspace / ("backup-" + datetime.datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6])
    if not gl.private_graph_path(backup):
        raise ValueError("backup escapes private storage")
    backup.mkdir(parents=True)
    old_wiki = target.read_bytes()
    old_log = log.read_bytes() if log.exists() else None
    receipt = {"schema": SCHEMA, "status": "prepared", "transaction_id": transaction_id,
               "page": state["page"], "backup": backup.relative_to(gl.REPO).as_posix(),
               "created_at": _now()}
    receipt_path = backup / "receipt.json"
    _write_json(receipt_path, receipt)
    conn = None
    try:
        with gl.graph_writer_lock(private_db):
            source = gl.connect(private_db, read_only=True)
            try:
                gl.backup_graph(source, backup / "graph.db")
            finally:
                source.close()
            (backup / "wiki.md").write_bytes(old_wiki)
            if old_log is not None:
                (backup / "log.md").write_bytes(old_log)
            temporary = target.with_suffix(".md.tmp")
            temporary.write_bytes((workspace / "candidate.md").read_bytes())
            temporary.replace(target)
            args = Namespace(
                page=state["page"], db=str(private_db), clean=True,
                semantic=str(workspace / "semantic.txt"), citations=None,
                triples=None, triples_json=None, knowledge_ir=None,
                knowledge_ir_out=None, graph_plan_out=None, page_file=None,
                raw_source_override=None, raw_relationship_json=None,
            )
            with redirect_stdout(io.StringIO()):
                gi._cmd_ingest_locked(args)
            errors, warnings = _graph_validation(target)
            if errors:
                raise ValueError("post-commit validation failed: " + "; ".join(errors))
            _append_log(log, state["page"], transaction_id)
        receipt["status"] = "completed"
        receipt["completed_at"] = _now()
        receipt["warnings"] = warnings
        _write_json(receipt_path, receipt)
        state["status"] = "completed"
        state["backup"] = backup.relative_to(gl.REPO).as_posix()
        state["receipt"] = receipt_path.relative_to(gl.REPO).as_posix()
        state["validation"] = result | {"errors": [], "warnings": warnings}
        _write_json(workspace / "state.json", state)
        return {"status": "completed", "transaction_id": transaction_id,
                "page": state["page"], "receipt": state["receipt"],
                "warnings": warnings, "raw_changed": False}
    except Exception as exc:
        try:
            with gl.graph_writer_lock(private_db):
                gl.restore_graph(backup / "graph.db", private_db)
                target.write_bytes(old_wiki)
                if old_log is None:
                    log.unlink(missing_ok=True)
                else:
                    log.write_bytes(old_log)
        except Exception:
            receipt["rollback_error"] = traceback.format_exc()
        receipt["status"] = "rolled_back"
        receipt["error"] = str(exc)
        receipt["failed_at"] = _now()
        _write_receipt_safe(receipt_path, receipt)
        state["status"] = "failed"
        state["error"] = str(exc)
        state["backup"] = backup.relative_to(gl.REPO).as_posix()
        state["receipt"] = receipt_path.relative_to(gl.REPO).as_posix()
        _write_json(workspace / "state.json", state)
        raise


def _write_receipt_safe(path: Path, receipt: dict) -> None:
    try:
        _write_json(path, receipt)
    except Exception:
        pass


def verify(transaction_id: str) -> dict:
    workspace, state = _find_transaction(transaction_id)
    if state.get("status") != "completed":
        raise ValueError("only completed transactions can be verified")
    target = _page_file(state["page"])
    candidate = workspace / "candidate.md"
    semantic = workspace / "semantic.txt"
    errors: list[str] = []
    warnings: list[str] = []
    if _file_sha(target) != _file_sha(candidate):
        errors.append("live Wiki does not match committed candidate")
    for source, expected in state["original"]["source_hashes"].items():
        if _file_sha(gl.REPO / source) != expected:
            errors.append(f"Raw source changed after commit: {source}")
    if _public_baseline() != state.get("public_baseline"):
        errors.append("public storage changed during transaction")
    validation_errors, validation_warnings = _graph_validation(target)
    errors.extend(validation_errors)
    warnings.extend(validation_warnings)
    private_db = gl.REPO / "private" / "graph.db"
    copy = workspace / "verify-idempotence.db"
    if copy.exists():
        copy.unlink()
    source = gl.connect(private_db, read_only=True)
    try:
        gl.backup_graph(source, copy)
    finally:
        source.close()
    try:
        before_conn = gl.connect(copy, read_only=True)
        try:
            before = _graph_signature(before_conn, state["page"])
        finally:
            before_conn.close()
        args = Namespace(
            page=state["page"], db=str(copy), clean=True,
            semantic=str(semantic), citations=None, triples=None,
            triples_json=None, knowledge_ir=None, knowledge_ir_out=None,
            graph_plan_out=None, page_file=None, raw_source_override=None,
            raw_relationship_json=None,
        )
        with gl.graph_writer_lock(copy), redirect_stdout(io.StringIO()):
            gi._cmd_ingest_locked(args)
        after_conn = gl.connect(copy, read_only=True)
        try:
            after = _graph_signature(after_conn, state["page"])
        finally:
            after_conn.close()
        idempotent = before == after
        if not idempotent:
            errors.append("second ingest changed page graph signature")
    finally:
        if copy.exists():
            copy.unlink()
    report = {
        "status": "verified" if not errors else "failed",
        "transaction_id": transaction_id,
        "page": state["page"],
        "idempotent": idempotent,
        "public_unchanged": _public_baseline() == state.get("public_baseline"),
        "raw_unchanged": all(_file_sha(gl.REPO / s) == h for s, h in state["original"]["source_hashes"].items()),
        "errors": errors,
        "warnings": warnings,
    }
    _write_json(workspace / "verification.json", report)
    if errors:
        raise ValueError("verification failed: " + "; ".join(errors))
    return report


def status(transaction_id: str) -> dict:
    workspace, state = _find_transaction(transaction_id)
    return {"workspace": workspace.relative_to(gl.REPO).as_posix(), **state}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--page", required=True)
    prepare_parser.add_argument("--batch", required=True)
    for name in ("validate", "commit", "verify", "status"):
        item = sub.add_parser(name)
        item.add_argument("--txn", required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.page, args.batch)
        elif args.command == "validate":
            result = validate(args.txn)
        elif args.command == "commit":
            result = commit(args.txn)
        elif args.command == "verify":
            result = verify(args.txn)
        else:
            result = status(args.txn)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

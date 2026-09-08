#!/usr/bin/env python3
"""Atomic ingestion for structured facts from inbox/facts-pending.md."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".scripts"))
import agent_task

PROTOCOL = "user-assertions-transaction-v1"
PROPOSAL_SCHEMA = "user-assertions-proposal-v1"
FACT_RE = re.compile(
    r"^- \[(?P<date>\d{4}-\d{2}-\d{2})\] "
    r"\*\*(?P<assertion>.+?)\*\*\s*"
    r"\{:\s*#(?P<fact_id>fact-[A-Za-z0-9_.:-]+)\s*\}\s*$"
)
WIKI_PATH_RE = re.compile(r"^(academic|admin|teaching|business)/wiki/.+\.md$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes()) if path.is_file() else ""


def _write_json(path: Path, value: dict) -> None:
    _atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def parse_fact_entries(text: str) -> list[dict]:
    entries = []
    seen = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        match = FACT_RE.fullmatch(line.strip())
        if not match:
            continue
        fact = match.groupdict()
        if fact["fact_id"] in seen:
            raise ValueError(f"duplicate fact id: {fact['fact_id']}")
        seen.add(fact["fact_id"])
        fact.update({"line": line, "line_number": line_number})
        entries.append(fact)
    return entries


def _workspace(repo: Path, transaction_id: str) -> Path:
    return repo / "temp" / "user-assertions" / transaction_id


def prepare_transaction(path: Path | None = None, repo: Path | None = None) -> dict:
    repo = (repo or REPO).resolve()
    pending = (path or repo / "inbox" / "facts-pending.md").resolve()
    expected = (repo / "inbox" / "facts-pending.md").resolve()
    if pending != expected:
        raise ValueError("user assertions input must be inbox/facts-pending.md")
    pending_bytes = pending.read_bytes()
    pending_text = pending_bytes.decode("utf-8")
    facts = parse_fact_entries(pending_text)
    dated_lines = [
        line for line in pending_text.splitlines()
        if re.match(r"^- \[\d{4}-\d{2}-\d{2}\]", line.strip())
    ]
    if len(facts) != len(dated_lines):
        raise ValueError(
            "every dated assertion must use `- [YYYY-MM-DD] **text** {: #fact-id}`"
        )
    if not facts:
        return {"ok": True, "status": "no_action", "fact_entries": 0}
    digest = _sha256(pending_bytes)[:16]
    transaction_id = f"user-assertions-{digest}"
    workspace = _workspace(repo, transaction_id)
    manifest_path = workspace / "manifest.json"
    proposal_path = workspace / "proposal.json"
    raw_path = repo / "cross-domain" / "raw" / "facts" / "user-assertions.md"
    manifest = {
        "schema": PROTOCOL,
        "transaction_id": transaction_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "pending_path": "inbox/facts-pending.md",
        "pending_sha256": _sha256(pending_bytes),
        "raw_path": "cross-domain/raw/facts/user-assertions.md",
        "raw_sha256": _file_sha256(raw_path),
        "facts": facts,
        "proposal_schema": {
            "schema": PROPOSAL_SCHEMA,
            "transaction_id": transaction_id,
            "handled_fact_ids": [fact["fact_id"] for fact in facts],
            "wiki_updates": [{
                "page": "<domain>/wiki/<managed-page>.md",
                "before_sha256": "sha256 of current page, or empty for a new page",
                "content": "complete updated Wiki Markdown with anchored Raw source citations",
            }],
            "relations": [{
                "page": "same Wiki page path",
                "fact_id": facts[0]["fact_id"],
                "subject": "canonical subject",
                "predicate": "short relation",
                "object": "canonical object or complete proposition",
                "confidence": "[可追溯]",
            }],
        },
    }
    if manifest_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("pending_sha256") != manifest["pending_sha256"]:
            raise ValueError("existing transaction hash mismatch")
    else:
        _write_json(manifest_path, manifest)
    task = agent_task.make_task(
        kind="ingest_user_assertions",
        transaction_id=transaction_id,
        inputs=[{
            "name": "assertion_manifest",
            "path": str(manifest_path.relative_to(repo)),
            "role": "immutable_pending_snapshot_and_output_contract",
            "read": "full",
        }],
        outputs=[{
            "name": "proposal",
            "path": str(proposal_path.relative_to(repo)),
            "format": PROPOSAL_SCHEMA,
        }],
        protocol={
            "name": PROPOSAL_SCHEMA,
            "schema_source": str(manifest_path.relative_to(repo)),
            "validator": "ingest_user_assertions._validate_proposal",
        },
        commands={
            "commit": f"python3 .scripts/ingest_user_assertions.py --apply {transaction_id}",
        },
        context={"fact_entries": len(facts)},
    )
    return {
        "file": pending.name,
        "type": "user-assertions",
        "ok": False,
        "status": "prepared",
        "agent_task": task,
        "transaction_id": transaction_id,
        "fact_entries": len(facts),
        "write_to": str(proposal_path.relative_to(repo)),
        "manifest_path": str(manifest_path.relative_to(repo)),
        "retryable": False,
        "next_action": "complete_agent_task",
        "apply_command": (
            f"python3 .scripts/ingest_user_assertions.py --apply {transaction_id}"
        ),
    }


def _load_apply_inputs(repo: Path, transaction_id: str) -> tuple[Path, dict, dict]:
    if not re.fullmatch(r"user-assertions-[a-f0-9]{16}", transaction_id):
        raise ValueError("invalid user assertion transaction id")
    workspace = _workspace(repo, transaction_id)
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    proposal = json.loads((workspace / "proposal.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != PROTOCOL or manifest.get("transaction_id") != transaction_id:
        raise ValueError("invalid user assertion manifest")
    if proposal.get("schema") != PROPOSAL_SCHEMA or proposal.get("transaction_id") != transaction_id:
        raise ValueError("invalid user assertion proposal")
    return workspace, manifest, proposal


def _managed_wiki_path(repo: Path, value: str) -> Path:
    relative = str(value or "").strip()
    if not WIKI_PATH_RE.fullmatch(relative) or ".." in Path(relative).parts:
        raise ValueError(f"unmanaged wiki path: {relative}")
    target = (repo / relative).resolve()
    if not target.is_relative_to(repo):
        raise ValueError(f"wiki path escapes repository: {relative}")
    return target


def _validate_proposal(repo: Path, manifest: dict, proposal: dict) -> tuple[list[dict], list[dict]]:
    expected_ids = [fact["fact_id"] for fact in manifest["facts"]]
    handled = proposal.get("handled_fact_ids")
    if handled != expected_ids:
        raise ValueError("handled_fact_ids must exactly match manifest order")
    updates = proposal.get("wiki_updates")
    relations = proposal.get("relations")
    if not isinstance(updates, list) or not updates:
        raise ValueError("wiki_updates must be a non-empty list")
    if not isinstance(relations, list) or not relations:
        raise ValueError("relations must be a non-empty list")
    pages = set()
    normalized_updates = []
    for update in updates:
        if not isinstance(update, dict):
            raise ValueError("wiki update must be an object")
        target = _managed_wiki_path(repo, update.get("page", ""))
        relative = str(target.relative_to(repo))
        if relative in pages:
            raise ValueError(f"duplicate wiki update: {relative}")
        pages.add(relative)
        content = update.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"empty wiki content: {relative}")
        if _file_sha256(target) != str(update.get("before_sha256") or ""):
            raise ValueError(f"wiki changed after proposal: {relative}")
        try:
            frontmatter = yaml.safe_load(content.split("---", 2)[1]) or {}
        except (IndexError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid Wiki frontmatter: {relative}: {exc}") from exc
        if not isinstance(frontmatter, dict) or not frontmatter.get("title"):
            raise ValueError(f"Wiki frontmatter lacks title: {relative}")
        normalized_updates.append({**update, "page": relative, "target": target})
    facts_with_relations = set()
    normalized_relations = []
    for relation in relations:
        if not isinstance(relation, dict):
            raise ValueError("relation must be an object")
        page = str(relation.get("page") or "")
        fact_id = str(relation.get("fact_id") or "")
        if page not in pages:
            raise ValueError(f"relation page has no Wiki update: {page}")
        if fact_id not in expected_ids:
            raise ValueError(f"relation references unknown fact: {fact_id}")
        for field in ("subject", "predicate", "object"):
            if not str(relation.get(field) or "").strip():
                raise ValueError(f"relation {field} is required for {fact_id}")
        locator = f"cross-domain/raw/facts/user-assertions.md#{fact_id}"
        content = next(item["content"] for item in normalized_updates if item["page"] == page)
        if locator not in content:
            raise ValueError(f"Wiki update lacks exact Raw locator: {page} -> {fact_id}")
        facts_with_relations.add(fact_id)
        normalized_relations.append({**relation, "source": locator})
    missing = [fact_id for fact_id in expected_ids if fact_id not in facts_with_relations]
    if missing:
        raise ValueError("facts without graph relations: " + ", ".join(missing))
    return normalized_updates, normalized_relations


def _run_command(command: list[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=repo, text=True, capture_output=True)


def _last_json_object(text: str) -> dict:
    """Return the last complete JSON object from mixed or pretty stdout."""
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\{", text):
        try:
            value, length = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((match.start() + length, match.start(), value))
    if not candidates:
        raise ValueError("subprocess output contains no JSON object")
    return max(candidates, key=lambda item: (item[0], -item[1]))[2]


def _restore(path: Path, existed: bool, data: bytes) -> None:
    if existed:
        _atomic_write(path, data)
    elif path.exists():
        os.remove(path)


def apply_transaction(transaction_id: str, repo: Path | None = None) -> dict:
    repo = (repo or REPO).resolve()
    workspace, manifest, proposal = _load_apply_inputs(repo, transaction_id)
    pending = repo / manifest["pending_path"]
    raw = repo / manifest["raw_path"]
    if _file_sha256(pending) != manifest["pending_sha256"]:
        raise ValueError("facts-pending.md changed after transaction preparation")
    if _file_sha256(raw) != manifest["raw_sha256"]:
        raise ValueError("user-assertions.md changed after transaction preparation")
    updates, relations = _validate_proposal(repo, manifest, proposal)
    original_pending = pending.read_bytes()
    raw_existed, original_raw = raw.exists(), raw.read_bytes() if raw.exists() else b""
    wiki_originals = {
        update["page"]: (update["target"].exists(), update["target"].read_bytes()
                         if update["target"].exists() else b"")
        for update in updates
    }
    graph = repo / "cross-domain" / "graph.db"
    if not graph.is_file():
        raise ValueError("cross-domain/graph.db is missing")
    graph_before = workspace / "graph-before.db"
    graph_staged = workspace / "graph-staged.db"
    shutil.copy2(graph, graph_before)
    shutil.copy2(graph, graph_staged)
    fact_lines = [fact["line"] for fact in manifest["facts"]]
    raw_text = original_raw.decode("utf-8") if raw_existed else (
        "# 用户申明事实累积\n\n"
        "> source_type = `user-assertion`; confidence 默认 medium。\n\n---\n"
    )
    appended = raw_text.rstrip() + "\n\n" + "\n\n".join(fact_lines) + "\n"
    handled = set(proposal["handled_fact_ids"])
    pending_lines = []
    for line in original_pending.decode("utf-8").splitlines():
        match = FACT_RE.fullmatch(line.strip())
        if not match or match.group("fact_id") not in handled:
            pending_lines.append(line)
    staged_pending = "\n".join(pending_lines).rstrip() + "\n"
    receipt_path = workspace / "receipt.json"
    try:
        # Raw is exposed only inside the guarded transaction so staged graph locators resolve.
        _atomic_write(raw, appended.encode("utf-8"))
        relation_pages = {}
        for relation in relations:
            relation_pages.setdefault(relation["page"], []).append(relation)
        graph_reports = []
        for index, update in enumerate(updates):
            staged_wiki = workspace / f"wiki-{index}.md"
            semantic = workspace / f"semantic-{index}.txt"
            ir_path = workspace / f"knowledge-ir-{index}.json"
            plan_path = workspace / f"graph-plan-{index}.json"
            _atomic_write(staged_wiki, update["content"].encode("utf-8"))
            semantic_lines = ["三元组:"] + [
                f"{row['subject']} | {row['predicate']} | {row['object']}"
                for row in relation_pages.get(update["page"], [])
            ]
            _atomic_write(semantic, ("\n".join(semantic_lines) + "\n").encode("utf-8"))
            page = update["page"].removesuffix(".md")
            command = [
                sys.executable, ".scripts/graph_ingest.py", "--db", str(graph_staged),
                "ingest", "--page", page, "--page-file", str(staged_wiki),
                "--semantic", str(semantic), "--transaction-id", transaction_id,
                "--knowledge-ir-out", str(ir_path), "--graph-plan-out", str(plan_path),
                "--clean",
            ]
            completed = _run_command(command, repo)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"graph staging failed for {page}: {completed.stderr or completed.stdout}"
                )
            try:
                graph_reports.append(_last_json_object(completed.stdout))
            except ValueError:
                graph_reports.append({"status": "completed", "output": completed.stdout[-500:]})
        for update in updates:
            _atomic_write(update["target"], update["content"].encode("utf-8"))
        graph_swap = graph.with_name(f".{graph.name}.{transaction_id}.tmp")
        shutil.copy2(graph_staged, graph_swap)
        os.replace(graph_swap, graph)
        validations = []
        for update in updates:
            check = _run_command([
                sys.executable, ".scripts/ingest_check.py", update["page"], "--graph",
            ], repo)
            validations.append({
                "page": update["page"], "returncode": check.returncode,
                "output": (check.stdout or check.stderr)[-1000:],
            })
            if check.returncode != 0:
                raise RuntimeError(f"ingest_check failed for {update['page']}: {check.stdout or check.stderr}")
        _atomic_write(pending, staged_pending.encode("utf-8"))
        receipt = {
            "schema": PROTOCOL,
            "status": "completed",
            "transaction_id": transaction_id,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "fact_ids": proposal["handled_fact_ids"],
            "raw_path": manifest["raw_path"],
            "wiki_pages": [update["page"] for update in updates],
            "graph_reports": graph_reports,
            "validations": validations,
            "pending_sha256_after": _file_sha256(pending),
        }
        _write_json(receipt_path, receipt)
        return {**receipt, "ok": True, "receipt_path": str(receipt_path.relative_to(repo))}
    except Exception as exc:
        _restore(raw, raw_existed, original_raw)
        for update in updates:
            existed, data = wiki_originals[update["page"]]
            _restore(update["target"], existed, data)
        _atomic_write(graph, graph_before.read_bytes())
        _atomic_write(pending, original_pending)
        receipt = {
            "schema": PROTOCOL,
            "status": "failed",
            "transaction_id": transaction_id,
            "failed_at": datetime.now().isoformat(timespec="seconds"),
            "rolled_back": True,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }
        _write_json(receipt_path, receipt)
        return {**receipt, "ok": False, "receipt_path": str(receipt_path.relative_to(repo))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--apply", metavar="TRANSACTION_ID")
    args = parser.parse_args(argv)
    try:
        result = prepare_transaction() if args.prepare else apply_transaction(args.apply)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"ok": False, "status": "validation_error", "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else (2 if result.get("status") == "agent_required" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

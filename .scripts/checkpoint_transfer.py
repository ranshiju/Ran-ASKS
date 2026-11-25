"""Sealed incremental restic objects over an asynchronous file-sync directory.

No live repository in Drive. Append-only packs; no pruning, overwrite or merge.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

import workspace_checkpoint as cp

SCHEMA = "wikigraph-restic-pack-v1"
OBJECT = re.compile(r"(?:config|(?:keys|index|snapshots)/[0-9a-f]{64}|data/[0-9a-f]{2}/[0-9a-f]{64})$")


def local_repository(store, directory):
    repository = store.config["repository"]
    if re.match(r"^[a-z][a-z0-9+.-]*:", repository):
        raise ValueError("Transfer packs require a local restic repository outside the sync directory")
    root = cp.reject_link_components(Path(repository).absolute())
    directory = cp.reject_link_components(directory)
    cp.outside(root, directory)
    cp.outside(store.root, directory)
    return root


def objects(root):
    result = {}
    if not root.exists():
        return result
    for parent, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(parent) / name
            if p.is_symlink():
                raise ValueError("Repository contains a symlink")
        for name in files:
            p = Path(parent) / name
            rel = p.relative_to(root).as_posix()
            if rel.startswith("locks/"):
                raise ValueError("Repository is locked; finish restic operations before transfer")
            if not OBJECT.fullmatch(rel):
                raise ValueError(f"Unexpected repository object: {rel}")
            result[rel] = {"size": p.stat().st_size, "sha256": cp.digest(p)}
    return result


def read_state(store):
    path = store.root / "transfer.json"
    return json.loads(path.read_text()) if path.exists() else {"sequence": 0, "head": None, "objects": {}}


def chain(directory):
    result = []
    for p in sorted(directory.glob("*.ready.json")):
        if p.is_symlink():
            raise ValueError("Transfer receipt cannot be a symlink")
        doc = json.loads(p.read_text(encoding="utf-8"))
        if doc.get("schema") != SCHEMA or not isinstance(doc.get("sequence"), int):
            raise ValueError("Invalid transfer receipt")
        if not re.fullmatch(r"[0-9a-f]{64}", doc.get("archive_sha256", "")):
            raise ValueError("Invalid archive hash")
        expected = f"{doc['sequence']:06d}-{doc['archive_sha256'][:16]}"
        if p.name != expected + ".ready.json" or doc.get("archive") != expected + ".tar":
            raise ValueError("Invalid archive name")
        if not isinstance(doc.get("objects"), dict):
            raise ValueError("Invalid object catalog")
        for name, entry in doc["objects"].items():
            if not OBJECT.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
                raise ValueError("Invalid object path/hash")
            if not isinstance(entry.get("size"), int) or entry["size"] < 0:
                raise ValueError("Invalid object size")
        result.append(doc)
    parent = None
    for i, doc in enumerate(result, 1):
        if doc["sequence"] != i or doc["parent"] != parent:
            raise ValueError("Transfer chain incomplete or branched; wait for sync or resolve both writers")
        parent = doc["archive_sha256"]
    return result


def publish(source, target):
    """Exclusive atomic publication on the normal same-volume Drive layout."""
    if target.exists():
        raise FileExistsError(target)
    try:
        os.link(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Cross-volume sync folders use an incomplete name; ready receipt is last.
        with tempfile.NamedTemporaryFile(prefix=".incomplete-", dir=target.parent, delete=False) as f:
            partial = Path(f.name)
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, f, 1024 * 1024)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(partial, target)
        finally:
            partial.unlink()


def export_pack(store, directory):
    root = local_repository(store, directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with store.lock():
        store.run("check")
        previous = read_state(store)
        existing = chain(directory)
        head = existing[-1]["archive_sha256"] if existing else None
        if head != previous["head"]:
            raise ValueError("Sync directory has a different head; import completed packs before exporting")
        catalog = objects(root)
        if any(catalog.get(k) != v for k, v in previous["objects"].items()):
            raise ValueError("Repository objects were removed/changed; pruning requires a new full transfer chain")
        additions = {k: v for k, v in catalog.items() if k not in previous["objects"]}
        if not additions:
            return {"unchanged": True, "head": head}
        repo_id = json.loads(store.run("cat", "config"))["id"]
        if existing and existing[-1]["repository_id"] != repo_id:
            raise ValueError("Transfer directory belongs to another repository")
        with tempfile.TemporaryDirectory(prefix="pack-", dir=store.root) as tmp:
            archive = Path(tmp) / "objects.tar"
            with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tar:
                for name in sorted(additions):
                    tar.add(root / name, arcname=name, recursive=False)
            if objects(root) != catalog:
                raise ValueError("Repository changed while sealing the transfer pack")
            sha = cp.digest(archive)
            seq = previous["sequence"] + 1
            stem = f"{seq:06d}-{sha[:16]}"
            doc = {"schema": SCHEMA, "repository_id": repo_id,
                   "workspace_id": store.config["workspace_id"], "sequence": seq,
                   "parent": head, "archive": stem + ".tar", "archive_sha256": sha,
                   "archive_bytes": archive.stat().st_size, "objects": additions}
            receipt = Path(tmp) / "ready.json"
            cp.write_json(receipt, doc)
            publish(archive, directory / doc["archive"])
            publish(receipt, directory / (stem + ".ready.json"))
            cp.write_json(store.root / "transfer.json", {"sequence": seq, "head": sha,
                                                         "objects": catalog, "repository_id": repo_id})
            return {"directory": str(directory), "archive": doc["archive"],
                    "bytes": doc["archive_bytes"], "new_objects": len(additions),
                    "sequence": seq, "sync_status": "published locally; Drive upload/download confirmation required"}


def import_packs(store, directory):
    root = local_repository(store, directory)
    with store.lock():
        docs = chain(directory)
        if not docs:
            raise ValueError("No complete transfer receipts; wait for Drive sync")
        state = read_state(store)
        if state["sequence"] and (len(docs) < state["sequence"] or
                docs[state["sequence"] - 1]["archive_sha256"] != state["head"]):
            raise ValueError("Local and synced transfer histories diverge")
        imported = []
        for doc in docs[state["sequence"]:]:
            if doc["workspace_id"] != store.config["workspace_id"]:
                raise ValueError("Workspace identity differs")
            if state.get("repository_id") not in (None, doc["repository_id"]):
                raise ValueError("Repository identity differs")
            archive = directory / doc["archive"]
            if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != doc["archive_bytes"]:
                raise ValueError("Archive incomplete; wait for Drive sync")
            if cp.digest(archive) != doc["archive_sha256"]:
                raise ValueError("Archive hash mismatch; incomplete or damaged sync")
            expected = {**state["objects"], **doc["objects"]}
            if any(k in state["objects"] and state["objects"][k] != v for k, v in doc["objects"].items()):
                raise ValueError("Transfer tries to change an immutable object")
            current = objects(root)
            if any(expected.get(k) != v for k, v in current.items()):
                raise ValueError("Local repository has unsent or conflicting objects; preserve both branches")
            if any(current.get(k) != v for k, v in state["objects"].items()):
                raise ValueError("Previously imported objects are missing or changed")
            with tempfile.TemporaryDirectory(prefix="import-", dir=store.root) as tmp:
                staging = Path(tmp)
                seen = set()
                with tarfile.open(archive, "r:") as tar:
                    for member in tar:
                        name = member.name
                        if name in seen or not member.isfile() or name not in doc["objects"]:
                            raise ValueError("Unexpected/duplicate/nonregular archive member")
                        entry = doc["objects"][name]
                        if member.size != entry["size"]:
                            raise ValueError("Archive member size mismatch")
                        target = staging / name
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with tar.extractfile(member) as source, target.open("xb") as dest:
                            shutil.copyfileobj(source, dest, 1024 * 1024)
                        if cp.digest(target) != entry["sha256"]:
                            raise ValueError("Archive object hash mismatch")
                        seen.add(name)
                if seen != set(doc["objects"]):
                    raise ValueError("Archive is missing objects")
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Objects are immutable and never overwritten. Snapshots become
                # visible last; retries can reuse matching objects after interruption.
                for name in sorted(seen, key=lambda n: (n.startswith("snapshots/"), n)):
                    target = root / name
                    if target.exists():
                        if cp.digest(target) != doc["objects"][name]["sha256"]:
                            raise ValueError("Object changed during import")
                        continue
                    cp.reject_link_components(target.parent)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    publish(staging / name, target)
                if json.loads(store.run("cat", "config"))["id"] != doc["repository_id"]:
                    raise ValueError("Imported repository identity mismatch")
                store.run("check")
                state = {"sequence": doc["sequence"], "head": doc["archive_sha256"],
                         "objects": expected, "repository_id": doc["repository_id"]}
                cp.write_json(store.root / "transfer.json", state)
                imported.append(doc["sequence"])
        return {"imported_sequences": imported, "head": state["head"],
                "next": "restore the desired checkpoint to a new local directory"}

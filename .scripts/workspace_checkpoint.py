#!/usr/bin/env python3
"""Quiescent, private workspace checkpoints. No model loop or source writes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack, closing
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time

import graph_lib
from platform_compat import fcntl, reject_link_components

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path.home() / "WikiGraphData/checkpoints/config.json"
SCHEMA = "wikigraph-checkpoint-v1"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".new")
    with tmp.open("w", encoding="utf-8") as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def outside(path, source):
    path = reject_link_components(Path(path).expanduser().absolute())
    if path == source or source in path.parents or path in source.parents:
        raise ValueError("Storage/target must be disjoint from the source workspace")
    return path


def local_git_pointer(root):
    from workspace_setup import local_store
    pointer = root / ".git"
    if (not (root / "system-tools").is_dir() or pointer.is_symlink()
            or not pointer.is_file() or pointer.stat().st_size > 4096):
        return False
    expected = local_store(root) / "git"
    return (pointer.read_bytes().rstrip(b"\r\n") == b"gitdir: " + os.fsencode(expected)
            and expected.resolve() == expected and (expected / "HEAD").is_file())


def excluded(path, relative):
    # Fact files and private source material are never classified as caches.
    if "raw" in relative.parts:
        return False
    if relative.parts == (".git",) and local_git_pointer(path.parent):
        return True
    if len(relative.parts) == 1 and path.is_symlink():
        from local_environments import registered
        if registered(path.parent, relative.name):
            return True
    if len(relative.parts) == 1 and (relative.name == ".env" or
            relative.name in {".env.local", ".env.production", ".github.local.env", ".DS_Store"}):
        return True
    if path.is_dir() and not path.is_symlink():
        if (path / "pyvenv.cfg").is_file() and relative.name.startswith((".venv", "venv")):
            return True
        if relative.name == "__pycache__":
            return True
        if relative.name == "node_modules" and (path.parent / "package.json").is_file():
            return True
    return False


def inventory(root, *, filter_runtime=True):
    entries, omissions = {}, []
    def walk(parent):
        for p in sorted(parent.iterdir()):
            rel = p.relative_to(root)
            if filter_runtime and excluded(p, rel):
                omissions.append(rel.as_posix())
                continue
            s = p.lstat()
            kind = "link" if stat.S_ISLNK(s.st_mode) else "dir" if stat.S_ISDIR(s.st_mode) else "file"
            if kind == "file" and not stat.S_ISREG(s.st_mode):
                raise ValueError(f"Unsupported special file: {rel}")
            # Directory timestamps change when a read-only SQLite connection creates SHM.
            entries[rel.as_posix()] = (kind, stat.S_IMODE(s.st_mode),
                s.st_size if kind != "dir" else 0,
                s.st_mtime_ns if kind != "dir" else 0,
                s.st_ctime_ns if kind != "dir" else 0,
                os.readlink(p) if kind == "link" else "")
            if kind == "dir":
                walk(p)
    walk(root)
    return entries, omissions


def live_database(rel):
    p = Path(rel)
    return (rel in {"cross-domain/graph.db", "private/graph.db",
                    "cross-domain/source-fingerprints.db", "cross-domain/embeddings.db",
                    "academic/sources/index.sqlite", "academic/knowledge/index.sqlite",
                    "academic/frontier/index.sqlite", "academic/frontier/frontier.db"}
            or (p.name == "index.sqlite" and p.parent.name in {".workspace", "workspace-state"}))


def environment_inventory(source, omissions):
    result = {}
    for rel in omissions:
        env = source / rel
        if env.is_symlink():
            from local_environments import registered
            saved = registered(source, rel)
            if saved:
                result[rel] = saved
            continue
        if not (env / "pyvenv.cfg").is_file():
            continue
        packages = []
        for site in sorted(env.glob("lib/python*/site-packages")):
            for dist in metadata.distributions(path=[str(site)]):
                name, version = dist.metadata.get("Name", ""), dist.version
                if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
                    packages.append(f"{name}=={version}")
        result[rel] = {"packages": sorted(set(packages)),
                       "python": next((line.split("=", 1)[1].strip() for line in
                                       (env / "pyvenv.cfg").read_text().splitlines()
                                       if line.startswith(("version =", "version_info ="))), "unknown")}
    return result


def git_preflight(source):
    git = source / ".git"
    if local_git_pointer(source):
        from workspace_setup import local_store
        git = local_store(source) / "git"
    if git.is_file() or git.is_symlink():
        raise ValueError("Linked Git worktrees need an independent repository before checkpointing")
    if git.is_dir():
        if list(git.rglob("*.lock")):
            raise ValueError("Git lock found; finish Git operations first (do not delete locks blindly)")
        if (git / "objects/info/alternates").exists():
            raise ValueError("Git alternate object stores are not self-contained")


def database_check(path):
    # Only existing databases; graph_lib owns read-only SQLite connections.
    sidecars = [Path(str(path) + suffix) for suffix in ("-shm", "-wal")]
    absent = [p for p in sidecars if not p.exists()]
    conn = graph_lib.connect(path, read_only=True)
    try:
        result = [r[0] for r in conn.execute("PRAGMA quick_check")]
        if result != ["ok"]:
            raise ValueError(f"SQLite quick_check failed: {path.name}")
    finally:
        conn.close()
        for p in absent:
            p.unlink(missing_ok=True)  # only disposable/restored copies, never the source


def capture(source, destination, *, quiescent=False, parent=None, workspace_id="local"):
    if not quiescent:
        raise ValueError("Stop application/Git writers, then supply --quiescent")
    source = reject_link_components(Path(source).expanduser().absolute())
    destination = outside(destination, source)
    if destination.exists():
        raise FileExistsError(f"Capture destination already exists: {destination}")
    git_preflight(source)
    initial, _ = inventory(source)
    # Establish read snapshots before the drift baseline. SQLite may create empty
    # WAL/SHM on first read; keep connections open through the consistency check.
    with ExitStack() as stack:
        connections = {}
        for rel, info in initial.items():
            if info[0] == "file" and live_database(rel):
                conn = stack.enter_context(closing(graph_lib.connect(source / rel, read_only=True)))
                conn.execute("BEGIN")
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                connections[rel] = conn
        return _capture(source, destination, connections, quiescent=quiescent,
                        parent=parent, workspace_id=workspace_id, initial=initial)


def _capture(source, destination, connections, *, quiescent, parent, workspace_id, initial):
    if not quiescent:
        raise ValueError("Stop application/Git writers, then supply --quiescent")
    source = reject_link_components(Path(source).expanduser().absolute())
    destination = outside(destination, source)
    if not source.is_dir() or not (source / ".scripts").is_dir():
        raise ValueError("Source must be a WikiGraph workspace")
    if destination.exists():
        raise FileExistsError(f"Capture destination already exists: {destination}")
    git_preflight(source)
    started = time.monotonic()
    before, omissions = inventory(source)
    databases = {r for r, info in before.items() if info[0] == "file" and live_database(r)}
    if databases != set(connections):
        raise ValueError("Database inventory changed before capture")
    def startup_baseline(entries):
        return {k: v for k, v in entries.items()
                if k not in {r + "-shm" for r in databases}
                and not (k in {r + "-wal" for r in databases} and v[2] == 0)}
    if startup_baseline(initial) != startup_baseline(before):
        raise ValueError("Source changed while establishing database snapshots; capture refused")
    sidecars = {r + "-shm" for r in databases}
    # SQLite SHM is a transient lock/index; WAL remains part of source drift checking.
    def stable(entries):
        return {k: v for k, v in entries.items() if k not in {r + "-shm" for r in databases}}
    destination.mkdir(parents=True, mode=0o700)
    work = destination / "workspace"
    work.mkdir(mode=0o700)
    manifest, warnings = {}, []
    for rel, info in before.items():
        if rel in sidecars:
            continue
        src, dst = source / rel, work / rel
        kind, mode = info[:2]
        if kind == "dir":
            dst.mkdir(parents=True, exist_ok=True)
            manifest[rel] = {"kind": kind, "mode": mode}
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if kind == "link":
            os.symlink(info[5], dst)
            manifest[rel] = {"kind": kind, "target": info[5]}
            target = (src.parent / info[5]).resolve()
            if Path(info[5]).is_absolute() or (source not in target.parents and target != source) or not src.exists():
                warnings.append({"path": rel, "reason": "absolute, external or broken symlink; preserved, never followed"})
            continue
        if rel in databases:
            # Validate a SQLite backup, but retain the original DB and WAL bytes
            # in the checkpoint so a tracked graph does not gain a spurious diff.
            with tempfile.TemporaryDirectory(prefix="sqlite-check-", dir=destination) as tmp:
                check = Path(tmp) / "database.db"
                graph_lib.backup_graph(connections[rel], check)
                database_check(check)
        shutil.copy2(src, dst, follow_symlinks=False)
        manifest[rel] = {"kind": kind, "mode": mode, "size": dst.stat().st_size, "sha256": digest(dst)}
    after, after_omissions = inventory(source)
    git_preflight(source)
    if stable(before) != stable(after) or omissions != after_omissions:
        left, right = stable(before), stable(after)
        changed = [k for k in sorted(left.keys() | right.keys()) if left.get(k) != right.get(k)][:12]
        raise ValueError(f"Source changed during capture ({changed}); checkpoint rejected. Incomplete copy retained: {destination}")
    for rel, entry in reversed(list(manifest.items())):
        if entry["kind"] == "dir":
            os.chmod(work / rel, entry["mode"])
    doc = {"schema": SCHEMA, "workspace_id": workspace_id, "parent": parent,
           "created_at": datetime.now(timezone.utc).isoformat(), "files": manifest,
           "excluded": omissions, "databases": sorted(databases),
           "database_strategy": "original-database-and-wal-bytes",
           "environments": environment_inventory(source, omissions),
           "warnings": warnings, "capture_seconds": round(time.monotonic() - started, 3)}
    write_json(destination / "checkpoint.json", doc)
    verify_bundle(destination)
    return doc


def safe_relative(value):
    p = Path(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or p.as_posix() != value:
        raise ValueError("Invalid manifest relative path")
    return p


def verify_bundle(bundle):
    bundle = Path(bundle)
    doc = json.loads((bundle / "checkpoint.json").read_text(encoding="utf-8"))
    if doc.get("schema") != SCHEMA:
        raise ValueError("Unsupported checkpoint schema")
    work = bundle / "workspace"
    if work.is_symlink():
        raise ValueError("Workspace root cannot be a symlink")
    actual, _ = inventory(work, filter_runtime=False)
    if set(actual) != set(doc["files"]):
        raise ValueError("Restored file inventory differs from manifest")
    for rel, entry in doc["files"].items():
        p = work / safe_relative(rel)
        if actual[rel][0] != entry["kind"]:
            raise ValueError(f"File type mismatch: {rel}")
        if entry["kind"] == "link":
            if os.readlink(p) != entry["target"]:
                raise ValueError(f"Link mismatch: {rel}")
        elif entry["kind"] == "file":
            if p.stat().st_size != entry["size"] or digest(p) != entry["sha256"]:
                raise ValueError(f"Hash mismatch: {rel}")
        if entry["kind"] != "link" and os.name != "nt" and stat.S_IMODE(p.stat().st_mode) != entry["mode"]:
            raise ValueError(f"Permission mismatch: {rel}")
    for rel in doc.get("databases", doc.get("normalized_databases", [])):
        p = work / safe_relative(rel)
        if doc["files"].get(rel, {}).get("kind") != "file":
            raise ValueError("Invalid database entry")
        database_check(p)
    return doc


class Store:
    def __init__(self, config):
        self.path = reject_link_components(Path(config).expanduser().absolute())
        self.config = json.loads(self.path.read_text(encoding="utf-8"))
        self.root = self.path.parent
        self.state_path = self.root / "state.json"
        self.tag = "wikigraph:" + self.config["workspace_id"]

    @contextmanager
    def lock(self):
        with (self.root / "operation.lock").open("a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def run(self, *args, cwd=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("RESTIC_")}
        env.update(RESTIC_REPOSITORY=self.config["repository"],
                   RESTIC_PASSWORD_FILE=self.config["password_file"],
                   RESTIC_CACHE_DIR=str(self.root / "cache"))
        proc = subprocess.run(["restic", *args], cwd=cwd, env=env, capture_output=True, text=True)
        if proc.returncode:
            raise RuntimeError(f"restic {args[0]} failed ({proc.returncode}): {proc.stderr[-2000:]}")
        return proc.stdout

    def snapshots(self):
        return json.loads(self.run("snapshots", "--tag", self.tag, "--json")) or []

    def head(self):
        items = self.snapshots()
        return max(items, key=lambda s: s["time"])["id"] if items else None

    def state(self):
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def record(self, source, snapshot):
        state = self.state()
        state[str(Path(source).absolute())] = snapshot
        write_json(self.state_path, state)

    def save(self, source, quiescent):
        source = Path(source).expanduser().absolute()
        outside(self.root, source)
        if not re.match(r"^[a-z][a-z0-9+.-]*:", self.config["repository"]):
            outside(self.config["repository"], source)
        with self.lock():
            parent = self.head()
            if self.state().get(str(source)) != parent:
                raise ValueError("Stale/unregistered workspace: restore the latest checkpoint to a new directory first")
            stage = Path(tempfile.mkdtemp(prefix="save-", dir=self.root)) / "bundle"
            doc = capture(source, stage, quiescent=quiescent, parent=parent,
                          workspace_id=self.config["workspace_id"])
            if self.head() != parent:
                raise ValueError("Remote head changed while capturing; save refused")
            output = self.run("backup", "--json", "--tag", self.tag,
                              "--tag", SCHEMA, "--host", "wikigraph", ".", cwd=stage)
            summaries = [json.loads(line) for line in output.splitlines() if line.strip()]
            summary = next(x for x in reversed(summaries) if x.get("message_type") == "summary")
            sid = summary["snapshot_id"]
            matches = [x["id"] for x in self.snapshots() if x["id"].startswith(sid)]
            if len(matches) != 1:
                raise ValueError("Snapshot identity could not be confirmed")
            sid = matches[0]
            self.run("check")
            if self.head() != sid:
                raise ValueError("Another writer saved concurrently; preserve both snapshots and resolve explicitly")
            self.record(source, sid)
            shutil.rmtree(stage.parent)
            return {"snapshot": sid, "parent": parent, "summary": summary,
                    "entries": len(doc["files"]), "warnings": doc["warnings"],
                    "verification": "restic metadata check; full payload verified on restore"}

    def restore(self, snapshot, target):
        target = reject_link_components(Path(target).expanduser().absolute())
        outside(target, self.root)
        if target.exists():
            raise FileExistsError("Restore target must not exist; existing changes are never overwritten")
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            candidates = self.snapshots()
            sid = self.head() if snapshot == "latest" else snapshot
            matches = [s["id"] for s in candidates if sid and s["id"].startswith(sid)]
            if len(matches) != 1:
                raise ValueError("Snapshot must identify exactly one checkpoint in this workspace")
            sid = matches[0]
            stage = Path(tempfile.mkdtemp(prefix=".wikigraph-restore-", dir=target.parent))
            os.chmod(stage, 0o700)
            try:
                self.run("restore", sid, "--target", str(stage), "--verify")
                doc = verify_bundle(stage)
                if doc["workspace_id"] != self.config["workspace_id"]:
                    raise ValueError("Workspace identity mismatch")
                # Reserve the target after validation, then move into the empty reservation.
                target.mkdir(mode=0o700, exist_ok=False)
                os.replace(stage / "workspace", target)
                write_json(self.root / "receipts" / (sid + ".json"),
                           {"snapshot": sid, "target": str(target), "manifest": doc})
                self.record(target, sid)
                shutil.rmtree(stage)
                return {"snapshot": sid, "target": str(target), "entries": len(doc["files"]),
                        "warnings": doc["warnings"], "verified": "all SHA-256 hashes and SQLite quick_check"}
            except Exception as exc:
                raise RuntimeError(f"Restore failed; retained staging: {stage}; {exc}") from exc


def configure(args):
    path = reject_link_components(Path(args.config).expanduser().absolute())
    outside(path.parent, ROOT)
    if path.exists():
        raise FileExistsError("Config exists; use a separate config directory for another backend")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.workspace_id):
        raise ValueError("workspace-id must be 1-80 ASCII letters, digits, underscore or hyphen")
    repository = args.repository
    if not re.match(r"^[a-z][a-z0-9+.-]*:", repository):
        repository = str(outside(Path(repository).expanduser().absolute(), ROOT))
    password = reject_link_components(Path(args.password_file).expanduser().absolute())
    outside(password, ROOT)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not password.exists():
        if not args.init:
            raise FileNotFoundError("Supply an existing password file, or --init to create a new repository/key")
        password.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(48) + "\n")
    if os.name != "nt" and password.stat().st_mode & 0o077:
        raise ValueError("Password file must be private (chmod 600)")
    write_json(path, {"schema": SCHEMA, "repository": repository,
                     "password_file": str(password), "workspace_id": args.workspace_id})
    store = Store(path)
    if args.defer_connect:
        if args.init:
            raise ValueError("--defer-connect cannot be combined with --init")
        return {"config": str(path), "next": "import-pack from the synced transfer directory"}
    if args.init:
        store.run("init")
    store.run("snapshots", "--json")
    return {"config": str(path), "workspace_id": args.workspace_id,
            "key_recovery": "Keep an independent secure copy of the password file; never store it only beside the backup"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("configure")
    p.add_argument("--repository", required=True)
    p.add_argument("--password-file", required=True)
    p.add_argument("--workspace-id", default="wikigraph")
    p.add_argument("--init", action="store_true")
    p.add_argument("--defer-connect", action="store_true", help="Create machine config before importing an existing repository")
    p = sub.add_parser("save")
    p.add_argument("--source", default=str(ROOT))
    p.add_argument("--quiescent", action="store_true")
    p = sub.add_parser("restore")
    p.add_argument("--snapshot", default="latest")
    p.add_argument("--target", required=True)
    sub.add_parser("list")
    sub.add_parser("status")
    p = sub.add_parser("archive-temp")
    p.add_argument("name")
    p.add_argument("--evidence", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--quarantine", required=True)
    p = sub.add_parser("restore-temp")
    p.add_argument("name")
    p.add_argument("--target", required=True)
    for action in ("externalize-env", "prepare-env", "restore-env"):
        p = sub.add_parser(action)
        p.add_argument("name", choices=(".venv", ".venv-docling"))
        if action != "restore-env":
            p.add_argument("--store", default=str(Path.home() / "WikiGraphData/environments"))
        if action == "prepare-env":
            p.add_argument("--python")
    for action in ("export-pack", "import-pack"):
        p = sub.add_parser(action)
        p.add_argument("--directory", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--source", default=str(ROOT))
    p.add_argument("--destination", required=True)
    p.add_argument("--quiescent", action="store_true")
    p = sub.add_parser("verify")
    p.add_argument("bundle")
    args = parser.parse_args(argv)
    try:
        if args.action == "configure":
            result = configure(args)
        elif args.action == "capture":
            doc = capture(args.source, args.destination, quiescent=args.quiescent)
            result = {"bundle": args.destination, "entries": len(doc["files"]), "warnings": doc["warnings"]}
        elif args.action == "verify":
            doc = verify_bundle(args.bundle)
            result = {"entries": len(doc["files"]), "verified": True}
        elif args.action in {"externalize-env", "prepare-env", "restore-env"}:
            import local_environments
            if args.action == "externalize-env":
                result = local_environments.externalize(ROOT, args.name, args.store)
            elif args.action == "prepare-env":
                result = local_environments.prepare(ROOT, args.name, args.store, args.python)
            else:
                result = local_environments.restore_in_tree(ROOT, args.name)
        elif args.action in {"archive-temp", "restore-temp"}:
            import temp_archive
            if args.action == "archive-temp":
                result = temp_archive.archive(ROOT, args.name, args.evidence, args.reason,
                                              Path(args.quarantine).expanduser())
            else:
                _, _, packed = temp_archive.paths(ROOT, args.name)
                receipt = json.loads(packed.with_suffix(".json").read_text())
                result = temp_archive.restore(packed, Path(args.target).expanduser(), receipt["archive_sha256"])
        else:
            store = Store(args.config)
            if args.action == "save":
                result = store.save(args.source, args.quiescent)
            elif args.action == "restore":
                result = store.restore(args.snapshot, args.target)
            elif args.action == "list":
                result = store.snapshots()
            elif args.action in {"export-pack", "import-pack"}:
                import checkpoint_transfer
                function = checkpoint_transfer.export_pack if args.action == "export-pack" else checkpoint_transfer.import_packs
                result = function(store, Path(args.directory).expanduser().absolute())
            else:
                result = {"config": str(store.path), "workspace_id": store.config["workspace_id"],
                          "head": store.head(), "local_bases": store.state()}
        print(json.dumps({"ok": True, "action": args.action, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "action": args.action, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())

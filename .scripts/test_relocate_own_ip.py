#!/usr/bin/env python3
"""Isolated managed own-IP relocation and recovery regressions."""
import copy
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import graph_lib as gl
import ingest_check
import relocate_own_ip as migration
import source_fingerprints as fingerprints


@contextmanager
def workspace():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory).resolve()
        for relative in [migration.SOURCE_DIR, *migration.TARGET_DIRS,
                         Path("academic/wiki/references"), Path("cross-domain")]:
            (root / relative).mkdir(parents=True, exist_ok=True)
        (root / "academic/wiki/log.md").write_text("# Log\n", encoding="utf-8")
        for module in (gl, ingest_check, ingest_check.wl, ingest_check.wl.raw_locator):
            stack.enter_context(patch.object(module, "REPO", root))
        db = root / "cross-domain/graph.db"
        conn = gl.connect(db)
        gl.init_schema(conn)
        manifest = {"schema": "own-ip-relocation-v1", "reason": "用户确认自有成果", "items": []}
        for number, category in enumerate(("patents", "software")):
            name = f"自有成果{number}V1.0"
            source = migration.SOURCE_DIR / f"{name}.pdf"
            destination = Path("academic/raw/works") / category / source.name
            wiki = f"academic/wiki/references/{name}.md"
            page = wiki[:-3]
            raw = str(source.with_suffix(""))
            locator = str(source.with_suffix(".md"))
            (root / source).write_bytes(f"binary-{number}".encode())
            (root / source.with_suffix(".md")).write_text("# Certificate\n\nRegistered.\n", encoding="utf-8")
            (root / wiki).write_text(
                f"---\ntitle: Certificate\ntype: academic-reference\nsources: [{locator}]\n"
                "source_type: official-doc\ndate: 2026-09-10\nstatus: current\n"
                "confidence: high\ncreated: 2026-09-10\nupdated: 2026-09-10\n---\n"
                f"## Navigation\nCertificate.\n## Content\nRegistered.[^r3]\n"
                f"## Sources\n[^r3]: {locator}#L3\n", encoding="utf-8")
            conn.execute("INSERT INTO nodes(path,title,type) VALUES (?,?,?)", (page, "Certificate", "page"))
            conn.execute("INSERT INTO nodes(path,title,type) VALUES (?,?,?)", (raw, name, "raw"))
            conn.execute("INSERT INTO edges(subject,predicate,object,source) VALUES (?,?,?,?)",
                         (page, "来源", raw, locator))
            edge_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO edge_origins(edge_id,origin_page,source) VALUES (?,?,?)",
                         (edge_id, page, locator + "#L3"))
            conn.execute("INSERT INTO node_origins(node_path,origin_page,source) VALUES (?,?,?)",
                         (raw, page, locator + "#L3"))
            conn.execute("INSERT INTO node_glosses(node_path,origin_page,source,description) VALUES (?,?,?,?)",
                         (raw, page, locator + "#L3", "Local certificate"))
            fingerprints.register_source(root / source, text_path=root / source.with_suffix(".md"),
                                         db_path=root / "cross-domain/source-fingerprints.db", repo=root)
            manifest["items"].append({"source": str(source), "destination": str(destination), "wiki": wiki,
                                      "sha256": migration.sha256_file(root / source),
                                      "companion_sha256": migration.sha256_file(root / source.with_suffix(".md")),
                                      "wiki_sha256": migration.sha256_file(root / wiki)})
        conn.commit()
        conn.close()
        yield root, manifest


def test_preview_and_complete_preserve_hashes_ids_and_lineage():
    with workspace() as (root, manifest):
        preview = migration.relocate(root, manifest)
        assert preview["status"] == "planned"
        assert not (root / "temp/raw-relocations").exists()
        receipt = migration.relocate(root, manifest, apply=True)
        assert receipt["status"] == "completed"
        assert receipt["warnings"] == []
        conn = gl.connect(root / "cross-domain/graph.db")
        for item in manifest["items"]:
            old = str(Path(item["source"]).with_suffix(""))
            new = str(Path(item["destination"]).with_suffix(""))
            assert not (root / item["source"]).exists()
            assert migration.sha256_file(root / item["destination"]) == item["sha256"]
            assert migration.sha256_file((root / item["destination"]).with_suffix(".md")) == item["companion_sha256"]
            assert conn.execute("SELECT type FROM nodes WHERE path=?", (new,)).fetchone()[0] == "raw"
            assert conn.execute("SELECT node_path FROM aliases WHERE alias=?", (old,)).fetchone()[0] == new
            assert conn.execute("SELECT source FROM node_origins WHERE node_path=?", (new,)).fetchone()[0] == new + ".md#L3"
            assert old not in (root / item["wiki"]).read_text()
            errors, _warnings = ingest_check.graph_checks(root / item["wiki"], connection=conn)
            assert not errors
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            match = fingerprints.lookup_exact(root / item["destination"], repo=root,
                                              db_path=root / "cross-domain/source-fingerprints.db")
            assert match["raw_path"] == item["destination"]
        assert [row[0] for row in conn.execute("SELECT id FROM edges ORDER BY id")] == [1, 2]
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        assert migration.relocate(root, resume=receipt["receipt_path"])["status"] == "completed"


def test_preflight_rejects_hash_collision_symlink_and_unlisted_reference():
    for failure in ("hash", "collision", "symlink", "reference", "domain", "missing-db"):
        with workspace() as (root, original):
            manifest = copy.deepcopy(original)
            item = manifest["items"][0]
            if failure == "hash":
                item["sha256"] = "wrong"
            elif failure == "collision":
                (root / item["destination"]).write_bytes(b"existing")
            elif failure == "symlink":
                source = root / item["source"]
                source.rename(source.with_suffix(".original"))
                source.symlink_to(source.with_suffix(".original"))
            elif failure == "reference":
                (root / "academic/wiki/another.md").write_text(item["source"])
            elif failure == "domain":
                item["destination"] = "private/raw/health/test.pdf"
            else:
                (root / "cross-domain/graph.db").unlink()
            try:
                migration.relocate(root, manifest, apply=True)
            except ValueError:
                pass
            else:
                raise AssertionError(f"preflight must reject {failure}")
            assert not (root / "temp/raw-relocations").exists()


def test_validation_failure_rolls_back_without_touching_originals():
    with workspace() as (root, manifest):
        with patch.object(migration, "validate_pages", side_effect=ValueError("validation injected")):
            try:
                migration.relocate(root, manifest, apply=True)
            except ValueError as error:
                assert "validation injected" in str(error)
            else:
                raise AssertionError("expected validation failure")
        for item in manifest["items"]:
            assert migration.sha256_file(root / item["source"]) == item["sha256"]
            assert migration.sha256_file(root / item["wiki"]) == item["wiki_sha256"]
            assert not (root / item["destination"]).exists()
        receipt = json.loads(next((root / "temp/raw-relocations").glob("*/receipt.json")).read_text())
        assert receipt["status"] == "rolled_back"
        assert migration.relocate(root, manifest)["status"] == "planned"


def test_postcommit_failure_resumes_cleanup():
    with workspace() as (root, manifest):
        with patch.object(fingerprints, "register_source", side_effect=OSError("index unavailable")):
            try:
                migration.relocate(root, manifest, apply=True)
            except OSError:
                pass
            else:
                raise AssertionError("expected fingerprint failure")
        receipt_path = next((root / "temp/raw-relocations").glob("*/receipt.json"))
        receipt = json.loads(receipt_path.read_text())
        assert receipt["status"] == "committed"
        for item in manifest["items"]:
            assert (root / item["source"]).is_file()
            assert (root / item["destination"]).is_file()
        result = migration.relocate(root, resume=str(receipt_path.relative_to(root)))
        assert result["status"] == "completed"


def test_copy_failure_does_not_leave_partial_raw_targets():
    with workspace() as (root, manifest):
        with patch.object(migration.shutil, "copy2", side_effect=OSError("copy interrupted")):
            try:
                migration.relocate(root, manifest, apply=True)
            except OSError:
                pass
            else:
                raise AssertionError("expected copy failure")
        for item in manifest["items"]:
            assert not (root / item["destination"]).exists()
            assert migration.sha256_file(root / item["source"]) == item["sha256"]
        assert migration.relocate(root, manifest)["status"] == "planned"


def test_uncommitted_recovery_preserves_later_edits():
    with workspace() as (root, manifest):
        with patch.object(migration, "validate_pages", side_effect=ValueError("stop")), \
                patch.object(migration, "rollback_files", side_effect=RuntimeError("interrupted")):
            try:
                migration.relocate(root, manifest, apply=True)
            except RuntimeError:
                pass
        receipt_path = next((root / "temp/raw-relocations").glob("*/receipt.json"))
        wiki = root / manifest["items"][0]["wiki"]
        staged = wiki.read_bytes()
        wiki.write_bytes(staged + b"\nHuman edit.\n")
        try:
            migration.relocate(root, resume=str(receipt_path.relative_to(root)))
        except ValueError as error:
            assert "later edit" in str(error)
        else:
            raise AssertionError("must not overwrite later edits")
        assert b"Human edit" in wiki.read_bytes()
        wiki.write_bytes(staged)
        assert migration.relocate(root, resume=str(receipt_path.relative_to(root)))["status"] == "rolled_back"


if __name__ == "__main__":
    test_preview_and_complete_preserve_hashes_ids_and_lineage()
    test_preflight_rejects_hash_collision_symlink_and_unlisted_reference()
    test_validation_failure_rolls_back_without_touching_originals()
    test_postcommit_failure_resumes_cleanup()
    test_copy_failure_does_not_leave_partial_raw_targets()
    test_uncommitted_recovery_preserves_later_edits()
    print("own IP relocation regression: PASS")

#!/usr/bin/env python3
"""Regression tests for source_fingerprints.py."""
from __future__ import annotations

import importlib.util
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("source_fingerprints.py")
SPEC = importlib.util.spec_from_file_location("source_fingerprints", SCRIPT)
sf = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sf)


def test_register_and_exact_lookup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        raw = root / "academic/raw/references/demo/paper.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"same-pdf")
        incoming = root / "inbox/demo.pdf"
        incoming.parent.mkdir()
        incoming.write_bytes(b"same-pdf")
        sf.register_source(raw, db_path=db, repo=root)
        match = sf.lookup_exact(incoming, db_path=db, repo=root)
        assert match["raw_path"] == "academic/raw/references/demo/paper.pdf"
        assert match["match"] == "binary_sha256"


def test_lookup_exact_reuses_precomputed_digest():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        raw = root / "academic/raw/references/demo.pdf"
        incoming = root / "inbox/demo.pdf"
        raw.parent.mkdir(parents=True)
        incoming.parent.mkdir()
        raw.write_bytes(b"same-pdf")
        incoming.write_bytes(b"same-pdf")
        sf.register_source(raw, db_path=db, repo=root)
        digest = hashlib.sha256(incoming.read_bytes()).hexdigest()
        with patch.object(sf, "sha256_file", side_effect=AssertionError("must reuse digest")):
            match = sf.lookup_exact(
                incoming, db_path=db, repo=root,
                binary_sha256=digest, size_bytes=incoming.stat().st_size,
            )
        assert match["raw_path"] == "academic/raw/references/demo.pdf"


def test_default_databases_keep_public_and_private_physically_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        public = root / "academic/raw/references/public.txt"
        private = root / "private/raw/notes/private.txt"
        public.parent.mkdir(parents=True)
        private.parent.mkdir(parents=True)
        public.write_text("public", encoding="utf-8")
        private.write_text("private", encoding="utf-8")
        sf.register_source(public, repo=root)
        sf.register_source(private, repo=root)
        public_db = root / "cross-domain/source-fingerprints.db"
        private_db = root / "private/source-fingerprints.db"
        assert public_db.is_file() and private_db.is_file()
        assert sf.lookup_exact(public, db_path=public_db, repo=root)["raw_path"].startswith("academic/raw/")
        assert sf.lookup_exact(private, db_path=private_db, repo=root)["raw_path"].startswith("private/raw/")
        assert sf.lookup_exact(public, db_path=private_db, repo=root) is None
        assert sf.lookup_exact(private, db_path=public_db, repo=root) is None


def test_text_hash_is_candidate_only():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        raw = root / "academic/raw/references/demo/paper.pdf"
        text = raw.with_name("paper.md")
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"pdf-a")
        text.write_text("Title\n\nBody", encoding="utf-8")
        candidate = root / "candidate.md"
        candidate.write_text(" title   body ", encoding="utf-8")
        sf.register_source(raw, text_path=text, db_path=db, repo=root)
        match = sf.lookup_text_candidate(candidate, db_path=db, repo=root)
        assert match["match"] == "normalized_text_sha256"
        assert sf.lookup_exact(candidate, db_path=db, repo=root) is None


def test_lookup_ignores_stale_raw_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        stale = root / "academic/raw/old/a.pdf"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"same-pdf")
        sf.register_source(stale, db_path=db, repo=root)
        stale.unlink()

        incoming = root / "inbox/demo.pdf"
        incoming.parent.mkdir()
        incoming.write_bytes(b"same-pdf")
        assert sf.lookup_exact(incoming, db_path=db, repo=root) is None


def test_lookup_prefers_existing_raw_path_for_duplicate_hash():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        stale = root / "academic/raw/old/a.pdf"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"same-pdf")
        sf.register_source(stale, db_path=db, repo=root)
        stale.unlink()

        valid = root / "academic/raw/references/demo/paper.pdf"
        valid.parent.mkdir(parents=True)
        valid.write_bytes(b"same-pdf")
        sf.register_source(valid, db_path=db, repo=root)
        incoming = root / "inbox/demo.pdf"
        incoming.parent.mkdir()
        incoming.write_bytes(b"same-pdf")
        match = sf.lookup_exact(incoming, db_path=db, repo=root)
        assert match["raw_path"] == "academic/raw/references/demo/paper.pdf"


def test_text_lookup_ignores_stale_raw_paths():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = root / "fingerprints.db"
        stale_pdf = root / "academic/raw/old/a.pdf"
        stale_md = root / "academic/raw/old/a.md"
        stale_pdf.parent.mkdir(parents=True)
        stale_pdf.write_bytes(b"pdf")
        stale_md.write_text("Title\n\nBody", encoding="utf-8")
        sf.register_source(stale_pdf, text_path=stale_md, db_path=db, repo=root)
        stale_pdf.unlink()
        stale_md.unlink()

        candidate = root / "candidate.md"
        candidate.write_text(" title   body ", encoding="utf-8")
        assert sf.lookup_text_candidate(candidate, db_path=db, repo=root) is None


def test_rebuild_does_not_modify_raw_and_skips_sidecars():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "academic/raw/references/demo"
        package.mkdir(parents=True)
        pdf = package / "paper.pdf"
        md = package / "paper.md"
        source = package / "source.yaml"
        pdf.write_bytes(b"pdf")
        md.write_text("content", encoding="utf-8")
        source.write_text("source_type: copy\n", encoding="utf-8")
        docx = package / "report.docx"
        companion = package / "report.md"
        docx.write_bytes(b"docx")
        companion.write_text("generated extraction", encoding="utf-8")
        standalone = package / "notes.md"
        standalone.write_text("original markdown", encoding="utf-8")
        hidden = package / ".DS_Store"
        hidden.write_bytes(b"finder metadata")
        paths = (pdf, md, source, docx, companion, standalone, hidden)
        before = {path: path.read_bytes() for path in paths}
        db = root / "fingerprints.db"
        result = sf.rebuild(db_path=db, roots=(root / "academic/raw",), repo=root)
        assert result["indexed"] == 3
        assert before == {path: path.read_bytes() for path in paths}


def test_reconcile_adds_changes_removes_stale_and_skips_unchanged_hashing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_root = root / "academic/raw"
        first = raw_root / "references/first.txt"
        stale = raw_root / "references/stale.txt"
        first.parent.mkdir(parents=True)
        first.write_text("first", encoding="utf-8")
        stale.write_text("stale", encoding="utf-8")
        db = root / "fingerprints.db"
        sf.rebuild(db_path=db, roots=(raw_root,), repo=root)
        stale.unlink()
        second = raw_root / "references/second.txt"
        second.write_text("second", encoding="utf-8")

        original_hash = sf.sha256_file
        with patch.object(sf, "sha256_file", wraps=original_hash) as hashing:
            result = sf.reconcile(db_path=db, roots=(raw_root,), repo=root)
        assert result == {
            "status": "reconciled", "db": str(db), "indexed": 2,
            "added": 1, "updated": 0, "removed": 1, "unchanged": 1,
        }
        assert hashing.call_count == 1
        assert sf.lookup_exact(second, db_path=db, repo=root)

        with patch.object(sf, "sha256_file", side_effect=AssertionError("unchanged source rehashed")):
            unchanged = sf.reconcile(db_path=db, roots=(raw_root,), repo=root)
        assert unchanged["unchanged"] == 2
        assert unchanged["added"] == unchanged["updated"] == unchanged["removed"] == 0


def test_rebuild_failure_keeps_previous_index_visible():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw_root = root / "academic/raw"
        first = raw_root / "references/first.txt"
        second = raw_root / "references/second.txt"
        first.parent.mkdir(parents=True)
        first.write_text("first", encoding="utf-8")
        db = root / "fingerprints.db"
        sf.rebuild(db_path=db, roots=(raw_root,), repo=root)
        second.write_text("second", encoding="utf-8")
        original_record = sf._fingerprint_record

        def fail_on_second(path, **kwargs):
            if Path(path).name == "second.txt":
                raise OSError("simulated read failure")
            return original_record(path, **kwargs)

        with patch.object(sf, "_fingerprint_record", side_effect=fail_on_second):
            try:
                sf.rebuild(db_path=db, roots=(raw_root,), repo=root)
            except OSError as exc:
                assert "simulated" in str(exc)
            else:
                raise AssertionError("rebuild should fail")
        assert sf.lookup_exact(first, db_path=db, repo=root)
        assert sf.lookup_exact(second, db_path=db, repo=root) is None


def test_image_raw_package_indexes_only_original_and_preserves_standalone_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "admin/raw/references/form"
        package.mkdir(parents=True)
        original = package / "form.JPG"
        original.write_bytes(b"image")
        (package / "form.md").write_text("faithful transcription", encoding="utf-8")
        (package / "form.JPG.source.json").write_text('{"schema":"document-source-context-v1"}', encoding="utf-8")
        standalone = package / "data.json"
        standalone.write_text('{"amount":15000}', encoding="utf-8")
        before = {path: path.read_bytes() for path in package.iterdir()}
        db = root / "fingerprints.db"
        result = sf.rebuild(db_path=db, roots=(root / "admin/raw",), repo=root)
        assert result["indexed"] == 2
        assert sf.lookup_exact(original, db_path=db, repo=root)
        assert sf.lookup_exact(standalone, db_path=db, repo=root)
        assert before == {path: path.read_bytes() for path in package.iterdir()}


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"source_fingerprints regression: {len(tests)}/{len(tests)} PASS")


if __name__ == "__main__":
    main()

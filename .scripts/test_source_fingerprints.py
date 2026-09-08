#!/usr/bin/env python3
"""Regression tests for source_fingerprints.py."""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path


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
        match = sf.lookup_text_candidate(candidate, db_path=db)
        assert match["match"] == "normalized_text_sha256"
        assert sf.lookup_exact(candidate, db_path=db, repo=root) is None


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
        paths = (pdf, md, source, docx, companion, standalone)
        before = {path: path.read_bytes() for path in paths}
        db = root / "fingerprints.db"
        result = sf.rebuild(db_path=db, roots=(root / "academic/raw",), repo=root)
        assert result["indexed"] == 3
        assert before == {path: path.read_bytes() for path in paths}


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

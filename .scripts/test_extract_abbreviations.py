#!/usr/bin/env python3
"""Source-bound abbreviation discovery and automatic resolution regressions."""
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import extract_abbreviations as ea
import graph_lib as gl
import resolve_abbreviations as resolver


DEFINITION = "covariance matrix adaptation evolution strategy (CMA-ES). CMA-ES is used."


class AbbreviationTests(unittest.TestCase):
    def test_apply_without_verified_pairs_preserves_existing_page(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "existing.md"
            original = "---\nabbreviations: [{abbr: MPS, full: Matrix Product State}]\n---\n"
            page.write_text(original)
            with patch.object(sys, "argv", ["extract_abbreviations.py", str(page), "--apply"]), \
                    patch.object(ea, "extract_for_page", return_value=([], None)):
                with self.assertRaises(SystemExit) as raised:
                    ea.main()
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(page.read_text(), original)

    def test_yaml_source_variants_without_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "academic/raw/references/example/paper.md"
            raw.parent.mkdir(parents=True)
            raw.write_text(DEFINITION)
            page = root / "academic/wiki/papers/example.md"
            page.parent.mkdir(parents=True)
            sources = [
                'sources:\n  - "academic/raw/references/example/paper.md"',
                "sources:\n- academic/raw/references/example/paper.md",
                "sources: ['academic/raw/references/example/paper.md#L1']",
                "sources:\n    - 'raw/references/example/paper.md'",
            ]
            with patch.object(ea, "_REPO", root), \
                    patch.object(subprocess, "run", side_effect=AssertionError("no scan")):
                for source in sources:
                    for newline in ("\n", "\r\n"):
                        page.write_bytes(("---\n" + source + "\n---\n").replace("\n", newline).encode())
                        self.assertEqual(Path(ea.find_raw(page)), raw.resolve())

    def test_missing_unsafe_and_ambiguous_sources_abstain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "academic/wiki/papers/example.md"
            page.parent.mkdir(parents=True)
            for relative in ["academic/raw/a.md", "academic/raw/b.md", "private/raw/a.md", "notes.md"]:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(DEFINITION)
            with patch.object(ea, "_REPO", root), \
                    patch.object(subprocess, "run", side_effect=AssertionError("no scan")):
                for source in ["[]", "['missing.md']", "['notes.md']", "['private/raw/a.md']",
                               "['https://example.org/paper.md']", "['../../outside.md']",
                               "['academic/raw/a.md', 'academic/raw/b.md']"]:
                    page.write_text("---\nsources: " + source + "\n---\n")
                    self.assertIsNone(ea.find_raw(page), source)

    def test_source_symlink_cannot_cross_raw_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = root / "academic/wiki/papers/example.md"
            page.parent.mkdir(parents=True)
            private = root / "private/raw/secret.md"
            private.parent.mkdir(parents=True)
            private.write_text(DEFINITION)
            link = root / "academic/raw/linked.md"
            link.parent.mkdir(parents=True)
            link.symlink_to(private)
            page.write_text("---\nsources: [academic/raw/linked.md]\n---\n")
            with patch.object(ea, "_REPO", root):
                self.assertIsNone(ea.find_raw(page))

    def test_hyphenated_definition_in_both_orders(self):
        for text in [DEFINITION, "CMA-ES (covariance matrix adaptation evolution strategy). CMA-ES is used."]:
            pairs = dict(ea.extract_from_segments([text]))
            self.assertEqual(pairs["CMA-ES"][0].lower(), "covariance matrix adaptation evolution strategy")
        self.assertIn("MPS", dict(ea.extract_from_segments(["matrix product state (MPS). MPS is used."])))

    def test_frequency_initials_and_malformed_tokens_remain_rejected(self):
        for text in ["covariance matrix adaptation evolution strategy (CMA-ES).",
                     "different words (CMA-ES). CMA-ES.",
                     "covariance matrix adaptation evolution strategy (CMA--ES). CMA--ES.",
                     "covariance matrix adaptation evolution strategy (CMA-ES-). CMA-ES-.",
                     "covariance matrix adaptation evolution strategy (CMA-ES). CMA-ESExtra."]:
            self.assertEqual(ea.extract_from_segments([text]), [], text)

    def test_conflicting_definitions_abstain_but_duplicates_survive(self):
        other = "central memory allocation execution service (CMA-ES). CMA-ES is used."
        self.assertEqual(ea.extract_from_segments([DEFINITION, other]), [])
        self.assertEqual(ea.extract_from_segments([other, DEFINITION]), [])
        self.assertEqual(len(ea.extract_from_segments([DEFINITION, DEFINITION])), 1)

    def test_excluded_reference_definition_does_not_resolve(self):
        raw = "# Paper\n## Methods\nCMA-ES is used.\n## References\n" + DEFINITION
        self.assertEqual(ea.extract_from_segments(ea.locate_key_sections(raw)), [])

    def test_raw_definition_flows_to_existing_auto_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = "academic/wiki/papers/example"
            wiki = root / (page + ".md")
            raw = root / "academic/raw/references/example/paper.md"
            wiki.parent.mkdir(parents=True)
            raw.parent.mkdir(parents=True)
            wiki.write_text('---\nsources: ["academic/raw/references/example/paper.md"]\n---\n')
            raw.write_text("# Paper\n## Methods\n" + DEFINITION)
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            gl.init_schema(conn)
            entries = [{"token": "CMA-ES", "page": page, "field": "object",
                        "object": "CMA-ES优化", "predicate": "对比方法", "subject": page}]
            try:
                with patch.object(ea, "_REPO", root), patch.object(resolver, "REPO", root), \
                        patch.object(subprocess, "run", side_effect=AssertionError("no external process")):
                    pending = resolver.list_pending(conn, entries, emit=False)
                    self.assertEqual(pending["raw_resolvable"], 1)
                    self.assertEqual(pending["warning_count"], 0)
                    applied = resolver.apply(conn, entries, emit=False)
                    self.assertEqual(applied["resolved"][0]["token"], "CMA-ES")
                    self.assertEqual(resolver.list_pending(conn, entries, emit=False)["candidates"], [])
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

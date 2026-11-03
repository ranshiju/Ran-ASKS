#!/usr/bin/env python3
"""Single-read, deterministic label-preserving Wiki recall regressions."""
import json
import socket
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import graph_lib as gl
import hub_semantics as hs
import query_actions as qa
import wiki_locator as wl


class WikiRecallTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.public_db = self.root / "cross-domain/graph.db"
        self.private_db = self.root / "private/graph.db"
        for module, name, value in (
            (qa, "_REPO", self.root), (gl, "REPO", self.root), (gl, "GRAPH_DB", self.public_db),
            (gl, "PRIVATE_GRAPH_DB", self.private_db), (gl, "PRIVATE_DIR", self.private_db.parent),
            (hs, "REPO", self.root), (hs, "PAGE_ROOT", None), (wl, "REPO", self.root),
            (qa.sl, "REPO", self.root),
        ):
            self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.object(qa, "_tok", return_value=0))
        self.stack.enter_context(patch.object(qa.ns, "semantic_search", side_effect=AssertionError("embedding invoked")))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("network invoked")))
        for database in (self.public_db, self.private_db):
            database.parent.mkdir(parents=True)
            connection = gl.connect(database)
            gl.init_schema(connection)
            connection.close()

    def add_page(self, stem, title, navigation="", profile="", aliases=(), domain="academic", indexed=True, content="",
                 status="current", page_status="", superseded_by=""):
        page = f"{domain}/wiki/papers/{stem}"
        target = self.root / f"{page}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        text = f"# {title}\n\n## Navigation\n\n{navigation}\n"
        if page_status or superseded_by:
            text = f"---\nstatus: {page_status or status}\nsuperseded_by: '{superseded_by}'\n---\n" + text
        if profile:
            text += f"\n## 研究方向定位\n\n{profile}[^r1]\n"
        text += f"\n## Content\n\n{content}\n\n[^r1]: {domain}/raw/example.md#L2\n"
        target.write_text(text)
        if indexed:
            connection = gl.connect(self.private_db if domain == "private" else self.public_db)
            gl.ensure_node(connection, page, title, "page", status=status)
            gl.insert_aliases(connection, page, aliases)
            connection.commit()
            connection.close()
        return page

    def recall(self, query, domain="academic", topk=8, include_deprecated=False):
        text, _ = qa.wiki_recall(query, domain, str(topk), include_deprecated)
        return json.loads(text)

    def link_pages(self, *pages):
        connection = gl.connect(self.private_db if pages[0].startswith("private/") else self.public_db)
        gl.ensure_node(connection, "lookup-concept", "UniqueGraphConcept", "entity", entity_subtype="keyword")
        for page in pages:
            connection.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)",
                               ("lookup-concept", "核心方法", page))
        connection.commit()
        connection.close()

    def hybrid(self, query, **kwargs):
        with patch.object(qa.ns, "semantic_search", return_value={"candidates": []}):
            text, _ = qa.hybrid_recall(query, domain=kwargs.pop("domain", "academic"), **kwargs)
        return json.loads(text)

    def test_recall_entry_reads_actual_section_then_raw_without_full_page(self):
        page = self.add_page("entry", "Module guidance", "Module guidance", domain="teaching")
        target = self.root / f"{page}.md"
        target.write_text("# Module guidance\n\n## Navigation\nModule guidance\n\n"
                          "## Unrelated\nDO-NOT-DISCLOSE-FULL-PAGE\n\n"
                          "## Actual rules\nModule guidance rule.[^r1]\n\n"
                          "[^r1]: teaching/raw/example.md#L2\n")
        raw = self.root / "teaching/raw/example.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("heading\nThe chosen path is sufficient.\n")
        result = self.recall("Module guidance", domain="teaching")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["read_action"], "read_section")
        self.assertEqual(candidate["read_input"], {"page": page, "section": "actual-rules"})
        self.assertNotIn("DO-NOT-DISCLOSE-FULL-PAGE", json.dumps(result))
        read = qa.execute(candidate["read_action"], candidate["read_input"])
        self.assertTrue(read["ok"], read)
        data = json.loads(read["text"])
        self.assertEqual(data["raw_citations"], ["teaching/raw/example.md#L2"])
        self.assertNotIn("DO-NOT-DISCLOSE-FULL-PAGE", data["text"])
        evidence = qa.execute("read_raw", {"locator": data["raw_citations"][0]})
        self.assertTrue(evidence["ok"], evidence)
        self.assertIn("The chosen path is sufficient.", evidence["text"])

    def test_entry_fallback_uses_existing_section_and_missing_file_has_no_entry(self):
        page = self.add_page("no-cites", "Only navigation", "Navigation clue")
        candidate = self.recall("Only navigation")["candidates"][0]
        self.assertEqual(candidate["read_input"], {"page": page, "section": "navigation"})
        (self.root / f"{page}.md").write_text("# Only navigation\nNo subsection exists.\n")
        self.assertNotIn("read_action", self.recall("Only navigation")["candidates"][0])
        (self.root / f"{page}.md").write_text("# Only navigation\nA whole-page citation.[^r1]\n\n[^r1]: academic/raw/example.md#L2\n")
        self.assertNotIn("read_action", self.recall("Only navigation")["candidates"][0])
        (self.root / f"{page}.md").unlink()
        self.assertNotIn("read_action", self.recall("Only navigation")["candidates"][0])

    def test_missing_heading_lists_real_entries_and_context_accepts_fragment(self):
        page = self.add_page("context", "Context guide", "Context navigation", content="Body evidence.[^r1]")
        failed = qa.execute("read_section", {"page": page})
        self.assertFalse(failed["ok"])
        self.assertIn("可用", failed["error"])
        self.assertIn("content", failed["error"])
        result = qa.execute("wiki_context", {"page": page + "#content", "profile": "fact"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(json.loads(result["text"])["wiki"]["section"], "content")

    def test_private_read_entry_stays_scoped(self):
        page = self.add_page("private-entry", "Private guide", "Private navigation",
                             domain="private", content="Private guide evidence.[^r1]")
        candidate = self.recall("Private guide", domain="private")["candidates"][0]
        self.assertEqual(candidate["read_input"]["page"], page)
        denied = qa.execute(candidate["read_action"], candidate["read_input"], subproject="academic")
        self.assertFalse(denied["ok"])
        allowed = qa.execute(candidate["read_action"], candidate["read_input"], subproject="private")
        self.assertTrue(allowed["ok"], allowed)

    def test_deprecated_exact_match_does_not_displace_current(self):
        old = self.add_page("a-old", "Shared label", profile="Shared label", status="deprecated")
        current = self.add_page("z-current", "Shared label", profile="Shared label")
        result = self.recall("Shared label", topk=1)
        self.assertEqual([item["path"] for item in result["candidates"]], [current])
        self.assertEqual(result["direction_profile_hits"], 1)
        self.assertFalse(result["include_deprecated"])
        history = self.recall("Shared label", include_deprecated=True)
        self.assertEqual([item["path"] for item in history["candidates"]], [old, current])

    def test_all_deprecated_is_no_candidate_not_a_hit(self):
        self.add_page("old", "Shared label", status="deprecated")
        result = self.recall("Shared label")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["next"], "no_candidate")

    def test_unindexed_frontmatter_deprecated_and_successor(self):
        old = self.add_page("old", "Old label", "Shared label", indexed=False,
                            page_status="deprecated", superseded_by="[[Current label]]")
        self.assertEqual(self.recall("Shared label")["candidates"], [])
        candidate = self.recall("Shared label", include_deprecated=True)["candidates"][0]
        self.assertEqual(candidate["path"], old)
        self.assertEqual(candidate["status"], "deprecated")
        self.assertEqual(candidate["superseded_by"], "[[Current label]]")

    def test_either_graph_or_page_deprecated_blocks_admission(self):
        for graph_status, page_status in (("current", "deprecated"), ("deprecated", "current")):
            with self.subTest(graph_status=graph_status):
                self.add_page("conflict", "Conflict label", status=graph_status, page_status=page_status)
                self.assertEqual(self.recall("Conflict label")["candidates"], [])
                candidate = self.recall("Conflict label", include_deprecated=True)["candidates"][0]
                self.assertEqual(candidate["status"], graph_status)
                self.assertEqual(candidate["page_status"], page_status)

    def test_graph_only_deprecated_filtered(self):
        connection = gl.connect(self.public_db)
        gl.ensure_node(connection, "academic/wiki/papers/old", "Old label", "page", status="deprecated")
        connection.commit()
        connection.close()
        self.assertEqual(self.recall("Old label")["candidates"], [])
        self.assertEqual(self.recall("Old label", include_deprecated=True)["count"], 1)

    def test_graph_unknown_status_is_not_overwritten_by_disk_metadata(self):
        self.add_page("unknown", "Unknown label", status="unknown", page_status="current")
        candidate = self.recall("Unknown label")["candidates"][0]
        self.assertEqual(candidate["status"], "unknown")
        self.assertEqual(candidate["page_status"], "current")

    def test_fallback_filters_before_topk_and_keeps_history(self):
        old = self.add_page("a-old", "Old label", status="deprecated")
        current = self.add_page("z-current", "Current label")
        self.link_pages(old, current)
        result = self.recall("UniqueGraphConcept", topk=1)
        self.assertEqual(result["mode"], "graph_fallback")
        self.assertEqual([item["path"] for item in result["candidates"]], [current])
        self.assertEqual(self.recall("UniqueGraphConcept", topk=1, include_deprecated=True)["candidates"][0]["path"], old)

    def test_false_string_does_not_enable_history_and_invalid_values_rejected(self):
        self.add_page("old", "Old label", status="deprecated")
        for action, inputs in (("wiki_recall", {"domain": "academic"}),
                               ("admin_recall", {}), ("hybrid_recall", {"domain": "academic"})):
            for value in ("false", "FALSE", False):
                with self.subTest(action=action, value=value):
                    with patch.object(qa.ns, "semantic_search", return_value={"candidates": []}):
                        result = qa.execute(action, {"query": "Old label", "include_deprecated": value, **inputs})
                    self.assertTrue(result["ok"], result)
                    self.assertFalse(json.loads(result["text"])["include_deprecated"])
            for value in ("yes", "", 1, None, [], {}):
                result = qa.execute(action, {"query": "Old label", "include_deprecated": value, **inputs})
                self.assertFalse(result["ok"], result)

    def test_dispatch_admin_wrapper_and_private_history(self):
        for domain in ("admin", "private"):
            old = self.add_page("old", "Old label", status="deprecated", domain=domain)
            action = "admin_recall" if domain == "admin" else "wiki_recall"
            result = qa.execute(action, {"query": "Old label", "include_deprecated": "true"}, subproject=domain)
            self.assertTrue(result["ok"], result)
            self.assertEqual([item["path"] for item in json.loads(result["text"])["candidates"]], [old])
            self.assertEqual(self.recall("Old label", domain=domain)["count"], 0)
        self.assertIsNone(qa._QUERY_SCOPE.get())

    def test_hybrid_all_graph_channels_cannot_reintroduce_old_page(self):
        old = self.add_page("old", "Old label", status="deprecated")
        current = self.add_page("current", "Current label")
        self.link_pages(old, current)
        semantic = {"candidates": [{"node_id": old}, {"node_id": "lookup-concept"}]}
        with patch.object(qa.ns, "semantic_search", return_value=semantic):
            text, _ = qa.hybrid_recall("UniqueGraphConcept", domain="academic", intent="lineage")
            result = json.loads(text)
            self.assertEqual([item["path"] for item in result["candidates"]], [current])
            text, _ = qa.hybrid_recall("UniqueGraphConcept", domain="academic", include_deprecated=True)
            self.assertEqual({item["path"] for item in json.loads(text)["candidates"]}, {old, current})
        self.assertNotIn(old, {item["path"] for item in self.hybrid("Old label")["candidates"]})
        self.assertEqual(self.hybrid("Old label", include_deprecated=True)["candidates"][0]["path"], old)

    def test_hybrid_adjacency_exclusion_precedes_row_budget(self):
        old_pages = [self.add_page(f"old-{index}", "Old label", status="deprecated") for index in range(5)]
        current = self.add_page("current", "Current label")
        self.link_pages(*old_pages, current)
        self.assertEqual(self.hybrid("UniqueGraphConcept", topk="1")["candidates"][0]["path"], current)

    def test_deprecated_seed_can_still_navigate_to_current_neighbor(self):
        old = self.add_page("old", "UniqueOldLabel", status="deprecated")
        current = self.add_page("current", "Current document")
        self.add_page("other", "UniqueOldLabel overview")
        connection = gl.connect(self.public_db)
        connection.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)", (old, "关联", current))
        connection.commit()
        connection.close()
        candidates = {item["path"]: item for item in self.hybrid("UniqueOldLabel")["candidates"]}
        self.assertNotIn(old, candidates)
        self.assertIn(current, candidates)
        self.assertEqual(candidates[current]["signals"][0]["channel"], "graph_exact")

    def test_metadata_filter_still_reads_each_wiki_once(self):
        pages = [self.add_page("old", "Old label", page_status="deprecated"),
                 self.add_page("current", "Current label", page_status="current")]
        original_read = Path.read_text
        reads = Counter()

        def read_text(target, *args, **kwargs):
            if target.suffix == ".md":
                reads[str(target.relative_to(self.root)).removesuffix(".md")] += 1
            return original_read(target, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            self.recall("label")
        self.assertEqual(reads, Counter({page: 1 for page in pages}))

    def test_hybrid_excludes_before_capsule_read_without_rescanning(self):
        old = self.add_page("old", "UniqueOldLabel", page_status="deprecated")
        original_read = Path.read_text
        reads = Counter()

        def read_text(target, *args, **kwargs):
            if target.suffix == ".md":
                reads[str(target.relative_to(self.root)).removesuffix(".md")] += 1
            return original_read(target, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            self.assertEqual(self.hybrid("UniqueOldLabel")["candidates"], [])
        self.assertEqual(reads, Counter({old: 1}))

    def test_graph_search_dispatch_history_is_scoped_public_and_private(self):
        for domain in ("academic", "private"):
            old = self.add_page("old", "UniqueOldLabel", status="deprecated", domain=domain)
            for flag, expected in ((False, []), ("false", []), (True, [old]), ("true", [old])):
                result = qa.execute("graph_search", {"term": "UniqueOldLabel", "include_deprecated": flag}, subproject=domain)
                self.assertTrue(result["ok"], result)
                self.assertEqual([item["path"] for item in json.loads(result["text"])["nodes"]], expected)
        self.assertIsNone(qa._QUERY_SCOPE.get())

    def test_resolved_identity_does_not_make_deprecated_page_current(self):
        old = self.add_page("old", "UniqueOldLabel", status="deprecated")
        result = qa.execute("node_resolve", {"name": "UniqueOldLabel"})
        self.assertTrue(result["ok"], result)
        resolved = json.loads(result["text"])
        self.assertEqual(resolved["decision"], "resolved")
        self.assertEqual(resolved["node_id"], old)
        self.assertEqual(resolved["version_context"]["historical_page_paths"], [old])
        self.assertEqual(resolved["version_context"]["eligible_page_paths"], [])

    def test_semantic_dispatch_passes_strict_history_flag(self):
        for flag, expected in (("false", False), (True, True), ("true", True)):
            with patch.object(qa.ns, "semantic_search", return_value={"candidates": []}) as search:
                result = qa.execute("semantic_search", {"query": "old", "include_deprecated": flag})
            self.assertTrue(result["ok"], result)
            self.assertIs(search.call_args.kwargs["include_deprecated"], expected)
        for action, inputs in (("semantic_search", {"query": "old"}), ("graph_search", {"term": "old"})):
            result = qa.execute(action, {**inputs, "include_deprecated": 1})
            self.assertFalse(result["ok"], result)

    def test_each_scanned_file_read_once(self):
        pages = [self.add_page(f"page-{index}", f"Title {index}", "shared term", "研究 shared term。") for index in range(3)]
        (self.root / "academic/wiki/index.md").write_text("shared term")
        (self.root / "academic/wiki/log.md").write_text("shared term")
        original_read = Path.read_text
        reads = Counter()

        def read_text(target, *args, **kwargs):
            if target.suffix == ".md":
                reads[str(target.relative_to(self.root)).removesuffix(".md")] += 1
            return original_read(target, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            self.recall("shared term")
        self.assertEqual(reads, Counter({page: 1 for page in pages}))

    def test_exact_title_survives_lexical_merge(self):
        target = self.add_page("opaque-target", "Tensorizing Neural Networks", "Tensorizing method")
        self.add_page("noise", "General methods", "Tensorizing Neural Networks " * 80)
        result = self.recall("Tensorizing Neural Networks", topk=1)
        self.assertEqual(result["candidates"][0]["path"], target)
        self.assertEqual(result["candidates"][0]["title"], "Tensorizing Neural Networks")
        self.assertEqual(result["candidates"][0]["status"], "current")
        self.assertEqual(result["candidates"][0]["match_kind"], "exact_label")

    def test_exact_alias_keeps_canonical_title_and_one_candidate(self):
        target = self.add_page("opaque", "矩阵乘积态", "MPS applications", aliases=("MPS", "Matrix Product State"))
        self.add_page("noise", "Other applications", "MPS " * 80)
        result = self.recall("mps")
        self.assertEqual(result["candidates"][0]["path"], target)
        self.assertEqual(result["candidates"][0]["title"], "矩阵乘积态")
        self.assertEqual(len([item for item in result["candidates"] if item["path"] == target]), 1)

    def test_title_phrase_beats_navigation_repetition(self):
        target = self.add_page("opaque", "Density Matrix Renormalization Group with Tensor Processing Units", "TPU")
        self.add_page("noise", "Hardware overview", "Tensor Processing Units " * 100)
        result = self.recall("Tensor Processing Units", topk=1)
        self.assertEqual(result["candidates"][0]["path"], target)

    def test_unique_term_coverage_beats_repeated_common_term(self):
        target = self.add_page("target", "Thermal methods", "quantum lattice and thermodynamics")
        self.add_page("noise", "A generic method", "quantum " * 100)
        self.assertEqual(self.recall("quantum lattice thermodynamics", topk=1)["candidates"][0]["path"], target)

    def test_short_chinese_label_beats_navigation_repetition(self):
        target = self.add_page("opaque", "谐振子纠缠演化", "动力学")
        self.add_page("noise", "General discussion", "纠缠 " * 100)
        self.assertEqual(self.recall("纠缠", topk=1)["candidates"][0]["path"], target)

    def test_literal_wildcards_do_not_match_other_labels(self):
        percent = self.add_page("percent", "20% method")
        underscore = self.add_page("underscore", "Variable_name")
        slash = self.add_page("slash", "Path\\part")
        self.add_page("noise", "Other method")
        for query, expected in (("%", percent), ("_", underscore), ("\\", slash)):
            with self.subTest(query=query):
                self.assertEqual([item["path"] for item in self.recall(query)["candidates"]], [expected])

    def test_supplied_profile_text_matches_file_parser_without_reread(self):
        page = self.add_page("profile", "Example", profile="研究开放量子系统中的纠缠动力学。")
        text = (self.root / f"{page}.md").read_text()
        expected = hs.read_paper_profile(page)
        with patch.object(Path, "read_text", side_effect=AssertionError("second read")):
            actual = hs.read_paper_profile(page, text=text)
            self.assertIsNone(hs.read_paper_profile(page, text="# no direction section"))
        self.assertEqual(expected, actual)
        self.assertEqual(actual.raw_citations, ("academic/raw/example.md#L2",))

    def test_only_navigation_and_profile_not_content_are_scored(self):
        target = self.add_page("target", "Example", profile="EntanglementDynamics", content="SECRET_BODY_ONLY")
        result = self.recall("EntanglementDynamics")
        self.assertEqual(result["candidates"][0]["path"], target)
        self.assertEqual(result["direction_profile_hits"], 1)
        self.assertEqual(self.recall("SECRET_BODY_ONLY")["candidates"], [])

    def test_unindexed_wiki_and_graph_only_label_remain_reachable(self):
        page = self.add_page("unindexed", "Unindexed", "UniqueNavigation", indexed=False)
        self.assertEqual(self.recall("UniqueNavigation")["candidates"][0]["path"], page)
        connection = gl.connect(self.public_db)
        gl.ensure_node(connection, "academic/wiki/papers/missing", "GraphOnlyLabel", "page")
        connection.commit()
        connection.close()
        result = self.recall("GraphOnlyLabel")
        self.assertEqual(result["candidates"][0]["path"], "academic/wiki/papers/missing")
        self.assertEqual(result["candidates"][0]["navigation"], "")

    def test_changes_visible_on_next_query_without_cached_state(self):
        page = self.add_page("target", "Example", "OldNavigation")
        self.assertEqual(self.recall("NewNavigation")["candidates"], [])
        (self.root / f"{page}.md").write_text("# Example\n\n## Navigation\nNewNavigation\n\n## Content\n")
        self.assertEqual(self.recall("NewNavigation")["candidates"][0]["path"], page)

    def test_public_private_and_domain_boundaries(self):
        academic = self.add_page("shared", "Shared label", "PUBLIC_ONLY")
        admin = self.add_page("shared", "Shared label", "ADMIN_ONLY", domain="admin")
        private = self.add_page("shared", "Shared label", "PRIVATE_ONLY", domain="private")
        self.assertEqual({item["path"] for item in self.recall("Shared label", domain="")["candidates"]}, {academic, admin})
        self.assertEqual([item["path"] for item in self.recall("Shared label", domain="private")["candidates"]], [private])
        self.assertEqual([item["path"] for item in self.recall("Shared label", domain="admin")["candidates"]], [admin])
        self.assertIsNone(qa._QUERY_SCOPE.get())

    def test_equal_rank_uses_path_order_and_is_read_only(self):
        second = self.add_page("z-page", "Shared label", "shared")
        first = self.add_page("a-page", "Shared label", "shared")
        before = self.public_db.read_bytes()
        result = self.recall("Shared label")
        self.assertEqual([item["path"] for item in result["candidates"]], [first, second])
        self.assertEqual(result, self.recall("Shared label"))
        self.assertEqual(before, self.public_db.read_bytes())


    def test_repeated_mentions_do_not_raise_score(self):
        self.add_page("short", "A generic method", "quantum and lattice")
        self.add_page("long", "Another generic method", "quantum and lattice " * 100)
        result = self.recall("quantum lattice")
        self.assertEqual({item["score"] for item in result["candidates"]}, {2})
        self.assertEqual({item["match_kind"] for item in result["candidates"]}, {"lexical"})

    def test_graph_fallback_still_returns_navigation_without_embedding(self):
        page = self.add_page("target", "A generic method", "A generic navigation")
        connection = gl.connect(self.public_db)
        gl.ensure_node(connection, "lookup-concept", "UniqueGraphConcept", "entity", entity_subtype="keyword")
        connection.execute("INSERT INTO edges(subject,predicate,object) VALUES (?,?,?)",
                           ("lookup-concept", "核心方法", page))
        connection.commit()
        connection.close()
        result = self.recall("UniqueGraphConcept")
        self.assertEqual(result["mode"], "graph_fallback")
        self.assertEqual(result["candidates"][0]["path"], page)
        self.assertEqual(result["candidates"][0]["navigation"], "A generic navigation")

    def test_scope_escape_rejects_read_and_closes_connection(self):
        private = self.add_page("private-page", "Private title", "PRIVATE_ONLY", domain="private")
        public_root = self.root / "academic/wiki/papers"
        public_root.mkdir(parents=True)
        (public_root / "escape.md").symlink_to(self.root / f"{private}.md")
        connection = gl.connect(self.public_db, read_only=True)
        with patch.object(qa, "_connect", return_value=connection):
            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                self.recall("Private title")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        self.assertIsNone(qa._QUERY_SCOPE.get())


if __name__ == "__main__":
    unittest.main()

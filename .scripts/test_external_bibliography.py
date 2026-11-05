#!/usr/bin/env python3
"""Offline service fixtures and production bibliography/workspace boundary tests."""
import copy
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import external_bibliography as eb
import ingest_paper as ip
import ingest_check as check

TITLE = "Reliable learning with quantum circuits"
MD = f"# {TITLE}\n\nAlice Smith, Bob Jones\n\narXiv:2101.12345v2\n\n## Abstract\nAn earlier study cites a different article.\n## References\n[1] Other paper 10.1000/other\n"
RECORD = {"type": "journal-article", "DOI": "10.1000/example", "title": [TITLE],
          "container-title": ["Journal of Quantum Research"],
          "author": [{"given": "Alice", "family": "Smith"}, {"given": "Bob", "family": "Jones"}],
          "published-print": {"date-parts": [[2022, 1]]},
          "published-online": {"date-parts": [[2021, 12, 9]]}, "volume": "4", "page": "123"}
SEEDS = {"title": [TITLE], "doi": [RECORD["DOI"]], "arxiv_id": ["2101.12345"]}
CONFERENCE = {
    "paperId": "a" * 40, "title": TITLE, "year": 2022,
    "publicationTypes": ["Conference"],
    "publicationVenue": {"type": "conference", "name": "Quantum Research Conference"},
    "externalIds": {"DBLP": "conf/quantum/SmithJones22"},
    "authors": [{"name": "Alice Smith"}, {"name": "Bob Jones"}],
    "publicationDate": "2021-01-01",  # index date is not assumed to be the conference date
}


def body(record=RECORD):
    return json.dumps({"message": record})


def atom(identity="2101.12345v2", linked="10.1000/example"):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry><id>http://arxiv.org/abs/{identity}</id><title>{TITLE}</title><author><name>Alice Smith</name></author><author><name>Bob Jones</name></author><arxiv:doi>{linked}</arxiv:doi></entry></feed>'''


class BibliographyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "external-bibliography.json"

    def resolve(self, fetcher=None, seeds=None, md=MD):
        return eb.resolve(md, seeds or SEEDS, self.cache, fetcher=fetcher or (lambda url: body()))

    def conference(self, records=None, **payload):
        records = [CONFERENCE] if records is None else records
        def fetch(url):
            if url.startswith(eb.SEMANTIC_SCHOLAR):
                return json.dumps({"total": len(records), "data": records, **payload})
            return body({"items": []})
        return self.resolve(fetch, {**SEEDS, "doi": [], "arxiv_id": []})

    def test_formal_doi_and_date_precision(self):
        result = self.resolve()
        bib = result["bibliographic"]
        self.assertEqual(bib["year"], "2022")
        self.assertEqual(bib["publication_dates"]["published-online"], [2021, 12, 9])
        self.assertEqual(bib["authors"], ["Alice Smith", "Bob Jones"])
        self.assertEqual(bib["source_content_version"], "not_verified")
        self.assertEqual(result["receipt"]["request_count"], 1)

    def test_arxiv_link_is_resolved_to_formal(self):
        urls = []
        def fetch(url):
            urls.append(url)
            return atom() if url.startswith(eb.ARXIV) else body()
        result = self.resolve(fetch, {**SEEDS, "doi": []})
        self.assertEqual(result["bibliographic"]["doi"], "10.1000/example")
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith(eb.ARXIV))

    def test_arxiv_doi_is_not_a_formal_publication(self):
        result = self.resolve(lambda url: atom() if url.startswith(eb.ARXIV) else body(),
                              {**SEEDS, "doi": ["10.48550/arXiv.2101.12345"], "arxiv_id": []})
        self.assertEqual(result["bibliographic"]["doi"], "10.1000/example")
        self.assertEqual(result["receipt"]["request_count"], 2)

    def test_wrong_arxiv_entry_does_not_supply_doi(self):
        urls = []
        def fetch(url):
            urls.append(url)
            return atom("2201.99999") if url.startswith(eb.ARXIV) else body({"items": []})
        self.assertIsNone(self.resolve(fetch, {**SEEDS, "doi": []})["bibliographic"])
        self.assertFalse(any("/works/" in url for url in urls))

    def test_title_search_requires_unique_identity(self):
        result = self.resolve(lambda url: body({"items": [RECORD]}), {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertEqual(result["bibliographic"]["venue"], RECORD["container-title"][0])
        self.cache.unlink()
        other = {**RECORD, "DOI": "10.1000/second"}
        result = self.resolve(lambda url: body({"items": [RECORD, other]}), {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertIsNone(result["bibliographic"])
        self.assertEqual(result["receipt"]["status"], "unresolved")

    def test_wrong_title_author_doi_or_incomplete_record_rejected(self):
        variants = [
            {**RECORD, "title": ["A completely unrelated article"]},
            {**RECORD, "author": [{"given": "Chris", "family": "Other"}]},
            {**RECORD, "container-title": []},
            {**RECORD, "author": []},
            {**RECORD, "type": "posted-content"},
            {**RECORD, "published-print": {}, "published-online": {}},
            {**RECORD, "DOI": "10.1000/other"},
        ]
        for record in variants:
            with self.subTest(record=record):
                self.cache.unlink(missing_ok=True)
                # Only direct DOI response has a record; search cannot rescue a mismatch.
                result = self.resolve(lambda url: body(record) if "/works/" in url else body({"items": []}))
                self.assertIsNone(result["bibliographic"])

    def test_reference_title_does_not_identify_source(self):
        md = "# Different Current Paper\nAlice Smith, Bob Jones\n## References\n[1] " + TITLE
        self.assertIsNone(self.resolve(md=md)["bibliographic"])

    def test_cache_reuses_failure_and_success_without_calls(self):
        for good in (False, True):
            self.cache.unlink(missing_ok=True)
            def first(url):
                if not good:
                    raise TimeoutError()
                return body()
            initial = self.resolve(first)
            again = self.resolve(lambda url: self.fail("cache must not call service"))
            self.assertTrue(again["cache_hit"])
            self.assertEqual(initial["bibliographic"], again["bibliographic"])
            self.assertLessEqual(initial["receipt"]["request_count"], eb.MAX_REQUESTS)
            self.assertNotIn("unpublished", json.dumps(again))

    def test_replay_rejects_source_and_response_drift(self):
        result = self.resolve()
        self.assertIsNone(eb.replay(result["receipt"], MD + "changed"))
        receipt = copy.deepcopy(result["receipt"])
        receipt["responses"][0]["body"] += " "
        self.assertIsNone(eb.replay(receipt, MD))
        receipt = copy.deepcopy(result["receipt"])
        receipt["responses"][0]["url"] = "https://attacker.test/works"
        self.assertIsNone(eb.replay(receipt, MD))

    def test_source_change_invalidates_cache(self):
        self.resolve()
        urls = []
        result = self.resolve(lambda url: urls.append(url) or body(), md=MD + "\nmore text")
        self.assertTrue(urls)
        self.assertFalse(result["cache_hit"])

    def test_endpoint_and_response_bounds(self):
        for url in ("http://api.crossref.org/works/x", "https://api.crossref.org.attacker.test/works/x",
                    "https://api.crossref.org@attacker.test/works/x", "file:///etc/passwd"):
            self.assertFalse(eb.allowed_url(url))
            with self.assertRaises(ValueError):
                eb.fetch(url)
        result = self.resolve(lambda url: "x" * (eb.MAX_BYTES + 1))
        self.assertIsNone(result["bibliographic"])
        self.assertFalse(result["receipt"]["responses"])
        with self.assertRaises(ValueError):
            eb._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org")

    def test_no_credentials_and_search_uses_only_title(self):
        urls = []
        self.resolve(lambda url: urls.append(url) or body({"items": [RECORD]}),
                     {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertEqual(len(urls), 1)
        self.assertNotIn("Alice", urls[0])
        self.assertNotIn("Abstract", urls[0])
        self.assertNotIn("key=", urls[0])

    def test_backend_review_zero_llm_and_archive_receipt(self):
        bib = self.resolve()["bibliographic"]
        for backend in ("api", "agent"):
            with patch.object(ip, "ingest_mode", return_value=backend), patch.object(ip, "call_json", side_effect=AssertionError("no LLM")):
                result = ip.review_bibliographic_metadata(bib, MD, agent_workspace=True)
            self.assertTrue(result["ok"])
            self.assertEqual(result["decision"]["protocol_version"], eb.PROTOCOL)
            self.assertFalse(result["worker"]["api_called"])
        ip.persist_bibliographic_metadata(self.root, bib)
        persisted = ip.yaml.safe_load((self.root / "source.yaml").read_text())["bibliographic"]
        self.assertEqual(eb.replay(persisted["external_receipt"], MD)["authors"], bib["authors"])

    def test_resume_rejects_worker_overwrite(self):
        bib = self.resolve()["bibliographic"]
        result = ip.review_bibliographic_metadata(bib, MD, agent_workspace=True)
        (self.root / "paper.md").write_text(MD)
        review_path = self.root / "bibliographic-review.json"
        state = {"transaction_id": "test", "extract_dir": ".", "status": "prepared",
                 "bibliographic_meta": bib, "bibliographic_review": {"status": "prepared"}}
        decision = copy.deepcopy(result["decision"])
        decision["fields"]["year"] = "1999"
        review_path.write_text(json.dumps(decision))
        with patch.object(ip, "REPO", self.root):
            self.assertFalse(ip._resume_bibliographic_review(state))
            review_path.write_text(json.dumps(result["decision"]))
            self.assertTrue(ip._resume_bibliographic_review(state))
        self.assertEqual(state["bibliographic_meta"]["year"], "2022")

    def test_new_transactions_enable_lookup_but_legacy_and_reingest_do_not(self):
        with patch.object(ip, "REPO", self.root), patch.object(eb, "fetch", return_value=body()) as fetch:
            state = ip.new_state_for_pdf(self.root / "inbox/paper.pdf")
            state["extract_dir"] = "."
            state["bibliographic_meta"] = {"doi": "10.1000/example"}
            ip.prepare_external_bibliography(state, MD)
            self.assertEqual(state["bibliographic_meta"]["year"], "2022")
            self.assertEqual(fetch.call_count, 1)
            ip.prepare_external_bibliography(state, MD)
            self.assertEqual(fetch.call_count, 1)
            for other in ({"extract_dir": "."}, {**state, "reingest": True}):
                ip.prepare_external_bibliography(other, MD)
            self.assertEqual(fetch.call_count, 1)

    def test_external_api_prompt_omits_bibliographic_generation(self):
        result = ip.review_bibliographic_metadata(self.resolve()["bibliographic"], MD)
        prompt = ip.build_api_paper_workspace_prompt(MD, self.root / "paper.md", "# Skeleton",
                                                    result["catalog"], locked_decision=result["decision"])
        self.assertNotIn("[A. 书目裁决]", prompt)
        self.assertNotIn("<<<BIBLIOGRAPHIC>>>", prompt)
        self.assertIn("<<<WIKI>>>", prompt)
        self.assertIn("<<<SLOTS>>>", prompt)

    def test_api_workspace_program_supplies_locked_segment_and_hashes(self):
        bib = self.resolve()["bibliographic"]
        review = ip.review_bibliographic_metadata(bib, MD, agent_workspace=True)
        paper = self.root / "paper.md"
        paper.write_text(MD)
        (self.root / "paper.pdf").write_bytes(b"fixture PDF")
        skeleton = self.root / "skeleton.md"
        skeleton.write_text("---\ntitle: Locked\n---\n# Locked\n")
        state = {"transaction_id": "test", "status": "extract", "extract_dir": ".",
                 "source": "inbox/paper.pdf", "bibliographic_meta": bib, "errors": []}
        generated = "<<<BIBLIOGRAPHIC>>>\n{\"year\":1999}\n<<<WIKI>>>\n# Content\n<<<SLOTS>>>\n三元组:\n本论文 | 核心方法 | Test\n"
        with patch.object(ip, "REPO", self.root), patch.object(ip, "ingest_mode", return_value="api"), \
                patch.object(ip.inbox_state, "save"), \
                patch.object(ip, "call_text", return_value={"ok": True, "text": generated, "history": []}) as call:
            ok, message = ip.execute_api_paper_workspace(state, review, paper,
                skeleton_bundle=(skeleton, skeleton.read_text(), ""))
        self.assertTrue(ok, message)
        self.assertEqual(call.call_count, 1)  # content generation only
        self.assertEqual(state["bibliographic_meta"]["year"], "2022")
        self.assertEqual(state["bibliographic_review"]["decision"], review["decision"])
        self.assertEqual(state["agent_workspace"]["artifact_bundle_sha256"], state["artifact_bundle_sha256"])
        self.assertTrue((self.root / "source.yaml").is_file())

    def test_agent_content_task_does_not_request_bibliography(self):
        bib = self.resolve()["bibliographic"]
        (self.root / "paper.md").write_text(MD)
        (self.root / "skeleton.md").write_text("---\ntitle: old\ndate: 2021\nvenue: arXiv\n---\n# old\n")
        state = {"transaction_id": "test", "status": "write_wiki", "extract_dir": ".",
                 "paper_id": "test", "raw_dir": "academic/raw/references/test",
                 "wiki_path": "academic/wiki/papers/test", "bibliographic_meta": bib}
        with patch.object(ip, "REPO", self.root), patch.object(ip, "ingest_mode", return_value="agent"), \
                patch.object(ip.agent_task, "prepare") as task, \
                patch.object(ip, "call_text", side_effect=AssertionError("no API in Agent mode")):
            ok, _ = ip.step_write_wiki(state)
        self.assertFalse(ok)  # content task remains; bibliography is already resolved
        self.assertEqual(task.call_args.kwargs["protocol"]["order"], ["WIKI", "SLOTS"])
        self.assertIn("date: 2022", (self.root / "skeleton.md").read_text())

    def test_archived_validator_prefers_formal_date_and_rejects_drift(self):
        bib = self.resolve()["bibliographic"]
        bib["first_page_evidence"] = ["Published online December 2021"]
        (self.root / "paper.md").write_text(MD)
        ip.persist_bibliographic_metadata(self.root, bib)
        fm = {"type": "paper-summary", "sources": ["paper.md"], "date": 2022,
              "venue": bib["venue"]}
        with patch.object(check, "REPO", self.root):
            self.assertFalse(check.check_bibliographic_consistency("academic/wiki/papers/test.md", fm))
            self.assertTrue(check.check_bibliographic_consistency("academic/wiki/papers/test.md", {**fm, "date": 2021}))
            (self.root / "paper.md").write_text(MD + "drift")
            self.assertTrue(check.check_bibliographic_consistency("academic/wiki/papers/test.md", fm))

    def test_invalid_arxiv_month_and_reference_id_not_propagated(self):
        self.assertEqual(eb.arxiv_id("2023.0218"), "")
        result = self.resolve(seeds={**SEEDS, "arxiv_id": ["1605.01735"]})
        self.assertEqual(result["bibliographic"]["arxiv_id"], "")

    def test_author_superscripts_and_numbered_reference_boundary(self):
        md = MD.replace("Alice Smith, Bob Jones", "Alice Smith<sup>1</sup>, Bob Jones<sup>2</sup>")
        self.assertIsNotNone(self.resolve(md=md)["bibliographic"])
        front = eb.frontmatter("# Different Paper\nSome author\n1. " + TITLE + "\nAlice Smith, Bob Jones")
        self.assertNotIn(TITLE, front)

    def test_arxiv_reference_record_is_not_followed_or_propagated(self):
        urls = []
        def fetch(url):
            urls.append(url)
            return atom().replace(TITLE, "A cited and unrelated paper") if url.startswith(eb.ARXIV) else body({"items": [RECORD]})
        md = MD.replace("arXiv:2101.12345v2", "An earlier paper mentions arXiv:2101.12345v2.")
        result = self.resolve(fetch, {**SEEDS, "doi": []}, md)
        self.assertEqual(len(urls), 2)  # arXiv then title search, no wrong DOI call
        self.assertFalse(any("/works/" in url for url in urls))
        self.assertEqual(result["bibliographic"]["arxiv_id"], "")

    def test_malformed_receipts_fail_closed(self):
        for receipt in ([], "instruction", None, {"protocol_version": eb.PROTOCOL,
                "source_sha256": eb.digest(MD), "responses": ["not a response"]}):
            self.assertIsNone(eb.replay(receipt, MD))

    def test_reference_heading_variants_and_numbered_lists_exclude_identifiers(self):
        for heading in ("## REFERENCES AND NOTES", "REFERENCES", "1. Lieb, E. H. & Robinson, D. W. A paper (1972)."):
            md = f"# {TITLE}\nAlice Smith, Bob Jones\n{heading}\n20. Other, arXiv:1605.01735\n"
            candidates = ip.build_bibliographic_candidates({"arxiv_id": "1605.01735"}, md)
            self.assertEqual(candidates["arxiv_id"], [])
            self.assertNotIn("1605.01735", ip.bibliographic_identity_region(md))
            self.assertEqual(candidates["excluded_identifiers"][0]["reasons"], ["reference_list"])

    def test_numbered_sections_do_not_become_reference_lists(self):
        md = f"# {TITLE}\nAlice Smith, Bob Jones\n1. Introduction\narXiv:2101.12345v2\n"
        candidates = ip.build_bibliographic_candidates({}, md)
        self.assertEqual(candidates["arxiv_id"], ["2101.12345v2"])

    def test_abstract_citation_is_excluded_but_own_header_retained(self):
        md = f"# {TITLE}\nAlice Smith, Bob Jones\n## Abstract\nPrevious solutions (Pfau et. al arxiv:1909.02487v2) differ.\narXiv:2101.12345v2\n"
        candidates = ip.build_bibliographic_candidates({"arxiv_id": "1909.02487v2"}, md)
        self.assertEqual(candidates["arxiv_id"], ["2101.12345v2"])
        self.assertIn("explicit_citation", candidates["excluded_identifiers"][0]["reasons"])

    def test_metadata_hint_without_counterevidence_and_repeated_own_id_survive(self):
        candidates = ip.build_bibliographic_candidates({"arxiv_id": "2101.12345v2"}, "# Title\nAlice Smith\n")
        self.assertEqual(candidates["arxiv_id"], ["2101.12345v2"])
        md = MD + "\nSee arxiv:2101.12345v2 in the bibliography.\n"
        self.assertEqual(ip.build_bibliographic_candidates({}, md)["arxiv_id"], ["2101.12345v2"])

    def test_legacy_arxiv_and_invalid_month_candidate_filter(self):
        md = f"# {TITLE}\nAlice Smith\narXiv:cond-mat/0407066v2\nDOI: 10.1098/rspa.2023.0218\n"
        self.assertEqual(ip.build_bibliographic_candidates({}, md)["arxiv_id"], ["cond-mat/0407066v2"])
        self.assertEqual(ip.build_bibliographic_candidates({"arxiv_id": "2023.0218"}, "# Test\n")["arxiv_id"], [])

    def test_cache_execution_cost_is_zero_but_receipt_cost_survives(self):
        initial = self.resolve()
        cached = self.resolve(lambda url: self.fail("no network on cache hit"))
        self.assertEqual(initial["execution"]["request_count"], 1)
        self.assertEqual(cached["execution"]["request_count"], 0)
        self.assertEqual(cached["receipt"], initial["receipt"])
        with patch.object(ip, "REPO", self.root):
            state = {"bibliography_source_policy": "free_external_first", "extract_dir": ".",
                     "bibliographic_meta": initial["bibliographic"]}
            ip.prepare_external_bibliography(state, MD)
            telemetry = state["telemetry"]["external_bibliography"]
            self.assertEqual(telemetry["request_count"], 0)
            self.assertEqual(telemetry["source_fetch"]["request_count"], 1)
            self.assertTrue(telemetry["cache_hit"])

    def test_doi_less_conference_fallback_has_year_precision_and_stable_ids(self):
        result = self.conference()
        bib = result["bibliographic"]
        self.assertEqual(result["execution"]["request_count"], 2)
        self.assertEqual(bib["doi"], "")
        self.assertEqual(bib["venue"], "Quantum Research Conference")
        self.assertEqual(bib["publication_dates"], {"published": [2022]})
        self.assertEqual(bib["bibliographic_identifiers"]["dblp"], "conf/quantum/SmithJones22")
        self.assertEqual(bib["source_content_version"], "not_verified")

    def test_conference_requires_formal_type_identity_and_dblp_record(self):
        variants = [
            {**CONFERENCE, "publicationTypes": ["Preprint"]},
            {**CONFERENCE, "publicationTypes": "Conference"},
            {**CONFERENCE, "publicationVenue": {"type": "repository", "name": "arXiv.org"}},
            {**CONFERENCE, "publicationVenue": {"type": "conference", "name": "arXiv.org"}},
            {**CONFERENCE, "externalIds": {"DBLP": "journals/corr/abs-2101-12345"}},
            {**CONFERENCE, "externalIds": {"DOI": "10.48550/arXiv.2101.12345", "DBLP": "conf/x/paper"}},
            {**CONFERENCE, "year": None}, {**CONFERENCE, "paperId": "bad id"},
            {**CONFERENCE, "title": "An unrelated quantum article"},
            {**CONFERENCE, "authors": [{"name": "A. Other"}]},
        ]
        for record in variants:
            with self.subTest(record=record):
                self.cache.unlink(missing_ok=True)
                self.assertIsNone(self.conference([record])["bibliographic"])

    def test_conference_ambiguity_pagination_and_malformed_results_fail_closed(self):
        for records, extra in [
            ([CONFERENCE, {**CONFERENCE, "paperId": "b" * 40}], {}),
            ([CONFERENCE], {"token": "next-page"}),
            ([CONFERENCE], {"total": 2}),
            ([CONFERENCE, "malformed"], {}),
            ([CONFERENCE] * (eb.MAX_BULK_RESULTS + 1), {}),
        ]:
            self.cache.unlink(missing_ok=True)
            self.assertIsNone(self.conference(records, **extra)["bibliographic"])

    def test_fallback_does_not_run_after_crossref_success(self):
        urls = []
        result = self.resolve(lambda url: urls.append(url) or body())
        self.assertTrue(result["bibliographic"])
        self.assertEqual(len(urls), 1)
        self.assertFalse(any(url.startswith(eb.SEMANTIC_SCHOLAR) for url in urls))

    def test_failed_services_keep_four_request_budget_and_no_retry(self):
        urls = []
        def fail(url):
            urls.append(url)
            raise TimeoutError()
        result = self.resolve(fail)
        self.assertEqual(len(urls), 4)
        self.assertEqual(len(set(urls)), 4)
        self.assertIsNone(result["bibliographic"])
        again = self.resolve(lambda url: self.fail("failed transaction must remain cached"))
        self.assertEqual(again["execution"]["request_count"], 0)

    def test_conference_shared_backend_lock_and_raw_receipt_validator(self):
        bib = self.conference()["bibliographic"]
        (self.root / "paper.md").write_text(MD)
        ip.persist_bibliographic_metadata(self.root, bib)
        for backend in ("agent", "api"):
            with patch.object(ip, "ingest_mode", return_value=backend), \
                    patch.object(ip, "call_json", side_effect=AssertionError("no bibliography LLM")):
                result = ip.review_bibliographic_metadata(bib, MD, agent_workspace=True)
                self.assertTrue(result["ok"])
                self.assertEqual(result["decision"]["fields"]["doi"], "")
        with patch.object(check, "REPO", self.root):
            self.assertFalse(check.check_bibliographic_consistency("academic/wiki/papers/test.md", {
                "type": "paper-summary", "sources": ["paper.md"], "date": "2022", "venue": bib["venue"],
            }))

    def test_indexed_author_abbreviations_not_invented_or_expanded(self):
        record = {**CONFERENCE, "authors": [{"name": "A. Smith"}, {"name": "B. Jones"}]}
        bib = self.conference([record])["bibliographic"]
        self.assertEqual(bib["authors"], ["A. Smith", "B. Jones"])
        self.assertEqual(bib["author_name_precision"], "as_indexed")

    def test_fallback_endpoint_allowlist_is_exact(self):
        self.assertTrue(eb.allowed_url(eb.SEMANTIC_SCHOLAR + "?query=example"))
        for url in ("https://api.semanticscholar.org/graph/v1/paper/search",
                    "https://api.semanticscholar.org/graph/v1/author/123",
                    "https://api.semanticscholar.org.attacker.test/graph/v1/paper/search/bulk"):
            self.assertFalse(eb.allowed_url(url))

    def test_rate_limit_is_recorded_without_retry_or_message_copy(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if url.startswith(eb.SEMANTIC_SCHOLAR):
                raise urllib.error.HTTPError(url, 429, "apply for a key", {}, None)
            return body({"items": []})
        result = self.resolve(fetch, {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["receipt"]["errors"][0]["http_status"], 429)
        self.assertNotIn("apply for a key", json.dumps(result))
        cached = self.resolve(lambda url: self.fail("rate limit must remain cached"),
                              {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertEqual(cached["execution"]["request_count"], 0)

    def test_malformed_crossref_search_does_not_partially_lock(self):
        result = self.resolve(lambda url: body({"items": [RECORD, "invalid"]}),
                              {**SEEDS, "doi": [], "arxiv_id": []})
        self.assertIsNone(result["bibliographic"])


if __name__ == "__main__":
    unittest.main()

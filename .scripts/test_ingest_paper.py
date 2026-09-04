#!/usr/bin/env python3
"""ingest_paper.py 纯代码函数回归测试。"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("ingest_paper.py")
spec = importlib.util.spec_from_file_location("ingest_paper", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_paper_id_generation():
    md = "# Light cone tensor network and time evolution\n\nMiguel Frías-Pérez, Mari Carmen Bañuls\n\nPhys. Rev. B 106, 115117 (2022)\n"
    pid = module.generate_paper_id(md)
    assert pid == "frias-perez-2022-light-cone-tensor-network", f"got {pid}"


def test_paper_id_prefers_pdf_bibliographic_year_over_citation_year():
    md = "# FEVER: a Large-scale Dataset\n\nJames Thorne\n\nDagan et al. (2009) introduced NLI.\n"
    pid = module.generate_paper_id(md, year_hint="2018")
    assert pid == "thorne-2018-fever-large-scale-dataset", f"got {pid}"


def test_extract_title_from_md_strips_aps_volume_header_prefix():
    md = "# PHYSICAL REVIEW B 96, 195145 (2017) Machine learning topological states\n"
    assert module.extract_title_from_md(md) == "Machine learning topological states"


def test_chinese_paper_id_uses_stable_unicode_components():
    pid = module.generate_paper_id(
        "# CCCF专题导言初排版\n\n张鹏\n",
        year_hint="2023",
        title_hint="CCCF专题导言初排版",
        authors_hint=["张鹏"],
    )
    assert pid == "张鹏-2023-cccf专题导言初排版", f"got {pid}"
    assert pid != "paper-2023-paper"


def test_agent_wiki_handoff_resumes_from_declared_output():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        extract_dir = root / "temp" / "inbox-extract" / "agent-wiki"
        extract_dir.mkdir(parents=True)
        (extract_dir / "paper.md").write_text("# Test Paper\n\n张鹏\n", encoding="utf-8")
        (extract_dir / "skeleton.md").write_text("---\ntitle: Test Paper\n---\n", encoding="utf-8")
        agent_output = extract_dir / "agent-wiki-slots.txt"
        agent_output.write_text(
            "<<<WIKI>>>\n---\ntitle: Test Paper\ntype: paper-summary\n"
            "sources:\n  - placeholder\nsource_type: paper\ndate: 2023\nstatus: final\n---\n"
            "## Navigation\n\n## Content\n\n正文。\n"
            "<<<SLOTS>>>\n三元组:\n本论文 | 涉及 | 测试主题\n",
            encoding="utf-8",
        )
        state = {
            "transaction_id": "agent-wiki",
            "status": "agent_required",
            "pre_handoff_status": "write_wiki",
            "_awaiting_agent_wiki_slots": True,
            "agent_required": True,
            "agent_write_to": str(agent_output.relative_to(root)),
            "extract_dir": str(extract_dir.relative_to(root)),
            "paper_id": "张鹏-2023-test-paper",
            "raw_dir": "academic/raw/references/张鹏-2023-test-paper",
            "wiki_path": "academic/wiki/papers/张鹏-2023-test-paper",
            "bibliographic_meta": {},
            "wiki_retry": 0,
        }
        original_repo = module.REPO
        original_call = module.call_text
        try:
            module.REPO = root
            module.call_text = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("resume must not call LLM"))
            assert module.resume_after_agent_generation(state)
            ok, msg = module.step_write_wiki(state)
        finally:
            module.REPO = original_repo
            module.call_text = original_call
        assert ok, msg
        assert "Test Paper" in state["wiki_content"]
        assert "测试主题" in state["slots_content"]
        assert "_awaiting_agent_wiki_slots" not in state
        assert not state["agent_required"]


def test_agent_required_result_includes_write_to():
    import contextlib
    import io
    state = {
        "status": "agent_required",
        "transaction_id": "agent-output-contract",
        "agent_prompt": "prompt",
        "agent_write_to": "temp/inbox-extract/t/agent-wiki-slots.txt",
    }
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        module.print_result(state)
    payload = __import__("json").loads(output.getvalue())
    assert payload["write_to"] == state["agent_write_to"]


def test_extract_pdf_bibliography_reads_metadata_and_first_page_footer():
    import fitz
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "thorne-2018-fever.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "FEVER: a Large-scale Dataset for Fact Extraction and VERification")
        page.insert_text((72, 100), "Proceedings of NAACL-HLT 2018, pages 809-819")
        page.insert_text((72, 120), "2018 Association for Computational Linguistics")
        doc.set_metadata({
            "title": "FEVER: a Large-scale Dataset for Fact Extraction and VERification",
            "author": "James Thorne ; Andreas Vlachos",
            "subject": "N18-1 2018",
            "creationDate": "D:20180511125549-07'00'",
        })
        doc.save(pdf_path)
        doc.close()
        result = module.extract_pdf_bibliography(pdf_path)
    assert result["title"].startswith("FEVER:")
    assert result["authors"] == ["James Thorne", "Andreas Vlachos"]
    assert result["year"] == "2018"
    assert result["venue"] == "NAACL-HLT 2018"
    assert result["evidence"]["year"] == "pdf_first_page"
    assert result["evidence"]["venue"] == "pdf_first_page"


def test_extract_pdf_bibliography_prefers_published_year_and_aps_doi_venue():
    import fitz
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "(Received 18 December 2012; published 3 July 2013)")
        page.insert_text((72, 100), "DOI: 10.1103/PhysRevB.88.035103")
        doc.save(pdf_path)
        doc.close()
        result = module.extract_pdf_bibliography(pdf_path)
    assert result["year"] == "2013"
    assert result["evidence"]["year"] == "pdf_first_page.published"
    assert result["venue"] == "Phys. Rev. B 88, 035103 (2013)"
    assert result["evidence"]["venue"] == "doi_aps"


def test_extract_pdf_bibliography_reads_published_conference_venue():
    import fitz
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Published as a conference paper at ICLR 2025")
        doc.save(pdf_path)
        doc.close()
        result = module.extract_pdf_bibliography(pdf_path)
    assert result["year"] == "2025"
    assert result["venue"] == "ICLR 2025"
    assert result["evidence"]["venue"] == "pdf_first_page"


def test_extract_pdf_bibliography_reads_iop_citation_venue():
    import fitz
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "paper.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (72, 72),
            "To cite this article: Antonio Acin et al 2018 New J. Phys. 20 080201",
        )
        doc.save(pdf_path)
        doc.close()
        result = module.extract_pdf_bibliography(pdf_path)
    assert result["year"] == "2018"
    assert result["venue"] == "New J. Phys. 20, 080201 (2018)"
    assert result["evidence"]["venue"] == "pdf_first_page"


def test_repeated_title_author_block_extends_candidates_and_quality_gate():
    md = """# Roadmap Paper

To cite this article: Alice Example et al 2024 New J. Phys. 20 123456

# Roadmap Paper

Alice Example<sup>1</sup>, Bob Builder<sup>2</sup> and Carol Researcher<sup>3</sup>

Example University, Department of Physics
"""
    candidates = module.build_bibliographic_candidates(
        {"title": "Roadmap Paper", "authors": ["Alice Example"], "year": "2024"},
        md,
    )
    assert {"Alice Example", "Bob Builder", "Carol Researcher"}.issubset(candidates["authors"])
    warnings = module.bibliographic_quality_warnings(
        {"title": "Roadmap Paper", "authors": ["Alice Example"]}, md,
    )
    assert [item["issue"] for item in warnings] == ["bibliographic_authors_incomplete"]


def test_repeated_title_author_block_skips_publisher_heading():
    md = """To cite this article: Tai-Danae Bradley et al 2020 Journal 1 035008

# Modeling sequences with quantum states: a look under the hood

## OPEN ACCESS

Tai-Danae Bradley<sup>1</sup>, E M Stoudenmire<sup>2</sup> and John Terilla<sup>3</sup>\ue9d9

RECEIVED 17 October 2019
"""
    expected = ["Tai-Danae Bradley", "E M Stoudenmire", "John Terilla"]
    assert module._repeated_title_authors(md) == expected
    candidates = module.build_bibliographic_candidates({
        "title": "Modeling sequences with quantum states: a look under the hood",
        "authors": ["Tai-Danae Bradley ,E M Stoudenmire ,John Terilla"],
        "year": "2020",
    }, md)
    assert all(author in candidates["authors"] for author in expected)


def test_bibliographic_candidates_include_full_acl_first_page_venues():
    cases = [
        (
            "Findings of the Association for Computational Linguistics: ACL 2024, pages 11375-11388",
            "",
            "Findings of the Association for Computational Linguistics: ACL 2024",
        ),
        (
            "Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics "
            "(Volume 1: Long Papers), pages 6036-6063",
            "the 63rd Annual Meeting of the Association for Computational Linguistics "
            "(Volume 1: Long Papers)",
            "Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics "
            "(Volume 1: Long Papers)",
        ),
    ]
    for evidence, canonical_venue, expected in cases:
        candidates = module.build_bibliographic_candidates({
            "venue": canonical_venue,
            "first_page_evidence": [evidence],
        }, "# Test paper\n")
        assert expected in candidates["venue"], candidates["venue"]


def test_bibliographic_candidates_exclude_reference_only_arxiv_id():
    md_text = """# Paper without a preprint identifier

Alice Example

## References

Related Work, arXiv:1701.07056.
"""
    candidates = module.build_bibliographic_candidates({}, md_text)
    assert candidates["arxiv_id"] == []


def test_extract_pdf_bibliography_reads_iop_wrapper_second_page_header():
    import fitz
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / "paper.pdf"
        doc = fitz.open()
        wrapper = doc.new_page()
        wrapper.insert_text((72, 72), "This content has been downloaded from IOPscience.")
        wrapper.insert_text((72, 92), "Please scroll down to see the full text.")
        article = doc.new_page()
        article.insert_text((72, 72), "EPL, 104 (2013) 57009")
        article.insert_text((72, 92), "doi: 10.1209/0295-5075/104/57009")
        article.insert_text((72, 112), "published online 23 December 2013")
        doc.save(pdf_path)
        doc.close()
        result = module.extract_pdf_bibliography(pdf_path)
    assert result["year"] == "2013"
    assert result["venue"] == "EPL"
    assert result["doi"] == "10.1209/0295-5075/104/57009"
    assert result["evidence"]["venue"] == "pdf_front_matter"
    assert result["evidence"]["year"] == "pdf_front_matter.published"


def test_bibliographic_review_schema_accepts_example():
    review = {
        "doc_type": "paper",
        "review_status": "corrected",
        "bibliographic": {
            "title": {"value": "Pyrochlore S=1/2 quantum spin liquid", "evidence": "paper.md#L1", "status": "corrected"},
            "authors": {
                "value": ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"],
                "evidence": "paper.md#L3",
                "rejected": ["Institutfur Theorie", "Kondensierten Materie"],
                "status": "corrected",
            },
            "year": {"value": "2011", "evidence": "paper.md#L7", "kind": "published", "status": "confirmed"},
            "venue": {"value": "Phys. Rev. B 84, 100406 (2011)", "evidence": "doi_aps", "status": "confirmed"},
            "doi": {"value": "10.1103/PhysRevB.84.100406", "evidence": "paper.md#L9", "status": "confirmed"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [],
        "review_notes": ["机构片段已从作者列表中剔除"],
    }
    assert module.bibliographic_review_schema(review)


def test_salvage_slots_without_delimiter_accepts_misplaced_wrapper_only():
    content = "三元组:\n本论文 | 核心方法 | 张量网络\n\n<<<SLOTS>>>"
    assert module.salvage_slots_without_delimiter(content).startswith("三元组:")
    malformed = "<<<SLOTS>\n三元组:\n本论文 | 核心方法 | 张量网络\n<<<SLOTS>"
    assert module.salvage_slots_without_delimiter(malformed).startswith("三元组:")
    assert module.salvage_slots_without_delimiter("普通说明文字") == ""


def test_normalize_slots_preserves_two_column_concept_glosses():
    source = """三元组:
本论文 | 核心方法 | 影响泛函influence functional
概念说明:
影响泛函influence functional | 本文将其表示为时间方向的矩阵乘积态。
"""
    normalized = module.normalize_slots(source)
    assert "概念说明:\n影响泛函influence functional | 本文将其表示为时间方向的矩阵乘积态。" in normalized


def test_normalize_slots_keeps_legacy_and_current_section_headers():
    source = "核心方法: 张量网络\n概念说明:\n张量网络 | 本文用于表示量子态。\n"
    normalized = module.normalize_slots(source)
    assert "核心方法:\n张量网络" in normalized
    assert "概念说明:\n张量网络 | 本文用于表示量子态。" in normalized
    assert module.LEGACY_SLOT_SECTIONS | module.CURRENT_SLOT_SECTIONS == module.KNOWN_SECTIONS


def test_slots_prompts_assign_local_glosses_to_worker_without_locators():
    standalone = module.build_slots_prompt("# Example")
    combined = module.build_agent_wiki_slots_prompt(
        Path("/tmp/paper.md"), "sources:\n  - academic/raw/example/paper.md\n"
    )
    for prompt in (standalone, combined):
        assert "概念说明:" in prompt
        assert "locator 由程序" in prompt or "程序会从 wiki section 脚注" in prompt


def test_paper_semantic_contract_is_shared_by_api_and_agent_prompts():
    contract = module.build_paper_semantic_contract()
    standalone = module.build_slots_prompt("# Example")
    combined = module.build_agent_wiki_slots_prompt(
        Path("/tmp/paper.md"), "sources:\n  - academic/raw/example/paper.md\n"
    )
    assert module.PAPER_SEMANTIC_CONTRACT_VERSION in contract
    assert contract in standalone
    assert contract in combined
    for prompt in (standalone, combined):
        assert "研究基础`、`核心方法`、`对比方法`指向可复用的规范概念名" in prompt
        assert "核心创新点`、`局限性`、`未来展望`指向论文明确陈述的完整、简洁 proposition" in prompt
        assert "旧式“叙述节点 + 拆分边”不再生成" in prompt
        assert "必须输出 4 组三元组" not in prompt


def test_bibliographic_review_merges_and_rejects_german_institutions():
    bibliography = {
        "title": "Pyrochlore S=1/2 quantum spin liquid",
        "authors": ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"],
        "year": "2011",
        "venue": "Phys. Rev. B 84, 100406 (2011)",
        "doi": "10.1103/PhysRevB.84.100406",
        "arxiv_id": "",
        "evidence": {"year": "pdf_first_page.published", "venue": "doi_aps"},
    }
    md_text = """# Pyrochlore S=1/2 quantum spin liquid

Johannes Reuther, Ronny Thomale, Simon Trebst

Institutfur Theorie der Kondensierten Materie, Karlsruhe Institute of Technology

Phys. Rev. B 84, 100406 (2011)

DOI: 10.1103/PhysRevB.84.100406
"""
    candidates = module.build_bibliographic_candidates(bibliography, md_text)
    assert "Institutfur Theorie" in candidates["authors"]
    review = {
        "doc_type": "paper",
        "review_status": "corrected",
        "bibliographic": {
            "title": {"value": "Pyrochlore S=1/2 quantum spin liquid", "evidence": "paper.md#L1", "status": "corrected"},
            "authors": {
                "value": ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"],
                "evidence": "paper.md#L3",
                "rejected": ["Institutfur Theorie", "Kondensierten Materie"],
                "status": "corrected",
            },
            "year": {"value": "2011", "evidence": "paper.md#L7", "kind": "published", "status": "confirmed"},
            "venue": {"value": "Phys. Rev. B 84, 100406 (2011)", "evidence": "doi_aps", "status": "confirmed"},
            "doi": {"value": "10.1103/PhysRevB.84.100406", "evidence": "paper.md#L9", "status": "confirmed"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [],
        "review_notes": [],
    }
    merged = module.merge_bibliographic_review(bibliography, review)
    assert merged["authors"] == ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"]
    assert "Institutfur Theorie" in merged["authors_rejected"]
    assert merged["review"]["locked"] is True


def test_bibliographic_normalization_moves_external_affiliation_and_locks_ids():
    review = {
        "bibliographic": {
            "authors": {
                "value": ["Alice Example"],
                "rejected": ["Example University"],
            },
        },
        "review_notes": [],
    }
    candidates = {
        "authors": ["Alice Example"],
        "doi": ["10.1234/example"],
        "arxiv_id": ["2401.01234"],
    }
    changes = module.normalize_bibliographic_review(review, candidates)
    bib = review["bibliographic"]
    assert bib["authors"]["rejected"] == []
    assert "Example University" in review["review_notes"][0]
    assert bib["doi"]["value"] == "10.1234/example"
    assert bib["arxiv_id"]["value"] == "2401.01234"
    assert len(changes) == 3


def _candidate_id_decision(catalog, *, title_id=None):
    fields = catalog["fields"]
    only = lambda field: fields[field][0]["id"] if fields[field] else ""
    selected_title = title_id or only("title")
    return {
        "protocol_version": module.BIBLIOGRAPHIC_DECISION_PROTOCOL,
        "doc_type": "paper",
        "review_status": "clean",
        "selections": {
            "title": {"candidate_id": selected_title, "status": "confirmed"},
            "authors": {
                "accepted_ids": [item["id"] for item in fields["authors"]],
                "rejected_ids": [],
                "proposed": [],
                "status": "confirmed",
            },
            "year": {"candidate_id": only("year"), "kind": "published", "status": "confirmed"},
            "venue": {
                "candidate_id": only("venue"),
                "status": "confirmed" if fields["venue"] else "ambiguous",
            },
            "doi": {
                "candidate_id": only("doi"),
                "status": "confirmed" if fields["doi"] else "ambiguous",
            },
            "arxiv_id": {
                "candidate_id": only("arxiv_id"),
                "status": "confirmed" if fields["arxiv_id"] else "ambiguous",
            },
        },
        "conflicts": [],
        "review_notes": [],
    }


def test_candidate_id_catalog_compiles_without_free_form_values():
    bibliography = {
        "title": "Stable Paper",
        "authors": ["Alice Example", "Example University"],
        "year": "2024",
        "doi": "10.1234/stable",
        "evidence": {"year": "pdf_first_page.published"},
    }
    candidates = {
        "title": ["Stable Paper"],
        "authors": ["Alice Example", "Example University"],
        "year": ["2024"], "venue": [], "doi": ["10.1234/stable"], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(
        candidates, bibliography, "# Stable Paper\n\nAlice Example\n",
    )
    decision = _candidate_id_decision(catalog)
    decision["selections"]["authors"]["accepted_ids"] = ["author-01"]
    decision["selections"]["authors"]["rejected_ids"] = ["author-02"]
    review = module.compile_bibliographic_decision(decision, catalog)
    assert review["bibliographic"]["authors"]["value"] == ["Alice Example"]
    assert review["bibliographic"]["authors"]["rejected"] == ["Example University"]
    assert review["bibliographic"]["doi"]["value"] == "10.1234/stable"

    invalid = _candidate_id_decision(catalog, title_id="title-99")
    try:
        module.compile_bibliographic_decision(invalid, catalog)
    except ValueError as exc:
        assert "未知 candidate_id" in str(exc)
    else:
        raise AssertionError("unknown candidate IDs must be rejected")


def test_candidate_id_v2_compiles_evidence_bound_author_proposals():
    md_text = """# Paper

## OPEN ACCESS

Alice Example<sup>1</sup>, Bob Builder<sup>2</sup> and Carol Researcher<sup>3</sup>
"""
    candidates = {
        "title": ["Paper"], "authors": ["Alice Example, Bob Builder, Carol Researcher"],
        "year": ["2024"], "venue": [], "doi": [], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, {}, md_text)
    decision = _candidate_id_decision(catalog)
    decision["review_status"] = "corrected"
    decision["selections"]["authors"] = {
        "accepted_ids": [],
        "rejected_ids": ["author-01"],
        "proposed": [
            {"value": "Alice Example", "evidence": "paper.md#L5"},
            {"value": "Bob Builder", "evidence": "paper.md#L5"},
            {"value": "Carol Researcher", "evidence": "paper.md#L5"},
        ],
        "status": "corrected",
    }
    review = module.compile_bibliographic_decision(decision, catalog, md_text)
    assert review["bibliographic"]["authors"]["value"] == [
        "Alice Example", "Bob Builder", "Carol Researcher",
    ]
    assert module.validate_bibliographic_review(review, candidates, md_text) == []


def test_candidate_id_v2_rejects_unverified_or_compound_author_proposals():
    md_text = "# Paper\n\nAlice Example and Bob Builder\n"
    candidates = {
        "title": ["Paper"], "authors": [], "year": ["2024"],
        "venue": [], "doi": [], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, {}, md_text)
    for proposal, expected in [
        ({"value": "Made Up Person", "evidence": "paper.md#L3"}, "未在"),
        ({"value": "Alice Example and Bob Builder", "evidence": "paper.md#L3"}, "multiple authors"),
        ({"value": "Alice Example, Bob Builder", "evidence": "paper.md#L3"}, "multiple authors"),
        ({"value": "Alice Example", "evidence": "paper.md#L41"}, "前 40 行"),
    ]:
        decision = _candidate_id_decision(catalog)
        decision["review_status"] = "corrected"
        decision["selections"]["authors"] = {
            "accepted_ids": [], "rejected_ids": [], "proposed": [proposal],
            "status": "corrected",
        }
        try:
            module.compile_bibliographic_decision(decision, catalog, md_text)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid evidence-bound author proposal must be rejected")

    decision = _candidate_id_decision(catalog)
    decision["review_status"] = "corrected"
    decision["selections"]["authors"] = {
        "accepted_ids": [], "rejected_ids": [],
        "proposed": [
            {"value": "Bob Builder", "evidence": "paper.md#L3"},
            {"value": "Alice Example", "evidence": "paper.md#L3"},
        ],
        "status": "corrected",
    }
    try:
        module.compile_bibliographic_decision(decision, catalog, md_text)
    except ValueError as exc:
        assert "顺序" in str(exc)
    else:
        raise AssertionError("out-of-order author proposals must be rejected")


def test_bibliographic_validation_rejects_compound_candidate_selected_as_one_author():
    md_text = "# Paper\n\nAlice Example, Bob Builder, Carol Researcher\n"
    candidates = {
        "title": ["Paper"], "authors": ["Alice Example, Bob Builder, Carol Researcher"],
        "year": ["2024"], "venue": [], "doi": [], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, {}, md_text)
    decision = _candidate_id_decision(catalog)
    review = module.compile_bibliographic_decision(decision, catalog, md_text)
    errors = module.validate_bibliographic_review(review, candidates, md_text)
    assert any("不是单一人物" in error for error in errors)


def test_bibliographic_worker_manual_required_hands_off_to_agent():
    bibliography = {"title": "Paper", "authors": [], "year": "2024"}
    candidates = {
        "doc_type": "paper", "title": ["Paper"], "authors": [], "year": ["2024"],
        "venue": [], "doi": [], "arxiv_id": [], "evidence": {},
        "first_page_evidence": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, bibliography, "# Paper\n")
    decision = _candidate_id_decision(catalog)
    decision["review_status"] = "manual_required"
    decision["selections"]["authors"] = {
        "accepted_ids": [], "rejected_ids": [], "proposed": [], "status": "ambiguous",
    }
    originals = module.REPO, module.build_bibliographic_candidates, module.call_json
    with tempfile.TemporaryDirectory() as directory:
        module.REPO = Path(directory)
        module.build_bibliographic_candidates = lambda *_args: candidates
        module.call_json = lambda *_args, **_kwargs: {
            "ok": True, "parsed": decision, "history": [{"attempt": 1}],
        }
        try:
            result = module.review_bibliographic_metadata(
                bibliography, "# Paper\n", "txn-manual")
        finally:
            module.REPO, module.build_bibliographic_candidates, module.call_json = originals
    assert result["status"] == "agent_required"
    assert "candidate-id-v2" in result["agent_prompt"]
    assert result["decision"]["review_status"] == "manual_required"
    assert result["worker"]["api_called"] is True


def test_empty_optional_catalog_is_compiled_by_program():
    candidates = {
        "title": ["Stable Paper"], "authors": ["Alice Example"], "year": ["2024"],
        "venue": [], "doi": [], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, {}, "# Stable Paper\n")
    decision = _candidate_id_decision(catalog)
    for field in ("venue", "doi", "arxiv_id"):
        decision["selections"][field]["status"] = "confirmed"
    review = module.compile_bibliographic_decision(decision, catalog)
    for field in ("venue", "doi", "arxiv_id"):
        assert review["bibliographic"][field] == {
            "value": "", "evidence": "", "status": "ambiguous",
        }


def test_empty_selection_with_candidates_still_requires_ambiguous_status():
    candidates = {
        "title": ["Stable Paper"], "authors": ["Alice Example"], "year": ["2024"],
        "venue": ["ICLR 2024"], "doi": [], "arxiv_id": [],
    }
    catalog = module.build_bibliographic_candidate_catalog(candidates, {}, "# Stable Paper\n")
    decision = _candidate_id_decision(catalog)
    decision["selections"]["venue"] = {"candidate_id": "", "status": "confirmed"}
    try:
        module.compile_bibliographic_decision(decision, catalog)
    except ValueError as exc:
        assert "venue 未选择 candidate_id" in str(exc)
    else:
        raise AssertionError("non-empty candidate catalogs still require an explicit decision")


def test_bibliographic_fast_path_skips_worker_call():
    bibliography = {
        "title": "Stable Paper", "authors": ["Alice Example"], "year": "2024",
        "venue": "Journal", "doi": "10.1234/stable", "arxiv_id": "",
        "evidence": {"year": "pdf_first_page.published"},
    }
    candidates = {
        "doc_type": "paper", "title": ["Stable Paper"], "authors": ["Alice Example"],
        "year": ["2024"], "venue": ["Journal"], "doi": ["10.1234/stable"],
        "arxiv_id": [], "evidence": {}, "first_page_evidence": [],
    }
    originals = module.build_bibliographic_candidates, module.call_json
    module.build_bibliographic_candidates = lambda *_args: candidates
    module.call_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("deterministic fast path must not call worker"))
    try:
        result = module.review_bibliographic_metadata(
            bibliography, "# Stable Paper\n\nAlice Example\n", "txn-fast")
    finally:
        module.build_bibliographic_candidates, module.call_json = originals
    assert result["ok"] is True
    assert result["worker"]["skipped"] is True
    assert result["worker"]["skip_reason"] == "deterministic_fast_path"
    assert result["worker"]["api_called"] is False


def test_bibliographic_front_matter_fast_path_skips_worker_without_pdf_authors():
    bibliography = {
        "title": "Stable Paper", "authors": [], "year": "2024",
        "venue": "Journal", "doi": "10.1234/stable", "arxiv_id": "",
        "evidence": {"title": "pdf_metadata.title", "year": "pdf_first_page.published"},
    }
    md_text = "# Stable Paper\n\nAlice Example, Bob Example\n\n## Abstract\nBody text.\n"
    original_call = module.call_json
    module.call_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("front-matter deterministic fast path must not call worker"))
    try:
        result = module.review_bibliographic_metadata(
            bibliography, md_text, "txn-front-matter-fast")
    finally:
        module.call_json = original_call
    assert result["ok"] is True
    assert result["bibliographic"]["authors"] == ["Alice Example", "Bob Example"]
    assert result["worker"]["skip_reason"] == "deterministic_front_matter_fast_path"
    assert result["worker"]["api_called"] is False


def test_bibliographic_review_view_stops_before_abstract_body():
    md_text = (
        "# Stable Paper\n\nAlice Example, Bob Example\n\n"
        "## Abstract\n" + "unrelated abstract sentence " * 300
        + "\nPublished 1 January 2024\n"
    )
    title_view, evidence_view = module._paper_md_review_view(md_text)
    assert "Alice Example" in title_view
    assert "unrelated abstract sentence" not in title_view
    assert "Published 1 January 2024" in evidence_view


def test_bibliographic_worker_cache_prevents_resume_recall():
    with tempfile.TemporaryDirectory() as directory:
        bibliography = {
            "title": "Stable Paper", "authors": ["Alice Example"], "year": "2024",
            "doi": "10.1234/stable", "evidence": {"year": "pdf_first_page.published"},
        }
        candidates = {
            "doc_type": "paper", "title": ["Stable Paper", "Stable Paper Extended"],
            "authors": ["Alice Example"], "year": ["2024"], "venue": [],
            "doi": ["10.1234/stable"], "arxiv_id": [], "evidence": {},
            "first_page_evidence": [],
        }
        original_repo = module.REPO
        original_candidates = module.build_bibliographic_candidates
        original_call = module.call_json
        calls = []
        module.REPO = Path(directory)
        module.build_bibliographic_candidates = lambda *_args: candidates

        def worker(prompt, schema, **_kwargs):
            catalog = module.build_bibliographic_candidate_catalog(
                candidates, bibliography, "# Stable Paper\n\nAlice Example\n",
            )
            decision = _candidate_id_decision(catalog)
            calls.append(prompt)
            return {"ok": True, "parsed": decision, "history": [{"attempt": 1}]}

        module.call_json = worker
        try:
            first = module.review_bibliographic_metadata(
                bibliography, "# Stable Paper\n\nAlice Example\n", "txn-cache")
            module.call_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("cache hit must not recall worker"))
            second = module.review_bibliographic_metadata(
                bibliography, "# Stable Paper\n\nAlice Example\n", "txn-cache")
        finally:
            module.REPO = original_repo
            module.build_bibliographic_candidates = original_candidates
            module.call_json = original_call
    assert first["ok"] is True and first["worker"]["api_called"] is True
    assert second["ok"] is True and second["worker"]["cache_hit"] is True
    assert second["worker"]["api_called"] is False
    assert len(calls) == 1


def test_validate_bibliographic_review_rejects_invented_author():
    bibliography = {
        "title": "Pyrochlore S=1/2 quantum spin liquid",
        "authors": ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"],
        "year": "2011",
        "venue": "Phys. Rev. B 84, 100406 (2011)",
        "doi": "10.1103/PhysRevB.84.100406",
        "arxiv_id": "",
        "evidence": {},
    }
    md_text = """# Pyrochlore S=1/2 quantum spin liquid

Johannes Reuther, Ronny Thomale, Simon Trebst

Institutfur Theorie der Kondensierten Materie, Karlsruhe Institute of Technology

Phys. Rev. B 84, 100406 (2011)

DOI: 10.1103/PhysRevB.84.100406
"""
    candidates = module.build_bibliographic_candidates(bibliography, md_text)
    review = {
        "doc_type": "paper",
        "review_status": "corrected",
        "bibliographic": {
            "title": {"value": "Pyrochlore S=1/2 quantum spin liquid", "evidence": "paper.md#L1", "status": "confirmed"},
            "authors": {
                "value": ["Johannes Reuther", "Made Up Person", "Simon Trebst"],
                "evidence": "paper.md#L3",
                "rejected": [],
                "status": "corrected",
            },
            "year": {"value": "2011", "evidence": "paper.md#L7", "kind": "published", "status": "confirmed"},
            "venue": {"value": "Phys. Rev. B 84, 100406 (2011)", "evidence": "doi_aps", "status": "confirmed"},
            "doi": {"value": "10.1103/PhysRevB.84.100406", "evidence": "paper.md#L9", "status": "confirmed"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [],
        "review_notes": [],
    }
    errors = module.validate_bibliographic_review(review, candidates)
    assert any("authors" in error and "候选之外" in error for error in errors)


def test_validate_bibliographic_review_accepts_evidence_bound_affiliation_rejections():
    affiliation = "Foundation Model Research Center, Institute of Automation, CAS"
    md_text = f"# Paper\n\nAlice, Bob\n\n{affiliation}\n"
    candidates = {
        "title": ["Paper"], "authors": ["Alice", "Bob"], "year": [],
        "venue": [], "doi": [], "arxiv_id": [],
    }
    review = {
        "doc_type": "paper", "review_status": "clean",
        "bibliographic": {
            "title": {"value": "Paper", "evidence": "paper.md#L1", "status": "confirmed"},
            "authors": {
                "value": ["Alice", "Bob"], "evidence": "paper.md#L3, paper.md#L5",
                "rejected": [affiliation], "status": "confirmed",
            },
            "year": {"value": "", "evidence": "", "kind": "unknown", "status": "ambiguous"},
            "venue": {"value": "", "evidence": "", "status": "ambiguous"},
            "doi": {"value": "", "evidence": "", "status": "ambiguous"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [], "review_notes": [],
    }
    assert module.validate_bibliographic_review(review, candidates, md_text) == []
    review["bibliographic"]["authors"]["rejected"] = ["Invented Institute"]
    errors = module.validate_bibliographic_review(review, candidates, md_text)
    assert any("rejected 包含" in error for error in errors)


def test_resume_stored_bibliographic_validation_error_without_model_call():
    affiliation = "Foundation Model Research Center, Institute of Automation, CAS"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        extract_dir = root / "temp/inbox-extract/txn"
        extract_dir.mkdir(parents=True)
        (extract_dir / "paper.md").write_text(
            f"# Paper\n\nAlice, Bob\n\n{affiliation}\n", encoding="utf-8",
        )
        candidates = {
            "title": ["Paper"], "authors": ["Alice", "Bob"], "year": ["2025"],
            "venue": [], "doi": [], "arxiv_id": [],
        }
        review = {
            "doc_type": "paper", "review_status": "clean",
            "bibliographic": {
                "title": {"value": "Paper", "evidence": "paper.md#L1", "status": "confirmed"},
                "authors": {
                    "value": ["Alice", "Bob"], "evidence": "paper.md#L3, paper.md#L5",
                    "rejected": [affiliation], "status": "confirmed",
                },
                "year": {"value": "2025", "evidence": "candidate", "kind": "published", "status": "confirmed"},
                "venue": {"value": "", "evidence": "", "status": "ambiguous"},
                "doi": {"value": "", "evidence": "", "status": "ambiguous"},
                "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
            },
            "conflicts": [], "review_notes": [],
        }
        state = {
            "status": "bibliographic_review_required",
            "extract_dir": "temp/inbox-extract/txn",
            "bibliographic_meta": {},
            "bibliographic_review_required": True,
            "bibliographic_review": {
                "status": "validation_error", "review": review, "candidates": candidates,
            },
            "errors": ["old validation error"],
        }
        old_repo = module.REPO
        module.REPO = root
        try:
            assert module._resume_bibliographic_review(state)
        finally:
            module.REPO = old_repo
        assert state["status"] == "write_wiki"
        assert state["bibliographic_review"]["status"] == "ok"
        assert state["bibliographic_review_required"] is False
        assert state["errors"] == []


def test_resume_candidate_id_decision_compiles_and_caches_without_worker():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        extract_dir = root / "temp/inbox-extract/txn-id"
        extract_dir.mkdir(parents=True)
        md_text = "# Paper\n\nAlice Example\n\nDOI: 10.1234/paper\n"
        (extract_dir / "paper.md").write_text(md_text, encoding="utf-8")
        bibliography = {
            "title": "Paper", "authors": ["Alice Example"], "year": "2025",
            "doi": "10.1234/paper", "evidence": {"year": "pdf_first_page.published"},
        }
        candidates = {
            "title": ["Paper"], "authors": ["Alice Example"], "year": ["2025"],
            "venue": [], "doi": ["10.1234/paper"], "arxiv_id": [],
        }
        catalog = module.build_bibliographic_candidate_catalog(
            candidates, bibliography, md_text,
        )
        decision = _candidate_id_decision(catalog)
        (extract_dir / "bibliographic-review.json").write_text(
            module.json.dumps(decision), encoding="utf-8",
        )
        state = {
            "transaction_id": "txn-id",
            "status": "agent_required",
            "extract_dir": "temp/inbox-extract/txn-id",
            "bibliographic_meta": bibliography,
            "bibliographic_review_required": False,
            "bibliographic_review": {
                "status": "agent_required", "candidates": candidates, "catalog": catalog,
                "input_hash": module._bibliographic_worker_input_hash(catalog, md_text),
                "worker": {"api_called": False},
            },
            "errors": [],
        }
        old_repo = module.REPO
        module.REPO = root
        try:
            assert module._resume_bibliographic_review(state)
        finally:
            module.REPO = old_repo
        assert state["status"] == "write_wiki"
        assert state["bibliographic_meta"]["authors"] == ["Alice Example"]
        assert state["bibliographic_review"]["decision"] == decision
        assert (root / "temp/inbox-state/txn-id-bibliographic-decision.json").is_file()


def test_validate_bibliographic_review_accepts_superscript_equivalent_title():
    candidates = {
        "title": [
            "Featureless quantum spin liquid, <sup>1</sup>-magnetization plateau state, "
            "and thermodynamics of the spin- <sup>1</sup> model"
        ],
        "authors": [], "year": [], "venue": [], "doi": [], "arxiv_id": [],
    }
    review = {
        "doc_type": "paper",
        "review_status": "clean",
        "bibliographic": {
            "title": {
                "value": "Featureless quantum spin liquid, 1-magnetization plateau state, "
                "and thermodynamics of the spin-1 model",
                "evidence": "paper.md#L1", "status": "confirmed",
            },
            "authors": {"value": [], "evidence": "", "rejected": [], "status": "ambiguous"},
            "year": {"value": "", "evidence": "", "kind": "unknown", "status": "ambiguous"},
            "venue": {"value": "", "evidence": "", "status": "ambiguous"},
            "doi": {"value": "", "evidence": "", "status": "ambiguous"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [], "review_notes": [],
    }
    assert module.validate_bibliographic_review(review, candidates) == []


def test_validate_bibliographic_review_promotes_only_declared_line_evidence():
    md_text = """# 时间矩阵乘积态理论及其应用\\*

彭程 $^{1}$，冉仕举 $^{2}$

Published in Navigation Journal
"""
    candidates = {
        "title": [], "authors": [], "year": [], "venue": [], "doi": [], "arxiv_id": [],
    }
    review = {
        "doc_type": "paper", "review_status": "clean",
        "bibliographic": {
            "title": {"value": "时间矩阵乘积态理论及其应用", "evidence": "paper.md#L1", "status": "confirmed"},
            "authors": {"value": ["彭程", "冉仕举"], "evidence": "paper.md#L3", "rejected": [], "status": "confirmed"},
            "year": {"value": "", "evidence": "", "kind": "unknown", "status": "ambiguous"},
            "venue": {"value": "Navigation Journal", "evidence": "paper.md#L5", "status": "confirmed"},
            "doi": {"value": "", "evidence": "", "status": "ambiguous"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": [], "review_notes": [],
    }
    assert module.validate_bibliographic_review(review, candidates, md_text) == []
    review["bibliographic"]["authors"]["value"].append("Made Up Person")
    errors = module.validate_bibliographic_review(review, candidates, md_text)
    assert any("Made Up Person" in error for error in errors)


def test_bibliographic_schema_accepts_descriptive_string_conflicts():
    review = {
        "doc_type": "paper", "review_status": "corrected",
        "bibliographic": {
            "title": {"value": "Paper", "evidence": "paper.md#L1", "status": "confirmed"},
            "authors": {"value": ["Author"], "evidence": "paper.md#L3", "rejected": [], "status": "confirmed"},
            "year": {"value": "2024", "evidence": "paper.md#L5", "kind": "published", "status": "confirmed"},
            "venue": {"value": "Journal", "evidence": "paper.md#L7", "status": "confirmed"},
            "doi": {"value": "", "evidence": "", "status": "ambiguous"},
            "arxiv_id": {"value": "", "evidence": "", "status": "ambiguous"},
        },
        "conflicts": ["Program author candidate contained an affiliation fragment."],
        "review_notes": [],
    }
    assert module.bibliographic_review_schema(review)


def test_bibliographic_evidence_locator_repairs_unique_exact_venue_hit():
    md_text = """# Article title

To cite: 2024 Quantum Sci. Technol. 9 015008

# Quantum Science and Technology
"""
    review = {"bibliographic": {"venue": {
        "value": "Quantum Science and Technology",
        "evidence": "paper.md#L3",
    }}}
    repairs = module.repair_bibliographic_evidence_locators(review, md_text)
    assert review["bibliographic"]["venue"]["evidence"] == "paper.md#L5"
    assert repairs[0]["action"] == "relocate_unique_exact_evidence"


def test_bibliographic_evidence_locator_repairs_unique_multiline_title_hit():
    md_text = """## Machine learning of chaotic characteristics in classical nonlinear dynamics using

## variational quantum circuit

Authors
"""
    title = (
        "Machine learning of chaotic characteristics in classical nonlinear dynamics "
        "using variational quantum circuit"
    )
    review = {"bibliographic": {"title": {
        "value": title,
        "evidence": "paper.md#L1, paper.md#L3",
    }}}
    repairs = module.repair_bibliographic_evidence_locators(review, md_text)
    assert review["bibliographic"]["title"]["evidence"] == "paper.md#L1-L3"
    assert repairs[0]["action"] == "relocate_unique_exact_evidence"


def test_bibliographic_evidence_locator_does_not_repair_repeated_multiline_title():
    title = "A title split across lines"
    md_text = """## A title split
## across lines

## A title split
## across lines
"""
    review = {"bibliographic": {"title": {
        "value": title,
        "evidence": "paper.md#L99",
    }}}
    repairs = module.repair_bibliographic_evidence_locators(review, md_text)
    assert repairs == []
    assert review["bibliographic"]["title"]["evidence"] == "paper.md#L99"


def test_salvage_slots_accepts_split_wrapper_around_valid_triples():
    text = """<<<
SLOTS>>>
本论文 | 核心方法 | 树状张量网络
树状张量网络 | 应用于 | 图像分类
<<<SLOTS>>>"""
    salvaged = module.salvage_slots_without_delimiter(text)
    assert salvaged.splitlines() == [
        "三元组:",
        "本论文 | 核心方法 | 树状张量网络",
        "树状张量网络 | 应用于 | 图像分类",
    ]


def test_salvage_slots_rejects_unstructured_prose_without_section():
    assert module.salvage_slots_without_delimiter(
        "这里是解释。\n本论文 | 核心方法 | 树状张量网络"
    ) == ""


def test_apply_bibliographic_frontmatter_overrides_locked_authors():
    markdown = ('---\ntitle: "Demo"\nauthors: ["Wrong A", "Wrong B"]\ndate: 2009\nvenue: ""  # <-- LLM 填\nconfidence: high\n---\n'
                '> **作者**：Wrong A, Wrong B | **发表**：错误期刊\n')
    updated = module.apply_bibliographic_frontmatter(
        markdown, {
            "authors": ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"],
            "year": "2011",
            "venue": "Phys. Rev. B 84, 100406 (2011)",
        })
    assert 'authors: ["Johannes Reuther", "Ronny Thomale", "Simon Trebst"]' in updated
    assert "Wrong A" not in updated.split('---', 2)[1]
    assert "> **作者**：Johannes Reuther、Ronny Thomale、Simon Trebst | **发表**：Phys. Rev. B 84, 100406 (2011)" in updated


def test_apply_bibliographic_frontmatter_replaces_markdown_escape_in_title():
    markdown = ('---\ntitle: "时间矩阵乘积态理论及其应用\\*"\ndate: 2019\nvenue: "Demo"\n'
                '---\n# 时间矩阵乘积态理论及其应用\\*\n')
    updated = module.apply_bibliographic_frontmatter(markdown, {
        "title": "时间矩阵乘积态理论及其应用", "year": "2019", "venue": "Demo",
    })
    assert 'title: "时间矩阵乘积态理论及其应用"' in updated
    assert "# 时间矩阵乘积态理论及其应用\n" in updated
    assert "\\*" not in updated


def test_apply_bibliographic_frontmatter_overrides_llm_guess():
    markdown = ('---\ntitle: "Demo"\ndate: 2009\nvenue: ""  # <-- LLM 填\n---\n'
                '> **作者**：A | **发表**：错误期刊\n')
    updated = module.apply_bibliographic_frontmatter(
        markdown, {"year": "2018", "venue": "NAACL-HLT 2018"})
    assert "date: 2018" in updated
    assert 'venue: "NAACL-HLT 2018"' in updated
    assert "2009" not in updated
    assert "LLM 填" not in updated
    assert "**发表**：NAACL-HLT 2018" in updated


def test_salvage_wiki_without_delimiter_requires_complete_structure():
    valid = ("---\ntitle: Demo\n---\n# Demo\n\n## Navigation\nx\n\n"
             "## 研究方向定位\n方向句。[^r1]\n\n## Content\ny\n")
    assert module.salvage_wiki_without_delimiter(valid) == valid.strip()
    assert module.salvage_wiki_without_delimiter("# Demo\n\n## Content\ny") == ""


def test_load_bibliographic_metadata_reuses_archived_source_yaml():
    with tempfile.TemporaryDirectory() as directory:
        raw_dir = Path(directory)
        (raw_dir / "source.yaml").write_text(
            "bibliographic:\n  year: '2018'\n  venue: NAACL-HLT 2018\n", encoding="utf-8")
        result = module.load_bibliographic_metadata(raw_dir)
    assert result["year"] == "2018"
    assert result["venue"] == "NAACL-HLT 2018"


def test_inbox_pdf_paths_are_sorted_and_pdf_only():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inbox = root / "inbox"
        inbox.mkdir()
        (inbox / "zeta.PDF").write_bytes(b"pdf")
        (inbox / "Alpha.pdf").write_bytes(b"pdf")
        (inbox / "notes.txt").write_text("ignore", encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = root
            assert [path.name for path in module.inbox_pdf_paths()] == ["Alpha.pdf", "zeta.PDF"]
        finally:
            module.REPO = original_repo


def test_new_state_for_pdf_uses_relative_source_and_unique_transactions():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inbox = root / "inbox"
        inbox.mkdir()
        source = inbox / "A Paper.pdf"
        source.write_bytes(b"pdf")
        original_repo = module.REPO
        try:
            module.REPO = root
            state = module.new_state_for_pdf(source)
        finally:
            module.REPO = original_repo
    assert state["source"] == "inbox/A Paper.pdf"
    assert state["status"] == "dedup_check"
    assert state["transaction_id"].endswith("-a-paper")


def test_paper_id_no_authors():
    # 无 Published 信息 → arxiv 编号 1234.5678 → 2012（提交年）
    md = "# Some Random Title\n\narXiv:1234.5678\n"
    pid = module.generate_paper_id(md)
    assert pid == "paper-2012-some-random-title", f"got {pid}"


def test_paper_id_no_year():
    md = "# Test Paper\n\nJohn Smith\n\nSome journal\n"
    pid = module.generate_paper_id(md)
    assert "0000" in pid, f"got {pid}"


def test_ensure_unique_disambiguates():
    existing = "frias-perez-2022-light-cone-tensor-network"
    original_repo = module.REPO
    with tempfile.TemporaryDirectory() as directory:
        module.REPO = Path(directory)
        page = module.REPO / "academic" / "wiki" / "papers" / f"{existing}.md"
        page.parent.mkdir(parents=True)
        page.write_text("# Existing\n", encoding="utf-8")
        try:
            unique = module.ensure_unique_paper_id(existing)
        finally:
            module.REPO = original_repo
    assert unique == existing + "-2", f"got {unique}"


def test_ensure_unique_passes_new():
    new_id = "nobody-2099-brand-new-slug"
    assert module.ensure_unique_paper_id(new_id) == new_id


def test_slugify_deaccent():
    assert module.slugify("Frías-Pérez") == "frias-perez"
    assert module.slugify("Guifré Vidal") == "guifre-vidal"
    assert module.slugify("  Multiple   Spaces  ") == "multiple-spaces"


def test_title_to_slug_stops_at_four():
    slug = module.title_to_slug("Light cone tensor network and time evolution")
    assert slug == "light-cone-tensor-network", f"got {slug}"


def test_parse_delimited_wiki():
    text = "<<<WIKI>>>\n---\ntitle: Test\n---\n# Test\n<<<SLOTS>>>\n期刊:\nPhys. Rev. B\n"
    wiki = module.parse_delimited(text, module.WIKI_DELIMITER)
    assert "title: Test" in wiki
    # wiki 段截到 SLOTS 之前
    assert "期刊:" not in wiki


def test_parse_delimited_slots():
    text = "<<<WIKI>>>\n# Just wiki\n<<<SLOTS>>>\n期刊:\nPhys. Rev. B\n"
    slots = module.parse_delimited(text, module.SLOTS_DELIMITER)
    assert "期刊:" in slots
    assert "Just wiki" not in slots


def test_parse_delimited_missing():
    wiki = module.parse_delimited("<<<WIKI>>>\n# Just wiki\n", module.WIKI_DELIMITER)
    assert wiki == "# Just wiki"
    slots = module.parse_delimited("<<<WIKI>>>\n# Just wiki\n", module.SLOTS_DELIMITER)
    assert slots == ""


def test_parse_delimited_strips_closing_delimiter():
    text = "<<<WIKI>>>\n# Just wiki\n<<<WIKI>>>\n<<<SLOTS>>>\n期刊:\nPhys. Rev. B\n"
    wiki = module.parse_delimited(text, module.WIKI_DELIMITER)
    assert wiki == "# Just wiki"
    slots = module.parse_delimited(text, module.SLOTS_DELIMITER)
    assert slots == "期刊:\nPhys. Rev. B"


def test_parse_delimited_empty():
    assert module.parse_delimited("", module.WIKI_DELIMITER) == ""


def test_parse_delimited_strips_slash_closing_delimiter():
    text = "<<<WIKI>>>\n# Just wiki\n<<</WIKI>>>\n<<<SLOTS>>>\n期刊:\nPhys. Rev. B\n"
    wiki = module.parse_delimited(text, module.WIKI_DELIMITER)
    assert wiki == "# Just wiki"
    slots = module.parse_delimited(text, module.SLOTS_DELIMITER)
    assert slots == "期刊:\nPhys. Rev. B"


def test_normalize_slots_splits_inline_header():
    text = "期刊: Phys. Rev. B 106\n三元组:\n本论文 | 研究基础 | MPS\n"
    result = module.normalize_slots(text)
    lines = result.splitlines()
    assert "期刊:" in lines
    assert "Phys. Rev. B 106" in lines
    assert "三元组:" in lines
    assert "本论文 | 研究基础 | MPS" in lines


def test_normalize_slots_keeps_free_edges():
    text = "三元组:\nVidal|所属|Caltech\n"
    result = module.normalize_slots(text)
    assert "Vidal|所属|Caltech" in result.splitlines()


def test_normalize_slots_normalizes_predicate_alias():
    text = "三元组:\n本论文 | 用于 | 张量网络\n"
    result = module.normalize_slots(text)
    assert "本论文 | 应用于 | 张量网络" in result.splitlines()


def test_normalize_slots_wraps_bare_triples_and_drops_protocol_end_marker():
    text = (
        "本论文 | 核心方法 | 多重网格算法multigrid algorithm\n"
        "多重网格算法multigrid algorithm | 改进 | 密度矩阵重整化群\n"
        "<<<END>>>\n"
    )
    result = module.normalize_slots(text)
    assert result.splitlines() == [
        "三元组:",
        "本论文 | 核心方法 | 多重网格算法multigrid algorithm",
        "多重网格算法multigrid algorithm | 改进 | 密度矩阵重整化群",
    ]


def test_graph_parser_recovers_bare_triples_with_structure_diagnostics():
    import graph_ingest

    text = (
        "本论文 | 核心方法 | 多重网格算法\n"
        "多重网格算法 | 改进 | 密度矩阵重整化群\n"
        "<<<END>>>\n"
    )
    sections, diagnostics = graph_ingest.parse_semantic_sections(text)
    assert sections["三元组"] == [
        "本论文 | 核心方法 | 多重网格算法",
        "多重网格算法 | 改进 | 密度矩阵重整化群",
    ]
    assert diagnostics["triple_section_present"] is False
    assert diagnostics["bare_triples_recovered"] == 2
    assert diagnostics["semantic_triple_count"] == 2


def test_normalize_slots_skips_unknown_inline():
    text = "arXiv: 2203.12345\n期刊:\nPRL\n"
    result = module.normalize_slots(text)
    assert "arXiv: 2203.12345" in result.splitlines()


def test_build_wiki_prompt_includes_skeleton():
    prompt = module.build_wiki_prompt("paper text", "skeleton content")
    assert "paper" in prompt and "text" in prompt
    assert "skeleton content" in prompt
    assert "<<<WIKI>>>" in prompt
    assert "<<<SLOTS>>>" not in prompt  # 语义槽移至第二阶段 build_slots_prompt
    assert "对象、条件和比较基准" in prompt
    assert "研究对象、实验设置和适用场景本身不是局限" in prompt
    assert "论文定向摘要" in prompt
    assert "关键证据包" not in prompt
    assert "<<<META>>>" not in prompt


def test_legacy_paper_meta_is_audit_only():
    state = {
        "paper_id": "example-2021-paper",
        "raw_dir": "academic/raw/references/example-2021-paper",
        "wiki_path": "academic/wiki/papers/example-2021-paper",
        "bibliographic_meta": {"year": "2021", "doc_type": "paper"},
    }
    module.record_legacy_paper_meta(state, """<<<META>>>
doc_date: 2022
doc_type: document
<<</META>>>
<<<WIKI>>>
# Example
""")
    assert state["paper_id"] == "example-2021-paper"
    assert state["raw_dir"].endswith("example-2021-paper")
    assert "type_mismatch" not in state
    assert "meta_year_corrected" not in state
    audit = state["legacy_meta_audit"]
    assert audit["ignored"] is True
    assert len(audit["mismatches"]) == 2


def test_build_paper_context_keeps_normal_paper_full():
    assert module.build_paper_context("short paper") == "short paper"


def test_build_paper_context_reduces_long_paper_without_path():
    long_md = "# Title\n\n" + ("x" * (module.FULL_TEXT_MAX_CHARS + 1_000))
    reduced = module.build_paper_context(long_md)
    assert len(reduced) < len(long_md)
    assert "fallback" in reduced
    assert "x" * 100 in reduced


def test_build_paper_evidence_packet_keeps_abstract_theorems_and_conclusion():
    paper = """# Title

Abstract claim.

Theorem 1: conditional result.

Conclusion.—Qualified conclusion.
"""
    packet = module.build_paper_evidence_packet(paper)
    assert "Abstract claim." in packet
    assert "Theorem 1: conditional result." in packet
    assert "Conclusion.—Qualified conclusion." in packet


def test_build_slots_prompt_references_wiki():
    prompt = module.build_slots_prompt("# Some wiki content")
    assert "Some wiki content" in prompt
    assert "<<<SLOTS>>>" in prompt
    assert "<<<WIKI>>>" in prompt  # 引用已写好的 wiki
    assert "优先使用以下已登记谓词" in prompt
    assert "研究方向也不写入三元组" in prompt
    assert "不得从“关联/构造/表示”自行推导“基于”" in prompt
    assert "研究对象、模型维度、实验设置和适用场景不得标为局限性" in prompt
    assert "<<</SLOTS>>>" in prompt
    assert "不得输出 `<<<END>>>`" in prompt


def test_build_slots_retry_prompt_includes_previous_output_and_exact_count():
    previous = "三元组:\n本论文 | 核心方法 | 张量网络"
    error = "格式校验已通过，成功解析 1 条 Worker 三元组，至少需要 4 条"
    prompt = module.build_slots_prompt("# Wiki", [error], previous)
    assert error in prompt
    assert "[上次语义槽输出]" in prompt
    assert previous in prompt
    assert prompt.index("<<<SLOTS>>>") < prompt.index("<<</SLOTS>>>")


def test_paper_slots_retry_passes_semantic_reasoning_context():
    captured = {}

    def fake_call(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return {"ok": True, "status": "ok", "text": (
            "<<<SLOTS>>>\n三元组:\n本论文 | 核心方法 | 张量网络\n")}

    with tempfile.TemporaryDirectory() as directory:
        original_repo, original_call = module.REPO, module.call_text
        module.REPO = Path(directory)
        module.call_text = fake_call
        try:
            state = {
                "extract_dir": "temp/reasoning-paper",
                "wiki_content": "# Paper\n\n## Content\n\n正文。",
                "slots_retry": 1,
                "slots_errors": ["谓词不合法"],
                "_sparse_slots_best": {
                    "count": 1,
                    "content": "三元组:\n本论文 | 核心方法 | 旧方法",
                },
                "transaction_id": "reasoning-paper",
            }
            success, message = module.step_write_slots(state)
        finally:
            module.REPO, module.call_text = original_repo, original_call
    assert success, message
    assert captured["operation"] == "ingest_semantic_extract"
    assert captured["reasoning_context"]["document_kind"] == "paper"
    assert captured["reasoning_context"]["failure_kind"] == "semantic"
    assert captured["reasoning_context"]["retry"] == 1
    assert "[上次语义槽输出]" in captured["prompt"]
    assert "本论文 | 核心方法 | 旧方法" in captured["prompt"]


def test_predicate_candidate_validation():
    assert module.is_valid_predicate_candidate("表示")
    assert module.is_valid_predicate_candidate("relatesTo")
    assert not module.is_valid_predicate_candidate("表示，关联")
    assert not module.is_valid_predicate_candidate("关系谓词长度超过十二个汉字")


def test_semantic_predicate_guide_matches_registered_set():
    guide = set(module.semantic_predicate_guide().split("、"))
    assert guide == module.SEMANTIC_PREDICATES


def test_validate_before_commit_returns_semantic_errors():
    original = module.step_validate_semantics
    try:
        module.step_validate_semantics = lambda state: (["谓词格式不合法"], [])
        assert module.ic.validate_before_commit({}, module.step_validate_semantics, module.NON_BLOCKING_ISSUES) == ["谓词格式不合法"]
    finally:
        module.step_validate_semantics = original


def test_semantic_coverage_count_excludes_deterministic_metadata_edges():
    import graph_ingest

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "semantic.txt"
        semantic.write_text(
            "三元组:\n"
            "本论文 | 核心方法 | 张量网络\n"
            "本论文 | 研究基础 | 量子多体系统\n",
            encoding="utf-8",
        )
        page = "academic/wiki/papers/demo"
        parsed = [
            {"subject": page, "predicate": "核心方法", "object": "张量网络"},
            {"subject": page, "predicate": "研究基础", "object": "量子多体系统"},
            {"subject": "作者甲", "predicate": "第一作者", "object": page},
            {"subject": "作者乙", "predicate": "作者", "object": page},
            {"subject": "作者丙", "predicate": "作者", "object": page},
            {"subject": "作者丁", "predicate": "作者", "object": page},
            {"subject": page, "predicate": "发表于", "object": "物理评论快报"},
        ]
        original_repo = module.REPO
        original_parse = graph_ingest.parse_semantic_text
        module.REPO = root
        graph_ingest.parse_semantic_text = lambda *_args: (
            parsed, [], None, set(), [], [],
        )
        state = {
            "semantic_path": "semantic.txt",
            "wiki_path": page,
        }
        try:
            hard_errors, _warnings = module.step_validate_semantics(state)
        finally:
            module.REPO = original_repo
            graph_ingest.parse_semantic_text = original_parse
        assert not hard_errors
        assert state["semantic_triple_count"] == 2
        assert state["semantic_slot_diagnostics"]["semantic_triple_count"] == 2


def test_descriptive_object_matches_graph_rule():
    assert module.is_clearly_descriptive("矩阵乘积态matrix product state(MPS)表示")
    assert not module.is_clearly_descriptive("自洽方程")


def test_stop_for_semantic_errors_preserves_resume_context():
    state = {"transaction_id": "txn-1", "semantic_path": "temp/slots.txt"}
    module.ic.stop_for_semantic_errors(state, ["谓词格式不合法: 用于说明"], module._resume_cmd(state))
    assert state["status"] == "agent_required"
    assert state["errors"] == ["谓词格式不合法: 用于说明"]
    assert "--resume txn-1" in state["agent_prompt"]


def test_record_predicate_candidates_keeps_source():
    import json
    import tempfile
    original_path, original_govern = module.PREDICATE_CANDIDATES_PATH, module.govern_predicates
    try:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "predicate-candidates.jsonl"
            module.PREDICATE_CANDIDATES_PATH = queue
            module.govern_predicates = lambda: None
            state = {
                "transaction_id": "txn-1", "paper_id": "paper-1", "wiki_path": "academic/wiki/papers/paper-1",
                "source": "inbox/source.pdf", "predicate_candidates": [{"predicate": "表示", "subject": "A", "object": "B"}],
            }
            module.record_predicate_candidates(state)
            assert json.loads(queue.read_text(encoding="utf-8"))["source"] == "inbox/source.pdf"
    finally:
        module.PREDICATE_CANDIDATES_PATH, module.govern_predicates = original_path, original_govern


def test_build_wiki_prompt_with_errors():
    prompt = module.build_wiki_prompt("paper", "skeleton", ["ERROR: missing title"])
    assert "ERROR: missing title" in prompt
    assert "上次输出的问题" in prompt



def test_find_slot_section_期刊():
    sem = "期刊:\nPhys. Rev. Lett. 102, 240603\n三元组:\n本论文 | 研究基础 | MPS\n"
    assert module.find_slot_section("发表于", sem, "Phys. Rev. Lett. 102, 240603") == "期刊"


def test_find_slot_section_研究基础():
    sem = "期刊:\nPRL\n三元组:\n本论文 | 研究基础 | MPS\nA|所属|B\n"
    assert module.find_slot_section("研究基础", sem, "MPS") == "三元组"


def test_find_slot_section_自由边():
    sem = "三元组:\nHastings|所属|Microsoft Research, Station Q\n"
    assert module.find_slot_section("所属", sem, "Microsoft Research, Station Q") == "三元组"


def test_patch_semantic_lines_期刊():
    sem = "期刊:\nPhys. Rev. Lett. 102, 240603 (2009)\n三元组:\n本论文 | 研究基础 | MPS\n"
    repaired = "期刊: PRL\n"
    warnings = [{"line": "Phys. Rev. Lett. 102, 240603 (2009)", "section": "期刊", "issue": "descriptive_phrase"}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "PRL" in result
    assert "Phys. Rev. Lett. 102, 240603" not in result
    assert "MPS" in result


def test_patch_semantic_lines_自由边():
    sem = "三元组:\nHastings|所属|Microsoft Research, Station Q\n"
    repaired = "Hastings|所属|Microsoft Research\n"
    warnings = [{"line": "Microsoft Research, Station Q", "section": "三元组", "issue": "descriptive_phrase"}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "Microsoft Research" in result
    assert "Station Q" not in result


def test_patch_semantic_lines_bare_abbrev():
    sem = "三元组:\n本论文 | 核心创新点 | 避免MPS键维度的显式截断\n"
    repaired = "本论文 | 核心创新点 | 避免矩阵积态(MPS)键维度的显式截断\n"
    warnings = [{"line": "避免MPS键维度的显式截断", "section": "三元组", "issue": "bare_abbreviation"}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "矩阵积态(MPS)" in result


def test_validate_and_patch_paper_triple_repairs_bare_abbreviations():
    sem = (
        "三元组:\n"
        "本论文 | 核心创新点 | 提出基于DMRG计算TEE的实用数值方案\n"
        "本论文 | 局限性 | TEE不能完全确定拓扑相具体性质\n"
    )
    # 临时空图：确保 DMRG/TEE resolve miss → 保留 bare_abbreviation warning，独立于真实图状态
    import graph_lib
    old_db = graph_lib.GRAPH_DB
    try:
        with tempfile.TemporaryDirectory() as db_dir:
            graph_lib.GRAPH_DB = Path(db_dir) / "graph.db"
            conn = graph_lib.connect()
            graph_lib.init_schema(conn)
            conn.close()
            with tempfile.TemporaryDirectory() as directory:
                semantic_path = Path(directory) / "semantic.txt"
                semantic_path.write_text(sem, encoding="utf-8")
                state = {"semantic_path": str(semantic_path), "wiki_path": "academic/wiki/papers/test"}
                errors, warnings = module.step_validate_semantics(state)
            assert not errors
            assert all(warning["is_triple"] for warning in warnings)
            assert warnings[0]["line"] == "academic/wiki/papers/test | 核心创新点 | 提出基于DMRG计算TEE的实用数值方案"
            repaired = (
                "academic/wiki/papers/test | 核心创新点 | 密度矩阵重整化群density matrix renormalization group(DMRG)\n"
                "academic/wiki/papers/test | 局限性 | 拓扑纠缠熵topological entanglement entropy(TEE)\n"
            )
            result = module.patch_semantic_lines(sem, repaired, warnings)
            assert result is not None
            assert "密度矩阵重整化群density matrix renormalization group(DMRG)" in result
            assert "拓扑纠缠熵topological entanglement entropy(TEE)" in result
    finally:
        graph_lib.GRAPH_DB = old_db


def test_slot_validation_skips_warning_when_abbr_resolves_to_keyword():
    """缩写已在图里注册 alias（keyword 节点）→ resolve 命中 → 不产生 warning。"""
    import graph_lib
    old_db = graph_lib.GRAPH_DB
    try:
        with tempfile.TemporaryDirectory() as db_dir:
            graph_lib.GRAPH_DB = Path(db_dir) / "graph.db"
            conn = graph_lib.connect()
            graph_lib.init_schema(conn)
            # 图里已有 TPA keyword 节点 + alias
            graph_lib.ensure_node(conn, "TPA", "张量积注意力tensor product attention(TPA)",
                                  "entity", entity_subtype="keyword")
            graph_lib.insert_aliases(conn, "TPA", ["TPA", "张量积注意力", "tensor product attention"])
            conn.commit()
            conn.close()
            # 语义槽裸写 TPA（无括号）→ 应 resolve 命中 → 不 warn
            sem = "三元组:\n本论文 | 核心方法 | TPA\n"
            with tempfile.TemporaryDirectory() as directory:
                sp = Path(directory) / "semantic.txt"
                sp.write_text(sem, encoding="utf-8")
                state = {"semantic_path": str(sp), "wiki_path": "academic/wiki/papers/t"}
                errors, warnings = module.step_validate_semantics(state)
            assert not errors
            bare = [w for w in warnings if w["issue"] == "bare_abbreviation"]
            assert bare == [], f"已注册 alias 的缩写不应 warn: {bare}"
    finally:
        graph_lib.GRAPH_DB = old_db


def test_slot_validation_warns_for_unregistered_abbr():
    """缩写图里无 keyword 节点 → resolve miss → 保留 warning。"""
    import graph_lib
    old_db = graph_lib.GRAPH_DB
    try:
        with tempfile.TemporaryDirectory() as db_dir:
            graph_lib.GRAPH_DB = Path(db_dir) / "graph.db"
            conn = graph_lib.connect()
            graph_lib.init_schema(conn)
            conn.close()
            # 空图：ZZQT 无对应 keyword
            sem = "三元组:\n本论文 | 核心方法 | ZZQT\n"
            with tempfile.TemporaryDirectory() as directory:
                sp = Path(directory) / "semantic.txt"
                sp.write_text(sem, encoding="utf-8")
                state = {"semantic_path": str(sp), "wiki_path": "academic/wiki/papers/t"}
                errors, warnings = module.step_validate_semantics(state)
            assert not errors
            bare = [w for w in warnings if w["issue"] == "bare_abbreviation"]
            assert len(bare) == 1, f"未注册缩写应保留 warning: {warnings}"
    finally:
        graph_lib.GRAPH_DB = old_db


def test_bare_tokens_resolvable_proposition_not_counted():
    """resolve 到 proposition 节点不算 resolved（非概念端点，防歧义）。"""
    import graph_lib
    import graph_ingest
    old_db = graph_lib.GRAPH_DB
    try:
        with tempfile.TemporaryDirectory() as db_dir:
            graph_lib.GRAPH_DB = Path(db_dir) / "graph.db"
            conn = graph_lib.connect()
            graph_lib.init_schema(conn)
            # 图里只有同名的 proposition 节点（非 keyword）
            graph_lib.ensure_node(conn, "TPA", "TPA", "entity",
                                  status="current", entity_subtype="proposition")
            conn.commit()
            ti, ai, si = graph_lib.build_name_index(conn)
            # proposition 不进 resolve 索引 → bare_tokens_resolvable 应返回 False
            assert graph_ingest.bare_tokens_resolvable("TPA", conn, ti, ai, si) is False
            conn.close()
    finally:
        graph_lib.GRAPH_DB = old_db

def test_semantic_patch_prompt_and_candidate_boundary():
    import ingest_common as ic

    catalog = ic.build_semantic_patch_catalog([{
        "section": "三元组",
        "line": "academic/wiki/papers/test | 核心方法 | DMRG",
        "issue": "bad_object",
        "reason": "裸缩写",
        "is_triple": True,
    }])
    prompt = ic._build_semantic_patch_prompt(catalog)
    assert "issue-01" in prompt
    assert "主体 | 谓词 | 客体" in prompt
    assert "不得输出完整 Wiki 或完整语义槽" in prompt

    valid = {
        "protocol_version": ic.SEMANTIC_PATCH_PROTOCOL,
        "review_status": "patched",
        "patches": [{
            "issue_id": "issue-01",
            "action": "replace",
            "replacement_lines": ["academic/wiki/papers/test | 核心方法 | 密度矩阵重整化群(DMRG)"],
        }],
        "review_notes": [],
    }
    assert ic._compile_semantic_patch(valid, catalog).startswith("academic/wiki/papers/test |")

    for invalid_patches in ([], [dict(valid["patches"][0], issue_id="issue-02")]):
        invalid = dict(valid, patches=invalid_patches)
        try:
            ic._compile_semantic_patch(invalid, catalog)
        except ValueError:
            pass
        else:
            raise AssertionError("semantic patch must cover exactly the catalog issue IDs")


def test_patch_semantic_lines_no_match():
    sem = "期刊:\nPRL\n"
    repaired = "期刊: Nature\n"
    warnings = [{"line": "不存在这一行", "section": "期刊", "issue": "descriptive_phrase"}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is None


def test_extract_arxiv_id():
    assert module.extract_arxiv_id("arXiv:0907.0401v2") == "0907.0401v2"
    assert module.extract_arxiv_id("see arxiv 1307.0401 for details") == "1307.0401"
    assert module.extract_arxiv_id("no id here") == ""


def test_extract_doi():
    assert module.extract_doi("DOI: 10.1103/PhysRevLett.106.127202") == "10.1103/PhysRevLett.106.127202"
    assert module.extract_doi("no doi here") == ""


def test_detect_raw_relationship_none():
    state = {"source": "nonexistent.pdf"}
    result = module.detect_raw_relationship(state, [])
    assert result["type"] is None


def test_detect_raw_relationship_supplementary():
    state = {"source": "supplementary_info.pdf"}
    dup_graph = [{"path": "academic/wiki/papers/test", "title": "Test", "ratio": 0.9}]
    result = module.detect_raw_relationship(state, dup_graph)
    assert result["type"] == "supplementary"
    assert result["target_page"] == "academic/wiki/papers/test"
    assert not result["uncertain"]


def test_detect_raw_relationship_translation():
    state = {"source": "paper-zh.pdf"}
    dup_graph = [{"path": "academic/wiki/papers/test", "title": "Test", "ratio": 0.9}]
    result = module.detect_raw_relationship(state, dup_graph)
    assert result["type"] == "translation"
    assert result["target_page"] == "academic/wiki/papers/test"


def test_detect_raw_relationship_does_not_match_transport():
    state = {"source": "Quantum Transport.pdf"}
    dup_graph = [{"path": "academic/wiki/papers/test", "title": "Test", "ratio": 0.9}]
    result = module.detect_raw_relationship(state, dup_graph)
    assert result["type"] != "translation"


def test_resume_after_semantic_fix_loads_disk_content():
    with tempfile.TemporaryDirectory() as directory:
        semantic_path = Path(directory) / "semantic.txt"
        semantic_path.write_text("三元组:\n本论文 | 研究关键词 | 修正概念\n", encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = Path(directory)
            state = {
                "status": "agent_required",
                "semantic_path": "semantic.txt",
                "slots_content": "过期内容",
                "agent_required": True,
                "errors": ["warning"],
            }
            assert module.resume_after_semantic_fix(state)
        finally:
            module.REPO = original_repo
    assert state["status"] == "finalize"
    assert state["slots_content"] == "三元组:\n本论文 | 研究关键词 | 修正概念\n"
    assert not state["agent_required"]
    assert state["errors"] == []


def test_handoff_to_agent_records_pre_handoff_status():
    """handoff 前若已落位(graph_ready)，应记录 pre_handoff_status 供 resume 恢复。"""
    sem = "期刊:\nPRX Quantum\n三元组:\n本论文 | 核心创新点 | 首次将DMRG方法系统性应用于基态能量计算\n"
    with tempfile.TemporaryDirectory() as directory:
        sp = Path(directory) / "semantic.txt"
        sp.write_text(sem, encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = Path(directory)
            state = {"transaction_id": "txn-p", "semantic_path": str(sp),
                     "wiki_path": "academic/wiki/papers/p", "status": "graph_ready"}
            module.ic.handoff_to_agent(state, "恢复前语义槽校验未通过",
                                       module.step_validate_semantics,
                                       module._resume_cmd(state), module._validate_cmd(state))
        finally:
            module.REPO = original_repo
    assert state["status"] == "agent_required"
    assert state["pre_handoff_status"] == "graph_ready"


def test_resume_after_semantic_fix_restores_pre_handoff_status():
    """落位后 handoff 的论文，resume 应恢复到 graph_ready 而非 finalize，避免重跑落位。"""
    with tempfile.TemporaryDirectory() as directory:
        semantic_path = Path(directory) / "semantic.txt"
        semantic_path.write_text("三元组:\n本论文 | 研究关键词 | 修正概念\n", encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = Path(directory)
            state = {
                "status": "agent_required",
                "semantic_path": "semantic.txt",
                "pre_handoff_status": "graph_ready",
                "slots_content": "过期内容",
                "agent_required": True,
                "errors": ["warning"],
            }
            assert module.resume_after_semantic_fix(state)
        finally:
            module.REPO = original_repo
            assert state["status"] == "graph_ready"
    assert state["slots_content"] == "三元组:\n本论文 | 研究关键词 | 修正概念\n"
    assert not state["agent_required"]
    assert state["errors"] == []


def test_run_one_dispatches_failed_graph_retry_directly_to_commit():
    state = {
        "transaction_id": "graph-retry",
        "status": "failed",
        "resume_from": "graph_ready",
    }
    calls = []
    original_run_phase = module._run_phase
    try:
        module._run_phase = lambda current, verbose, fn: calls.append(fn.__name__) or {
            **current, "status": "completed",
        }
        result = module.run_one(state, False)
    finally:
        module._run_phase = original_run_phase
    assert result["status"] == "completed"
    assert calls == ["run_commit"]


def test_run_one_blocks_unchanged_graph_failure_context():
    state = {
        "transaction_id": "graph-retry",
        "status": "failed",
        "resume_from": "graph_ready",
        "failure_context": "same-context",
    }
    original_context = module._graph_retry_context
    original_run_phase = module._run_phase
    original_save = module.inbox_state.save
    try:
        module._graph_retry_context = lambda _state: "same-context"
        module._run_phase = lambda *_args: (_ for _ in ()).throw(
            AssertionError("unchanged graph failure must not rerun"))
        module.inbox_state.save = lambda *_args: None
        result = module.run_one(state, False)
    finally:
        module._graph_retry_context = original_context
        module._run_phase = original_run_phase
        module.inbox_state.save = original_save
    assert result["resume_blocked"] == "unchanged_failure_context"
    assert result["retryable"] is False
    assert result["next_action"] == "repair_graph_identity_or_metadata_then_resume"


def test_run_one_allows_graph_resume_after_context_changes():
    state = {
        "transaction_id": "graph-retry",
        "status": "failed",
        "resume_from": "graph_ready",
        "failure_context": "old-context",
        "retryable": False,
        "next_action": "repair_graph_identity_or_metadata_then_resume",
    }
    calls = []
    original_context = module._graph_retry_context
    original_run_phase = module._run_phase
    try:
        module._graph_retry_context = lambda _state: "new-context"
        module._run_phase = lambda current, verbose, fn: calls.append(fn.__name__) or {
            **current, "status": "completed",
        }
        result = module.run_one(state, False)
    finally:
        module._graph_retry_context = original_context
        module._run_phase = original_run_phase
    assert result["status"] == "completed"
    assert calls == ["run_commit"]
    assert "retryable" not in result
    assert "next_action" not in result


def test_detect_raw_relationship_uncertain_duplicate():
    state = {"source": "paper.pdf"}
    dup_graph = [{"path": "academic/wiki/papers/test", "title": "Test Paper", "ratio": 0.97}]
    result = module.detect_raw_relationship(state, dup_graph)
    assert result["type"] == "duplicate"
    assert result["uncertain"] is True


def test_detect_raw_relationship_uncertain_version():
    state = {"source": "paper.pdf"}
    dup_graph = [{"path": "academic/wiki/papers/test", "title": "Test Paper", "ratio": 0.88}]
    result = module.detect_raw_relationship(state, dup_graph)
    assert result["type"] is None


def test_detect_raw_relationship_raw_candidate_without_page():
    """raw-only candidate (no graph page) with high title ratio → uncertain duplicate."""
    state = {"source": "paper.pdf"}
    cand = [{"dir": "test-2020-paper", "title": "Test Paper", "ratio": 0.97,
             "raw_path": "academic/raw/references/test-2020-paper/paper.md"}]
    result = module.detect_raw_relationship(state, cand)
    assert result["type"] == "duplicate"
    assert result["uncertain"] is True
    assert result.get("target_raw_dir") == "test-2020-paper"


def test_relationship_schema_forbids_worker_duplicate_decision():
    decision = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "review_status": "decided",
        "selection": {
            "candidate_id": "relation-01", "relation": "duplicate", "status": "confirmed",
        },
        "review_notes": [],
    }
    assert not module.relationship_decision_schema(decision)


def test_relationship_compile_rejects_unknown_candidate_id():
    catalog = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "current": {},
        "candidates": [{"id": "relation-01"}],
    }
    decision = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "review_status": "decided",
        "selection": {
            "candidate_id": "relation-99", "relation": "version", "status": "confirmed",
        },
        "review_notes": [],
    }
    try:
        module._compile_relationship_decision(decision, catalog)
    except ValueError as exc:
        assert "未知 relationship candidate_id" in str(exc)
    else:
        raise AssertionError("unknown relationship candidate ID must fail")


def test_relationship_deterministic_fast_path_uses_locked_identity():
    bibliography = {
        "title": "A Study", "authors": ["Ada Lovelace"], "year": "2025",
        "doi": "", "arxiv_id": "",
    }
    catalog = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "current": {"bibliographic": bibliography, "opening_excerpt": "new"},
        "candidates": [{
            "id": "relation-01", "bibliographic": bibliography,
            "title": "A Study", "similarity": 1.0, "opening_excerpt": "old",
        }],
    }
    decision = module._deterministic_relationship_decision(catalog)
    assert decision["selection"] == {
        "candidate_id": "relation-01", "relation": "version", "status": "confirmed",
    }


def test_relationship_decision_cache_reuses_identical_input():
    decision = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "review_status": "decided",
        "selection": {
            "candidate_id": "relation-01", "relation": "unrelated", "status": "confirmed",
        },
        "review_notes": [],
    }
    with tempfile.TemporaryDirectory() as directory:
        old_repo = module.REPO
        try:
            module.REPO = Path(directory)
            module._save_relationship_cache("txn", "same-input", decision)
            assert module._load_relationship_cache("txn", "same-input") == decision
            assert module._load_relationship_cache("txn", "changed-input") is None
        finally:
            module.REPO = old_repo


def test_relationship_worker_single_call_then_cache_hit():
    catalog = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "current": {"bibliographic": {"title": "Study", "authors": [], "year": ""}},
        "candidates": [{
            "id": "relation-01", "title": "Study revised", "similarity": 0.97,
            "bibliographic": {"title": "Study revised", "authors": [], "year": ""},
            "opening_excerpt": "old",
        }],
    }
    decision = {
        "protocol_version": module.RELATION_DECISION_PROTOCOL,
        "review_status": "decided",
        "selection": {
            "candidate_id": "relation-01", "relation": "unrelated", "status": "confirmed",
        },
        "review_notes": [],
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paper_md = root / "paper.md"
        paper_md.write_text("# Study\n", encoding="utf-8")
        old_repo, old_builder, old_call = (
            module.REPO, module.build_relationship_candidate_catalog, module.call_json,
        )
        calls = []
        try:
            module.REPO = root
            module.build_relationship_candidate_catalog = lambda _state, _paper: catalog

            def fake_call(*args, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "status": "ok", "parsed": decision, "history": [{}]}

            module.call_json = fake_call
            state = {"transaction_id": "rel", "relation_candidates": [{"path": "target"}]}
            first = module.review_uncertain_relationship(state, paper_md)
            second = module.review_uncertain_relationship(state, paper_md)
        finally:
            module.REPO = old_repo
            module.build_relationship_candidate_catalog = old_builder
            module.call_json = old_call
        assert first["ok"] and first["worker"]["api_called"] is True
        assert second["ok"] and second["worker"]["cache_hit"] is True
        assert len(calls) == 1
        assert calls[0]["retries"] == 0


def test_relationship_handoff_is_not_consumed_as_semantic_fix():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "semantic.txt"
        semantic.write_text("三元组:\nA | 涉及 | B\n", encoding="utf-8")
        state = {
            "status": "agent_required",
            "semantic_path": "semantic.txt",
            "relationship_review": {"status": "agent_required"},
        }
        old_repo = module.REPO
        try:
            module.REPO = root
            assert module.resume_after_semantic_fix(state) is False
        finally:
            module.REPO = old_repo


def test_uncertain_title_match_defers_until_post_extract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "inbox").mkdir()
        (root / "inbox" / "test-paper.pdf").write_bytes(b"not-a-real-pdf")
        raw_dir = root / "academic" / "raw" / "references" / "test-paper"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.md").write_text("# Test Paper\n\nAda Lovelace\n", encoding="utf-8")
        old_repo = module.REPO
        old_ensure, old_lookup = module.sf.ensure_index, module.sf.lookup_exact
        old_extract = module.extract_pdf_bibliography
        try:
            module.REPO = root
            module.sf.ensure_index = lambda: None
            module.sf.lookup_exact = lambda _path: None
            module.extract_pdf_bibliography = lambda _path: {
                "title": "Test Paper", "authors": [], "year": "", "doi": "", "arxiv_id": "",
            }
            state = {"source": "inbox/test-paper.pdf"}
            is_duplicate, _message = module.step_dedup_check(state)
        finally:
            module.REPO = old_repo
            module.sf.ensure_index, module.sf.lookup_exact = old_ensure, old_lookup
            module.extract_pdf_bibliography = old_extract
        assert not is_duplicate
        assert state["raw_relationship"]["uncertain"] is True
        assert state["dedup_result"]["duplicate"] is False


def test_semantic_duplicate_cleanup_skips_worker():
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "semantic.txt"
        semantic.write_text("三元组:\nA | 涉及 | B\nA | 涉及 | B\n", encoding="utf-8")
        state = {"transaction_id": "dedup", "semantic_path": "semantic.txt"}
        warning = {
            "section": "三元组", "line": "A | 涉及 | B",
            "issue": "duplicate_line", "reason": "重复", "is_triple": True,
        }
        old_call = ic.call_json
        try:
            ic.call_json = lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("duplicate cleanup must not call Worker")
            )

            def validate(_state):
                text = semantic.read_text(encoding="utf-8")
                return ([], [warning] if text.count("A | 涉及 | B") > 1 else [])

            ok, message = ic.repair_slots(state, root, [warning], validate)
        finally:
            ic.call_json = old_call
        assert ok, message
        assert semantic.read_text(encoding="utf-8").count("A | 涉及 | B") == 1
        assert state["semantic_repair_worker"]["skip_reason"] == "deterministic_duplicate_cleanup"
        assert state["semantic_repair_worker"]["api_called"] is False


def test_semantic_patch_worker_single_call_and_cache():
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        semantic = root / "semantic.txt"
        original_text = "三元组:\nA | 涉及 | bad\n"
        semantic.write_text(original_text, encoding="utf-8")
        warning = {
            "section": "三元组", "line": "A | 涉及 | bad", "issue": "bad_object",
            "field": "object", "reason": "测试问题", "is_triple": True,
        }
        calls = []
        decision = {
            "protocol_version": ic.SEMANTIC_PATCH_PROTOCOL,
            "review_status": "patched",
            "patches": [{
                "issue_id": "issue-01", "action": "replace",
                "replacement_lines": ["A | 涉及 | good"],
            }],
            "review_notes": [],
        }
        old_call = ic.call_json
        try:
            def fake_call(*args, **kwargs):
                calls.append(kwargs)
                return {"ok": True, "status": "ok", "parsed": decision, "history": [{}]}

            ic.call_json = fake_call

            def validate(_state):
                return ([], [warning] if "bad" in semantic.read_text(encoding="utf-8") else [])

            first = {"transaction_id": "patch", "semantic_path": "semantic.txt"}
            ok, message = ic.repair_slots(first, root, [warning], validate)
            assert ok, message
            assert len(calls) == 1
            assert calls[0]["retries"] == 0
            assert first["semantic_repair_worker"]["api_called"] is True

            semantic.write_text(original_text, encoding="utf-8")
            second = {"transaction_id": "patch", "semantic_path": "semantic.txt"}
            ok, message = ic.repair_slots(second, root, [warning], validate)
            assert ok, message
            assert len(calls) == 1
            assert second["semantic_repair_worker"]["cache_hit"] is True
        finally:
            ic.call_json = old_call


def test_dedup_low_title_ratio_not_duplicate():
    """标题相似度 ≤ 0.95 不应判重（回归：NIPS workshop 误判为 biamonte 重复）。"""
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        inbox = repo / "inbox"
        inbox.mkdir()
        pdf = inbox / "test.pdf"
        pdf.write_bytes(b"pdf")
        raw_refs = repo / "academic" / "raw" / "references" / "existing-2017-paper"
        raw_refs.mkdir(parents=True)
        (raw_refs / "paper.md").write_text("# Existing Paper Title\\n", encoding="utf-8")
        old_repo, old_extract = module.REPO, module.extract_title_from_pdf
        module.REPO = repo
        module.extract_title_from_pdf = lambda _p: "Completely Different Topic"
        try:
            state = module.new_state_for_pdf(pdf)
            ok, msg = module.step_dedup_check(state)
        finally:
            module.REPO = old_repo
            module.extract_title_from_pdf = old_extract
    assert ok is False
    assert "疑似已摄入" not in msg


def test_exact_fingerprint_stops_before_pdf_metadata_and_mineru():
    with tempfile.TemporaryDirectory() as directory:
        repo = Path(directory)
        pdf = repo / "inbox" / "same.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(b"same-pdf")
        state = {"source": "inbox/same.pdf", "quality_warnings": []}
        originals = (
            module.REPO, module.sf.ensure_index, module.sf.lookup_exact,
            module.extract_pdf_bibliography, module.extract_title_from_pdf,
        )
        module.REPO = repo
        module.sf.ensure_index = lambda: None
        module.sf.lookup_exact = lambda _path: {
            "raw_path": "academic/raw/references/existing/paper.pdf",
            "binary_sha256": "abc123",
            "size_bytes": 8,
        }
        module.extract_pdf_bibliography = lambda _path: (_ for _ in ()).throw(
            AssertionError("exact fingerprint must stop before PDF metadata"))
        module.extract_title_from_pdf = lambda _path: (_ for _ in ()).throw(
            AssertionError("exact fingerprint must stop before title extraction"))
        try:
            duplicate, message = module.step_dedup_check(state)
        finally:
            (module.REPO, module.sf.ensure_index, module.sf.lookup_exact,
             module.extract_pdf_bibliography, module.extract_title_from_pdf) = originals
    assert duplicate is True
    assert state["dedup_result"]["match"] == "binary_sha256"
    assert "academic/raw/references/existing/paper.pdf" in message


def test_post_extract_text_candidate_requires_locked_bibliography():
    with tempfile.TemporaryDirectory() as directory:
        paper_md = Path(directory) / "paper.md"
        paper_md.write_text("Normalized paper text", encoding="utf-8")
        candidate = {
            "raw_path": "academic/raw/references/existing/paper.pdf",
            "match": "normalized_text_sha256",
            "text_sha256": "text123",
        }
        current = {
            "title": "A Stable Title",
            "authors": ["Alice Example", "Bob Example"],
            "year": "2024",
            "doi": "",
            "arxiv_id": "",
        }
        original_lookup = module.sf.lookup_text_candidate
        original_bibliography = module._bibliography_for_raw_artifact
        try:
            module.sf.lookup_text_candidate = lambda _path: candidate
            module._bibliography_for_raw_artifact = lambda _path: dict(current)
            assert module._post_extract_duplicate(
                {"bibliographic_meta": dict(current)}, paper_md) == candidate
            mismatch = {**current, "year": "2025"}
            assert module._post_extract_duplicate(
                {"bibliographic_meta": mismatch}, paper_md) is None
        finally:
            module.sf.lookup_text_candidate = original_lookup
            module._bibliography_for_raw_artifact = original_bibliography


def test_patch_semantic_lines_collision_no_loss():
    """同一 (主体,谓词) 多条客体不同时，修复不应丢失也不应造成重复（deque 保序消费）。"""
    sem = "三元组:\n本论文 | 研究基础 | MPS\n本论文 | 研究基础 | PEPS\n"
    repaired = (
        "academic/wiki/papers/test | 研究基础 | 矩阵乘积态matrix product state(MPS)\n"
        "academic/wiki/papers/test | 研究基础 | 投影纠缠对态projected entangled pair state(PEPS)\n"
    )
    warnings = [
        {"section": "三元组", "line": "本论文 | 研究基础 | MPS", "issue": "bare_abbreviation", "field": "object", "is_triple": True},
        {"section": "三元组", "line": "本论文 | 研究基础 | PEPS", "issue": "bare_abbreviation", "field": "object", "is_triple": True},
    ]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "矩阵乘积态matrix product state(MPS)" in result
    assert "投影纠缠对态projected entangled pair state(PEPS)" in result
    # 不应出现重复行
    assert result.count("投影纠缠对态") == 1
    assert result.count("矩阵乘积态") == 1


def test_patch_preserves_original_subject_not_full_path():
    """修复行 subject 是全路径时，写回应保留原行 subject（本论文），不替换为全路径。"""
    sem = "三元组:\n本论文 | 核心创新点 | 辅助分布重要性采样\n"
    repaired = "academic/wiki/papers/test | 核心创新点 | 辅助分布重要性采样方法\n"
    warnings = [{"section": "三元组", "line": "本论文 | 核心创新点 | 辅助分布重要性采样",
                 "issue": "descriptive_phrase", "field": "object", "is_triple": True}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "辅助分布重要性采样方法" in result
    # 主体应保留「本论文」，不应出现全路径
    assert "本论文 | 核心创新点 | 辅助分布重要性采样方法" in result
    assert "academic/wiki/papers/test" not in result


def test_patch_repairs_subject_when_field_is_subject():
    """warning field=subject 时，应只替换 subject 列，object 保留原行。"""
    sem = "三元组:\niPEPS | 基于 | 投影纠缠对态projected entangled-pair state(PEPS)\n"
    repaired = "无限投影纠缠对态infinite projected entangled-pair state(iPEPS) | 基于 | 投影纠缠对态projected entangled-pair state(PEPS)\n"
    warnings = [{"section": "三元组",
                 "line": "iPEPS | 基于 | 投影纠缠对态projected entangled-pair state(PEPS)",
                 "issue": "bare_abbreviation", "field": "subject", "is_triple": True}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    assert "无限投影纠缠对态infinite projected entangled-pair state(iPEPS)" in result
    # object 保留原行（含完整 PEPS 释义），未被 LLM 输出替换
    assert "投影纠缠对态projected entangled-pair state(PEPS)" in result
    # object 列只出现一次（原行未被整行替换造成重复）
    assert result.count("projected entangled-pair state(PEPS)") == 1


def test_patch_decomposes_long_object_into_new_triples():
    """长客体含主谓宾时，LLM 拆解输出多行：替换行 + 新三元组，新行应追加到三元组段。"""
    sem = "三元组:\n本论文 | 核心创新点 | 基于自动微分的变分iPEPS方法\n"
    repaired = (
        "本论文 | 核心创新点 | 变分iPEPS\n"
        "变分iPEPS | 基于 | 自动微分\n"
    )
    warnings = [{"section": "三元组",
                 "line": "本论文 | 核心创新点 | 基于自动微分的变分iPEPS方法",
                 "issue": "descriptive_phrase", "field": "object", "is_triple": True}]
    result = module.patch_semantic_lines(sem, repaired, warnings)
    assert result is not None
    # 原行客体被替换为规范概念名
    assert "本论文 | 核心创新点 | 变分iPEPS" in result
    # 新三元组被追加到三元组段
    assert "变分iPEPS | 基于 | 自动微分" in result
    # 原长客体已消失
    assert "基于自动微分的变分iPEPS方法" not in result


def test_normalize_slots_dedups_duplicate_triples():
    """完全相同的三角组行应被机械去重，只留首条。"""
    text = "三元组:\n本论文 | 对比方法 | AlexNet\n本论文 | 对比方法 | AlexNet\n本论文 | 核心方法 | PEPS\n"
    result = module.normalize_slots(text)
    assert result.count("本论文 | 对比方法 | AlexNet") == 1
    assert result.count("本论文 | 核心方法 | PEPS") == 1


def test_validate_flags_duplicate_line():
    """修复后若引入重复三元组，validator 应作为 duplicate_line warning 检出。"""
    sem = (
        "三元组:\n"
        "本论文 | 未来展望 | 轻量级网络\n"
        "本论文 | 未来展望 | 轻量级网络\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        semantic_path = Path(directory) / "semantic.txt"
        semantic_path.write_text(sem, encoding="utf-8")
        state = {"semantic_path": str(semantic_path), "wiki_path": "academic/wiki/papers/test"}
        errors, warnings = module.step_validate_semantics(state)
    assert not errors
    dup = [w for w in warnings if w.get("issue") == "duplicate_line"]
    assert len(dup) == 1
    assert dup[0]["is_triple"] is True

def test_handoff_to_agent_includes_full_warnings():
    sem = "期刊:\nPRX Quantum\n三元组:\n本论文 | 核心创新点 | 首次将DMRG方法系统性应用于基态能量计算\n"
    # 临时空图：确保 PRX/DMRG resolve miss → 保留 warning，独立于真实图状态
    import graph_lib
    old_db = graph_lib.GRAPH_DB
    with tempfile.TemporaryDirectory() as db_dir:
        graph_lib.GRAPH_DB = Path(db_dir) / "graph.db"
        conn = graph_lib.connect()
        graph_lib.init_schema(conn)
        conn.close()
        try:
            with tempfile.TemporaryDirectory() as directory:
                sp = Path(directory) / "semantic.txt"
                sp.write_text(sem, encoding="utf-8")
                original_repo = module.REPO
                try:
                    module.REPO = Path(directory)
                    state = {"transaction_id": "txn-h", "semantic_path": str(sp),
                             "wiki_path": "academic/wiki/papers/h", "status": "agent_required"}
                    module.ic.handoff_to_agent(state, "两级模型修复未通过", module.step_validate_semantics, module._resume_cmd(state), module._validate_cmd(state))
                finally:
                    module.REPO = original_repo
            assert state["status"] == "agent_required"
            assert state["agent_required"] is True
            # 期刊/作者已改由确定性 metadata 生成，不再交弱 LLM 修复；语义 warning 仍完整保留。
            assert "PRX Quantum" not in state["agent_prompt"]
            assert "DMRG" in state["agent_prompt"]
            assert "--validate txn-h" in state["agent_prompt"]
            assert "--resume txn-h" in state["agent_prompt"]
        finally:
            graph_lib.GRAPH_DB = old_db


def test_validate_transaction_reports_warnings():
    import graph_lib as gl
    sem = "期刊:\nPRX Quantum\n三元组:\n本论文 | 核心创新点 | 首次将DMRG方法系统性应用于基态能量计算\n"
    with tempfile.TemporaryDirectory() as directory:
        sp = Path(directory) / "semantic.txt"
        sp.write_text(sem, encoding="utf-8")
        graph_path = Path(directory) / "graph.db"
        conn = sqlite3.connect(graph_path)
        gl.init_schema(conn)
        conn.close()
        original_graph_db = gl.GRAPH_DB
        state = {"semantic_path": str(sp), "wiki_path": "academic/wiki/papers/v",
                 "transaction_id": "txn-v", "paper_id": "v"}
        try:
            gl.GRAPH_DB = graph_path
            report = module.validate_transaction(state)
        finally:
            gl.GRAPH_DB = original_graph_db
    # proposition 改革：descriptive_phrase(object) 与 bare_abbreviation 均非阻断 → status=pass
    # 语义 warnings 仍上报（blocking=False）；确定性书目字段不进入语义 warning。
    assert report["status"] == "pass", f"全非阻断应 pass，got {report['status']}"
    assert not any(w["line"] == "PRX Quantum" for w in report["warnings"])
    assert any("DMRG" in w["line"] for w in report["warnings"])
    assert not any(w["issue"] == "descriptive_phrase" for w in report["warnings"])
    # 改革后所有 warning 均非阻断
    assert all(not w.get("blocking") for w in report["warnings"]), \
        f"不应有阻断型 warning: {[w for w in report['warnings'] if w.get('blocking')]}"


def test_find_ready_txn_matches_graph_ready_only():
    import json as _json
    with tempfile.TemporaryDirectory() as directory:
        original_repo = module.REPO
        try:
            module.REPO = Path(directory)
            sd = Path(directory) / "temp" / "inbox-state"
            sd.mkdir(parents=True)
            (sd / "a.json").write_text(_json.dumps({"source": "inbox/a.pdf", "status": "graph_ready"}))
            (sd / "b.json").write_text(_json.dumps({"source": "inbox/b.pdf", "status": "agent_required"}))
            (sd / "c.json").write_text(_json.dumps({"source": "inbox/c.pdf", "status": "completed"}))
            r = module.find_ready_txn("inbox/a.pdf")
            assert r and r["source"] == "inbox/a.pdf"
            assert module.find_ready_txn("inbox/b.pdf") is None
            assert module.find_ready_txn("inbox/c.pdf") is None
        finally:
            module.REPO = original_repo


def test_validate_before_commit_proposition_object_is_nonblocking():
    """proposition 改革：命题谓词(核心创新点)的 object 是命题，不再阻断校验。

    改革前 descriptive_phrase(field=object) 阻断 → 触发 3.6b 修复/agent handoff；
    改革后作为 proposition 节点入图，validate_before_commit 应返回空（无硬错误、无阻断 warning）。
    handoff 机制由 test_stop_for_semantic_errors_preserves_resume_context /
    test_handoff_to_agent_includes_full_warnings 单独覆盖。
    """
    sem = "期刊:\n物理学期刊\n三元组:\n本论文 | 核心创新点 | 首次将密度矩阵重整化群方法系统性应用于基态能量计算\n"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "semantic.txt").write_text(sem, encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = root
            state = {"semantic_path": "semantic.txt",
                     "wiki_path": "academic/wiki/papers/z", "transaction_id": "txn-z"}
            errors = module.ic.validate_before_commit(state, module.step_validate_semantics, module.NON_BLOCKING_ISSUES)
        finally:
           module.REPO = original_repo
    assert errors == [], f"命题 object 不应阻断，但仍返回错误: {errors}"


def test_nonblocking_semantic_warning_persists_quality_status():
    state = {
        "quality_warnings": [{"issue": "bibliographic_authors_incomplete", "detail": "x"}],
    }
    module._record_semantic_quality_warnings(state, [{
        "issue": "bare_abbreviation", "section": "三元组",
        "line": "本论文 | 核心方法 | DMRG", "reason": "unresolved abbreviation",
    }])
    assert state["quality_status"] == "degraded"
    assert state["semantic_warnings"][0]["issue"] == "bare_abbreviation"
    assert {warning["issue"] for warning in state["quality_warnings"]} == {
        "bibliographic_authors_incomplete", "semantic_bare_abbreviation",
    }
    module._record_semantic_quality_warnings(state, [])
    assert state["quality_status"] == "degraded", "bibliographic warning must remain"
    assert [warning["issue"] for warning in state["quality_warnings"]] == [
        "bibliographic_authors_incomplete"
    ]


def test_validate_before_commit_records_nonblocking_semantic_warning():
    state = {"quality_warnings": []}
    warning = {
        "issue": "bare_abbreviation", "section": "三元组",
        "line": "本论文 | 核心方法 | DMRG", "reason": "unresolved abbreviation",
    }
    errors = module.ic.validate_before_commit(
        state, lambda _state: ([], [warning]), module.NON_BLOCKING_ISSUES,
        module._record_semantic_quality_warnings,
    )
    assert errors == []
    assert state["semantic_warnings"] == [warning]
    assert state["quality_status"] == "degraded"


def test_step_extract_propositions_skips_when_no_propositions():
    """无命题谓词时 step_extract_propositions 直接跳过，不调 LLM。"""
    module.progress = lambda *a, **k: None
    sem = "三元组:\n本论文 | 研究关键词 | 纠缠熵\n本论文 | 核心方法 | MPS\n"
    called = []
    orig = module.call_json
    module.call_json = lambda *a, **k: called.append(1) or {"ok": True}
    try:
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "semantic.txt"
            sp.write_text(sem, encoding="utf-8")
            orig_repo = module.REPO
            try:
                module.REPO = Path(d)
                state = {"semantic_path": "semantic.txt", "wiki_path": "academic/wiki/papers/t"}
                ok, msg = module.step_extract_propositions(state)
                assert ok and msg == ""
                assert called == [], "无命题不应调 LLM"
                assert state["proposition_details"] == {
                    "proposition_count": 0,
                    "execution_mode": "deterministic",
                }
            finally:
                module.REPO = orig_repo
    finally:
        module.call_json = orig
        del module.progress


def test_step_extract_propositions_is_zero_llm_and_preserves_semantic():
    """有命题时也不调 LLM、不改 semantic，只登记稀疏编译状态。"""
    module.progress = lambda *a, **k: None
    sem = "三元组:\n本论文 | 核心创新点 | 证明ANTN超越矩阵乘积态matrix product state(MPS)\n"
    orig = module.call_json
    called = []
    module.call_json = lambda *a, **k: called.append((a, k)) or {"ok": False}
    try:
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "semantic.txt"
            sp.write_text(sem, encoding="utf-8")
            orig_repo = module.REPO
            try:
                module.REPO = Path(d)
                state = {"semantic_path": "semantic.txt", "wiki_path": "academic/wiki/papers/t"}
                ok, msg = module.step_extract_propositions(state)
                assert ok and msg == ""
                assert called == [], "稀疏命题编译不应调用 LLM"
                assert sp.read_text(encoding="utf-8") == sem, "稀疏编译不应改写 semantic"
                assert state["proposition_status"] == "sparse: 1 propositions"
                assert state["proposition_details"] == {
                    "proposition_count": 1,
                    "execution_mode": "deterministic",
                    "concept_links": "deterministic_graph_ingest",
                }
                assert not state.get("quality_warnings"), "确定性未匹配不是 degraded"
            finally:
                module.REPO = orig_repo
    finally:
        module.call_json = orig
        del module.progress


def test_run_prepare_does_not_record_proposition_llm_call():
    """3.6c 已是纯代码步骤，不得残留 LLM 计数。"""
    import inspect
    source = inspect.getsource(module.run_prepare)
    assert 'record_llm_call(state, "extract_propositions")' not in source


def test_run_prepare_passes_through_terminal_status():
    """resume 已完成/已就绪事务应原样返回（不重跑、不返回 None），由 run_one 决定是否写图。"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "semantic.txt").write_text("三元组:\n本论文 | 研究关键词 | 测试\n", encoding="utf-8")
        original_repo = module.REPO
        original_save = module.inbox_state.save
        try:
            module.REPO = root
            module.inbox_state.save = lambda tid, s: None
            for status in ("graph_ready", "completed"):
                state = {"status": status, "semantic_path": "semantic.txt",
                         "wiki_path": "academic/wiki/papers/g", "transaction_id": "txn-g",
                         "paper_id": "g", "source": "inbox/g.pdf"}
                result = module.run_prepare(state)
                assert result is not None, f"run_prepare returned None for status={status}"
                assert result["status"] == status, f"status changed: {status} → {result['status']}"
        finally:
            module.REPO = original_repo
            module.inbox_state.save = original_save


def _inbox_batch_harness(prep_states, commit_to="completed"):
    """跑 run_inbox_batch，用桩替换 prepare/commit；返回 (exit_code, stdout, calls)。"""
    import io, contextlib
    calls = {"prepare": [], "commit": []}
    def fake_phase(state, verbose, fn):
        name = state["source"].split("/")[-1]
        if fn is module.run_prepare:
            calls["prepare"].append(name)
            return dict(prep_states[name])
        calls["commit"].append(name)
        s = dict(prep_states[name]); s["status"] = commit_to; return s
    orig = (module.REPO, module.inbox_pdf_paths, module.find_ready_txn,
            module.new_state_for_pdf, module._run_phase)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inbox = root / "inbox"; inbox.mkdir()
        names = list(prep_states)
        for n in names:
            (inbox / n).write_bytes(b"x")
        try:
            module.REPO = root
            module.inbox_pdf_paths = lambda: [inbox / n for n in names]
            module.find_ready_txn = lambda src: None
            module.new_state_for_pdf = lambda p: {
                "source": "inbox/" + p.name, "transaction_id": "t-" + p.stem,
                "paper_id": p.stem, "status": "init"}
            module._run_phase = fake_phase
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = module.run_inbox_batch(False)
        finally:
            (module.REPO, module.inbox_pdf_paths, module.find_ready_txn,
             module.new_state_for_pdf, module._run_phase) = orig
    return code, buf.getvalue(), calls


def test_run_inbox_batch_barrier_holds_when_not_ready():
    code, out, calls = _inbox_batch_harness({
        "a.pdf": {"source": "inbox/a.pdf", "status": "graph_ready", "transaction_id": "t-a", "paper_id": "a"},
        "b.pdf": {"source": "inbox/b.pdf", "status": "agent_required", "transaction_id": "t-b", "paper_id": "b", "errors": ["w"]},
    })
    assert code == 1
    assert sorted(calls["prepare"]) == ["a.pdf", "b.pdf"]
    assert calls["commit"] == [], "barrier must hold: no commit when a paper is not ready"
    assert '"phase": "prepare"' in out and '"status": "partial"' in out
    payload = json.loads(out)
    failed_item = next(item for item in payload["items"] if item["status"] == "agent_required")
    assert failed_item["failure_disposition"]["category"] == "semantic_decision"
    assert failed_item["failure_disposition"]["owner"] == "specialist_agent"


def test_run_inbox_batch_commits_when_all_ready():
    code, out, calls = _inbox_batch_harness({
        "a.pdf": {"source": "inbox/a.pdf", "status": "graph_ready", "transaction_id": "t-a", "paper_id": "a"},
        "b.pdf": {"source": "inbox/b.pdf", "status": "graph_ready", "transaction_id": "t-b", "paper_id": "b"},
    })
    assert code == 0
    assert sorted(calls["prepare"]) == ["a.pdf", "b.pdf"]
    assert calls["commit"] == ["a.pdf", "b.pdf"], "phase 2 must commit all ready papers"
    assert '"phase": "commit"' in out and '"status": "completed"' in out


def test_run_inbox_batch_prepare_uses_bounded_parallelism_and_serial_commit():
    import contextlib
    import io
    import threading
    import time
    active = 0
    max_active = 0
    commit_order = []
    lock = threading.Lock()
    original = (module.REPO, module.inbox_pdf_paths, module.find_ready_txn,
                module.new_state_for_pdf, module._run_phase)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inbox = root / "inbox"
        inbox.mkdir()
        paths = [inbox / name for name in ("a.pdf", "b.pdf", "c.pdf")]
        for path in paths:
            path.write_bytes(b"x")
        def fake_phase(state, verbose, fn):
            nonlocal active, max_active
            if fn is module.run_prepare:
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                return {**state, "status": "graph_ready"}
            commit_order.append(state["source"])
            return {**state, "status": "completed"}
        try:
            module.REPO = root
            module.inbox_pdf_paths = lambda: paths
            module.find_ready_txn = lambda source: None
            module.new_state_for_pdf = lambda path: {
                "source": "inbox/" + path.name,
                "transaction_id": "t-" + path.stem,
                "paper_id": path.stem,
                "status": "init",
            }
            module._run_phase = fake_phase
            with contextlib.redirect_stdout(io.StringIO()):
                assert module.run_inbox_batch(False) == 0
        finally:
            (module.REPO, module.inbox_pdf_paths, module.find_ready_txn,
             module.new_state_for_pdf, module._run_phase) = original
    assert max_active == 2, f"prepare 并发上限应为2，got {max_active}"
    assert commit_order == ["inbox/a.pdf", "inbox/b.pdf", "inbox/c.pdf"]


def test_is_blocking_warning_descriptive_phrase_is_nonblocking():
    """proposition 改革：descriptive_phrase（subject 与 object）均为非阻断。

    命题谓词(核心创新点/局限性/未来展望)的 object 现作为 proposition 节点入图，
    不再触发 3.6b LLM 局部重写；bare_abbreviation 仍非阻断；
    duplicate_line / 未知 issue 仍阻断。
    """
    is_blocking = lambda w: module.ic.is_blocking_warning(w, module.NON_BLOCKING_ISSUES)
    # 命题 object 现非阻断（改革核心）
    assert is_blocking({"issue": "descriptive_phrase", "field": "object"}) is False
    # 主体侧仍非阻断
    assert is_blocking({"issue": "descriptive_phrase", "field": "subject"}) is False
    # 图入库期 warning 不带 field，也应非阻断
    assert is_blocking({"issue": "descriptive_phrase"}) is False
    # bare_abbreviation 仍非阻断
    assert is_blocking({"issue": "bare_abbreviation"}) is False
    # duplicate_line 仍阻断（机械去重）
    assert is_blocking({"issue": "duplicate_line"}) is True
    # 未知 issue 默认阻断
    assert is_blocking({"issue": "unknown_issue"}) is True



# ===== from_raw / reingest / clean 对齐测试 =====

def test_new_state_for_raw_sets_from_raw_flag():
    """from_raw 模式：raw 已在位，跳过 dedup+extract，状态从 write_wiki 开始。"""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw_dir = root / "academic" / "raw" / "references" / "smith-2020-test-paper"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.md").write_text("# Test", encoding="utf-8")
        original_repo = module.REPO
        try:
            module.REPO = root
            state = module.new_state_for_raw(raw_dir / "paper.md")
        finally:
            module.REPO = original_repo
    assert state["from_raw"] is True
    assert state["status"] == "write_wiki"
    assert state["paper_id"] == "smith-2020-test-paper"
    assert state["raw_dir"] == "academic/raw/references/smith-2020-test-paper"
    assert state["wiki_path"] == "academic/wiki/papers/smith-2020-test-paper"
    assert state["source"].endswith("paper.md")
    assert state["bibliographic_meta"] == {}


def test_step_update_graph_clean_passes_flag():
    """clean=True 时 graph_ingest 命令含 --clean；clean=False 时不含。"""
    import ingest_common as ic
    captured = []
    original_run = ic.run_tracked
    try:
        def fake_run(cmd, repo, state=None, label=None):
            captured.append(cmd)
            return '{"edges_added": 5}'
        ic.run_tracked = fake_run
        state = {"wiki_path": "academic/wiki/papers/test", "semantic_path": "temp/test.sem"}
        ic.step_update_graph(state, module.REPO, clean=True)
        assert "--clean" in captured[0]
        state2 = {"wiki_path": "academic/wiki/papers/test2", "semantic_path": "temp/test2.sem"}
        ic.step_update_graph(state2, module.REPO, clean=False)
        assert "--clean" not in captured[1]
    finally:
        ic.run_tracked = original_run


def test_step_update_graph_default_no_clean():
    """默认（不传 clean）不加 --clean，保持 inbox create 行为不变。"""
    import ingest_common as ic
    captured = []
    original_run = ic.run_tracked
    try:
        def fake_run(cmd, repo, state=None, label=None):
            captured.append(cmd)
            return '{"edges_added": 3}'
        ic.run_tracked = fake_run
        state = {"wiki_path": "academic/wiki/papers/test", "semantic_path": "temp/test.sem"}
        ic.step_update_graph(state, module.REPO)
        assert "--clean" not in captured[0]
    finally:
        ic.run_tracked = original_run


def test_graph_navigation_soft_gaps_degrade_quality_status():
    state = {
        "quality_warnings": [],
        "graph_report": {"graph_delta": {
            "subgraph": {"semantic_edges": 3},
            "query_probes": {
                "boundary_total": 11,
                "boundary_reachable_within_2": 8,
                "ambiguous_mentions": 3,
            },
        }},
    }
    module._record_graph_quality_warnings(state)
    assert [warning["issue"] for warning in state["quality_warnings"]] == [
        "graph_semantic_coverage_sparse",
        "graph_navigation_incomplete",
        "graph_navigation_ambiguous",
    ]

    state["graph_report"]["graph_delta"]["subgraph"]["semantic_edges"] = 4
    state["graph_report"]["graph_delta"]["query_probes"] = {
        "boundary_total": 4,
        "boundary_reachable_within_2": 4,
        "ambiguous_mentions": 0,
    }
    module._record_graph_quality_warnings(state)
    assert state["quality_warnings"] == []


def test_sparse_slots_retry_is_bounded_and_keeps_higher_coverage():
    import tempfile
    from pathlib import Path

    old_repo = module.REPO
    try:
        with tempfile.TemporaryDirectory() as tmp:
            module.REPO = Path(tmp)
            semantic_path = module.REPO / "slots.txt"
            semantic_path.write_text("first", encoding="utf-8")
            state = {
                "semantic_path": "slots.txt",
                "slots_content": "first",
                "semantic_triple_count": 2,
                "slots_retry": 0,
            }
            assert module._handle_sparse_slots(state) == "retry"
            assert state["sparse_slots_retry"] == 1
            assert state["slots_content"] == ""
            assert state["_skip_wiki_for_slots_resume"] is True
            assert "成功解析 2 条 Worker 三元组" in state["slots_errors"][0]
            assert state["_sparse_slots_best"]["content"] == "first"

            semantic_path.write_text("worse", encoding="utf-8")
            state["slots_content"] = "worse"
            state["semantic_triple_count"] = 1
            assert module._handle_sparse_slots(state) == "restored"
            assert state["slots_content"] == "first"
            assert semantic_path.read_text(encoding="utf-8") == "first"
            assert state["semantic_coverage"] == {
                "minimum": module.MIN_SEMANTIC_TRIPLES,
                "selected_count": 2,
                "retry_count": 1,
                "status": "sparse",
            }
    finally:
        module.REPO = old_repo


def test_step_finalize_tail_skip_index():
    """skip_index=True 时跳过 index.md，仅写 log.md + catalog。"""
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "academic" / "wiki").mkdir(parents=True)
        log_path = root / "academic" / "wiki" / "log.md"
        index_path = root / "academic" / "wiki" / "index.md"
        config = {
            "doc_id_key": "paper_id",
            "get_log_path": lambda state, REPO: log_path,
            "get_index_path": lambda state, REPO: index_path,
            "skip_index": True,
            "build_log_entry": lambda ctx: f"## re-ingest {ctx['doc_id']}\n",
        }
        original_run = ic.run
        try:
            ic.run = lambda cmd, repo: ''
            state = {"paper_id": "test-2020", "wiki_path": "academic/wiki/papers/test-2020",
                     "graph_report": {"edges_added": 10}}
            ok, msg = ic.step_finalize_tail(state, root, config)
            assert ok, f"should succeed: {msg}"
            assert log_path.exists(), "log.md should be written"
            assert not index_path.exists(), "index.md should be skipped"
        finally:
            ic.run = original_run


def test_step_finalize_tail_no_skip_writes_index():
    """skip_index 未设或 False 时正常写 index.md。"""
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "academic" / "wiki").mkdir(parents=True)
        log_path = root / "academic" / "wiki" / "log.md"
        index_path = root / "academic" / "wiki" / "index.md"
        config = {
            "doc_id_key": "paper_id",
            "get_log_path": lambda state, REPO: log_path,
            "get_index_path": lambda state, REPO: index_path,
            "build_log_entry": lambda ctx: f"## ingest {ctx['doc_id']}\n",
        }
        original_run = ic.run
        try:
            ic.run = lambda cmd, repo: ''
            state = {"paper_id": "test-2020", "wiki_path": "academic/wiki/papers/test-2020",
                     "graph_report": {"edges_added": 5}}
            ok, msg = ic.step_finalize_tail(state, root, config)
            assert ok, f"should succeed: {msg}"
            assert log_path.exists(), "log.md should be written"
            assert index_path.exists(), "index.md should be written"
        finally:
            ic.run = original_run


def test_step_finalize_tail_frontier_failure_is_nonblocking():
    """Frontier 后置候选捕获失败只记 warning，不得让事实摄入失败。"""
    import ingest_common as ic
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "academic" / "wiki").mkdir(parents=True)
        log_path = root / "academic" / "wiki" / "log.md"
        index_path = root / "academic" / "wiki" / "index.md"
        config = {
            "doc_id_key": "paper_id",
            "get_log_path": lambda state, REPO: log_path,
            "get_index_path": lambda state, REPO: index_path,
            "build_log_entry": lambda ctx: f"## ingest {ctx['doc_id']}\n",
            "frontier_capture": True,
        }
        original_run, original_subprocess = ic.run, ic.subprocess.run
        try:
            ic.run = lambda cmd, repo: ''
            ic.subprocess.run = lambda *a, **kw: type("R", (), {
                "returncode": 1, "stdout": "", "stderr": "simulated frontier failure",
            })()
            state = {"paper_id": "test-2020", "wiki_path": "academic/wiki/papers/test-2020",
                     "graph_report": {"edges_added": 5}}
            ok, msg = ic.step_finalize_tail(state, root, config)
            assert ok, f"Frontier 失败不应阻断 ingest: {msg}"
            assert any("Frontier 候选捕获失败" in item for item in state.get("warnings", []))
        finally:
            ic.run, ic.subprocess.run = original_run, original_subprocess


def test_finalize_tail_config_from_raw_log():
    """from_raw=True 时 log 文案含「raw 已在位」，不含「inbox PDF」。"""
    config = module.FINALIZE_TAIL_CONFIG
    ctx_from_raw = {
        "today": "2026-08-13", "doc_id": "smith-2020-test", "page_name": "smith-2020-test",
        "title": "Test Paper", "edges": 15,
        "report": {"derived_directions": {}}, "state": {"from_raw": True}, "fm": {},
    }
    entry = config["build_log_entry"](ctx_from_raw)
    assert "raw 已在位" in entry
    assert "inbox PDF" not in entry

    ctx_inbox = {**ctx_from_raw, "state": {}}
    entry_inbox = config["build_log_entry"](ctx_inbox)
    assert "inbox PDF" in entry_inbox
    assert "raw 已在位" not in entry_inbox


def test_reingest_state_has_reingest_flag():
    """re-ingest 状态带 reingest=True，run_prepare 会走 propositions_done 分支。"""
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw_dir = root / "academic" / "raw" / "references" / "doe-2019-example"
        raw_dir.mkdir(parents=True)
        (raw_dir / "paper.md").write_text("# Example", encoding="utf-8")
        original_repo, original_ri_repo = module.REPO, ri.REPO
        original_temp = ri.TEMP_REINGEST
        try:
            module.REPO = root
            ri.REPO = root
            ri.TEMP_REINGEST = root / "temp" / "reingest-extract"
            state = ri.new_state_for_reingest("doe-2019-example", "academic/raw/references/doe-2019-example/paper.md")
        finally:
            module.REPO = original_repo
            ri.REPO = original_ri_repo
            ri.TEMP_REINGEST = original_temp
    assert state["reingest"] is True
    assert state["paper_id"] == "doe-2019-example"
    assert state["status"] == "write_wiki"
    assert state["bibliographic_meta"] == {}


def test_reingest_repairs_archived_bibliography_without_mutating_raw():
    """旧 Raw 锁错书目只在事务状态修正，source.yaml 保持不可变。"""
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw_dir = root / "academic" / "raw" / "references" / "acin-2018-roadmap"
        raw_dir.mkdir(parents=True)
        raw_md = raw_dir / "paper.md"
        raw_md.write_text(
            "# The roadmap\n\nwrapper\n\n# The roadmap\n"
            "Antonio Acín, Immanuel Bloch, Harry Buhrman\n",
            encoding="utf-8",
        )
        source_yaml = raw_dir / "source.yaml"
        original_source = (
            "bibliographic:\n"
            "  title: PHYSICAL REVIEW B 96, 195145 (2017) Machine learning topological states\n"
            "  authors:\n"
            "  - Antonio Acín\n"
        )
        source_yaml.write_text(original_source, encoding="utf-8")
        original_repo, original_ri_repo = module.REPO, ri.REPO
        original_temp = ri.TEMP_REINGEST
        try:
            module.REPO = root
            ri.REPO = root
            ri.TEMP_REINGEST = root / "temp" / "reingest-extract"
            state = ri.new_state_for_reingest(
                "acin-2018-roadmap",
                "academic/raw/references/acin-2018-roadmap/paper.md",
            )
            source_after = source_yaml.read_text(encoding="utf-8")
        finally:
            module.REPO = original_repo
            ri.REPO = original_ri_repo
            ri.TEMP_REINGEST = original_temp
    assert state["bibliographic_meta"]["title"] == "Machine learning topological states"
    assert state["bibliographic_meta"]["authors"] == [
        "Antonio Acín", "Immanuel Bloch", "Harry Buhrman",
    ]
    assert {item["field"] for item in state["bibliographic_corrections"]} == {"title", "authors"}
    assert source_after == original_source


def test_reingest_runs_evidence_bound_bibliographic_review():
    """re-ingest must not bypass the candidate-id bibliography Worker gate."""
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw_dir = root / "academic" / "raw" / "references" / "terilla-2020-example"
        raw_dir.mkdir(parents=True)
        raw_md = raw_dir / "paper.md"
        raw_md.write_text(
            "# Modeling sequences with quantum states\n\n"
            "Tai-Danae Bradley, E M Stoudenmire and John Terilla\n",
            encoding="utf-8",
        )
        source_yaml = raw_dir / "source.yaml"
        original_source = (
            "bibliographic:\n"
            "  title: Modeling sequences with quantum states\n"
            "  authors:\n"
            "  - Tai-Danae Bradley ,E M Stoudenmire ,John Terilla\n"
        )
        source_yaml.write_text(original_source, encoding="utf-8")
        corrected = {
            "title": "Modeling sequences with quantum states",
            "authors": ["Tai-Danae Bradley", "E M Stoudenmire", "John Terilla"],
            "year": "2020",
        }
        calls = []
        original_repo, original_ri_repo = module.REPO, ri.REPO
        original_temp = ri.TEMP_REINGEST
        original_review = ri.ip.review_bibliographic_metadata
        original_record = ri.ip._record_bibliographic_quality_warnings
        try:
            module.REPO = root
            ri.REPO = root
            ri.TEMP_REINGEST = root / "temp" / "reingest-extract"
            state = ri.new_state_for_reingest(
                "terilla-2020-example",
                "academic/raw/references/terilla-2020-example/paper.md",
            )
            ri.ip.review_bibliographic_metadata = lambda bibliography, text, txn: (
                calls.append((bibliography, text, txn))
                or {
                    "ok": True,
                    "status": "ok",
                    "bibliographic": corrected,
                    "review": {"review_status": "corrected"},
                    "decision": {"protocol_version": "candidate-id-v2"},
                    "candidates": {},
                    "catalog": {},
                    "input_hash": "hash",
                    "worker": {"api_called": True},
                }
            )
            ri.ip._record_bibliographic_quality_warnings = lambda *_args: None
            assert ri.review_reingest_bibliography(state) is True
            source_after = source_yaml.read_text(encoding="utf-8")
        finally:
            module.REPO = original_repo
            ri.REPO = original_ri_repo
            ri.TEMP_REINGEST = original_temp
            ri.ip.review_bibliographic_metadata = original_review
            ri.ip._record_bibliographic_quality_warnings = original_record
    assert len(calls) == 1
    assert calls[0][2] == state["transaction_id"]
    assert state["bibliographic_meta"]["authors"] == corrected["authors"]
    assert state["bibliographic_review"]["worker"]["api_called"] is True
    assert source_after == original_source


def test_reingest_bibliographic_worker_escalates_without_prepare_commit():
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        extract_dir = root / "temp" / "reingest-extract" / "txn-review"
        extract_dir.mkdir(parents=True)
        (extract_dir / "paper.md").write_text("# Paper\nAlice and Bob\n", encoding="utf-8")
        state = {
            "transaction_id": "txn-review",
            "status": "write_wiki",
            "extract_dir": "temp/reingest-extract/txn-review",
            "bibliographic_meta": {"authors": ["Alice and Bob"]},
        }
        original_repo = ri.REPO
        original_review = ri.ip.review_bibliographic_metadata
        try:
            ri.REPO = root
            ri.ip.review_bibliographic_metadata = lambda *_args, **_kwargs: {
                "ok": False,
                "status": "agent_required",
                "agent_prompt": "review authors",
                "input_hash": "hash",
                "worker": {"api_called": True},
            }
            assert ri.review_reingest_bibliography(state) is False
        finally:
            ri.REPO = original_repo
            ri.ip.review_bibliographic_metadata = original_review
    assert state["status"] == "agent_required"
    assert state["pre_handoff_status"] == "write_wiki"
    assert "re_ingest.py --resume txn-review" in state["agent_prompt"]


def test_reingest_restores_wiki_when_graph_update_fails():
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        wiki_path = root / "academic" / "wiki" / "papers" / "demo.md"
        wiki_path.parent.mkdir(parents=True)
        wiki_path.write_text("old wiki\n", encoding="utf-8")
        extract_dir = root / "temp" / "reingest-extract" / "txn"
        extract_dir.mkdir(parents=True)
        (extract_dir / "wiki.md").write_text("new wiki\n", encoding="utf-8")
        state = {
            "transaction_id": "txn", "paper_id": "demo",
            "wiki_path": "academic/wiki/papers/demo",
            "extract_dir": "temp/reingest-extract/txn",
        }
        original_repo = ri.REPO
        original_update = ri.ic.step_update_graph
        original_save = ri.inbox_state.save
        try:
            ri.REPO = root
            ri.ic.step_update_graph = lambda *_args, **_kwargs: (False, "simulated graph failure")
            ri.inbox_state.save = lambda *_args, **_kwargs: None
            result = ri.commit_wiki_and_graph(state)
            wiki_after = wiki_path.read_text(encoding="utf-8")
        finally:
            ri.REPO = original_repo
            ri.ic.step_update_graph = original_update
            ri.inbox_state.save = original_save
    assert result["status"] == "failed"
    assert result["wiki_restored"] is True
    assert result["errors"] == ["simulated graph failure"]
    assert wiki_after == "old wiki\n"


def test_reingest_current_raw_skips_generation_without_force():
    import contextlib
    import io
    import re_ingest as ri
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw_path = root / "academic" / "raw" / "references" / "demo" / "paper.md"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_text("# Demo\n", encoding="utf-8")
        original_repo = ri.REPO
        original_version = ri.page_ingest_version
        original_argv = sys.argv
        try:
            ri.REPO = root
            ri.page_ingest_version = lambda _paper_id: 999
            sys.argv = ["re_ingest.py", "--raw", str(raw_path.relative_to(root))]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = ri.main()
        finally:
            ri.REPO = original_repo
            ri.page_ingest_version = original_version
            sys.argv = original_argv
    payload = json.loads(output.getvalue())
    assert result == 0
    assert payload["status"] == "completed"
    assert payload["items"][0]["status"] == "up_to_date"
    assert payload["items"][0]["api_called"] is False


def main():
    import graph_lib as gl
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    original_graph_db = gl.GRAPH_DB
    with tempfile.TemporaryDirectory() as directory:
        graph_path = Path(directory) / "graph.db"
        conn = sqlite3.connect(graph_path)
        gl.init_schema(conn)
        conn.close()
        gl.GRAPH_DB = graph_path
        try:
            for test in tests:
                try:
                    test()
                    passed += 1
                except AssertionError as exc:
                    print(f"FAIL {test.__name__}: {exc}")
                    return 1
        finally:
            gl.GRAPH_DB = original_graph_db
    print(f"ingest_paper regression: {passed}/{len(tests)} PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

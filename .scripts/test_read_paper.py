#!/usr/bin/env python3
"""read_paper.py 定向截取回归测试。"""
import importlib.util
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("read_paper.py")
spec = importlib.util.spec_from_file_location("read_paper", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def _paper(text):
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
    tmp.write(text)
    tmp.close()
    return Path(tmp.name)


def test_method_prefers_main_section_over_appendix():
    paper = _paper(
        "# Title\n\n## 3 SELF-RAG: LEARNING TO RETRIEVE\n\nmethod body\n\n"
        "## A.2 ADVANTAGES OF LEARNING-BASED METHODS\n\nappendix body\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["method"])
        assert not misses, misses
        assert hits[0][1].startswith("3 SELF-RAG"), hits
        assert "method body" in hits[0][2]
        assert "appendix body" not in hits[0][2]
    finally:
        paper.unlink()


def test_method_absorbs_related_child_sections():
    paper = _paper(
        "# Title\n\n## 3 SELF-RAG: LEARNING TO RETRIEVE\n\noverview\n\n"
        "## 3.1 PROBLEM FORMALIZATION AND OVERVIEW\n\nformal def\n\n"
        "## 3.2 SELF-RAG TRAINING\n\ntraining detail\n\n"
        "## 3.3 SELF-RAG INFERENCE\n\ninference detail\n\n"
        "## 4 EXPERIMENTS\n\nbenchmark\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["method"])
        assert not misses, misses
        assert "formal def" in hits[0][2]
        assert "training detail" in hits[0][2]
        assert "inference detail" in hits[0][2]
        assert "benchmark" not in hits[0][2]
        assert hits[0][1].startswith("3 SELF-RAG"), hits
    finally:
        paper.unlink()


def test_results_prefers_results_over_experiments():
    paper = _paper(
        "# Title\n\n## 4 EXPERIMENTS\n\nsetup only\n\n"
        "## 5 RESULTS AND ANALYSIS\n\n## 5.1 MAIN RESULTS\n\nbenchmark 54.9\n\n"
        "## 5.2 ANALYSIS\n\ndiscussion\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["results"])
        assert not misses, misses
        assert "5 RESULTS" in hits[0][1], hits
        assert "benchmark 54.9" in hits[0][2]
        assert "discussion" not in hits[0][2]
    finally:
        paper.unlink()


def test_is_appendix_section():
    assert module.is_appendix_section("A.2 ADVANTAGES")
    assert module.is_appendix_section("APPENDIX")
    assert not module.is_appendix_section("5 RESULTS AND ANALYSIS")
    assert not module.is_appendix_section("3 SELF-RAG")


def test_section_match_score_weighting():
    assert module.section_match_score("results", "5 RESULTS AND ANALYSIS") > \
           module.section_match_score("results", "4 EXPERIMENTS")


def test_spaced_all_caps_sections_match_without_changing_original_heading():
    paper = _paper(
        "# Title\n\n## A B S T R A C T\n\nreal abstract\n\n"
        "## 5. C O N C L U S I O N S\n\nreal conclusion\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["abstract", "conclusions"])
    finally:
        paper.unlink()
    assert not misses, misses
    assert hits[0][1] == "A B S T R A C T"
    assert hits[1][1] == "5. C O N C L U S I O N S"


def test_unknown_spaced_heading_does_not_match_conclusion():
    assert module.normalize_heading_for_match("C A U S A L") == "C A U S A L"
    assert module.section_match_score("conclusions", "C A U S A L") == 0
    assert module.normalize_heading_for_match("I N T R O D U C T I O N") == "INTRODUCTION"


def test_preamble_without_abstract_boundary_is_reported_missing():
    paper = _paper(
        "# Paper title\n\nAlice Example\nInstitute of Materials\nSomewhere\n\n"
        "## 1 Introduction\n\nbody\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["abstract"])
    finally:
        paper.unlink()
    assert hits == []
    assert misses == ["abstract"]


def main():
    test_method_prefers_main_section_over_appendix()
    test_method_absorbs_related_child_sections()
    test_results_prefers_results_over_experiments()
    test_is_appendix_section()
    test_section_match_score_weighting()
    print("read_paper regression: PASS")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""read_paper.py 定向截取回归测试。"""
import importlib.util
import re
import subprocess
import sys
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


def test_multilevel_main_sections_precede_matching_appendix():
    paper = _paper(
        "# Title\n\n### I. INTRODUCTION\n\nIntroduction evidence.\n\n"
        "### VI. SUMMARY AND DISCUSSION\n\n"
        "### A. Summary\n\nSummary evidence.\n\n"
        "### B. Context\n\nOnly under stated assumptions; not a general result.\n\n"
        "### C. Practical implications\n\nPractical boundary.\n\n"
        "### D. Limits\n\nDimension requirement.\n\n"
        "## APPENDIX A: DETAILED DISCUSSION\n\nAppendix evidence.\n"
    )
    try:
        hits, misses, _ = module.extract_sections(
            paper, ["introduction", "discussion"], source_lines=True, source_path="raw/paper.md",
        )
        assert not misses, misses
        assert hits[0][1] == "I. INTRODUCTION"
        assert hits[1][1] == "VI. SUMMARY AND DISCUSSION"
        assert "Only under stated assumptions" in hits[1][2]
        assert "Practical boundary" in hits[1][2]
        assert "Dimension requirement" in hits[1][2]
        assert "Appendix evidence" not in hits[1][2]
        original = paper.read_text().splitlines()
        for _requested, _title, content in hits:
            for line in content.splitlines():
                if line:
                    matched = re.fullmatch(r"<raw/paper.md#L(\d+)> (.*)", line)
                    assert matched, line
                    assert matched.group(2) == original[int(matched.group(1)) - 1]
    finally:
        paper.unlink()


def test_nested_markdown_levels_keep_child_evidence():
    paper = _paper(
        "# Title\n\n## Discussion\n\n### Assumptions\n\nCondition evidence.\n\n"
        "#### Scope\n\nLimited setting.\n\n## References\n\nReference evidence.\n"
    )
    try:
        hits, misses, _ = module.extract_sections(paper, ["discussion"])
        assert not misses
        assert "Condition evidence" in hits[0][2]
        assert "Limited setting" in hits[0][2]
        assert "Reference evidence" not in hits[0][2]
    finally:
        paper.unlink()


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


def test_source_handles_preserve_original_noncontiguous_lines():
    text = (
        "# Title\n\n## 3 Method\nOverview\n"
        "## 3.1 Training\nRepeated evidence\n"
        "## 3.2 Dataset\nNot a method subsection\n"
        "## 3.3 Inference\nRepeated evidence\n"
        "## 4 Results\nDistinct result\n"
    )
    paper = _paper(text)
    try:
        plain, _, _ = module.extract_sections(paper, ["method", "results"])
        located, missing, _ = module.extract_sections(
            paper, ["method", "results"], source_lines=True, source_path="raw/paper.md",
        )
        assert not missing
        for original, annotated in zip(plain, located):
            assert re.sub(r"^<[^>]+#L\d+> ", "", annotated[2], flags=re.M) == original[2]
            for line in annotated[2].splitlines():
                match = re.match(r"<raw/paper.md#L(\d+)> (.*)", line)
                if match:
                    assert text.split('\n')[int(match[1]) - 1] == match[2]
        assert "#L6> Repeated evidence" in located[0][2]
        assert "#L10> Repeated evidence" in located[0][2]
        assert "Dataset" not in located[0][2]
        assert "#L12> Distinct result" in located[1][2]
    finally:
        paper.unlink()


def test_source_handles_cover_fallbacks_and_full_cli():
    text = "# Title\nReceived today\n\nActual abstract\nDOI: example\n[1] First ref\ncontinued\n"
    paper = _paper(text)
    try:
        hits, missing, _ = module.extract_sections(
            paper, ["abstract", "references"], source_lines=True,
        )
        assert not missing
        assert f"<{paper}#L4> Actual abstract" in hits[0][2]
        assert f"<{paper}#L6> [1] First ref" in hits[1][2]
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(paper), "--full", "--source-lines"],
            capture_output=True, text=True, check=True,
        )
        assert f"<{paper}#L1> # Title" in result.stdout
        assert f"<{paper}#L7> continued" in result.stdout
        window = subprocess.run(
            [sys.executable, str(SCRIPT), str(paper), "--full", "--source-lines",
             "--start-line", "4", "--end-line", "4"],
            capture_output=True, text=True, check=True,
        )
        assert window.stdout == f"<{paper}#L4> Actual abstract\n"
        selected = subprocess.run(
            [sys.executable, str(SCRIPT), str(paper), "--source-lines", "abstract", "references"],
            capture_output=True, text=True, check=True,
        )
        assert f"<{paper}#L4> Actual abstract" in selected.stdout
        assert f"<{paper}#L6> [1] First ref" in selected.stdout
    finally:
        paper.unlink()


def test_abstract_handles_do_not_add_trailing_blank_evidence():
    paper = _paper("# Title\nReceived today\n\nActual abstract\n\nDOI: example\n")
    try:
        plain, _, _ = module.extract_sections(paper, ["abstract"])
        located, _, _ = module.extract_sections(paper, ["abstract"], source_lines=True)
        assert re.sub(r"^<[^>]+#L\d+> ", "", located[0][2], flags=re.M) == plain[0][2]
    finally:
        paper.unlink()


def main():
    for name, test in sorted(globals().items()):
        if name.startswith("test_") and callable(test):
            test()
    print("read_paper regression: PASS")


if __name__ == "__main__":
    main()

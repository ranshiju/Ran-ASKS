import tempfile
from pathlib import Path

import extractor


def test_single_noncanonical_pdf_is_copied_to_paper_pdf():
    with tempfile.TemporaryDirectory() as directory:
        paper_dir = Path(directory) / "demo"
        paper_dir.mkdir()
        source = paper_dir / "original title.pdf"
        source.write_bytes(b"pdf")
        old_papers_dir = extractor.PAPERS_DIR
        old_extractors = extractor.extract_pymupdf
        extractor.PAPERS_DIR = Path(directory)
        extractor.extract_pymupdf = lambda path, paper_id: "# Demo\n"
        try:
            assert extractor.extract_paper("demo", engine="pymupdf")
            assert (paper_dir / "paper.pdf").read_bytes() == b"pdf"
            assert (paper_dir / "paper.md").exists()
        finally:
            extractor.PAPERS_DIR = old_papers_dir
            extractor.extract_pymupdf = old_extractors


def test_multiple_noncanonical_pdfs_are_rejected():
    with tempfile.TemporaryDirectory() as directory:
        paper_dir = Path(directory) / "demo"
        paper_dir.mkdir()
        (paper_dir / "one.pdf").write_bytes(b"1")
        (paper_dir / "two.pdf").write_bytes(b"2")
        old_papers_dir = extractor.PAPERS_DIR
        extractor.PAPERS_DIR = Path(directory)
        try:
            assert not extractor.extract_paper("demo", engine="pymupdf")
            assert not (paper_dir / "paper.pdf").exists()
        finally:
            extractor.PAPERS_DIR = old_papers_dir


if __name__ == "__main__":
    test_single_noncanonical_pdf_is_copied_to_paper_pdf()
    test_multiple_noncanonical_pdfs_are_rejected()
    print("extractor regression: PASS")

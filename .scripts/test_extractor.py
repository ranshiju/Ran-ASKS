import tempfile
from pathlib import Path

import extractor
from mineru_api import MinerUError, MinerUExtraction


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


def test_mineru_bundle_keeps_only_referenced_images_and_sidecars():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paper_dir = root / "paper"
        artifact_root = root / "result"
        (artifact_root / "images").mkdir(parents=True)
        markdown_path = artifact_root / "full.md"
        markdown = "# Demo\n\n![](images/keep.png)\n"
        markdown_path.write_text(markdown, encoding="utf-8")
        keep = artifact_root / "images/keep.png"
        keep.write_bytes(b"keep")
        (artifact_root / "images/unreferenced.png").write_bytes(b"drop")
        layout = artifact_root / "layout.json"
        layout.write_text("{}", encoding="utf-8")
        checkpoint = root / "job.json"
        checkpoint.write_text("{}", encoding="utf-8")
        bundle = MinerUExtraction(
            markdown=markdown,
            markdown_path=markdown_path,
            artifact_root=artifact_root,
            image_paths=(keep,),
            sidecar_paths=(layout,),
            meta={"batch_id": "batch-1"},
            checkpoint_path=checkpoint,
        )
        paper_dir.mkdir()
        meta = extractor._commit_mineru_document_bundle(
            paper_dir,
            paper_dir / "paper.md",
            extractor.ExtractionContent(markdown, bundle.meta, bundle),
        )
        assert (paper_dir / "paper.md").read_text(encoding="utf-8") == markdown
        assert (paper_dir / "images/keep.png").read_bytes() == b"keep"
        assert not (paper_dir / "images/unreferenced.png").exists()
        assert (paper_dir / "mineru/layout.json").is_file()
        assert meta["referenced_image_count"] == 1
        assert meta["sidecar_count"] == 1


def test_mineru_bundle_rejects_missing_local_image_without_partial_markdown():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paper_dir = root / "paper"
        artifact_root = root / "result"
        artifact_root.mkdir()
        paper_dir.mkdir()
        markdown_path = artifact_root / "full.md"
        markdown = "# Demo\n\n![](images/missing.png)\n"
        markdown_path.write_text(markdown, encoding="utf-8")
        bundle = MinerUExtraction(
            markdown=markdown,
            markdown_path=markdown_path,
            artifact_root=artifact_root,
            image_paths=(),
            sidecar_paths=(),
            meta={},
            checkpoint_path=root / "job.json",
        )
        try:
            extractor._commit_mineru_document_bundle(
                paper_dir,
                paper_dir / "paper.md",
                extractor.ExtractionContent(markdown, {}, bundle),
            )
        except MinerUError:
            pass
        else:
            raise AssertionError("missing referenced image must fail the document bundle")
        assert not (paper_dir / "paper.md").exists()


def test_mineru_bundle_rejects_non_directory_managed_targets_without_replacing_markdown():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paper_dir = root / "paper"
        artifact_root = root / "result"
        paper_dir.mkdir()
        artifact_root.mkdir()
        (paper_dir / "paper.md").write_text("# Old\n", encoding="utf-8")
        (paper_dir / "images").write_text("not a directory", encoding="utf-8")
        markdown_path = artifact_root / "full.md"
        markdown_path.write_text("# New\n", encoding="utf-8")
        bundle = MinerUExtraction(
            markdown="# New\n",
            markdown_path=markdown_path,
            artifact_root=artifact_root,
            image_paths=(),
            sidecar_paths=(),
            meta={},
            checkpoint_path=root / "job.json",
        )
        try:
            extractor._commit_mineru_document_bundle(
                paper_dir,
                paper_dir / "paper.md",
                extractor.ExtractionContent("# New\n", {}, bundle),
            )
        except MinerUError as exc:
            assert "普通目录" in str(exc)
        else:
            raise AssertionError("managed images target must be a real directory")
        assert (paper_dir / "paper.md").read_text(encoding="utf-8") == "# Old\n"
        assert (paper_dir / "images").is_file()


if __name__ == "__main__":
    test_single_noncanonical_pdf_is_copied_to_paper_pdf()
    test_multiple_noncanonical_pdfs_are_rejected()
    test_mineru_bundle_keeps_only_referenced_images_and_sidecars()
    test_mineru_bundle_rejects_missing_local_image_without_partial_markdown()
    test_mineru_bundle_rejects_non_directory_managed_targets_without_replacing_markdown()
    print("extractor regression: PASS")

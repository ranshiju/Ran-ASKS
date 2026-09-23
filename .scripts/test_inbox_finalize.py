import importlib.util
import hashlib
import json
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("inbox_finalize.py")
spec = importlib.util.spec_from_file_location("inbox_finalize", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def workspace():
    directory = tempfile.TemporaryDirectory()
    root = Path(directory.name) / "WikiRan"
    (root / "temp/inbox-extract/item").mkdir(parents=True)
    return directory, root, root / "temp/inbox-extract/item"


def manifest(extract, raw_files, wiki_file="wiki.md"):
    path = extract / "manifest.json"
    path.write_text(json.dumps({"raw_files": raw_files, "wiki_file": wiki_file}), encoding="utf-8")
    return path


def manifest_v2(extract, artifacts, wiki_file="wiki.md"):
    entries = []
    for relative, role in artifacts:
        path = extract / relative
        payload = path.read_bytes()
        entries.append({
            "path": relative,
            "role": role,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    path = extract / "manifest.json"
    path.write_text(json.dumps({
        "schema": "inbox-artifact-manifest-v2",
        "raw_artifacts": entries,
        "wiki_file": wiki_file,
    }), encoding="utf-8")
    return path


def finalize(root, extract, cleanup=False):
    return module.finalize(
        root, "demo", root / "academic/raw/references/demo", root / "academic/wiki/papers/demo.md",
        extract, extract / "manifest.json", cleanup,
    )


def test_manifest_only_copies_declared_entity_files_and_writes_receipt():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "paper.md").write_text("# paper", encoding="utf-8")
        (extract / "entity-resolution.json").write_text("derived", encoding="utf-8")
        (extract / "wiki.md").write_text("---\ntitle: Demo\n---", encoding="utf-8")
        manifest(extract, ["paper.pdf", "paper.md"])

        receipt = finalize(root, extract)

        raw = root / "academic/raw/references/demo"
        assert (raw / "paper.pdf").read_bytes() == b"pdf"
        assert not (raw / "entity-resolution.json").exists()
        assert (root / "academic/wiki/papers/demo.md").is_file()
        data = json.loads(receipt.read_text(encoding="utf-8"))
        assert data["status"] == "committed"
        assert [entry["path"] for entry in data["raw_files"]] == ["paper.pdf", "paper.md"]
    finally:
        directory.cleanup()


def test_rejects_existing_destination_without_partial_write():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["paper.pdf"])
        existing = root / "academic/raw/references/demo"
        existing.mkdir(parents=True)

        try:
            finalize(root, extract)
            assert False, "expected collision failure"
        except ValueError as exc:
            assert "already exists" in str(exc)
        assert not (root / "academic/wiki/papers/demo.md").exists()
        assert (extract / "paper.pdf").exists()
    finally:
        directory.cleanup()


def test_allows_existing_raw_container_only_with_explicit_flag():
    directory, root, extract = workspace()
    try:
        (extract / "0731.txt").write_text("meeting", encoding="utf-8")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["0731.txt"])
        container = root / "academic/raw/conferences/2026"
        container.mkdir(parents=True)
        (container / "0730.txt").write_text("existing meeting", encoding="utf-8")

        receipt = module.finalize(
            root, "0731", container, root / "academic/wiki/conferences/0731.md",
            extract, extract / "manifest.json", False, allow_existing_raw_dir=True,
        )

        assert (container / "0730.txt").read_text(encoding="utf-8") == "existing meeting"
        assert (container / "0731.txt").read_text(encoding="utf-8") == "meeting"
        assert (root / "academic/wiki/conferences/0731.md").is_file()
        assert json.loads(receipt.read_text(encoding="utf-8"))["raw_dir"] == "academic/raw/conferences/2026"
    finally:
        directory.cleanup()


def test_existing_raw_container_rejects_manifest_file_collision():
    directory, root, extract = workspace()
    try:
        (extract / "0731.txt").write_text("new", encoding="utf-8")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["0731.txt"])
        container = root / "academic/raw/conferences/2026"
        container.mkdir(parents=True)
        (container / "0731.txt").write_text("existing", encoding="utf-8")

        try:
            module.finalize(
                root, "0731", container, root / "academic/wiki/conferences/0731.md",
                extract, extract / "manifest.json", False, allow_existing_raw_dir=True,
            )
            assert False, "expected raw file collision"
        except ValueError as exc:
            assert "raw files already exist" in str(exc)
        assert not (root / "academic/wiki/conferences/0731.md").exists()
        assert (extract / "0731.txt").exists()
    finally:
        directory.cleanup()


def test_rejects_derived_symlink_and_cleanup_only_follows_commit():
    directory, root, extract = workspace()
    try:
        external = root / "external.pdf"
        external.write_bytes(b"pdf")
        (extract / "paper.pdf").symlink_to(external)
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["paper.pdf"])

        try:
            finalize(root, extract, cleanup=True)
            assert False, "expected symlink rejection"
        except ValueError as exc:
            assert "manifest raw_files entry" in str(exc)
        assert extract.exists()
    finally:
        directory.cleanup()


def test_cleanup_removes_only_committed_item_directory():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["paper.pdf"])

        original_check = module.run_ingest_check
        module.run_ingest_check = lambda project_root, wiki_path: None
        try:
            receipt = finalize(root, extract, cleanup=True)
        finally:
            module.run_ingest_check = original_check

        assert not extract.exists()
        assert receipt.is_file()
        assert (root / "academic/raw/references/demo/paper.pdf").is_file()
    finally:
        directory.cleanup()


def test_cleanup_removes_hidden_extractor_directories():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        (extract / ".mineru").mkdir()
        (extract / ".mineru" / "trace.json").write_text("{}", encoding="utf-8")
        manifest(extract, ["paper.pdf"])
        original_check = module.run_ingest_check
        module.run_ingest_check = lambda project_root, wiki_path: None
        try:
            receipt = finalize(root, extract, cleanup=True)
        finally:
            module.run_ingest_check = original_check
        assert not extract.exists()
        assert json.loads(receipt.read_text(encoding="utf-8"))["cleanup"] == "completed"
    finally:
        directory.cleanup()


def test_failed_ingest_check_retains_committed_files_and_extract_directory():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["paper.pdf"])
        original_check = module.run_ingest_check
        module.run_ingest_check = lambda project_root, wiki_path: (_ for _ in ()).throw(ValueError("ingest_check failed"))
        try:
            finalize(root, extract, cleanup=True)
            assert False, "expected ingest-check failure"
        except ValueError as exc:
            assert "ingest_check failed" in str(exc)
        finally:
            module.run_ingest_check = original_check
        assert extract.exists()
        assert (root / "academic/raw/references/demo/paper.pdf").is_file()
        assert (root / "academic/wiki/papers/demo.md").is_file()
    finally:
        directory.cleanup()


def test_rejects_paths_outside_inbox_raw_and_wiki_boundaries():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest(extract, ["paper.pdf"])

        try:
            module.finalize(
                root, "demo", root / "outside", root / "academic/wiki/papers/demo.md",
                extract, extract / "manifest.json", False,
            )
            assert False, "expected non-raw destination rejection"
        except ValueError as exc:
            assert "raw/ directory" in str(exc)

        try:
            module.finalize(
                root, "demo", root / "academic/raw/references/demo", root / "outside.md",
                extract, extract / "manifest.json", False,
            )
            assert False, "expected non-wiki destination rejection"
        except ValueError as exc:
            assert "wiki/ directory" in str(exc)
    finally:
        directory.cleanup()


def test_v2_manifest_preserves_nested_artifacts_and_hashes():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "paper.md").write_text("# paper\n![](images/a.png)\n", encoding="utf-8")
        (extract / "images").mkdir()
        (extract / "images/a.png").write_bytes(b"image")
        (extract / "mineru").mkdir()
        (extract / "mineru/layout.json").write_text("{}", encoding="utf-8")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest_v2(extract, [
            ("paper.pdf", "source"),
            ("paper.md", "locator-companion"),
            ("images/a.png", "referenced-image"),
            ("mineru/layout.json", "extraction-sidecar"),
        ])
        receipt = finalize(root, extract)
        raw = root / "academic/raw/references/demo"
        assert (raw / "images/a.png").read_bytes() == b"image"
        assert (raw / "mineru/layout.json").is_file()
        entries = json.loads(receipt.read_text(encoding="utf-8"))["raw_files"]
        assert [item["path"] for item in entries] == [
            "paper.pdf", "paper.md", "images/a.png", "mineru/layout.json",
        ]
        assert entries[2]["role"] == "referenced-image"
    finally:
        directory.cleanup()


def test_v2_manifest_rejects_hash_drift_and_nested_symlink():
    directory, root, extract = workspace()
    try:
        (extract / "paper.pdf").write_bytes(b"pdf")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest_v2(extract, [("paper.pdf", "source")])
        (extract / "paper.pdf").write_bytes(b"changed")
        try:
            finalize(root, extract)
        except ValueError as exc:
            assert "changed" in str(exc)
        else:
            raise AssertionError("manifest hash drift must fail")

        (extract / "paper.pdf").write_bytes(b"pdf")
        external = root / "external"
        external.mkdir()
        (external / "a.png").write_bytes(b"image")
        (extract / "images").symlink_to(external, target_is_directory=True)
        manifest_v2(extract, [("paper.pdf", "source")])
        data = json.loads((extract / "manifest.json").read_text(encoding="utf-8"))
        payload = (external / "a.png").read_bytes()
        data["raw_artifacts"].append({
            "path": "images/a.png", "role": "referenced-image",
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        })
        (extract / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        try:
            finalize(root, extract)
        except ValueError as exc:
            assert "symlink" in str(exc)
        else:
            raise AssertionError("nested symlink must fail")
    finally:
        directory.cleanup()


def test_existing_raw_container_rolls_back_partial_nested_landing():
    directory, root, extract = workspace()
    try:
        (extract / "nested").mkdir()
        (extract / "nested/a.txt").write_text("a", encoding="utf-8")
        (extract / "nested/b.txt").write_text("b", encoding="utf-8")
        (extract / "wiki.md").write_text("wiki", encoding="utf-8")
        manifest_v2(extract, [
            ("nested/a.txt", "source"),
            ("nested/b.txt", "source"),
        ])
        container = root / "academic/raw/conferences/2026"
        container.mkdir(parents=True)
        (container / "existing.txt").write_text("keep", encoding="utf-8")
        original_replace = module.os.replace

        def fail_second_nested(source, destination):
            if Path(source).name == "b.txt":
                raise OSError("simulated second nested landing failure")
            return original_replace(source, destination)

        module.os.replace = fail_second_nested
        try:
            module.finalize(
                root, "demo", container,
                root / "academic/wiki/conferences/demo.md",
                extract, extract / "manifest.json", False,
                allow_existing_raw_dir=True,
            )
        except OSError as exc:
            assert "second nested" in str(exc)
        else:
            raise AssertionError("partial nested landing must fail")
        finally:
            module.os.replace = original_replace
        assert (container / "existing.txt").read_text(encoding="utf-8") == "keep"
        assert not (container / "nested/a.txt").exists()
        assert not (container / "nested").exists()
        assert not (root / "academic/wiki/conferences/demo.md").exists()
    finally:
        directory.cleanup()


if __name__ == "__main__":
    test_manifest_only_copies_declared_entity_files_and_writes_receipt()
    test_rejects_existing_destination_without_partial_write()
    test_allows_existing_raw_container_only_with_explicit_flag()
    test_existing_raw_container_rejects_manifest_file_collision()
    test_rejects_derived_symlink_and_cleanup_only_follows_commit()
    test_cleanup_removes_only_committed_item_directory()
    test_cleanup_removes_hidden_extractor_directories()
    test_failed_ingest_check_retains_committed_files_and_extract_directory()
    test_rejects_paths_outside_inbox_raw_and_wiki_boundaries()
    test_v2_manifest_preserves_nested_artifacts_and_hashes()
    test_v2_manifest_rejects_hash_drift_and_nested_symlink()
    test_existing_raw_container_rolls_back_partial_nested_landing()
    print("inbox finalize regression: PASS")

import importlib.util
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("relativize_internal_symlinks.py")
spec = importlib.util.spec_from_file_location("relativize_internal_symlinks", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_only_project_internal_absolute_links_are_selected():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "renamable-project"
        root.mkdir()
        target = root / "assets" / "source.pdf"
        target.parent.mkdir()
        target.write_bytes(b"pdf")
        internal_link = root / "nested" / "paper.pdf"
        internal_link.parent.mkdir()
        internal_link.symlink_to(target)
        outside_link = root / "external.pdf"
        outside_link.symlink_to("/tmp/external.pdf")

        changes = module.internal_absolute_links(root)

        assert len(changes) == 1
        link_path, target_path, relative_target = changes[0]
        assert link_path == internal_link
        assert target_path == target
        assert (internal_link.parent / relative_target).resolve() == target.resolve()
        assert os.readlink(outside_link) == "/tmp/external.pdf"


def test_relative_link_survives_copy_and_directory_rename():
    with tempfile.TemporaryDirectory() as directory:
        original = Path(directory) / "WikiRan"
        source = original / "source" / "file.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"pdf")
        link = original / "nested" / "paper.pdf"
        link.parent.mkdir()
        link.symlink_to("../source/file.pdf")

        renamed = Path(directory) / "renamed-knowledge-base"
        import shutil
        shutil.copytree(original, renamed, symlinks=True)

        assert (renamed / "nested" / "paper.pdf").read_bytes() == b"pdf"


def test_broken_raw_link_materialization_and_idempotence():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        folder = root / "academic/raw/papers/example"
        folder.mkdir(parents=True)
        source, link = folder / "original.pdf", folder / "paper.pdf"
        source.write_bytes(b"verified original")
        link.symlink_to("/old/project/missing.pdf")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        plan = module.materialize_broken_link(root, link, source, digest)
        assert plan["status"] == "planned" and link.is_symlink()
        result = module.materialize_broken_link(root, link, source, digest, apply=True)
        assert result["status"] == "completed" and not link.is_symlink()
        assert link.read_bytes() == source.read_bytes() == b"verified original"
        receipt = json.loads((root / result["receipt"]).read_text())
        assert receipt["old_target"] == "/old/project/missing.pdf"
        assert module.materialize_broken_link(root, link, source, digest, apply=True)["status"] == "already_materialized"


def test_materialization_rejects_hash_mismatch_and_live_links():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        folder = root / "academic/raw/papers/example"
        folder.mkdir(parents=True)
        source, link = folder / "original.pdf", folder / "paper.pdf"
        source.write_bytes(b"original")
        link.symlink_to(source.name)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        for expected in ["0" * 64, digest]:
            try:
                module.materialize_broken_link(root, link, source, expected, apply=True)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid repair accepted")
        assert link.is_symlink() and source.read_bytes() == b"original"
        link.unlink()
        link.symlink_to("/old/missing.pdf")
        outside = root / "outside.pdf"
        outside.write_bytes(b"original")
        for candidate in [outside, link]:
            try:
                module.materialize_broken_link(root, link, candidate, digest, apply=True)
            except ValueError:
                pass
            else:
                raise AssertionError("unconfined or symlink source accepted")


def test_materialization_restores_link_on_receipt_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        folder = root / "admin/raw/references/example"
        folder.mkdir(parents=True)
        source, link = folder / "original.pdf", folder / "paper.pdf"
        source.write_bytes(b"original")
        link.symlink_to("/old/missing.pdf")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        real_replace = os.replace
        failed = False
        def fail_completed_receipt(src, dst):
            nonlocal failed
            if Path(dst).suffix == ".json" and not link.is_symlink() and not failed:
                failed = True
                raise OSError("injected receipt failure")
            return real_replace(src, dst)
        with patch.object(module.os, "replace", side_effect=fail_completed_receipt):
            try:
                module.materialize_broken_link(root, link, source, digest, apply=True)
            except OSError:
                pass
            else:
                raise AssertionError("injected failure not propagated")
        assert link.is_symlink() and os.readlink(link) == "/old/missing.pdf"
        assert source.read_bytes() == b"original"
        receipt = next((root / "temp/raw-link-repair").glob("*.json"))
        assert json.loads(receipt.read_text())["status"] == "rolled_back"


if __name__ == "__main__":
    test_only_project_internal_absolute_links_are_selected()
    test_relative_link_survives_copy_and_directory_rename()
    test_broken_raw_link_materialization_and_idempotence()
    test_materialization_rejects_hash_mismatch_and_live_links()
    test_materialization_restores_link_on_receipt_failure()
    print("relativize_internal_symlinks regression: PASS")

import importlib.util
import os
import tempfile
from pathlib import Path

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


if __name__ == "__main__":
    test_only_project_internal_absolute_links_are_selected()
    test_relative_link_survives_copy_and_directory_rename()
    print("relativize_internal_symlinks regression: PASS")

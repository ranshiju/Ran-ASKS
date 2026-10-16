#!/usr/bin/env python3
"""Regression checks for Ubuntu/macOS host adapters."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import document_text
import platform_compat
import trash_util


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_soffice_resolution_and_environment(tmp: Path) -> None:
    explicit = _executable(tmp / "soffice")
    resolved = platform_compat.find_soffice(
        explicit,
        environ={},
        which=lambda _name: None,
        home=tmp / "home",
    )
    assert resolved == explicit.resolve()

    snap_launcher = _executable(tmp / "snap")
    snap_link = tmp / "libreoffice"
    snap_link.symlink_to(snap_launcher)
    resolved_snap = platform_compat.find_soffice(
        environ={},
        which=lambda name: str(snap_link) if name == "libreoffice" else None,
        home=tmp / "home",
    )
    assert resolved_snap == snap_link.absolute()
    assert resolved_snap != snap_link.resolve()

    bundled = tmp / "runtime/dependencies/bin/override/soffice"
    _executable(bundled)
    config = (
        tmp
        / "runtime/dependencies/native/libreoffice-headless/libreoffice"
        / "LibreOfficeDev.app/Contents/Resources/fontconfig/fonts.conf"
    )
    config.parent.mkdir(parents=True)
    config.write_text("<fontconfig/>", encoding="utf-8")
    env = platform_compat.soffice_environment(bundled, environ={})
    assert env["FONTCONFIG_FILE"] == str(config)
    preserved = platform_compat.soffice_environment(
        bundled, environ={"FONTCONFIG_PATH": "/custom/fonts"}
    )
    assert preserved == {"FONTCONFIG_PATH": "/custom/fonts"}

    app_binary = _executable(
        tmp / "LibreOffice.app/Contents/MacOS/soffice"
    )
    app_config = (
        tmp / "LibreOffice.app/Contents/Resources/fontconfig/fonts.conf"
    )
    app_config.parent.mkdir(parents=True)
    app_config.write_text("<fontconfig/>", encoding="utf-8")
    linked_binary = tmp / "usr/local/bin/soffice"
    linked_binary.parent.mkdir(parents=True)
    linked_binary.symlink_to(app_binary)
    linked_env = platform_compat.soffice_environment(linked_binary, environ={})
    assert linked_env["FONTCONFIG_FILE"] == str(app_config)


def test_linux_and_macos_open_commands(tmp: Path) -> None:
    tools = {
        "xdg-open": _executable(tmp / "xdg-open"),
        "gio": _executable(tmp / "gio"),
        "open": _executable(tmp / "open"),
    }
    resolver = lambda name: str(tools[name]) if name in tools else None
    assert platform_compat.default_open_command(
        "result.html", platform="linux", which=resolver
    ) == [str(tools["xdg-open"].resolve()), str(Path("result.html").absolute())]
    assert platform_compat.default_open_command(
        "result.html", platform="darwin", which=resolver
    ) == [str(tools["open"].resolve()), str(Path("result.html").absolute())]

    calls: list[list[str]] = []

    outcomes = iter((1, 0))

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=next(outcomes))

    assert platform_compat.open_with_default_app(
        "result.html", platform="linux", which=resolver, run=run
    )
    assert calls == [
        [str(tools["xdg-open"].resolve()), str(Path("result.html").absolute())],
        [str(tools["gio"].resolve()), "open", str(Path("result.html").absolute())],
    ]


def test_wsl_explorer_converts_linux_path(tmp: Path) -> None:
    tools = {
        "explorer.exe": _executable(tmp / "explorer.exe"),
        "wslpath": _executable(tmp / "wslpath"),
        "xdg-open": _executable(tmp / "xdg-open"),
    }
    resolver = lambda name: str(tools[name]) if name in tools else None
    source = tmp / "result.html"
    source.write_text("desktop opener", encoding="utf-8")
    converted_path = r"\\wsl.localhost\Ubuntu\tmp\result.html"
    opened: list[list[str]] = []

    def run(command, **_kwargs):
        if Path(command[0]).name == "wslpath":
            assert command[1:] == ["-w", str(source)]
            return SimpleNamespace(
                returncode=0, stdout=f"{converted_path}\n", stderr=""
            )
        opened.append(command)
        # xdg-open exit 4 is failure even when a WSL bridge is available.
        return SimpleNamespace(returncode=4 if Path(command[0]).name == "xdg-open" else 0, stdout="", stderr="")

    assert platform_compat.open_with_default_app(
        source, platform="linux", which=resolver, run=run
    )
    assert opened == [
        [str(tools["xdg-open"].resolve()), str(source)],
        [str(tools["explorer.exe"].resolve()), converted_path],
    ]

    opened.clear()
    resolver_without_xdg = (
        lambda name: str(tools[name])
        if name in {"explorer.exe", "wslpath"} else None
    )

    def explorer_run(command, **_kwargs):
        if Path(command[0]).name == "wslpath":
            return SimpleNamespace(
                returncode=0, stdout=f"{converted_path}\n", stderr=""
            )
        opened.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    assert platform_compat.open_with_default_app(
        source, platform="linux", which=resolver_without_xdg, run=explorer_run
    )
    assert opened == [[str(tools["explorer.exe"].resolve()), converted_path]]




def test_linux_trash_order_system_success_and_project_fallback(tmp: Path) -> None:
    tools = {
        "gio": _executable(tmp / "gio"),
        "trash-put": _executable(tmp / "trash-put"),
        "trash": _executable(tmp / "trash"),
    }
    resolver = lambda name: str(tools[name]) if name in tools else None
    commands = platform_compat.system_trash_commands(
        tmp / "source.txt", platform="linux", which=resolver
    )
    assert [Path(command[0]).name for command in commands] == [
        "gio", "trash-put", "trash",
    ]
    assert commands[0][1] == "trash"

    original_repo = trash_util.REPO
    original_commands = trash_util.platform_compat.system_trash_commands
    original_run = trash_util.subprocess.run
    try:
        trash_util.REPO = tmp / "repo"

        source = tmp / "system-trash.txt"
        source.write_text("recoverable", encoding="utf-8")
        trash_util.platform_compat.system_trash_commands = lambda path: [
            ["gio", "trash", str(path)]
        ]

        def successful_trash(_command, **_kwargs):
            source.unlink()
            return SimpleNamespace(returncode=0)

        trash_util.subprocess.run = successful_trash
        trash_util.trash_path(source)
        assert not source.exists()
        assert not (trash_util.REPO / "temp/trash").exists()

        trash_util.platform_compat.system_trash_commands = lambda _path: []
        trash_util.subprocess.run = original_run
        source = tmp / "fallback.txt"
        source.write_text("recoverable", encoding="utf-8")
        trash_util.trash_path(source)
        assert not source.exists()
        recovered = list((trash_util.REPO / "temp/trash").glob("*/fallback.txt"))
        assert len(recovered) == 1
        assert recovered[0].read_text(encoding="utf-8") == "recoverable"
    finally:
        trash_util.REPO = original_repo
        trash_util.platform_compat.system_trash_commands = original_commands
        trash_util.subprocess.run = original_run


def test_docx_structured_text_extraction(tmp: Path) -> None:
    source = tmp / "sample.docx"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{document_text.WORD_NS}" xmlns:r="{document_text.OFFICE_REL_NS}"><w:body>
  <w:p><w:r><w:t>Title</w:t><w:footnoteReference w:id="1"/><w:endnoteReference w:id="1"/></w:r></w:p>
  <w:tbl><w:tr>
    <w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc>
  </w:tr></w:tbl>
  <w:sdt><w:sdtContent><w:p><w:r><w:t>Nested</w:t></w:r></w:p></w:sdtContent></w:sdt>
  <w:p><w:r><w:t>Line</w:t><w:tab/><w:t>two</w:t><w:br/><w:t>tail</w:t></w:r></w:p>
  <w:sectPr><w:headerReference r:id="rHeader"/><w:footerReference r:id="rFooter"/></w:sectPr>
</w:body></w:document>"""
    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:hdr xmlns:w="{document_text.WORD_NS}">
  <w:p><w:r><w:t>Header</w:t></w:r></w:p>
</w:hdr>"""
    footer = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:ftr xmlns:w="{document_text.WORD_NS}">
  <w:p><w:r><w:t>Footer</w:t></w:r></w:p>
</w:ftr>"""
    footnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:footnotes xmlns:w="{document_text.WORD_NS}">
  <w:footnote w:id="1"><w:p><w:r><w:t>Footnote</w:t></w:r></w:p></w:footnote>
  <w:footnote w:id="2"><w:p><w:r><w:t>Orphan footnote</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""
    endnotes = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:endnotes xmlns:w="{document_text.WORD_NS}">
  <w:endnote w:id="1"><w:p><w:r><w:t>Endnote</w:t></w:r></w:p></w:endnote>
  <w:endnote w:id="2"><w:p><w:r><w:t>Orphan endnote</w:t></w:r></w:p></w:endnote>
</w:endnotes>"""
    relationships = f"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{document_text.PACKAGE_REL_NS}">
  <Relationship Id="rHeader" Type="{document_text.OFFICE_REL_NS}/header" Target="header1.xml"/>
  <Relationship Id="rFooter" Type="{document_text.OFFICE_REL_NS}/footer" Target="footer1.xml"/>
  <Relationship Id="rFootnotes" Type="{document_text.OFFICE_REL_NS}/footnotes" Target="footnotes.xml"/>
  <Relationship Id="rEndnotes" Type="{document_text.OFFICE_REL_NS}/endnotes" Target="endnotes.xml"/>
</Relationships>"""
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/header1.xml", header)
        archive.writestr("word/footer1.xml", footer)
        archive.writestr("word/header-orphan.xml", header.replace("Header", "Orphan header"))
        archive.writestr("word/footer-orphan.xml", footer.replace("Footer", "Orphan footer"))
        archive.writestr("word/footnotes.xml", footnotes)
        archive.writestr("word/endnotes.xml", endnotes)
    assert document_text.extract_word_text(source) == (
        "Title\nA1\tB1\nNested\nLine\ttwo\ntail\n\nHeader\n\nFooter\n\n"
        "Footnote\n\nEndnote\n"
    )
    assert document_text.extract_word_text(source, max_chars=5) == "Title"

    strict_source = tmp / "strict.docx"
    strict_parts = {
        "word/document.xml": xml,
        "word/_rels/document.xml.rels": relationships,
        "word/header1.xml": header,
        "word/footer1.xml": footer,
        "word/footnotes.xml": footnotes,
        "word/endnotes.xml": endnotes,
    }
    with zipfile.ZipFile(strict_source, "w") as archive:
        for name, content in strict_parts.items():
            content = content.replace(
                document_text.WORD_NS, document_text.STRICT_WORD_NS
            ).replace(
                document_text.OFFICE_REL_NS,
                document_text.STRICT_OFFICE_REL_NS,
            )
            archive.writestr(name, content)
    assert document_text.extract_word_text(strict_source) == (
        "Title\nA1\tB1\nNested\nLine\ttwo\ntail\n\nHeader\n\nFooter\n\n"
        "Footnote\n\nEndnote\n"
    )


def test_legacy_doc_uses_soffice_without_persisting_conversion(tmp: Path) -> None:
    source = tmp / "legacy.doc"
    source.write_bytes(b"legacy")
    soffice = _executable(tmp / "soffice")
    original_executable = document_text.platform_compat.find_executable
    original_soffice = document_text.platform_compat.find_soffice
    original_environment = document_text.platform_compat.soffice_environment
    original_run = document_text.subprocess.run
    output_dirs: list[Path] = []

    def run(command, **kwargs):
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        output_dir = Path(command[command.index("--outdir") + 1])
        output_dirs.append(output_dir)
        assert command[command.index("--convert-to") + 1] == "docx:Office Open XML Text"
        with zipfile.ZipFile(output_dir / "legacy.docx", "w") as archive:
            archive.writestr("word/document.xml", f'<w:document xmlns:w="{document_text.WORD_NS}"><w:body><w:p><w:r><w:t>portable text</w:t></w:r></w:p></w:body></w:document>')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    try:
        document_text.platform_compat.find_executable = lambda *_names: None
        document_text.platform_compat.find_soffice = lambda: soffice
        document_text.platform_compat.soffice_environment = lambda _path: {
            "LANG": "C.UTF-8"
        }
        document_text.subprocess.run = run
        assert document_text.extract_word_text(source) == "portable text\n"
    finally:
        document_text.platform_compat.find_executable = original_executable
        document_text.platform_compat.find_soffice = original_soffice
        document_text.platform_compat.soffice_environment = original_environment
        document_text.subprocess.run = original_run
    assert output_dirs and all(not path.exists() for path in output_dirs)



def test_soffice_precedence_app_bundle_and_child_environment(tmp: Path) -> None:
    explicit = _executable(tmp / "precedence/explicit")
    configured = _executable(tmp / "precedence/configured")
    path_binary = _executable(tmp / "precedence/path")
    resolver = lambda name: str(path_binary) if name == "soffice" else None
    options = {"environ": {"SOFFICE_BIN": str(configured)}, "which": resolver, "home": tmp}
    assert platform_compat.find_soffice(explicit, **options) == explicit.absolute()
    assert platform_compat.find_soffice(**options) == configured.absolute()
    app = tmp / "Portable LibreOffice.app"
    binary = _executable(app / "Contents/MacOS/soffice")
    assert platform_compat.find_soffice(app, environ={}, which=lambda _: None, home=tmp) == binary.absolute()
    caller = {"FONTCONFIG_FILE": "/custom/fonts.conf", "UNCHANGED": "yes"}
    child = platform_compat.soffice_environment(binary, environ=caller)
    child["UNCHANGED"] = "child"
    assert caller == {"FONTCONFIG_FILE": "/custom/fonts.conf", "UNCHANGED": "yes"}
    assert child["FONTCONFIG_FILE"] == caller["FONTCONFIG_FILE"]


def test_headless_open_failure_and_absolute_operands(tmp: Path) -> None:
    assert not platform_compat.open_with_default_app("-e", platform="linux", which=lambda _: None)
    tools = {name: _executable(tmp / "operand-tools" / name) for name in ("xdg-open", "gio", "trash-put", "open")}
    resolver = lambda name: str(tools[name]) if name in tools else None
    expected = str(Path("-e").absolute())
    commands = platform_compat.default_open_commands("-e", platform="linux", which=resolver)
    assert commands and all(command[-1] == expected for command in commands)
    assert all(command[-1] == expected for command in platform_compat.system_trash_commands("-e", platform="linux", which=resolver))
    attempts = []
    def unavailable(command, **kwargs):
        attempts.append(command)
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=3)
    assert not platform_compat.open_with_default_app("-e", platform="linux", which=resolver, run=unavailable)
    assert attempts == commands
    def broken(*_args, **_kwargs):
        raise OSError("desktop unavailable")
    assert not platform_compat.open_with_default_app("-e", platform="darwin", which=resolver, run=broken)


def test_same_name_same_time_files_and_directories_remain_recoverable(tmp: Path) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls):
            return datetime(2026, 10, 16, 22, 0, 0)
    repo = tmp / "collision-repo"
    with patch.object(trash_util, "REPO", repo), patch.object(trash_util.platform_compat, "system_trash_commands", return_value=[]), patch.object(trash_util, "datetime", FixedDateTime):
        for group, content in (("one", "first"), ("two", "second")):
            source = tmp / group / "same.txt"
            source.parent.mkdir(parents=True)
            source.write_text(content, encoding="utf-8")
            trash_util.trash_path(source)
            assert not source.exists()
        recovered = list((repo / "temp/trash").glob("*/same.txt"))
        assert len(recovered) == 2
        assert {p.read_text() for p in recovered} == {"first", "second"}
        for group, content in (("three", "third"), ("four", "fourth")):
            source = tmp / group / "same-dir"
            source.mkdir(parents=True)
            (source / "marker").write_text(content)
            trash_util.trash_path(source)
            assert not source.exists()
        recovered_dirs = list((repo / "temp/trash").glob("*/same-dir"))
        assert len(recovered_dirs) == 2
        assert {(p / "marker").read_text() for p in recovered_dirs} == {"third", "fourth"}
        assert all(not (p / "same-dir").exists() for p in recovered_dirs)


def test_trash_moves_symlinks_not_targets_and_keeps_source_on_failure(tmp: Path) -> None:
    repo = tmp / "symlink-repo"
    target = tmp / "original.txt"
    target.write_text("keep target")
    link = tmp / "-link"
    link.symlink_to(target)
    missing = tmp / "missing-target"
    dangling = tmp / "dangling"
    dangling.symlink_to(missing)
    gio = _executable(tmp / "symlink-tools/gio")
    commands = platform_compat.system_trash_commands(link, platform="linux", which=lambda name: str(gio) if name == "gio" else None)
    assert commands == [[str(gio.absolute()), "trash", str(link.absolute())]]
    assert commands[0][-1] != str(link.resolve())
    with patch.object(trash_util, "REPO", repo), patch.object(trash_util.platform_compat, "system_trash_commands", return_value=[]):
        trash_util.trash_path(link)
        trash_util.trash_path(dangling)
        saved_link = list((repo / "temp/trash").glob("*/-link"))
        saved_dangling = list((repo / "temp/trash").glob("*/dangling"))
        assert len(saved_link) == len(saved_dangling) == 1
        assert saved_link[0].is_symlink() and os.readlink(saved_link[0]) == str(target)
        assert saved_dangling[0].is_symlink() and os.readlink(saved_dangling[0]) == str(missing)
        assert target.read_text() == "keep target" and not missing.exists()
        assert not link.is_symlink() and not dangling.is_symlink()
        failed = tmp / "move-failed.txt"
        failed.write_text("source survives")
        with patch.object(trash_util.shutil, "move", side_effect=OSError("unavailable destination")):
            try:
                trash_util.trash_path(failed)
            except OSError:
                pass
            else:
                raise AssertionError("move failure must propagate")
        assert failed.read_text() == "source survives"



def test_docx_native_failures_preserve_textutil_fallback(tmp: Path) -> None:
    textutil = _executable(tmp / "docx-fallback-tools/textutil")
    badzip = tmp / "broken.docx"
    badzip.write_bytes(b"not a ZIP package")
    unsupported = tmp / "unsupported.docx"
    malformed = tmp / "malformed.docx"
    empty = tmp / "empty.docx"
    for path, xml in ((unsupported, '<w:document xmlns:w="urn:unsupported"><w:body><w:p><w:r><w:t>not native</w:t></w:r></w:p></w:body></w:document>'), (malformed, '<unclosed'), (empty, f'<w:document xmlns:w="{document_text.WORD_NS}"><w:body/></w:document>')):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)
    originals = {path: path.read_bytes() for path in (badzip, unsupported, malformed, empty)}
    calls = []
    def run(command, **_kwargs):
        calls.append(command)
        assert command[:4] == [str(textutil), "-convert", "txt", "-stdout"]
        assert Path(command[-1]).is_absolute()
        return SimpleNamespace(returncode=0, stdout="host converter text\n", stderr="")
    resolver = lambda name: textutil if name == "textutil" else None
    with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.subprocess, "run", side_effect=run):
        for path in originals:
            assert document_text.extract_docx_text(path) == ""
            assert document_text.extract_word_text(path) == "host converter text\n"
        assert document_text.extract_word_text(badzip, max_chars=4) == "host"
    assert len(calls) == 5
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_docx_textutil_empty_exception_and_failure_return_empty(tmp: Path) -> None:
    source = tmp / "unreadable.docx"
    source.write_bytes(b"invalid package")
    textutil = _executable(tmp / "empty-textutil/textutil")
    resolver = lambda name: textutil if name == "textutil" else None
    outcomes = [SimpleNamespace(returncode=0, stdout=" \n\t", stderr=""), OSError("textutil unavailable"), SimpleNamespace(returncode=1, stdout="partial text", stderr="conversion failed")]
    with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.platform_compat, "find_soffice", return_value=None):
        for outcome in outcomes:
            with patch.object(document_text.subprocess, "run", side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome):
                assert document_text.extract_word_text(source) == ""
    assert source.read_bytes() == b"invalid package"


def test_legacy_doc_keeps_textutil_first_and_continues_after_empty(tmp: Path) -> None:
    source = tmp / "converter-chain.doc"
    source.write_bytes(b"synthetic legacy document")
    tools = {name: _executable(tmp / "converter-chain-tools" / name) for name in ("textutil", "antiword")}
    resolver = lambda name: tools.get(name)
    with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.platform_compat, "find_soffice", side_effect=AssertionError("textutil success must win")), patch.object(document_text.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="textutil first\n", stderr="")):
        assert document_text.extract_word_text(source) == "textutil first\n"
    calls = []
    def run(command, **_kwargs):
        calls.append(Path(command[0]).name)
        return SimpleNamespace(returncode=0, stdout=" \n" if calls[-1] == "textutil" else "antiword fallback\n", stderr="")
    with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.platform_compat, "find_soffice", return_value=None), patch.object(document_text.subprocess, "run", side_effect=run):
        assert document_text.extract_word_text(source) == "antiword fallback\n"
    assert calls == ["textutil", "antiword"]



def test_headless_opener_timeouts_are_bounded_and_readonly(tmp: Path) -> None:
    artifact = tmp / "headless-artifact.html"
    original = b"<!doctype html><title>generated artifact</title>"
    artifact.write_bytes(original)
    tools = {name: _executable(tmp / "timeout-tools" / name) for name in ("xdg-open", "gio")}
    resolver = lambda name: str(tools[name]) if name in tools else None
    attempts = []
    def timeout_run(command, **kwargs):
        attempts.append(command)
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    assert not platform_compat.open_with_default_app(artifact, platform="linux", which=resolver, run=timeout_run)
    assert [Path(command[0]).name for command in attempts] == ["xdg-open", "gio"]
    assert artifact.read_bytes() == original
    attempts.clear()
    def recover_after_timeout(command, **kwargs):
        attempts.append(command)
        assert kwargs["timeout"] == 30
        if Path(command[0]).name == "xdg-open":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(returncode=0)
    assert platform_compat.open_with_default_app(artifact, platform="linux", which=resolver, run=recover_after_timeout)
    assert [Path(command[0]).name for command in attempts] == ["xdg-open", "gio"]
    assert artifact.read_bytes() == original



def test_wsl_xdg_failure_continues_fallback_without_false_success(tmp: Path) -> None:
    tools = {name: _executable(tmp / "wsl-failure-tools" / name) for name in ("xdg-open", "gio", "wslview", "explorer.exe", "wslpath")}
    resolver = lambda name: str(tools[name]) if name in tools else None
    artifact = tmp / "wsl-failure-artifact.html"
    original = b"<title>preserved artifact</title>"
    artifact.write_bytes(original)
    opened = []
    def failing(command, **_kwargs):
        name = Path(command[0]).name
        if name == "wslpath":
            return SimpleNamespace(returncode=0, stdout="C:/artifact.html\n", stderr="")
        opened.append(name)
        return SimpleNamespace(returncode=4, stdout="", stderr="")
    assert not platform_compat.open_with_default_app(artifact, platform="linux", which=resolver, run=failing)
    assert opened == ["xdg-open", "gio", "wslview", "explorer.exe"]
    assert artifact.read_bytes() == original
    opened.clear()
    def later_success(command, **_kwargs):
        name = Path(command[0]).name
        if name == "wslpath":
            return SimpleNamespace(returncode=0, stdout="C:/artifact.html\n", stderr="")
        opened.append(name)
        return SimpleNamespace(returncode=0 if name == "gio" else 4, stdout="", stderr="")
    assert platform_compat.open_with_default_app(artifact, platform="linux", which=resolver, run=later_success)
    assert opened == ["xdg-open", "gio"]
    assert artifact.read_bytes() == original



def test_docx_alternate_content_uses_fallback_once_in_all_visible_containers(tmp: Path) -> None:
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    def paragraph(value):
        return f"<w:p><w:r><w:t>{value}</w:t></w:r></w:p>"
    def alternate(choice, fallback):
        return f'<mc:AlternateContent><mc:Choice Requires="w14">{choice}</mc:Choice><mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent>'
    run = '<w:p><w:r>' + alternate('<w:t>RUN_CHOICE</w:t>', '<w:t>RUN_FALLBACK</w:t>') + '</w:r></w:p>'
    body = alternate(paragraph("BODY_CHOICE"), paragraph("BODY_FALLBACK"))
    wrapper = '<w:sdt><w:sdtContent>' + alternate(paragraph("WRAPPER_CHOICE"), paragraph("WRAPPER_FALLBACK")) + '</w:sdtContent></w:sdt>'
    cell = alternate('<w:tc>' + paragraph("TABLE_CELL_CHOICE") + '</w:tc>', '<w:tc>' + paragraph("TABLE_FALLBACK") + '</w:tc>')
    table = '<w:tbl>' + alternate('<w:tr><w:tc>' + paragraph("TABLE_ROW_CHOICE") + '</w:tc></w:tr>', '<w:tr>' + cell + '</w:tr>') + '</w:tbl>'
    document = f'<w:document xmlns:w="{document_text.WORD_NS}" xmlns:r="{document_text.OFFICE_REL_NS}" xmlns:mc="{mc}" xmlns:w14="urn:unsupported-feature"><w:body>{run}{body}{wrapper}{table}<w:sectPr><w:headerReference r:id="rHeader"/></w:sectPr></w:body></w:document>'
    header = f'<w:hdr xmlns:w="{document_text.WORD_NS}" xmlns:mc="{mc}" xmlns:w14="urn:unsupported-feature">' + alternate(paragraph("HEADER_CHOICE"), paragraph("HEADER_FALLBACK")) + '</w:hdr>'
    relationships = f'<Relationships xmlns="{document_text.PACKAGE_REL_NS}"><Relationship Id="rHeader" Type="{document_text.OFFICE_REL_NS}/header" Target="header1.xml"/></Relationships>'
    expected = "RUN_FALLBACK\nBODY_FALLBACK\nWRAPPER_FALLBACK\nTABLE_FALLBACK\n\nHEADER_FALLBACK\n"
    for strict in (False, True):
        source = tmp / ("alternate-strict.docx" if strict else "alternate.docx")
        parts = {"word/document.xml": document, "word/header1.xml": header, "word/_rels/document.xml.rels": relationships}
        with zipfile.ZipFile(source, "w") as archive:
            for name, value in parts.items():
                if strict:
                    value = value.replace(document_text.WORD_NS, document_text.STRICT_WORD_NS).replace(document_text.OFFICE_REL_NS, document_text.STRICT_OFFICE_REL_NS)
                archive.writestr(name, value)
        original = source.read_bytes()
        with patch.object(document_text.platform_compat, "find_executable", side_effect=AssertionError("native fallback must not call a host converter")), patch.object(document_text.platform_compat, "find_soffice", side_effect=AssertionError("native fallback must not call LibreOffice")):
            extracted = document_text.extract_word_text(source)
        assert extracted == expected
        assert "CHOICE" not in extracted
        assert all(extracted.count(token) == 1 for token in ("RUN_FALLBACK", "BODY_FALLBACK", "WRAPPER_FALLBACK", "TABLE_FALLBACK", "HEADER_FALLBACK"))
        assert source.read_bytes() == original


def test_docx_alternate_content_without_fallback_uses_controlled_host_recovery(tmp: Path) -> None:
    source = tmp / "alternate-no-fallback.docx"
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    xml = f'<w:document xmlns:w="{document_text.WORD_NS}" xmlns:mc="{mc}" xmlns:w14="urn:unsupported-feature"><w:body><w:p><w:r><w:t>before</w:t><mc:AlternateContent><mc:Choice Requires="w14"><w:t>UNSUPPORTED_CHOICE</w:t></mc:Choice></mc:AlternateContent><w:t>after</w:t></w:r></w:p></w:body></w:document>'
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", xml)
    original = source.read_bytes()
    assert document_text.extract_docx_text(source) == ""
    textutil = _executable(tmp / "alternate-recovery-tools/textutil")
    resolver = lambda name: textutil if name == "textutil" else None
    with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.platform_compat, "find_soffice", side_effect=AssertionError("successful textutil must win")), patch.object(document_text.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="host recovered representation\n", stderr="")) as converter:
        assert document_text.extract_word_text(source) == "host recovered representation\n"
    converter.assert_called_once()
    assert converter.call_args.args[0] == [str(textutil), "-convert", "txt", "-stdout", str(source.absolute())]
    assert source.read_bytes() == original


def test_docx_soffice_recovery_preserves_headers_footers_and_does_not_recurse(tmp: Path) -> None:
    source = tmp / "lo-recovery.docx"
    source.write_bytes(b"unsupported source package")
    original = source.read_bytes()
    soffice = _executable(tmp / "docx-lo-tools/soffice")
    output_dirs = []
    document = f'<w:document xmlns:w="{document_text.WORD_NS}" xmlns:r="{document_text.OFFICE_REL_NS}"><w:body><w:p><w:r><w:t>Recovered body</w:t></w:r></w:p><w:sectPr><w:headerReference r:id="rHeader"/><w:footerReference r:id="rFooter"/></w:sectPr></w:body></w:document>'
    header = f'<w:hdr xmlns:w="{document_text.WORD_NS}"><w:p><w:r><w:t>Recovered header</w:t></w:r></w:p></w:hdr>'
    footer = f'<w:ftr xmlns:w="{document_text.WORD_NS}"><w:p><w:r><w:t>Recovered footer</w:t></w:r></w:p></w:ftr>'
    relationships = f'<Relationships xmlns="{document_text.PACKAGE_REL_NS}"><Relationship Id="rHeader" Type="{document_text.OFFICE_REL_NS}/header" Target="header1.xml"/><Relationship Id="rFooter" Type="{document_text.OFFICE_REL_NS}/footer" Target="footer1.xml"/></Relationships>'
    valid_output = True
    def convert(command, **kwargs):
        assert command[command.index("--convert-to") + 1] == "docx:Office Open XML Text"
        assert kwargs["env"] == {"LANG": "C.UTF-8"}
        output_dir = Path(command[command.index("--outdir") + 1])
        output_dirs.append(output_dir)
        with zipfile.ZipFile(output_dir / "lo-recovery.docx", "w") as archive:
            archive.writestr("word/document.xml", document if valid_output else '<unsupported/>')
            archive.writestr("word/header1.xml", header)
            archive.writestr("word/footer1.xml", footer)
            archive.writestr("word/_rels/document.xml.rels", relationships)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    native_reader = document_text.extract_docx_text
    with patch.object(document_text.platform_compat, "find_executable", return_value=None), patch.object(document_text.platform_compat, "find_soffice", return_value=soffice), patch.object(document_text.platform_compat, "soffice_environment", return_value={"LANG": "C.UTF-8"}), patch.object(document_text.subprocess, "run", side_effect=convert) as converter, patch.object(document_text, "extract_docx_text", wraps=native_reader) as native:
        assert document_text.extract_word_text(source) == "Recovered body\n\nRecovered header\n\nRecovered footer\n"
        assert converter.call_count == 1 and native.call_count == 2
        valid_output = False
        assert document_text.extract_word_text(source) == ""
        assert converter.call_count == 2 and native.call_count == 4
    assert output_dirs and all(not path.exists() for path in output_dirs)
    assert source.read_bytes() == original



def _write_notes_alternate_content_fixture(source: Path, *, unsupported_kind: str | None = None, strict: bool = False) -> None:
    mc = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    declarations = f'xmlns:w="{document_text.WORD_NS}" xmlns:mc="{mc}" xmlns:w14="urn:unsupported-feature"'
    document = f'<w:document {declarations}><w:body><w:p><w:r><w:t>Body</w:t><mc:AlternateContent><mc:Choice Requires="w14"><w:footnoteReference w:id="2"/><w:endnoteReference w:id="2"/></mc:Choice><mc:Fallback><w:footnoteReference w:id="1"/><w:endnoteReference w:id="1"/></mc:Fallback></mc:AlternateContent></w:r></w:p></w:body></w:document>'
    relationships = f'<Relationships xmlns="{document_text.PACKAGE_REL_NS}"><Relationship Id="rFoot" Type="{document_text.OFFICE_REL_NS}/footnotes" Target="footnotes.xml"/><Relationship Id="rEnd" Type="{document_text.OFFICE_REL_NS}/endnotes" Target="endnotes.xml"/></Relationships>'
    parts = {"word/document.xml": document, "word/_rels/document.xml.rels": relationships}
    for kind in ("footnotes", "endnotes"):
        note = kind[:-1]
        label = note.upper()
        wrong_tag = "endnote" if note == "footnote" else "footnote"
        def item(tag, note_id, value):
            return f'<w:{tag} w:id="{note_id}"><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:{tag}>'
        choice = item(note, "1", label + "_CHOICE")
        fallback = item(note, "1", label + "_FALLBACK") + item(note, "2", label + "_UNREFERENCED") + item(wrong_tag, "1", label + "_WRONG_TYPE")
        branch = f'<mc:Choice Requires="w14">{choice}</mc:Choice>'
        if unsupported_kind != kind:
            branch += f'<mc:Fallback>{fallback}</mc:Fallback>'
        parts[f"word/{kind}.xml"] = f'<w:{kind} {declarations}><mc:AlternateContent>{branch}</mc:AlternateContent>{item(note, "3", label + "_UNREFERENCED_OUTSIDE")}</w:{kind}>'
    with zipfile.ZipFile(source, "w") as archive:
        for name, value in parts.items():
            if strict:
                value = value.replace(document_text.WORD_NS, document_text.STRICT_WORD_NS).replace(document_text.OFFICE_REL_NS, document_text.STRICT_OFFICE_REL_NS)
            archive.writestr(name, value)


def test_docx_notes_roots_select_fallback_once_and_filter_visible_reference_ids(tmp: Path) -> None:
    expected = "Body\n\nFOOTNOTE_FALLBACK\n\nENDNOTE_FALLBACK\n"
    for strict in (False, True):
        source = tmp / ("notes-fallback-strict.docx" if strict else "notes-fallback.docx")
        _write_notes_alternate_content_fixture(source, strict=strict)
        original = source.read_bytes()
        with patch.object(document_text.platform_compat, "find_executable", side_effect=AssertionError("supported notes must stay native")), patch.object(document_text.platform_compat, "find_soffice", side_effect=AssertionError("supported notes must stay native")):
            extracted = document_text.extract_word_text(source)
        assert extracted == expected
        assert extracted.count("FOOTNOTE_FALLBACK") == extracted.count("ENDNOTE_FALLBACK") == 1
        assert all(hidden not in extracted for hidden in ("CHOICE", "UNREFERENCED", "WRONG_TYPE"))
        assert source.read_bytes() == original


def test_docx_notes_roots_without_fallback_fail_closed_and_use_controlled_recovery(tmp: Path) -> None:
    textutil = _executable(tmp / "notes-recovery-tools/textutil")
    resolver = lambda name: textutil if name == "textutil" else None
    for kind in ("footnotes", "endnotes"):
        for strict in (False, True):
            source = tmp / f"{kind}-without-fallback-{strict}.docx"
            _write_notes_alternate_content_fixture(source, unsupported_kind=kind, strict=strict)
            original = source.read_bytes()
            assert document_text.extract_docx_text(source) == ""
            with patch.object(document_text.platform_compat, "find_executable", return_value=None), patch.object(document_text.platform_compat, "find_soffice", return_value=None), patch.object(document_text.subprocess, "run", side_effect=AssertionError("no configured converter may run")):
                assert document_text.extract_word_text(source) == ""
            with patch.object(document_text.platform_compat, "find_executable", side_effect=resolver), patch.object(document_text.platform_compat, "find_soffice", side_effect=AssertionError("successful textutil must win")), patch.object(document_text.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="controlled notes recovery\n", stderr="")) as converter:
                assert document_text.extract_word_text(source) == "controlled notes recovery\n"
            converter.assert_called_once()
            assert converter.call_args.args[0] == [str(textutil), "-convert", "txt", "-stdout", str(source.absolute())]
            assert source.read_bytes() == original


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="platform-compat-test-") as temp_dir:
        tmp = Path(temp_dir)
        test_soffice_resolution_and_environment(tmp)
        test_linux_and_macos_open_commands(tmp)
        test_wsl_explorer_converts_linux_path(tmp)
        test_wsl_xdg_failure_continues_fallback_without_false_success(tmp)
        test_linux_trash_order_system_success_and_project_fallback(tmp)
        test_docx_structured_text_extraction(tmp)
        test_legacy_doc_uses_soffice_without_persisting_conversion(tmp)
        test_soffice_precedence_app_bundle_and_child_environment(tmp)
        test_headless_open_failure_and_absolute_operands(tmp)
        test_headless_opener_timeouts_are_bounded_and_readonly(tmp)
        test_same_name_same_time_files_and_directories_remain_recoverable(tmp)
        test_trash_moves_symlinks_not_targets_and_keeps_source_on_failure(tmp)
        test_docx_native_failures_preserve_textutil_fallback(tmp)
        test_docx_textutil_empty_exception_and_failure_return_empty(tmp)
        test_legacy_doc_keeps_textutil_first_and_continues_after_empty(tmp)
        test_docx_alternate_content_uses_fallback_once_in_all_visible_containers(tmp)
        test_docx_alternate_content_without_fallback_uses_controlled_host_recovery(tmp)
        test_docx_soffice_recovery_preserves_headers_footers_and_does_not_recurse(tmp)
        test_docx_notes_roots_select_fallback_once_and_filter_visible_reference_ids(tmp)
        test_docx_notes_roots_without_fallback_fail_closed_and_use_controlled_recovery(tmp)
    print("platform compatibility regression: PASS")


if __name__ == "__main__":
    main()

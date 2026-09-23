#!/usr/bin/env python3
"""Regression tests for the deterministic CV workspace function."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
spec = importlib.util.spec_from_file_location("cv_under_test", SCRIPTS / "cv.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def template_spec() -> dict:
    return {
        "schema": "cv-template-v1",
        "id": "academic-full",
        "renderer": "native-docx-v1",
        "modules": [
            {"id": "profile", "kind": "profile",
             "fields": ["name", "headline", "research_interests"]},
            {"id": "publications", "kind": "records",
             "label": {"zh": "论文", "en": "Publications"},
             "categories": ["publication"], "required_categories": [], "sort": "date_desc"},
            {"id": "experience", "kind": "records",
             "label": {"zh": "教育经历", "en": "Education"},
             "categories": ["education"], "required_categories": ["education"], "sort": "date_asc"},
        ],
    }


def create_template(workspace: Path) -> None:
    docx = module._load_docx()
    templates = workspace / "templates"
    templates.mkdir()
    source = workspace / "sensitive-source.docx"
    document = docx.Document()
    values = [
        "Sensitive Person " + "s" + "jran@example.test 1988 1232025 GJ090301",
        "Professor", "Testing", "Publications", "Publication record",
        "Education", "Education record",
    ]
    for value in values:
        document.add_paragraph(value)
    document.core_properties.author = "Sensitive Person"
    document.save(source)
    template = template_spec()
    (templates / "academic-full.template.yaml").write_text(
        yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8")
    paragraph_map = {
        "{{profile.name}}": 0,
        "{{profile.headline}}": 1,
        "{{profile.research_interests}}": 2,
        "{{module.publications.label}}": 3,
        "{{module.publications.records}}": 4,
        "{{module.experience.label}}": 5,
        "{{module.experience.records}}": 6,
    }
    module.extract_anonymized_template(
        source, templates / "academic-full.docx", template, paragraph_map)


def fixture() -> tuple[Path, Path, Path, dict]:
    root = Path(tempfile.mkdtemp(prefix="test-cv-", dir=REPO / "temp"))
    projects = root / "projects"
    workspace = projects / "demo"
    evidence = root / "academic" / "raw" / "evidence.md"
    workspace.mkdir(parents=True)
    create_template(workspace)
    evidence.parent.mkdir(parents=True)
    evidence.write_text("- Verified fact one. {: #fact-one}\n\n- Verified fact two. {: #fact-two}\n", encoding="utf-8")
    relative_evidence = evidence.relative_to(REPO)
    data = {
        "schema_version": 1,
        "authority": {"working_state_only": True, "factual_source": "raw"},
        "profile": {
            "person_id": "test-person",
            "name": {"zh": "测试用户", "en": "Test User"},
            "headline": {"zh": "教授", "en": "Professor"},
            "verification": "verified",
            "research_interests": {"zh": ["测试"], "en": ["Testing"]},
            "research_interests_verification": "verified",
        },
        "editions": {
            "full-zh": {"language": "zh-CN", "renderer": "native-docx-v1", "status": "active",
                        "template": "academic-full"},
            "full-en": {"language": "en", "renderer": "native-docx-v1", "status": "active",
                        "template": "academic-full"},
        },
        "records": [
            {
                "id": "record-one", "category": "education", "date": "2025-01",
                "title": {"zh": "测试教育经历", "en": "Test education"},
                "evidence": [f"{relative_evidence}#fact-one"],
                "verification": "verified", "include_in": ["full-zh", "full-en"],
            },
            {
                "id": "record-two", "category": "publication", "date": "2026",
                "title": {"zh": None, "en": "English source article"}, "source_language": "en",
                "evidence": [f"{relative_evidence}#fact-two"],
                "verification": "verified", "include_in": [],
            },
            {
                "id": "draft", "category": "funding", "date": "2024",
                "title": {"zh": "待核验项目", "en": None},
                "evidence": [f"{relative_evidence}#fact-two"],
                "verification": "needs_verification", "include_in": [],
            },
        ],
    }
    source = workspace / "cv-records.yaml"
    source.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return root, workspace, source, data


def test_structure_and_formal_gates(workspace: Path, data: dict) -> None:
    assert module.validate_structure(data) == []
    data["records"][2]["include_in"] = ["full-zh"]
    report = module.validate_edition(workspace, data, "full-zh")
    assert any(item["code"] == "not_verified" for item in report["errors"])
    data["records"][2]["include_in"] = []

    data["records"][2]["date"] = ""
    report = module.validate_edition(workspace, data, "full-zh")
    assert not any(item.get("record_id") == "draft" for item in report["errors"])
    data["records"][2]["date"] = "2024"


def test_source_language_fallback(workspace: Path, data: dict) -> None:
    data["records"][1]["include_in"] = ["full-zh"]
    report = module.validate_edition(workspace, data, "full-zh")
    assert report["errors"] == [], report
    assert any(item["code"] == "source_language_fallback" for item in report["warnings"])
    data["records"][1]["include_in"] = []


def test_required_category_gate(workspace: Path, data: dict) -> None:
    spec_path = workspace / "templates" / "academic-full.template.yaml"
    template = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    template["modules"][1]["required_categories"].append("publication")
    spec_path.write_text(yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8")
    report = module.validate_edition(workspace, data, "full-zh")
    assert any(item["code"] == "missing_required_category" for item in report["errors"])
    template["modules"][1]["required_categories"].remove("publication")
    spec_path.write_text(yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_template_category_mapping_gate(workspace: Path, data: dict) -> None:
    spec_path = workspace / "templates" / "academic-full.template.yaml"
    template = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    experience = template["modules"][2]
    experience["categories"] = ["employment"]
    experience["required_categories"] = []
    spec_path.write_text(yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8")
    report = module.validate_edition(workspace, data, "full-zh")
    assert any(item["code"] == "unmapped_category" for item in report["errors"])
    experience["categories"] = ["education"]
    experience["required_categories"] = ["education"]
    spec_path.write_text(yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_template_bookmark_gate(workspace: Path, data: dict) -> None:
    docx = module._load_docx()
    path = workspace / "templates" / "academic-full.docx"
    backup = path.read_bytes()
    document = docx.Document(path)
    bookmark = next(document._element.iter(module.WORD + "bookmarkStart"))
    bookmark.getparent().remove(bookmark)
    document.save(path)
    report = module.validate_edition(workspace, data, "full-zh")
    assert any(item["code"] == "template_bookmarks" for item in report["errors"])
    path.write_bytes(backup)


def test_template_is_anonymized(workspace: Path) -> None:
    path = workspace / "templates" / "academic-full.docx"
    forbidden = [b"Sensitive Person", b"s" + b"jran@", b"1988", b"1232025", b"GJ090301"]
    with zipfile.ZipFile(path) as archive:
        contents = b"\n".join(archive.read(name) for name in archive.namelist())
        assert not any(value.lower() in contents.lower() for value in forbidden)
        assert not any(name.startswith("customXml/") or name.startswith("word/media/")
                       for name in archive.namelist())


def test_evidence_and_docx_paragraph(root: Path) -> None:
    ok, detail = module.evidence_status("admin/raw/missing.md#fact")
    assert not ok and "exist" in detail
    try:
        docx = module._load_docx()
    except module.CvError:
        return
    path = root / "admin" / "raw" / "legacy.docx"
    path.parent.mkdir(parents=True)
    document = docx.Document()
    document.add_paragraph("one")
    document.add_paragraph("two")
    document.save(path)
    relative = path.relative_to(REPO)
    assert module.evidence_status(str(relative)) == (True, "whole-document")
    assert module.evidence_status(f"{relative}#paragraph-2")[0]
    assert not module.evidence_status(f"{relative}#paragraph-3")[0]


def test_render_collision_history_and_diff(workspace: Path, source: Path, data: dict) -> None:
    try:
        module._load_docx()
    except module.CvError:
        return
    first = module.render(workspace, source, data, "full-zh", "20260922")
    assert first["status"] == "ok", first
    first_artifact = REPO / first["artifact"]
    assert first_artifact.name.endswith("-20260922.docx")
    with zipfile.ZipFile(first_artifact) as archive:
        assert "word/document.xml" in archive.namelist()

    data["records"][1]["include_in"] = ["full-zh"]
    source.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    second = module.render(workspace, source, data, "full-zh", "20260922")
    assert second["status"] == "ok", second
    assert (REPO / second["artifact"]).name.endswith("-20260922-r2.docx")
    docx = module._load_docx()
    rendered = docx.Document(REPO / second["artifact"])
    texts = [paragraph.text for paragraph in rendered.paragraphs]
    assert texts.index("论文") < texts.index("教育经历")
    comparison = module.diff_versions(workspace, first["manifest"], second["manifest"])
    assert comparison["added"] == ["record-two"]
    assert comparison["removed"] == []
    listing = module.history(workspace)
    assert len(listing["versions"]) == 2
    index = workspace / "versions" / "index.jsonl"
    assert len(index.read_text(encoding="utf-8").splitlines()) == 2


def test_current_workspace_renders_all_selected_records() -> None:
    workspace = REPO / "projects" / "简历维护"
    if not (workspace / "cv-records.yaml").is_file():
        return
    _, source, data = module.load_workspace("简历维护")
    template_spec_path, template_docx, template, issues = module.load_template(
        workspace, data, "academic-full-zh")
    assert template_spec_path.is_file() and not issues
    forbidden = ["冉仕举", "Shi-Ju Ran", "s" + "jran@", "1988", "1232025", "GJ090301", "Ran ShiJu"]
    package_bytes = template_spec_path.read_bytes()
    with zipfile.ZipFile(template_docx) as archive:
        package_bytes += b"\n".join(archive.read(name) for name in archive.namelist())
        assert not any(name.startswith("customXml/") or name.startswith("word/media/")
                       for name in archive.namelist())
    assert not any(value.casefold().encode("utf-8") in package_bytes.lower() for value in forbidden)
    for edition_id in ("academic-full-zh", "academic-full-en"):
        report = module.validate_edition(workspace, data, edition_id)
        assert report["errors"] == [], report
        records = module.selected_records(data, edition_id)
        assert len(records) == 86
        target = workspace / f".test-{edition_id}.docx"
        try:
            mechanical = module.build_docx(target, template_docx, template, data, edition_id, records)
            assert mechanical["records"] == 86
        finally:
            target.unlink(missing_ok=True)


def test_workspace_escape(root: Path) -> None:
    original = module.PROJECTS
    module.PROJECTS = root / "projects"
    try:
        try:
            module.resolve_workspace("../../outside")
            assert False, "workspace escape should fail"
        except module.CvError:
            pass
    finally:
        module.PROJECTS = original


def main() -> None:
    (REPO / "temp").mkdir(exist_ok=True)
    root, workspace, source, data = fixture()
    original_projects = module.PROJECTS
    module.PROJECTS = root / "projects"
    try:
        loaded_workspace, loaded_source, loaded = module.load_workspace("demo")
        assert loaded_workspace == workspace.resolve()
        assert loaded_source == source.resolve()
        test_structure_and_formal_gates(workspace, loaded)
        test_source_language_fallback(workspace, loaded)
        test_required_category_gate(workspace, loaded)
        test_template_category_mapping_gate(workspace, loaded)
        test_template_bookmark_gate(workspace, loaded)
        test_template_is_anonymized(workspace)
        test_evidence_and_docx_paragraph(root)
        test_render_collision_history_and_diff(workspace, source, data)
        test_workspace_escape(root)
    finally:
        module.PROJECTS = original_projects
        shutil.rmtree(root)
    test_current_workspace_renders_all_selected_records()
    print("cv regression: PASS")


if __name__ == "__main__":
    main()

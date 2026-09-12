#!/usr/bin/env python3
"""Regression checks for manuscript-diagnosis naming and read-only discovery."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

import diagnosis_preflight as diagnosis

SKILL = Path(__file__).resolve().parents[1]
REPO = SKILL.parents[2]


class DiagnosisPreflightTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "projects" / "稿件诊断"
        self.target = self.root / "0912 PRL"
        self.target.mkdir(parents=True)
        (self.target / "manuscript.pdf").write_bytes(b"local manuscript fixture")
        (self.root / "style-library").mkdir()
        (self.root / "style-library" / "稿件诊断写作风格指南.md").write_text("style fixture")
        (self.root / "STYLE.md").write_text("profile fixture")

    def test_new_root_and_style_paths(self):
        result = diagnosis.preflight(str(self.target))
        self.assertTrue(result["ok"])
        self.assertEqual(result["journal_hint"], "PRL")
        self.assertEqual(result["manuscripts"], ["0912 PRL/manuscript.pdf"])
        self.assertEqual(result["base_style_guide"], "style-library/稿件诊断写作风格指南.md")
        self.assertEqual(result["style_profile"], "STYLE.md")
        self.assertEqual(result["style_samples"], [])

    def test_missing_private_style_files_are_optional(self):
        (self.root / "STYLE.md").unlink()
        (self.root / "style-library" / "稿件诊断写作风格指南.md").unlink()
        result = diagnosis.preflight(str(self.target))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["base_style_guide"])
        self.assertIsNone(result["style_profile"])
        self.assertEqual(result["style_samples"], [])

    def test_new_report_names_and_existing_english_samples(self):
        for name in ("Manuscript_Diagnosis_draft.md", "稿件诊断意见.txt", "notes.md"):
            (self.target / name).write_text("report fixture")
        prior = self.root / "0801 PRA"
        prior.mkdir()
        for name in ("Reviewer_Report_revised_ran.docx", "Reviewer_Report_revised_ran.md",
                     "diagnosis_revised.txt", "report_draft.md", "paper.tex"):
            (prior / name).write_text("historical fixture")
        result = diagnosis.preflight(str(self.target))
        self.assertEqual(set(result["existing_reports"]), {
            "0912 PRL/Manuscript_Diagnosis_draft.md", "0912 PRL/稿件诊断意见.txt"})
        self.assertEqual(result["style_samples"], [
            "0801 PRA/Reviewer_Report_revised_ran.md",
            "0801 PRA/diagnosis_revised.txt", "0801 PRA/report_draft.md"])

    def test_discovery_does_not_read_or_change_document_bodies(self):
        def snapshot():
            return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in self.root.rglob("*") if p.is_file()}
        before = snapshot()
        with patch.object(Path, "read_text", side_effect=AssertionError("unexpected body read")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected body read")):
            self.assertTrue(diagnosis.preflight(str(self.target))["ok"])
        self.assertEqual(snapshot(), before)

    def test_outside_workspace_is_rejected(self):
        outside = self.base / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "projects/稿件诊断"):
            diagnosis.preflight(str(outside))

    def test_symlink_cannot_escape_workspace(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "projects/稿件诊断"):
            diagnosis.preflight(str(self.root / "escape"))

    def test_missing_target_is_rejected_without_creation(self):
        target = self.root / "missing"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            diagnosis.preflight(str(target))
        self.assertFalse(target.exists())

    def test_no_manuscript_remains_a_visible_failure(self):
        (self.target / "manuscript.pdf").unlink()
        result = diagnosis.preflight(str(self.target))
        self.assertFalse(result["ok"])
        self.assertEqual(result["warnings"], ["no PDF or TeX manuscript found"])

    def test_multiple_candidates_are_exposed_not_silently_chosen(self):
        (self.target / "supplement.tex").write_text("supplement")
        result = diagnosis.preflight(str(self.target))
        self.assertEqual(len(result["manuscripts"]), 2)

    def test_cli_json_and_error_exit(self):
        script = SKILL / "scripts" / "diagnosis_preflight.py"
        good = subprocess.run([sys.executable, str(script), str(self.target)],
                              capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertTrue(json.loads(good.stdout)["ok"])
        bad = subprocess.run([sys.executable, str(script), str(self.base)],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertFalse(json.loads(bad.stdout)["ok"])


class DiagnosisIntegrationTest(unittest.TestCase):
    def test_skill_identity_and_interface_match(self):
        text = (SKILL / "SKILL.md").read_text()
        metadata = yaml.safe_load(text.split("---", 2)[1])
        interface = yaml.safe_load((SKILL / "agents" / "openai.yaml").read_text())["interface"]
        self.assertEqual(metadata["name"], "manuscript-diagnosis")
        self.assertEqual(SKILL.name, metadata["name"])
        self.assertEqual(interface["display_name"], "稿件诊断")
        self.assertIn("$manuscript-diagnosis", interface["default_prompt"])
        self.assertIn("Manuscript_Diagnosis_draft.md", text)
        self.assertIn("## Core Literature Gate", text)
        self.assertIn("Do not upload", text)
        self.assertTrue(25 <= len(interface["short_description"]) <= 64)

    def test_functional_surface_has_no_retired_label(self):
        retired_label = "\u5ba1\u7a3f"
        files = [REPO / ".gitignore", REPO / "operations/engineering/graph.yaml"]
        files += [p for p in SKILL.rglob("*") if p.suffix in {".md", ".py", ".yaml"}]
        for path in files:
            with self.subTest(path=path):
                self.assertNotIn(retired_label, path.as_posix())
                self.assertNotIn(retired_label, path.read_text())
        project = REPO / "projects" / "稿件诊断"
        for path in [project / "STYLE.md", project / "style-library/稿件诊断写作风格指南.md"]:
            if not path.exists():
                continue  # Local profiles are deliberately absent from public releases.
            for line in path.read_text().splitlines():
                if retired_label in line:
                    # Original evidence addresses are immutable provenance, not feature labels.
                    self.assertTrue("academic/raw/" in line or "academic/wiki/" in line, line)

    def test_engineering_references_exist(self):
        graph = yaml.safe_load((REPO / "operations/engineering/graph.yaml").read_text())
        for node in ("manuscript_diagnosis_skill", "manuscript_diagnosis_preflight",
                     "manuscript_diagnosis_test"):
            with self.subTest(node=node):
                self.assertTrue((REPO / graph["nodes"][node]["path"]).is_file())
        self.assertEqual(graph["script_contracts"]["manuscript_diagnosis_preflight"]["writes"], [])

    def test_skill_files_are_explicitly_allowlisted(self):
        manifest = yaml.safe_load((REPO / "operations/engineering/open-source-manifest.yaml").read_text())
        files = ["SKILL.md", "agents/openai.yaml", "scripts/diagnosis_preflight.py",
                 "scripts/test_diagnosis_preflight.py"]
        for name in files:
            path = (SKILL / name).relative_to(REPO).as_posix()
            self.assertIn(path, manifest["include"])
        self.assertFalse(any(p.startswith("projects/") for p in manifest["include"]))

    def test_skill_is_not_git_ignored(self):
        # Public build directories may not yet have Git metadata.
        if not (REPO / ".git").exists():
            self.skipTest("release tree has no Git metadata yet")
        result = subprocess.run(["git", "check-ignore", "--no-index", str(SKILL / "SKILL.md")],
                                cwd=REPO, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
